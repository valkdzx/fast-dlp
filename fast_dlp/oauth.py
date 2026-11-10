"""YouTube TV OAuth: client discovery, device-code login, refresh-token exchange.

The OAuth client id/secret are scraped from the youtube.com/tv kabuki bundle at
runtime (as CDisc Music does), cached for 6 hours.
"""
import json
import os
import re
import sys
import threading
import time
import uuid

from . import net
from .utils import FastDlpError, cache_dir, config_dir, debug

TV_PAGE = "https://www.youtube.com/tv"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DEVICE_URL = "https://oauth2.googleapis.com/device/code"
SCOPES = "http://gdata.youtube.com https://www.googleapis.com/auth/youtube"
COBALT_UA = ("Mozilla/5.0 (ChromiumStylePlatform) Cobalt/25.lts.30.1034943-gold "
             "(unlike Gecko), Unknown_TV_Unknown_0/Unknown (Unknown, Unknown)")

_BUNDLE = re.compile(r"src\s*=\s*['\"](/s/_/kabuki/_/js/[^'\"]+m=base)['\"]")
_CLIENT = re.compile(r'clientId:"([^"]+)",[^"]*?:"([^"]+)"')
_VERSION = re.compile(r'"INNERTUBE_CLIENT_VERSION":"\d+\.(\d{8})')
_PAGE_TTL = 6 * 3600
_RENEW_AHEAD = 600
_FALLBACK_DATE = "20261014"
_LOCK = threading.RLock()       # parallel jobs share one token


def _load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


class TvAuth:
    def __init__(self, refresh_token=None):
        self._client_path = os.path.join(cache_dir(), "tv_client.json")
        self._token_path = os.path.join(config_dir(), "oauth.json")
        self._tokens = _load(self._token_path)
        self._explicit = refresh_token or os.environ.get("FAST_DLP_REFRESH_TOKEN")
        if self._explicit and self._explicit != self._tokens.get("refresh_token"):
            # A different refresh token was supplied: forget the cached access token.
            self._tokens = {"refresh_token": self._explicit}

    # -- client discovery -------------------------------------------------
    def _client(self, force=False):
        cached = _load(self._client_path)
        if not force and cached.get("client_id") and time.time() - cached.get("fetched", 0) < _PAGE_TTL:
            return cached
        hdrs = {"User-Agent": COBALT_UA, "Accept-Language": "en-US,en;q=0.9"}
        page = net.get_text(TV_PAGE, hdrs)
        bundle = _BUNDLE.search(page)
        if not bundle:
            raise FastDlpError("youtube.com/tv named no kabuki bundle")
        script = net.get_text("https://www.youtube.com" + bundle.group(1), hdrs)
        found = _CLIENT.search(script)
        if not found:
            raise FastDlpError("TV bundle has no OAuth client id")
        version = _VERSION.search(page)
        cached = {
            "client_id": found.group(1),
            "client_secret": found.group(2),
            "date": version.group(1) if version else _FALLBACK_DATE,
            "fetched": time.time(),
        }
        _save(self._client_path, cached)
        debug(f"TV client refreshed, date {cached['date']}")
        return cached

    def client_version(self):
        try:
            return "5." + self._client()["date"]
        except Exception:
            return "5." + _FALLBACK_DATE

    @property
    def has_token(self):
        return bool(self._tokens.get("refresh_token"))

    # -- tokens ------------------------------------------------------------
    def access_token(self):
        with _LOCK:
            return self._access_token()

    def _access_token(self):
        tok = self._tokens
        if not tok.get("refresh_token"):
            raise FastDlpError("this video needs a signed-in account: run `fast-dlp login` "
                               "or pass --refresh-token")
        if tok.get("access_token") and tok.get("expires_at", 0) - _RENEW_AHEAD > time.time():
            return tok["access_token"]
        return self._refresh()

    def _refresh(self, retried=False):
        client = self._client(force=retried)
        status, data = net.post_form(TOKEN_URL, {
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "refresh_token": self._tokens["refresh_token"],
            "grant_type": "refresh_token",
        })
        error = data.get("error")
        if error == "invalid_client" and not retried:
            return self._refresh(retried=True)
        if error == "invalid_grant":
            raise FastDlpError("the refresh token was revoked or expired: run `fast-dlp login` again")
        if status != 200 or "access_token" not in data:
            raise FastDlpError(f"token refresh failed: HTTP {status} {error or data}")
        self._tokens["access_token"] = data["access_token"]
        self._tokens["expires_at"] = time.time() + int(data.get("expires_in", 3600))
        self._persist()
        return data["access_token"]

    def _persist(self):
        _save(self._token_path, self._tokens)

    def login(self):
        client = self._client()
        status, data = net.post_form(DEVICE_URL, {
            "client_id": client["client_id"],
            "scope": SCOPES,
            "device_id": uuid.uuid4().hex,
            "device_model": "ytlr::",
        })
        if status != 200 or "device_code" not in data:
            raise FastDlpError(f"device code request failed: HTTP {status} {data}")
        url = data.get("verification_url", "https://www.google.com/device")
        print(f"Open {url} and enter the code: {data['user_code']}", file=sys.stderr)
        interval = int(data.get("interval", 5))
        deadline = time.time() + int(data.get("expires_in", 1800))
        while time.time() < deadline:
            time.sleep(interval)
            status, tok = net.post_form(TOKEN_URL, {
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
                "code": data["device_code"],
                "grant_type": "http://oauth.net/grant_type/device/1.0",
            })
            error = tok.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 2
                continue
            if error or "refresh_token" not in tok:
                raise FastDlpError(f"login failed: {error or tok}")
            self._tokens = {
                "refresh_token": tok["refresh_token"],
                "access_token": tok["access_token"],
                "expires_at": time.time() + int(tok.get("expires_in", 3600)),
            }
            _save(self._token_path, self._tokens)
            print(f"Signed in. Token saved to {self._token_path}", file=sys.stderr)
            return
        raise FastDlpError("the login code expired")

    def logout(self):
        try:
            os.remove(self._token_path)
            print("Signed out.", file=sys.stderr)
        except FileNotFoundError:
            print("Not signed in.", file=sys.stderr)
