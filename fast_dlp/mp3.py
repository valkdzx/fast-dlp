"""MP3 output: ffmpeg (libmp3lame) encodes, fast-dlp writes the ID3 tag itself.

YouTube serves no MP3, so this is a transcode, not a remux - the one step that needs
ffmpeg. The tag is ID3v2.3 (what Windows Explorer and most players read best), with
UTF-16 text and the cover as an APIC front-cover frame.
"""
import os
import struct
import subprocess

from .utils import FastDlpError

TEXT_FRAMES = (("title", "TIT2"), ("artist", "TPE1"), ("album", "TALB"),
               ("album_artist", "TPE2"), ("track", "TRCK"))


def _syncsafe(n):
    return bytes(((n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F))


def _frame(fid, payload):
    return fid.encode("ascii") + struct.pack(">I", len(payload)) + b"\0\0" + payload


def _text(value):
    return b"\x01" + str(value).encode("utf-16")          # encoding 1: UTF-16 with BOM


def id3v23(meta):
    """An ID3v2.3 tag (bytes) for meta; empty bytes when there is nothing to write."""
    frames = b""
    for key, fid in TEXT_FRAMES:
        if meta.get(key):
            frames += _frame(fid, _text(meta[key]))
    if meta.get("date"):
        frames += _frame("TYER", _text(str(meta["date"])[:4]))
    if meta.get("comment"):
        # encoding, language, empty description (BOM + UTF-16 terminator), then the text
        frames += _frame("COMM", b"\x01" + b"eng" + "".encode("utf-16") + b"\0\0" +
                         str(meta["comment"]).encode("utf-16"))
    if meta.get("lyrics"):
        # USLT holds the LRC text when synced: what MusicBee, Poweramp, foobar2000 and others read
        frames += _frame("USLT", b"\x01" + b"XXX" + "".encode("utf-16") + b"\0\0" + str(meta["lyrics"]).encode("utf-16"))
    if meta.get("replaygain"):
        frames += _frame("TXXX", b"\x01" + "REPLAYGAIN_TRACK_GAIN".encode("utf-16") + b"\0\0" +
                         str(meta["replaygain"]).encode("utf-16"))
    frames += _frame("TSSE", _text("fast-dlp"))
    if meta.get("cover"):
        mime = (meta.get("cover_mime") or "image/jpeg").encode("ascii")
        frames += _frame("APIC", b"\x00" + mime + b"\x00" + b"\x03" + b"\x00" + meta["cover"])
    if not frames:
        return b""
    return b"ID3\x03\x00\x00" + _syncsafe(len(frames)) + frames


def _existing_tag_size(head):
    if head[:3] != b"ID3" or len(head) < 10:
        return 0
    size = 0
    for b in head[6:10]:
        size = (size << 7) | (b & 0x7F)
    return 10 + size + (10 if head[5] & 0x10 else 0)


def tag_file(path, meta):
    """Replace any ID3v2 tag at the start of an MP3 file with ours."""
    if not meta:
        return
    tag = id3v23(meta)
    with open(path, "rb") as src:
        skip = _existing_tag_size(src.read(10))
        src.seek(skip)
        with open(path + ".tag", "wb") as out:
            out.write(tag)
            while True:
                block = src.read(4 << 20)
                if not block:
                    break
                out.write(block)
    os.replace(path + ".tag", path)


def encode(ffmpeg, src, dst, quality="320k", meta=None):
    """Transcode src's first audio stream to MP3 at dst, then prepend our ID3 tag."""
    if not ffmpeg:
        raise FastDlpError("MP3 needs ffmpeg (YouTube serves no MP3, so it is a transcode): "
                           "install it (winget install Gyan.FFmpeg) or pass --ffmpeg")
    tmp = dst + ".enc.mp3"
    rate = ["-q:a", quality[1:]] if quality.upper().startswith("V") else ["-b:a", quality]
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-nostdin", "-i", src, "-map", "0:a:0", "-vn",
           "-c:a", "libmp3lame", *rate, "-map_metadata", "-1", "-id3v2_version", "0",
           "-write_id3v1", "0", tmp]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise FastDlpError(f"ffmpeg MP3 encode failed: {proc.stderr.strip()[-400:]}")
    tag = id3v23(meta or {})
    try:
        with open(dst + ".part", "wb") as out, open(tmp, "rb") as audio:
            out.write(tag)
            while True:
                block = audio.read(4 << 20)
                if not block:
                    break
                out.write(block)
        os.replace(dst + ".part", dst)
    finally:
        for p in (tmp, dst + ".part"):
            if os.path.exists(p):
                os.remove(p)
