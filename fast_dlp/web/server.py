"""fast-dlp web: a downloader page for every link the command line takes.

Nothing is stored on the server. A download is first planned (formats' headers and indexes
only, a fraction of a second), which gives a one-time token, the file name and the exact
size. GET /dl/<token> then streams the file straight from googlevideo through the muxer into
the response (other sites: see targets.py). After the last byte the server closes its side and waits for the browser to
close too: that FIN only comes once the browser has read everything, so the download is
marked "delivered" on proof, not on hope. The page polls /api/status for its tokens.

    fast-dlp web [--host 127.0.0.1] [--port 8000]
"""
import argparse
import json
import mimetypes
import os
import re
import secrets
import socket
import sys
import threading
import time
import types
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import formats as fmt, lyrics, net, postprocess, search, section, stream, utils, ytmeta
from ..utils import FastDlpError
from . import extras, targets, texts
from .texts import WebError

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
VIDEO_TTL = 15 * 60          # player answers (stream URLs) are reused this long
JOB_TTL = 60 * 60            # a token lives this long
CONFIRM_TIMEOUT = 60         # seconds to wait for the browser's FIN after the last byte

TEMPLATES = ("{title}", "{author} - {title}", "{title} [{id}]", "{title} ({height}p)")
LIST_LIMIT = 500             # rows of a list sent to the page
FORMAT_SPEC = re.compile(r"^[\w-]{1,24}(\+[\w-]{1,24})?$")
LIVE_MAX = 12 * 3600         # the longest live recording one request may ask for
MP3_QUALITIES = ("320k", "256k", "192k", "128k", "V0", "V2")
CODECS = ("", "avc1", "av01", "vp09")


class Job:
    def __init__(self, token, video, opts, output, chosen, name):
        self.token = token
        self.video_id = video.id
        self.title = video.title
        self.opts = opts
        self.output = output
        self.chosen = chosen
        self.filename = name
        self.size = output.size
        self.state = "ready"
        self.sent = 0
        self.error = None
        self.created = time.time()
        self.started = self.finished = None
        self.attempts = 0
        self.samples = []            # (time, sent) for the speed
        self.cond = threading.Condition()
        self.stop = threading.Event()  # live recordings: the page's Stop button
        self.live = False

    def update(self, **fields):
        with self.cond:
            for k, v in fields.items():
                setattr(self, k, v)
            now = time.time()
            self.samples.append((now, self.sent))
            self.samples = [s for s in self.samples if now - s[0] <= 2.0]
            self.cond.notify_all()

    def speed(self):
        if len(self.samples) < 2:
            return 0
        (t0, b0), (t1, b1) = self.samples[0], self.samples[-1]
        return (b1 - b0) / (t1 - t0) if t1 > t0 else 0

    def public(self):
        return {"token": self.token, "state": self.state, "sent": self.sent, "size": self.size,
                "speed": round(self.speed()), "filename": self.filename, "title": self.title,
                "error": self.error, "attempts": self.attempts, "live": self.live,
                "stopping": self.stop.is_set(),
                "elapsed": round((self.finished or time.time()) - self.started, 2) if self.started else None,
                "itags": [f["itag"] for f in self.chosen], "download": f"/dl/{self.token}"}


class App:
    def __init__(self, ffmpeg=None):
        self._extractor = None
        self.ffmpeg = ffmpeg
        self.lock = threading.Lock()
        self.videos = {}             # id -> (time, Video)
        self.targets = {}            # input text -> (time, list title, [job])
        self.jobs = {}
        self.args = targets.cli_args()

    @property
    def extractor(self):
        with self.lock:
            if self._extractor is None:
                from ..extractor import Extractor
                self._extractor = Extractor()
            return self._extractor

    def video(self, text, fresh=False):
        vid = utils.extract_video_id(text or "")
        if not vid:
            raise WebError("not_youtube")
        with self.lock:
            hit = self.videos.get(vid)
        if hit and not fresh and time.time() - hit[0] < VIDEO_TTL:
            return hit[1]
        video = self.extractor.extract(vid)
        with self.lock:
            self.videos[vid] = (time.time(), video)
        return video

    def sweep(self):
        now = time.time()
        with self.lock:
            for token, job in list(self.jobs.items()):
                if now - job.created > JOB_TTL and job.state != "streaming":
                    del self.jobs[token]
            for vid, (t, _) in list(self.videos.items()):
                if now - t > VIDEO_TTL:
                    del self.videos[vid]
            for key, hit in list(self.targets.items()):
                if now - hit[0] > VIDEO_TTL:
                    del self.targets[key]

    def target(self, text, fresh=False):
        """Input text -> (list title or None, [job]), expanded like `fast-dlp <text>`."""
        key = (text or "").strip()
        with self.lock:
            hit = self.targets.get(key)
        if hit and not fresh and time.time() - hit[0] < VIDEO_TTL:
            return hit[1], hit[2]
        vid = utils.extract_video_id(key) if search.looks_like_target(key) else None
        if vid and not re.search(r"[?&]list=", key):
            title, jobs = None, [{"source": "youtube", "video_id": vid, "playlist": None, "url": key}]
        else:
            title, jobs = targets.expand(key, self.args)
        with self.lock:
            self.targets[key] = (time.time(), title, jobs)
        return title, jobs

    def pick_job(self, body):
        title, jobs = self.target(body.get("url"))
        n = body.get("entry")
        if n in (None, ""):
            if title is not None:
                raise WebError("pick_entry")
            return jobs[0]
        n = _int(n, 0, len(jobs) - 1)
        if n is None:
            raise WebError("no_entry")
        return jobs[n]

    def job_video(self, job, fresh=False):
        """A YouTube job or a track (Spotify...) -> the YouTube Video to stream."""
        vid = job.get("video_id") or job.get("_video_id")
        if not vid:
            from ..cli import resolve_spotify
            video = resolve_spotify(self.extractor, job["track"], targets.site(job))
            with self.lock:
                job["_video_id"] = video.id
                self.videos[video.id] = (time.time(), video)
            vid = video.id
        video = self.video(vid, fresh)
        track = job.get("track")
        if track:
            video.title = track["title"]
            video.author = ", ".join(track.get("artists") or []) or video.author
        return video

    def job_spec(self, job, fresh=False):
        """The download spec of a direct job (None: a Yandex preview, played from YouTube)."""
        with self.lock:
            hit = job.get("_spec")
        if hit and not fresh and time.time() - hit[0] < VIDEO_TTL:
            return hit[1]
        spec = targets.resolve_spec(job)
        with self.lock:
            if spec is None and job.get("fallback_track"):
                job.update(source="match", track=job["fallback_track"])
            job["_spec"] = (time.time(), spec)
        return spec

    # -- API ------------------------------------------------------------------------------------
    def info(self, url):
        title, jobs = self.target(url)
        if title is not None:
            first = jobs[0]
            searched = not search.looks_like_target(url.strip())
            return {"kind": "list", "title": title, "count": len(jobs), "search": searched,
                    "query": url.strip() if searched else None,
                    "site": "all" if searched else targets.site(first),
                    "entries": [targets.entry(n, j) for n, j in enumerate(jobs[:LIST_LIMIT])],
                    "ffmpeg": bool(self.ffmpeg), "templates": TEMPLATES}
        job = jobs[0]
        if targets.kind(job) == "direct":
            spec = self.job_spec(job)
            if spec is not None:
                return self.file_info(job, spec)
        return self.video_info(job)

    def file_info(self, job, spec):
        size = None
        if spec["kind"] == "file":
            try:
                size, ranges = targets.probe(spec)
                spec["size"], spec["ranges"] = size, ranges
            except FastDlpError:
                size = None
        s = targets.site(job)
        return {"kind": "file", "id": job["id"], "title": job["title"], "author": job.get("author") or "",
                "site": s, "duration": job.get("duration") or 0,
                "thumbnail": job.get("cover_url"), "audio": bool(job.get("audio")),
                "container": "HLS" if spec["kind"] == "hls" else (spec.get("ext") or "bin").upper(),
                "size": size or None, "ffmpeg": bool(self.ffmpeg), "templates": TEMPLATES}

    def video_info(self, job):
        video = self.job_video(job)
        details = video.raw.get("videoDetails") or {}
        thumbs = (details.get("thumbnail") or {}).get("thumbnails") or []
        videos, audios = [], []
        for f in video.formats:
            if fmt.size(f) <= 0 or "initRange" not in f:
                continue
            if fmt.kind(f) == "video":
                videos.append({"itag": f["itag"], "container": fmt.ext(f), "height": f.get("height") or 0,
                               "width": f.get("width") or 0, "fps": f.get("fps") or 0,
                               "codec": fmt.codec(f).split(".")[0], "size": fmt.size(f),
                               "bitrate": f.get("bitrate") or 0, "hdr": "HDR" in (f.get("qualityLabel") or "")})
            elif (f.get("audioTrack") or {}).get("audioIsDefault", True):
                audios.append({"itag": f["itag"], "ext": fmt.ext(f), "codec": fmt.codec(f).split(".")[0],
                               "size": fmt.size(f), "bitrate": f.get("bitrate") or 0,
                               "drc": fmt.is_drc(f)})
        track = job.get("track") or {}
        s = targets.site(job)
        thumb = thumbs[-1]["url"] if thumbs else f"https://i.ytimg.com/vi/{video.id}/hqdefault.jpg"
        captions, translations = extras.captions(video)
        return {"kind": "video", "id": video.id, "title": video.title, "author": video.author,
                "duration": video.duration, "views": 0 if track else int(details.get("viewCount") or 0),
                "site": s, "track": bool(track), "live": bool(details.get("isLive")),
                "thumbnail": track.get("cover") or thumb,
                "formats": self.format_rows(video), "captions": captions, "translations": translations,
                "sponsor": list(extras.SPONSOR), "description": bool(details.get("shortDescription")),
                "videos": sorted(videos, key=lambda v: (-v["height"], -v["fps"], v["container"], v["codec"])),
                "audios": sorted(audios, key=lambda a: -a["bitrate"]),
                "ffmpeg": bool(self.ffmpeg), "templates": TEMPLATES}

    def options(self, body):
        o = {}
        o["mode"] = body.get("mode") if body.get("mode") in ("video", "audio", "video_only", "original") else "video"
        o["container"] = body.get("container") if body.get("container") in ("mp4", "webm") else "mp4"
        o["audio_format"] = body.get("audio_format") if body.get("audio_format") in ("m4a", "opus", "mp3") else "m4a"
        o["codec"] = body.get("codec") if body.get("codec") in CODECS else ""
        o["height"] = _int(body.get("height"), 0, 4320) or None
        o["fps"] = _int(body.get("fps"), 0, 120) or None
        o["mp3_quality"] = body.get("mp3_quality") if body.get("mp3_quality") in MP3_QUALITIES else "320k"
        o["connections"] = _int(body.get("connections"), 1, 32) or 16
        o["tags"] = bool(body.get("tags", True))
        o["cover"] = bool(body.get("cover", o["mode"] == "audio"))
        template = str(body.get("template") or TEMPLATES[0])[:200]
        o["template"] = template
        start = end = None
        try:
            if body.get("start") not in (None, ""):
                start = section.parse_time(str(body["start"]))
            if body.get("end") not in (None, ""):
                end = section.parse_time(str(body["end"]))
        except ValueError:
            raise WebError("bad_time")
        if start is not None and end is not None and end <= start:
            raise WebError("time_order")
        o["start"], o["end"] = (start or 0.0, end) if (start or end) else (None, None)
        spec = str(body.get("format") or "").strip()
        if spec and not FORMAT_SPEC.match(spec):
            raise WebError("bad_format")
        o["format"] = spec
        o["chapters"] = bool(body.get("chapters"))
        o["sponsor"] = extras.sponsor_list(body.get("sponsor"))
        o["lyrics"] = bool(body.get("lyrics"))
        o["replaygain"] = bool(body.get("replaygain", True))
        o["live_duration"] = _int(body.get("live_duration"), 0, LIVE_MAX) or None
        o["live_from_start"] = bool(body.get("live_from_start"))
        return o

    @staticmethod
    def format_rows(video):
        """The -F table as data: every format the page can pick by hand."""
        rows = []
        for f in video.formats:
            if fmt.size(f) <= 0 or "initRange" not in f:
                continue
            track = f.get("audioTrack") or {}
            rows.append({"id": fmt.format_id(f), "kind": fmt.kind(f), "ext": fmt.ext(f),
                         "container": "webm" if fmt.ext(f) == "webm" else "mp4",
                         "quality": fmt.quality(f) if fmt.kind(f) == "video" else "",
                         "codec": fmt.codec_name(f), "bitrate": f.get("averageBitrate") or f.get("bitrate") or 0,
                         "size": fmt.size(f), "drc": fmt.is_drc(f), "lang": track.get("displayName") or "",
                         "channels": f.get("audioChannels") or 0, "height": f.get("height") or 0})
        rows.sort(key=lambda r: (r["kind"] != "video", -r["height"], r["drc"], r["lang"] != "", -r["bitrate"]))
        return rows

    def create(self, body):
        opts = self.options(body)
        job = self.pick_job(body)
        if targets.kind(job) == "direct":
            spec = self.job_spec(job)
            if spec is not None:
                return self.create_direct(job, spec, opts)
        if opts["mode"] == "original":
            opts["mode"] = "audio" if job.get("track") else "video"
        video = self.job_video(job)
        if (video.raw.get("videoDetails") or {}).get("isLive"):
            return self.create_live(job, video, opts)
        meta = self.meta(video, opts, job) if opts["tags"] or opts["cover"] else {}
        self.add_extras(meta, video, opts, job)
        meta = meta or None
        try:
            output, chosen = stream.prepare(video, opts, meta, self.ffmpeg)
        except FastDlpError as e:
            if "HTTP 403" not in str(e):
                raise
            video = self.job_video(job, fresh=True)                 # stale URLs: ask once more
            output, chosen = stream.prepare(video, opts, meta, self.ffmpeg)
        name = self.file_name(opts["template"], video, chosen, output.ext, job)
        token = secrets.token_urlsafe(16)
        job = Job(token, video, opts, output, chosen, name)
        with self.lock:
            self.jobs[token] = job
        return job

    def add_extras(self, meta, video, opts, job):
        """--embed-chapters / --sponsorblock-mark and lyrics, into the streamed file."""
        if (opts["chapters"] or opts["sponsor"]) and opts["start"] is None:
            found = extras.chapters(video, opts["sponsor"])
            if found:
                meta["_chapters"] = found
        audio = opts["mode"] == "audio" or bool(job.get("track"))
        if opts["lyrics"] and audio and opts["tags"]:
            track = job.get("track") or {}
            text, _ = extras.lyrics_text(track.get("title") or video.title,
                                         ", ".join(track.get("artists") or []) or video.author,
                                         track.get("album"), video.duration)
            if text:
                meta["lyrics"] = text
        if not opts["replaygain"]:
            meta.pop("replaygain", None)

    def file_name(self, template, video, chosen, ext, job):
        """The same fields as the command line's -o (upload date only when the template asks)."""
        upload = None
        if postprocess.template_fields(template) & set(postprocess.DATE_FIELDS):
            try:
                upload = ytmeta.Watch(video.id).upload_date()
            except Exception:                    # the date is a nicety; never fail a download on it
                upload = None
        values = postprocess.fields(video, chosen, stream.file_ext(ext), job, upload)
        return stream.filename(template, values, ext)

    def create_live(self, job, video, opts):
        token = secrets.token_urlsafe(16)
        stop = threading.Event()
        output = targets.live_output(video, opts, self.ffmpeg, stop.is_set)
        stamp = time.strftime("%Y-%m-%d %H-%M")
        name = self.file_name(opts["template"], video, [], output.ext, job)
        stem, dot, ext = name.rpartition(".")
        result = Job(token, video, opts, output, [], f"{stem} [live {stamp}].{ext}" if dot else name)
        result.stop, result.live = stop, True
        with self.lock:
            self.jobs[token] = result
        return result

    def urls(self, body):
        """--get-url: what a download with these options would fetch, without fetching it."""
        opts = self.options(body)
        job = self.pick_job(body)
        if targets.kind(job) == "direct":
            spec = self.job_spec(job)
            if spec is not None:
                if spec["kind"] == "steamcmd":
                    raise WebError("steam")
                return [{"url": spec["url"], "label": spec["kind"].upper()}]
        video = self.job_video(job)
        if (video.raw.get("videoDetails") or {}).get("isLive"):
            return [{"url": (video.raw.get("streamingData") or {}).get("hlsManifestUrl"), "label": "HLS"}]
        if opts["mode"] == "original":
            opts["mode"] = "audio" if job.get("track") else "video"
        return [{"url": f["url"], "label": f"{fmt.format_id(f)} · {fmt.ext(f)} · "
                                          f"{fmt.quality(f) if fmt.kind(f) == 'video' else fmt.codec_name(f)}"}
                for f in stream.pick(video, opts)]

    def extra(self, params):
        """A side file: (bytes, name, content type)."""
        kind = params.get("kind") or ""
        extras.check_kind(kind)
        job = self.pick_job(params)
        if targets.kind(job) == "direct" and self.job_spec(job) is not None:
            return extras.direct_file(job, kind)
        return extras.youtube_file(self.job_video(job), kind, params, job.get("track"))

    def create_direct(self, job, spec, opts):
        meta = None
        if opts["mode"] == "audio" and opts["audio_format"] == "mp3" and (opts["tags"] or opts["cover"]):
            meta = {}
            if opts["tags"]:
                meta = {k: v for k, v in (job.get("meta") or {}).items() if v}
                meta.setdefault("title", job["title"])
                if job.get("author"):
                    meta.setdefault("artist", job["author"])
            if opts["cover"] and job.get("cover_url"):
                from ..cli import _image
                cover, cover_mime = _image(job["cover_url"])
                if cover:
                    meta["cover"], meta["cover_mime"] = cover, cover_mime
            if opts["lyrics"] and opts["tags"] and job.get("audio"):
                text, _ = extras.lyrics_text(job["title"], job.get("author") or "",
                                             (job.get("meta") or {}).get("album"), job.get("duration"))
                if text:
                    meta["lyrics"] = text
        try:
            output = targets.direct_output(job, spec, opts, meta, self.ffmpeg)
        except FastDlpError as e:
            if "HTTP 403" not in str(e) and "HTTP 410" not in str(e):
                raise
            spec = self.job_spec(job, fresh=True)                   # the link expired: resolve again
            output = targets.direct_output(job, spec, opts, meta, self.ffmpeg)
        fields = {"title": job["title"], "author": job.get("author") or "", "uploader": job.get("author") or "",
                  "id": job["id"], "height": "", "fps": "", "ext": stream.file_ext(output.ext)}
        name = stream.filename(opts["template"], fields, output.ext)
        token = secrets.token_urlsafe(16)
        item = types.SimpleNamespace(id=job["id"], title=job["title"])
        result = Job(token, item, opts, output, [], name)
        with self.lock:
            self.jobs[token] = result
        return result

    def meta(self, video, opts, job=None):
        if job and job.get("track"):
            from ..cli import build_meta
            meta = build_meta(job, video, opts["cover"])
            if not opts["tags"]:
                meta = {k: v for k, v in meta.items() if k in ("cover", "cover_mime")}
            return meta
        meta = {}
        if opts["tags"]:
            meta.update(title=video.title, artist=video.author, comment=f"https://youtu.be/{video.id}")
            if job and job.get("playlist"):
                meta.update(album=job["playlist"], track=job.get("position"))
            if opts["mode"] == "audio":
                loud = ((video.raw.get("playerConfig") or {}).get("audioConfig") or {}).get("trackAbsoluteLoudnessLkfs")
                if loud is not None:
                    meta["replaygain"] = lyrics.replaygain(loud)
        if opts["cover"]:
            for name in ("maxresdefault", "hqdefault"):
                status, body = net.request(f"https://i.ytimg.com/vi/{video.id}/{name}.jpg", timeout=15)
                if status == 200 and len(body) > 1000:
                    meta["cover"], meta["cover_mime"] = body, "image/jpeg"
                    break
        return meta

    def job(self, token):
        with self.lock:
            return self.jobs.get(token)


def _int(value, low, high):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return max(low, min(high, n))


def _disposition(name):
    ascii_name = re.sub(r'[^\x20-\x7e]|["\\]', "_", name)
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{urllib.parse.quote(name, safe='')}"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "fast-dlp"
    app = None                       # set by serve()

    def log_message(self, fmt_, *args):
        if self.app.verbose:
            sys.stderr.write(f"[web] {self.address_string()} {fmt_ % args}\n")

    # -- plumbing ---------------------------------------------------------------------------------
    def _send(self, status, body, ctype="application/json; charset=utf-8", extra=None):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _error(self, status, message):
        """message: text, or an exception shown in the page's language."""
        if isinstance(message, BaseException):
            if isinstance(message, FastDlpError):
                message = texts.message(message, texts.language(self.headers))
            elif isinstance(message, OSError):
                message = WebError("network", error=utils.net_error(message)).text(texts.language(self.headers))
            else:
                message = WebError("internal", error=repr(message)).text(texts.language(self.headers))
        self._send(status, {"error": message})

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path, _, query = self.path.partition("?")
        params = urllib.parse.parse_qs(query)
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path in ("/logo.svg", "/favicon.svg"):
                return self._static("logo.svg")
            if path == "/api/info":
                return self._send(200, self.app.info((params.get("url") or [""])[0]))
            if path == "/api/extra":
                flat = {k: v[0] for k, v in params.items()}
                data, name, ctype = self.app.extra(flat)
                return self._send(200, data, ctype, {"Content-Disposition": _disposition(name)})
            if path == "/api/status":
                # One short request for every download on the page: a browser keeps only six
                # connections per site, and each running download already holds one.
                tokens = ",".join(params.get("t") or []).split(",")[:50]
                jobs = {t: self.app.job(t) for t in tokens if t}
                return self._send(200, {t: (j.public() if j else None) for t, j in jobs.items()})
            m = re.fullmatch(r"/api/jobs/([\w-]+)", path)
            if m:
                job = self.app.job(m.group(1))
                if not job:
                    return self._error(404, WebError("no_job"))
                return self._send(200, job.public())
            m = re.fullmatch(r"/dl/([\w-]+)", path)
            if m:
                return self._download(m.group(1))
            self._error(404, "not found")
        except FastDlpError as e:
            self._error(400, e)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except OSError as e:                     # a site did not answer: say so, keep serving
            self._error(502, e)
        except Exception as e:
            self.app.log(f"{self.path}: {e!r}")
            self._error(500, e)

    def do_POST(self):
        m = re.fullmatch(r"/api/jobs/([\w-]+)/stop", self.path)
        if m:
            job = self.app.job(m.group(1))
            if not job:
                return self._error(404, WebError("no_job"))
            job.stop.set()
            return self._send(200, job.public())
        if self.path not in ("/api/jobs", "/api/urls"):
            return self._error(404, "not found")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("not an object")
        except ValueError:                       # JSONDecodeError is a ValueError
            return self._error(400, WebError("bad_json"))
        try:
            self.app.sweep()
            if self.path == "/api/urls":
                return self._send(200, {"urls": self.app.urls(body)})
            job = self.app.create(body)
            self._send(200, job.public())
        except FastDlpError as e:
            self._error(400, e)
        except OSError as e:
            self._error(502, e)
        except Exception as e:
            self.app.log(f"POST {self.path}: {e!r}")
            self._error(500, e)

    def _static(self, name):
        with open(os.path.join(STATIC, name), "rb") as fh:
            data = fh.read()
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("svg+xml"):
            ctype += "; charset=utf-8"
        self._send(200, data, ctype)

    # -- the stream ----------------------------------------------------------------------------------
    def _download(self, token):
        job = self.app.job(token)
        if not job:
            return self._error(404, WebError("no_job"))
        if self.command == "HEAD":
            return self._headers(job)
        with job.cond:
            if job.state in ("delivered", "unconfirmed"):
                return self._error(410, WebError("delivered"))
            if job.state in ("streaming", "confirming"):
                return self._error(409, WebError("busy"))
            job.state = "streaming"
        job.update(sent=0, started=time.time(), finished=None, error=None, attempts=job.attempts + 1)
        self.close_connection = True
        self._headers(job)
        chunked = job.size is None
        sent = 0
        try:
            last = 0.0
            for block in job.output.chunks():
                if chunked:
                    self.wfile.write(b"%x\r\n" % len(block) + bytes(block) + b"\r\n")
                else:
                    self.wfile.write(block)
                sent += len(block)
                now = time.time()
                if now - last > 0.2:
                    job.update(sent=sent)
                    last = now
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout):
            job.update(sent=sent, state="aborted", error="the browser stopped the download",
                       finished=time.time())
            return
        except Exception as e:                   # headers are gone: closing tells the browser it failed
            job.update(sent=sent, state="failed", error=str(e) or e.__class__.__name__, finished=time.time())
            self.app.log(f"{token}: {e!r}")
            return
        job.update(sent=sent, state="confirming")
        job.update(state=self._confirm(), finished=time.time())

    def _headers(self, job):
        self.send_response(200)
        self.send_header("Content-Type", job.output.mime)
        self.send_header("Content-Disposition", _disposition(job.filename))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if job.size is None:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", str(job.size))
        self.send_header("Connection", "close")
        self.end_headers()

    def _confirm(self):
        """Close our side, then wait for the browser to close its side. A clean FIN means it
        read the whole body (it only closes after the last byte); a reset means it did not."""
        sock = self.connection
        try:
            sock.shutdown(socket.SHUT_WR)
            sock.settimeout(CONFIRM_TIMEOUT)
            while True:
                if not sock.recv(65536):
                    return "delivered"
        except socket.timeout:
            return "unconfirmed"
        except OSError:
            return "aborted"


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # Browsers drop idle keep-alive connections all the time: not worth a traceback.
        if isinstance(sys.exc_info()[1], (ConnectionError, socket.timeout)):
            return
        super().handle_error(request, client_address)


def serve(host="127.0.0.1", port=8000, verbose=False):
    app = App(ffmpeg=utils.find_tool("ffmpeg"))
    app.verbose = verbose
    app.log = (lambda msg: sys.stderr.write(f"[web] {msg}\n")) if verbose else (lambda msg: None)
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = Server((host, port), handler)
    shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
    print(f"fast-dlp web: http://{shown}:{httpd.server_address[1]}/  (Ctrl+C to stop)", file=sys.stderr)
    if not app.ffmpeg:
        print("note: ffmpeg not found, MP3 is off", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main(argv=None):
    p = argparse.ArgumentParser(prog="fast-dlp web", description="downloader page for every link fast-dlp "
                                                                   "takes; streams, stores nothing")
    p.add_argument("--host", default="127.0.0.1", help="address to listen on (0.0.0.0 for the LAN)")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("-4", dest="ipv4", action="store_true", help="talk to YouTube over IPv4")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    utils.VERBOSE = args.verbose
    if args.ipv4:
        net.set_family(socket.AF_INET)
    serve(args.host, args.port, args.verbose)
    return 0
