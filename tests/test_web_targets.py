"""The web page takes every input the command line takes: lists, direct files, HLS (offline)."""
import random
import time
import unittest
from unittest import mock

from fast_dlp import hls, net
from fast_dlp.utils import FastDlpError
from fast_dlp.web import server, targets


def direct_job(ident, spec, audio=False):
    return {"source": "direct", "site": "web", "id": ident, "title": f"File {ident}", "author": "Someone",
            "audio": audio, "meta": {}, "cover_url": None, "resolve": lambda: dict(spec), "url": None}


class OrderedTest(unittest.TestCase):
    def test_keeps_order_whatever_finishes_first(self):
        def fetch(i):
            time.sleep(random.random() / 200)
            return i
        self.assertEqual(list(targets._ordered(range(50), fetch, 6)), list(range(50)))


class ListTest(unittest.TestCase):
    def setUp(self):
        self.app = server.App()
        self.jobs = [{"source": "youtube", "video_id": "aaaaaaaaaaa", "playlist": "Mix",
                      "hint": {"title": "First", "author": "A", "duration": 61}},
                     {"source": "match", "site": "spotify", "playlist": "Mix", "url": None,
                      "track": {"title": "Second", "artists": ["B", "C"], "duration": 200, "cover": "http://x/c.jpg"}},
                     direct_job("3", {"kind": "file", "url": "http://x/f.mp4", "ext": "mp4"}, audio=True)]
        patcher = mock.patch.object(targets, "expand", return_value=("Mix", self.jobs))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_info_lists_every_kind(self):
        info = self.app.info("https://example.com/list")
        self.assertEqual(info["kind"], "list")
        self.assertEqual(info["count"], 3)
        self.assertFalse(info["search"])
        rows = info["entries"]
        self.assertEqual([r["kind"] for r in rows], ["youtube", "track", "direct"])
        self.assertEqual(rows[0]["title"], "First")
        self.assertEqual(rows[1]["author"], "B, C")
        self.assertEqual(rows[1]["site"], "Spotify")
        self.assertTrue(rows[1]["audio"])
        self.assertEqual(rows[0]["duration"], 61)

    def test_list_needs_an_entry(self):
        with self.assertRaises(FastDlpError):
            self.app.create({"url": "https://example.com/list"})
        with self.assertRaises(FastDlpError):
            self.app.create({"url": "https://example.com/list", "entry": "x"})

    def test_expanded_once(self):
        self.app.info("https://example.com/list")
        self.app.info("https://example.com/list")
        self.assertEqual(targets.expand.call_count, 1)


class DirectTest(unittest.TestCase):
    def setUp(self):
        self.app = server.App()

    def use(self, job):
        patcher = mock.patch.object(targets, "expand", return_value=(None, [job]))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_unranged_file_streams_as_is(self):
        body = [b"abc", b"defg"]
        self.use(direct_job("7", {"kind": "file", "url": "http://x/clip.webm", "ext": "webm", "size": 7, "ranges": False}))
        info = self.app.info("http://x/clip.webm")
        self.assertEqual((info["kind"], info["container"], info["size"]), ("file", "WEBM", 7))
        with mock.patch.object(targets, "_get_all", return_value=iter(body)):
            job = self.app.create({"url": "http://x/clip.webm", "mode": "original", "template": "{author} - {title}"})
            data = b"".join(job.output.chunks())
        self.assertEqual(data, b"abcdefg")
        self.assertEqual(job.size, 7)
        self.assertEqual(job.filename, "Someone - File 7.webm")
        self.assertEqual(job.output.mime, "video/webm")

    def test_ranged_file_is_planned_with_range_headers(self):
        self.use(direct_job("8", {"kind": "file", "url": "http://x/big.mp4", "ext": "mp4", "size": 10 << 20, "ranges": True}))
        job = self.app.create({"url": "http://x/big.mp4", "mode": "original"})
        (remote, start, end, patch, split), = job.output.plan
        self.assertEqual((start, end, split, remote.range_mode), (0, (10 << 20) - 1, True, "header"))
        self.assertEqual(job.size, 10 << 20)

    def test_mp3_needs_ffmpeg(self):
        self.use(direct_job("9", {"kind": "file", "url": "http://x/a.m4a", "ext": "m4a", "size": 5, "ranges": True}))
        with self.assertRaises(FastDlpError):
            self.app.create({"url": "http://x/a.m4a", "mode": "audio", "audio_format": "mp3"})

    def test_steam_workshop_is_cli_only(self):
        self.use(direct_job("10", {"kind": "steamcmd", "app": 1, "item": 2}))
        with self.assertRaises(FastDlpError):
            self.app.info("https://steamcommunity.com/sharedfiles/filedetails/?id=2")

    def test_hls_segments_in_order_with_init(self):
        playlist = ("#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MAP:URI=\"init.mp4\"\n"
                    + "".join(f"#EXTINF:2,\nseg{i}.m4s\n" for i in range(12)) + "#EXT-X-ENDLIST\n")
        segs = {f"http://x/seg{i}.m4s": b"\0\0\0\x10moof" + bytes([i]) * 7 for i in range(12)}

        def get(url, headers, binary=False):
            if url.endswith("init.mp4"):
                return b"\0\0\0\x08ftyp"
            return playlist

        def request(url, headers=None, timeout=None, **kw):
            time.sleep(random.random() / 500)
            return 200, segs[url]

        self.use(direct_job("11", {"kind": "hls", "url": "http://x/media.m3u8"}))
        with mock.patch.object(hls, "_get", side_effect=get), mock.patch.object(net, "request", side_effect=request):
            job = self.app.create({"url": "http://x/media.m3u8", "mode": "original"})
            data = b"".join(job.output.chunks())
        self.assertEqual(data, b"\0\0\0\x08ftyp" + b"".join(segs[f"http://x/seg{i}.m4s"] for i in range(12)))
        self.assertIsNone(job.size)
        self.assertTrue(job.filename.endswith(".mp4"))


class SearchFlagTest(unittest.TestCase):
    def test_plain_words_are_a_search(self):
        app = server.App()
        job = {"source": "youtube", "video_id": "bbbbbbbbbbb", "hint": {"title": "T"}, "site": "youtube"}
        with mock.patch.object(targets.search, "search_all", return_value=([job, dict(job)], "")):
            info = app.info("some artist - some title")
        self.assertTrue(info["search"])
        self.assertEqual(info["kind"], "list")


if __name__ == "__main__":
    unittest.main()
