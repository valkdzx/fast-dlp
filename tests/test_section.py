import os
import struct
import tempfile
import unittest

from fast_dlp import mux, section
from fast_dlp.utils import FastDlpError
from tests import media


class TimeTest(unittest.TestCase):
    def test_parse_time(self):
        for text, secs in (("220", 220), ("220s", 220), ("3:40", 220), ("1:02:03", 3723), ("1h2m3s", 3723),
                           ("3m40s", 220), ("1.5", 1.5)):
            self.assertEqual(section.parse_time(text), secs, text)
        with self.assertRaises(FastDlpError):
            section.parse_time("soon")

    def test_url_time_and_spec(self):
        self.assertEqual(section.url_time("https://www.youtube.com/watch?v=x&t=220s"), 220)
        self.assertEqual(section.url_time("https://youtu.be/x?t=1m5s"), 65)
        self.assertIsNone(section.url_time("https://youtu.be/x"))
        self.assertEqual(section.parse_section("3:40-5:10"), (220, 310))
        self.assertEqual(section.parse_section("3:40-"), (220, None))
        self.assertEqual(section.parse_section("-1:00"), (0.0, 60))
        self.assertEqual(section.parse_section("t", "https://youtu.be/x?t=90"), (90, None))
        with self.assertRaises(FastDlpError):
            section.parse_section("5:00-1:00")
        self.assertEqual(section.label(220, 310), "3m40s-5m10s")
        self.assertEqual(section.label(3723, None), "1h02m03s-end")


def sidx(refs, timescale=1000, first_offset=0):
    body = struct.pack(">IIIII", 0, 1, timescale, 0, first_offset) + struct.pack(">HH", 0, len(refs))
    for size, dur in refs:
        body += struct.pack(">III", size, dur, 0x90000000)
    return struct.pack(">I4s", 8 + len(body), b"sidx") + body


class IndexTest(unittest.TestCase):
    def test_mp4_index_and_cover(self):
        idx = sidx([(100, 5000), (200, 5000), (300, 5000), (400, 5000)])
        frags = section.mp4_index(idx, 1000)
        start = 1000 + len(idx)
        self.assertEqual(frags[0], (0.0, 5.0, start, start + 99))
        self.assertEqual(frags[3], (15.0, 20.0, start + 600, start + 999))
        self.assertEqual(section.cover(frags, 6, 12), (5.0, start + 100, start + 599))   # fragments 2-3
        self.assertEqual(section.cover(frags, 16, None), (15.0, start + 600, start + 999))
        with self.assertRaises(FastDlpError):
            section.cover(frags, 25, 30)

    def test_webm_index(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "a.webm")
        media.webm(p, 2, "A_OPUS", [(0, [b"a"]), (5000, [b"b"]), (10000, [b"c"])])
        with open(p, "rb") as fh:
            data = fh.read()
        _, ids, el = media.webm_blocks(p)
        # build Cues from the real cluster positions
        eid, n1 = mux._read_id(data, 0)
        hs, n2 = mux._read_size(data, n1)
        seg = n1 + n2 + hs
        eid, m1 = mux._read_id(data, seg)
        _, m2 = mux._read_size(data, seg + m1)
        data_start = seg + m1 + m2
        positions = [s - data_start for cid, s, bs, be in mux._children(data, data_start, len(data))
                     if cid == mux._CLUSTER]
        cues = mux._el(mux._CUES, b"".join(
            mux._el(0xBB, mux._uint(0xB3, t) + mux._el(0xB7, mux._uint(0xF7, 1, 1) + mux._uint(0xF1, pos)))
            for t, pos in zip((0, 5000, 10000), positions)))
        frags = section.webm_index(data[:data_start + positions[0]], cues, len(data))
        self.assertEqual([round(f[0]) for f in frags], [0, 5, 10])
        self.assertEqual(frags[1][2], data_start + positions[1])
        self.assertEqual(frags[-1][3], len(data) - 1)


class RebaseTest(unittest.TestCase):
    def test_mp4_rebase_and_duration(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "a.m4a")
        media.mp4(p, b"soun", 1000, [(60000, b"a" * 4), (65000, b"b" * 4)])
        mux.tag(p, None, {"rebase": 60.0, "section_duration": 10.0})
        with open(p, "rb") as fh:
            data = fh.read()
        boxes = list(mux._boxes(data, 0, len(data)))
        moofs = [b for b in boxes if b[0] == b"moof"]
        times = []
        for t, s, h, e in moofs:
            ds, dh, de = mux._Mp4Source._find(data, [b"traf", b"tfdt"], s + h, e)
            times.append(struct.unpack_from(">Q", data, ds + dh + 4)[0])
        self.assertEqual(times, [0, 5000])
        moov = data[boxes[1][1]:boxes[1][3]]
        i = moov.index(b"mvhd") + 4 + 4 + 8
        self.assertEqual(struct.unpack(">II", moov[i:i + 8]), (1000, 10000))

    def test_webm_rebase_keeps_sizes(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "a.webm")
        media.webm(p, 2, "A_OPUS", [(60000, [b"a"]), (65000, [b"b"])])
        mux.tag(p, None, {"rebase": 60.0, "section_duration": 10.0})
        blocks, ids, el = media.webm_blocks(p)
        with open(p, "rb") as fh:
            data = fh.read()
        stamps = []
        eid, n1 = mux._read_id(data, 0)
        hs, n2 = mux._read_size(data, n1)
        seg = n1 + n2 + hs
        eid, m1 = mux._read_id(data, seg)
        _, m2 = mux._read_size(data, seg + m1)
        for cid, s, bs, be in mux._children(data, seg + m1 + m2, len(data)):
            if cid == mux._CLUSTER:
                for c2, s2, bs2, be2 in mux._children(data, bs, be):
                    if c2 == mux._TIMESTAMP:
                        stamps.append(mux._uint_value(data, bs2, be2))
        self.assertEqual(stamps, [0, 5000])
        self.assertEqual([p for _, p in blocks], [b"a", b"b"])


if __name__ == "__main__":
    unittest.main()
