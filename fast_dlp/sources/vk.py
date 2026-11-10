"""VK Music: tracks, playlists and albums.

VK only serves audio to a few apps' tokens (Kate Mobile, VK's own), used under that
app's user agent. `fast-dlp vk-setup` gives the sign-in link (VK's own page, nothing
typed into fast-dlp but the resulting token). Streams are HLS with AES-128 segments;
hls.py decrypts them and ts.py pulls the MP3 out - no re-encoding.
"""
import os
import re
import sys
import urllib.parse

from ..utils import FastDlpError
from .common import direct, get_json, load_config, save_config

API = "https://api.vk.com/method/"
VERSION = "5.131"
KATE_ID = 2685278
UA = "KateMobileAndroid/56 lite-460 (Android 4.4.2; SDK 19; x86; unknown Android SDK built for x86; en)"
AUTH_URL = (f"https://oauth.vk.com/authorize?client_id={KATE_ID}&scope=audio,offline"
            "&redirect_uri=https://oauth.vk.com/blank.html&display=page&response_type=token&revoke=1")
_HOST = re.compile(r"^https?://(?:[\w-]+\.)*vk\.(?:com|ru)/", re.I)
_AUDIO = re.compile(r"audio(-?\d+_\d+(?:_[0-9a-f]+)?)")
_PLAYLIST = (re.compile(r"audio_playlist(-?\d+)_(\d+)(?:[/_]([0-9a-f]+))?"),
             re.compile(r"music/(?:playlist|album)/(-?\d+)_(\d+)(?:_([0-9a-f]+))?"))


def match(url):
    return bool(_HOST.match(url)) and bool(_AUDIO.search(urllib.parse.unquote(url)) or
                                          any(p.search(urllib.parse.unquote(url)) for p in _PLAYLIST))


def _config():
    cfg = load_config("vk.json")
    tok = os.environ.get("FAST_DLP_VK_TOKEN") or cfg.get("token")
    return tok, cfg.get("user_agent") or UA


def setup():
    print("Sign in to VK as Kate Mobile (the app VK still serves audio to):\n\n  "
          f"{AUTH_URL}\n\nAfter signing in, the browser lands on a blank page; copy that page's "
          "full address (it contains access_token=...) and paste it here.", file=sys.stderr)
    try:
        answer = input("Address or token: ").strip()
    except EOFError:
        raise FastDlpError("vk-setup needs an interactive terminal")
    m = re.search(r"access_token=([0-9A-Za-z._-]+)", answer)
    tok = m.group(1) if m else answer
    if not re.fullmatch(r"[0-9A-Za-z._-]{40,}", tok):
        raise FastDlpError("that does not look like a VK token")
    save_config("vk.json", {"token": tok, "user_agent": UA})
    _call("users.get")                                   # validates
    print("VK token saved.", file=sys.stderr)


def _call(method, **params):
    tok, ua = _config()
    if not tok:
        raise FastDlpError("VK Music needs an account token: run `fast-dlp vk-setup`")
    params = {k: v for k, v in params.items() if v is not None}
    params.update(access_token=tok, v=VERSION)
    status, body = get_json(API + method, {"User-Agent": ua}, data=params, method="POST")
    if status != 200 or not isinstance(body, dict):
        raise FastDlpError(f"VK answered HTTP {status}")
    err = body.get("error")
    if err:
        code, msg = err.get("error_code"), err.get("error_msg")
        if code == 5:
            raise FastDlpError(f"VK refused the token ({msg}): run `fast-dlp vk-setup`")
        raise FastDlpError(f"VK error {code}: {msg}")
    return body.get("response")


def _thumb(album):
    thumb = (album or {}).get("thumb") or {}
    for size in ("photo_1200", "photo_600", "photo_300", "photo_270"):
        if thumb.get(size):
            return thumb[size]
    return None


def _job(audio, playlist_title=None):
    key = audio.get("access_key")
    ident = f"{audio.get('owner_id')}_{audio.get('id')}" + (f"_{key}" if key else "")
    title = audio.get("title", "") + (f" ({audio['subtitle']})" if audio.get("subtitle") else "")
    album = audio.get("album") or {}

    def resolve():
        found = _call("audio.getById", audios=ident) or []
        url = (found[0].get("url") if found else "") or ""
        if not url or "audio_api_unavailable" in url:
            raise FastDlpError("VK gave no link for this track (restricted, or the token "
                               "is not a Kate Mobile token)")
        _, ua = _config()
        if ".m3u8" in urllib.parse.urlparse(url).path:
            return {"kind": "hls", "url": url, "headers": {"User-Agent": ua}}
        return {"kind": "file", "url": url, "headers": {"User-Agent": ua}, "ext": "mp3"}

    meta = {"title": title, "artist": audio.get("artist"), "album": album.get("title") or playlist_title,
            "comment": f"https://vk.com/audio{ident}"}
    job = direct("vk", ident, title, audio.get("artist", ""), resolve, audio=True, meta=meta,
                 cover_url=_thumb(album), url=f"https://vk.com/audio{ident}")
    job["duration"] = audio.get("duration")
    return job


def expand(url, args):
    text = urllib.parse.unquote(url)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(text).query).get("z", [])
    for target in q + [text]:
        for pattern in _PLAYLIST:
            m = pattern.search(target)
            if m:
                owner, pid, key = m.groups()
                meta = _call("audio.getPlaylistById", owner_id=owner, playlist_id=pid, access_key=key) or {}
                rows, offset = [], 0
                while True:
                    page = _call("audio.get", owner_id=owner, playlist_id=pid, access_key=key,
                                 offset=offset, count=200) or {}
                    items = page.get("items") or []
                    rows += items
                    offset += len(items)
                    if not items or offset >= page.get("count", 0):
                        break
                title = meta.get("title") or f"{owner}_{pid}"
                return title, [_job(a, title) for a in rows]
        m = _AUDIO.search(target)
        if m:
            found = _call("audio.getById", audios=m.group(1)) or []
            if not found:
                raise FastDlpError("VK: no such track, or it is not visible to this token")
            return None, [_job(found[0])]
    raise FastDlpError(f"not a VK track or playlist: {url}")
