"""What the command line writes next to a download, served by the web page instead.

--write-subs, --write-description, --write-thumbnail, --write-info-json, --write-comments,
--write-lyrics and the chapter list come as separate small files, made on request and never
stored. --embed-chapters / --sponsorblock-mark and lyrics go into the streamed file itself
(see stream.py: chapters ride in the planned header). Subtitles cannot ride along a streamed
file - a subtitle track needs fragments of its own - so on the page they are files beside it.
"""
import json

from .. import lyrics, net, postprocess, ytmeta
from ..utils import FastDlpError, sanitize_filename
from .texts import WebError

SPONSOR = ytmeta.SPONSOR_CATEGORIES
KINDS = ("thumbnail", "description", "info", "comments", "chapters", "subtitles", "lyrics")


def _args(**over):
    from .targets import cli_args
    args = cli_args()
    for k, v in over.items():
        setattr(args, k, v)
    return args


def sponsor_list(value):
    """'all' / 'sponsor,intro' / ['intro'] -> valid categories."""
    if not value:
        return []
    items = value if isinstance(value, (list, tuple)) else str(value).split(",")
    items = [str(x).strip() for x in items]
    return list(SPONSOR) if "all" in items else [x for x in items if x in SPONSOR]


def chapters(video, sponsor=()):
    """[(start_s, title)]: the player's markers or the description's timestamps, plus
    SponsorBlock segments as chapters of their own when asked."""
    cats = sponsor_list(sponsor)
    args = _args(embed_chapters=True, sponsorblock_mark=",".join(cats) if cats else None)
    return postprocess.chapters(video, args, ytmeta.Watch(video.id))


def captions(video):
    """What the page offers: [{lang, name, auto}], and the languages YouTube translates into."""
    tracks, langs = ytmeta.caption_tracks(video.raw)
    return ([{"lang": t["lang"], "name": t["name"], "auto": t["auto"]} for t in tracks],
            langs if any(t["translatable"] for t in tracks) else [])


def lyrics_text(title, artist, album=None, duration=None):
    """(text, synced) from LRCLIB, or (None, False)."""
    synced, plain = lyrics.fetch(title, artist, album, duration)
    return (synced or plain), bool(synced)


def _clock(seconds):
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _name(stem, suffix):
    return sanitize_filename(stem or "file") + suffix


def youtube_file(video, kind, params, track=None):
    """(bytes, file name, content type) of one side file of a YouTube video."""
    details = video.raw.get("videoDetails") or {}
    stem = video.title
    if kind == "thumbnail":
        for name in ("maxresdefault", "hqdefault"):
            status, body = net.request(f"https://i.ytimg.com/vi/{video.id}/{name}.jpg", timeout=15)
            if status == 200 and len(body) > 1000:
                return body, _name(stem, ".jpg"), "image/jpeg"
        raise WebError("no_extra")
    if kind == "description":
        return (details.get("shortDescription") or "").encode(), _name(stem, ".description.txt"), \
            "text/plain; charset=utf-8"
    if kind == "chapters":
        found = chapters(video, params.get("sponsor"))
        if not found:
            raise WebError("no_extra")
        text = "\n".join(f"{_clock(s)} {t}" for s, t in found) + "\n"
        return text.encode(), _name(stem, ".chapters.txt"), "text/plain; charset=utf-8"
    if kind == "comments":
        limit = max(1, min(int(params.get("limit") or 200), 2000))
        data = ytmeta.Watch(video.id).comments(limit)
        return json.dumps(data, ensure_ascii=False, indent=1).encode(), _name(stem, ".comments.json"), \
            "application/json; charset=utf-8"
    if kind == "info":
        found = chapters(video, params.get("sponsor"))
        info = postprocess.info_dict(video, ytmeta.Watch(video.id), [], [], found)
        return json.dumps(info, ensure_ascii=False, indent=1).encode(), _name(stem, ".info.json"), \
            "application/json; charset=utf-8"
    if kind == "subtitles":
        return _subtitles(video, params)
    if kind == "lyrics":
        title = (track or {}).get("title") or video.title
        artist = ", ".join((track or {}).get("artists") or []) or video.author
        text, synced = lyrics_text(title, artist, (track or {}).get("album"), video.duration)
        if not text:
            raise WebError("no_extra")
        return text.encode(), _name(f"{artist} - {title}", ".lrc" if synced else ".txt"), "text/plain; charset=utf-8"
    raise WebError("no_extra")


def _subtitles(video, params):
    lang = (params.get("lang") or "").strip()
    translate = (params.get("tlang") or "").strip() or None
    auto = str(params.get("auto") or "") in ("1", "true")
    out_fmt = "vtt" if params.get("format") == "vtt" else "srt"
    tracks, _ = ytmeta.caption_tracks(video.raw)
    pool = [t for t in tracks if t["lang"] == lang and t["auto"] == auto] or [t for t in tracks if t["lang"] == lang]
    if not pool:
        raise WebError("no_extra")
    vtt = ytmeta.fetch_caption(pool[0], translate)
    if not vtt:
        raise WebError("subs_refused")
    cues = ytmeta.vtt_cues(vtt)
    text = ytmeta.to_srt(cues) if out_fmt == "srt" else ytmeta.to_vtt(cues)
    tag = (translate or lang) + ("-auto" if auto and not translate else "")
    return text.encode(), _name(video.title, f".{tag}.{out_fmt}"), \
        ("application/x-subrip" if out_fmt == "srt" else "text/vtt") + "; charset=utf-8"


def direct_file(job, kind):
    """Side files a direct job (VK, SoundCloud, TikTok...) can have: its cover and its data."""
    if kind == "thumbnail" and job.get("cover_url"):
        status, body = net.request(job["cover_url"], timeout=15)
        if status == 200 and body:
            mime = "image/png" if body[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
            return body, _name(job["title"], ".png" if mime == "image/png" else ".jpg"), mime
    if kind == "info":
        info = {k: job.get(k) for k in ("site", "id", "title", "author", "duration", "url", "cover_url", "audio")}
        info["meta"] = {k: v for k, v in (job.get("meta") or {}).items() if isinstance(v, (str, int, float))}
        return json.dumps(info, ensure_ascii=False, indent=1).encode(), _name(job["title"], ".info.json"), \
            "application/json; charset=utf-8"
    if kind == "lyrics" and job.get("audio"):
        text, synced = lyrics_text(job["title"], job.get("author") or "", (job.get("meta") or {}).get("album"),
                                   job.get("duration"))
        if text:
            return text.encode(), _name(f"{job.get('author')} - {job['title']}", ".lrc" if synced else ".txt"), \
                "text/plain; charset=utf-8"
    raise WebError("no_extra")


def check_kind(kind):
    if kind not in KINDS:
        raise FastDlpError(f"unknown extra {kind!r}")
