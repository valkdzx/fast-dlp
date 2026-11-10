"""The web server: one-time streaming links and delivery confirmation over a real socket."""
import http.client
import json
import socket
import threading
import time
import unittest

from fast_dlp.utils import FastDlpError
from fast_dlp.web import server


class FakeOutput:
    def __init__(self, blocks, size=True, ext="mp4"):
        self.blocks = blocks
        self.size = sum(len(b) for b in blocks) if size else None
        self.ext = ext
        self.mime = "video/mp4"

    def chunks(self):
        for b in self.blocks:
            time.sleep(0.01)
            yield b


class FakeVideo:
    id, title, author = "abcdefghijk", "Тест", "Автор"


class WebTest(unittest.TestCase):
    def setUp(self):
        self.app = server.App()
        self.app.verbose = False
        self.app.log = lambda m: None
        handler = type("H", (server.Handler,), {"app": self.app})
        self.httpd = server.Server(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        server.CONFIRM_TIMEOUT = 5

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def add(self, output):
        job = server.Job("tok" + str(len(self.app.jobs)), FakeVideo(), {}, output, [{"itag": 1}], "Тест.mp4")
        self.app.jobs[job.token] = job
        return job

    def wait_final(self, job, states=("delivered", "unconfirmed", "aborted", "failed")):
        for _ in range(300):
            if job.state in states:
                return job.state
            time.sleep(0.02)
        return job.state

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", path)
        return c, c.getresponse()

    def test_delivered_then_gone(self):
        job = self.add(FakeOutput([b"a" * 70000, b"b" * 70000, b"c" * 5]))
        c, r = self.get(f"/dl/{job.token}")
        self.assertEqual(r.status, 200)
        self.assertEqual(int(r.getheader("Content-Length")), 140005)
        self.assertIn("filename*=UTF-8''%D0%A2%D0%B5%D1%81%D1%82.mp4", r.getheader("Content-Disposition"))
        body = r.read()
        c.close()
        self.assertEqual(len(body), 140005)
        self.assertEqual(self.wait_final(job), "delivered")
        self.assertEqual(job.sent, 140005)
        c, r = self.get(f"/dl/{job.token}")
        self.assertEqual(r.status, 410)                      # one-time link
        r.read()
        c.close()

    def test_reader_leaving_early_is_aborted_and_retry_works(self):
        job = self.add(FakeOutput([b"x" * 4_000_000] * 8))
        s = socket.create_connection(("127.0.0.1", self.port))
        s.sendall(f"GET /dl/{job.token} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        s.recv(65536)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
        s.close()                                            # reset: the browser went away
        self.assertEqual(self.wait_final(job), "aborted")
        self.assertLess(job.sent, 32_000_000)
        c, r = self.get(f"/dl/{job.token}")
        self.assertEqual(len(r.read()), 32_000_000)
        c.close()
        self.assertEqual(self.wait_final(job, ("delivered",)), "delivered")
        self.assertEqual(job.attempts, 2)

    def test_chunked_when_size_unknown(self):
        job = self.add(FakeOutput([b"ID3", b"mp3data" * 1000], size=False))
        c, r = self.get(f"/dl/{job.token}")
        self.assertEqual(r.getheader("Transfer-Encoding"), "chunked")
        self.assertEqual(r.read(), b"ID3" + b"mp3data" * 1000)  # http.client undoes the chunking
        c.close()
        self.assertEqual(self.wait_final(job), "delivered")

    def test_status_batch_and_unknown(self):
        job = self.add(FakeOutput([b"a"]))
        c, r = self.get(f"/api/status?t={job.token},nope")
        data = json.loads(r.read())
        c.close()
        self.assertEqual(data["nope"], None)
        self.assertEqual(data[job.token]["state"], "ready")
        self.assertEqual(data[job.token]["size"], 1)
        c, r = self.get("/dl/missing")
        self.assertEqual(r.status, 404)
        r.read()
        c.close()

    def test_page_served(self):
        c, r = self.get("/")
        page = r.read().decode()
        c.close()
        self.assertEqual(r.status, 200)
        self.assertIn("fast-dlp", page)
        c, r = self.get("/logo.svg")
        self.assertIn(b"<svg", r.read())
        c.close()


class OptionsTest(unittest.TestCase):
    def test_validation(self):
        app = server.App()
        o = app.options({"mode": "evil", "container": "avi", "codec": "x", "connections": 999,
                         "height": "1080", "start": "1:00", "end": "1:30", "template": "{title}" * 100})
        self.assertEqual((o["mode"], o["container"], o["codec"], o["connections"], o["height"]),
                         ("video", "mp4", "", 32, 1080))
        self.assertEqual((o["start"], o["end"]), (60.0, 90.0))
        self.assertEqual(len(o["template"]), 200)
        with self.assertRaises(FastDlpError):
            app.options({"start": "2:00", "end": "1:00"})
        with self.assertRaises(FastDlpError):
            app.options({"start": "x:y"})
        self.assertEqual(app.options({"mode": "audio"})["cover"], True)
        self.assertEqual(app.options({})["cover"], False)

    def test_rejects_non_youtube(self):
        with self.assertRaises(FastDlpError):
            server.App().video("https://example.com/file")


if __name__ == "__main__":
    unittest.main()
