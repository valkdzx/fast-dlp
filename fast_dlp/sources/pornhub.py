"""Pornhub videos: the best MP4 up to -q, downloaded in parallel ranges.

The watch page carries `flashvars_<id>` with the title, length, preview and a list of
formats. The MP4 entry points at /video/get_media, which answers with one direct file per
quality, but only to the session that loaded the page (its cookies). The files are tied
to the IP and throttled per connection, so the parallel range downloader is what makes
them fast. HLS links from the same list are the fallback.

The site's own 18+ notice is a click-through; the cookies it sets are sent here. Where a
country requires real age verification the site serves a different page instead: that is
reported, not worked around.
"""
import html
import http.cookiejar
import json
import re
import urllib.error
import urllib.request

from .. import net
from ..utils import FastDlpError, debug, warn
from .common import direct

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
_VIDEO = re.compile(r"(?:^|//|\.)pornhub\.(?:com|org|net)/(?:view_video\.php\?(?:[^#]*&)?viewkey=|embed/)([0-9a-z]+)", re.I)
_FLASHVARS = re.compile(r"var\s+flashvars_\d+\s*=\s*(\{.+?\});\s*\n")
_AUTHOR = re.compile(r'"author"\s*:\s*"([^"]+)"')


def match(url):
    return bool(_VIDEO.search(url))


class _Session:
    """One cookie jar per video: get_media only answers the session that loaded the page."""

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        for name in ("accessAgeDisclaimerPH", "age_verified", "accessAgeDisclaimerUK"):
            self.jar.set_cookie(http.cookiejar.Cookie(
                0, name, "1", None, False, ".pornhub.com", True, True, "/", True, False, None, False,
                None, None, {}))
        self.opener = urllib.request.build_opener(
            *net.proxy_handlers(), urllib.request.HTTPSHandler(context=net.SSL_CONTEXT),
            urllib.request.HTTPCookieProcessor(self.jar))

    def get(self, url, referer=None):
        headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8"}
        if referer:
            headers["Referer"] = referer
        try:
            with self.opener.open(urllib.request.Request(url, headers=headers), timeout=20) as r:
                return r.status, r.geturl(), r.read()
        except urllib.error.HTTPError as e:
            return e.code, url, e.read()


def _page(key):
    s = _Session()
    url = f"https://www.pornhub.com/view_video.php?viewkey={key}"
    status, final, body = s.get(url)
    text = body.decode("utf-8", "replace")
    if status == 404:
        raise FastDlpError(f"Pornhub: video {key} not found")
    m = _FLASHVARS.search(text)
    if not m:
        if re.search(r"age.?verif|verify your age", text, re.I):
            raise FastDlpError("Pornhub: in this country the site asks for real age verification "
                               "(fast-dlp does not get around it)")
        if status != 200:
            raise FastDlpError(f"Pornhub: HTTP {status} for {key}")
        raise FastDlpError(f"Pornhub: no player data on the page of {key} (removed or private?)")
    flash = json.loads(m.group(1))
    if flash.get("video_unavailable") in (True, "true"):
        where = " in this country" if flash.get("video_unavailable_country") in (True, "true") else ""
        raise FastDlpError(f"Pornhub: video {key} is unavailable{where}")
    author = _AUTHOR.search(text)
    return s, final, flash, html.unescape(author.group(1)) if author else ""


def _formats(session, page_url, flash):
    """[(height, kind, url)] best first: direct MP4s from get_media, then HLS."""
    out = []
    for d in flash.get("mediaDefinitions") or []:
        if d.get("format") == "mp4" and d.get("videoUrl"):
            status, _, body = session.get(d["videoUrl"], referer=page_url)
            try:
                files = json.loads(body) if status == 200 else []
            except ValueError:
                files = []
            for f in files if isinstance(files, list) else []:
                if f.get("videoUrl"):
                    out.append((int(f.get("height") or f.get("quality") or 0), "file", f["videoUrl"]))
    for d in flash.get("mediaDefinitions") or []:
        if d.get("format") == "hls" and d.get("videoUrl"):
            try:
                height = int(d.get("quality") or 0)
            except (TypeError, ValueError):
                height = 0
            out.append((height, "hls", d["videoUrl"]))
    # MP4 before HLS at the same height: one file, ranged, nothing to join.
    return sorted(out, key=lambda f: (f[0], f[1] == "file"), reverse=True)


def _pick(formats, max_height):
    usable = [f for f in formats if not max_height or f[0] <= max_height] or formats[-1:]
    if not usable:
        raise FastDlpError("Pornhub: the page lists no playable formats")
    return usable[0]


def expand(url, args):
    key = _VIDEO.search(url).group(1)
    session, page_url, flash, author = _page(key)
    title = html.unescape(flash.get("video_title") or key).strip()
    if getattr(args, "audio_only", False):
        warn("Pornhub: no separate audio stream; downloading the video")
    state = {"first": (session, page_url, flash)}

    def resolve():
        # The first call reuses the page just read; later ones (an expired link) load it again.
        s, page, fv = state.pop("first", None) or _page(key)[:3]
        height, kind, link = _pick(_formats(s, page, fv), getattr(args, "max_height", None))
        debug(f"Pornhub {key}: {height}p {kind}")
        return {"kind": kind, "url": link, "ext": "mp4", "headers": {"User-Agent": UA, "Referer": page}}

    meta = {"title": title, "artist": author, "comment": f"https://www.pornhub.com/view_video.php?viewkey={key}"}
    job = direct("pornhub", key, title, author, resolve, meta=meta, cover_url=flash.get("image_url"),
                 url=f"https://www.pornhub.com/view_video.php?viewkey={key}")
    try:
        job["duration"] = int(flash.get("video_duration") or 0) or None
    except (TypeError, ValueError):
        pass
    return None, [job]
