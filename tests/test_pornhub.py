"""Pornhub source, offline: a synthetic watch page and get_media answer."""
import argparse
import json
import unittest
from unittest import mock

from fast_dlp.sources import find, pornhub
from fast_dlp.utils import FastDlpError

KEY = "abc123def"
FLASH = {
    "video_title": "Sample &amp; title", "video_duration": "125", "image_url": "https://img.example/x.jpg",
    "mediaDefinitions": [
        {"format": "hls", "quality": "1080", "videoUrl": "https://cdn.example/1080/master.m3u8"},
        {"format": "hls", "quality": "720", "videoUrl": "https://cdn.example/720/master.m3u8"},
        {"format": "mp4", "quality": [], "videoUrl": "https://www.pornhub.com/video/get_media?s=x"},
    ],
}
MEDIA = [{"height": 240, "videoUrl": "https://cdn.example/240.mp4"},
         {"height": 720, "videoUrl": "https://cdn.example/720.mp4"},
         {"height": 1080, "videoUrl": "https://cdn.example/1080.mp4"}]


def page(flash=FLASH):
    return ('<html><script type="application/ld+json">{"author" : "Uploader"}</script>'
            f"<script>var flashvars_123 = {json.dumps(flash)};\n</script></html>").encode()


def fake_get(pages):
    calls = []

    def get(self, url, referer=None):
        calls.append((url, referer))
        for part, body in pages.items():
            if part in url:
                return (200, url, body) if isinstance(body, bytes) else body
        raise AssertionError(url)
    return get, calls


def args(**kw):
    a = argparse.Namespace(max_height=None, audio_only=False)
    a.__dict__.update(kw)
    return a


class PornhubTest(unittest.TestCase):
    def test_match(self):
        for u in (f"https://www.pornhub.com/view_video.php?viewkey={KEY}", f"https://rt.pornhub.com/view_video.php?x=1&viewkey={KEY}",
                  f"pornhub.com/embed/{KEY}", f"https://pornhub.org/view_video.php?viewkey={KEY}"):
            self.assertTrue(pornhub.match(u), u)
            self.assertIs(find(u), pornhub)
        self.assertFalse(pornhub.match(f"https://example.com/view_video.php?viewkey={KEY}"))

    def test_best_mp4_under_limit_with_session(self):
        get, calls = fake_get({"view_video.php": page(), "get_media": json.dumps(MEDIA).encode()})
        with mock.patch.object(pornhub._Session, "get", get):
            title, jobs = pornhub.expand(f"https://www.pornhub.com/view_video.php?viewkey={KEY}", args(max_height=720))
            job = jobs[0]
            self.assertEqual((job["title"], job["author"], job["duration"]), ("Sample & title", "Uploader", 125))
            spec = job["resolve"]()
        self.assertEqual((spec["kind"], spec["url"]), ("file", "https://cdn.example/720.mp4"))
        self.assertIn("view_video.php", spec["headers"]["Referer"])
        self.assertEqual(calls[1][1], f"https://www.pornhub.com/view_video.php?viewkey={KEY}")  # get_media with Referer
        self.assertEqual(len(calls), 2)                      # the page is read once

    def test_hls_when_no_mp4(self):
        get, _ = fake_get({"view_video.php": page(), "get_media": b"[]"})
        with mock.patch.object(pornhub._Session, "get", get):
            _, jobs = pornhub.expand(f"https://www.pornhub.com/view_video.php?viewkey={KEY}", args())
            spec = jobs[0]["resolve"]()
        self.assertEqual((spec["kind"], spec["url"]), ("hls", "https://cdn.example/1080/master.m3u8"))

    def test_refresh_reloads_page(self):
        get, calls = fake_get({"view_video.php": page(), "get_media": json.dumps(MEDIA).encode()})
        with mock.patch.object(pornhub._Session, "get", get):
            _, jobs = pornhub.expand(f"https://www.pornhub.com/view_video.php?viewkey={KEY}", args())
            jobs[0]["resolve"]()
            jobs[0]["resolve"]()                              # an expired link: fresh page + get_media
        self.assertEqual([u for u, _ in calls].count(f"https://www.pornhub.com/view_video.php?viewkey={KEY}"), 2)

    def test_unavailable_and_age_verification(self):
        cases = {
            "unavailable": page(dict(FLASH, video_unavailable=True, video_unavailable_country=True)),
            "age verification": b"<html>Please verify your age to continue</html>",
            "not found": (404, "u", b""),
        }
        for expected, body in cases.items():
            get, _ = fake_get({"view_video.php": body})
            with mock.patch.object(pornhub._Session, "get", get):
                with self.assertRaises(FastDlpError) as ctx:
                    pornhub.expand(f"https://www.pornhub.com/view_video.php?viewkey={KEY}", args())
            self.assertIn(expected, str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
