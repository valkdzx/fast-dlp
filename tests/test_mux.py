import os
import struct
import tempfile
import unittest

from fast_dlp import mp3, mux
from tests import media

META = {"title": "Название", "artist": "Artist", "album": "Album", "album_artist": "AA",
        "date": "2025-10-17", "track": 3, "comment": "https://example.com",
        "cover": b"\xff\xd8\xff" + b"J" * 2000, "cover_mime": "image/jpeg"}


class WebmTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.v = os.path.join(self.dir, "v.webm")
        self.a = os.path.join(self.dir, "a.webm")
        self.out = os.path.join(self.dir, "out.webm")
        self.vp = media.webm(self.v, 1, "V_VP9", [(0, [b"v0", b"v1"]), (2000, [b"v2"]), (4000, [b"v3"])])
        self.ap = media.webm(self.a, 2, "A_OPUS", [(0, [b"a0"]), (1000, [b"a1"]), (3000, [b"a2"]), (5000, [b"a3"])])

    def test_merge_interleaves_and_renumbers(self):
        mux.merge(self.v, self.a, self.out)
        blocks, ids, _ = media.webm_blocks(self.out)
        self.assertEqual([p for t, p in blocks if t == 1], self.vp)
        self.assertEqual([p for t, p in blocks if t == 2], self.ap)
        self.assertEqual({t for t, _ in blocks}, {1, 2})
        # clusters interleaved by time: v@0, a@0, a@1000, v@2000, a@3000, v@4000, a@5000
        order = [t for t, _ in blocks]
        self.assertEqual(order, [1, 1, 2, 2, 1, 2, 1, 2])
        self.assertEqual(ids[:4], [mux._SEEKHEAD, mux._INFO, mux._TRACKS, mux._CUES])

    def test_merge_writes_cues_for_video_clusters(self):
        mux.merge(self.v, self.a, self.out)
        _, _, el = media.webm_blocks(self.out)
        cues = el[mux._CUES]
        self.assertEqual(cues.count(mux._id_bytes(0xBB) + b"\x01"), 3)   # one CuePoint per video cluster

    def test_tags_and_cover(self):
        mux.merge(self.v, self.a, self.out, META)
        _, ids, el = media.webm_blocks(self.out)
        self.assertIn(mux._TAGS, ids)
        self.assertIn(mux._ATTACHMENTS, ids)
        self.assertIn("Название".encode(), el[mux._TAGS])
        self.assertIn(META["cover"], el[mux._ATTACHMENTS])

    def test_tag_single_file_in_place(self):
        mux.tag(self.a, META)
        blocks, ids, el = media.webm_blocks(self.a)
        self.assertEqual([p for _, p in blocks], self.ap)
        self.assertEqual({t for t, _ in blocks}, {1})
        self.assertIn(b"ALBUM_ARTIST", el[mux._TAGS])
        self.assertFalse(os.path.exists(self.a + ".mux"))


class Mp4Test(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.v = os.path.join(self.dir, "v.mp4")
        self.a = os.path.join(self.dir, "a.m4a")
        self.out = os.path.join(self.dir, "out.mp4")
        # video timescale 90000, audio 44100: fragments at 0s, 5s, 10s / 0s, 4s, 8s, 12s
        self.vp = media.mp4(self.v, b"vide", 90000, [(0, b"V" * 10), (450000, b"W" * 10), (900000, b"X" * 10)])
        self.ap = media.mp4(self.a, b"soun", 44100, [(0, b"a" * 5), (176400, b"b" * 5), (352800, b"c" * 5),
                                                     (529200, b"d" * 5)])

    def _fragments(self, path):
        data, boxes = media.mp4_boxes(path)
        frags = []
        for i, (t, s, h, e) in enumerate(boxes):
            if t == b"moof":
                ts, th, te = mux._Mp4Source._find(data, [b"traf", b"tfhd"], s + h, e)
                ms, mh, me = mux._Mp4Source._find(data, [b"mfhd"], s + h, e)
                track = struct.unpack_from(">I", data, ts + th + 4)[0]
                seq = struct.unpack_from(">I", data, ms + mh + 4)[0]
                t2, s2, h2, e2 = boxes[i + 1]
                self.assertEqual(t2, b"mdat")
                frags.append((track, seq, data[s2 + h2:e2]))
        return data, boxes, frags

    def test_merge_interleaves_by_decode_time(self):
        mux.merge(self.v, self.a, self.out)
        data, boxes, frags = self._fragments(self.out)
        self.assertEqual([b[0] for b in boxes[:2]], [b"ftyp", b"moov"])
        self.assertEqual([f[2] for f in frags if f[0] == 1], self.vp)
        self.assertEqual([f[2] for f in frags if f[0] == 2], self.ap)
        self.assertEqual([f[1] for f in frags], list(range(1, 8)))          # mfhd renumbered
        # times: v0 a0 a4 v5 a8 v10 a12
        self.assertEqual([f[0] for f in frags], [1, 2, 2, 1, 2, 1, 2])

    def test_moov_has_both_tracks(self):
        mux.merge(self.v, self.a, self.out)
        data, boxes = media.mp4_boxes(self.out)
        t, s, h, e = boxes[1]
        traks = [x for x in mux._boxes(data, s + h, e) if x[0] == b"trak"]
        ids = []
        for _, ts, th, te in traks:
            ks, kh, ke = mux._Mp4Source._find(data, [b"tkhd"], ts + th, te)
            ids.append(struct.unpack_from(">I", data, ks + kh + 12)[0])
        self.assertEqual(ids, [1, 2])

    def test_ilst_tags_and_cover(self):
        mux.merge(self.v, self.a, self.out, META)
        data, boxes = media.mp4_boxes(self.out)
        t, s, h, e = boxes[1]
        us, uh, ue = mux._Mp4Source._find(data, [b"udta"], s + h, e)
        udta = data[us:ue]
        for atom in (b"\xa9nam", b"\xa9ART", b"\xa9alb", b"aART", b"\xa9day", b"trkn", b"covr"):
            self.assertIn(atom, udta)
        self.assertIn("Название".encode(), udta)
        self.assertIn(META["cover"], udta)

    def test_tag_single_m4a(self):
        mux.tag(self.a, META)
        _, _, frags = self._fragments(self.a)
        self.assertEqual([f[2] for f in frags], self.ap)


class Id3Test(unittest.TestCase):
    def test_header_and_frames(self):
        tag = mp3.id3v23(META)
        self.assertEqual(tag[:5], b"ID3\x03\x00")
        size = 0
        for b in tag[6:10]:
            self.assertLess(b, 0x80)              # syncsafe
            size = (size << 7) | b
        self.assertEqual(size, len(tag) - 10)
        frames, p = {}, 10
        while p < len(tag):
            fid = tag[p:p + 4].decode()
            n = struct.unpack(">I", tag[p + 4:p + 8])[0]
            frames[fid] = tag[p + 10:p + 10 + n]
            p += 10 + n
        self.assertEqual(p, len(tag))
        for fid in ("TIT2", "TPE1", "TALB", "TPE2", "TRCK", "TYER", "COMM", "APIC"):
            self.assertIn(fid, frames)
        self.assertEqual(frames["TIT2"][1:].decode("utf-16"), "Название")
        self.assertEqual(frames["TYER"][1:].decode("utf-16"), "2025")
        self.assertTrue(frames["APIC"].endswith(META["cover"]))

    def test_empty(self):
        self.assertTrue(mp3.id3v23({}).startswith(b"ID3"))   # still carries the encoder frame


if __name__ == "__main__":
    unittest.main()
