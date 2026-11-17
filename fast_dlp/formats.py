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


def is_drc(f):
    """Audio with dynamic range compression (the "stable volume" copy of a track). The
    xtags field is base64 protobuf, so the plain flag is what to look at."""
    if f.get("isDrc"):
        return True
    tags = f.get("xtags") or ""
    if "drc" in tags:
        return True
    try:
        import base64
        return b"drc" in base64.urlsafe_b64decode(tags + "=" * (-len(tags) % 4))
    except (ValueError, TypeError):
        return False


def format_id(f):
    """What -f takes for this format: the itag, '140-drc' for the DRC copy, '251-de' for a
    dubbed audio track (the same itag exists once per language)."""
    fid = str(f["itag"])
    track = f.get("audioTrack")
    if track and not track.get("audioIsDefault", False):
        fid += "-" + (track.get("id") or "x").split(".")[0]
    if is_drc(f):
        fid += "-drc"
    return fid


def _audio_key(f, container):
    same = 1 if container and ext(f) == {"mp4": "m4a", "webm": "webm"}.get(container) else 0
    return (_is_main_audio(f), same, not is_drc(f), f.get("bitrate") or 0)


def select(formats, spec="best", max_height=None, prefer_codec=None):
    """Return a list of 1 or 2 formats: [video, audio] or [audio] or [video]."""
    formats = [f for f in formats if size(f) > 0]
    by_itag = {}
    # A bare itag means the main copy (original language, no DRC) when there are several.
    for f in sorted(formats, key=lambda f: _is_main_audio(f) and not is_drc(f), reverse=True):
        by_itag.setdefault(format_id(f), f)
        by_itag.setdefault(str(f["itag"]), f)
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


CODEC_NAMES = {"avc1": "H.264", "av01": "AV1", "vp9": "VP9", "vp09": "VP9", "hev1": "H.265", "hvc1": "H.265",
               "opus": "Opus", "vorbis": "Vorbis", "ec-3": "E-AC-3", "ac-3": "AC-3"}
AAC_PROFILES = {"2": "AAC-LC", "5": "HE-AAC", "29": "HE-AACv2"}


def codec_name(f):
    """'H.264', 'AV1', 'AAC-LC', 'Opus'... from the codecs string."""
    raw = codec(f)
    head = raw.split(".")[0].lower()
    if head == "mp4a":
        return AAC_PROFILES.get(raw.split(".")[-1], "AAC")
    return CODEC_NAMES.get(head, raw or "?")


def quality(f):
    """'2160p60 HDR', '720p'..."""
    h = f.get("height") or 0
    fps = f.get("fps") or 0
    label = f"{h}p{fps if fps > 30 else ''}"
    if "HDR" in (f.get("qualityLabel") or ""):
        label += " HDR"
    return label


def _bitrate(f, t):
    b = f.get("averageBitrate") or f.get("bitrate") or 0
    return t("lf_mbps", n=f"{b / 1e6:.1f}") if b >= 1_000_000 else t("lf_kbps", n=b // 1000)


def _grid(rows, right=()):
    """Rows of cells -> aligned lines; columns in `right` are right-aligned."""
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    out = []
    for r in rows:
        cells = [str(c).rjust(w) if i in right else str(c).ljust(w) for i, (c, w) in enumerate(zip(r, widths))]
        out.append("  " + "   ".join(cells).rstrip())
    return out


def table(formats, chosen=(), lang_t=None):
    """The -F listing: video and audio apart, best first, the default choice starred."""
    from .i18n import t
    t = lang_t or t
    formats = [f for f in formats if size(f) > 0]
    chosen_ids = {id(f) for f in chosen}
    star = lambda f: "*" if id(f) in chosen_ids else ""                   # noqa: E731
    videos = sorted((f for f in formats if kind(f) == "video"),
                    key=lambda f: _video_key(f, None), reverse=True)
    audios = sorted((f for f in formats if kind(f) == "audio"),
                    key=lambda f: _audio_key(f, None), reverse=True)
    lines = []
    if videos:
        rows = [(t("lf_id"), t("lf_ext"), t("lf_quality"), t("lf_res"), t("lf_codec"), t("lf_bitrate"),
                 t("lf_size"), "")]
        for f in videos:
            rows.append((format_id(f), ext(f), quality(f), f"{f.get('width')}x{f.get('height')}", codec_name(f),
                         _bitrate(f, t), human_size(size(f)), star(f)))
        lines.append(t("lf_video", n=len(videos)))
        lines += _grid(rows, right={5, 6})
    if audios:
        rows = [(t("lf_id"), t("lf_ext"), t("lf_codec"), t("lf_bitrate"), t("lf_size"), "", t("lf_note"))]
        for f in audios:
            notes = []
            track = f.get("audioTrack")
            if track:
                notes.append(track.get("displayName") or track.get("id") or "")
            if is_drc(f):
                notes.append(t("lf_drc"))
            ch = f.get("audioChannels") or 2
            if ch > 2:
                notes.append({6: "5.1", 8: "7.1"}.get(ch, f"{ch} ch"))
            rate = f.get("audioSampleRate")
            if rate:
                notes.append(f"{int(rate) / 1000:g} kHz")
            rows.append((format_id(f), ext(f), codec_name(f), _bitrate(f, t), human_size(size(f)), star(f),
                         ", ".join(n for n in notes if n)))
        if lines:
            lines.append("")
        lines.append(t("lf_audio", n=len(audios)))
        lines += _grid(rows, right={3, 4})
    return chr(10).join(lines)
