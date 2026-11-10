import http.server
import os
import struct
import tempfile
import threading
import unittest

from fast_dlp import hls, mp3, ts
from fast_dlp.aes import AES128
from fast_dlp.sources import find, generic, pinterest, soundcloud, steam, tiktok, vk, yandex

KEY = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
IV = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
CIPHER = bytes.fromhex("7649abac8119b246cee98e9b12e9197d5086cb9b507219ee95db113a917678b2"
                       "73bed6b8e3c1743b7116e69e222295163ff1caa1681fac09120eca307586e1a7")
PLAIN = bytes.fromhex("6bc1bee22e409f96e93d7e117393172aae2d8a571e03ac9c9eb76fac45af8e51"
                      "30c81c46a35ce411e5fbc1191a0a52eff69f2445df4f9b17ad2b417be66c3710")


class AesTest(unittest.TestCase):
    def test_fips197_block(self):
        k = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        self.assertEqual(AES128(k).decrypt_block(bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")),
                         bytes.fromhex("00112233445566778899aabbccddeeff"))

    def test_sp800_38a_cbc(self):
        self.assertEqual(AES128(KEY).decrypt_cbc(CIPHER, IV, unpad=False), PLAIN)

    def test_pkcs7_unpad(self):
        # the CBC chain of PLAIN[:48] + 16 bytes of padding 0x10, built from the vector's own blocks
        self.assertEqual(len(AES128(KEY).decrypt_cbc(CIPHER, IV)), 64)   # last byte 0x10 but not a full pad


def _ts_packet(pid, payload, start=False):
    header = bytes([0x47, (0x40 if start else 0) | (pid >> 8), pid & 0xFF])
    if len(payload) < 184:
        stuffing = 184 - len(payload) - 1                # adaptation field: length byte + stuffing
        af = bytes([stuffing]) + (bytes([0x00]) + b"\xff" * (stuffing - 1) if stuffing else b"")
        return header + bytes([0x30]) + af + payload
    return header + bytes([0x10]) + payload


def _section(table_id, body):
    length = len(body) + 4                                # + CRC
    return bytes([table_id, 0xB0 | (length >> 8), length & 0xFF]) + body + b"\0\0\0\0"


def synthetic_ts(audio):
    pat = _section(0x00, b"\x00\x01\xC1\x00\x00" + b"\x00\x01\xF0\x00")              # program 1 -> PMT 0x1000
    pmt = _section(0x02, b"\x00\x01\xC1\x00\x00" + b"\xE1\x00\xF0\x00" + b"\x03\xE1\x00\xF0\x00")
    out = _ts_packet(0, b"\x00" + pat, True) + _ts_packet(0x1000, b"\x00" + pmt, True)
    pes = b"\x00\x00\x01\xC0" + struct.pack(">H", len(audio) + 3) + b"\x80\x00\x00" + audio
    first, rest = pes[:184], pes[184:]
    out += _ts_packet(0x100, first, True)
    while rest:
        out += _ts_packet(0x100, rest[:184])
        rest = rest[184:]
    return out


class TsTest(unittest.TestCase):
    def test_streams_and_extract(self):
        audio = b"\xff\xfb\x90\x00" + bytes(range(256)) * 3
        data = synthetic_ts(audio)
        self.assertEqual(len(data) % 188, 0)
        self.assertEqual(ts.streams(data), {0x100: "mp3"})
        self.assertEqual(ts.audio(data), ("mp3", audio))


class _Server:
    def __init__(self, files):
        files = dict(files)

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = files.get(self.path)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class HlsTest(unittest.TestCase):
    def test_master_encrypted_and_plain_segments(self):
        plain_seg = b"\xff\xfb\x90\x00" + b"A" * 60
        server = _Server({
            "/master.m3u8": b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\nlow.m3u8\n"
                            b"#EXT-X-STREAM-INF:BANDWIDTH=900\nhigh.m3u8\n",
            "/high.m3u8": (b"#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n"
                           b"#EXT-X-KEY:METHOD=AES-128,URI=\"key.bin\",IV=0x000102030405060708090a0b0c0d0e0f\n"
                           b"#EXTINF:5,\nseg0.mp3\n#EXT-X-KEY:METHOD=NONE\n#EXTINF:5,\nseg1.mp3\n#EXT-X-ENDLIST\n"),
            "/low.m3u8": b"#EXTM3U\n#EXTINF:5,\nnope\n",
            "/key.bin": KEY,
            "/seg0.mp3": CIPHER,
            "/seg1.mp3": plain_seg,
        })
        try:
            out = hls.download(server.base + "/master.m3u8", os.path.join(tempfile.mkdtemp(), "x"),
                               audio_only=True, show_progress=False)
            with open(out, "rb") as fh:
                data = fh.read()
            self.assertEqual(data, PLAIN + plain_seg)        # high variant, decrypted, joined in order
        finally:
            server.close()

    def test_parse_rejects_drm(self):
        with self.assertRaises(Exception):
            hls.parse("#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI=\"k\"\n#EXTINF:1,\na.ts\n", "http://x/")


class Id3RewriteTest(unittest.TestCase):
    def test_tag_file_replaces_existing(self):
        path = os.path.join(tempfile.mkdtemp(), "a.mp3")
        audio = b"\xff\xfb\x90\x00" + b"\x55" * 500
        with open(path, "wb") as fh:
            fh.write(mp3.id3v23({"title": "old"}) + audio)
        mp3.tag_file(path, {"title": "new", "artist": "me"})
        with open(path, "rb") as fh:
            data = fh.read()
        self.assertTrue(data.endswith(audio))
        self.assertEqual(data.count(b"ID3\x03"), 1)
        self.assertIn("new".encode("utf-16")[2:], data)


class MatchersTest(unittest.TestCase):
    def test_routing(self):
        cases = {
            "https://music.yandex.ru/album/38551467/track/143738516": yandex,
            "https://music.yandex.com/users/someone/playlists/3": yandex,
            "https://vk.com/audio-2001_456239017": vk,
            "https://vk.com/music/playlist/-2000_123_abcdef0123": vk,
            "https://vk.com/feed?z=audio_playlist-147845620_2949": vk,
            "https://soundcloud.com/forss/flickermood": soundcloud,
            "https://soundcloud.com/forss/sets/soulhack": soundcloud,
            "https://www.pinterest.com/pin/99360735500167749/": pinterest,
            "https://ru.pinterest.com/pin/some-title--99360735500167749/": pinterest,
            "https://pin.it/abc123": pinterest,
            "https://steamcommunity.com/sharedfiles/filedetails/?id=497669393": steam,
            "https://steamcommunity.com/workshop/filedetails/?l=en&id=2503622437": steam,
            "https://example.com/file.zip": generic,
            "https://www.tiktok.com/@scout2015/video/6718335390845095173": tiktok,
            "https://www.tiktok.com/@someone/photo/7300000000000000000": tiktok,
            "https://vm.tiktok.com/ZMabcdEF/": tiktok,
            "https://www.tiktok.com/t/ZTabc123/": tiktok,
        }
        for url, module in cases.items():
            self.assertIs(find(url), module, url)
        self.assertIsNot(find("https://vk.com/id1"), vk)        # a profile is not music

    def test_yandex_locate(self):
        self.assertEqual(yandex.locate("https://music.yandex.ru/album/1/track/22"), ("track", "22"))
        self.assertEqual(yandex.locate("https://music.yandex.ru/album/1"), ("album", "1"))
        self.assertEqual(yandex.locate("https://music.yandex.ru/users/me/playlists/7"), ("playlist", "me:7"))


if __name__ == "__main__":
    unittest.main()
