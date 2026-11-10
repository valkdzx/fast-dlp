"""Pinterest pins: the video (best MP4, else HLS) or the original image."""
import json
import re
import urllib.parse
import urllib.request

from .. import net
from ..utils import FastDlpError
from .common import direct, ext_of, get_json

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
_PIN = re.compile(r"pinterest\.[a-z.]+/pin/(?:[^/]*--)?(\d+)", re.I)
_SHORT = re.compile(r"^https?://pin\.it/", re.I)
_MP4_ORDER = ("V_EXP7", "V_1080P", "V_720P", "V_EXP6", "V_EXP5", "V_EXP4", "V_480P", "V_EXP3", "V_360P", "V_240P")


def match(url):
    return bool(_PIN.search(url) or _SHORT.match(url))


def _pin_id(url):
    if _SHORT.match(url):
        req = urllib.request.Request(url, headers={"User-Agent": UA}, method="HEAD")
        with net._opener.open(req, timeout=15) as resp:      # follows the redirect
            url = resp.geturl()
    m = _PIN.search(url)
    if not m:
        raise FastDlpError(f"Pinterest: no pin id in {url}")
    return m.group(1)


def _videos(data):
    lists = []
    vl = ((data.get("videos") or {}).get("video_list")) or {}
    if vl:
        lists.append(vl)
    for page in ((data.get("story_pin_data") or {}).get("pages") or []):
        for block in page.get("blocks") or []:
            v = (block.get("video") or {}).get("video_list")
            if v:
                lists.append(v)
    return lists


def _best(video_list):
    for key in _MP4_ORDER:
        v = video_list.get(key)
        if v and v.get("url", "").endswith(".mp4"):
            return "file", v["url"]
    for key, v in video_list.items():
        if v.get("url", "").endswith(".mp4"):
            return "file", v["url"]
    for key, v in video_list.items():
        if ".m3u8" in v.get("url", ""):
            return "hls", v["url"]
    return None, None


def expand(url, args):
    pin = _pin_id(url)
    payload = json.dumps({"options": {"id": pin, "field_set_key": "detailed"}})
    status, body = get_json("https://www.pinterest.com/resource/PinResource/get/",
                            {"User-Agent": UA, "X-Pinterest-PWS-Handler": "www/pin/[id].js"},
                            {"data": payload})
    if status != 200 or not body:
        raise FastDlpError(f"Pinterest: pin {pin} not found (HTTP {status})")
    data = body["resource_response"]["data"]
    title = (data.get("title") or data.get("grid_title") or data.get("description") or "").strip()[:100] or f"pin {pin}"
    author = ((data.get("pinner") or {}).get("full_name") or (data.get("pinner") or {}).get("username") or "")
    image = ((data.get("images") or {}).get("orig") or {}).get("url")
    meta = {"title": title, "artist": author, "comment": f"https://www.pinterest.com/pin/{pin}/"}
    jobs = []
    for i, vl in enumerate(_videos(data)):
        kind, link = _best(vl)
        if link:
            spec = {"kind": kind, "url": link, "headers": {"User-Agent": UA}, "ext": "mp4"}
            name = title if i == 0 else f"{title} ({i + 1})"
            jobs.append(direct("pinterest", f"{pin}" + (f"-{i + 1}" if i else ""), name, author,
                               lambda spec=spec: spec, meta=meta, cover_url=image))
    if not jobs and image:
        spec = {"kind": "file", "url": image, "headers": {"User-Agent": UA}, "ext": ext_of(image, "jpg")}
        jobs.append(direct("pinterest", pin, title, author, lambda: spec, meta=None))
    if not jobs:
        raise FastDlpError(f"Pinterest: pin {pin} has no video or image")
    return (title if len(jobs) > 1 else None), jobs
