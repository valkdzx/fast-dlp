import unittest

from fast_dlp import cli, formats, playlist, spotify, utils, ytmusic
from fast_dlp.cipher import _Scanner, _builder_name, _strip_lead, prepare_player


class UrlTest(unittest.TestCase):
    def test_video_ids(self):
        for text in ("AbCdEfGh1_K", "https://www.youtube.com/watch?v=AbCdEfGh1_K&t=10",
                     "https://youtu.be/AbCdEfGh1_K?si=x", "https://www.youtube.com/shorts/AbCdEfGh1_K",
                     "https://music.youtube.com/watch?v=AbCdEfGh1_K&list=RDAMVM",
                     "https://www.youtube.com/embed/AbCdEfGh1_K", "https://www.youtube.com/live/AbCdEfGh1_K"):
            self.assertEqual(utils.extract_video_id(text), "AbCdEfGh1_K", text)
        self.assertIsNone(utils.extract_video_id("https://example.com/"))

    def test_playlists(self):
        url = "https://www.youtube.com/playlist?list=PLaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        self.assertEqual(playlist.playlist_id(url), "PLaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        self.assertTrue(playlist.is_playlist_only(url))
        # a video inside a playlist is the video
        self.assertFalse(playlist.is_playlist_only("https://www.youtube.com/watch?v=AbCdEfGh1_K&list=PLxxxxxxxxxxxx"))
        self.assertIsNone(playlist.playlist_id("https://www.youtube.com/watch?v=x&list=RDAbCdEfGh1_K"))

    def test_spotify(self):
        self.assertEqual(spotify.parse("https://open.spotify.com/track/TrackIdTrackIdTrackId1?si=1"),
                         ("track", "TrackIdTrackIdTrackId1"))
        self.assertEqual(spotify.parse("https://open.spotify.com/intl-ru/album/4yP0hdKOZPNshxUOjY0cZj"),
                         ("album", "4yP0hdKOZPNshxUOjY0cZj"))
        self.assertEqual(spotify.parse("spotify:playlist:37i9dQZF1DX0000000000A"),
                         ("playlist", "37i9dQZF1DX0000000000A"))
        self.assertIsNone(spotify.parse("https://youtu.be/AbCdEfGh1_K"))

    def test_sanitize(self):
        self.assertEqual(utils.sanitize_filename('a/b:c*d?"e<f>|g'), "a_b_c_d__e_f__g")
        self.assertEqual(utils.sanitize_filename("  name. "), "name")
        self.assertEqual(utils.sanitize_filename(""), "video")
        self.assertEqual(utils.sanitize_filename("кризис.", edges=False), "кризис.")

    def test_render_keeps_inner_dots(self):
        class V:
            id, title, author = "x", "тупой", "кризис."
        job = {"playlist": "Почему ты не улыбаешься?", "index": "3"}
        name = cli.render_name("{playlist}/{index} - {author} - {title}.{ext}", V, [], "mp3", job)
        self.assertEqual(name.replace("\\", "/"), "Почему ты не улыбаешься_/3 - кризис. - тупой.mp3")
        name = cli.render_name("{author}/{title}.{ext}", V, [], "mp3", {})
        self.assertEqual(name.replace("\\", "/"), "кризис/тупой.mp3")       # folder can't end in '.'


class CliTest(unittest.TestCase):
    def test_playlist_items(self):
        self.assertEqual(cli.parse_items(None, 5), [1, 2, 3, 4, 5])
        self.assertEqual(cli.parse_items("1-3,5", 10), [1, 2, 3, 5])
        self.assertEqual(cli.parse_items("8-", 10), [8, 9, 10])
        self.assertEqual(cli.parse_items("-2,40", 10), [1, 2])

    def test_numbering_pads_and_filters(self):
        args = cli.build_parser().parse_args(["x", "-I", "2,11"])
        jobs = cli._numbered([{"n": i} for i in range(12)], args)
        self.assertEqual([j["index"] for j in jobs], ["02", "11"])
        self.assertEqual(jobs[0]["total"], 12)


def fmt(itag, mime, bitrate, height=None, fps=None, size=1000, **extra):
    f = {"itag": itag, "mimeType": mime, "bitrate": bitrate, "contentLength": str(size), "url": "u"}
    if height:
        f.update(height=height, width=height * 16 // 9, fps=fps or 30, qualityLabel=f"{height}p")
    f.update(extra)
    return f


FORMATS = [
    fmt(137, 'video/mp4; codecs="avc1.640028"', 4000000, 1080),
    fmt(248, 'video/webm; codecs="vp9"', 1500000, 1080),
    fmt(313, 'video/webm; codecs="vp9"', 18000000, 2160),
    fmt(401, 'video/mp4; codecs="av01.0.12M.08"', 17000000, 2160),
    fmt(140, 'audio/mp4; codecs="mp4a.40.2"', 130000),
    fmt(251, 'audio/webm; codecs="opus"', 136000),
    fmt(0, 'audio/webm; codecs="opus"', 0, size=0),       # no size: ignored
]


class FormatsTest(unittest.TestCase):
    def test_best_pairs_same_container(self):
        v, a = formats.select(FORMATS)
        self.assertEqual(v["itag"], 401)                  # 2160p, av01 ranks above vp9
        self.assertEqual(a["itag"], 140)                  # m4a goes with mp4

    def test_max_height_and_codec(self):
        v, a = formats.select(FORMATS, max_height=1080, prefer_codec="vp9")
        self.assertEqual((v["itag"], a["itag"]), (248, 251))

    def test_explicit_and_audio(self):
        self.assertEqual([f["itag"] for f in formats.select(FORMATS, "137+251")], [137, 251])
        self.assertEqual(formats.select(FORMATS, "bestaudio")[0]["itag"], 251)
        with self.assertRaises(utils.FastDlpError):
            formats.select(FORMATS, "999")


class MatchTest(unittest.TestCase):
    TRACK = {"title": "Song Title", "artists": ["Artist Name"], "duration": 200}

    def test_exact_beats_cover(self):
        exact = {"title": "Song Title", "artists": ["Artist Name"], "duration": 201}
        cover = {"title": "Song Title", "artists": ["L.A Band"], "duration": 199}
        self.assertGreater(ytmusic.score(self.TRACK, exact)[0], ytmusic.score(self.TRACK, cover)[0])

    def test_noise_and_reupload(self):
        reup = {"title": "Artist Name - Song Title (feat. Nobody)", "artists": ["7clouds"], "duration": 200}
        s, title, artist, diff = ytmusic.score(self.TRACK, reup)
        self.assertGreaterEqual(title, 0.9)
        self.assertGreaterEqual(artist, 0.8)              # artist found in the title

    def test_duration_penalty(self):
        long = {"title": "Song Title", "artists": ["Artist Name"], "duration": 260}
        self.assertEqual(ytmusic.score(self.TRACK, long)[3], 60)


PLAYER = """var _yt_player={};(function(g){var window=this;/*
 comment */
var a=1,b="x;y";g.k=function(c){return c/2};
sideEffect(document.body);
new g.Thing;
g.ch=function(u){this.u=u;this.p={}};g.ch.prototype.set=function(k,v){this.p[k]=v};
g.ch[c0[1]][c0[2]]=function(){};
om=function(a,b,c){a=new g.ch(a);a.set("alr","yes");c&&a.set(b,c);return a};
nest=function(a){if(a){b.set("alr","yes")}};
re=/[;{}]/g;tpl=`a${1+{x:1}.x}b`;
if(x){y()}else{z()}
"use strict";
})(_yt_player);"""


class CipherTest(unittest.TestCase):
    def test_strip_lead(self):
        self.assertEqual(_strip_lead("  /* a */ // b\n var x"), "var x")

    def test_statement_split_and_prepare(self):
        start = PLAYER.index("{", PLAYER.index("(function(g)")) + 1
        end = PLAYER.rindex("})(_yt_player)")
        spans = list(_Scanner(PLAYER).statements(start, end))
        texts = [_strip_lead(PLAYER[s:e]).strip() for s, e in spans]
        self.assertIn('var a=1,b="x;y";', texts)          # ; inside a string does not split
        self.assertIn("re=/[;{}]/g;", texts)               # nor inside a regex
        self.assertIn("tpl=`a${1+{x:1}.x}b`;", texts)      # nor braces inside a template
        prepared = prepare_player(PLAYER)
        self.assertNotIn("sideEffect(document.body)", prepared)   # expression statements blanked
        self.assertNotIn("new g.Thing", prepared)
        self.assertIn("g.ch[c0[1]][c0[2]]=function", prepared)  # nested-bracket assignment kept
        self.assertIn("var url=(om)(", prepared)                # the URL builder found
        self.assertNotIn("var url=(nest)(", prepared)           # a nested .set("alr") is not one
        self.assertTrue(prepared.rstrip().endswith("return _cdisc;"))

    def test_builder_name(self):
        self.assertEqual(_builder_name('om=function(a){a=new X(a);a.set("alr","yes");return a};'), "om")
        self.assertIsNone(_builder_name('f=function(a){a.get("alr")||a.set("alr","yes")};'))


if __name__ == "__main__":
    unittest.main()
