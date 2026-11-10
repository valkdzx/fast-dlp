"""Live checks against YouTube / Spotify. Opt-in: set FAST_DLP_NETWORK=1."""
import os
import socket
import unittest

from fast_dlp import innertube, net, playlist, ytmusic
from fast_dlp.extractor import Extractor

LIVE = os.environ.get("FAST_DLP_NETWORK") == "1"


@unittest.skipUnless(LIVE, "set FAST_DLP_NETWORK=1 to run network tests")
class LiveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.extractor = Extractor()

    def test_visionos_direct_links(self):
        video = self.extractor.extract("jNQXAC9IVRw")
        self.assertTrue(video.formats)
        self.assertTrue(all(f.get("url") for f in video.formats))
        audio = max((f for f in video.formats if f["mimeType"].startswith("audio/")), key=lambda f: f["bitrate"])
        last = int(audio["contentLength"]) - 1
        status, _ = net.request(audio["url"], headers={"User-Agent": innertube.VISION_UA,
                                                       "Range": f"bytes={last}-{last}"})
        self.assertIn(status, (200, 206))

    def test_song_match(self):
        # Take a song YouTube Music itself lists, then ask the matcher to find it again.
        song = ytmusic.search("Beethoven Fur Elise")[0]
        cand, how = ytmusic.find({"title": song["title"], "artists": song["artists"],
                                  "duration": song["duration"]})
        self.assertTrue(cand["video_id"])
        self.assertTrue(how)

    def test_playlist_continuation(self):
        title, entries = playlist.fetch("UUBR8-60-B28hp2BmDPdntcQ")          # uploads of YouTube's own channel
        self.assertTrue(title)
        self.assertGreaterEqual(len(entries), 20)
        self.assertEqual(len({e["video_id"] for e in entries}), len(entries))

    def test_ipv4_available(self):
        if not net.has_ipv4():
            self.skipTest("no IPv4 route")
        self.assertTrue(socket.getaddrinfo("www.youtube.com", 443, socket.AF_INET))


if __name__ == "__main__":
    unittest.main()
