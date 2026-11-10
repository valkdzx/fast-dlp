"""fast-dlp web: a YouTube downloader page.

Nothing is stored on the server. A download is first planned (formats' headers and indexes
only, a fraction of a second), which gives a one-time token, the file name and the exact
size. GET /dl/<token> then streams the file straight from googlevideo through the muxer into
the response. After the last byte the server closes its side and waits for the browser to
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
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import formats as fmt, lyrics, net, section, stream, utils
from ..utils import FastDlpError

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
VIDEO_TTL = 15 * 60          # player answers (stream URLs) are reused this long
JOB_TTL = 60 * 60            # a token lives this long
CONFIRM_TIMEOUT = 60         # seconds to wait for the browser's FIN after the last byte

TEMPLATES = ("{title}", "{author} - {title}", "{title} [{id}]", "{title} ({height}p)")
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
                "error": self.error, "attempts": self.attempts,
                "elapsed": round((self.finished or time.time()) - self.started, 2) if self.started else None,
                "itags": [f["itag"] for f in self.chosen], "download": f"/dl/{self.token}"}


class App:
    def __init__(self, ffmpeg=None):
        self._extractor = None
        self.ffmpeg = ffmpeg
        self.lock = threading.Lock()
        self.videos = {}             # id -> (time, Video)
        self.jobs = {}

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
            raise FastDlpError("это не ссылка на видео YouTube")
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

    # -- API ------------------------------------------------------------------------------------
    def info(self, url):
        video = self.video(url)
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
                               "drc": "drc" in f.get("xtags", "")})
        return {"id": video.id, "title": video.title, "author": video.author, "duration": video.duration,
                "views": int(details.get("viewCount") or 0),
                "thumbnail": thumbs[-1]["url"] if thumbs else f"https://i.ytimg.com/vi/{video.id}/hqdefault.jpg",
                "videos": sorted(videos, key=lambda v: (-v["height"], -v["fps"], v["container"], v["codec"])),
                "audios": sorted(audios, key=lambda a: -a["bitrate"]),
                "ffmpeg": bool(self.ffmpeg), "templates": TEMPLATES}

    def options(self, body):
        o = {}
        o["mode"] = body.get("mode") if body.get("mode") in ("video", "audio", "video_only") else "video"
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
            raise FastDlpError("время отрывка: 3:40, 220 или 3m40s")
        if start is not None and end is not None and end <= start:
            raise FastDlpError("конец отрывка раньше начала")
        o["start"], o["end"] = (start or 0.0, end) if (start or end) else (None, None)
        return o

    def create(self, body):
        opts = self.options(body)
        video = self.video(body.get("url"))
        meta = self.meta(video, opts) if opts["tags"] or opts["cover"] else None
        try:
            output, chosen = stream.prepare(video, opts, meta, self.ffmpeg)
        except FastDlpError as e:
            if "HTTP 403" not in str(e):
                raise
            video = self.video(body.get("url"), fresh=True)         # stale URLs: ask once more
            output, chosen = stream.prepare(video, opts, meta, self.ffmpeg)
        v = chosen[0] if fmt.kind(chosen[0]) == "video" else None
        fields = {"title": video.title, "author": video.author, "uploader": video.author, "id": video.id,
                  "height": (v or {}).get("height") or "audio", "fps": (v or {}).get("fps") or "",
                  "ext": stream.file_ext(output.ext)}
        name = stream.filename(opts["template"], fields, output.ext)
        token = secrets.token_urlsafe(16)
        job = Job(token, video, opts, output, chosen, name)
        with self.lock:
            self.jobs[token] = job
        return job

    def meta(self, video, opts):
        meta = {}
        if opts["tags"]:
            meta.update(title=video.title, artist=video.author, comment=f"https://youtu.be/{video.id}")
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
                    return self._error(404, "нет такой загрузки (ссылка одноразовая и живёт час)")
                return self._send(200, job.public())
            m = re.fullmatch(r"/dl/([\w-]+)", path)
            if m:
                return self._download(m.group(1))
            self._error(404, "not found")
        except FastDlpError as e:
            self._error(400, str(e))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    def do_POST(self):
        if self.path != "/api/jobs":
            return self._error(404, "not found")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
            self.app.sweep()
            job = self.app.create(body)
            self._send(200, job.public())
        except (ValueError, json.JSONDecodeError):
            self._error(400, "bad JSON")
        except FastDlpError as e:
            self._error(400, str(e))

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
            return self._error(404, "нет такой загрузки (ссылка одноразовая и живёт час)")
        if self.command == "HEAD":
            return self._headers(job)
        with job.cond:
            if job.state in ("delivered", "unconfirmed"):
                return self._error(410, "файл уже доставлен; ссылка одноразовая")
            if job.state in ("streaming", "confirming"):
                return self._error(409, "этот файл уже передаётся")
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
            job.update(sent=sent, state="aborted", error="браузер прервал загрузку; ссылку можно открыть снова",
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
    p = argparse.ArgumentParser(prog="fast-dlp web", description="YouTube downloader page; streams, stores nothing")
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
