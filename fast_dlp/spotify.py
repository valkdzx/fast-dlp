"""Spotify track metadata. Order: the user's own Web API keys (if set), then the CDisc
backend (raw Spotify objects with ISRC, no keys needed), then the public embed page
(no ISRC). Audio itself always comes from YouTube."""
import base64
import json
import os
import re
import sys
import time
import urllib.parse

from . import net
from .utils import FastDlpError, cache_dir, config_dir, debug, warn

TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
BACKEND = os.environ.get("FAST_DLP_SPOTIFY_BACKEND", "https://2281273.xyz").rstrip("/")
_URL = re.compile(r"(?:open\.spotify\.com/(?:intl-[a-z]{2}/)?(?:embed/)?track/|spotify:track:)([A-Za-z0-9]{22})")
_ANY = re.compile(r"(?:open\.spotify\.com/(?:intl-[a-z]{2}/)?(?:embed/)?|spotify:)"
                  r"(track|album|playlist)[/:]([A-Za-z0-9]{22})")
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

SETUP_HELP = """
Spotify setup (once): fast-dlp reads track info (name, artists, ISRC) from the Spotify
Web API and finds the same track on YouTube. You need your own API keys:

  1. Open https://developer.spotify.com/dashboard and sign in.
  2. Create app: any name/description, Redirect URI http://127.0.0.1:8888/callback,
     tick "Web API", save.
  3. In the app's Settings copy the Client ID and Client secret.

Optional: without keys fast-dlp asks the CDisc backend, which also gives ISRC.
Leave the Client ID empty to skip.
"""


def track_id(text):
    m = _URL.search(text)
    return m.group(1) if m else None


def parse(text):
    """('track'|'album'|'playlist', id) for a Spotify link, or None."""
    m = _ANY.search(text)
    return (m.group(1), m.group(2)) if m else None


def _load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)


class Spotify:
    def __init__(self):
        self.config_path = os.path.join(config_dir(), "spotify.json")
        self.token_path = os.path.join(cache_dir(), "spotify_token.json")
        self.config = _load(self.config_path)
        env_id, env_secret = os.environ.get("FAST_DLP_SPOTIFY_ID"), os.environ.get("FAST_DLP_SPOTIFY_SECRET")
        if env_id and env_secret:
            self.config = {"client_id": env_id, "client_secret": env_secret}

    # -- setup --------------------------------------------------------------------
    def setup(self, force=False):
        """Ask for keys on first use (interactive terminals only). Returns True if keys are set."""
        if self.config.get("client_id") and not force:
            return True
        if self.config.get("skipped") and not force:
            return False
        if not sys.stdin.isatty():
            if force:
                raise FastDlpError("spotify-setup needs an interactive terminal")
            warn("Spotify keys are not set (run `fast-dlp spotify-setup`); matching without ISRC")
            return False
        print(SETUP_HELP, file=sys.stderr)
        try:
            client_id = input("Client ID: ").strip()
        except EOFError:
            if force:
                raise FastDlpError("no input: spotify-setup needs an interactive terminal")
            warn("no input; matching without ISRC this time")
            return False
        if not client_id:
            _save(self.config_path, {"skipped": True})
            self.config = {"skipped": True}
            print(f"Skipped. Run `fast-dlp spotify-setup` to add keys later.", file=sys.stderr)
            return False
        try:
            client_secret = input("Client secret: ").strip()
        except EOFError:
            raise FastDlpError("no Client secret entered")
        self.config = {"client_id": client_id, "client_secret": client_secret}
        self._token(force=True)          # validates the keys
        _save(self.config_path, self.config)
        print(f"Saved to {self.config_path}", file=sys.stderr)
        return True

    def _token(self, force=False):
        cached = _load(self.token_path)
        if (not force and cached.get("client_id") == self.config["client_id"]
                and cached.get("expires_at", 0) - 60 > time.time()):
            return cached["access_token"]
        basic = base64.b64encode(f"{self.config['client_id']}:{self.config['client_secret']}".encode()).decode()
        status, data = net.post_form(TOKEN_URL, {"grant_type": "client_credentials"},
                                     {"Authorization": "Basic " + basic})
        if status != 200 or "access_token" not in data:
            raise FastDlpError(f"Spotify rejected the keys: {data.get('error_description') or data.get('error') or status}")
        _save(self.token_path, {"client_id": self.config["client_id"], "access_token": data["access_token"],
                                "expires_at": time.time() + int(data.get("expires_in", 3600))})
        return data["access_token"]

    def has_keys(self):
        return bool(self.config.get("client_id"))

    # -- metadata -----------------------------------------------------------------
    def track(self, tid):
        """Return {title, artists, duration, isrc, album, cover, id}."""
        if self.has_keys():
            try:
                return self._api_track(tid)
            except FastDlpError as e:
                warn(f"{e}; trying the backend")
        try:
            return self._backend_track(tid)
        except FastDlpError as e:
            warn(f"{e}; falling back to the public page (no ISRC)")
        return self._embed_track(tid)

    # -- backend (2281273.xyz, beta=true -> raw Spotify JSON) -------------------------
    @staticmethod
    def _backend(kind, sid, extra=""):
        link = urllib.parse.quote(f"https://open.spotify.com/{kind}/{sid}", safe="")
        try:
            # Cloudflare in front of it refuses the default Python-urllib agent.
            status, body = net.request(f"{BACKEND}/spotify?url={link}&beta=true{extra}",
                                       headers={"User-Agent": "fast-dlp"}, timeout=60)
        except OSError as e:
            raise FastDlpError(f"Spotify backend unreachable: {e}")
        try:
            data = json.loads(body)
        except ValueError:
            raise FastDlpError(f"Spotify backend answered {status} with no JSON")
        if status != 200 or (isinstance(data, dict) and data.get("error")):
            raise FastDlpError(f"Spotify backend: {data.get('error') if isinstance(data, dict) else status}")
        return data

    def _backend_track(self, tid):
        out = self._from_api(self._backend("track", tid))
        debug(f"Spotify backend: {out}")
        return out

    def _backend_collection(self, kind, cid):
        d = self._backend(kind, cid)
        if d.get("truncated"):
            warn(f"Spotify backend gave {d.get('count') or len(d.get('tracks') or [])} of {d.get('total')} tracks")
        is_album = kind == "album"
        tracks = []
        for t in d.get("tracks") or []:
            tracks.append({
                "id": t.get("id"),
                "title": t.get("title"),
                "artists": list(t.get("artists") or []),
                "duration": round((t.get("duration_ms") or 0) / 1000),
                "isrc": t.get("isrc"),
                "album": t.get("album"),
                "album_artist": ", ".join(d.get("artists") or []) or None if is_album else None,
                "date": d.get("release_date") if is_album else None,
                "track_number": t.get("track_number") if is_album else None,
                "cover": d.get("cover") if is_album else None,   # playlists: filled by complete()
            })
        return d.get("name") or cid, tracks

    def complete(self, track):
        """Playlist items lack cover/date/album artist (and ISRC on the embed fallback):
        fill them from the full track, in place, right before the track is downloaded."""
        if not track.get("id") or (track.get("cover") and track.get("isrc")):
            return track
        try:
            full = self.track(track["id"]) if self.has_keys() else self._backend_track(track["id"])
        except FastDlpError as e:
            debug(f"Spotify complete {track['id']}: {e}")
            return track
        for key, value in full.items():
            if value and not track.get(key) and key != "track_number":
                track[key] = value
        return track

    def _get(self, path):
        url = path if path.startswith("http") else API + path
        status, body = net.request(url, headers={"Authorization": "Bearer " + self._token()})
        if status == 401:
            status, body = net.request(url, headers={"Authorization": "Bearer " + self._token(force=True)})
        if status != 200:
            raise FastDlpError(f"Spotify API answered {status} for {path.split('?')[0]}")
        return json.loads(body)

    @staticmethod
    def _from_api(d, album=None):
        album = d.get("album") or album or {}
        images = album.get("images") or []
        return {
            "id": d.get("id"),
            "title": d["name"],
            "artists": [a["name"] for a in d.get("artists", [])],
            "duration": round(d.get("duration_ms", 0) / 1000),
            "isrc": (d.get("external_ids") or {}).get("isrc"),
            "album": album.get("name"),
            "album_artist": ", ".join(a["name"] for a in album.get("artists", [])) or None,
            "date": album.get("release_date"),
            "track_number": d.get("track_number"),
            "cover": images[0]["url"] if images else None,
        }

    def _api_track(self, tid):
        out = self._from_api(self._get(f"/tracks/{tid}"))
        debug(f"Spotify API: {out}")
        return out

    # -- albums and playlists -------------------------------------------------------
    def collection(self, kind, cid):
        """(title, [track dicts]) for an album or playlist."""
        if self.has_keys():
            try:
                return self._api_album(cid) if kind == "album" else self._api_playlist(cid)
            except FastDlpError as e:
                warn(f"{e}; trying the backend")
        try:
            return self._backend_collection(kind, cid)
        except FastDlpError as e:
            warn(f"{e}; falling back to the public page (no ISRC)")
        return self._embed_collection(kind, cid)

    def _full_tracks(self, ids):
        """Album track listings carry no ISRC; the full track objects do (50 per call)."""
        full = {}
        for i in range(0, len(ids), 50):
            for d in self._get("/tracks?ids=" + ",".join(ids[i:i + 50])).get("tracks") or []:
                if d:
                    full[d["id"]] = d
        return full

    def _api_album(self, cid):
        album = self._get(f"/albums/{cid}")
        items, page = list(album["tracks"]["items"]), album["tracks"]
        while page.get("next"):
            page = self._get(page["next"])
            items += page["items"]
        full = self._full_tracks([t["id"] for t in items if t.get("id")])
        tracks = [self._from_api(full.get(t["id"], t), album) for t in items]
        return album["name"], tracks

    def _api_playlist(self, cid):
        meta = self._get(f"/playlists/{cid}?fields=name")
        tracks, url = [], f"/playlists/{cid}/tracks?limit=100"
        while url:
            page = self._get(url)
            for item in page.get("items", []):
                t = item.get("track")
                if t and t.get("type", "track") == "track" and t.get("id"):
                    tracks.append(self._from_api(t))
            url = page.get("next")
        return meta.get("name") or cid, tracks

    def _embed_collection(self, kind, cid):
        page = net.get_text(f"https://open.spotify.com/embed/{kind}/{cid}")
        m = _NEXT_DATA.search(page)
        if not m:
            raise FastDlpError(f"Spotify embed page carried no {kind} data")
        try:
            e = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
        except (ValueError, KeyError) as exc:
            raise FastDlpError(f"Spotify embed page changed: {exc}")
        cover = None
        for src in (e.get("coverArt") or {}).get("sources") or []:
            cover = src.get("url") or cover
        tracks = []
        for i, t in enumerate(e.get("trackList") or [], 1):
            uri = t.get("uri", "")
            tracks.append({
                "id": uri.rsplit(":", 1)[-1] if uri else None,
                "title": t.get("title"),
                "artists": [a.strip() for a in (t.get("subtitle") or "").split(",") if a.strip()],
                "duration": round((t.get("duration") or 0) / 1000),
                "isrc": None,
                "album": e.get("name") if kind == "album" else None,
                "album_artist": None,
                "date": None,
                "track_number": i if kind == "album" else None,
                "cover": cover if kind == "album" else None,
            })
        return e.get("name") or e.get("title") or cid, tracks

    def _embed_track(self, tid):
        page = net.get_text(f"https://open.spotify.com/embed/track/{tid}")
        m = _NEXT_DATA.search(page)
        if not m:
            raise FastDlpError("Spotify embed page carried no track data")
        try:
            e = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
        except (ValueError, KeyError) as exc:
            raise FastDlpError(f"Spotify embed page changed: {exc}")
        cover = None
        for src in (e.get("visualIdentity") or {}).get("image") or (e.get("coverArt") or {}).get("sources") or []:
            if isinstance(src, dict) and src.get("url"):
                cover = src["url"]
        out = {
            "id": tid,
            "title": e.get("name") or e.get("title"),
            "artists": [a["name"] for a in e.get("artists", [])],
            "duration": round((e.get("duration") or 0) / 1000),
            "isrc": None,
            "album": None,
            "album_artist": None,
            "date": ((e.get("releaseDate") or {}).get("isoString") or "")[:10] or None,
            "track_number": None,
            "cover": cover,
        }
        debug(f"Spotify embed: {out}")
        return out
