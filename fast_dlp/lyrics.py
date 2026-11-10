"""Song lyrics from LRCLIB (lrclib.net: free, no key), synced (LRC) when available."""
import json
import re
import urllib.parse

from . import net
from .utils import debug

API = "https://lrclib.net/api"
UA = "fast-dlp (https://github.com/valkdzx)"
_NOISE = re.compile(r"\s*[\(\[](?:official|lyric|audio|video|visualizer|hd|4k|remaster|explicit)[^\)\]]*[\)\]]", re.I)


def _clean(title):
    return _NOISE.sub("", title or "").strip()


def _get(path, params):
    status, body = net.request(f"{API}/{path}?{urllib.parse.urlencode(params)}", headers={"User-Agent": UA}, timeout=10)
    if status != 200:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def fetch(title, artist, album=None, duration=None):
    """(synced_lrc or None, plain or None). Exact lookup first, then a search close in length."""
    title, artist = _clean(title), (artist or "").split(",")[0].strip()
    if not title:
        return None, None
    params = {"track_name": title, "artist_name": artist}
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = int(duration)
    found = _get("get", params) if artist else None
    if not found and album:
        params.pop("album_name")
        found = _get("get", params)
    if not found:
        hits = _get("search", {"q": f"{artist} {title}".strip()}) or []
        if duration:
            hits = [h for h in hits if abs((h.get("duration") or 0) - duration) <= 4]
        hits.sort(key=lambda h: (not h.get("syncedLyrics"), not h.get("plainLyrics")))
        found = hits[0] if hits else None
    if not found or found.get("instrumental"):
        debug(f"lyrics: none for {artist} - {title}")
        return None, None
    return found.get("syncedLyrics") or None, found.get("plainLyrics") or None


def replaygain(lufs):
    """ReplayGain 2.0 track gain (reference -18 LUFS) from an absolute loudness, as '-10.16 dB'."""
    return f"{-18.0 - float(lufs):+.2f} dB"
