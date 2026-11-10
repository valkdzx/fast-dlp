"""Format listing and selection."""
from .utils import FastDlpError, human_size


def kind(f):
    mime = f.get("mimeType", "")
    return "audio" if mime.startswith("audio/") else "video"


def ext(f):
    mime = f.get("mimeType", "")
    sub = mime.split(";")[0].split("/")[-1]
    if kind(f) == "audio" and sub == "mp4":
        return "m4a"
    return sub or "bin"


def codec(f):
    mime = f.get("mimeType", "")
    if 'codecs="' in mime:
        return mime.split('codecs="')[1].split('"')[0]
    return ""


def size(f):
    return int(f.get("contentLength") or 0)


def _is_main_audio(f):
    track = f.get("audioTrack")
    return track is None or track.get("audioIsDefault", False)


_CODEC_RANK = {"av01": 3, "vp9": 2, "vp09": 2, "avc1": 1}


def _video_key(f, prefer_codec):
    c = codec(f).split(".")[0]
    preferred = 1 if prefer_codec and c.startswith(prefer_codec) else 0
    return (preferred, f.get("height") or 0, f.get("fps") or 0, _CODEC_RANK.get(c, 0), f.get("bitrate") or 0)


def _audio_key(f, container):
    same = 1 if container and ext(f) == {"mp4": "m4a", "webm": "webm"}.get(container) else 0
    return (_is_main_audio(f), same, "drc" not in f.get("xtags", ""), f.get("bitrate") or 0)


def select(formats, spec="best", max_height=None, prefer_codec=None):
    """Return a list of 1 or 2 formats: [video, audio] or [audio] or [video]."""
    formats = [f for f in formats if size(f) > 0]
    by_itag = {str(f["itag"]): f for f in formats}
    videos = [f for f in formats if kind(f) == "video"]
    audios = [f for f in formats if kind(f) == "audio"]
    if max_height:
        videos = [f for f in videos if (f.get("height") or 0) <= max_height] or videos

    def best_video():
        if not videos:
            raise FastDlpError("no video formats")
        return max(videos, key=lambda f: _video_key(f, prefer_codec))

    def best_audio(container=None):
        if not audios:
            raise FastDlpError("no audio formats")
        return max(audios, key=lambda f: _audio_key(f, container))

    if spec in ("best", "bv+ba", "bestvideo+bestaudio"):
        v = best_video()
        return [v, best_audio(ext(v))]
    if spec in ("bestaudio", "ba", "audio"):
        return [best_audio()]
    if spec in ("bestvideo", "bv"):
        return [best_video()]
    picked = []
    for part in spec.split("+"):
        if part in ("ba", "bestaudio"):
            picked.append(best_audio(ext(picked[0]) if picked else None))
        elif part in ("bv", "bestvideo"):
            picked.append(best_video())
        elif part in by_itag:
            picked.append(by_itag[part])
        else:
            raise FastDlpError(f"format {part!r} not available (use -F to list)")
    return picked


def table(formats):
    rows = ["ITAG  EXT   KIND   RES        FPS  CODEC                 BITRATE     SIZE"]
    for f in sorted(formats, key=lambda f: (kind(f), f.get("height") or 0, f.get("bitrate") or 0)):
        res = f"{f.get('width')}x{f.get('height')}" if kind(f) == "video" else "audio"
        note = ""
        if f.get("audioTrack"):
            note = " " + f["audioTrack"].get("displayName", "")
        rows.append(f"{f['itag']:<5} {ext(f):<5} {kind(f):<6} {res:<10} {f.get('fps') or '':<4} "
                    f"{codec(f)[:21]:<21} {(f.get('bitrate') or 0) // 1000:>6}k  {human_size(size(f)):>10}{note}")
    return "\n".join(rows)
