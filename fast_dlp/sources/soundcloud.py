"""SoundCloud: tracks and sets (playlists/albums), via the web player's own client id."""
import re
import threading

from .. import net
from ..utils import FastDlpError, debug
from .common import direct, get_json

API = "https://api-v2.soundcloud.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
_SCRIPT = re.compile(r'<script[^>]+src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"')
_CLIENT_ID = re.compile(r'client_id\s*[:=]\s*"([A-Za-z0-9]{32})"')
_URL = re.compile(r"^https?://(?:www\.|m\.|on\.)?soundcloud\.com/", re.I)
_client_id = None
_lock = threading.Lock()


def match(url):
    return bool(_URL.match(url))


def client_id(refresh=False):
    global _client_id
    with _lock:
        if _client_id and not refresh:
            return _client_id
        page = net.get_text("https://soundcloud.com/", {"User-Agent": UA})
        for script in reversed(_SCRIPT.findall(page)):
            found = _CLIENT_ID.search(net.get_text(script, {"User-Agent": UA}))
            if found:
                _client_id = found.group(1)
                return _client_id
    raise FastDlpError("SoundCloud: could not find the web player's client id")


def _api(url, params=None):
    for attempt in range(2):
        q = dict(params or {}, client_id=client_id(refresh=attempt > 0))
        status, data = get_json(url, {"User-Agent": UA}, q)
        if status in (401, 403) and attempt == 0:
            continue
        if status == 404:
            raise FastDlpError("SoundCloud: not found (private or removed)")
        if status != 200:
            raise FastDlpError(f"SoundCloud answered HTTP {status}")
        return data
    raise FastDlpError("SoundCloud refused the client id")


def _rank(t):
    fmt = t.get("format", {})
    mime = fmt.get("mime_type", "")
    return (t.get("snipped", False), fmt.get("protocol") != "progressive",
            not ("mpeg" in mime and "mp4" not in mime), t.get("quality") != "hq")


def _artwork(track):
    url = track.get("artwork_url") or (track.get("user") or {}).get("avatar_url")
    return url.replace("-large.", "-t500x500.") if url else None


def _job(track, playlist_title=None):
    def resolve(track=track):
        fresh = _api(f"{API}/tracks/{track['id']}") if not track.get("media") else track
        for t in sorted(fresh.get("media", {}).get("transcodings", []), key=_rank):
            params = {}
            if fresh.get("track_authorization"):
                params["track_authorization"] = fresh["track_authorization"]
            try:
                url = _api(t["url"], params).get("url")
            except FastDlpError as e:
                debug(f"SoundCloud transcoding {t.get('preset')} refused: {e}")
                continue
            if url:
                if t.get("snipped"):
                    debug("SoundCloud: only a 30-second preview is offered")
                kind = "file" if t["format"]["protocol"] == "progressive" else "hls"
                return {"kind": kind, "url": url, "headers": {"User-Agent": UA}, "ext": "mp3"}
        raise FastDlpError("SoundCloud offered no stream (Go+ only or blocked in this region)")

    user = (track.get("user") or {}).get("username") or ""
    meta = {"title": track.get("title"), "artist": user, "album": playlist_title,
            "date": (track.get("release_date") or track.get("created_at") or "")[:10],
            "comment": track.get("permalink_url")}
    job = direct("soundcloud", track.get("id"), track.get("title"), user, resolve, audio=True,
                 meta=meta, cover_url=_artwork(track), url=track.get("permalink_url"))
    job["duration"] = round((track.get("full_duration") or track.get("duration") or 0) / 1000)
    return job


def expand(url, args):
    url = url.split("?")[0]
    data = _api(f"{API}/resolve", {"url": url})
    kind = data.get("kind")
    if kind == "track":
        return None, [_job(data)]
    if kind == "playlist":
        tracks = data.get("tracks") or []
        partial = [t["id"] for t in tracks if not t.get("title")]
        full = {}
        for i in range(0, len(partial), 50):
            for t in _api(f"{API}/tracks", {"ids": ",".join(map(str, partial[i:i + 50]))}) or []:
                full[t["id"]] = t
        tracks = [full.get(t["id"], t) for t in tracks]
        title = data.get("title") or "SoundCloud"
        return title, [_job(t, title) for t in tracks if t.get("title")]
    raise FastDlpError(f"SoundCloud: {kind or 'this link'} is not a track or a set")
