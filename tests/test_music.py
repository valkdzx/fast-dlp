import argparse
import os
import struct
import tempfile
import unittest
from unittest import mock

from fast_dlp import cli, lyrics, mp3, mux, search, sync
from tests import media

LRC = "[00:16.48] Первая строка\n[00:18.20] Вторая строка, проверка UTF-16"


class TagTest(unittest.TestCase):
    def test_replaygain_value(self):
        self.assertEqual(lyrics.replaygain(-7.84), "-10.16 dB")
        self.assertEqual(lyrics.replaygain(-20), "+2.00 dB")

    def test_id3_uslt_and_txxx(self):
        tag = mp3.id3v23({"title": "t", "lyrics": LRC, "replaygain": "-10.16 dB"})
        frames, p = {}, 10
        while p < len(tag):
            fid = tag[p:p + 4].decode()
            n = struct.unpack(">I", tag[p + 4:p + 8])[0]
            frames[fid] = tag[p + 10:p + 10 + n]
            p += 10 + n
        self.assertIn("проверка".encode("utf-16-le"), frames["USLT"])
        self.assertIn("REPLAYGAIN_TRACK_GAIN".encode("utf-16-le"), frames["TXXX"])
        self.assertTrue(frames["TXXX"].endswith("-10.16 dB".encode("utf-16-le")))

    def test_mp4_lyr_and_freeform(self):
        p = os.path.join(tempfile.mkdtemp(), "a.m4a")
        media.mp4(p, b"soun", 1000, [(0, b"a" * 4)])
        mux.tag(p, {"lyrics": LRC, "replaygain": "-10.16 dB"})
        with open(p, "rb") as fh:
            data = fh.read()
        self.assertIn(b"\xa9lyr", data)
        self.assertIn("проверка".encode(), data)
        self.assertIn(b"----", data)
        self.assertIn(b"REPLAYGAIN_TRACK_GAIN", data)

    def test_matroska_lyrics_tag(self):
        p = os.path.join(tempfile.mkdtemp(), "a.webm")
        media.webm(p, 2, "A_OPUS", [(0, [b"a"])])
        mux.tag(p, {"lyrics": LRC, "replaygain": "-7.23 dB"})
        _, ids, el = media.webm_blocks(p)
        self.assertIn(b"LYRICS", el[mux._TAGS])
        self.assertIn(b"REPLAYGAIN_TRACK_GAIN", el[mux._TAGS])


class LyricsLookupTest(unittest.TestCase):
    def test_exact_then_search(self):
        calls = []

        def fake_get(path, params):
            calls.append(path)
            if path == "get":
                return None
            return [{"duration": 300, "syncedLyrics": "[00:01.00] wrong"},
                    {"duration": 121, "syncedLyrics": None, "plainLyrics": "plain one"},
                    {"duration": 122, "syncedLyrics": "[00:01.00] right", "plainLyrics": "x"}]

        with mock.patch.object(lyrics, "_get", fake_get):
            synced, plain = lyrics.fetch("Song (Official Video)", "Artist, Other", None, 122)
        self.assertEqual(synced, "[00:01.00] right")       # close in length, synced preferred
        self.assertEqual(calls, ["get", "search"])


class SearchDetectTest(unittest.TestCase):
    def test_looks_like_target(self):
        for t in ("https://youtu.be/x", "AbCdEfGh1_K", "ytsearch5:x", "spotify:track:abc", "music.yandex.ru/album/1",
                  "www.youtube.com/watch?v=x", "aqz-KE-bpKQ"):
            self.assertTrue(search.looks_like_target(t), t)
        for t in ("исполнитель", "-", "название", "artist", "some song name"):
            self.assertFalse(search.looks_like_target(t), t)

    def test_search_all_survives_a_dead_source(self):
        def boom(q, n):
            raise IOError("down")

        fake = {"ytmusic": lambda q, n: [{"source": "youtube", "video_id": "a"}], "spotify": boom,
                "yandex": lambda q, n: [], "youtube": lambda q, n: [{"source": "youtube", "video_id": "b"}],
                "soundcloud": boom}
        with mock.patch.dict(search._SEARCHERS, fake):
            jobs, counts = search.search_all("q")
        self.assertEqual([j["video_id"] for j in jobs], ["a", "b"])
        self.assertEqual([j["found_on"] for j in jobs], ["ytmusic", "youtube"])
        self.assertEqual(counts, "YT Music 1, YouTube 1")


class SyncTest(unittest.TestCase):
    def test_add_keep_remove_and_m3u(self):
        folder = tempfile.mkdtemp()
        args = cli.build_parser().parse_args(["x", "-P", folder])
        jobs = [{"source": "youtube", "video_id": v, "playlist": "PL", "hint": {"title": v, "duration": 60}}
                for v in ("aaaaaaaaaaa", "bbbbbbbbbbb")]

        def fake_run(js, job_args):
            for j in js:
                path = os.path.join(job_args.paths, f"{j['video_id']}.webm")
                with open(path, "w") as fh:
                    fh.write("x")
                job_args.on_file(j, path)

        label = lambda j: j["hint"]["title"]
        dur = lambda j: j["hint"]["duration"]
        sync.run(jobs, args, fake_run, label, dur, music=False)
        target = os.path.join(folder, "PL")
        self.assertEqual(sorted(f for f in os.listdir(target) if f.endswith(".webm")),
                         ["aaaaaaaaaaa.webm", "bbbbbbbbbbb.webm"])
        with open(os.path.join(target, "mine.txt"), "w") as fh:
            fh.write("user file")
        sync.run(jobs[1:], args, fake_run, label, dur, music=False)          # "a" left the playlist
        names = os.listdir(target)
        self.assertNotIn("aaaaaaaaaaa.webm", names)
        self.assertIn("mine.txt", names)                                   # never touches other files
        with open(os.path.join(target, "PL.m3u8"), encoding="utf-8") as fh:
            m3u = fh.read()
        self.assertEqual(m3u, "#EXTM3U\n#PLAYLIST:PL\n#EXTINF:60,bbbbbbbbbbb\nbbbbbbbbbbb.webm\n")


if __name__ == "__main__":
    unittest.main()
