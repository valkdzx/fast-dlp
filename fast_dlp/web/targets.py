"""Everything the command line takes, for the web page.

A link (YouTube, Spotify, Yandex Music, VK, SoundCloud, TikTok, Pinterest, Pornhub, a direct
file or .m3u8), a YouTube playlist / channel / ytsearch: query, or plain words searched on
every source - expanded by the same code as `fast-dlp <link>`. Each resulting job is either
a YouTube video (streamed through the planned muxer, see stream.py), a track found on YouTube
(Spotify and the like), or a direct job from a source module, streamed here: a ranged file
in parallel 4 MiB pieces, or an HLS playlist segment by segment, in order. Nothing touches
the disk.
"""
import collections
import mimetypes
import subprocess
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .. import download, hls, mp3, net, search, stream
from ..utils import FastDlpError

SITES = {"youtube": "YouTube", "ytmusic": "YT Music", "spotify": "Spotify", "yandex": "Яндекс Музыка",
         "vk": "VK", "soundcloud": "SoundCloud", "tiktok": "TikTok", "pinterest": "Pinterest",
         "pornhub": "Pornhub", "steam": "Steam", "web": "Файл"}
MUSIC = {"ytmusic", "spotify", "yandex", "vk", "soundcloud"}
EXTRA_MIME = {"ts": "video/mp2t", "aac": "audio/aac", "mp3": "audio/mpeg", "flac": "audio/flac",
              "ogg": "audio/ogg", "mkv": "video/x-matroska", "mov": "video/quicktime"}
SEGMENT_WORKERS = 8


def cli_args():
    """The command line's defaults: what source modules read from `args`."""
    from .. import cli
    return cli.build_parser().parse_args([])


def expand(text, args):
    """-> (list title or None, [job, ...]). One job without a title is a single item."""
    from .. import cli
    text = (text or "").strip()
    if not text:
        raise FastDlpError("вставьте ссылку или напишите, что искать")
    if not search.looks_like_target(text):
        jobs, _ = search.search_all(text)
        if not jobs:
            raise FastDlpError(f"ничего не нашлось по запросу «{text}»")
        return f"Поиск «{text}»", jobs
    try:
        jobs = cli.expand(text, args)
    except FastDlpError as e:
        if str(e).startswith(("not a supported", "not a YouTube")):
            raise FastDlpError("эту ссылку fast-dlp не понимает: YouTube, Spotify, Яндекс Музыка, VK, "
                               "SoundCloud, TikTok, Pinterest, Pornhub или прямая ссылка на файл")
        raise
    if not jobs:
        raise FastDlpError("по ссылке нечего скачать")
    title = jobs[0].get("playlist")
    if title or len(jobs) > 1:
        return title or "Список", jobs
    return None, jobs


def kind(job):
    if job.get("source") == "direct":
        return "direct"
    if job.get("track"):
        return "track"
    return "youtube"


def site(job):
    if job.get("source") == "youtube" and not job.get("site"):
        return "youtube"
    return job.get("site") or job.get("found_on") or job.get("source") or ""


def entry(n, job):
    """One row of a list on the page."""
    from .. import cli
    track = job.get("track")
    hint = job.get("hint") or {}
    if track:
        title, author, thumb = track["title"], ", ".join(track.get("artists") or []), track.get("cover")
    elif job.get("source") == "direct":
        title, author, thumb = job["title"], job.get("author") or "", job.get("cover_url")
    else:
        title, author = hint.get("title") or job.get("video_id"), hint.get("author") or ""
        thumb = f"https://i.ytimg.com/vi/{job['video_id']}/mqdefault.jpg"
    s = site(job)
    return {"n": n, "title": title, "author": author, "duration": cli._job_duration(job) or 0,
            "thumbnail": thumb, "site": SITES.get(s, s), "kind": kind(job),
            "audio": bool(track) or s in MUSIC or bool(job.get("audio"))}


def resolve_spec(job):
    """The direct job's download spec; Yandex without Plus turns into a YouTube match."""
    from ..sources import yandex
    try:
        spec = job["resolve"]()
    except yandex.Preview:
        return None
    if spec["kind"] == "steamcmd":
        raise FastDlpError("Steam Workshop качается через steamcmd: это только в командной строке")
    return spec


def headers_of(spec):
    h = dict(spec.get("headers") or {})
    h.setdefault("User-Agent", net.BROWSER_UA)
    return h


def probe(spec):
    """(size, ranges_ok) of a direct file, unless the source already said."""
    from .. import cli
    if spec.get("size") and spec.get("ranges") is not None:
        return spec["size"], spec["ranges"]
    return cli._probe_size(spec["url"], headers_of(spec))


# -- outputs -------------------------------------------------------------------------------------

class _Plain:
    """A plain remote file as a stream.Remote look-alike: Range header, not &range=."""
    range_mode = "header"

    def __init__(self, url, size, label, headers, refresh=None):
        self.stream = download.Stream(url, size, None, label, headers["User-Agent"], refresh=refresh,
                                      headers=headers)


class Piped(stream.Output):
    """An Output fed by a generator instead of a plan (unranged files, HLS)."""

    def __init__(self, ext, source, size, connections, transcode=None, prefix=b""):
        super().__init__(None, ext, [], size, connections, transcode, prefix)
        self.source = source

    def _container_chunks(self):
        yield from self.source()


def mime(ext):
    return stream.MIME.get(ext) or EXTRA_MIME.get(ext) or mimetypes.guess_type("x." + ext)[0] \
        or "application/octet-stream"


def _get_all(url, headers):
    req = urllib.request.Request(url, headers=headers)
    try:
        with net._opener.open(req, timeout=60) as resp:
            while True:
                block = resp.read(256 * 1024)
                if not block:
                    return
                yield block
    except urllib.error.HTTPError as e:
        raise FastDlpError(f"{url.split('?')[0]} answered HTTP {e.code}")


def _ordered(items, fetch, workers):
    """fetch(item) for every item on a pool, yielded in order, a bounded window ahead."""
    pool = ThreadPoolExecutor(max(1, workers), thread_name_prefix="fast-dlp-seg")
    pending = collections.deque()
    it = iter(items)
    try:
        for item in it:
            pending.append(pool.submit(fetch, item))
            if len(pending) >= workers * 2:
                break
        while pending:
            data = pending.popleft().result()
            nxt = next(it, None)
            if nxt is not None:
                pending.append(pool.submit(fetch, nxt))
            yield data
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _hls_media(media_url, headers, audio_only, connections):
    """(ext, chunk generator) of one media playlist, segments in order."""
    kind_, segments, init = hls.parse(hls._get(media_url, headers), media_url)
    if kind_ != "media" or not segments:
        raise FastDlpError("в HLS-плейлисте нет сегментов")
    keys = {}
    for s in segments:
        if s["key"] and s["key"]["uri"] not in keys:
            keys[s["key"]["uri"]] = hls._get(s["key"]["uri"], headers, binary=True)

    def fetch(i):
        s = segments[i]
        status = 0
        for attempt in range(4):
            status, body = net.request(s["uri"], headers=headers, timeout=30)
            if status == 200:
                break
            threading.Event().wait(0.3 * (attempt + 1))
        else:
            raise FastDlpError(f"HLS: сегмент {i} не отдался (HTTP {status})")
        if s["key"]:
            iv = s["key"]["iv"]
            iv = bytes.fromhex(iv[2:].zfill(32)) if iv else s["seq"].to_bytes(16, "big")
            body = hls._decrypt_job((keys[s["key"]["uri"]], iv, body))
        return body

    first = fetch(0)
    init_bytes = hls._get(init, headers, binary=True) if init else b""
    strip = False
    if init_bytes or first[4:8] in (b"ftyp", b"styp", b"moof"):
        ext = "m4a" if audio_only else "mp4"
    elif first[:1] == b"\x47":
        ext = "ts"
    elif hls._audio_kind(first):
        ext, strip = hls._audio_kind(first), True
    else:
        ext = "bin"
    workers = max(1, min(SEGMENT_WORKERS, connections, len(segments)))

    def chunks():
        if init_bytes:
            yield init_bytes
        yield hls._strip_id3(first) if strip else first
        for data in _ordered(range(1, len(segments)), fetch, workers):
            yield hls._strip_id3(data) if strip else data

    return ext, chunks


def _ffmpeg_join(ffmpeg, video_url, audio_url, headers):
    """HLS with audio apart: ffmpeg joins both playlists into fragmented MP4 on stdout."""
    hdr = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    cmd = [ffmpeg, "-loglevel", "error", "-nostdin", "-headers", hdr, "-i", video_url, "-headers", hdr,
           "-i", audio_url, "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-f", "mp4",
           "-movflags", "frag_keyframe+empty_moov+default_base_moof", "pipe:1"]

    def chunks():
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        try:
            while True:
                block = proc.stdout.read(256 * 1024)
                if not block:
                    break
                yield block
            if proc.wait() != 0:
                raise FastDlpError(f"ffmpeg: {proc.stderr.read().decode(errors='replace')[-300:]}")
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            proc.stdout.close()
            proc.stderr.close()

    return chunks


def direct_output(job, spec, opts, meta, ffmpeg):
    """The stream of a direct job: as it is, or MP3 through ffmpeg."""
    headers = headers_of(spec)
    to_mp3 = opts["mode"] == "audio" and opts["audio_format"] == "mp3"
    conns = opts["connections"]
    if to_mp3 and not ffmpeg:
        raise FastDlpError("MP3 нужен ffmpeg на сервере")
    if spec["kind"] == "hls":
        audio_only = bool(job.get("audio")) or to_mp3
        playlists = hls.choose(spec["url"], headers, audio_only, opts.get("height"))
        if len(playlists) == 2:
            if not ffmpeg:
                raise FastDlpError("здесь видео и звук идут отдельно: для склейки нужен ffmpeg на сервере")
            ext, source = "mp4", _ffmpeg_join(ffmpeg, playlists[0], playlists[1], headers)
        else:
            ext, source = _hls_media(playlists[0], headers, audio_only, conns)
        if to_mp3 and ext != "mp3":
            return Piped("mp3", source, None, conns, stream.mp3_command(ffmpeg, opts["mp3_quality"]),
                         mp3.id3v23(meta or {}))
        out = Piped(ext, source, None, conns)
        out.mime = mime(ext)
        return out

    ext = spec.get("ext") or "bin"
    size, ranges = probe(spec)
    if size and ranges:
        remote = _Plain(spec["url"], size, job.get("site") or "file", headers,
                        refresh=lambda: job["resolve"]()["url"])
        plan = [(remote, 0, size - 1, None, True)]
        if to_mp3 and ext != "mp3":
            return stream.Output(None, "mp3", plan, None, conns, transcode=stream.mp3_command(ffmpeg, opts["mp3_quality"]),
                                 prefix=mp3.id3v23(meta or {}))
        out = stream.Output(None, ext, plan, size, conns)
    else:
        source = lambda: _get_all(spec["url"], headers)                    # noqa: E731
        if to_mp3 and ext != "mp3":
            return Piped("mp3", source, None, conns, stream.mp3_command(ffmpeg, opts["mp3_quality"]),
                         mp3.id3v23(meta or {}))
        out = Piped(ext, source, size or None, conns)
    out.mime = mime(ext)
    return out
