"""Streaming output: planned from the formats' indexes, it must equal what the file muxer
writes from the same streams, byte for byte."""
import os
import struct
import tempfile
import unittest
from unittest import mock

from fast_dlp import mux, stream
from tests import media


class FakeRemote:
    """What stream.Remote holds, built from a local file instead of googlevideo."""

    def __init__(self, path, kind, itag):
        with open(path, "rb") as fh:
            self.data = fh.read()
        self.kind, self.itag, self.stream = kind, itag, None
        self.size = len(self.data)
        if kind == "mp4":
            src = mux._Mp4Source(path)
            boxes = list(mux._boxes(self.data, 0, len(self.data)))
            first_moof = next(s for t, s, h, e in boxes if t == b"moof")
            self.init = self.data[:first_moof]
            self.fragments = []
            for (moof, mdat_off, mdat_len, dt) in src.fragments:
                start = mdat_off - len(moof)
                t0 = dt / src.timescale
                self.fragments.append((t0, t0 + 1, start, mdat_off + mdat_len - 1))
            src.close()
        else:
            src = mux._WebmSource(path)
            self.init = self.data[:src.clusters[0][1]]
            scale = src.info_fields()[0]
            self.fragments = [(ts * scale / 1e9, ts * scale / 1e9 + 1, off, off + ln - 1)
                              for ts, off, ln in src.clusters]
            src.close()


def local_get(self, remote, start, end):
    return bytearray(remote.data[start:end + 1])


def produce(out):
    with mock.patch.object(stream._Fetcher, "_get", local_get):
        return b"".join(bytes(b) for b in out.chunks())


class StreamMp4Test(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.v, self.a, self.ref = (os.path.join(d, n) for n in ("v.mp4", "a.m4a", "ref.mp4"))
        media.mp4(self.v, b"vide", 1000, [(0, b"V0" * 50), (2000, b"V1" * 60), (4000, b"V2" * 40)])
        media.mp4(self.a, b"soun", 48000, [(0, b"A0" * 10), (48000, b"A1" * 10), (96000, b"A2" * 10),
                                            (144000, b"A3" * 10), (192000, b"A4" * 10)])

    def test_equals_file_muxer(self):
        meta = {"title": "t", "artist": "a"}
        mux.merge(self.v, self.a, self.ref, meta)
        plan, size = stream._mp4_plan([FakeRemote(self.v, "mp4", 1), FakeRemote(self.a, "mp4", 2)],
                                      meta, None, None, 6)
        out = produce(stream.Output("x", "mp4", plan, size, 4))
        with open(self.ref, "rb") as fh:
            ref = fh.read()
        self.assertEqual(len(out), size)
        self.assertEqual(out, ref)

    def test_section_rebases_and_cuts(self):
        remotes = [FakeRemote(self.v, "mp4", 1), FakeRemote(self.a, "mp4", 2)]
        plan, size = stream._mp4_plan(remotes, None, 2.5, 3.5, 6)
        out = produce(stream.Output("x", "mp4", plan, size, 4))
        self.assertEqual(len(out), size)
        tfdts = []
        for typ, s, h, e in mux._boxes(out, 0, len(out)):
            if typ == b"moof":
                ds, dh, de = mux._Mp4Source._find(out[s:e], [b"traf", b"tfdt"], 8)
                tfdts.append(struct.unpack_from(">Q", out, s + ds + dh + 4)[0])
        # video fragment from 2 s (rebased to 0) and audio fragments from 2 s and 3 s
        self.assertEqual(sorted(tfdts), [0, 0, 48000])
        self.assertNotIn(b"V0V0", out)
        self.assertIn(b"V1V1", out)


class StreamWebmTest(unittest.TestCase):
    def test_equals_file_muxer(self):
        d = tempfile.mkdtemp()
        v, a, ref = (os.path.join(d, n) for n in ("v.webm", "a.webm", "ref.webm"))
        media.webm(v, 1, "V_VP9", [(0, [b"v0", b"v1"]), (2000, [b"v2"]), (4000, [b"v3"])])
        media.webm(a, 2, "A_OPUS", [(0, [b"a0"]), (1000, [b"a1"]), (2000, [b"a2"]), (3000, [b"a3"])])
        meta = {"title": "t", "cover": b"\xff\xd8" + b"j" * 2000, "cover_mime": "image/jpeg"}
        mux.merge(v, a, ref, meta)
        plan, size = stream._webm_plan([FakeRemote(v, "webm", 1), FakeRemote(a, "webm", 2)], meta, None, None, 10)
        out = produce(stream.Output("x", "webm", plan, size, 3))
        with open(ref, "rb") as fh:
            expected = fh.read()
        self.assertEqual(len(out), size)
        self.assertEqual(out, expected)

    def test_section_webm(self):
        d = tempfile.mkdtemp()
        v, a, got = (os.path.join(d, n) for n in ("v.webm", "a.webm", "got.webm"))
        media.webm(v, 1, "V_VP9", [(0, [b"v0"]), (2000, [b"v1"]), (4000, [b"v2"])])
        media.webm(a, 2, "A_OPUS", [(0, [b"a0"]), (2000, [b"a1"]), (4000, [b"a2"])])
        plan, size = stream._webm_plan([FakeRemote(v, "webm", 1), FakeRemote(a, "webm", 2)], None, 2.5, 3.0, 6)
        out = produce(stream.Output("x", "webm", plan, size, 2))
        self.assertEqual(len(out), size)
        with open(got, "wb") as fh:
            fh.write(out)
        blocks, ids, el = media.webm_blocks(got)
        self.assertEqual(blocks, [(1, b"v1"), (2, b"a1")])
        src = mux._WebmSource(got)
        stamps = [ts for ts, off, ln in src.clusters]
        src.close()
        self.assertEqual(stamps, [0, 0])


class NamesTest(unittest.TestCase):
    def test_filename(self):
        self.assertEqual(stream.filename("{author} - {title}", {"title": "A/B", "author": "C"}, "opus"), "C - A_B.webm")
        self.assertEqual(stream.filename("{nope} {title}", {"title": "x"}, "mp4"), "NA x.mp4")
        self.assertEqual(stream.filename("{title", {"title": "x"}, "mp3"), "x.mp3")


if __name__ == "__main__":
    unittest.main()
