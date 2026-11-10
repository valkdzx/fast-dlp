"""HLS (m3u8) downloader: segments fetched in parallel, AES-128 decrypted, joined in order.

* master playlist -> best variant (or an audio rendition when only audio is wanted)
* fMP4 (EXT-X-MAP init + .m4s) joins into a valid fragmented MP4
* MPEG-TS joins into .ts; with audio_only the audio elementary stream is pulled out
  (MP3 -> .mp3, AAC -> .aac) by ts.py, no re-encoding
* AES-128 keys are fetched once per URI; large encrypted payloads are decrypted in a
  process pool (pure-Python AES is ~1.6 MiB/s per core)
"""
import os
import re
import sys
import time
import urllib.parse
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

from . import net, ts
from .aes import AES128
from .utils import FastDlpError, debug, human_size

_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')
PARALLEL_DECRYPT_BYTES = 2 << 20


def _attrs(text):
    return {k: v.strip('"') for k, v in _ATTR.findall(text)}


def _get(url, headers, binary=False):
    status, body = net.request(url, headers=headers, timeout=30)
    if status != 200:
        raise FastDlpError(f"HLS: {url.split('?')[0]} answered HTTP {status}")
    return body if binary else body.decode("utf-8", "replace")


def parse(text, base):
    """Return ('master', variants, audios) or ('media', segments, init)."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines or not lines[0].startswith("#EXTM3U"):
        raise FastDlpError("not an HLS playlist")
    if any(l.startswith("#EXT-X-STREAM-INF") for l in lines):
        variants, audios = [], []
        for i, l in enumerate(lines):
            if l.startswith("#EXT-X-STREAM-INF"):
                a = _attrs(l.split(":", 1)[1])
                uri = next((x for x in lines[i + 1:] if not x.startswith("#")), None)
                if uri:
                    variants.append(dict(a, uri=urllib.parse.urljoin(base, uri)))
            elif l.startswith("#EXT-X-MEDIA") and "TYPE=AUDIO" in l:
                a = _attrs(l.split(":", 1)[1])
                if a.get("URI"):
                    audios.append(dict(a, uri=urllib.parse.urljoin(base, a["URI"])))
        return "master", variants, audios
    segments, key, init, seq, duration = [], None, None, 0, 0.0
    for l in lines:
        if l.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            seq = int(l.split(":", 1)[1])
        elif l.startswith("#EXT-X-KEY:"):
            a = _attrs(l.split(":", 1)[1])
            method = a.get("METHOD", "NONE")
            if method == "NONE":
                key = None
            elif method == "AES-128":
                key = {"uri": urllib.parse.urljoin(base, a["URI"]), "iv": a.get("IV")}
            else:
                raise FastDlpError(f"HLS encryption {method} is not supported (DRM)")
        elif l.startswith("#EXT-X-MAP:"):
            init = urllib.parse.urljoin(base, _attrs(l.split(":", 1)[1])["URI"])
        elif l.startswith("#EXTINF:"):
            duration = float(l.split(":", 1)[1].split(",")[0] or 0)
        elif not l.startswith("#"):
            segments.append({"uri": urllib.parse.urljoin(base, l), "key": key, "seq": seq,
                             "duration": duration})
            seq += 1
    return "media", segments, init


def _strip_id3(data):
    if data[:3] != b"ID3" or len(data) < 10:
        return data
    size = 0
    for b in data[6:10]:
        size = (size << 7) | (b & 0x7F)
    return data[10 + size + (10 if data[5] & 0x10 else 0):]


def _audio_kind(data):
    """'aac' (ADTS) / 'mp3' for raw audio segments, possibly behind an ID3 tag; else None."""
    body = _strip_id3(data)
    if body[:2] in (b"\xff\xf1", b"\xff\xf9"):
        return "aac"
    if len(body) > 1 and body[0] == 0xFF and (body[1] & 0xE0) == 0xE0:
        return "mp3"
    return None


def _bandwidth(v):
    return int(v.get("BANDWIDTH") or v.get("AVERAGE-BANDWIDTH") or 0)


def _height(v):
    try:
        return int(v.get("RESOLUTION", "0x0").split("x")[-1])
    except ValueError:
        return 0


def choose(url, headers, audio_only=False, max_height=None, prefer_codec=None):
    """Master playlist -> [media playlist URL, optional separate audio URL].
    A media playlist URL comes back as [url]."""
    text = _get(url, headers)
    kind, variants, audios = parse(text, url)
    if kind != "master":
        return [url]
    if audio_only:
        if audios:
            a = max(audios, key=lambda x: (x.get("DEFAULT") == "YES", x.get("NAME", "")))
            debug(f"HLS audio rendition: {a.get('NAME') or a.get('GROUP-ID')}")
            return [a["uri"]]
        # No separate audio: audio-only variants (no RESOLUTION) if any, the best of them.
        pool = [v for v in variants if "RESOLUTION" not in v] or variants
        return [max(pool, key=_bandwidth)["uri"]]
    pool = variants
    if max_height:
        pool = [v for v in pool if _height(v) <= max_height] or pool
    if prefer_codec:
        pool = [v for v in pool if v.get("CODECS", "").startswith(prefer_codec)] or pool
    chosen = max(pool, key=lambda v: (_height(v), _bandwidth(v)))
    debug(f"HLS variant: {chosen.get('RESOLUTION', '')} {chosen.get('CODECS', '')} {_bandwidth(chosen)} bps")
    group = chosen.get("AUDIO")
    if group:
        same = [a for a in audios if a.get("GROUP-ID") == group]
        if same:
            a = max(same, key=lambda x: (x.get("DEFAULT") == "YES", x.get("NAME", "")))
            return [chosen["uri"], a["uri"]]
    return [chosen["uri"]]


def resolve(url, headers, audio_only=False, max_height=None):
    """Follow a master playlist to (media_url, segments, init) - first chosen playlist only."""
    media = choose(url, headers, audio_only, max_height)[0]
    kind, a, b = parse(_get(media, headers), media)
    return media, a, b


def _decrypt_job(args):
    key, iv, data = args
    return AES128(key).decrypt_cbc(data, iv)


def download(url, out_base, headers=None, audio_only=False, max_height=None, connections=16,
             show_progress=True, ffmpeg=None, prefer_codec=None):
    """Download an HLS stream (video + its separate audio when there is one, joined);
    returns the written path."""
    headers = dict(headers or {})
    headers.setdefault("User-Agent", net.BROWSER_UA)
    t0 = time.time()
    playlists = choose(url, headers, audio_only, max_height, prefer_codec)
    if len(playlists) == 1:
        path = _download_media(playlists[0], out_base, headers, audio_only, connections, show_progress)
    else:
        share = max(4, connections // 2)
        with ThreadPoolExecutor(2) as pool:
            v = pool.submit(_download_media, playlists[0], out_base + ".video", headers, False, share, False)
            a = pool.submit(_download_media, playlists[1], out_base + ".audio", headers, True, share, False)
            video, audio = v.result(), a.result()
        path = join(video, audio, out_base, ffmpeg)
    size = os.path.getsize(path) if os.path.isfile(path) else 0
    print(f"[hls] {human_size(size)} in {time.time() - t0:.1f}s", file=sys.stderr)
    return path


def join(video, audio, out_base, ffmpeg=None):
    """Video + audio files from HLS into one: fMP4 with the built-in muxer, TS with ffmpeg."""
    from . import mux
    try:
        out = out_base + ".mp4"
        mux.merge(video, audio, out)
        os.remove(video)
        os.remove(audio)
        return out
    except mux.MuxError as e:
        debug(f"built-in join refused ({e})")
    if ffmpeg:
        import subprocess
        out = out_base + ".mp4"
        proc = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-nostdin", "-i", video, "-i", audio,
                               "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", out], capture_output=True, text=True)
        if proc.returncode == 0:
            os.remove(video)
            os.remove(audio)
            return out
        debug(f"ffmpeg join failed: {proc.stderr.strip()[-300:]}")
    print(f"note: video and audio kept as separate files (MPEG-TS needs ffmpeg to join): "
          f"{os.path.basename(video)}, {os.path.basename(audio)}", file=sys.stderr)
    return video


def _download_media(media_url, out_base, headers, audio_only, connections, show_progress):
    """One media playlist (no master) -> file. Returns the path."""
    kind, segments, init = parse(_get(media_url, headers), media_url)
    if kind != "media" or not segments:
        raise FastDlpError("HLS playlist has no segments")
    keys = {}
    for s in segments:
        if s["key"] and s["key"]["uri"] not in keys:
            keys[s["key"]["uri"]] = _get(s["key"]["uri"], headers, binary=True)

    done = [0]
    results = [None] * len(segments)

    def fetch(i):
        for attempt in range(4):
            status, body = net.request(segments[i]["uri"], headers=headers, timeout=30)
            if status == 200:
                results[i] = body
                done[0] += len(body)
                if show_progress and sys.stderr.isatty():
                    sys.stderr.write(f"\r[hls] {sum(r is not None for r in results)}/{len(segments)} "
                                     f"segments, {human_size(done[0])}   ")
                return
            time.sleep(0.3 * (attempt + 1))
        raise FastDlpError(f"HLS segment {i} failed (HTTP {status})")

    with ThreadPoolExecutor(max(1, min(connections, len(segments)))) as pool:
        list(pool.map(fetch, range(len(segments))))
    init_bytes = _get(init, headers, binary=True) if init else b""

    # Decrypt: IV is explicit or the media sequence number.
    jobs = []
    for i, s in enumerate(segments):
        if s["key"]:
            iv = s["key"]["iv"]
            iv = bytes.fromhex(iv[2:].zfill(32)) if iv else s["seq"].to_bytes(16, "big")
            jobs.append((i, (keys[s["key"]["uri"]], iv, results[i])))
    if jobs:
        t = time.time()
        total = sum(len(j[1][2]) for j in jobs)
        if total >= PARALLEL_DECRYPT_BYTES and len(jobs) > 1:
            with ProcessPoolExecutor(min(os.cpu_count() or 4, len(jobs))) as pp:
                for (i, _), plain in zip(jobs, pp.map(_decrypt_job, [j[1] for j in jobs])):
                    results[i] = plain
        else:
            for i, job in jobs:
                results[i] = _decrypt_job(job)
        debug(f"HLS: decrypted {len(jobs)} segments ({human_size(total)}) in {time.time() - t:.2f}s")

    first = init_bytes or results[0]
    if init_bytes or first[4:8] in (b"ftyp", b"styp", b"moof"):
        ext = "m4a" if audio_only else "mp4"
        payload_parts = [init_bytes] + results
    elif first[:1] == b"\x47":
        joined = b"".join(results)
        kind, audio = ts.audio(joined) if audio_only else (None, b"")
        if audio:
            ext, payload_parts = kind, [audio]
        else:
            ext, payload_parts = "ts", [joined]
    elif _audio_kind(first):
        # "Packed audio" (ID3 timestamp tag + raw frames per segment): keep the frames only
        ext = _audio_kind(first)
        payload_parts = [_strip_id3(r) for r in results]
    else:
        ext, payload_parts = "bin", results
    path = f"{out_base}.{ext}"
    with open(path + ".part", "wb") as fh:
        for part in payload_parts:
            fh.write(part)
    os.replace(path + ".part", path)
    if show_progress and sys.stderr.isatty():
        sys.stderr.write("\n")
    debug(f"[hls] {len(segments)} segments -> {os.path.basename(path)}")
    return path


# -- live ----------------------------------------------------------------------------------

def record_live(url, out_base, headers=None, audio_only=False, max_height=None, duration=None,
                from_start=False, ffmpeg=None, show_progress=True):
    """Record a live HLS stream until it ends, `duration` seconds pass, or Ctrl+C.

    from_start: begin at the oldest segment still in the DVR window (YouTube keeps a few
    hours), else at the live edge. Segments are appended in order as they appear; video and
    audio (separate on YouTube) are recorded side by side and joined at the end."""
    headers = dict(headers or {})
    headers.setdefault("User-Agent", net.BROWSER_UA)
    playlists = choose(url, headers, audio_only, max_height)
    names = [out_base + (".video" if len(playlists) > 1 and i == 0 else ".audio" if len(playlists) > 1 else "")
             for i in range(len(playlists))]
    state = [{"seq": None, "fh": None, "path": None, "bytes": 0, "init": False} for _ in playlists]
    t0 = time.time()
    stop = False
    ended = False
    try:
        while not stop:
            waits = []
            for i, media in enumerate(playlists):
                kind, segs, init = parse(_get(media, headers), media)
                waits.append(segs[-1]["duration"] if segs else 5.0)
                st = state[i]
                if st["seq"] is None:
                    start = 0 if from_start else max(0, len(segs) - 3)
                    st["seq"] = segs[start]["seq"] - 1 if segs else -1
                new = [s for s in segs if s["seq"] > st["seq"]]
                if new and st["fh"] is None:
                    first = net.request(new[0]["uri"], headers=headers, timeout=30)[1]
                    ext = ("m4a" if audio_only or names[i].endswith(".audio") else "mp4") if (init or first[4:8] in (b"ftyp", b"styp", b"moof")) else "ts"
                    st["path"] = f"{names[i]}.{ext}"
                    st["fh"] = open(st["path"], "wb")
                    if init:
                        st["fh"].write(_get(init, headers, binary=True))
                with ThreadPoolExecutor(min(8, max(1, len(new)))) as pool:
                    bodies = list(pool.map(lambda s: net.request(s["uri"], headers=headers, timeout=30), new))
                for s, (status, body) in zip(new, bodies):
                    if status != 200:
                        debug(f"live segment {s['seq']} answered {status}; skipped")
                        continue
                    st["fh"].write(body)
                    st["bytes"] += len(body)
                    st["seq"] = s["seq"]
                if "#EXT-X-ENDLIST" in _get(media, headers):
                    ended = True
            if show_progress and sys.stderr.isatty():
                sys.stderr.write(f"\r[live] recording {time.time() - t0:5.0f}s, "
                                 f"{human_size(sum(s['bytes'] for s in state))}  (Ctrl+C to stop)   ")
            if ended or (duration and time.time() - t0 >= duration):
                break
            time.sleep(max(1.0, min(waits) if waits else 5.0))
    except KeyboardInterrupt:
        stop = True
    finally:
        for st in state:
            if st["fh"]:
                st["fh"].close()
        if show_progress and sys.stderr.isatty():
            sys.stderr.write("\n")
    paths = [s["path"] for s in state if s["path"]]
    if not paths:
        raise FastDlpError("the live stream produced no segments")
    print(f"[live] stopped after {time.time() - t0:.0f}s ({'ended' if ended else 'interrupted' if stop else 'duration reached'})",
          file=sys.stderr)
    if len(paths) == 2:
        if audio_only:
            return paths[1]
        return join(paths[0], paths[1], out_base, ffmpeg)
    path = paths[0]
    if audio_only and path.endswith(".ts"):
        with open(path, "rb") as fh:
            kind, audio = ts.audio(fh.read())
        if audio:
            out = f"{out_base}.{kind}"
            with open(out, "wb") as fh:
                fh.write(audio)
            os.remove(path)
            return out
    final = f"{out_base}{os.path.splitext(path)[1]}"
    if path != final:
        os.replace(path, final)
    return final
