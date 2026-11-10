import argparse
import io
import unittest
from unittest import mock

from fast_dlp import cli, tui


def picker(n=5):
    opts = {
        "format": tui.Option("Формат", ["best", "opus", "m4a", "mp3"], "best"),
        "quality": tui.Option("Качество MP3", ["320k", "192k"], "320k",
                              visible=lambda o: o["format"].value == "mp3"),
        "jobs": tui.Option("Параллельно", list(range(1, 9)), 4),
    }
    return tui.Picker("T", [{"label": f"track {i}", "right": "3:00"} for i in range(n)], opts)


class PickerTest(unittest.TestCase):
    def test_space_toggles_and_moves_down(self):
        p = picker()
        p.handle(tui.SPACE)
        self.assertEqual(p.checked, [False, True, True, True, True])
        self.assertEqual(p.cursor, 1)

    def test_all_none_invert(self):
        p = picker()
        p.handle("n")
        self.assertFalse(any(p.checked))
        p.handle("i")
        self.assertTrue(all(p.checked))
        p.handle(tui.SPACE)
        p.handle("a")
        self.assertTrue(all(p.checked))

    def test_options_left_right_and_visibility(self):
        p = picker()
        self.assertEqual(p.rows(), 5 + 2)                  # quality hidden until format is mp3
        p.handle(tui.END)
        p.handle(tui.UP)                                   # on "Формат"
        p.handle(tui.LEFT)                                 # best -> mp3 (wraps)
        self.assertEqual(p.options["format"].value, "mp3")
        self.assertEqual(p.rows(), 5 + 3)                  # quality now shown
        p.handle(tui.DOWN)
        p.handle(tui.RIGHT)
        self.assertEqual(p.options["quality"].value, "192k")
        p.handle(tui.DOWN)
        p.handle(tui.RIGHT)
        self.assertEqual(p.options["jobs"].value, 5)

    def test_left_right_page_on_tracks(self):
        p = picker(40)
        p.handle(tui.RIGHT, page=10)
        self.assertEqual(p.cursor, 10)
        p.handle(tui.LEFT, page=10)
        self.assertEqual(p.cursor, 0)

    def test_enter_needs_a_track(self):
        p = picker()
        p.handle("n")
        self.assertIsNone(p.handle(tui.ENTER))
        p.handle(tui.SPACE)
        self.assertEqual(p.handle(tui.ENTER), "start")
        self.assertEqual(p.handle(tui.CANCEL), "cancel")

    def test_render_scrolls_and_fits(self):
        p = picker(100)
        for _ in range(60):
            p.handle(tui.DOWN)
        lines = p.lines(80, 24)
        self.assertLessEqual(len(lines), 24)
        self.assertTrue(any("61" in tui._strip(l) and "▸" in tui._strip(l) for l in lines))


class ScriptedRunTest(unittest.TestCase):
    def test_pick_with_scripted_keys(self):
        # untick 0, skip 1, untick 2; END -> last option ("Параллельно"), UP -> "Формат", RIGHT -> opus
        keys = iter([tui.SPACE, tui.DOWN, tui.SPACE, tui.END, tui.UP, tui.RIGHT, tui.ENTER])
        screen = io.StringIO()

        class FakeScreen:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def draw(self, lines):
                screen.write("\n".join(lines))

        with mock.patch.object(tui, "read_key", lambda: next(keys)), \
                mock.patch.object(tui, "_Screen", FakeScreen), mock.patch.object(tui, "_Raw", FakeScreen):
            p = picker()
            result = tui.pick("T", p.items, p.options)
        indices, values = result
        self.assertEqual(indices, [1, 3, 4])               # 0 and 2 unticked
        self.assertEqual(values["format"], "opus")
        self.assertIn("track 4", screen.getvalue())


class CliPickerTest(unittest.TestCase):
    def jobs(self):
        return [{"source": "spotify", "playlist": "Album", "track": {"title": f"t{i}", "artists": ["a"],
                                                                     "duration": 100 + i}} for i in range(3)]

    def test_options_map_to_args(self):
        args = cli.build_parser().parse_args(["x"])
        with mock.patch.object(tui, "pick", return_value=([0, 2], {
                "format": "mp3", "quality": "V0", "jobs": 6, "tags": True, "cover": False, "skip": False})):
            chosen, new = cli.pick_jobs(self.jobs(), args)
        self.assertEqual([j["track"]["title"] for j in chosen], ["t0", "t2"])
        self.assertEqual((new.audio_format, new.audio_quality, new.concurrent_jobs), ("mp3", "V0", 6))
        self.assertTrue(new.no_cover)
        self.assertFalse(new.no_metadata)
        self.assertTrue(new.force_overwrites)
        self.assertEqual(args.audio_format, "best")        # the original args are untouched

    def test_when_it_opens(self):
        jobs = self.jobs()
        args = cli.build_parser().parse_args(["x"])
        with mock.patch.object(tui, "available", return_value=True):
            self.assertTrue(cli.wants_picker(jobs, args))
            self.assertFalse(cli.wants_picker(jobs, cli.build_parser().parse_args(["x", "-y"])))
            self.assertFalse(cli.wants_picker(jobs, cli.build_parser().parse_args(["x", "-I", "1-2"])))
            self.assertTrue(cli.wants_picker(jobs, cli.build_parser().parse_args(["x", "-I", "1", "--select"])))
            self.assertFalse(cli.wants_picker(jobs[:1], args))
        with mock.patch.object(tui, "available", return_value=False):
            self.assertFalse(cli.wants_picker(jobs, args))


class BoardTest(unittest.TestCase):
    def test_states_and_log(self):
        b = tui.Board("T", [f"job {i}" for i in range(30)])
        b.set(0, "done")
        b.set(1, "failed", "HTTP 403")
        b.set(2, "working")
        b.log.write("line one\nline two\r50%")
        text = "\n".join(tui._strip(l) for l in b.lines(100, 20))
        self.assertIn("1 готово", text)
        self.assertIn("1 ошибок", text)
        self.assertIn("HTTP 403", text)
        self.assertIn("line two", text)
        self.assertLessEqual(len(b.lines(100, 20)), 20)

    def test_run_restores_stderr(self):
        import sys
        before = sys.stderr

        class FakeScreen:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def draw(self, lines):
                pass

        b = tui.Board("T", ["a"])
        with mock.patch.object(tui, "_Screen", FakeScreen):
            self.assertEqual(b.run(lambda: print("to log", file=sys.stderr) or 42), 42)
        self.assertIs(sys.stderr, before)
        self.assertIn("to log", list(b.log.lines))


if __name__ == "__main__":
    unittest.main()
