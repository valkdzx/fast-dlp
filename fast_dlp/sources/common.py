"""Helpers shared by the source modules: tokens on disk, JSON over HTTP, direct jobs."""
import json
import os
import urllib.parse

from .. import net
from ..utils import FastDlpError, config_dir


def load_config(name):
    try:
        with open(os.path.join(config_dir(), name), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_config(name, data):
    path = os.path.join(config_dir(), name)
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    os.replace(path + ".tmp", path)
    return path


def get_json(url, headers=None, params=None, data=None, method=None, what="request"):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    body = urllib.parse.urlencode(data).encode() if isinstance(data, dict) else data
    hdrs = {"User-Agent": net.BROWSER_UA, "Accept": "application/json"}
    hdrs.update(headers or {})
    if body is not None and "Content-Type" not in hdrs:
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    status, raw = net.request(url, data=body, headers=hdrs, method=method)
    try:
        parsed = json.loads(raw or b"null")
    except ValueError:
        parsed = None
    return status, parsed


def ext_of(url, default="bin"):
    path = urllib.parse.urlparse(url).path
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    return ext if ext and len(ext) <= 5 and ext.isalnum() else default


def direct(site, ident, title, author, resolve, audio=False, meta=None, cover_url=None, url=None):
    return {"source": "direct", "site": site, "id": str(ident), "title": title or str(ident),
            "author": author or "", "audio": audio, "meta": meta or {}, "cover_url": cover_url,
            "resolve": resolve, "url": url}


def match_job(site, track, url):
    return {"source": "match", "site": site, "track": track, "url": url}


def need(token, setup_command, site):
    if not token:
        raise FastDlpError(f"{site} needs your account token: run `fast-dlp {setup_command}`")
    return token
