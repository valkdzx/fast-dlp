"""Interface language of the terminal picker and the download board.

Russian on a Russian system, English everywhere else. Windows: the user's display language
(what Settings > Language shows); elsewhere LC_ALL / LC_MESSAGES / LANG. FAST_DLP_LANG or
--lang ru|en override it. Log lines ([download], [done]...) stay English on purpose: they
are for scripts and bug reports.
"""
import locale
import os

LANGUAGES = ("ru", "en")

STRINGS = {
    # picker
    "chosen": {"ru": "выбрано {n} из {total}", "en": "{n} of {total} selected"},
    "keys": {"ru": "Пробел — отметить   ↑↓ — ходить   ←→ — менять параметр / листать   "
                   "A все  N ничего  I инвертировать   Enter — скачать   Esc — отмена",
             "en": "Space — tick   ↑↓ — move   ←→ — change option / page   "
                   "A all  N none  I invert   Enter — download   Esc — cancel"},
    "tracks": {"ru": "Треки", "en": "Tracks"},
    "options": {"ru": "Параметры", "en": "Options"},
    "none_chosen": {"ru": "ничего не выбрано — отметьте хотя бы один трек",
                    "en": "nothing selected — tick at least one track"},
    "above": {"ru": "↑ {n} выше", "en": "↑ {n} above"},
    "below": {"ru": "↓ ещё {n}", "en": "↓ {n} more"},
    # board
    "n_done": {"ru": "{n} готово", "en": "{n} done"},
    "n_working": {"ru": "{n} качается", "en": "{n} downloading"},
    "n_queued": {"ru": "{n} в очереди", "en": "{n} queued"},
    "n_skipped": {"ru": "{n} уже было", "en": "{n} already there"},
    "n_failed": {"ru": "{n} ошибок", "en": "{n} failed"},
    "seconds": {"ru": "{n} с", "en": "{n}s"},
    # options
    "opt_format": {"ru": "Формат", "en": "Format"},
    "opt_audio_format": {"ru": "Формат звука", "en": "Audio format"},
    "opt_video_quality": {"ru": "Качество видео", "en": "Video quality"},
    "opt_mp3_quality": {"ru": "Качество MP3", "en": "MP3 quality"},
    "opt_youtube_video": {"ru": "Видео с YouTube", "en": "YouTube videos"},
    "opt_parallel": {"ru": "Параллельно", "en": "In parallel"},
    "opt_tags": {"ru": "Теги", "en": "Tags"},
    "opt_cover": {"ru": "Обложка", "en": "Cover"},
    "opt_existing": {"ru": "Уже скачанные", "en": "Already downloaded"},
    # option values
    "yes": {"ru": "да", "en": "yes"},
    "no": {"ru": "нет", "en": "no"},
    "as_is": {"ru": "как есть (лучшее)", "en": "as is (best)"},
    "video_audio": {"ru": "видео + звук", "en": "video + audio"},
    "audio_only": {"ru": "только звук", "en": "audio only"},
    "audio_only_best": {"ru": "только звук (лучший)", "en": "audio only (best)"},
    "audio_opus": {"ru": "звук opus", "en": "opus audio"},
    "audio_m4a": {"ru": "звук m4a", "en": "m4a audio"},
    "best": {"ru": "лучшее", "en": "best"},
    "up_to": {"ru": "до {h}p", "en": "up to {h}p"},
    "skip": {"ru": "пропускать", "en": "skip"},
    "redownload": {"ru": "качать заново", "en": "download again"},
    # titles
    "search": {"ru": "Поиск «{q}»", "en": "Search “{q}”"},
    "quoted": {"ru": "«{s}»", "en": "“{s}”"},
    "site_yandex": {"ru": "Яндекс Музыка", "en": "Yandex Music"},
    "label_yandex": {"ru": "Яндекс", "en": "Yandex"},
}

_lang = None


def _system():
    """The raw language tag of the system, e.g. 'ru_RU' or 'English_United States'."""
    if os.name == "nt":
        try:
            import ctypes
            ui = ctypes.windll.kernel32.GetUserDefaultUILanguage()       # display language, a LANGID
            name = locale.windows_locale.get(ui)
            if name:
                return name
            return "ru" if ui & 0x3FF == 0x19 else "en"                # LANG_RUSSIAN
        except (OSError, AttributeError):
            pass
    for var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        value = os.environ.get(var)
        if value and value not in ("C", "POSIX"):
            return value
    try:
        return locale.getlocale()[0] or ""
    except ValueError:
        return ""


def detect(raw=None):
    code = (raw if raw is not None else _system()).strip().lower().replace("-", "_")
    return "ru" if code.startswith(("ru", "russian")) else "en"


def set_language(code):
    """'ru', 'en' or 'auto' (None)."""
    global _lang
    _lang = code if code in LANGUAGES else None


def language():
    global _lang
    if _lang is None:
        forced = os.environ.get("FAST_DLP_LANG", "").lower()
        _lang = forced if forced in LANGUAGES else detect()
    return _lang


def t(key, **values):
    text = STRINGS[key][language()]
    return text.format(**values) if values else text
