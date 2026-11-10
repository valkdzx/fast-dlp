"""Search every source at once: `fast-dlp исполнитель - название`.

YouTube Music (songs), Spotify, Yandex Music, YouTube (videos) and SoundCloud are asked in
parallel; the results become ordinary jobs, shown in the picker with their source."""
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from . import playlist, spotify, utils, ytmusic
from .utils import debug

SOURCES = ("ytmusic", "spotify", "yandex", "youtube", "soundcloud")
LABELS = {"ytmusic": "YT Music", "spotify": "Spotify", "yandex": "Яндекс", "youtube": "YouTube",
          "soundcloud": "SoundCloud"}
_LINKISH = re.compile(r"^(?:https?://|www\.|(?:[\w-]+\.)+(?:com|ru|be|net|org|io|me|it|app|tv|by|kz|ua)(?:/|$)"
                      r"|ytsearch|spotify:|audio-?\d)", re.I)


def looks_like_target(text):
    """A link, a search prefix or a bare video id - anything that is not plain words."""
    if _LINKISH.match(text) or "://" in text:
        return True
    # A bare id: 11 chars of the id alphabet with something an English word would not have.
    return bool(utils.VIDEO_ID_RE.match(text)) and bool(re.search(r"[A-Z0-9_-]", text))


def _ytmusic(query, n):
    out = []
    for c in ytmusic.search(query)[:n]:
        track = {"id": None, "title": c["title"], "artists": c["artists"], "duration": c["duration"] or 0,
                 "isrc": None, "album": None, "album_artist": None, "date": None, "track_number": None,
                 "cover": f"https://i.ytimg.com/vi/{c['video_id']}/hqdefault.jpg"}
        out.append({"source": "match", "site": "ytmusic", "track": track, "video_id": c["video_id"],
                    "url": f"https://music.youtube.com/watch?v={c['video_id']}"})
    return out


def _youtube(query, n):
    _, entries = playlist.search(f"ytsearch{n}:{query}")
    return [{"source": "youtube", "video_id": e["video_id"], "hint": e, "site": "youtube"} for e in entries]


def _spotify(query, n):
    client = spotify.Spotify()
    if not client.config.get("client_id"):
        return []
    items = client._get(f"/search?type=track&limit={n}&q={urllib.parse.quote(query)}")["tracks"]["items"]
    return [{"source": "spotify", "site": "spotify", "track": client._from_api(t),
             "url": f"https://open.spotify.com/track/{t['id']}"} for t in items]


def _yandex(query, n):
    from .sources import yandex
    found = yandex._call("/search", {"text": query, "type": "track", "page": 0}) or {}
    rows = ((found.get("tracks") or {}).get("results") or [])[:n]
    return [yandex._job(yandex._track_dict(t)) for t in rows]


def _soundcloud(query, n):
    from .sources import soundcloud
    data = soundcloud._api(f"{soundcloud.API}/search/tracks", {"q": query, "limit": n})
    return [soundcloud._job(t) for t in (data.get("collection") or []) if t.get("title")]


_SEARCHERS = {"ytmusic": _ytmusic, "spotify": _spotify, "yandex": _yandex, "youtube": _youtube,
              "soundcloud": _soundcloud}


def search_all(query, per_source=8, sources=SOURCES):
    """[job] from every source, grouped in source order; failures are skipped quietly."""
    def run(name):
        try:
            jobs = _SEARCHERS[name](query, per_source)
        except Exception as e:                       # one source down must not hide the others
            debug(f"search {name}: {e}")
            return name, []
        for j in jobs:
            j["found_on"] = name
        return name, jobs

    with ThreadPoolExecutor(len(sources)) as pool:
        results = dict(pool.map(run, sources))
    jobs = [j for name in sources for j in results.get(name, [])]
    counts = ", ".join(f"{LABELS[n]} {len(results.get(n, []))}" for n in sources if results.get(n))
    return jobs, counts
