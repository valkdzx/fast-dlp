"""YouTube Music search and matching a known track (title, artists, duration, ISRC) to a video."""
import difflib
import re
import unicodedata

from . import net
from .utils import debug

SEARCH_URL = "https://music.youtube.com/youtubei/v1/search?prettyPrint=false"
CLIENT_VERSION = "1.20261101.01.00"
FILTER_SONGS = "EgWKAQIIAWoKEAkQBRAKEAMQBA=="
FILTER_VIDEOS = "EgWKAQIQAWoKEAkQChAFEAMQBA=="
_DURATION = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})$")


def search(query, params=FILTER_SONGS):
    """Return [{video_id, title, artists, duration}] from a YouTube Music search."""
    body = {
        "context": {"client": {"clientName": "WEB_REMIX", "clientVersion": CLIENT_VERSION,
                               "hl": "en", "gl": "US"}},
        "query": query,
    }
    if params:
        body["params"] = params
    status, data = net.post_json(SEARCH_URL, body, {
        "origin": "https://music.youtube.com",
        "referer": "https://music.youtube.com/",
        "user-agent": net.BROWSER_UA,
        "x-youtube-client-name": "67",
        "x-youtube-client-version": CLIENT_VERSION,
    })
    if status != 200:
        raise IOError(f"YouTube Music search answered {status}")
    return [r for r in (_parse(item) for item in _items(data)) if r]


def _items(node):
    if isinstance(node, dict):
        if "musicResponsiveListItemRenderer" in node:
            yield node["musicResponsiveListItemRenderer"]
            return
        for v in node.values():
            yield from _items(v)
    elif isinstance(node, list):
        for v in node:
            yield from _items(v)


def _runs(col):
    return col.get("musicResponsiveListItemFlexColumnRenderer", {}).get("text", {}).get("runs", [])


def _parse(item):
    video_id = (item.get("playlistItemData") or {}).get("videoId")
    cols = item.get("flexColumns") or []
    if not video_id or len(cols) < 2:
        return None
    title = "".join(r.get("text", "") for r in _runs(cols[0]))
    artists, duration = [], None
    for run in _runs(cols[1]):
        text = run.get("text", "").strip()
        m = _DURATION.match(text)
        if m:
            duration = int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            continue
        browse = run.get("navigationEndpoint", {}).get("browseEndpoint", {})
        page = browse.get("browseEndpointContextSupportedConfigs", {}) \
            .get("browseEndpointContextMusicConfig", {}).get("pageType", "")
        if page == "MUSIC_PAGE_TYPE_ARTIST" or (page == "MUSIC_PAGE_TYPE_USER_CHANNEL"):
            artists.append(text)
    if not artists:
        first = "".join(r.get("text", "") for r in _runs(cols[1])).split(" • ")[0]
        artists = [first]
    return {"video_id": video_id, "title": title, "artists": artists, "duration": duration}


# -- matching -------------------------------------------------------------------

_NOISE = re.compile(r"\((?:feat|ft|with|prod)[^)]*\)|\[(?:feat|ft|with|prod)[^\]]*\]|"
                    r"\s-\s.*(?:remaster|version|edit|mix).*$", re.I)


def _norm(text):
    text = unicodedata.normalize("NFKC", text).casefold()
    text = _NOISE.sub("", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def _sim(a, b):
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.9
    return difflib.SequenceMatcher(None, a, b).ratio()


def score(track, cand):
    title = _sim(track["title"], cand["title"])
    artist = max((_sim(a, c) for a in track["artists"] for c in cand["artists"]), default=0.0)
    # Reuploads often put the artist into the title instead.
    artist = max(artist, max((0.8 for a in track["artists"] if _norm(a) and _norm(a) in _norm(cand["title"])),
                             default=0.0))
    if track.get("duration") and cand.get("duration"):
        diff = abs(track["duration"] - cand["duration"])
        dur = max(0.0, 1.0 - diff / 15.0)
    else:
        diff, dur = None, 0.5
    return 0.5 * title + 0.3 * artist + 0.2 * dur, title, artist, diff


def candidates(track):
    """Yield (candidate, how) best-first for track {title, artists, duration (s), isrc?}:
    the ISRC hit, then songs, then videos (reuploads), each scored and deduplicated.
    Lazy, so later searches run only if earlier candidates could not be downloaded."""
    seen = set()
    if track.get("isrc"):
        results = search(track["isrc"])
        if results:
            s, t, a, diff = score(track, results[0])
            debug(f"ISRC top hit: {results[0]['title']} / {results[0]['artists']} score={s:.2f} diff={diff}")
            if (diff is not None and diff <= 5 and (t >= 0.4 or a >= 0.5)) or t >= 0.8:
                seen.add(results[0]["video_id"])
                yield results[0], "ISRC"
    query = " ".join(track["artists"][:2]) + " " + track["title"]
    for label, params, threshold in (("songs", FILTER_SONGS, 0.72), ("videos", FILTER_VIDEOS, 0.7)):
        scored = []
        for cand in search(query, params)[:10]:
            s, t, a, diff = score(track, cand)
            if (diff is not None and diff > 15) or t < 0.5 or s < threshold:
                continue
            scored.append((s, cand))
        for s, cand in sorted(scored, key=lambda x: -x[0]):
            if cand["video_id"] in seen:
                continue
            seen.add(cand["video_id"])
            debug(f"{label} candidate: {cand['title']} / {cand['artists']} score={s:.2f}")
            yield cand, label


def find(track):
    """Best candidate only: (candidate, how) or (None, reason)."""
    for cand, how in candidates(track):
        return cand, how
    return None, "no match on YouTube Music"
