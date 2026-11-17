"""-F: DRC and dubbed copies of the same itag, -f ids for them, the table (offline)."""
import unittest

from fast_dlp import formats, i18n


def f(itag, mime, bitrate, height=None, **extra):
    d = {"itag": itag, "mimeType": mime, "bitrate": bitrate, "contentLength": str(bitrate * 10), "url": f"u{itag}"}
    if height:
        d.update(height=height, width=height * 16 // 9, fps=30, qualityLabel=f"{height}p")
    d.update(extra)
    return d


DRC_TAGS = "CggKA2RyYxIBMQ"                      # base64 protobuf saying drc=1, as YouTube sends it
FORMATS = [
    f(299, 'video/mp4; codecs="avc1.64002A"', 6000000, 1080, fps=60),
    f(401, 'video/mp4; codecs="av01.0.13M.08"', 17000000, 2160, qualityLabel="2160p60 HDR"),
    f(140, 'audio/mp4; codecs="mp4a.40.2"', 130000, audioSampleRate="44100"),
    f(140, 'audio/mp4; codecs="mp4a.40.2"', 131000, xtags=DRC_TAGS, audioSampleRate="44100"),
    f(251, 'audio/webm; codecs="opus"', 140000, audioChannels=6),
    f(251, 'audio/webm; codecs="opus"', 141000, xtags=DRC_TAGS),
    f(251, 'audio/webm; codecs="opus"', 139000,
      audioTrack={"id": "de.3", "displayName": "Deutsch", "audioIsDefault": False}),
]


class DrcTest(unittest.TestCase):
    def test_base64_xtags_are_read(self):
        self.assertTrue(formats.is_drc(FORMATS[3]))
        self.assertTrue(formats.is_drc({"isDrc": True}))
        self.assertFalse(formats.is_drc(FORMATS[2]))

    def test_ids(self):
        self.assertEqual([formats.format_id(x) for x in FORMATS],
                         ["299", "401", "140", "140-drc", "251", "251-drc", "251-de"])

    def test_bare_itag_is_the_main_copy(self):
        for spec, wanted in (("140", 2), ("140-drc", 3), ("251", 4), ("251-de", 6), ("299+140-drc", 3)):
            self.assertIs(formats.select(FORMATS, spec)[-1], FORMATS[wanted], spec)

    def test_best_audio_skips_drc_even_when_louder(self):
        self.assertIs(formats.select(FORMATS, "bestaudio")[0], FORMATS[4])


class TableTest(unittest.TestCase):
    def tearDown(self):
        i18n.set_language(None)

    def test_sections_star_and_notes(self):
        i18n.set_language("en")
        chosen = formats.select(FORMATS)
        text = formats.table(FORMATS, chosen)
        lines = text.splitlines()
        self.assertEqual(lines[0], "Video — 2")
        self.assertIn("Audio — 5", lines)
        row401 = next(l for l in lines if l.strip().startswith("401 "))
        self.assertIn("2160p HDR", row401)
        self.assertIn("AV1", row401)
        self.assertTrue(row401.rstrip().endswith("*"))
        self.assertIn("compressed loudness (DRC)", next(l for l in lines if "140-drc" in l))
        self.assertIn("Deutsch", next(l for l in lines if "251-de" in l))
        self.assertIn("5.1", next(l for l in lines if l.strip().startswith("251 ")))
        self.assertIn("AAC-LC", text)
        # columns line up: every video row has the codec at the same place
        starts = {l.index("H.264") if "H.264" in l else l.index("AV1") for l in lines[2:4]}
        self.assertEqual(len(starts), 1)

    def test_russian(self):
        i18n.set_language("ru")
        text = formats.table(FORMATS, [])
        self.assertIn("Видео — 2", text)
        self.assertIn("сжатая громкость", text)
        self.assertIn("Мбит/с", text)


if __name__ == "__main__":
    unittest.main()
