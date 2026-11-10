"""Yandex Music: tracks, albums, playlists.

With the account's OAuth token (`fast-dlp yandex-login`, device code at ya.ru/device)
files come straight from Yandex (MP3 320 with Plus). Without a token, or without Plus
(Yandex then offers a 30-second preview), the track is matched on YouTube instead, like
Spotify - the catalogue itself is public.
"""
import base64
import hashlib
import hmac
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET

from .. import net
from ..utils import FastDlpError, debug
from .common import direct, get_json, load_config, match_job, save_config

API = "https://api.music.yandex.net"
SITE = "https://music.yandex.ru"
HEADERS = {"User-Agent": "Yandex-Music-API", "X-Yandex-Music-Client": "YandexMusicAndroid/24023621",
           "Accept-Language": "ru"}
# The Yandex Music Android app's OAuth client: public, it ships inside the app.
CLIENT_ID = "23cabbbdc6cd418abb4b39c32c41195d"
CLIENT_SECRET = "53bc75238f0c4d08a118e51fe9203300"
FILE_INFO_KEY = b"p93jhgh689SBReK6ghtw62"
DOWNLOAD_SALT = "XGRlBW9FXlekgbPrRHuSiA"
_HOST = re.compile(r"^https?://(?:[\w-]+\.)*music\.yandex\.[a-z]{2,3}/", re.I)


class Preview(FastDlpError):
    """Yandex offers only a 30-second preview (no Plus): match on YouTube instead."""


def match(url):
    return bool(_HOST.match(url))


def token():
    import os
    return os.environ.get("FAST_DLP_YANDEX_TOKEN") or load_config("yandex.json").get("token")


def _call(path, params=None, data=None, method=None, auth=True):
    headers = dict(HEADERS)
    tok = token() if auth else None
    if tok:
        headers["Authorization"] = f"OAuth {tok}"
    status, body = get_json(API + path, headers, params, data, method)
    if status in (401, 403):
        raise FastDlpError(f"Yandex Music refused the token (HTTP {status}): run `fast-dlp yandex-login`")
    if status == 404:
        raise FastDlpError("Yandex Music: not found")
    if status != 200 or not isinstance(body, dict):
        raise FastDlpError(f"Yandex Music answered HTTP {status}")
    return body.get("result")


def login():
    status, code = get_json("https://oauth.yandex.ru/device/code", data={"client_id": CLIENT_ID}, method="POST")
    if status != 200 or not code or "device_code" not in code:
        raise FastDlpError(f"Yandex device code request failed: HTTP {status} {code}")
    print(f"Open {code.get('verification_url', 'https://ya.ru/device')} and enter the code: "
          f"{code['user_code']}", file=sys.stderr)
    interval = int(code.get("interval", 5))
    deadline = time.time() + int(code.get("expires_in", 300))
    while time.time() < deadline:
        time.sleep(interval)
        status, tok = get_json("https://oauth.yandex.ru/token", method="POST", data={
            "grant_type": "device_code", "code": code["device_code"],
            "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
        error = (tok or {}).get("error")
        if error == "authorization_pending":
            continue
        if error or not (tok or {}).get("access_token"):
            raise FastDlpError(f"Yandex login failed: {(tok or {}).get('error_description') or error or status}")
        path = save_config("yandex.json", {"token": tok["access_token"]})
        print(f"Signed in to Yandex Music. Token saved to {path}", file=sys.stderr)
        return
    raise FastDlpError("the Yandex login code expired")


def locate(url):
    parts = [p for p in urllib.parse.urlparse(url).path.split("/") if p]
    if "track" in parts and parts.index("track") + 1 < len(parts):
        return "track", parts[parts.index("track") + 1]
    if parts[:1] == ["album"] and len(parts) > 1:
        return "album", parts[1]
    if parts[:1] == ["users"] and len(parts) > 3 and parts[2] == "playlists":
        return "playlist", f"{parts[1]}:{parts[3]}"
    if parts[:1] == ["playlists"] and len(parts) > 1:
        return "playlist", parts[1]
    raise FastDlpError(f"not a Yandex Music track, album or playlist: {url}")


def _cover(uri):
    return f"https://{uri.replace('%%', '1000x1000')}" if uri else None


def _track_dict(t, index=None):
    album = (t.get("albums") or [{}])[0]
    title = t.get("title") or ""
    if t.get("version"):
        title += f" ({t['version']})"
    return {
        "id": str(t.get("id") or t.get("realId")),
        "title": title,
        "artists": [a.get("name") for a in t.get("artists") or [] if a.get("name")],
        "duration": round((t.get("durationMs") or 0) / 1000),
        "isrc": None,
        "album": album.get("title"),
        "album_artist": ", ".join(a.get("name", "") for a in album.get("artists") or []) or None,
        "date": str(album.get("year") or "") or None,
        "track_number": (album.get("trackPosition") or {}).get("index") or index,
        "cover": _cover(t.get("coverUri") or album.get("coverUri")),
        "url": f"{SITE}/album/{album.get('id')}/track/{t.get('id')}" if album.get("id") else f"{SITE}/track/{t.get('id')}",
    }


def _file_info(track_id):
    params = {"ts": str(int(time.time())), "trackId": track_id, "quality": "hq",
              "codecs": "flac,mp3,aac,he-aac", "transports": "raw"}
    message = "".join(params.values()).replace(",", "").encode()
    params["sign"] = base64.b64encode(hmac.new(FILE_INFO_KEY, message, hashlib.sha256).digest()).decode()[:-1]
    info = (_call("/get-file-info", params) or {}).get("downloadInfo") or {}
    url = info.get("url") or (info.get("urls") or [None])[0]
    if not url or info.get("transport", "raw") != "raw":
        return None
    if info.get("quality") == "preview":
        raise Preview("Yandex Music offers only a preview (no Plus subscription)")
    codec = info.get("codec") or "mp3"
    return {"kind": "file", "url": url, "headers": {}, "ext": {"mp3": "mp3", "flac": "flac"}.get(codec, "m4a" if "mp4" in codec else "aac")}


def _download_info(track_id):
    variants = _call(f"/tracks/{track_id}/download-info") or []
    full = [v for v in variants if not v.get("preview")]
    if not full:
        raise Preview("Yandex Music offers only a preview (no Plus subscription)")
    chosen = min(full, key=lambda v: (v.get("codec") != "mp3", -(v.get("bitrateInKbps") or 0)))
    status, raw = net.request(chosen["downloadInfoUrl"], headers=dict(HEADERS, Authorization=f"OAuth {token()}"))
    if status != 200:
        raise FastDlpError(f"Yandex Music storage answered HTTP {status}")
    fields = {c.tag: (c.text or "") for c in ET.fromstring(raw)}
    path = fields["path"]
    sign = hashlib.md5((DOWNLOAD_SALT + path[1:] + fields["s"]).encode()).hexdigest()
    return {"kind": "file", "url": f"https://{fields['host']}/get-mp3/{sign}/{fields['ts']}{path}",
            "headers": {}, "ext": "mp3" if chosen.get("codec") == "mp3" else "aac"}


def _job(track, playlist_title=None):
    if not token():
        return match_job("yandex", track, track["url"])

    def resolve(tid=track["id"]):
        try:
            found = _file_info(tid)
            if found:
                return found
        except Preview:
            raise
        except FastDlpError as e:
            debug(f"get-file-info refused {tid}: {e}; trying download-info")
        return _download_info(tid)

    meta = {"title": track["title"], "artist": ", ".join(track["artists"]), "album": track["album"],
            "album_artist": track["album_artist"], "date": track["date"],
            "track": track["track_number"], "comment": track["url"]}
    job = direct("yandex", track["id"], track["title"], ", ".join(track["artists"]), resolve,
                 audio=True, meta=meta, cover_url=track["cover"], url=track["url"])
    job["fallback_track"] = track
    return job


def expand(url, args):
    kind, ident = locate(url)
    if kind == "track":
        found = _call(f"/tracks/{ident}")
        if not found:
            raise FastDlpError("Yandex Music: no such track")
        return None, [_job(_track_dict(found[0]))]
    if kind == "album":
        album = _call(f"/albums/{ident}/with-tracks")
        rows = [t for volume in album.get("volumes") or [] for t in volume]
        title = album.get("title") or ident
        return title, [_job(_track_dict(t, i), title) for i, t in enumerate(rows, 1)]
    if ":" in ident:
        owner, kid = ident.split(":", 1)
        playlist = _call(f"/users/{owner}/playlists/{kid}")
    else:
        playlist = _call(f"/playlist/{ident}", {"richTracks": "true"})
    rows = playlist.get("tracks") or []
    full = [r["track"] for r in rows if r.get("track")]
    if len(full) < len(rows):
        ids = [f"{r['id']}:{r['albumId']}" if r.get("albumId") else str(r["id"]) for r in rows if r.get("id")]
        full = _call("/tracks", data={"track-ids": ",".join(ids)}, method="POST") or []
    title = playlist.get("title") or ident
    return title, [_job(_track_dict(t), title) for t in full]
