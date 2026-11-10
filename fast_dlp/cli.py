import argparse
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import urllib.error
import urllib.parse
import urllib.request

from . import formats as fmt
from . import hls, i18n, mp3, mux, net, postprocess, section, stream, utils, ytmeta
from .download import Stream, download
from .download import _Progress as download_progress
from .extractor import Extractor
from .oauth import TvAuth
from .i18n import t
from .utils import FastDlpError, err, warn

from . import __version__  # noqa: F401  (one place: fast_dlp/__init__.py)
DEFAULT_OUTPUT = "{title} [{id}].{ext}"
TEMPLATES = {
    ("youtube", False): DEFAULT_OUTPUT,
    ("spotify", False): "{author} - {title}.{ext}",
    ("youtube", True): "{playlist}/{index} - {title} [{id}].{ext}",
    ("spotify", True): "{playlist}/{index} - {author} - {title}.{ext}",
}

USAGE_EXTRA = """
commands:
  fast-dlp login           sign in with a Google account (TV device code) for age-restricted videos
  fast-dlp logout          forget the saved account
  fast-dlp spotify-setup   set Spotify Web API keys (ClientID/ClientSecret) for ISRC matching
  fast-dlp yandex-login    sign in to Yandex Music (device code) for full tracks from Yandex
  fast-dlp vk-setup        save a VK (Kate Mobile) token for VK Music
  fast-dlp sync LINK       keep a folder equal to a playlist/album/channel: download new entries,
                           delete removed ones, write an .m3u8 in playlist order

inputs: YouTube video / playlist, Spotify / Yandex Music track, album, playlist,
VK Music track / playlist, SoundCloud track / set, Pinterest pin, Steam Workshop item /
collection, any direct file or .m3u8 URL.
Spotify (and Yandex without a token) download the matching track from YouTube.

output template fields: {title} {id} {author} {album} {playlist} {index} {height} {ext}
  single video:      {title} [{id}].{ext}
  Spotify track:     {author} - {title}.{ext}
  YouTube playlist:  {playlist}/{index} - {title} [{id}].{ext}
  Spotify album/pl.: {playlist}/{index} - {author} - {title}.{ext}
"""


def build_parser():
    p = argparse.ArgumentParser(prog="fast-dlp", description="Fast YouTube downloader.",
                                epilog=USAGE_EXTRA, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("urls", nargs="*", help="links (YouTube, Spotify, Yandex, VK, SoundCloud, TikTok, ...) or "
                                           "plain words to search every source: fast-dlp artist - title")
    p.add_argument("-f", "--format", default="best",
                   help="best | bestaudio | bestvideo | ITAG | ITAG+ITAG | bv+ba (default: best)")
    p.add_argument("-q", "--max-height", type=int, help="highest video height, e.g. 1080")
    p.add_argument("--codec", choices=["avc1", "vp9", "av01", "hevc"],
                   help="preferred video codec (hevc: TikTok's h265 variants)")
    p.add_argument("-x", "--audio-only", action="store_true", help="audio only (same as -f bestaudio)")
    p.add_argument("--audio-format", choices=["best", "opus", "m4a", "mp3"], default="best",
                   help="audio container; opus/m4a are picked as-is, mp3 is transcoded with ffmpeg "
                        "(implies -x)")
    p.add_argument("--audio-quality", default="320k", help="MP3 bitrate (e.g. 320k, 192k) or V0..V9 VBR")
    p.add_argument("-o", "--output", help="output template (see below)")
    p.add_argument("-P", "--paths", default=".", help="output directory")
    p.add_argument("-I", "--playlist-items", help="which entries of a playlist/album: e.g. 1-10,15,20-")
    sel = p.add_mutually_exclusive_group()
    sel.add_argument("--select", dest="select", action="store_const", const=True, default=None,
                     help="always open the track picker for albums/playlists (default: in a terminal, "
                          "unless -I is given)")
    sel.add_argument("-y", "--no-select", dest="select", action="store_const", const=False,
                     help="never open the picker: download every entry")
    p.add_argument("-w", "--force-overwrites", action="store_true", help="re-download existing files")
    p.add_argument("--no-metadata", action="store_true", help="do not write tags")
    p.add_argument("--no-cover", action="store_true", help="do not embed cover art in audio files")
    p.add_argument("--lyrics", action="store_true",
                   help="embed lyrics (LRCLIB, synced when available) - on by default for music sources")
    p.add_argument("--no-lyrics", action="store_true", help="never look up lyrics")
    p.add_argument("--write-lyrics", action="store_true", help="also save lyrics as .lrc (synced) / .txt")
    p.add_argument("--no-replaygain", action="store_true",
                   help="do not write ReplayGain from YouTube's loudness data")
    p.add_argument("--embed-thumbnail", action="store_true", help="embed the thumbnail in video files too")
    g = p.add_argument_group("subtitles, chapters, extras (YouTube)")
    g.add_argument("--write-subs", action="store_true", help="save subtitles next to the file")
    g.add_argument("--write-auto-subs", action="store_true", help="also use auto-generated captions")
    g.add_argument("--sub-langs", default="en", help="languages: en,ru | all (missing ones are machine-translated "
                                                     "when YouTube allows; default en)")
    g.add_argument("--sub-format", choices=["srt", "vtt"], default="srt")
    g.add_argument("--embed-subs", action="store_true", help="put subtitles inside the file (mp4/mkv; webm becomes mkv)")
    g.add_argument("--embed-chapters", action="store_true", help="put chapters inside the file")
    g.add_argument("--sponsorblock-mark", metavar="CATS",
                   help="mark SponsorBlock segments as chapters: all | sponsor,intro,outro,selfpromo,"
                        "preview,filler,interaction,music_offtopic")
    g.add_argument("--write-info-json", action="store_true", help="save metadata as .info.json")
    g.add_argument("--write-description", action="store_true", help="save the description as .description")
    g.add_argument("--write-thumbnail", action="store_true", help="save the thumbnail as .jpg")
    g.add_argument("--write-comments", action="store_true", help="save top comments as .comments.json")
    g.add_argument("--max-comments", type=int, default=200, help="how many comments (default 200)")
    g.add_argument("--live-from-start", action="store_true",
                   help="record a live stream from the start of its DVR window, not from now")
    g.add_argument("--live-duration", type=int, metavar="SECONDS", help="stop recording a live stream after this")
    g.add_argument("--hls", action="store_true", help="download through HLS instead of direct links")
    g.add_argument("--section", metavar="FROM-TO",
                   help="download only part of a video, no ffmpeg: 3:40-5:10 | 3:40- | -1:00 | t (the link's &t=)")
    p.add_argument("--sync-dir", metavar="DIR", help="fast-dlp sync: the folder (default: -P/<playlist name>)")
    p.add_argument("--sync-keep", action="store_true",
                   help="fast-dlp sync: keep files of entries that left the playlist")
    p.add_argument("--download-archive", metavar="FILE",
                   help="record finished downloads in FILE and skip anything already listed")
    p.add_argument("--proxy", metavar="URL", help="HTTP(S) proxy for everything, e.g. http://127.0.0.1:8080")
    p.add_argument("-N", "--connections", type=int, default=16, help="parallel connections (default 16)")
    p.add_argument("-J", "--concurrent-jobs", type=int, default=4,
                   help="playlist/album entries processed at once (default 4)")
    p.add_argument("--range-mode", choices=["header", "param"], default="param",
                   help="send ranges as a Range header or as googlevideo's &range= (default)")
    p.add_argument("-F", "--list-formats", action="store_true", help="list formats and exit")
    p.add_argument("-g", "--get-url", action="store_true", help="print stream URLs and exit")
    p.add_argument("-j", "--dump-json", action="store_true", help="print the player response and exit")
    p.add_argument("--client", choices=["auto", "visionos", "tv"], default="auto",
                   help="auto: VISIONOS, falling back to TV+OAuth for age-restricted videos")
    p.add_argument("--refresh-token", help="Google OAuth refresh token for the TV client")
    p.add_argument("--steam-user", help="Steam login for Workshop items of games that need ownership "
                                        "(remembered; steamcmd asks for the password itself)")
    p.add_argument("--js-runtime", help="path to deno/node/bun (needed only for the TV client)")
    p.add_argument("--cipher", choices=["auto", "local", "remote"], default="auto",
                   help="sig/n solver: local JS runtime, kikkia yt-cipher server, or local then remote")
    p.add_argument("--cipher-url", help="yt-cipher server (default https://cipher.kikkia.dev/)")
    p.add_argument("--merger", choices=["builtin", "ffmpeg"], default="builtin",
                   help="merge with the built-in remuxer (default, no ffmpeg needed) or ffmpeg")
    p.add_argument("--ffmpeg", help="path to ffmpeg (for MP3, --merger ffmpeg or mixed containers)")
    p.add_argument("--keep-parts", action="store_true", help="keep separate streams / the source of an MP3")
    fam = p.add_mutually_exclusive_group()
    fam.add_argument("-4", "--force-ipv4", action="store_true",
                     help="IPv4 only (default: as the system prefers, switching to IPv4 by "
                          "itself when YouTube bot-checks the IPv6 address)")
    fam.add_argument("-6", "--force-ipv6", action="store_true", help="IPv6 only")
    p.add_argument("--no-progress", action="store_true")
    p.add_argument("--lang", choices=["auto", "ru", "en"], default="auto",
                   help="language of the picker and the download board (default: the system's)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"fast-dlp {__version__}")
    return p


def find_ffmpeg(explicit):
    if explicit:
        found = shutil.which(explicit) or (explicit if os.path.isfile(explicit) else None)
        if not found:
            warn(f"ffmpeg not found at {explicit}")
        return found
    return utils.find_tool("ffmpeg")


def container_for(picked):
    exts = {fmt.ext(f) for f in picked}
    if exts <= {"mp4", "m4a"}:
        return "mp4"
    if exts <= {"webm"}:
        return "webm"
    return "mkv"


def ffmpeg_merge(ffmpeg, parts, out):
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-nostdin"]
    for p in parts:
        cmd += ["-i", p]
    for i in range(len(parts)):
        cmd += ["-map", str(i)]
    cmd += ["-c", "copy", out]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FastDlpError(f"ffmpeg failed: {proc.stderr.strip()[-500:]}")


def parse_items(spec, total):
    """'1-10,15,20-' -> sorted 1-based indices within total."""
    if not spec:
        return list(range(1, total + 1))
    chosen = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            start = int(a) if a else 1
            end = int(b) if b else total
            chosen.update(range(start, min(end, total) + 1))
        else:
            chosen.add(int(part))
    return sorted(i for i in chosen if 1 <= i <= total)


# -- inputs -> jobs ---------------------------------------------------------------------

_YOUTUBE_HOST = re.compile(r"^(?:https?://)?(?:[\w-]+\.)*(?:youtube\.com|youtu\.be|youtube-nocookie\.com)/", re.I)


def expand(target, args):
    """One input -> list of jobs (see sources/__init__.py for the job shapes)."""
    from . import playlist, sources, spotify
    if playlist.is_search(target) or playlist.is_channel(target):
        title, entries = playlist.search(target) if playlist.is_search(target) else playlist.channel(target)
        if len(entries) == 1:
            return [{"source": "youtube", "video_id": entries[0]["video_id"], "playlist": None, "hint": entries[0]}]
        print(f"[youtube] \"{title}\": {len(entries)} videos", file=sys.stderr)
        return _numbered([{"source": "youtube", "video_id": e["video_id"], "playlist": title, "hint": e}
                          for e in entries], args)
    if not spotify.parse(target) and not _YOUTUBE_HOST.match(target) and not utils.VIDEO_ID_RE.match(target):
        module = sources.find(target)
        if not module:
            raise FastDlpError(f"not a supported link: {target}")
        title, jobs = module.expand(target, args)
        for job in jobs:
            job["playlist"] = title
        if title:
            print(f"[{jobs[0]['site'] if jobs else 'list'}] \"{title}\": {len(jobs)} items", file=sys.stderr)
            return _numbered(jobs, args)
        return jobs
    sp = spotify.parse(target)
    if sp:
        kind, sid = sp
        client = spotify.Spotify()
        if kind == "track":
            return [{"source": "spotify", "track": client.track(sid), "playlist": None,
                     "url": f"https://open.spotify.com/track/{sid}"}]
        title, tracks = client.collection(kind, sid)
        print(f"[spotify] {kind} \"{title}\": {len(tracks)} tracks", file=sys.stderr)
        return _numbered([{"source": "spotify", "track": t, "playlist": title,
                           "url": f"https://open.spotify.com/track/{t['id']}" if t.get("id") else None}
                          for t in tracks], args)
    if playlist.is_playlist_only(target):
        pid = playlist.playlist_id(target)
        title, entries = playlist.fetch(pid)
        print(f"[youtube] playlist \"{title}\": {len(entries)} videos", file=sys.stderr)
        return _numbered([{"source": "youtube", "video_id": e["video_id"], "playlist": title,
                           "hint": e} for e in entries], args)
    video_id = utils.extract_video_id(target)
    if not video_id:
        raise FastDlpError(f"not a YouTube video/playlist or a Spotify link: {target}")
    return [{"source": "youtube", "video_id": video_id, "playlist": None, "url": target}]


def _numbered(jobs, args):
    total = len(jobs)
    width = len(str(total))
    for i, job in enumerate(jobs, 1):
        job.update(index=str(i).zfill(width), position=i, total=total)
    keep = set(parse_items(args.playlist_items, total))
    return [j for j in jobs if j["position"] in keep]


# -- metadata ---------------------------------------------------------------------------

def _image(url):
    if not url:
        return None, None
    status, body = net.request(url, timeout=15)
    if status != 200 or len(body) < 1000:
        return None, None
    mime = "image/png" if body[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    return body, mime


def youtube_cover(video_id):
    for name in ("maxresdefault", "hqdefault"):
        data, mime = _image(f"https://i.ytimg.com/vi/{video_id}/{name}.jpg")
        if data:
            return data, mime
    return None, None


def build_meta(job, video, want_cover):
    track = job.get("track")
    if track:
        meta = {
            "title": track["title"],
            "artist": ", ".join(track["artists"]),
            "album": track.get("album"),
            "album_artist": track.get("album_artist"),
            "date": track.get("date"),
            "track": track.get("track_number") or (job.get("position") if job.get("playlist") else None),
            "comment": job.get("url"),
        }
        if want_cover:
            meta["cover"], meta["cover_mime"] = _image(track.get("cover"))
            if not meta["cover"]:
                meta["cover"], meta["cover_mime"] = youtube_cover(video.id)
    else:
        meta = {
            "title": video.title,
            "artist": video.author,
            "album": job.get("playlist"),
            "track": job.get("position") if job.get("playlist") else None,
            "comment": f"https://www.youtube.com/watch?v={video.id}",
        }
        if want_cover:
            meta["cover"], meta["cover_mime"] = youtube_cover(video.id)
    return {k: v for k, v in meta.items() if v}


def music_extras(meta, job, title, artist, album, duration, args, base, loudness=None):
    """Lyrics (LRCLIB) and ReplayGain into the tags; .lrc next to the file with --write-lyrics."""
    from . import lyrics
    if meta is None:
        return
    if loudness is not None and not args.no_replaygain:
        meta["replaygain"] = lyrics.replaygain(loudness)
    musical = job["source"] in MUSIC_SOURCES or (job["source"] == "direct" and job.get("audio"))
    if args.no_lyrics or not (args.lyrics or args.write_lyrics or musical):
        return
    synced, plain = lyrics.fetch(title, artist, album, duration)
    text = synced or plain
    if not text:
        return
    meta["lyrics"] = text
    print(f"[lyrics] {'synced' if synced else 'plain'} lyrics found", file=sys.stderr)
    if args.write_lyrics:
        path = f"{base}.{'lrc' if synced else 'txt'}"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"[side] {os.path.basename(path)}", file=sys.stderr)


def render_name(template, video, picked, ext, job, upload_date=None):
    return postprocess.render(template, postprocess.fields(video, picked, ext, job, upload_date))


# -- resolving --------------------------------------------------------------------------

MAX_SPOTIFY_CANDIDATES = 8


def resolve_spotify(extractor, track, site="spotify"):
    """Track metadata (Spotify, Yandex...) -> YouTube video, best candidate that plays."""
    from . import ytmusic
    t0 = time.time()
    if site == "spotify":
        spotify.Spotify().complete(track)          # playlist items: cover, date, ISRC
    who = ", ".join(track["artists"])
    print(f"[{site}] {who} - {track['title']} ({track['duration']}s"
          f"{', ISRC ' + track['isrc'] if track.get('isrc') else ''})", file=sys.stderr)
    errors = []
    for i, (cand, how) in enumerate(ytmusic.candidates(track)):
        if i >= MAX_SPOTIFY_CANDIDATES:
            break
        label = f"{cand['video_id']} \"{cand['title']}\" by {', '.join(cand['artists'])} [{how}]"
        try:
            video = extractor.extract(cand["video_id"])
        except FastDlpError as e:
            utils.debug(f"skip {label}: {e}")
            errors.append(f"{cand['video_id']}: {e}")
            continue
        print(f"[{site}] -> YouTube {label}", file=sys.stderr)
        utils.debug(f"Spotify resolved in {time.time() - t0:.2f}s")
        video.title = track["title"]
        video.author = who
        return video
    if not errors:
        raise FastDlpError(f"{site}: {who} - {track['title']}: no match on YouTube")
    copies = f"{len(errors)} cop{'y' if len(errors) == 1 else 'ies'} found on YouTube"
    raise FastDlpError(f"{site}: {who} - {track['title']}: {copies}, none downloadable "
                       f"(last: {errors[-1]})")


def _audio_spec(args, forced_audio):
    if args.format != "best" and not forced_audio:
        return args.format
    if args.audio_format == "m4a":
        return "bestaudio-m4a"
    if args.audio_format == "opus":
        return "bestaudio-opus"
    if args.audio_only or forced_audio or args.audio_format == "mp3":
        return "bestaudio"
    return args.format


# -- one job ----------------------------------------------------------------------------

MUSIC_SOURCES = ("spotify", "match")
DIRECT_TEMPLATES = {
    (True, False): "{author} - {title}.{ext}",
    (True, True): "{playlist}/{index} - {author} - {title}.{ext}",
    (False, False): "{title}.{ext}",
    (False, True): "{playlist}/{index} - {title}.{ext}",
}


def template_for(job, args):
    if args.output:
        return args.output
    if job["source"] == "direct":
        return DIRECT_TEMPLATES[(bool(job["audio"] and job.get("author")), bool(job.get("playlist")))]
    source = "spotify" if job["source"] in MUSIC_SOURCES else "youtube"
    return TEMPLATES[(source, bool(job.get("playlist")))]


class _Known:
    """Enough of a video to render a file name before asking YouTube anything."""

    def __init__(self, job):
        track = job.get("track")
        hint = job.get("hint") or {}
        self.id = job.get("video_id") or job.get("id") or ""
        if job["source"] == "direct":
            self.title, self.author = job["title"], job["author"]
        else:
            self.title = track["title"] if track else hint.get("title", "")
            self.author = ", ".join(track["artists"]) if track else hint.get("author", "")


def already_have(job, args):
    """Path of an earlier download of this job, checked before any network request."""
    if args.force_overwrites or args.dump_json or args.list_formats or args.get_url or args.section:
        return None
    template = template_for(job, args)
    known = _Known(job)
    simple = {"title", "id", "author", "uploader", "channel", "artist", "album", "playlist", "index", "ext",
              "site", "playlist_index", "track_number"}
    used = postprocess.template_fields(template)
    if ("id" in used and not known.id) or used - simple or not known.title:
        return None
    if args.audio_format == "mp3":
        exts = ["mp3"]
    elif job["source"] == "direct":
        exts = ["mp3", "m4a", "aac", "flac", "mp4", "webm", "ts", "jpg", "png", "gif", "webp", "bin"]
    else:
        exts = ["webm", "m4a", "mp4", "mkv"]
    for ext in exts:
        path = os.path.join(args.paths, render_name(template, known, [], ext, job))
        if os.path.exists(path):
            return path
    return None


def _probe_size(url, headers):
    """(size, ranges_ok) from a 1-byte ranged GET."""
    hdrs = dict(headers)
    hdrs["Range"] = "bytes=0-0"
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with net._opener.open(req, timeout=20) as resp:
            rng = resp.headers.get("Content-Range", "")
            if resp.status == 206 and "/" in rng and rng.rsplit("/", 1)[1].isdigit():
                return int(rng.rsplit("/", 1)[1]), True
            return int(resp.headers.get("Content-Length") or 0), False
    except urllib.error.HTTPError as e:
        raise FastDlpError(f"{url.split('?')[0]} answered HTTP {e.code}")


def _plain_get(url, headers, path):
    req = urllib.request.Request(url, headers=headers)
    with net._opener.open(req, timeout=60) as resp, open(path, "wb") as fh:
        shutil.copyfileobj(resp, fh, 1 << 20)


def process_direct(extractor, job, args, ffmpeg):
    """A job from a non-YouTube source: resolve the link now, download, tag."""
    from .sources import steam, yandex
    try:
        spec = job["resolve"]()
    except yandex.Preview:
        # No Plus: the same track from YouTube, matched like a Spotify track.
        print(f"[yandex] {job['author']} - {job['title']}: only a preview on Yandex, matching on YouTube",
              file=sys.stderr)
        fallback = dict(job, source="match", track=job["fallback_track"])
        return process(extractor, fallback, args, ffmpeg)
    template = template_for(job, args)
    prefix = f"[{job['index']}/{job['total']}] " if job.get("playlist") else ""
    known = _Known(job)

    if spec["kind"] == "steamcmd":
        folder = os.path.join(args.paths, utils.sanitize_path(job.get("playlist") or "")) if job.get("playlist") else args.paths
        os.makedirs(folder, exist_ok=True)
        print(f"{prefix}[steam] {job['title']}  ->  steamcmd", file=sys.stderr)
        name = utils.sanitize_path(utils.sanitize_filename(f"{job['title']} [{spec['item']}]"))
        target = steam.steamcmd_download(spec["app"], spec["item"], folder, name,
                                         steam.steam_user(args.steam_user))
        _done(args, job, target)
        return "done"

    out_any = os.path.join(args.paths, render_name(template, known, [], "x", job))
    base = out_any[:-2]
    os.makedirs(os.path.dirname(os.path.abspath(base)), exist_ok=True)
    headers = dict(spec.get("headers") or {})
    headers.setdefault("User-Agent", net.BROWSER_UA)
    print(f"{prefix}[{job['site']}] {(job['author'] + ' - ') if job.get('author') and job['audio'] else ''}"
          f"{job['title']}  ->  {spec['kind']}", file=sys.stderr)

    meta = None
    if not args.no_metadata and job.get("meta"):
        meta = {k: v for k, v in job["meta"].items() if v}
        if job["audio"] and not args.no_cover and job.get("cover_url"):
            meta["cover"], meta["cover_mime"] = _image(job["cover_url"])
            if not meta["cover"]:
                meta.pop("cover", None)
                meta.pop("cover_mime", None)
        if job["audio"]:
            music_extras(meta, job, job["title"], job.get("author"), (job.get("meta") or {}).get("album"),
                         job.get("duration") or (job.get("fallback_track") or {}).get("duration"), args, base)
    to_mp3 = job["audio"] and args.audio_format == "mp3"
    tagged = False

    if spec["kind"] == "hls":
        path = hls.download(spec["url"], base, headers, audio_only=job["audio"], max_height=args.max_height,
                            connections=args.connections, show_progress=not args.no_progress)
    else:
        path = f"{base}.{spec.get('ext') or 'bin'}"
        size, ranges = (spec.get("size") or 0), spec.get("ranges")
        if not size or ranges is None:
            if not args.no_progress and sys.stderr.isatty():
                # The size probe is the first request to the file's server: on a cold CDN
                # cache it can take a while, and that wait should not look like a hang.
                sys.stderr.write(f"\r[download] connecting to {urllib.parse.urlsplit(spec['url']).hostname}...")
                sys.stderr.flush()
            t_probe = time.time()
            size, ranges = _probe_size(spec["url"], headers)
            utils.debug(f"size probe: {size} bytes, ranges {'yes' if ranges else 'no'}, "
                        f"{time.time() - t_probe:.2f}s")
        if size and ranges:
            stream = Stream(spec["url"], size, path + ".part", job["site"], headers["User-Agent"],
                            refresh=lambda: job["resolve"]()["url"], headers=headers)
            try:
                held = download([stream], args.connections, "header", not args.no_progress, keep_open=True)
            except BaseException:
                if os.path.exists(stream.path):
                    os.remove(stream.path)
                raise
            with held[stream.path] as fh:
                # Tags through the handle that was open all along: reopening the fresh file
                # would first wait for the antivirus to scan all of it.
                if meta and not to_mp3 and path.rsplit(".", 1)[-1].lower() in ("mp4", "m4a", "mov"):
                    try:
                        tagged = mux.tag_open(fh, meta)
                    except (mux.MuxError, struct.error, OSError) as e:
                        utils.debug(f"no tags written ({e})")
                        tagged = True                # do not reopen the file to try again
            os.replace(stream.path, path)
        else:
            _plain_get(spec["url"], headers, path + ".part")
            os.replace(path + ".part", path)

    ext = path.rsplit(".", 1)[-1].lower()
    if to_mp3 and ext != "mp3":
        if not ffmpeg:
            raise FastDlpError("MP3 needs ffmpeg: install it (winget install Gyan.FFmpeg) or pass --ffmpeg")
        mp3_path = f"{base}.mp3"
        mp3.encode(ffmpeg, path, mp3_path, args.audio_quality, meta)
        if not args.keep_parts:
            os.remove(path)
        path = mp3_path
    elif meta and ext == "mp3":
        mp3.tag_file(path, meta)
    elif meta and mux.can_tag(ext) and not tagged:
        try:
            mux.tag(path, meta)
        except mux.MuxError as e:
            utils.debug(f"no tags written ({e})")
    _done(args, job, path)
    return "done"


def _done(args, job, path):
    print(f"[done] {path}", file=sys.stderr)
    hook = getattr(args, "on_file", None)
    if hook:
        hook(job, path)


def _skip(args, job, path):
    print(f"[skip] already exists: {path}", file=sys.stderr)
    hook = getattr(args, "on_file", None)
    if hook:
        hook(job, path)


def process(extractor, job, args, ffmpeg):
    """Returns 'done' or 'skipped'; raises FastDlpError on failure."""
    archive = getattr(args, "archive", None)
    if archive and archive.has(job) and not args.force_overwrites:
        print(f"[skip] in the download archive: {postprocess.Archive.key(job)}", file=sys.stderr)
        return "skipped"
    result = _process(extractor, job, args, ffmpeg)
    if archive and result == "done" and not (args.dump_json or args.list_formats or args.get_url):
        archive.add(job)
    return result


def _process(extractor, job, args, ffmpeg):
    have = already_have(job, args)
    if have:
        _skip(args, job, have)
        return "skipped"
    if job["source"] == "direct":
        return process_direct(extractor, job, args, ffmpeg)
    t0 = time.time()
    if job["source"] in MUSIC_SOURCES and job.get("video_id"):
        # A song already found on YouTube (search results): no matching needed.
        video = extractor.extract(job["video_id"])
        video.title, video.author = job["track"]["title"], ", ".join(job["track"]["artists"])
    elif job["source"] in MUSIC_SOURCES:
        video = resolve_spotify(extractor, job["track"], job.get("site", "spotify"))
    else:
        video = extractor.extract(job["video_id"])
    utils.debug(f"extracted {video.id} via {video.client} in {time.time() - t0:.2f}s")

    if args.dump_json:
        print(json.dumps(video.raw, ensure_ascii=False, indent=1))
        return "done"
    if args.list_formats:
        print(f"[{video.client}] {video.title}")
        print(fmt.table(video.formats))
        return "done"

    details = video.raw.get("videoDetails") or {}
    template = template_for(job, args)
    watch = ytmeta.Watch(video.id) if job["source"] == "youtube" else None
    upload_date = watch.upload_date() if watch and postprocess.template_fields(template) & set(postprocess.DATE_FIELDS) else None

    if details.get("isLive"):
        return record_live(video, job, args, ffmpeg, template, upload_date)

    spec = _audio_spec(args, job["source"] in MUSIC_SOURCES or job.get("as_audio", False))
    if spec.startswith("bestaudio-"):
        wanted = "m4a" if spec.endswith("m4a") else "webm"
        audio = [f for f in video.formats if fmt.kind(f) == "audio" and fmt.ext(f) == wanted]
        picked = fmt.select(audio or video.formats, "bestaudio")
    else:
        picked = fmt.select(video.formats, spec, args.max_height, args.codec)
    if args.get_url:
        for f in picked:
            print(f["url"])
        return "done"

    single = len(picked) == 1
    is_audio = all(fmt.kind(f) == "audio" for f in picked)
    to_mp3 = args.audio_format == "mp3" and is_audio
    ext = fmt.ext(picked[0]) if single else container_for(picked)
    section_range = section.parse_section(args.section, job.get("url")) if args.section else None
    subs = postprocess.subtitles(video, args) if watch else []
    # A section re-times the media to start at zero; full-length chapters/subtitles would not fit it.
    chapter_list = postprocess.chapters(video, args, watch) if watch and not section_range else []
    embed_subs = subs if args.embed_subs and not section_range else []
    if embed_subs and ext == "webm":
        ext = "mkv"           # WebVTT-in-WebM is poorly supported; the same streams in Matroska carry SRT
    final_ext = "mp3" if to_mp3 else ext
    out = os.path.join(args.paths, render_name(template, video, picked, final_ext, job, upload_date))
    if section_range:
        stem, dot, e = out.rpartition(".")
        out = f"{stem} [{section.label(*section_range)}].{e}"
    if os.path.exists(out) and not args.force_overwrites:
        _skip(args, job, out)
        return "skipped"
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    if to_mp3 and not ffmpeg:
        raise FastDlpError("MP3 needs ffmpeg (YouTube serves no MP3, so it is a transcode): "
                           "install it (winget install Gyan.FFmpeg) or pass --ffmpeg")

    use_ffmpeg, pair = False, None
    if not single:
        vids = [f for f in picked if fmt.kind(f) == "video"]
        auds = [f for f in picked if fmt.kind(f) == "audio"]
        if len(vids) == 1 and len(auds) == 1:
            pair = (picked.index(vids[0]), picked.index(auds[0]))
        builtin = pair is not None and mux.can_mux(fmt.ext(vids[0]), fmt.ext(auds[0]))
        use_ffmpeg = args.merger == "ffmpeg" or not builtin
        if use_ffmpeg and not ffmpeg:
            warn("these formats need ffmpeg to merge (mixed containers); saving them as separate "
                 "files. Use matching containers (default selection does) or --ffmpeg")

    desc = " + ".join(f"{f['itag']} ({fmt.ext(f)}, {f.get('qualityLabel') or str((f.get('bitrate') or 0)//1000) + 'k'})"
                      for f in picked)
    prefix = f"[{job['index']}/{job['total']}] " if job.get("playlist") else ""
    print(f"{prefix}[{video.client}] {video.title}  ->  {desc}{'  ->  mp3' if to_mp3 else ''}", file=sys.stderr)

    meta = None
    if not args.no_metadata:
        want_cover = not args.no_cover and (is_audio or args.embed_thumbnail)
        meta = build_meta(job, video, want_cover)
        audio_fmt = next((f for f in picked if fmt.kind(f) == "audio"), None)
        loudness = (audio_fmt or {}).get("trackAbsoluteLoudnessLkfs")
        if loudness is None:
            loudness = ((video.raw.get("playerConfig") or {}).get("audioConfig") or {}).get("trackAbsoluteLoudnessLkfs")
        track = job.get("track") or {}
        music_extras(meta, job, track.get("title") or video.title,
                     ", ".join(track["artists"]) if track else video.author, track.get("album"),
                     track.get("duration") or int(details.get("lengthSeconds") or 0) or None,
                     args, os.path.splitext(out)[0], loudness if audio_fmt else None)

    base_out = os.path.splitext(out)[0]
    hls_now = bool(args.hls)
    planned = (not use_ffmpeg and not embed_subs and not chapter_list and not args.hls and not args.keep_parts
               and all("initRange" in f and "indexRange" in f for f in picked))
    if planned:
        try:
            if _download_planned(video, picked, out, meta, section_range, to_mp3, ffmpeg, args):
                if watch:
                    postprocess.write_side_files(base_out, video, args, watch, subs, picked, chapter_list)
                _done(args, job, out)
                return "done"
        except (FastDlpError, OSError) as e:
            hls_url = (video.raw.get("streamingData") or {}).get("hlsManifestUrl")
            if not hls_url or to_mp3 or is_audio:
                raise
            warn(f"direct download failed ({e}); trying HLS")
            hls_now = True

    base = os.path.splitext(out)[0]
    streams = []
    headers_for = []
    t0s = []
    for f in picked:
        if single and not to_mp3:
            part = out
        else:
            part = f"{base}.f{f['itag']}.{fmt.ext(f)}"
        itag = f["itag"]
        size, offset = fmt.size(f), 0
        if section_range:
            head, t0, b0, b1 = section.plan(f, section_range[0], section_range[1], utils_ua(video.client))
            size, offset = b1 - b0 + 1, b0
            headers_for.append(head)
            t0s.append(t0)
        streams.append(Stream(f["url"], size, part + ".part", f"itag {itag}", utils_ua(video.client),
                              refresh=lambda itag=itag: video.fresh_url(itag), offset=offset))
    extras = {"chapters": chapter_list, "duration": float(details.get("lengthSeconds") or 0) or None,
              "subs": embed_subs}
    if section_range:
        total = float(details.get("lengthSeconds") or 0)
        extras["rebase"] = min(t0s)
        # Whole fragments (cut at keyframes, like ffmpeg -c copy). An edit-list "exact" cut was
        # tried and dropped: ffmpeg applied it to the video of a fragmented MP4 but not to its
        # audio, so players could drift apart.
        extras["section_duration"] = (section_range[1] or total) - min(t0s)
        print(f"[section] {section.label(*section_range)}: {sum(s.size for s in streams) / 1048576:.1f} MiB "
              f"instead of {sum(fmt.size(f) for f in picked) / 1048576:.1f} MiB", file=sys.stderr)
    hls_url = (video.raw.get("streamingData") or {}).get("hlsManifestUrl")
    try:
        if hls_now:
            raise FastDlpError("--hls")
        download(streams, args.connections, args.range_mode, not args.no_progress)
    except (FastDlpError, OSError) as e:
        for s in streams:
            try:
                os.remove(s.path)
            except OSError:
                pass
        if not hls_url or to_mp3 or is_audio and not args.hls:
            raise
        # The same answer also offers HLS (up to 4K): a second road when direct links fail.
        if str(e) != "--hls":
            warn(f"direct download failed ({e}); trying HLS")
        path = hls.download(hls_url, base_out, {"User-Agent": utils_ua(video.client)}, audio_only=is_audio,
                            max_height=args.max_height, connections=args.connections,
                            show_progress=not args.no_progress, ffmpeg=ffmpeg)
        if (meta or chapter_list or embed_subs) and mux.can_tag(path.rsplit(".", 1)[-1]):
            try:
                mux.tag(path, meta, extras)
            except mux.MuxError as err_:
                utils.debug(f"no tags on the HLS result ({err_})")
        postprocess.write_side_files(base_out, video, args, watch or ytmeta.Watch(video.id), subs, picked, chapter_list)
        _done(args, job, path)
        return "done"
    except BaseException:
        for s in streams:
            try:
                os.remove(s.path)
            except OSError:
                pass
        raise
    finals = []
    for i, s in enumerate(streams):
        if section_range:
            # header (ftyp+moov / EBML+Tracks) in front of the downloaded fragments
            with open(s.path[:-5], "wb") as dst, open(s.path, "rb") as src:
                dst.write(headers_for[i])
                shutil.copyfileobj(src, dst, 4 << 20)
            os.remove(s.path)
        else:
            os.replace(s.path, s.path[:-5])
        finals.append(s.path[:-5])

    t = time.time()
    if to_mp3:
        mp3.encode(ffmpeg, finals[0], out, args.audio_quality, meta)
        if not args.keep_parts:
            os.remove(finals[0])
        utils.debug(f"mp3 encoded in {time.time() - t:.2f}s")
    elif single:
        if (meta or chapter_list or embed_subs or section_range) and mux.can_tag(ext):
            try:
                mux.tag(out, meta, extras)
            except mux.MuxError as e:
                warn(f"could not write tags: {e}")
    elif not use_ffmpeg:
        try:
            mux.merge(finals[pair[0]], finals[pair[1]], out, meta, extras)
        except mux.MuxError as e:
            if not ffmpeg:
                raise FastDlpError(f"built-in merge failed ({e}); parts kept: {', '.join(finals)}")
            utils.debug(f"built-in merge failed ({e}); using ffmpeg")
            ffmpeg_merge(ffmpeg, finals, out)
            use_ffmpeg = True
    elif ffmpeg:
        ffmpeg_merge(ffmpeg, finals, out)
    else:
        for p in finals:
            _done(args, job, p)
        return "done"
    if not single and not args.keep_parts:
        for p in finals:
            if os.path.exists(p):
                os.remove(p)
    if watch:
        postprocess.write_side_files(base_out, video, args, watch, subs, picked, chapter_list)
    utils.debug(f"post-processing in {time.time() - t:.3f}s")
    _done(args, job, out)
    return "done"


def _download_planned(video, picked, out, meta, section_range, to_mp3, ffmpeg, args):
    """Download straight into the finished file (or straight into ffmpeg for MP3): no part
    files, no merge pass. False when the formats cannot be planned (then the parts path runs)."""
    start, end = section_range if section_range else (None, None)
    t = time.time()
    try:
        plan, size, kind = stream.plan_for(video, picked, None if to_mp3 else meta, start, end)
    except (FastDlpError, mux.MuxError, ValueError, KeyError, IndexError, struct.error) as e:
        utils.debug(f"no planned download ({e}); using part files")
        return False
    remote_bytes = sum(p[2] - p[1] + 1 for p in plan if not isinstance(p, (bytes, bytearray)))
    utils.debug(f"planned {size or remote_bytes} bytes in {len(plan)} pieces ({time.time() - t:.2f}s)")
    if section_range:
        print(f"[section] {section.label(*section_range)}: {remote_bytes / 1048576:.1f} MiB "
              f"instead of {sum(fmt.size(f) for f in picked) / 1048576:.1f} MiB", file=sys.stderr)
    part = out + ".part"
    try:
        if not to_mp3:
            stream.write_file(plan, part, args.connections, args.range_mode, not args.no_progress)
        else:
            # The audio goes from the network straight into ffmpeg; only MP3 frames reach the disk.
            output = stream.Output(None, "mp3", plan, None, args.connections,
                                   transcode=stream.mp3_command(ffmpeg, args.audio_quality),
                                   prefix=mp3.id3v23(meta or {}))
            progress = download_progress(remote_bytes, not args.no_progress)
            with progress, open(part, "wb") as fh:
                for block in output.chunks():
                    fh.write(block)
                    progress.done = output.fetched
                progress.done = remote_bytes
        os.replace(part, out)
    except BaseException:
        try:
            os.remove(part)
        except OSError:
            pass
        raise
    utils.debug(f"finished file in {time.time() - t:.2f}s")
    return True


def record_live(video, job, args, ffmpeg, template, upload_date):
    """A live stream: recorded from its HLS playlist until it ends, --live-duration, or Ctrl+C."""
    url = (video.raw.get("streamingData") or {}).get("hlsManifestUrl")
    if not url:
        raise FastDlpError("this live stream offers no HLS playlist")
    stamp = time.strftime("%Y-%m-%d %H-%M")
    out = os.path.join(args.paths, render_name(template, video, [], "x", job, upload_date))
    base = f"{out[:-2]} [live {stamp}]"
    os.makedirs(os.path.dirname(os.path.abspath(base)), exist_ok=True)
    print(f"[live] {video.title}  ->  recording{' from the start of the DVR window' if args.live_from_start else ''}"
          f"{f' for {args.live_duration}s' if args.live_duration else ' (Ctrl+C to stop)'}", file=sys.stderr)
    path = hls.record_live(url, base, {"User-Agent": utils_ua(video.client)}, audio_only=args.audio_only,
                           max_height=args.max_height, duration=args.live_duration,
                           from_start=args.live_from_start, ffmpeg=ffmpeg, show_progress=not args.no_progress)
    _done(args, job, path)
    return "done"


# -- interactive selection ----------------------------------------------------------------

def job_label(job):
    track = job.get("track")
    if track:
        return f"{track['title']} — {', '.join(track['artists'])}"
    if job["source"] == "direct":
        return f"{job['title']} — {job['author']}" if job.get("author") else job["title"]
    hint = job.get("hint") or {}
    title = hint.get("title") or job.get("video_id", "")
    return f"{title} — {hint['author']}" if hint.get("author") else title


def _job_duration(job):
    if job.get("track"):
        return job["track"].get("duration")
    return (job.get("hint") or {}).get("duration") or job.get("duration") or \
        (job.get("fallback_track") or {}).get("duration")


def collection_title(jobs):
    site = {"spotify": "Spotify", "youtube": "YouTube", "match": "", "direct": ""}.get(jobs[0]["source"], "")
    site = site or {"yandex": t("site_yandex"), "vk": "VK", "soundcloud": "SoundCloud", "tiktok": "TikTok",
                    "pinterest": "Pinterest", "steam": "Steam"}.get(jobs[0].get("site"), jobs[0].get("site", ""))
    return f"{site} · {t('quoted', s=jobs[0].get('playlist') or '')}"


def wants_picker(jobs, args):
    """The picker opens for a collection in a real terminal, unless told otherwise."""
    from . import tui
    if args.select is False or len(jobs) < 2 or not jobs[0].get("playlist"):
        return False
    if args.dump_json or args.list_formats or args.get_url:
        return False
    if args.select:
        return tui.available()
    return tui.available() and not args.playlist_items


def pick_search(jobs, args, query, counts):
    """The picker over mixed search results: only the best hit is ticked; YouTube videos can be
    taken as sound (default) or as video."""
    from . import search, tui
    o = {
        "format": tui.Option(t("opt_audio_format"), ["best", "opus", "m4a", "mp3"], args.audio_format,
                             {"best": t("as_is"), "opus": "opus", "m4a": "m4a (AAC)", "mp3": "mp3"}),
        "quality": tui.Option(t("opt_mp3_quality"), ["320k", "256k", "192k", "128k", "V0", "V2"], args.audio_quality,
                              {"V0": "V0 (VBR ~245k)", "V2": "V2 (VBR ~190k)"},
                              visible=lambda opts: opts["format"].value == "mp3"),
        "ytvideo": tui.Option(t("opt_youtube_video"), ["audio", "video"], "video" if args.format != "best" else "audio",
                              {"audio": t("audio_only"), "video": t("video_audio")}),
        "jobs": tui.Option(t("opt_parallel"), list(range(1, 9)), min(8, max(1, args.concurrent_jobs))),
        "tags": tui.Option(t("opt_tags"), [True, False], not args.no_metadata, {True: t("yes"), False: t("no")}),
        "cover": tui.Option(t("opt_cover"), [True, False], not args.no_cover, {True: t("yes"), False: t("no")},
                            visible=lambda opts: opts["tags"].value),
    }
    items = [{"label": job_label(j),
              "right": f"{search.label(j.get('found_on'))}  {tui.fmt_duration(_job_duration(j))}".strip()}
             for j in jobs]
    result = tui.pick(f"{t('search', q=query)} · {counts}", items, o, checked=[i == 0 for i in range(len(jobs))])
    if result is None:
        return None
    indices, values = result
    new = argparse.Namespace(**vars(args))
    new.audio_format = values["format"]
    new.audio_quality = values.get("quality", args.audio_quality)
    new.concurrent_jobs = values["jobs"]
    new.no_metadata = not values["tags"]
    new.no_cover = not values["cover"]
    chosen = []
    for i in indices:
        job = dict(jobs[i])
        if job["source"] == "youtube":
            job["as_audio"] = values["ytvideo"] == "audio"
        chosen.append(job)
    return chosen, new


def pick_jobs(jobs, args):
    """Show the picker; returns (chosen jobs, args with the chosen options) or None."""
    from . import tui
    music = jobs[0]["source"] in MUSIC_SOURCES or (jobs[0]["source"] == "direct" and jobs[0].get("audio"))
    youtube = jobs[0]["source"] == "youtube"
    o = {}
    if music or youtube:
        if youtube:
            fmt_values = ["video", "audio", "opus", "m4a", "mp3"]
            current = "mp3" if args.audio_format == "mp3" else (
                args.audio_format if args.audio_format in ("opus", "m4a") else ("audio" if args.audio_only else "video"))
            shown = {"video": t("video_audio"), "audio": t("audio_only_best"), "opus": t("audio_opus"),
                     "m4a": t("audio_m4a"), "mp3": "mp3"}
        else:
            fmt_values = ["best", "opus", "m4a", "mp3"]
            current = args.audio_format
            shown = {"best": t("as_is"), "opus": "opus", "m4a": "m4a (AAC)", "mp3": "mp3"}
        o["format"] = tui.Option(t("opt_format"), fmt_values, current, shown)
        if youtube:
            o["height"] = tui.Option(t("opt_video_quality"), [None, 2160, 1440, 1080, 720, 480, 360],
                                     args.max_height, {None: t("best")} | {h: t("up_to", h=h) for h in (2160, 1440, 1080, 720, 480, 360)},
                                     visible=lambda opts: opts["format"].value == "video")
        o["quality"] = tui.Option(t("opt_mp3_quality"), ["320k", "256k", "192k", "128k", "V0", "V2"], args.audio_quality,
                                  {"V0": "V0 (VBR ~245k)", "V2": "V2 (VBR ~190k)"},
                                  visible=lambda opts: opts["format"].value == "mp3")
    o["jobs"] = tui.Option(t("opt_parallel"), list(range(1, 9)), min(8, max(1, args.concurrent_jobs)))
    o["tags"] = tui.Option(t("opt_tags"), [True, False], not args.no_metadata, {True: t("yes"), False: t("no")})
    o["cover"] = tui.Option(t("opt_cover"), [True, False], not args.no_cover, {True: t("yes"), False: t("no")},
                            visible=lambda opts: opts["tags"].value)
    o["skip"] = tui.Option(t("opt_existing"), [True, False], not args.force_overwrites,
                           {True: t("skip"), False: t("redownload")})
    items = [{"label": job_label(j), "right": tui.fmt_duration(_job_duration(j))} for j in jobs]
    result = tui.pick(f"{collection_title(jobs)} · {len(jobs)}", items, o)
    if result is None:
        return None
    indices, values = result
    new = argparse.Namespace(**vars(args))
    if "format" in values:
        f = values["format"]
        if youtube:
            new.audio_only = f != "video"
            new.audio_format = f if f in ("opus", "m4a", "mp3") else "best"
            new.max_height = values.get("height") if f == "video" else args.max_height
        else:
            new.audio_format = f
        new.audio_quality = values.get("quality", args.audio_quality)
    new.concurrent_jobs = values["jobs"]
    new.no_metadata = not values["tags"]
    new.no_cover = not values["cover"]
    new.force_overwrites = not values["skip"]
    chosen = [jobs[i] for i in indices]
    return chosen, new


def utils_ua(client):
    from .innertube import TIZEN_UA, VISION_UA
    return TIZEN_UA if client == "TVHTML5" else VISION_UA


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    try:
        if argv[:1] == ["login"]:
            TvAuth().login()
            return 0
        if argv[:1] == ["logout"]:
            TvAuth().logout()
            return 0
        if argv[:1] == ["web"]:
            from .web import server
            return server.main(argv[1:])
        if argv[:1] == ["spotify-setup"]:
            from .spotify import Spotify
            Spotify().setup(force=True)
            return 0
        syncing = argv[:1] == ["sync"]
        if syncing:
            argv = argv[1:]
        if argv[:1] == ["yandex-login"]:
            from .sources import yandex
            yandex.login()
            return 0
        if argv[:1] == ["vk-setup"]:
            from .sources import vk
            vk.setup()
            return 0
        args = build_parser().parse_args(argv)
        utils.VERBOSE = args.verbose
        i18n.set_language(args.lang)
        if not args.urls:
            build_parser().print_help()
            return 2
        if args.force_ipv4 or args.force_ipv6:
            import socket
            net.set_family(socket.AF_INET if args.force_ipv4 else socket.AF_INET6)
        if args.proxy:
            net.set_proxy(args.proxy)
        args.archive = postprocess.Archive(args.download_archive) if args.download_archive else None
        extractor = Extractor(args.refresh_token, args.js_runtime, args.client,
                              args.cipher, args.cipher_url)
        if args.force_ipv4 or args.force_ipv6:
            extractor.auto_ipv4 = False
        ffmpeg = find_ffmpeg(args.ffmpeg)
        counts = {"done": 0, "skipped": 0, "failed": 0}
        lock = threading.Lock()
        many = False
        failures = []

        def run(job, job_args, board=None, index=None):
            if board:
                board.set(index, "working")
            note = ""
            try:
                result = process(extractor, job, job_args, ffmpeg)
            except FastDlpError as e:
                err(str(e))
                result, note = "failed", str(e)
                failures.append((job_label(job), str(e)))
            except Exception as e:                  # keep the other jobs (and the board) going
                err(f"{type(e).__name__}: {e}")
                result, note = "failed", f"{type(e).__name__}: {e}"
                failures.append((job_label(job), note))
            with lock:
                counts[result] += 1
            if board:
                board.set(index, result, note)

        def run_jobs(jobs, job_args, board=None):
            workers = max(1, min(job_args.concurrent_jobs, len(jobs)))
            if jobs and jobs[0].get("site") == "steam":
                workers = 1                     # one steamcmd at a time: they share its folder
            if workers == 1:
                for i, job in enumerate(jobs):
                    run(job, job_args, board, i)
                return
            # The first job runs alone: it settles everything shared (IPv4/IPv6 route,
            # visitor data, OAuth token, player script). The rest run side by side, each
            # with a share of the connections and no live progress line.
            run(jobs[0], job_args, board, 0)
            parallel = argparse.Namespace(**vars(job_args))
            parallel.no_progress = True
            parallel.connections = max(4, job_args.connections // workers)
            with ThreadPoolExecutor(workers) as pool:
                list(pool.map(lambda ij: run(ij[1], parallel, board, ij[0]), list(enumerate(jobs))[1:]))

        def with_board(chosen, job_args, title, numbers=None):
            from . import tui
            board = tui.Board(title, [job_label(j) for j in chosen], numbers)
            job_args.no_progress = True
            board.run(lambda: run_jobs(chosen, job_args, board))
            print(f"[select] {title}: {counts['done']} done, {counts['skipped']} already there, "
                  f"{counts['failed']} failed", file=sys.stderr)

        from . import search as search_mod
        targets = args.urls
        if not any(search_mod.looks_like_target(t) for t in args.urls):
            # Plain words: search every source (fast-dlp исполнитель - название)
            from . import tui
            query = " ".join(args.urls)
            found, summary = search_mod.search_all(query)
            if not found:
                err(f"nothing found for «{query}»")
                return 1
            print(f"[search] «{query}»: {summary}", file=sys.stderr)
            if tui.available() and args.select is not False:
                picked = pick_search(found, args, query, summary)
                if picked is None:
                    print("[search] cancelled", file=sys.stderr)
                    return 0
                chosen, job_args = picked
                with_board(chosen, job_args, t("search", q=query))
            else:
                first = dict(found[0])
                print(f"[search] taking the best match: {job_label(first)}", file=sys.stderr)
                run_jobs([first], args)
            targets = []

        for target in targets:
            try:
                jobs = expand(target, args)
            except FastDlpError as e:
                err(str(e))
                counts["failed"] += 1
                continue
            many = many or len(jobs) > 1
            if syncing:
                from . import sync
                if not jobs or not jobs[0].get("playlist"):
                    err(f"sync needs a playlist, album or channel link: {target}")
                    counts["failed"] += 1
                    continue
                music = jobs[0]["source"] in MUSIC_SOURCES or (jobs[0]["source"] == "direct" and jobs[0].get("audio"))
                def m3u_label(job):
                    track = job.get("track")
                    if track:
                        return f"{', '.join(track['artists'])} - {track['title']}"
                    if job["source"] == "direct" and job.get("author"):
                        return f"{job['author']} - {job['title']}"
                    return job_label(job)
                sync.run(jobs, args, run_jobs, m3u_label, _job_duration, music)
                continue
            if wants_picker(jobs, args):
                picked = pick_jobs(jobs, args)
                if picked is None:
                    print("[select] cancelled", file=sys.stderr)
                    continue
                chosen, job_args = picked
                with_board(chosen, job_args, collection_title(chosen),
                           [j.get("position") or i + 1 for i, j in enumerate(chosen)])
                continue
            run_jobs(jobs, args)
        for label, message in failures[:20]:
            print(f"  failed: {label}: {message}", file=sys.stderr)
        if many or len(args.urls) > 1:
            print(f"[summary] {counts['done']} done, {counts['skipped']} skipped, "
                  f"{counts['failed']} failed", file=sys.stderr)
        return 1 if counts["failed"] else 0
    except FastDlpError as e:
        err(str(e))
        return 1
    except KeyboardInterrupt:
        err("interrupted")
        return 130
