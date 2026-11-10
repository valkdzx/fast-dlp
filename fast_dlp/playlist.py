"""YouTube playlists via InnerTube /browse (no account needed for public ones)."""
import re

from . import net
from .utils import FastDlpError, debug

BROWSE_URL = "https://www.youtube.com/youtubei/v1/browse?prettyPrint=false"
WEB_VERSION = "2.20261105.05.01"
_LIST = re.compile(r"[?&]list=([A-Za-z0-9_-]{10,})")
_DURATION = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})$")
MAX_PAGES = 100


def playlist_id(text):
    """Playlist id from a URL, or None. Mixes (RD...) are endless and not supported."""
    m = _LIST.search(text)
    if not m:
        return None
    pid = m.group(1)
    return None if pid.startswith("RD") else pid


def is_playlist_only(text):
    """A playlist URL rather than a video that happens to be in one."""
    return bool(playlist_id(text)) and ("/playlist" in text or "v=" not in text)


def _browse(body):
    ctx = {"client": {"clientName": "WEB", "clientVersion": WEB_VERSION, "hl": "en", "gl": "US"}}
    status, data = net.post_json(BROWSE_URL, dict(body, context=ctx), {
        "user-agent": net.BROWSER_UA,
        "x-youtube-client-name": "1",
        "x-youtube-client-version": WEB_VERSION,
        "origin": "https://www.youtube.com",
    })
    if status != 200:
        raise FastDlpError(f"playlist browse answered HTTP {status}")
    return data


def _walk(node, key):
    if isinstance(node, dict):
        if key in node:
            yield node[key]
        for v in node.values():
            yield from _walk(v, key)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, key)


def _text(node):
    if not isinstance(node, dict):
        return ""
    if "content" in node:
        return node["content"]
    if "simpleText" in node:
        return node["simpleText"]
    return "".join(r.get("text", "") for r in node.get("runs", []))


def _clock(text):
    m = _DURATION.match(text or "")
    return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) if m else 0


def _entries(data):
    """Videos on one page: playlist rows, channel/search video cards, Shorts, new lockups."""
    for r in _walk(data, "playlistVideoRenderer"):
        if r.get("videoId"):
            yield {"video_id": r["videoId"], "title": _text(r.get("title")),
                   "author": _text(r.get("shortBylineText")),
                   "duration": int(r.get("lengthSeconds") or 0)}
    for r in _walk(data, "videoRenderer"):
        if r.get("videoId"):
            yield {"video_id": r["videoId"], "title": _text(r.get("title")),
                   "author": _text(r.get("ownerText") or r.get("shortBylineText")),
                   "duration": _clock(_text(r.get("lengthText")))}
    for r in _walk(data, "shortsLockupViewModel"):
        for cmd in _walk(r, "reelWatchEndpoint"):
            if cmd.get("videoId"):
                title = _text((r.get("overlayMetadata") or {}).get("primaryText"))
                yield {"video_id": cmd["videoId"], "title": title or cmd["videoId"], "author": "", "duration": 0}
                break
    for lock in _walk(data, "lockupViewModel"):
        if lock.get("contentType") not in (None, "LOCKUP_CONTENT_TYPE_VIDEO"):
            continue
        vid = lock.get("contentId")
        if not vid or len(vid) != 11:
            continue
        meta = lock.get("metadata", {}).get("lockupMetadataViewModel", {})
        rows = meta.get("metadata", {}).get("contentMetadataViewModel", {}).get("metadataRows", [])
        author = ""
        for row in rows:
            parts = row.get("metadataParts") or []
            if parts:
                author = _text(parts[0].get("text"))
                break
        duration = 0
        for badge in _walk(lock.get("contentImage", {}), "thumbnailBadgeViewModel"):
            m = _DURATION.match(badge.get("text", ""))
            if m:
                duration = int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        yield {"video_id": vid, "title": _text(meta.get("title")), "author": author, "duration": duration}


def _continuation(data):
    for item in _walk(data, "continuationItemRenderer"):
        token = item.get("continuationEndpoint", {}).get("continuationCommand", {}).get("token")
        if token:
            return token
    for item in _walk(data, "continuationItemViewModel"):
        for cmd in _walk(item, "continuationCommand"):
            if isinstance(cmd, dict) and cmd.get("token"):
                return cmd["token"]
    return None


def _title(data):
    for header in _walk(data, "pageHeaderViewModel"):
        title = _text(header.get("title", {}).get("dynamicTextViewModel", {}).get("text"))
        if title:
            return title
    meta = data.get("metadata", {}).get("playlistMetadataRenderer", {})
    return meta.get("title") or ""


_CHANNEL = re.compile(r"^(?:https?://)?(?:www\.|m\.)?youtube\.com/(?:@[^/?#]+|channel/UC[\w-]{22}|c/[^/?#]+|user/[^/?#]+)"
                      r"(?:/(videos|shorts|streams|featured|playlists)?)?/?(?:[?#].*)?$", re.I)
_SEARCH = re.compile(r"^ytsearch(\d*|all):(.+)$", re.I)
SEARCH_URL = "https://www.youtube.com/youtubei/v1/search?prettyPrint=false"


def is_channel(text):
    return bool(_CHANNEL.match(text))


def is_search(text):
    return bool(_SEARCH.match(text))


def _resolve_url(url):
    ctx = {"client": {"clientName": "WEB", "clientVersion": WEB_VERSION, "hl": "en", "gl": "US"}}
    status, data = net.post_json("https://www.youtube.com/youtubei/v1/navigation/resolve_url?prettyPrint=false",
                                 {"context": ctx, "url": url},
                                 {"user-agent": net.BROWSER_UA, "x-youtube-client-name": "1",
                                  "x-youtube-client-version": WEB_VERSION})
    ep = (data.get("endpoint") or {}).get("browseEndpoint") if status == 200 else None
    if not ep:
        raise FastDlpError(f"YouTube could not resolve {url}")
    return ep


def channel(url, limit=None):
    """(title, [entries]) for a channel's Videos tab (or the tab in the URL)."""
    m = _CHANNEL.match(url)
    tab = (m.group(1) or "videos").lower()
    if tab in ("featured", "playlists"):
        tab = "videos"
    base = re.sub(r"(/(videos|shorts|streams|featured|playlists))?/?([?#].*)?$", "", url)
    if not base.startswith("http"):
        base = "https://" + base
    ep = _resolve_url(f"{base}/{tab}")
    data = _browse({"browseId": ep["browseId"], **({"params": ep["params"]} if ep.get("params") else {})})
    meta = (data.get("metadata") or {}).get("channelMetadataRenderer") or {}
    title = f"{meta.get('title') or ep['browseId']} - {tab}"
    return title, _paginate(data, limit, f"channel {ep['browseId']}")


def search(text):
    """'ytsearch:q' (1), 'ytsearch10:q', 'ytsearchall:q' (up to 200) -> (title, entries)."""
    m = _SEARCH.match(text)
    count = 200 if m.group(1).lower() == "all" else int(m.group(1) or 1)
    query = m.group(2).strip()
    ctx = {"client": {"clientName": "WEB", "clientVersion": WEB_VERSION, "hl": "en", "gl": "US"}}
    hdrs = {"user-agent": net.BROWSER_UA, "x-youtube-client-name": "1", "x-youtube-client-version": WEB_VERSION}
    status, data = net.post_json(SEARCH_URL, {"context": ctx, "query": query, "params": "EgIQAQ=="}, hdrs)
    if status != 200:
        raise FastDlpError(f"YouTube search answered HTTP {status}")
    seen, entries = set(), []
    for page in range(20):
        for e in _entries(data):
            if e["video_id"] not in seen:
                seen.add(e["video_id"])
                entries.append(e)
        token = _continuation(data)
        if len(entries) >= count or not token:
            break
        status, data = net.post_json(SEARCH_URL, {"context": ctx, "continuation": token}, hdrs)
        if status != 200:
            break
    if not entries:
        raise FastDlpError(f"YouTube search found nothing for {query!r}")
    return f"search - {query}", entries[:count]


def _paginate(data, limit, what):
    seen, entries = set(), []
    for page in range(MAX_PAGES):
        added = 0
        for e in _entries(data):
            if e["video_id"] not in seen:
                seen.add(e["video_id"])
                entries.append(e)
                added += 1
        token = _continuation(data)
        debug(f"{what}: page {page + 1}, +{added} (total {len(entries)})")
        if not token or not added or (limit and len(entries) >= limit):
            break
        data = _browse({"continuation": token})
    if not entries:
        raise FastDlpError(f"{what} has no videos")
    return entries[:limit] if limit else entries


def fetch(pid):
    """Return (title, [entries]) for a playlist, following every continuation."""
    data = _browse({"browseId": "VL" + pid})
    alerts = [_text(a.get("text")) for a in _walk(data.get("alerts", []), "alertRenderer")]
    title = _title(data)
    seen, entries = set(), []
    for page in range(MAX_PAGES):
        added = 0
        for e in _entries(data):
            if e["video_id"] not in seen:
                seen.add(e["video_id"])
                entries.append(e)
                added += 1
        token = _continuation(data)
        debug(f"playlist {pid}: page {page + 1}, +{added} (total {len(entries)})")
        if not token or not added:
            break
        data = _browse({"continuation": token})
    if not entries:
        raise FastDlpError(f"playlist {pid} is empty or unavailable" + (f": {alerts[0]}" if alerts else ""))
    return title or pid, entries
