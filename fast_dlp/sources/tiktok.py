"""TikTok videos and photo posts (no watermark).

The video page carries everything in its __UNIVERSAL_DATA_FOR_REHYDRATION__ JSON: every
bitrate variant (h264 / h265) and, for photo posts, each image and the sound. TikTok's
CDN answers 403 unless the request carries the cookies that page set (tt_chain_token...)
and a tiktok.com Referer, so they travel with every download.
"""
import http.cookiejar
import json
import re
import urllib.request

from .. import net
from ..utils import FastDlpError, debug
from .common import direct, ext_of

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
_POST = re.compile(r"tiktok\.com/(?:@[\w.-]+/(?:video|photo)/|v/|embed(?:/v2)?/)(\d+)", re.I)
_SHORT = re.compile(r"^https?://(?:vm|vt)\.tiktok\.com/\w+|^https?://(?:www\.)?tiktok\.com/t/\w+", re.I)
_DATA = re.compile(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', re.S)


def match(url):
    return bool(_POST.search(url) or _SHORT.match(url))


def _session():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar),
                                         urllib.request.HTTPSHandler(context=net.SSL_CONTEXT),
                                         *net.proxy_handlers())
    return jar, opener


def _page(url):
    """(itemStruct, download headers) for a post URL; short links are followed."""
    jar, opener = _session()
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    with opener.open(req, timeout=20) as resp:
        final = resp.geturl()
        html = resp.read().decode("utf-8", "replace")
    m = _DATA.search(html)
    if not m:
        raise FastDlpError("TikTok sent no video data (a captcha or a region block); try again later")
    scope = json.loads(m.group(1)).get("__DEFAULT_SCOPE__", {})
    detail = scope.get("webapp.video-detail") or {}
    if detail.get("statusCode") not in (0, None):
        raise FastDlpError(f"TikTok: post unavailable ({detail.get('statusMsg') or detail.get('statusCode')}) "
                           "- private, removed or region-locked")
    item = (detail.get("itemInfo") or {}).get("itemStruct")
    if not item:
        raise FastDlpError(f"TikTok: no post found at {final}")
    cookie = "; ".join(f"{c.name}={c.value}" for c in jar)
    headers = {"User-Agent": UA, "Referer": "https://www.tiktok.com/", "Cookie": cookie}
    return item, headers, final


def _best_video(video, prefer_hevc=False):
    variants = []
    for b in video.get("bitrateInfo") or []:
        addr = b.get("PlayAddr") or {}
        urls = addr.get("UrlList") or []
        if urls:
            hevc = "265" in (b.get("CodecType") or "")
            variants.append((hevc == prefer_hevc, addr.get("Height") or 0, b.get("Bitrate") or 0,
                             urls[0], int(addr.get("DataSize") or 0)))
    if variants:
        _, height, rate, url, size = max(variants)
        debug(f"TikTok variant: {height}p {rate // 1000} kbps")
        return url, size
    url = video.get("playAddr") or video.get("downloadAddr")
    if not url:
        raise FastDlpError("TikTok: this post has no playable video")
    return url, 0


def expand(url, args):
    item, headers, final = _page(url)
    post_id = item.get("id")
    author = (item.get("author") or {}).get("uniqueId") or ""
    desc = (item.get("desc") or "").strip()
    title = re.sub(r"\s+", " ", desc)[:80].strip() or f"tiktok {post_id}"
    link = f"https://www.tiktok.com/@{author}/video/{post_id}"
    meta = {"title": desc or title, "artist": f"@{author}" if author else None, "comment": link}
    music = item.get("music") or {}
    cover = (item.get("video") or {}).get("cover") or music.get("coverLarge")

    def refreshed(pick):
        """A resolver that re-reads the page: TikTok's signed links and cookies expire."""
        def resolve():
            fresh, hdrs, _ = _page(link)
            return pick(fresh, hdrs)
        return resolve

    def sound(fresh, hdrs):
        play = (fresh.get("music") or {}).get("playUrl")
        if not play:
            raise FastDlpError("TikTok: this post's sound cannot be downloaded")
        return {"kind": "file", "url": play, "headers": hdrs, "ext": "mp3"}

    images = (item.get("imagePost") or {}).get("images") or []
    if images:
        jobs = []
        for i, img in enumerate(images, 1):
            def pick(fresh, hdrs, i=i):
                imgs = (fresh.get("imagePost") or {}).get("images") or []
                u = ((imgs[i - 1].get("imageURL") or {}).get("urlList") or [None])[0]
                if not u:
                    raise FastDlpError(f"TikTok: image {i} has no link")
                return {"kind": "file", "url": u, "headers": hdrs, "ext": "jpg" if ".jpeg" in u or ".jpg" in u else ext_of(u, "jpg")}
            jobs.append(direct("tiktok", f"{post_id}-{i}", f"{title} ({i})", f"@{author}", refreshed(pick)))
        if music.get("playUrl"):
            jobs.append(direct("tiktok", f"{post_id}-sound", f"{music.get('title') or title}",
                               music.get("authorName") or f"@{author}", refreshed(sound), audio=True,
                               meta={"title": music.get("title"), "artist": music.get("authorName"), "comment": link},
                               cover_url=music.get("coverLarge")))
        return f"{title} [{post_id}]", jobs

    if args.audio_only or args.audio_format == "mp3":
        return None, [direct("tiktok", post_id, music.get("title") or title,
                             music.get("authorName") or f"@{author}", refreshed(sound), audio=True,
                             meta={"title": music.get("title") or title,
                                   "artist": music.get("authorName") or author, "comment": link},
                             cover_url=music.get("coverLarge") or cover)]

    prefer_hevc = (args.codec or "") in ("hevc", "h265")

    def video(fresh, hdrs):
        u, size = _best_video(fresh.get("video") or {}, prefer_hevc)
        return {"kind": "file", "url": u, "headers": hdrs, "ext": "mp4", "size": size or None}

    job = direct("tiktok", post_id, f"{title} [{post_id}]", f"@{author}", refreshed(video), meta=meta,
                 cover_url=cover, url=link)
    # The first resolve may use what was just read instead of fetching the page again.
    first = {"done": False}
    later = job["resolve"]

    def resolve_once():
        if not first["done"]:
            first["done"] = True
            return video(item, headers)
        return later()

    job["resolve"] = resolve_once
    return None, [job]
