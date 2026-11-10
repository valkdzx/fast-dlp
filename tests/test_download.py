"""Downloader: chunk sizes that follow the network, files sized without writing zeros."""
import os
import tempfile
import time
import unittest

from fast_dlp import download, utils

MiB = 1024 * 1024


class DynamicChunksTest(unittest.TestCase):
    def work(self, size, connections=16):
        return download._Work([("stream", 0, size - 1, "key")], connections)

    def size_of(self, job):
        return job[2] - job[1] + 1

    def test_first_chunk_measures(self):
        self.assertEqual(self.size_of(self.work(1000 * MiB).take(None)), download.START_CHUNK)

    def test_slow_line_small_chunks(self):
        # 60 Mbit/s over 16 connections: ~0.47 MB/s each -> about 2 s of data per request
        per_conn = 60e6 / 8 / 16
        self.assertAlmostEqual(self.size_of(self.work(1000 * MiB).take(per_conn)), per_conn * 2, delta=1)

    def test_fast_line_capped(self):
        # a gigabit connection would want 2 s x 8 MB/s; googlevideo throttles ranges over ~10 MB
        self.assertEqual(self.size_of(self.work(1000 * MiB).take(1e9 / 8)), download.MAX_CHUNK)

    def test_never_below_minimum(self):
        self.assertEqual(self.size_of(self.work(1000 * MiB).take(1000)), download.MIN_CHUNK)

    def test_tail_is_shared(self):
        # 16 MiB left, 16 connections: nobody takes 8 MiB of it while the others idle
        w = self.work(16 * MiB)
        self.assertEqual(self.size_of(w.take(1e9)), MiB)

    def test_covers_every_byte_once(self):
        w = download._Work([("a", 0, 10 * MiB + 7, 1), ("b", 100, 3 * MiB, 2)], 4)
        seen, rates = {"a": [], "b": []}, [None, 3e5, 2e6, 5e7]
        i = 0
        while True:
            job = w.take(rates[i % len(rates)])
            i += 1
            if job is None:
                break
            seen[job[0]].append((job[1], job[2]))
        for name, (lo, hi) in (("a", (0, 10 * MiB + 7)), ("b", (100, 3 * MiB))):
            spans = sorted(seen[name])
            self.assertEqual(spans[0][0], lo)
            self.assertEqual(spans[-1][1], hi)
            for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
                self.assertEqual(a1 + 1, b0)
        self.assertEqual(w.left, 0)


class AllocateTest(unittest.TestCase):
    def test_large_file_instantly(self):
        path = os.path.join(tempfile.mkdtemp(), "big.part")
        t = time.time()
        utils.allocate(path, 3 * 1024 ** 3)
        took = time.time() - t
        try:
            self.assertEqual(os.path.getsize(path), 3 * 1024 ** 3)
            self.assertLess(took, 1.0)           # zero-filling 3 GiB takes seconds
            with open(path, "r+b") as fh:
                fh.seek(2 * 1024 ** 3)
                fh.write(b"data")
                fh.seek(12345)
                self.assertEqual(fh.read(4), b"\0\0\0\0")
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
