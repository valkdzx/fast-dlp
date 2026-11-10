"""Extra sources besides YouTube and Spotify.

Each module has:
    match(url) -> bool
    expand(url, args) -> (collection_title or None, [job, ...])

A job is either
    {"source": "match", "site", "track": {title, artists, duration, isrc?, album?, ...}, "url"}
        - metadata only: the audio is found on YouTube (like Spotify), or
    {"source": "direct", "site", "id", "title", "author", "audio": bool, "meta": {...},
     "cover_url", "resolve": callable -> spec}
        - spec: {"kind": "file", "url", "headers", "ext"} | {"kind": "hls", "url", "headers"}
                | {"kind": "steamcmd", "app", "item"}
"""
from . import generic, pinterest, pornhub, soundcloud, steam, tiktok, vk, yandex

SOURCES = (yandex, vk, soundcloud, tiktok, pinterest, pornhub, steam, generic)


def find(url):
    for module in SOURCES:
        if module.match(url):
            return module
    return None
