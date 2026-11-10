"""Extra YouTube metadata: upload date, chapters, comments (InnerTube /next), captions
(from the player response) and SponsorBlock segments. Fetched only when something asks."""
import datetime
import html
import json
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from . import net
from .utils import debug

NEXT_URL = "https://www.youtube.com/youtubei/v1/next?prettyPrint=false"
WEB_VERSION = "2.20261105.05.01"
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep",
                                        "oct", "nov", "dec"), 1)}
SPONSOR_CATEGORIES = ("sponsor", "intro", "outro", "selfpromo", "preview", "filler", "interaction",
                      "music_offtopic")
SPONSOR_NAMES = {"sponsor": "Sponsor", "intro": "Intro", "outro": "Outro", "selfpromo": "Self-promotion",
                 "preview": "Preview", "filler": "Filler", "interaction": "Interaction reminder",
                 "music_offtopic": "Non-music"}


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
    return node.get("simpleText") or node.get("content") or "".join(r.get("text", "") for r in node.get("runs", []))


def _post(body):
    ctx = {"client": {"clientName": "WEB", "clientVersion": WEB_VERSION, "hl": "en", "gl": "US"}}
    status, data = net.post_json(NEXT_URL, dict(body, context=ctx), {
        "user-agent": net.BROWSER_UA, "x-youtube-client-name": "1", "x-youtube-client-version": WEB_VERSION,
        "origin": "https://www.youtube.com"})
    return data if status == 200 else {}


class Watch:
    """The /next answer for one video, read lazily and once."""

    def __init__(self, video_id):
        self.video_id = video_id
        self._data = None

    @property
    def data(self):
        if self._data is None:
            self._data = _post({"videoId": self.video_id})
        return self._data

    def upload_date(self):
        """'YYYYMMDD' or None (relative dates like '3 hours ago' are not exact)."""
        for node in list(_walk(self.data, "dateText")) + list(_walk(self.data, "publishDate")):
            parsed = parse_date(_text(node))
            if parsed:
                return parsed
        return None

    def chapters(self, duration=None):
        """[(start_seconds, title)] from the player bar's chapter markers."""
        found = []
        for markers in _walk(self.data, "markersMap"):
            for entry in markers or []:
                if entry.get("key") in ("DESCRIPTION_CHAPTERS", "AUTO_CHAPTERS"):
                    for ch in (entry.get("value") or {}).get("chapters") or []:
                        r = ch.get("chapterRenderer") or {}
                        found.append((r.get("timeRangeStartMillis", 0) / 1000.0, _text(r.get("title"))))
            if found:
                break
        if not found:
            seen = set()
            for r in _walk(self.data, "chapterRenderer"):
                start = r.get("timeRangeStartMillis", 0) / 1000.0
                if start not in seen:
                    seen.add(start)
                    found.append((start, _text(r.get("title"))))
        found.sort()
        return [(s, t) for s, t in found if t and (not duration or s < duration)]

    def comments(self, limit=200, threads=4):
        """Top comments (not replies): [{id, author, text, likes, replies, published}]."""
        token = None
        for section in _walk(self.data, "itemSectionRenderer"):
            if section.get("sectionIdentifier") == "comment-item-section":
                for cmd in _walk(section, "continuationCommand"):
                    token = cmd.get("token")
                    break
        out, seen = [], set()
        while token and len(out) < limit:
            page = _post({"continuation": token})
            if not page:
                break
            for mut in (m for ms in _walk(page, "mutations") for m in ms):
                p = (mut.get("payload") or {}).get("commentEntityPayload")
                if not p:
                    continue
                props = p.get("properties") or {}
                cid = props.get("commentId")
                if cid in seen or (props.get("replyLevel") or 0) > 0:
                    continue
                seen.add(cid)
                tb = p.get("toolbar") or {}
                out.append({"id": cid, "author": (p.get("author") or {}).get("displayName"),
                            "text": (props.get("content") or {}).get("content"),
                            "likes": tb.get("likeCountNotliked"), "replies": tb.get("replyCount"),
                            "published": props.get("publishedTime")})
            token = None
            for action in page.get("onResponseReceivedEndpoints") or []:
                cmd = action.get("reloadContinuationItemsCommand") or action.get("appendContinuationItemsAction") or {}
                items = cmd.get("continuationItems") or []
                if items and "continuationItemRenderer" in items[-1]:
                    for c in _walk(items[-1], "continuationCommand"):
                        token = c.get("token")
                        break
        return out[:limit]


def parse_date(text):
    m = re.search(r"([A-Z][a-z]{2})[a-z]* (\d{1,2}), (\d{4})", text or "")
    if m and m.group(1).lower() in _MONTHS:
        return f"{m.group(3)}{_MONTHS[m.group(1).lower()]:02d}{int(m.group(2)):02d}"
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text or "")
    if m:
        return "".join(m.groups())
    return None


def description_chapters(description, duration=None):
    """'0:00 Intro' style chapters written into a description (needs >=2 and one at 0:00)."""
    found = []
    for line in (description or "").splitlines():
        m = re.match(r"^\s*(?:[-•▶►]\s*)?\(?((?:\d{1,2}:)?\d{1,2}:\d{2})\)?\s*[-–—:|]?\s*(.+?)\s*$", line)
        if m:
            parts = [int(x) for x in m.group(1).split(":")]
            secs = sum(v * 60 ** i for i, v in enumerate(reversed(parts)))
            if not duration or secs < duration:
                found.append((float(secs), m.group(2)))
    found.sort()
    if len(found) >= 2 and found[0][0] == 0:
        return found
    return []


def sponsorblock(video_id, categories):
    """[(start, end, category)] from sponsor.ajay.app (public API, no key)."""
    cats = json.dumps([c for c in categories if c in SPONSOR_CATEGORIES])
    url = f"https://sponsor.ajay.app/api/skipSegments?videoID={video_id}&categories={urllib.parse.quote(cats)}"
    status, body = net.request(url, headers={"User-Agent": "fast-dlp"}, timeout=10)
    if status != 200:
        debug(f"SponsorBlock: HTTP {status} for {video_id}")
        return []
    try:
        return sorted((float(s["segment"][0]), float(s["segment"][1]), s["category"]) for s in json.loads(body))
    except (ValueError, KeyError, TypeError):
        return []


def merge_sponsor_chapters(chapters, segments, duration):
    """Chapter list with SponsorBlock segments cut in as their own chapters."""
    if not segments:
        return chapters
    base = chapters or [(0.0, "Video")]
    bounds = []                                                   # (start, title)
    for start, end, cat in segments:
        bounds.append((start, f"[SponsorBlock] {SPONSOR_NAMES.get(cat, cat)}"))
        # after the segment, the chapter that was running continues
        resume = [t for s, t in base if s <= end]
        bounds.append((end, resume[-1] if resume else base[0][1]))
    merged = {}
    for s, t in list(base) + bounds:
        if duration and s >= duration:
            continue
        merged[round(s, 3)] = t
    points = sorted(merged.items())
    # A boundary less than a second after another replaces it (no 0.7 s slivers), then
    # neighbours with the same title collapse into one chapter.
    cleaned = []
    for s, t in points:
        if cleaned and s - cleaned[-1][0] < 1.0:
            cleaned[-1] = (cleaned[-1][0] if cleaned[-1][1] == t else s, t)
            if len(cleaned) == 1:
                cleaned[-1] = (min(cleaned[-1][0], points[0][0]), t)
            continue
        cleaned.append((s, t))
    out = []
    for s, t in cleaned:
        if not out or out[-1][1] != t:
            out.append((s, t))
    if out and out[0][0] > 0:
        out[0] = (0.0, out[0][1])
    return out


# -- captions -------------------------------------------------------------------------------

def caption_tracks(player_response):
    caps = (player_response.get("captions") or {}).get("playerCaptionsTracklistRenderer") or {}
    tracks = []
    for t in caps.get("captionTracks") or []:
        tracks.append({"lang": t.get("languageCode"), "name": _text(t.get("name")),
                       "auto": t.get("kind") == "asr", "url": t.get("baseUrl"),
                       "translatable": bool(t.get("isTranslatable"))})
    langs = [l.get("languageCode") for l in caps.get("translationLanguages") or []]
    return tracks, langs


def pick_captions(tracks, langs, wanted, include_auto=False):
    """[(lang, track, translate_to|None)] for the requested languages ('all' or 'en,ru')."""
    wanted = [w.strip() for w in wanted.split(",") if w.strip()] if wanted else ["en"]
    pool = [t for t in tracks if include_auto or not t["auto"]]
    chosen = []
    if "all" in wanted:
        return [(t["lang"] + ("-auto" if t["auto"] else ""), t, None) for t in pool]
    for w in wanted:
        exact = [t for t in pool if t["lang"] == w or t["lang"].split("-")[0] == w]
        manual = [t for t in exact if not t["auto"]]
        if manual or exact:
            chosen.append((w, (manual or exact)[0], None))
            continue
        # Not offered in this language: YouTube can machine-translate an existing track.
        source = next((t for t in tracks if t["translatable"] and not t["auto"]), None) or \
            next((t for t in tracks if t["translatable"]), None)
        if source and w in langs:
            chosen.append((w, source, w))
    return chosen


def fetch_caption(track, translate_to=None, user_agent=None):
    """WebVTT text of one track (optionally machine-translated)."""
    url = track["url"] + "&fmt=vtt" + (f"&tlang={translate_to}" if translate_to else "")
    status, body = net.request(url, headers={"User-Agent": user_agent or net.BROWSER_UA}, timeout=20)
    if status != 200 or not body.startswith(b"WEBVTT"):
        debug(f"caption {track['lang']}{'->' + translate_to if translate_to else ''}: HTTP {status}")
        return None
    return body.decode("utf-8", "replace")


_CUE_TIME = re.compile(r"(\d+:)?(\d{2}):(\d{2})\.(\d{3})")


def _secs(t):
    m = _CUE_TIME.match(t.strip())
    h = int((m.group(1) or "0:")[:-1])
    return h * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 1000


def vtt_cues(vtt):
    """[(start, end, text)] from WebVTT; YouTube's inline timing/colour tags removed and the
    rolling duplicates of auto-captions collapsed."""
    cues = []
    for block in re.split(r"\n\s*\n", vtt.replace("\r", "")):
        lines = block.strip().split("\n")
        idx = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if idx is None:
            continue
        a, b = lines[idx].split("-->")
        text = "\n".join(lines[idx + 1:])
        text = html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
        if not text:
            continue
        start, end = _secs(a), _secs(b.split()[0])
        if cues and cues[-1][2] == text:
            cues[-1] = (cues[-1][0], end, text)
            continue
        if cues and text.startswith(cues[-1][2] + "\n"):
            text = text[len(cues[-1][2]) + 1:]
        cues.append((start, end, text))
    return cues


def _ts(seconds, sep=","):
    ms = int(round(seconds * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d}{sep}{ms % 1000:03d}"


def to_srt(cues):
    return "".join(f"{i}\n{_ts(s)} --> {_ts(e)}\n{t}\n\n" for i, (s, e, t) in enumerate(cues, 1))


def to_vtt(cues):
    return "WEBVTT\n\n" + "".join(f"{_ts(s, '.')} --> {_ts(e, '.')}\n{t}\n\n" for s, e, t in cues)
