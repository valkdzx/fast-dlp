"""The web page's CLI features: -f by hand, chapters/SponsorBlock, side files, -g, live (offline)."""
import io
import json
import random
import threading
import time
import types
import unittest
from unittest import mock

from fast_dlp import hls, net
from fast_dlp.utils import FastDlpError
from fast_dlp.web import extras, server, targets, texts


def video(**raw):
    details = {"videoId": "aaaaaaaaaaa", "title": "Clip", "author": "Someone", "lengthSeconds": "120",
               "shortDescription": "0:00 Start\n1:00 Middle\nsome text"}
    details.update(raw.pop("details", {}))
    return types.SimpleNamespace(id="aaaaaaaaaaa", title="Clip", author="Someone", duration=120, client="VISIONOS",
                                 formats=raw.pop("formats", []), raw={"videoDetails": details, **raw})


class OptionsTest(unittest.TestCase):
    def setUp(self):
        self.app = server.App()

    def test_format_spec_checked(self):
        self.assertEqual(self.app.options({"format": "401+140-drc"})["format"], "401+140-drc")
        for bad in ("401;rm", "a" * 40, "1+2+3", "../x"):
            with self.assertRaises(texts.WebError, msg=bad):
                self.app.options({"format": bad})

    def test_sponsor_and_live(self):
        o = self.app.options({"sponsor": "intro,nonsense,outro", "live_duration": 10 ** 9, "chapters": 1})
        self.assertEqual(o["sponsor"], ["intro", "outro"])
        self.assertEqual(o["live_duration"], server.LIVE_MAX)
        self.assertTrue(o["chapters"])
        self.assertEqual(extras.sponsor_list("all"), list(extras.SPONSOR))
        self.assertEqual(extras.sponsor_list(""), [])


class SideFilesTest(unittest.TestCase):
    def test_description_and_chapters(self):
        v = video()
        data, name, ctype = extras.youtube_file(v, "description", {})
        self.assertTrue(data.startswith(b"0:00 Start"))
        self.assertEqual(name, "Clip.description.txt")
        with mock.patch.object(extras, "chapters", return_value=[(0.0, "Start"), (60.0, "Middle"), (3725.0, "Late")]):
            data, name, _ = extras.youtube_file(v, "chapters", {})
        self.assertEqual(data.decode(), "0:00 Start\n1:00 Middle\n1:02:05 Late\n")
        self.assertEqual(name, "Clip.chapters.txt")

    def test_nothing_there(self):
        with mock.patch.object(extras, "chapters", return_value=[]):
            with self.assertRaises(texts.WebError):
                extras.youtube_file(video(), "chapters", {})
        with self.assertRaises(FastDlpError):
            extras.check_kind("passwords")

    def test_subtitles_srt_and_translation(self):
        caps = {"playerCaptionsTracklistRenderer": {
            "captionTracks": [{"languageCode": "en", "name": {"simpleText": "English"}, "baseUrl": "https://x/tt",
                               "isTranslatable": True}],
            "translationLanguages": [{"languageCode": "ru"}, {"languageCode": "de"}]}}
        v = video(captions=caps)
        self.assertEqual(extras.captions(v), ([{"lang": "en", "name": "English", "auto": False}], ["ru", "de"]))
        vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:02.500\nHello\n"
        with mock.patch.object(extras.ytmeta, "fetch_caption", return_value=vtt) as fetch:
            data, name, ctype = extras.youtube_file(v, "subtitles", {"lang": "en", "tlang": "ru"})
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args[0][1], "ru")
        self.assertIn(b"00:00:01,000 --> 00:00:02,500", data)
        self.assertEqual(name, "Clip.ru.srt")
        self.assertTrue(ctype.startswith("application/x-subrip"))

    def test_direct_job_cover_and_info(self):
        job = {"site": "soundcloud", "id": "9", "title": "Song", "author": "Band", "audio": True,
               "cover_url": "https://x/c.jpg", "meta": {"album": "LP", "cover": b"bytes not json"}}
        with mock.patch.object(extras.net, "request", return_value=(200, b"\xff\xd8\xff" + b"0" * 100)):
            data, name, ctype = extras.direct_file(job, "thumbnail")
        self.assertEqual((name, ctype), ("Song.jpg", "image/jpeg"))
        data, name, _ = extras.direct_file(job, "info")
        info = json.loads(data)
        self.assertEqual((info["site"], info["meta"]), ("soundcloud", {"album": "LP"}))


class UrlsTest(unittest.TestCase):
    def test_direct_job(self):
        app = server.App()
        job = {"source": "direct", "site": "web", "id": "1", "title": "t", "author": "", "audio": False, "meta": {},
               "cover_url": None, "url": None, "resolve": lambda: {"kind": "hls", "url": "https://x/m.m3u8"}}
        with mock.patch.object(targets, "expand", return_value=(None, [job])):
            self.assertEqual(app.urls({"url": "https://x/m.m3u8"}), [{"url": "https://x/m.m3u8", "label": "HLS"}])


class LiveTest(unittest.TestCase):
    def test_segments_in_order_until_the_end_or_stop(self):
        seg = lambda i: {"uri": f"https://x/s{i}.ts", "seq": i, "duration": 0.01, "key": None}   # noqa: E731
        rounds = [[seg(i) for i in range(5)], [seg(i) for i in range(3, 8)]]
        texts_ = ["first", "second"]
        state = {"n": 0}

        def get(url, headers, binary=False):
            n = min(state["n"], 1)
            state["n"] += 1
            return texts_[n] + ("\n#EXT-X-ENDLIST" if n == 1 else "")

        def parse(text, base):
            return "media", rounds[0 if text.startswith("first") else 1], None

        def request(url, headers=None, timeout=None, **kw):
            time.sleep(random.random() / 500)
            return 200, b"\x47" + url[-5:].encode()

        with mock.patch.object(hls, "_get", side_effect=get), mock.patch.object(hls, "parse", side_effect=parse), \
                mock.patch.object(net, "request", side_effect=request):
            ext, chunks = targets._live_media("https://x/media.m3u8", {}, None, False, lambda: False, False, 4)
            data = b"".join(chunks())
        self.assertEqual(ext, "ts")
        # live edge: the last 3 of the first round, then what came after, each once, in order
        self.assertEqual(data, b"".join(b"\x47" + f"s{i}.ts"[-5:].encode() for i in range(2, 8)))

        stop = threading.Event()
        stop.set()
        state["n"] = 0
        with mock.patch.object(hls, "_get", side_effect=get), mock.patch.object(hls, "parse", side_effect=parse), \
                mock.patch.object(net, "request", side_effect=request):
            _, chunks = targets._live_media("https://x/media.m3u8", {}, None, True, stop.is_set, False, 4)
            got = list(chunks())
        self.assertEqual(len(got), 2)               # the first segment, then stop wins


class StopRouteTest(unittest.TestCase):
    def test_stop_sets_the_event(self):
        app = server.App()
        job = server.Job("tok", types.SimpleNamespace(id="x", title="t"), {}, types.SimpleNamespace(size=None), [], "f.ts")
        app.jobs["tok"] = job
        handler = server.Handler.__new__(server.Handler)
        handler.app, handler.path, handler.headers = app, "/api/jobs/tok/stop", {}
        sent = []
        handler._send = lambda status, body, *a, **k: sent.append((status, body))
        handler.do_POST()
        self.assertTrue(job.stop.is_set())
        self.assertEqual(sent[0][0], 200)
        self.assertTrue(sent[0][1]["stopping"])


if __name__ == "__main__":
    unittest.main()
