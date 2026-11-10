"""Download only part of a video, without ffmpeg: --section 3:40-5:10

Every YouTube DASH format names its header (initRange) and its index (indexRange: `sidx` in
MP4, `Cues` in WebM). One small request gets both; the index maps time to byte ranges, so
only the fragments covering the section are downloaded (one contiguous range), then glued
behind the header and re-timed to start at zero by the built-in muxer. Cuts land on fragment
boundaries (keyframes, a few seconds apart) - like ffmpeg -c copy, never a re-encode.
"""
import re
import struct
import urllib.parse

from . import mux, net
from .utils import FastDlpError, debug

_HMS = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s?)?$")


def parse_time(text):
    """'220', '220s', '3:40', '1:02:03', '1h2m3s', '3m40s' -> seconds (float)."""
    text = (text or "").strip().lower()
    if not text:
        return None
    if ":" in text:
        parts = [float(p) for p in text.split(":")]
        return sum(v * 60 ** i for i, v in enumerate(reversed(parts)))
    m = _HMS.match(text)
    if not m or not any(m.groups()):
        raise FastDlpError(f"not a time: {text!r} (use 3:40, 220, 3m40s)")
    h, mi, s = m.groups()
    return int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0)


def url_time(url):
    """The t= / start= of a YouTube link, in seconds, or None."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    for key in ("t", "start", "time_continue"):
        if q.get(key):
            return parse_time(q[key][0])
    m = re.search(r"#t=([\dhms:.]+)", url)
    return parse_time(m.group(1)) if m else None


def parse_section(spec, url=None):
    """'3:40-5:10', '3:40-', '-1:00', 't' (from the link's &t=) -> (start, end|None)."""
    spec = spec.strip()
    if spec.lower() in ("t", "url", "link"):
        start = url_time(url or "")
        if start is None:
            raise FastDlpError("--section t: the link has no &t= time")
        return start, None
    if "-" not in spec:
        return parse_time(spec), None
    a, b = spec.split("-", 1)
    start, end = parse_time(a) or 0.0, parse_time(b)
    if end is not None and end <= start:
        raise FastDlpError(f"section ends before it starts: {spec}")
    return start, end


def label(start, end):
    def f(x):
        x = int(x)
        return f"{x // 3600}h{x % 3600 // 60:02d}m{x % 60:02d}s" if x >= 3600 else f"{x // 60}m{x % 60:02d}s"
    return f"{f(start)}-{f(end) if end is not None else 'end'}"


# -- indexes ---------------------------------------------------------------------------------

def mp4_index(index_bytes, index_start):
    """[(t_start, t_end, byte_start, byte_end)] from a sidx box at file offset index_start."""
    size, typ = struct.unpack_from(">I4s", index_bytes, 0)
    if typ != b"sidx":
        raise FastDlpError("no sidx index in this MP4 format")
    version = index_bytes[8]
    timescale = struct.unpack_from(">I", index_bytes, 16)[0]
    if version == 0:
        pts, first = struct.unpack_from(">II", index_bytes, 20)
        p = 28
    else:
        pts, first = struct.unpack_from(">QQ", index_bytes, 20)
        p = 36
    count = struct.unpack_from(">H", index_bytes, p + 2)[0]
    p += 4
    offset = index_start + size + first
    t = pts
    out = []
    for _ in range(count):
        ref, dur, _sap = struct.unpack_from(">III", index_bytes, p)
        p += 12
        length = ref & 0x7FFFFFFF
        out.append((t / timescale, (t + dur) / timescale, offset, offset + length - 1))
        t += dur
        offset += length
    return out


def webm_index(init_bytes, index_bytes, content_length):
    """[(t_start, t_end, byte_start, byte_end)] from WebM Cues (cluster positions)."""
    eid, n1 = mux._read_id(init_bytes, 0)
    hsize, n2 = mux._read_size(init_bytes, n1)
    p = n1 + n2 + hsize
    eid, n1 = mux._read_id(init_bytes, p)
    if eid != mux._SEGMENT:
        raise FastDlpError("no Segment in this WebM header")
    _, n2 = mux._read_size(init_bytes, p + n1)
    data_start = p + n1 + n2
    scale = 1000000
    for cid, s, bs, be in mux._children(init_bytes, data_start, len(init_bytes)):
        if cid == mux._INFO:
            for c2, s2, bs2, be2 in mux._children(init_bytes, bs, be):
                if c2 == mux._TIMECODE_SCALE:
                    scale = mux._uint_value(init_bytes, bs2, be2)
    eid, n1 = mux._read_id(index_bytes, 0)
    if eid != mux._CUES:
        raise FastDlpError("no Cues index in this WebM format")
    _, n2 = mux._read_size(index_bytes, n1)
    points = {}
    for cid, s, bs, be in mux._children(index_bytes, n1 + n2, len(index_bytes)):
        if cid != 0xBB:
            continue
        time = pos = None
        for c2, s2, bs2, be2 in mux._children(index_bytes, bs, be):
            if c2 == 0xB3:
                time = mux._uint_value(index_bytes, bs2, be2)
            elif c2 == 0xB7:
                for c3, s3, bs3, be3 in mux._children(index_bytes, bs2, be2):
                    if c3 == 0xF1:
                        pos = mux._uint_value(index_bytes, bs3, be3)
        if time is not None and pos is not None:
            points[pos] = time
    cues = sorted(points.items())
    out = []
    for i, (pos, time) in enumerate(cues):
        start = data_start + pos
        end = data_start + cues[i + 1][0] - 1 if i + 1 < len(cues) else content_length - 1
        t_end = cues[i + 1][1] * scale / 1e9 if i + 1 < len(cues) else float("inf")
        out.append((time * scale / 1e9, t_end, start, end))
    return out


def cover(fragments, start, end):
    """The contiguous run of fragments covering [start, end): (t0, byte_start, byte_end)."""
    chosen = [f for f in fragments if f[1] > start and (end is None or f[0] < end)]
    if not chosen:
        raise FastDlpError("the section is outside the video")
    return chosen[0][0], chosen[0][2], chosen[-1][3]


def plan(fmt_info, start, end, user_agent):
    """For one format: (header bytes, t0, byte_start, byte_end) of the section."""
    init_end = int(fmt_info["initRange"]["end"])
    idx_start, idx_end = int(fmt_info["indexRange"]["start"]), int(fmt_info["indexRange"]["end"])
    url = fmt_info["url"]
    status, head = net.request(f"{url}&range=0-{idx_end}", headers={"User-Agent": user_agent}, timeout=20)
    if status not in (200, 206) or len(head) < idx_end + 1:
        raise FastDlpError(f"could not read the format's index (HTTP {status})")
    init, index = head[:init_end + 1], head[idx_start:idx_end + 1]
    if index[4:8] == b"sidx":
        frags = mp4_index(index, idx_start)
    else:
        frags = webm_index(init, index, int(fmt_info["contentLength"]))
    t0, b0, b1 = cover(frags, start, end)
    debug(f"section itag {fmt_info['itag']}: {len(frags)} fragments, bytes {b0}-{b1} "
          f"({(b1 - b0 + 1) / max(1, int(fmt_info['contentLength'])):.0%} of the file) from t={t0:.2f}s")
    return init, t0, b0, b1
