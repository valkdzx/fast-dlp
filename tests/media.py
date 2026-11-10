"""Tiny synthetic WebM and fragmented-MP4 files, shaped like YouTube's DASH streams."""
import struct

from fast_dlp import mux

E = mux._el
U = mux._uint


def webm(path, track_type, codec, clusters, block_payload=b"x", track_number=1):
    """clusters: list of (timestamp_ms, [block_payload,...]). Returns the payloads written."""
    ebml = E(0x1A45DFA3, E(0x4282, b"webm"))
    info = E(mux._INFO, U(mux._TIMECODE_SCALE, 1000000, 3) + E(mux._DURATION, struct.pack(">d", 10000.0)))
    entry = E(mux._TRACK_ENTRY, U(mux._TRACK_NUMBER, track_number, 1) + U(mux._TRACK_UID, 99, 8) +
              U(mux._TRACK_TYPE, track_type, 1) + mux._str(0x86, codec))
    tracks = E(mux._TRACKS, entry)
    body = info + tracks
    written = []
    for ts, blocks in clusters:
        content = U(mux._TIMESTAMP, ts, 2)
        for i, payload in enumerate(blocks):
            # SimpleBlock: track vint, int16 relative timecode, flags, data
            content += E(mux._SIMPLE_BLOCK, bytes([0x80 | track_number]) + struct.pack(">hB", i, 0x80) + payload)
            written.append(payload)
        body += E(mux._CLUSTER, content)
    with open(path, "wb") as fh:
        fh.write(ebml + mux._id_bytes(mux._SEGMENT) + mux._size8(len(body)) + body)
    return written


B = mux._box


def _full(typ, version, payload, flags=0):
    return B(typ, struct.pack(">I", (version << 24) | flags) + payload)


def mp4(path, handler, timescale, fragments, track_id=1):
    """fragments: list of (decode_time, payload). Returns payloads in order."""
    ftyp = B(b"ftyp", b"dash" + b"\0\0\0\0" + b"iso6mp41")
    mvhd = _full(b"mvhd", 0, b"\0" * 8 + struct.pack(">II", 1000, 0) + b"\0" * 76 + struct.pack(">I", 2))
    tkhd = _full(b"tkhd", 0, b"\0" * 8 + struct.pack(">I", track_id) + b"\0" * 68, flags=3)
    mdhd = _full(b"mdhd", 0, b"\0" * 8 + struct.pack(">II", timescale, 0) + b"\0" * 4)
    hdlr = _full(b"hdlr", 0, b"\0" * 4 + handler + b"\0" * 12 + b"\0")
    trak = B(b"trak", tkhd + B(b"mdia", mdhd + hdlr + B(b"minf", b"")))
    trex = _full(b"trex", 0, struct.pack(">IIIII", track_id, 1, 0, 0, 0))
    moov = B(b"moov", mvhd + trak + B(b"mvex", trex))
    out = ftyp + moov
    for seq, (dt, payload) in enumerate(fragments, 1):
        mfhd = _full(b"mfhd", 0, struct.pack(">I", seq))
        tfhd = _full(b"tfhd", 0, struct.pack(">I", track_id), flags=0x020000)   # default-base-is-moof
        tfdt = _full(b"tfdt", 1, struct.pack(">Q", dt))
        out += B(b"moof", mfhd + B(b"traf", tfhd + tfdt)) + B(b"mdat", payload)
    with open(path, "wb") as fh:
        fh.write(out)
    return [p for _, p in fragments]


def webm_blocks(path):
    """[(track_number, payload)] in file order, plus the parsed top-level ids."""
    with open(path, "rb") as fh:
        data = fh.read()
    eid, n1 = mux._read_id(data, 0)
    size, n2 = mux._read_size(data, n1)
    p = n1 + n2 + size
    eid, n1 = mux._read_id(data, p)
    size, n2 = mux._read_size(data, p + n1)
    seg_start = p + n1 + n2
    blocks, ids, elements = [], [], {}
    for cid, s, bs, be in mux._children(data, seg_start, seg_start + size):
        ids.append(cid)
        elements[cid] = data[s:be]
        if cid == mux._CLUSTER:
            for bid, s2, bs2, be2 in mux._children(data, bs, be):
                if bid == mux._SIMPLE_BLOCK:
                    blocks.append((data[bs2] & 0x7F, data[bs2 + 4:be2]))
    return blocks, ids, elements


def mp4_boxes(path):
    with open(path, "rb") as fh:
        data = fh.read()
    return data, [(t, s, h, e) for t, s, h, e in mux._boxes(data, 0, len(data))]
