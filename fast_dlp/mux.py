"""Built-in remuxer and tagger: no ffmpeg.

YouTube DASH streams are already fragmented, so muxing is pure copying:

* WebM (+ WebM): Tracks combined (audio renumbered to track 2), clusters interleaved by
  timestamp, each audio block's one-byte track number patched in place, fresh
  Cues/SeekHead written so the result seeks. Metadata goes into Matroska Tags, the cover
  into an attachment.
* MP4 (+ M4A), fragmented: one moov with every trak/trex (track_IDs renumbered), then
  moof+mdat pairs interleaved by decode time with tfhd/mfhd patched in place. Metadata
  goes into moov/udta/meta/ilst (iTunes atoms), the cover into covr.

A single stream goes through the same writer when it only needs tags. Media payload is
never parsed beyond block headers, so the cost is one sequential write.

meta = {"title", "artist", "album", "album_artist", "date", "track", "comment",
        "cover": bytes, "cover_mime": "image/jpeg"|"image/png"}  (all optional)
"""
import os
import struct

COPY_BUF = 8 * 1024 * 1024


class MuxError(Exception):
    pass


def can_mux(video_ext, audio_ext):
    return (video_ext, audio_ext) in (("webm", "webm"), ("mp4", "m4a"), ("mp4", "mp4"))


def can_tag(ext):
    return ext in ("webm", "mkv", "mp4", "m4a")


def _kind(path):
    with open(path, "rb") as fh:
        head = fh.read(8)
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    if head[4:8] in (b"ftyp", b"styp"):
        return "mp4"
    raise MuxError("unknown container")


def merge(video_path, audio_path, out_path, meta=None, extras=None):
    """Video + audio into out_path (same container family), with optional tags.
    extras: {"chapters": [(start_s, title)], "duration": s, "subs": [(lang, name, cues)]}"""
    kinds = {_kind(video_path), _kind(audio_path)}
    if len(kinds) != 1:
        raise MuxError("mixed containers")
    _write_atomic(kinds.pop(), [video_path, audio_path], out_path, meta, extras)


def tag(path, meta, extras=None):
    """Rewrite one file in place with tags (and cover, chapters, subtitles)."""
    if not meta and not extras:
        return
    kind = _kind(path)
    if kind == "mp4" and not extras and not _fragmented(path):
        _tag_progressive(path, meta)
        return
    _write_atomic(kind, [path], path, meta, extras)


def _top_boxes(fh, size):
    """[(type, start, length)] of the top-level boxes, reading only their headers."""
    out, pos = [], 0
    while pos + 8 <= size:
        fh.seek(pos)
        hdr = fh.read(16)
        n, typ = struct.unpack_from(">I4s", hdr, 0)
        if n == 1:
            n = struct.unpack_from(">Q", hdr, 8)[0]
        elif n == 0:
            n = size - pos
        if n < 8:
            raise MuxError("bad box size")
        out.append((typ, pos, n))
        pos += n
    return out


def _fragmented(path):
    with open(path, "rb") as fh:
        return any(t == b"moof" for t, _, _ in _top_boxes(fh, os.fstat(fh.fileno()).st_size))


def _tag_progressive(path, meta):
    with open(path, "r+b") as fh:
        if not tag_open(fh, meta):
            raise MuxError("not an ordinary MP4")


def tag_open(fh, meta):
    """Tags for an ordinary (non-fragmented) MP4, through an already open r+b handle, without
    touching its media data. False when the file is not one (fragmented, WebM...).

    Its sample tables point at absolute offsets in mdat, so mdat must not move. When moov is
    the last box it is simply replaced; otherwise the old moov becomes a `free` box and the
    new one is appended. Either way only the moov (a few MiB) is written, not the file."""
    size = os.fstat(fh.fileno()).st_size
    fh.seek(0)
    if fh.read(8)[4:8] not in (b"ftyp", b"styp"):
        return False
    boxes = _top_boxes(fh, size)
    found = [(s, n) for t, s, n in boxes if t == b"moov"]
    if len(found) != 1 or any(t == b"moof" for t, _, _ in boxes):
        return False
    start, length = found[0]
    fh.seek(start)
    moov = fh.read(length)
    body = b"".join(moov[s:e] for t, s, h, e in _boxes(moov, 8, len(moov)) if t != b"udta")
    new = _box(b"moov", body + _mp4_udta(meta))
    if start + length == size:
        fh.seek(start)
        fh.write(new)
        fh.truncate()
    else:
        fh.seek(start + 4)
        fh.write(b"free")
        fh.seek(size)
        fh.write(new)
    fh.flush()
    return True


def _write_atomic(kind, sources, out_path, meta, extras=None):
    tmp = out_path + ".mux"
    try:
        if kind == "webm":
            _write_webm(sources, tmp, meta, extras or {}, mkv=out_path.lower().endswith(".mkv"))
        else:
            _write_mp4(sources, tmp, meta, extras or {})
        os.replace(tmp, out_path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


class _Copier:
    def __init__(self):
        self.buf = bytearray(COPY_BUF)
        self.view = memoryview(self.buf)

    def copy(self, src, offset, length, dst):
        src.seek(offset)
        while length > 0:
            n = src.readinto(self.view[:min(length, COPY_BUF)])
            if not n:
                raise MuxError("source ended early")
            dst.write(self.view[:n])
            length -= n


# ----------------------------------------------------------------------------- WebM

_EBML = 0x1A45DFA3
_SEGMENT = 0x18538067
_SEEKHEAD = 0x114D9B74
_INFO = 0x1549A966
_TRACKS = 0x1654AE6B
_TRACK_ENTRY = 0xAE
_TRACK_TYPE = 0x83
_CUES = 0x1C53BB6B
_CLUSTER = 0x1F43B675
_TIMESTAMP = 0xE7
_SIMPLE_BLOCK = 0xA3
_BLOCK_GROUP = 0xA0
_BLOCK = 0xA1
_TIMECODE_SCALE = 0x2AD7B1
_DURATION = 0x4489
_TRACK_NUMBER = 0xD7
_TRACK_UID = 0x73C5
_TAGS = 0x1254C367
_ATTACHMENTS = 0x1941A469


def _read_id(b, p):
    first = b[p]
    n = 1 if first & 0x80 else 2 if first & 0x40 else 3 if first & 0x20 else 4 if first & 0x10 else 0
    if not n:
        raise MuxError("bad EBML id")
    return int.from_bytes(b[p:p + n], "big"), n


def _read_size(b, p):
    first = b[p]
    n, mask = 1, 0x80
    while n <= 8 and not first & mask:
        mask >>= 1
        n += 1
    if n > 8:
        raise MuxError("bad EBML size")
    value = first & (mask - 1)
    for i in range(1, n):
        value = (value << 8) | b[p + i]
    unknown = value == (1 << (7 * n)) - 1
    return (None if unknown else value), n


def _children(b, start, end):
    """Yield (id, element_start, body_start, body_end) for elements in b[start:end]."""
    p = start
    while p < end:
        eid, n1 = _read_id(b, p)
        size, n2 = _read_size(b, p + n1)
        body = p + n1 + n2
        if size is None:
            raise MuxError("unknown-size element")
        yield eid, p, body, body + size
        p = body + size


def _id_bytes(eid):
    return eid.to_bytes((eid.bit_length() + 7) // 8, "big")


def _size8(n):
    return b"\x01" + n.to_bytes(7, "big")


def _el(eid, payload):
    return _id_bytes(eid) + _size8(len(payload)) + payload


def _uint(eid, value, width=8):
    return _el(eid, value.to_bytes(width, "big"))


def _str(eid, text):
    return _el(eid, text.encode("utf-8"))


def _uint_value(b, s, e):
    return int.from_bytes(b[s:e], "big") if e > s else 0


def _body(b):
    eid, n1 = _read_id(b, 0)
    _, n2 = _read_size(b, n1)
    return n1 + n2


class _WebmSource:
    def __init__(self, path):
        self.path = path
        self.fh = open(path, "rb")
        size = os.fstat(self.fh.fileno()).st_size
        head = self.fh.read(min(size, 1 << 20))
        eid, n1 = _read_id(head, 0)
        if eid != _EBML:
            raise MuxError("not EBML")
        hsize, n2 = _read_size(head, n1)
        self.ebml_header = head[:n1 + n2 + hsize]
        p = n1 + n2 + hsize
        eid, n1 = _read_id(head, p)
        if eid != _SEGMENT:
            raise MuxError("no Segment")
        seg_size, n2 = _read_size(head, p + n1)
        seg_body = p + n1 + n2
        seg_end = size if seg_size is None else min(size, seg_body + seg_size)
        self.info = self.tracks = None
        self.clusters = []          # (timestamp, offset, length)
        pos = seg_body
        while pos < seg_end:
            self.fh.seek(pos)
            hdr = self.fh.read(16)
            if len(hdr) < 2:
                break
            eid, n1 = _read_id(hdr, 0)
            esize, n2 = _read_size(hdr, n1)
            if esize is None:
                raise MuxError("unknown-size element in Segment")
            total = n1 + n2 + esize
            if eid == _CLUSTER:
                self.fh.seek(pos + n1 + n2)
                first = self.fh.read(16)
                tid, m1 = _read_id(first, 0)
                tsize, m2 = _read_size(first, m1)
                if tid != _TIMESTAMP:
                    raise MuxError("cluster without leading timestamp")
                self.clusters.append((_uint_value(first, m1 + m2, m1 + m2 + tsize), pos, total))
            elif eid in (_INFO, _TRACKS):
                self.fh.seek(pos)
                raw = self.fh.read(total)
                if eid == _INFO:
                    self.info = raw
                else:
                    self.tracks = raw
            pos += total
        if self.info is None or self.tracks is None or not self.clusters:
            raise MuxError(f"{os.path.basename(path)}: missing Info/Tracks/Clusters")

    def info_fields(self):
        b = self.info
        scale, duration = 1000000, None
        for cid, s, bs, be in _children(b, _body(b), len(b)):
            if cid == _TIMECODE_SCALE:
                scale = _uint_value(b, bs, be)
            elif cid == _DURATION:
                duration = struct.unpack(">f" if be - bs == 4 else ">d", b[bs:be])[0]
        return scale, duration

    def track_entries(self):
        b = self.tracks
        return [b[s:e] for cid, s, bs, e in _children(b, _body(b), len(b)) if cid == _TRACK_ENTRY]

    def close(self):
        self.fh.close()


def _track_field(entry, field):
    for cid, s, bs, be in _children(entry, _body(entry), len(entry)):
        if cid == field:
            return _uint_value(entry, bs, be)
    return None


def _renumber_track(entry, number):
    out = bytearray()
    for cid, s, bs, be in _children(entry, _body(entry), len(entry)):
        if cid == _TRACK_NUMBER:
            out += _uint(_TRACK_NUMBER, number, 1)
        elif cid == _TRACK_UID:
            out += _uint(_TRACK_UID, number, 8)
        else:
            out += entry[s:be]
    return _el(_TRACK_ENTRY, bytes(out))


def _patch_cluster_tracks(cluster, number):
    """Rewrite the (one-byte) track number of every block in a cluster, in place."""
    b = cluster
    tag_byte = 0x80 | number

    def patch(body_start):
        if not b[body_start] & 0x80:
            raise MuxError("multi-byte track number")
        b[body_start] = tag_byte

    for cid, s, bs, be in _children(b, _body(b), len(b)):
        if cid == _SIMPLE_BLOCK:
            patch(bs)
        elif cid == _BLOCK_GROUP:
            for gid, gs, gbs, gbe in _children(b, bs, be):
                if gid == _BLOCK:
                    patch(gbs)


def _retime_cluster(cluster, delta):
    """Move a cluster earlier by delta timecode units, in place (same byte width: the new
    value is smaller), so cluster sizes and every precomputed position stay valid."""
    for cid, s, bs, be in _children(cluster, _body(cluster), len(cluster)):
        if cid == _TIMESTAMP:
            value = max(0, _uint_value(cluster, bs, be) - delta)
            cluster[bs:be] = value.to_bytes(be - bs, "big")
            return
    raise MuxError("cluster without a timestamp")


_MKV_TAGS = (("title", "TITLE"), ("artist", "ARTIST"), ("album", "ALBUM"),
             ("album_artist", "ALBUM_ARTIST"), ("date", "DATE"),
             ("track", "PART_NUMBER"), ("comment", "COMMENT"), ("lyrics", "LYRICS"),
             ("replaygain", "REPLAYGAIN_TRACK_GAIN"))


def _webm_tags(meta):
    simple = b""
    for key, name in _MKV_TAGS:
        if meta.get(key):
            simple += _el(0x67C8, _str(0x45A3, name) + _str(0x4487, str(meta[key])))
    if not simple:
        return b""
    targets = _el(0x63C0, _uint(0x68CA, 50, 1))          # TargetTypeValue 50: album/movie level
    return _el(_TAGS, _el(0x7373, targets + simple))


def _webm_attachments(meta):
    if not meta.get("cover"):
        return b""
    mime = meta.get("cover_mime") or "image/jpeg"
    name = "cover.png" if mime == "image/png" else "cover.jpg"
    f = (_str(0x466E, name) + _str(0x4660, mime) + _el(0x465C, meta["cover"]) + _uint(0x46AE, 1))
    return _el(_ATTACHMENTS, _el(0x61A7, f))


_LANG3 = {"en": "eng", "ru": "rus", "de": "deu", "fr": "fra", "es": "spa", "it": "ita", "pt": "por",
          "ja": "jpn", "ko": "kor", "zh": "zho", "uk": "ukr", "pl": "pol", "tr": "tur", "ar": "ara",
          "hi": "hin", "nl": "nld", "sv": "swe", "cs": "ces", "be": "bel", "kk": "kaz", "id": "ind"}


def lang3(code):
    return _LANG3.get((code or "").split("-")[0].lower(), "und")


def _webm_chapters(chapters, duration):
    if not chapters:
        return b""
    atoms = b""
    for i, (start, title) in enumerate(chapters):
        end = chapters[i + 1][0] if i + 1 < len(chapters) else (duration or start + 1)
        display = _el(0x80, _str(0x85, title) + _str(0x437C, "eng"))
        atoms += _el(0xB6, _uint(0x73C4, i + 1) + _uint(0x91, int(start * 1e9)) +
                     _uint(0x92, int(end * 1e9)) + display)
    return _el(0x1043A770, _el(0x45B9, _uint(0x45BC, 1) + atoms))


def _webm_sub_track(number, lang, name, mkv):
    codec = "S_TEXT/UTF8" if mkv else "D_WEBVTT/SUBTITLES"
    body = (_uint(_TRACK_NUMBER, number, 1) + _uint(_TRACK_UID, 1000 + number) + _uint(_TRACK_TYPE, 0x11, 1) +
            _str(0x86, codec) + _str(0x22B59C, lang3(lang)) + _str(0x22B59D, lang) +
            _str(0x536E, name or lang) + _uint(0x88, 0, 1))
    return _el(_TRACK_ENTRY, body)


def _webm_sub_clusters(number, cues):
    """One small cluster per cue: Timestamp + BlockGroup(Block, BlockDuration). Times in ms."""
    out = []
    for start, end, text in cues:
        ts = int(start * 1000)
        block = bytes([0x80 | number]) + b"\x00\x00\x00" + text.encode("utf-8")
        group = _el(_BLOCK_GROUP, _el(_BLOCK, block) + _uint(0x9B, max(1, int((end - start) * 1000))))
        out.append((ts, _el(_CLUSTER, _uint(_TIMESTAMP, ts) + group)))
    return out


def _write_webm(paths, out_path, meta, extras=None, mkv=False):
    extras = extras or {}
    sources = [_WebmSource(p) for p in paths]
    try:
        scales = {s.info_fields()[0] for s in sources}
        if len(scales) != 1:
            raise MuxError("different timecode scales")
        entries = [s.track_entries() for s in sources]
        if any(len(e) != 1 for e in entries):
            raise MuxError("expected one track per file")

        # Info: the first source's, with the longest duration.
        b = sources[0].info
        body = bytearray()
        for cid, s, bs, be in _children(b, _body(b), len(b)):
            if cid != _DURATION:
                body += b[s:be]
        duration = max([d for _, d in (s.info_fields() for s in sources) if d] or [0.0])
        scale = next(iter(scales))
        rebase = int(round((extras.get("rebase") or 0) * 1e9 / scale))       # in timecode units
        if extras.get("section_duration"):
            duration = extras["section_duration"] * 1e9 / scale
        if duration:
            body += _el(_DURATION, struct.pack(">d", duration))
        info = _el(_INFO, bytes(body))

        patch = []                  # per source: new track number if the blocks need it
        track_bytes = b""
        for i, (src_entries, n) in enumerate(zip(entries, range(1, len(sources) + 1))):
            entry = src_entries[0]
            patch.append(n if _track_field(entry, _TRACK_NUMBER) != n else None)
            track_bytes += _renumber_track(entry, n)
        memory = []                           # (timestamp, bytes) clusters built here: subtitles
        for j, (lang, name, cues) in enumerate(extras.get("subs") or []):
            number = len(sources) + j + 1
            track_bytes += _webm_sub_track(number, lang, name, mkv)
            memory += _webm_sub_clusters(number, cues)
        tracks = _el(_TRACKS, track_bytes)
        # Cues point at clusters of the first video track, else of track 1.
        types = [_track_field(e[0], _TRACK_TYPE) for e in entries]
        cue_src = types.index(1) if 1 in types else 0

        tags = _webm_tags(meta or {})
        attachments = _webm_attachments(meta or {})
        chapters = _webm_chapters(extras.get("chapters"), extras.get("duration") or duration / 1000.0)

        merged = sorted([(ts, i, off, ln) for i, s in enumerate(sources) for ts, off, ln in s.clusters] +
                        [(ts, -1 - k, 0, len(data)) for k, (ts, data) in enumerate(memory)])

        def cue_point(ts, pos):
            ts = max(0, ts - rebase)
            return _el(0xBB, _uint(0xB3, ts) + _el(0xB7, _uint(0xF7, cue_src + 1, 1) + _uint(0xF1, pos)))

        n_cues = sum(1 for _, i, _, _ in merged if i == cue_src)
        cues_size = len(_el(_CUES, b"\0" * (len(cue_point(0, 0)) * n_cues)))

        def seek(eid, pos):
            return _el(0x4DBB, _el(0x53AB, _id_bytes(eid)) + _uint(0x53AC, pos))

        listed = [(_INFO, info), (_TRACKS, tracks)]
        if chapters:
            listed.append((0x1043A770, chapters))
        if tags:
            listed.append((_TAGS, tags))
        if attachments:
            listed.append((_ATTACHMENTS, attachments))
        seekhead_size = len(_el(_SEEKHEAD, b"".join(seek(e, 0) for e, _ in listed) + seek(_CUES, 0)))
        positions, pos = {}, seekhead_size
        for eid, data in listed:
            positions[eid] = pos
            pos += len(data)
        positions[_CUES] = pos
        pos += cues_size
        points = bytearray()
        for ts, i, off, ln in merged:
            if i == cue_src:
                points += cue_point(ts, pos)
            pos += ln
        segment_size = pos
        seekhead = _el(_SEEKHEAD, b"".join(seek(e, positions[e]) for e, _ in listed) + seek(_CUES, positions[_CUES]))
        cues = _el(_CUES, bytes(points))
        assert len(seekhead) == seekhead_size and len(cues) == cues_size

        copier = _Copier()
        with open(out_path, "wb") as out:
            out.write(sources[0].ebml_header)
            out.write(_id_bytes(_SEGMENT) + _size8(segment_size))
            out.write(seekhead + b"".join(d for _, d in listed) + cues)
            for ts, i, off, ln in merged:
                if i < 0:
                    out.write(memory[-1 - i][1])
                elif patch[i] is None and not rebase:
                    copier.copy(sources[i].fh, off, ln, out)
                elif patch[i] is None:
                    sources[i].fh.seek(off)
                    cluster = bytearray(sources[i].fh.read(ln))
                    _retime_cluster(cluster, rebase)
                    out.write(cluster)
                else:
                    sources[i].fh.seek(off)
                    cluster = bytearray(sources[i].fh.read(ln))
                    _patch_cluster_tracks(cluster, patch[i])
                    if rebase:
                        _retime_cluster(cluster, rebase)
                    out.write(cluster)
    finally:
        for s in sources:
            s.close()


# ----------------------------------------------------------------------------- MP4

def _boxes(b, start, end):
    """Yield (type, box_start, header_len, box_end)."""
    p = start
    while p + 8 <= end:
        size, typ = struct.unpack_from(">I4s", b, p)
        hdr = 8
        if size == 1:
            size = struct.unpack_from(">Q", b, p + 8)[0]
            hdr = 16
        elif size == 0:
            size = end - p
        if size < hdr:
            raise MuxError("bad box size")
        yield typ, p, hdr, p + size
        p += size


def _box(typ, payload):
    return struct.pack(">I4s", 8 + len(payload), typ) + payload


class _Mp4Source:
    def __init__(self, path):
        self.fh = open(path, "rb")
        size = os.fstat(self.fh.fileno()).st_size
        self.ftyp = self.moov = None
        self.fragments = []     # [moof_bytes, mdat_offset, mdat_length, decode_time]
        pos = 0
        pending_moof = None
        while pos < size:
            self.fh.seek(pos)
            hdr = self.fh.read(16)
            if len(hdr) < 8:
                break
            box_size, typ = struct.unpack_from(">I4s", hdr, 0)
            if box_size == 1:
                box_size = struct.unpack_from(">Q", hdr, 8)[0]
            elif box_size == 0:
                box_size = size - pos
            if typ in (b"ftyp", b"moov", b"moof"):
                self.fh.seek(pos)
                raw = self.fh.read(box_size)
                if typ == b"ftyp":
                    self.ftyp = raw
                elif typ == b"moov":
                    self.moov = raw
                else:
                    pending_moof = raw
            elif typ == b"mdat":
                if pending_moof is None:
                    raise MuxError("mdat without moof")
                self.fragments.append([bytearray(pending_moof), pos, box_size, 0])
                pending_moof = None
            pos += box_size
        if not self.ftyp or not self.moov or not self.fragments:
            raise MuxError("not a fragmented MP4")
        self._parse_moov()
        for frag in self.fragments:
            frag[3] = self._decode_time(frag[0])

    def _parse_moov(self):
        b = self.moov
        self.mvhd = None
        self.traks, self.trexs = [], []
        for typ, s, h, e in _boxes(b, 8, len(b)):
            if typ == b"mvhd":
                self.mvhd = bytearray(b[s:e])
            elif typ == b"trak":
                self.traks.append(bytearray(b[s:e]))
            elif typ == b"mvex":
                for t2, s2, h2, e2 in _boxes(b, s + h, e):
                    if t2 == b"trex":
                        self.trexs.append(bytearray(b[s2:e2]))
        if self.mvhd is None or len(self.traks) != 1 or len(self.trexs) != 1:
            raise MuxError("expected one trak per file")
        self.timescale = self._mdhd_timescale(self.traks[0])

    @staticmethod
    def _find(b, path, start=8, end=None):
        end = len(b) if end is None else end
        for typ, s, h, e in _boxes(b, start, end):
            if typ == path[0]:
                if len(path) == 1:
                    return s, h, e
                return _Mp4Source._find(b, path[1:], s + h, e)
        raise MuxError(f"no {path[0].decode()} box")

    def _mdhd_timescale(self, trak):
        s, h, e = self._find(trak, [b"mdia", b"mdhd"])
        version = trak[s + h]
        return struct.unpack_from(">I", trak, s + h + (20 if version == 1 else 12))[0]

    def _decode_time(self, moof):
        s, h, e = self._find(moof, [b"traf", b"tfdt"])
        version = moof[s + h]
        if version == 1:
            return struct.unpack_from(">Q", moof, s + h + 4)[0]
        return struct.unpack_from(">I", moof, s + h + 4)[0]

    def close(self):
        self.fh.close()


def _set_track_id_tkhd(trak, track_id):
    s, h, e = _Mp4Source._find(trak, [b"tkhd"])
    version = trak[s + h]
    struct.pack_into(">I", trak, s + h + (20 if version == 1 else 12), track_id)


_ILST_TEXT = (("title", b"\xa9nam"), ("artist", b"\xa9ART"), ("album", b"\xa9alb"),
              ("album_artist", b"aART"), ("date", b"\xa9day"), ("comment", b"\xa9cmt"), ("lyrics", b"\xa9lyr"))


def _freeform(name, value):
    """iTunes '----' atom (mean/name/data), how MP4 carries tags like REPLAYGAIN_TRACK_GAIN."""
    return _box(b"----", _box(b"mean", b"\0" * 4 + b"com.apple.iTunes") + _box(b"name", b"\0" * 4 + name.encode()) +
                _box(b"data", struct.pack(">II", 1, 0) + value.encode("utf-8")))


def _ilst_item(name, data_type, payload):
    return _box(name, _box(b"data", struct.pack(">II", data_type, 0) + payload))


def _mp4_udta(meta):
    items = b""
    for key, atom in _ILST_TEXT:
        if meta.get(key):
            items += _ilst_item(atom, 1, str(meta[key]).encode("utf-8"))
    if meta.get("track"):
        try:
            n = int(str(meta["track"]).split("/")[0])
            total = int(str(meta["track"]).split("/")[1]) if "/" in str(meta["track"]) else 0
            items += _ilst_item(b"trkn", 0, struct.pack(">HHHH", 0, n, total, 0))
        except ValueError:
            pass
    items += _ilst_item(b"\xa9too", 1, b"fast-dlp")
    if meta.get("replaygain"):
        items += _freeform("REPLAYGAIN_TRACK_GAIN", str(meta["replaygain"]))
    if meta.get("cover"):
        items += _ilst_item(b"covr", 14 if meta.get("cover_mime") == "image/png" else 13, meta["cover"])
    hdlr = _box(b"hdlr", b"\0" * 4 + b"\0" * 4 + b"mdir" + b"appl" + b"\0" * 8 + b"\0")
    meta_box = _box(b"meta", b"\0" * 4 + hdlr + _box(b"ilst", items))
    return _box(b"udta", meta_box)


def _full(typ, version, flags, payload):
    return _box(typ, struct.pack(">I", (version << 24) | flags) + payload)


def _mdhd_lang(code):
    c = lang3(code)
    return ((ord(c[0]) - 0x60) << 10) | ((ord(c[1]) - 0x60) << 5) | (ord(c[2]) - 0x60)


def _tx3g_trak(track_id, lang, name):
    """A tx3g (3GPP timed text) subtitle trak, empty sample tables (samples live in moof)."""
    tkhd = _full(b"tkhd", 0, 3, b"\0" * 8 + struct.pack(">I", track_id) + b"\0" * 4 + b"\0" * 4 +
                 b"\0" * 8 + struct.pack(">hhh", 0, 0, 0) + b"\0\0" +
                 struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000) + b"\0" * 8)
    mdhd = _full(b"mdhd", 0, 0, b"\0" * 8 + struct.pack(">II", 1000, 0) + struct.pack(">HH", _mdhd_lang(lang), 0))
    hdlr = _full(b"hdlr", 0, 0, b"\0" * 4 + b"sbtl" + b"\0" * 12 + (name or lang).encode("utf-8") + b"\0")
    ftab = _box(b"ftab", struct.pack(">HHB", 1, 1, 5) + b"Serif")
    tx3g = _box(b"tx3g", b"\0" * 6 + struct.pack(">H", 1) + struct.pack(">I", 0) + bytes([1, 0xFF]) +
                b"\0\0\0\0" + b"\0" * 8 + struct.pack(">HHHBB", 0, 0, 1, 0, 18) + b"\xff\xff\xff\xff" + ftab)
    stbl = _box(b"stbl", _full(b"stsd", 0, 0, struct.pack(">I", 1) + tx3g) +
                _full(b"stts", 0, 0, b"\0" * 4) + _full(b"stsc", 0, 0, b"\0" * 4) +
                _full(b"stsz", 0, 0, b"\0" * 8) + _full(b"stco", 0, 0, b"\0" * 4))
    dinf = _box(b"dinf", _full(b"dref", 0, 0, struct.pack(">I", 1) + _full(b"url ", 0, 1, b"")))
    minf = _box(b"minf", _full(b"nmhd", 0, 0, b"") + dinf + stbl)
    trak = _box(b"trak", tkhd + _box(b"mdia", mdhd + hdlr + minf))
    trex = _full(b"trex", 0, 0, struct.pack(">IIIII", track_id, 1, 0, 0, 0))
    return trak, trex


def _tx3g_fragment(track_id, cues, seq):
    """All cues as one fragment at t=0: gaps become empty samples so timing stays exact."""
    samples, t = [], 0
    for start, end, text in cues:
        s, e = int(start * 1000), int(end * 1000)
        if s > t:
            samples.append((s - t, b"\0\0"))
            t = s
        if e > t:
            data = text.encode("utf-8")
            samples.append((e - t, struct.pack(">H", len(data)) + data))
            t = e
    if not samples:
        return None
    payload = b"".join(d for _, d in samples)
    entries = b"".join(struct.pack(">II", dur, len(d)) for dur, d in samples)

    def build(offset):
        trun = _full(b"trun", 0, 0x000301, struct.pack(">Ii", len(samples), offset) + entries)
        traf = _box(b"traf", _full(b"tfhd", 0, 0x020000, struct.pack(">I", track_id)) +
                    _full(b"tfdt", 1, 0, struct.pack(">Q", 0)) + trun)
        return _box(b"moof", _full(b"mfhd", 0, 0, struct.pack(">I", seq)) + traf)

    moof = build(0)
    moof = build(len(moof) + 8)
    return moof + _box(b"mdat", payload)


def _nero_chapters(chapters):
    """Nero 'chpl' chapter list (moov/udta), read by ffmpeg, mpv, VLC, MPC and others."""
    body = struct.pack(">I", 0x01000000) + b"\0\0\0\0" + bytes([min(255, len(chapters))])
    for start, title in chapters[:255]:
        name = title.encode("utf-8")[:255]
        body += struct.pack(">Q", int(start * 10_000_000)) + bytes([len(name)]) + name
    return _box(b"chpl", body)


def _set_durations(mvhd, traks, seconds):
    """A section: movie, track and edit-list durations say the section's length, not the source's."""
    v = mvhd[8]
    ts_off = 8 + 4 + (16 if v == 1 else 8)
    timescale = struct.unpack_from(">I", mvhd, ts_off)[0]
    value = int(seconds * timescale)
    if v == 1:
        struct.pack_into(">Q", mvhd, ts_off + 4, value)
    else:
        struct.pack_into(">I", mvhd, ts_off + 4, value)
    for trak in traks:
        s, h, e = _Mp4Source._find(trak, [b"tkhd"])
        tv = trak[s + h]
        if tv == 1:
            struct.pack_into(">Q", trak, s + h + 4 + 8 + 8 + 4 + 4, value)
        else:
            struct.pack_into(">I", trak, s + h + 4 + 4 + 4 + 4 + 4, value)
        ms, mh, me = _Mp4Source._find(trak, [b"mdia", b"mdhd"])
        mv = trak[ms + mh]
        if mv == 1:
            mts = struct.unpack_from(">I", trak, ms + mh + 4 + 16)[0]
            struct.pack_into(">Q", trak, ms + mh + 4 + 16 + 4, int(seconds * mts))
        else:
            mts = struct.unpack_from(">I", trak, ms + mh + 4 + 8)[0]
            struct.pack_into(">I", trak, ms + mh + 4 + 8 + 4, int(seconds * mts))
        try:
            es, eh, ee = _Mp4Source._find(trak, [b"edts", b"elst"])
        except MuxError:
            continue
        ev = trak[es + eh]
        count = struct.unpack_from(">I", trak, es + eh + 4)[0]
        p = es + eh + 8
        for _ in range(count):
            if ev == 1:
                struct.pack_into(">Q", trak, p, value)
                p += 20
            else:
                struct.pack_into(">I", trak, p, value)
                p += 12


def _write_mp4(paths, out_path, meta, extras=None):
    extras = extras or {}
    sources = [_Mp4Source(p) for p in paths]
    try:
        for i, s in enumerate(sources, 1):
            _set_track_id_tkhd(s.traks[0], i)
            struct.pack_into(">I", s.trexs[0], 12, i)
        subs = extras.get("subs") or []
        sub_traks = [_tx3g_trak(len(sources) + 1 + j, lang, name) for j, (lang, name, _) in enumerate(subs)]
        mvhd = sources[0].mvhd
        struct.pack_into(">I", mvhd, len(mvhd) - 4, len(sources) + len(subs) + 1)       # next_track_ID
        if extras.get("section_duration"):
            _set_durations(mvhd, [s.traks[0] for s in sources], extras["section_duration"])
        moov_body = bytes(mvhd) + b"".join(bytes(s.traks[0]) for s in sources) + b"".join(t for t, _ in sub_traks)
        moov_body += _box(b"mvex", b"".join(bytes(s.trexs[0]) for s in sources) + b"".join(x for _, x in sub_traks))
        udta = b""
        if meta:
            udta = _mp4_udta(meta)[8:]                       # contents of udta (meta box)
        if extras.get("chapters"):
            udta += _nero_chapters(extras["chapters"])
        if udta:
            moov_body += _box(b"udta", udta)
        moov = _box(b"moov", moov_body)

        frags = sorted(((f[3] / s.timescale, i, f) for i, s in enumerate(sources) for f in s.fragments),
                       key=lambda x: (x[0], x[1]))

        copier = _Copier()
        with open(out_path, "wb") as out:
            out.write(sources[0].ftyp)
            out.write(moov)
            for seq, (_, which, (moof, mdat_off, mdat_len, _)) in enumerate(frags, 1):
                ms, mh, me = _Mp4Source._find(moof, [b"mfhd"], 8)
                struct.pack_into(">I", moof, ms + mh + 4, seq)
                ts, th, te = _Mp4Source._find(moof, [b"traf", b"tfhd"], 8)
                flags = int.from_bytes(moof[ts + th + 1:ts + th + 4], "big")
                struct.pack_into(">I", moof, ts + th + 4, which + 1)
                if extras.get("rebase"):
                    ds, dh, de = _Mp4Source._find(moof, [b"traf", b"tfdt"], 8)
                    shift = int(round(extras["rebase"] * sources[which].timescale))
                    if moof[ds + dh] == 1:
                        old = struct.unpack_from(">Q", moof, ds + dh + 4)[0]
                        struct.pack_into(">Q", moof, ds + dh + 4, max(0, old - shift))
                    else:
                        old = struct.unpack_from(">I", moof, ds + dh + 4)[0]
                        struct.pack_into(">I", moof, ds + dh + 4, max(0, old - shift))
                if flags & 0x000001:
                    # Absolute base_data_offset: shift it by how far this moof moved
                    # (moof and its mdat stay adjacent, as in the source).
                    old_moof_pos = mdat_off - len(moof)
                    old_base = struct.unpack_from(">Q", moof, ts + th + 8)[0]
                    struct.pack_into(">Q", moof, ts + th + 8, old_base - old_moof_pos + out.tell())
                out.write(moof)
                copier.copy(sources[which].fh, mdat_off, mdat_len, out)
            seq = len(frags)
            for j, (lang, name, cues) in enumerate(subs):
                seq += 1
                frag = _tx3g_fragment(len(sources) + 1 + j, cues, seq)
                if frag:
                    out.write(frag)
    finally:
        for s in sources:
            s.close()
