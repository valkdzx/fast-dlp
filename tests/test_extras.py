import os
import struct
import tempfile
import unittest

from fast_dlp import cli, hls, mux, playlist, postprocess, ytmeta
from tests import media
from tests.test_sources import _Server

VTT = """WEBVTT
Kind: captions
Language: en

00:00:01.000 --> 00:00:03.500 align:start position:0%
Hello<00:00:02.000><c> world</c>

00:00:03.500 --> 00:00:05.000
Hello world
second line

01:02:03.250 --> 01:02:04.000
&amp; done
"""


class CaptionTest(unittest.TestCase):
    def test_vtt_cues_and_srt(self):
        cues = ytmeta.vtt_cues(VTT)
        self.assertEqual(cues[0], (1.0, 3.5, "Hello world"))
        self.assertEqual(cues[1][2], "second line")                 # rolling auto-caption duplicate removed
        self.assertEqual(cues[2], (3723.25, 3724.0, "& done"))
        srt = ytmeta.to_srt(cues)
        self.assertIn("1\n00:00:01,000 --> 00:00:03,500\nHello world\n", srt)
        self.assertIn("01:02:03,250 --> 01:02:04,000", srt)
        self.assertTrue(ytmeta.to_vtt(cues).startswith("WEBVTT\n\n00:00:01.000 --> "))

    def test_pick_captions(self):
        tracks = [{"lang": "en", "auto": False, "translatable": True, "name": "English", "url": "u"},
                  {"lang": "en", "auto": True, "translatable": True, "name": "English (auto)", "url": "a"},
                  {"lang": "de-DE", "auto": False, "translatable": True, "name": "German", "url": "d"}]
        picked = ytmeta.pick_captions(tracks, ["ru", "fr"], "en,de,ru,xx")
        self.assertEqual([(l, t["url"], tr) for l, t, tr in picked],
                         [("en", "u", None), ("de", "d", None), ("ru", "u", "ru")])
        self.assertEqual(len(ytmeta.pick_captions(tracks, [], "all")), 2)                 # manual only
        self.assertEqual(len(ytmeta.pick_captions(tracks, [], "all", include_auto=True)), 3)


class ChapterTest(unittest.TestCase):
    def test_description_chapters(self):
        desc = "Intro text\n0:00 Start\n1:30 - Middle part\n(1:02:03) Late\nnot 9:99x"
        self.assertEqual(ytmeta.description_chapters(desc), [(0.0, "Start"), (90.0, "Middle part"), (3723.0, "Late")])
        self.assertEqual(ytmeta.description_chapters("1:00 only one"), [])
        self.assertEqual(ytmeta.description_chapters("0:30 a\n1:00 b"), [])                # must start at 0:00

    def test_sponsor_merge(self):
        base = [(0.0, "Intro"), (692.0, "Sponsor"), (812.0, "Camera")]
        merged = ytmeta.merge_sponsor_chapters(base, [(692.733, 812.002, "sponsor")], 1300)
        self.assertEqual(merged, [(0.0, "Intro"), (692.733, "[SponsorBlock] Sponsor"), (812.0, "Camera")])
        self.assertEqual(ytmeta.merge_sponsor_chapters([], [(100, 160, "sponsor")], 300),
                         [(0.0, "Video"), (100, "[SponsorBlock] Sponsor"), (160, "Video")])

    def test_parse_date(self):
        self.assertEqual(ytmeta.parse_date("Oct 24, 2009"), "20091024")
        self.assertEqual(ytmeta.parse_date("Premiered Mar 5, 2021"), "20210305")
        self.assertEqual(ytmeta.parse_date("Streamed live on Dec 31, 2025"), "20251231")
        self.assertIsNone(ytmeta.parse_date("3 hours ago"))


class MuxExtrasTest(unittest.TestCase):
    CH = [(0.0, "One"), (2.0, "Two")]
    SUBS = [("ru", "Русские", [(0.5, 1.5, "Привет"), (3.0, 4.0, "Пока")])]

    def test_matroska_chapters_and_subs(self):
        d = tempfile.mkdtemp()
        v, a, out = os.path.join(d, "v.webm"), os.path.join(d, "a.webm"), os.path.join(d, "o.mkv")
        media.webm(v, 1, "V_VP9", [(0, [b"v0"]), (2000, [b"v1"])])
        media.webm(a, 2, "A_OPUS", [(0, [b"a0"]), (1000, [b"a1"])])
        mux.merge(v, a, out, None, {"chapters": self.CH, "duration": 5.0, "subs": self.SUBS})
        blocks, ids, el = media.webm_blocks(out)
        self.assertIn(0x1043A770, ids)
        self.assertIn("Two".encode(), el[0x1043A770])
        tracks = el[mux._TRACKS]
        self.assertIn(b"S_TEXT/UTF8", tracks)                            # .mkv -> SRT text track
        self.assertIn(b"rus", tracks)
        with open(out, "rb") as fh:
            data = fh.read()
        self.assertIn("Привет".encode(), data)
        self.assertEqual([p for t, p in blocks if t in (1, 2)], [b"v0", b"a0", b"a1", b"v1"])

    def test_mp4_chpl_and_tx3g(self):
        d = tempfile.mkdtemp()
        v, a, out = os.path.join(d, "v.mp4"), os.path.join(d, "a.m4a"), os.path.join(d, "o.mp4")
        media.mp4(v, b"vide", 1000, [(0, b"V" * 4), (2000, b"W" * 4)])
        media.mp4(a, b"soun", 1000, [(0, b"a" * 4)])
        mux.merge(v, a, out, {"title": "t"}, {"chapters": self.CH, "subs": self.SUBS})
        data, boxes = media.mp4_boxes(out)
        moov = data[boxes[1][1]:boxes[1][3]]
        self.assertIn(b"chpl", moov)
        self.assertIn(b"tx3g", moov)
        self.assertIn(b"sbtl", moov)
        # chpl: 2 chapters, start times in 100 ns units
        i = moov.index(b"chpl") + 4 + 4 + 4
        self.assertEqual(moov[i], 2)
        self.assertEqual(struct.unpack(">Q", moov[i + 1:i + 9])[0], 0)
        # the subtitle fragment: an empty sample for the 0.5 s gap, then "Привет"
        self.assertIn(struct.pack(">H", len("Привет".encode())) + "Привет".encode(), data)
        moofs = [b for b in boxes if b[0] == b"moof"]
        self.assertEqual(len(moofs), 4)                                  # 2 video + 1 audio + 1 subtitle


class HlsAudioGroupTest(unittest.TestCase):
    def test_variant_with_separate_audio(self):
        server = _Server({
            "/m.m3u8": b"#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID=\"a\",NAME=\"x\",DEFAULT=YES,URI=\"aud.m3u8\"\n"
                       b"#EXT-X-STREAM-INF:BANDWIDTH=900,RESOLUTION=1280x720,AUDIO=\"a\"\nhi.m3u8\n"
                       b"#EXT-X-STREAM-INF:BANDWIDTH=300,RESOLUTION=640x360,AUDIO=\"a\"\nlo.m3u8\n",
        })
        try:
            got = hls.choose(server.base + "/m.m3u8", {})
            self.assertEqual([u.rsplit("/", 1)[-1] for u in got], ["hi.m3u8", "aud.m3u8"])
            got = hls.choose(server.base + "/m.m3u8", {}, max_height=400)
            self.assertEqual(got[0].rsplit("/", 1)[-1], "lo.m3u8")
            self.assertEqual([u.rsplit("/", 1)[-1] for u in hls.choose(server.base + "/m.m3u8", {}, audio_only=True)],
                             ["aud.m3u8"])
        finally:
            server.close()

    def test_packed_audio_detection(self):
        id3 = b"ID3\x04\x00\x00\x00\x00\x00\x05" + b"x" * 5
        self.assertEqual(hls._audio_kind(id3 + b"\xff\xf1\x50\x80"), "aac")
        self.assertEqual(hls._audio_kind(b"\xff\xfb\x90\x00"), "mp3")
        self.assertEqual(hls._strip_id3(id3 + b"DATA"), b"DATA")


class TemplateTest(unittest.TestCase):
    class V:
        id, title, author = "abc", "A/B: test", "Chan."
        raw = {"videoDetails": {"lengthSeconds": "3725", "viewCount": "1234567", "channelId": "UC1"}}

    def test_fields_and_specs(self):
        f = postprocess.fields(self.V, [{"itag": 1, "mimeType": 'video/mp4; codecs="avc1.64"', "height": 1080,
                                         "width": 1920, "fps": 60}], "mp4", {"position": 7}, "20240229")
        name = postprocess.render("{date} {channel} - {title} [{id}] {resolution} {view_count:,} "
                                  "{duration_string} #{playlist_index:03d} {nope}.{ext}", f)
        self.assertEqual(name, "2024-02-29 Chan. - A_B_ test [abc] 1920x1080 1,234,567 1-02-05 #007 NA.mp4")
        self.assertEqual(postprocess.template_fields("{a}/{b:03d}.{ext}"), {"a", "b", "ext"})


class ArchiveAndUrlsTest(unittest.TestCase):
    def test_archive(self):
        path = os.path.join(tempfile.mkdtemp(), "a.txt")
        arc = postprocess.Archive(path)
        job = {"source": "youtube", "video_id": "AbCdEfGh1_K"}
        self.assertFalse(arc.has(job))
        arc.add(job)
        arc.add(job)
        self.assertTrue(postprocess.Archive(path).has(job))
        with open(path) as fh:
            self.assertEqual(fh.read(), "youtube AbCdEfGh1_K\n")
        self.assertEqual(postprocess.Archive.key({"source": "spotify", "track": {"id": "x"}}), "spotify x")

    def test_channel_and_search_urls(self):
        for url in ("https://www.youtube.com/@BlenderOfficial", "https://www.youtube.com/@BlenderOfficial/videos",
                    "https://youtube.com/channel/UCSMOQeBJ2RAnuFungnQOxLg/shorts", "https://www.youtube.com/c/Name",
                    "https://www.youtube.com/user/name/streams"):
            self.assertTrue(playlist.is_channel(url), url)
        self.assertFalse(playlist.is_channel("https://www.youtube.com/watch?v=AbCdEfGh1_K"))
        self.assertTrue(playlist.is_search("ytsearch5:cats"))
        self.assertTrue(playlist.is_search("ytsearch:cats"))
        self.assertFalse(playlist.is_search("https://ytsearch.com"))

    def test_flags_parse(self):
        a = cli.build_parser().parse_args(["x", "--embed-subs", "--sub-langs", "ru,en", "--sponsorblock-mark", "all",
                                           "--live-from-start", "--live-duration", "60", "--proxy", "http://p:1"])
        self.assertTrue(a.embed_subs and a.live_from_start)
        self.assertEqual((a.sub_langs, a.live_duration, a.proxy), ("ru,en", 60, "http://p:1"))


if __name__ == "__main__":
    unittest.main()
