"""Tags in an ordinary (non-fragmented) MP4 without moving its media data, and the progress line."""
import io
import os
import struct
import tempfile
import unittest
from unittest import mock

from fast_dlp import download, mux

B = mux._box


def progressive(path, moov_first):
    """ftyp + moov + mdat (or ftyp + mdat + moov) with an stco pointing into mdat."""
    payload = b"MEDIA" * 2000
    ftyp = B(b"ftyp", b"isom\0\0\x02\0isomavc1")

    def moov(mdat_pos):
        stco = B(b"stco", struct.pack(">II", 0, 1) + struct.pack(">I", mdat_pos + 8))
        trak = B(b"trak", B(b"mdia", B(b"minf", B(b"stbl", stco))))
        return B(b"moov", B(b"mvhd", b"\0" * 100) + trak + B(b"udta", B(b"old!", b"x")))

    mdat = B(b"mdat", payload)
    if moov_first:
        m = moov(0)
        data = ftyp + moov(len(ftyp) + len(m)) + mdat
    else:
        data = ftyp + mdat + moov(len(ftyp))
    with open(path, "wb") as fh:
        fh.write(data)
    return payload


def media_and_moov(path):
    with open(path, "rb") as fh:
        data = fh.read()
    boxes = {t: (s, e) for t, s, h, e in mux._boxes(data, 0, len(data))}
    s, e = boxes[b"moov"]
    moov = data[s:e]
    stco = moov.find(b"stco")
    offset = struct.unpack_from(">I", moov, stco + 12)[0]
    return data, moov, offset


class ProgressiveTagTest(unittest.TestCase):
    def check(self, moov_first):
        path = os.path.join(tempfile.mkdtemp(), "v.mp4")
        payload = progressive(path, moov_first)
        _, _, offset_before = media_and_moov(path)
        mux.tag(path, {"title": "Название", "artist": "Автор"})
        data, moov, offset = media_and_moov(path)
        self.assertEqual(offset, offset_before)                      # sample offsets untouched
        self.assertEqual(data[offset:offset + len(payload)], payload)  # ...and still point at the media
        self.assertIn("Название".encode(), moov)
        self.assertNotIn(b"old!", moov)                              # the old udta is replaced
        self.assertEqual(data.count(b"moov"), 1)
        return data

    def test_moov_at_end_replaced_in_place(self):
        data = self.check(moov_first=False)
        self.assertNotIn(b"free", data)

    def test_moov_at_start_moved_to_end(self):
        data = self.check(moov_first=True)
        types = [t for t, s, h, e in mux._boxes(data, 0, len(data))]
        self.assertEqual(types, [b"ftyp", b"free", b"mdat", b"moov"])


class ProgressLineTest(unittest.TestCase):
    def test_final_line_clears_longer_waiting_line(self):
        err = io.StringIO()
        p = download._Progress(100, False)
        with mock.patch("sys.stderr", err):
            p._write(p._line())                  # nothing yet: "waiting for the first bytes 0s"
            p.done = 100
            p._write(p._line(final=True), "\n")
        waiting, final = err.getvalue().split("\r")[1:]
        self.assertIn("waiting for the first bytes", waiting)
        self.assertGreaterEqual(len(final.rstrip("\n")), len(waiting))
        self.assertNotIn("first bytes", final)


if __name__ == "__main__":
    unittest.main()
