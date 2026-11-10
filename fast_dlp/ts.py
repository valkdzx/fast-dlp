"""Minimal MPEG-TS demuxer: pull one elementary stream (the audio) out of HLS .ts segments.

VK and SoundCloud serve MP3 or AAC inside MPEG-TS; the PES payloads of the audio PID,
concatenated, are a plain .mp3 (MPEG audio) or .aac (ADTS) file - no re-encoding.
"""

PACKET = 188
STREAM_TYPES = {0x03: "mp3", 0x04: "mp3", 0x0F: "aac", 0x11: "aac", 0x1B: "h264", 0x24: "hevc"}


def _sections(packets, pid):
    for p in packets:
        if _pid(p) == pid and p[1] & 0x40:            # payload_unit_start
            off = _payload_offset(p)
            if off is None:
                continue
            off += 1 + p[off]                            # pointer_field
            yield p[off:]


def _pid(p):
    return ((p[1] & 0x1F) << 8) | p[2]


def _payload_offset(p):
    afc = (p[3] >> 4) & 0x3
    if afc == 1:
        return 4
    if afc == 3:
        return 5 + p[4]
    return None                                          # adaptation field only / reserved


def _packets(data):
    start = data.find(b"\x47")
    while start >= 0 and start + PACKET < len(data) and data[start + PACKET] != 0x47:
        start = data.find(b"\x47", start + 1)
    for i in range(max(start, 0), len(data) - PACKET + 1, PACKET):
        if data[i] == 0x47:
            yield data[i:i + PACKET]


def streams(data):
    """{pid: kind} from the PAT/PMT of a TS buffer."""
    packets = list(_packets(data[:PACKET * 64]))
    pmt_pids = []
    for sec in _sections(packets, 0):
        length = ((sec[1] & 0x0F) << 8) | sec[2]
        body = sec[8:3 + length - 4]
        for i in range(0, len(body) - 3, 4):
            program = (body[i] << 8) | body[i + 1]
            if program:
                pmt_pids.append(((body[i + 2] & 0x1F) << 8) | body[i + 3])
        break
    found = {}
    for pmt in pmt_pids:
        for sec in _sections(packets, pmt):
            length = ((sec[1] & 0x0F) << 8) | sec[2]
            info_len = ((sec[10] & 0x0F) << 8) | sec[11]
            i, end = 12 + info_len, 3 + length - 4
            while i + 5 <= end:
                stype = sec[i]
                pid = ((sec[i + 1] & 0x1F) << 8) | sec[i + 2]
                es_len = ((sec[i + 3] & 0x0F) << 8) | sec[i + 4]
                found[pid] = STREAM_TYPES.get(stype, f"0x{stype:02x}")
                i += 5 + es_len
            break
    return found


def extract(data, pid):
    """Concatenated PES payloads (without PES headers) of one PID."""
    out = bytearray()
    for p in _packets(data):
        if _pid(p) != pid:
            continue
        off = _payload_offset(p)
        if off is None or off >= PACKET:
            continue
        payload = p[off:]
        if p[1] & 0x40 and payload[:3] == b"\x00\x00\x01":   # PES header at unit start
            header_len = payload[8]
            payload = payload[9 + header_len:]
        out += payload
    return bytes(out)


def audio(data):
    """(kind, bytes) of the first audio stream in a TS buffer, or (None, b"")."""
    for pid, kind in streams(data).items():
        if kind in ("mp3", "aac"):
            return kind, extract(data, pid)
    return None, b""
