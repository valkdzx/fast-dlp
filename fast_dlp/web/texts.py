"""Server messages of the web page in Russian and English.

The page sends its language in the X-Lang header (ru / en); without it the browser's
Accept-Language decides. Errors raised deeper (extractor, sources) are English already.
"""
from ..utils import FastDlpError

LANGUAGES = ("ru", "en")

MESSAGES = {
    "empty": {"ru": "вставьте ссылку или напишите, что искать",
              "en": "paste a link or type what to search for"},
    "nothing_found": {"ru": "ничего не нашлось по запросу «{q}»", "en": "nothing found for “{q}”"},
    "unsupported": {"ru": "эту ссылку fast-dlp не понимает: YouTube, Spotify, Яндекс Музыка, VK, SoundCloud, "
                          "TikTok, Pinterest, Pornhub или прямая ссылка на файл",
                    "en": "fast-dlp does not know this link: YouTube, Spotify, Yandex Music, VK, SoundCloud, "
                          "TikTok, Pinterest, Pornhub or a direct link to a file"},
    "empty_link": {"ru": "по ссылке нечего скачать", "en": "nothing to download at this link"},
    "pick_entry": {"ru": "это список: выберите, что из него скачать", "en": "this is a list: pick what to download"},
    "no_entry": {"ru": "нет такого пункта в списке", "en": "no such item in the list"},
    "not_youtube": {"ru": "это не ссылка на видео YouTube", "en": "not a YouTube video link"},
    "steam": {"ru": "Steam Workshop качается через steamcmd: это только в командной строке",
              "en": "Steam Workshop items download through steamcmd: command line only"},
    "hls_empty": {"ru": "в HLS-плейлисте нет сегментов", "en": "the HLS playlist has no segments"},
    "hls_segment": {"ru": "HLS: сегмент {i} не отдался (HTTP {status})",
                    "en": "HLS: segment {i} failed (HTTP {status})"},
    "mp3_ffmpeg": {"ru": "для MP3 нужен ffmpeg на сервере", "en": "MP3 needs ffmpeg on the server"},
    "join_ffmpeg": {"ru": "здесь видео и звук идут отдельно: для склейки нужен ffmpeg на сервере",
                    "en": "video and audio come apart here: joining them needs ffmpeg on the server"},
    "bad_time": {"ru": "время отрывка: 3:40, 220 или 3m40s", "en": "section times: 3:40, 220 or 3m40s"},
    "time_order": {"ru": "конец отрывка раньше начала", "en": "the section ends before it starts"},
    "no_job": {"ru": "нет такой загрузки (ссылка одноразовая и живёт час)",
               "en": "no such download (links are one-time and live an hour)"},
    "delivered": {"ru": "файл уже доставлен; ссылка одноразовая", "en": "already delivered; the link is one-time"},
    "busy": {"ru": "этот файл уже передаётся", "en": "this file is being sent already"},
    "bad_json": {"ru": "запрос не в JSON", "en": "the request is not JSON"},
    "network": {"ru": "сайт не ответил как надо: {error}", "en": "the site did not answer properly: {error}"},
    "internal": {"ru": "ошибка на сервере: {error}", "en": "server error: {error}"},
}


class WebError(FastDlpError):
    """An error shown on the page, in the page's language."""

    def __init__(self, key, **values):
        self.key, self.values = key, values
        super().__init__(MESSAGES[key]["en"].format(**values))

    def text(self, lang):
        return MESSAGES[self.key][lang if lang in LANGUAGES else "en"].format(**self.values)


def message(error, lang):
    return error.text(lang) if isinstance(error, WebError) else str(error)


def language(headers):
    """'ru' or 'en' for a request: the page's X-Lang, else the browser's first language."""
    forced = (headers.get("X-Lang") or "").strip().lower()
    if forced in LANGUAGES:
        return forced
    first = (headers.get("Accept-Language") or "").split(",")[0].strip().lower()
    return "ru" if first.startswith("ru") else "en"
