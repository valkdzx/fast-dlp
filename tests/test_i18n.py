"""Interface language: detection, every string in both languages, the picker and board in each."""
import os
import re
import unittest
from unittest import mock

from fast_dlp import i18n, tui


class I18nTest(unittest.TestCase):
    def tearDown(self):
        i18n.set_language(None)

    def test_detect(self):
        for raw in ("ru_RU", "ru-RU", "ru_RU.UTF-8", "Russian_Russia", "russian"):
            self.assertEqual(i18n.detect(raw), "ru", raw)
        for raw in ("en_US", "English_United States", "de_DE", "uk_UA", "C", ""):
            self.assertEqual(i18n.detect(raw), "en", raw)

    def test_env_override(self):
        i18n.set_language(None)
        with mock.patch.dict(os.environ, {"FAST_DLP_LANG": "en"}):
            self.assertEqual(i18n.language(), "en")
        i18n.set_language(None)
        with mock.patch.dict(os.environ, {"FAST_DLP_LANG": "ru"}):
            self.assertEqual(i18n.language(), "ru")

    def test_every_string_in_both_languages_with_same_fields(self):
        for key, texts in i18n.STRINGS.items():
            self.assertEqual(set(texts), set(i18n.LANGUAGES), key)
            fields = {lang: set(re.findall(r"\{(\w+)\}", text)) for lang, text in texts.items()}
            self.assertEqual(fields["ru"], fields["en"], key)

    def picker_text(self, lang):
        i18n.set_language(lang)
        options = {"tags": tui.Option(i18n.t("opt_tags"), [True, False], True,
                                      {True: i18n.t("yes"), False: i18n.t("no")})}
        items = [{"label": f"Track {n}", "right": "3:00"} for n in range(1, 40)]
        p = tui.Picker("Album", items, options)
        return "\n".join(tui._strip(line) for line in p.lines(100, 20))

    def test_picker_english(self):
        text = self.picker_text("en")
        for part in ("39 of 39 selected", "Space — tick", "─ Tracks ", "─ Options ", "Tags", "yes", "more"):
            self.assertIn(part, text)
        self.assertIsNone(re.search("[А-Яа-яЁё]", text))

    def test_picker_russian(self):
        text = self.picker_text("ru")
        for part in ("выбрано 39 из 39", "Пробел — отметить", "─ Треки ", "─ Параметры ", "Теги", "да", "ещё"):
            self.assertIn(part, text)

    def test_board_both(self):
        for lang, words in (("en", ("1 done", "1 downloading", "1 queued", "1 failed")),
                            ("ru", ("1 готово", "1 качается", "1 в очереди", "1 ошибок"))):
            i18n.set_language(lang)
            b = tui.Board("List", ["a", "b", "c", "d"])
            b.set(0, "done")
            b.set(1, "working")
            b.set(3, "failed", "x")
            head = tui._strip(b.lines(120, 20)[0])
            for w in words:
                self.assertIn(w, head)


if __name__ == "__main__":
    unittest.main()
