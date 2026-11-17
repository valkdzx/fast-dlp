"""PATH self-fix, --get-url output, bilingual web errors (offline)."""
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from fast_dlp import cli, pathfix
from fast_dlp.web import server, texts


@unittest.skipUnless(os.name == "nt", "PATH is fixed on Windows; elsewhere a hint is printed")
class PathFixTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.scripts = os.path.join(self.tmp.name, "Scripts")
        os.makedirs(self.scripts)
        open(os.path.join(self.scripts, "fast-dlp.exe"), "w").close()
        env = mock.patch.dict(os.environ, {"PATH": r"C:\Windows", "FAST_DLP_NO_PATH": ""})
        env.start()
        self.addCleanup(env.stop)
        patches = [mock.patch.object(pathfix, "scripts_dir", return_value=self.scripts),
                   mock.patch.object(pathfix, "_marker", return_value=os.path.join(self.tmp.name, "path-added")),
                   mock.patch.object(pathfix, "_registry_paths", return_value="")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.added = []
        add = mock.patch.object(pathfix, "_add_windows", side_effect=self.added.append)
        add.start()
        self.addCleanup(add.stop)

    def test_adds_once(self):
        out = io.StringIO()
        self.assertTrue(pathfix.ensure(out=out))
        self.assertEqual(self.added, [self.scripts])
        self.assertIn(self.scripts, out.getvalue())
        # the user took it out again: not re-added on its own...
        self.assertFalse(pathfix.ensure(out=io.StringIO()))
        self.assertEqual(len(self.added), 1)
        # ...but `fast-dlp path` does it on request
        self.assertTrue(pathfix.ensure(force=True, out=io.StringIO()))
        self.assertEqual(len(self.added), 2)

    def test_already_on_path(self):
        with mock.patch.dict(os.environ, {"PATH": r"C:\Windows;" + self.scripts.upper() + "\\"}):
            self.assertFalse(pathfix.ensure(out=io.StringIO()))
        self.assertEqual(self.added, [])

    def test_opt_out(self):
        with mock.patch.dict(os.environ, {"FAST_DLP_NO_PATH": "1"}):
            self.assertFalse(pathfix.ensure(out=io.StringIO()))
        self.assertEqual(self.added, [])

    def test_from_source_does_nothing(self):
        with mock.patch.object(pathfix, "scripts_dir", return_value=None):
            self.assertFalse(pathfix.ensure(out=io.StringIO()))
        self.assertEqual(self.added, [])


class GetUrlTest(unittest.TestCase):
    def test_direct_job_prints_its_url_and_downloads_nothing(self):
        args = cli.build_parser().parse_args(["-g", "x"])
        job = {"source": "direct", "site": "web", "id": "1", "title": "t", "author": "", "audio": False,
               "meta": {}, "cover_url": None, "url": None,
               "resolve": lambda: {"kind": "file", "url": "https://cdn.example/f.mp4", "ext": "mp4"}}
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(cli, "download", side_effect=AssertionError("downloaded")):
            self.assertEqual(cli.process_direct(None, job, args, None), "done")
        self.assertEqual(out.getvalue(), "https://cdn.example/f.mp4\n")

    def test_steam_has_no_url(self):
        args = cli.build_parser().parse_args(["-g", "x"])
        job = {"source": "direct", "site": "steam", "id": "1", "title": "t", "author": "", "audio": False,
               "meta": {}, "cover_url": None, "url": None, "resolve": lambda: {"kind": "steamcmd", "app": 1, "item": 2}}
        with self.assertRaises(cli.FastDlpError):
            cli.process_direct(None, job, args, None)


class WebLanguageTest(unittest.TestCase):
    def test_language_of_a_request(self):
        self.assertEqual(texts.language({"X-Lang": "ru"}), "ru")
        self.assertEqual(texts.language({"X-Lang": "en", "Accept-Language": "ru-RU"}), "en")
        self.assertEqual(texts.language({"Accept-Language": "ru-RU,ru;q=0.9"}), "ru")
        self.assertEqual(texts.language({"Accept-Language": "de-DE"}), "en")
        self.assertEqual(texts.language({}), "en")

    def test_every_message_in_both_languages(self):
        import re
        for key, by_lang in texts.MESSAGES.items():
            self.assertEqual(set(by_lang), set(texts.LANGUAGES), key)
            fields = {lang: set(re.findall(r"\{(\w+)\}", t)) for lang, t in by_lang.items()}
            self.assertEqual(fields["ru"], fields["en"], key)

    def test_errors_follow_the_language(self):
        app = server.App()
        with self.assertRaises(texts.WebError) as caught:
            app.info("   ")
        self.assertIn("paste a link", caught.exception.text("en"))
        self.assertIn("вставьте ссылку", caught.exception.text("ru"))

    def test_eleven_letter_word_is_a_search(self):
        app = server.App()
        job = {"source": "youtube", "video_id": "bbbbbbbbbbb", "hint": {"title": "T"}, "site": "youtube"}
        with mock.patch("fast_dlp.web.targets.search.search_all", return_value=([job, dict(job)], "")) as found:
            info = app.info("Imagination")
        found.assert_called_once()
        self.assertEqual((info["kind"], info["query"]), ("list", "Imagination"))


if __name__ == "__main__":
    unittest.main()
