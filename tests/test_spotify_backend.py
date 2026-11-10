import json
import unittest
from unittest import mock

from fast_dlp import spotify

RAW_TRACK = {
    "id": "TrackIdTrackIdTrackId1", "name": "Track One", "duration_ms": 213573,
    "artists": [{"name": "Artist Name"}], "external_ids": {"isrc": "XX0000000001"}, "track_number": 1,
    "album": {"name": "Album Name", "release_date": "2001-02-03",
              "artists": [{"name": "Artist Name"}], "images": [{"url": "https://i.scdn.co/image/big"}]},
}
PLAYLIST = {
    "type": "playlist", "name": "Hot", "total": 2, "count": 1, "truncated": True, "cover": "pl.jpg",
    "tracks": [{"index": 1, "id": "TrackIdTrackIdTrackId1", "title": "Track One",
                "artists": ["Artist Name"], "album": "Album Name", "duration_ms": 213573,
                "isrc": "XX0000000001", "track_number": 1}],
}


def fake_request(routes):
    def request(url, **kw):
        for part, payload in routes.items():
            if part in url:
                assert "beta=true" in url
                status = 404 if "error" in payload else 200
                return status, json.dumps(payload).encode()
        raise AssertionError(url)
    return request


class BackendTest(unittest.TestCase):
    def client(self):
        c = spotify.Spotify()
        c.config = {}
        return c

    def test_track_from_raw_json(self):
        with mock.patch.object(spotify.net, "request", fake_request({"track%2FTrackId": RAW_TRACK})):
            t = self.client().track("TrackIdTrackIdTrackId1")
        self.assertEqual((t["isrc"], t["album"], t["date"], t["cover"], t["duration"]),
                         ("XX0000000001", "Album Name", "2001-02-03",
                          "https://i.scdn.co/image/big", 214))

    def test_playlist_then_complete(self):
        routes = {"playlist%2F": PLAYLIST, "track%2FTrackId": RAW_TRACK}
        c = self.client()
        with mock.patch.object(spotify.net, "request", fake_request(routes)), \
                mock.patch.object(spotify, "warn") as warned:
            title, tracks = c.collection("playlist", "PlaylistIdPlaylistId12")
            self.assertEqual(title, "Hot")
            self.assertIsNone(tracks[0]["cover"])
            self.assertIn("1 of 2", warned.call_args[0][0])
            c.complete(tracks[0])
        self.assertEqual(tracks[0]["cover"], "https://i.scdn.co/image/big")
        self.assertEqual(tracks[0]["date"], "2001-02-03")
        self.assertIsNone(tracks[0]["track_number"])        # playlist position stays the track number

    def test_backend_error_falls_back_to_embed(self):
        routes = {"playlist%2F": {"error": "editorial playlist", "code": "not_found"}}
        c = self.client()
        with mock.patch.object(spotify.net, "request", fake_request(routes)), \
                mock.patch.object(c, "_embed_collection", return_value=("E", [])) as embed, \
                mock.patch.object(spotify, "warn"):
            self.assertEqual(c.collection("playlist", "x" * 22), ("E", []))
        embed.assert_called_once()


if __name__ == "__main__":
    unittest.main()
