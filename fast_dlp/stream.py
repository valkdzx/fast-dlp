"""Streaming output: a finished, tagged file produced straight from googlevideo into any
writable (an HTTP response), never touching the disk.

YouTube's DASH formats carry an index (MP4 `sidx`, WebM `Cues`) that lists every fragment's
time and byte range. From the two indexes the whole output is planned before the first
byte: the new header (one moov with both tracks, or Info/Tracks/Tags/Cues for WebM), the
interleaving order and therefore the exact size. Then the fragments are fetched in parallel
(a bounded window, in order) and patched on the fly - the same in-place edits the file
muxer makes: track number, sequence number, timestamps for a section.

MP3 is the one transcode: the audio is planned the same way and piped through ffmpeg.
"""
import bisect
import collections
import struct
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

from . import download, formats as fmt, mp3, mux, net, section
from .innertube import TIZEN_UA, VISION_UA
from .utils import FastDlpError, allocate, debug, sanitize_filename

CHUNK = 4 * 1024 * 1024          # one parallel request (16 x 4 MiB ~ 50 MiB/s)
MIME = {"mp4": "video/mp4", "m4a": "audio/mp4", "webm": "video/webm", "opus": "audio/webm",
        "mp3": "audio/mpeg"}


def user_agent(video):
    return TIZEN_UA if video.client == "TVHTML5" else VISION_UA


# -- fetching ---------------------------------------------------------------------------------

class _Mem:
    """File-like target for download._fetch_chunk: bytes [start, start+n) of the remote file."""

    def __init__(self, start, n):
        self.start, self.pos = start, 0
        self.data = bytearray(n)

    def seek(self, absolute):
        self.pos = absolute - self.start

    def write(self, b):
        self.data[self.pos:self.pos + len(b)] = b
        self.pos += len(b)


class _Count:
    def __init__(self):
        self.n = 0
        self.lock = threading.Lock()

    def add(self, stream, n):
        with self.lock:
            self.n += n


class _Fetcher:
    def __init__(self, workers):
        self.pool = ThreadPoolExecutor(max(1, workers), thread_name_prefix="fast-dlp-fetch")
        self.local = threading.local()
        self.count = _Count()

    def _get(self, remote, start, end):
        conns = getattr(self.local, "conns", None)
        if conns is None:
            conns = self.local.conns = download._Conn()
        buf = _Mem(start, end - start + 1)
        download._fetch_chunk(remote.stream, start, end, conns, buf, self.count, "param")
        return buf.data

    def submit(self, remote, start, end):
        return self.pool.submit(self._get, remote, start, end)

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)


class Remote:
    """One googlevideo format: its header bytes and fragment index."""

    def __init__(self, video, f):
        self.f = f
        self.itag = f["itag"]
        self.size = fmt.size(f)
        self.ext = fmt.ext(f)
        ua = user_agent(video)
        self.stream = download.Stream(f["url"], self.size, None, f"itag {self.itag}", ua,
                                      refresh=lambda itag=self.itag: video.fresh_url(itag))
        if "initRange" not in f or "indexRange" not in f:
            raise FastDlpError(f"itag {self.itag} has no index; it cannot be streamed")
        init_end = int(f["initRange"]["end"])
        idx_start, idx_end = int(f["indexRange"]["start"]), int(f["indexRange"]["end"])
        status, head = net.request(f"{f['url']}&range=0-{idx_end}", headers={"User-Agent": ua}, timeout=20)
        if status not in (200, 206) or len(head) < idx_end + 1:
            raise FastDlpError(f"could not read the index of itag {self.itag} (HTTP {status})")
        self.init = bytes(head[:init_end + 1])
        index = head[idx_start:idx_end + 1]
        if index[4:8] == b"sidx":
            self.kind = "mp4"
            self.fragments = section.mp4_index(index, idx_start)
        else:
            self.kind = "webm"
            self.fragments = section.webm_index(self.init, index, self.size)
        self.fragments = [(t0, t1, b0, min(b1, self.size - 1)) for t0, t1, b0, b1 in self.fragments]


# -- plans ----------------------------------------------------------------------------------------
# A plan is a list of pieces: bytes (written as they are) or (remote, start, end, patch, split).
# patch(buffer) edits the piece's first chunk (or the whole piece when split is False).

def _choose(fragments, start, end):
    if start is None and end is None:
        return fragments
    chosen = [f for f in fragments if f[1] > (start or 0) and (end is None or f[0] < end)]
    if not chosen:
        raise FastDlpError("the section is outside the video")
    return chosen


def _mp4_plan(remotes, meta, start, end, duration):
    sources = []
    for r in remotes:
        ftyp = moov = None
        for typ, s, h, e in mux._boxes(r.init, 0, len(r.init)):
            if typ == b"ftyp":
                ftyp = r.init[s:e]
            elif typ == b"moov":
                moov = r.init[s:e]
        if not ftyp or not moov:
            raise FastDlpError(f"itag {r.itag}: no ftyp/moov in the header")
        src = mux._Mp4Source.__new__(mux._Mp4Source)
        src.moov = moov
        src._parse_moov()
        src.ftyp = ftyp
        sources.append(src)
    for i, s in enumerate(sources, 1):
        mux._set_track_id_tkhd(s.traks[0], i)
        struct.pack_into(">I", s.trexs[0], 12, i)
    mvhd = sources[0].mvhd
    struct.pack_into(">I", mvhd, len(mvhd) - 4, len(sources) + 1)

    chosen = [_choose(r.fragments, start, end) for r in remotes]
    sectioned = start is not None or end is not None
    rebase = min(c[0][0] for c in chosen) if sectioned else 0.0
    if sectioned:
        mux._set_durations(mvhd, [s.traks[0] for s in sources], (end or duration) - rebase)
    body = bytes(mvhd) + b"".join(bytes(s.traks[0]) for s in sources)
    body += mux._box(b"mvex", b"".join(bytes(s.trexs[0]) for s in sources))
    if meta:
        body += mux._mp4_udta(meta)
    header = bytes(sources[0].ftyp) + mux._box(b"moov", body)

    merged = sorted(((t0, i, b0, b1) for i, c in enumerate(chosen) for t0, t1, b0, b1 in c),
                    key=lambda x: (x[0], x[1]))
    plan, pos = [header], len(header)
    for seq, (t0, i, b0, b1) in enumerate(merged, 1):
        plan.append((remotes[i], b0, b1, _mp4_patch(seq, i + 1, pos, b0, rebase, sources[i].timescale), True))
        pos += b1 - b0 + 1
    return plan, pos


def _mp4_patch(seq, track, out_pos, src_pos, rebase, timescale):
    def patch(buf):
        typ = bytes(buf[4:8])
        if typ != b"moof":
            raise FastDlpError(f"expected a moof at byte {src_pos}, found {typ!r}")
        moof_len = struct.unpack_from(">I", buf, 0)[0]
        moof = memoryview(buf)[:moof_len]
        ms, mh, me = mux._Mp4Source._find(moof, [b"mfhd"], 8)
        struct.pack_into(">I", buf, ms + mh + 4, seq)
        ts, th, te = mux._Mp4Source._find(moof, [b"traf", b"tfhd"], 8)
        flags = int.from_bytes(buf[ts + th + 1:ts + th + 4], "big")
        struct.pack_into(">I", buf, ts + th + 4, track)
        if flags & 0x000001:                      # absolute base_data_offset: moved with the moof
            old = struct.unpack_from(">Q", buf, ts + th + 8)[0]
            struct.pack_into(">Q", buf, ts + th + 8, old - src_pos + out_pos)
        if rebase:
            ds, dh, de = mux._Mp4Source._find(moof, [b"traf", b"tfdt"], 8)
            shift = int(round(rebase * timescale))
            if buf[ds + dh] == 1:
                old = struct.unpack_from(">Q", buf, ds + dh + 4)[0]
                struct.pack_into(">Q", buf, ds + dh + 4, max(0, old - shift))
            else:
                old = struct.unpack_from(">I", buf, ds + dh + 4)[0]
                struct.pack_into(">I", buf, ds + dh + 4, max(0, old - shift))
    return patch


def _webm_header(init):
    eid, n1 = mux._read_id(init, 0)
    hsize, n2 = mux._read_size(init, n1)
    ebml = init[:n1 + n2 + hsize]
    p = n1 + n2 + hsize
    eid, n1 = mux._read_id(init, p)
    if eid != mux._SEGMENT:
        raise FastDlpError("no Segment in the WebM header")
    _, n2 = mux._read_size(init, p + n1)
    info = tracks = None
    pos = p + n1 + n2
    while pos < len(init):
        cid, m1 = mux._read_id(init, pos)
        size, m2 = mux._read_size(init, pos + m1)
        if size is None or pos + m1 + m2 + size > len(init):
            break
        if cid == mux._INFO:
            info = init[pos:pos + m1 + m2 + size]
        elif cid == mux._TRACKS:
            tracks = init[pos:pos + m1 + m2 + size]
        pos += m1 + m2 + size
    if info is None or tracks is None:
        raise FastDlpError("no Info/Tracks in the WebM header")
    return ebml, info, tracks


def _retime_head(buf, delta):
    """Move the cluster at the start of buf earlier by delta timecode units, in place."""
    eid, n1 = mux._read_id(buf, 0)
    _, n2 = mux._read_size(buf, n1)
    p = n1 + n2
    tid, m1 = mux._read_id(buf, p)
    tsize, m2 = mux._read_size(buf, p + m1)
    if eid != mux._CLUSTER or tid != mux._TIMESTAMP:
        raise FastDlpError("expected a cluster starting with its timestamp")
    s = p + m1 + m2
    value = max(0, int.from_bytes(buf[s:s + tsize], "big") - delta)
    buf[s:s + tsize] = value.to_bytes(tsize, "big")


def _webm_plan(remotes, meta, start, end, duration):
    heads = [_webm_header(r.init) for r in remotes]
    scales, durations, entries = set(), [], []
    for ebml, info, tracks in heads:
        scale, dur = 1000000, None
        for cid, s, bs, be in mux._children(info, mux._body(info), len(info)):
            if cid == mux._TIMECODE_SCALE:
                scale = mux._uint_value(info, bs, be)
            elif cid == mux._DURATION:
                dur = struct.unpack(">f" if be - bs == 4 else ">d", info[bs:be])[0]
        scales.add(scale)
        durations.append(dur or 0.0)
        found = [tracks[s:e] for cid, s, bs, e in mux._children(tracks, mux._body(tracks), len(tracks))
                 if cid == mux._TRACK_ENTRY]
        if len(found) != 1:
            raise FastDlpError("expected one track per WebM format")
        entries.append(found[0])
    if len(scales) != 1:
        raise FastDlpError("different timecode scales")
    scale = scales.pop()

    chosen = [_choose(r.fragments, start, end) for r in remotes]
    sectioned = start is not None or end is not None
    rebase_s = min(c[0][0] for c in chosen) if sectioned else 0.0
    rebase = int(round(rebase_s * 1e9 / scale))
    info_src = heads[0][1]
    body = bytearray()
    for cid, s, bs, be in mux._children(info_src, mux._body(info_src), len(info_src)):
        if cid != mux._DURATION:
            body += info_src[s:be]
    total = ((end or duration) - rebase_s) * 1e9 / scale if sectioned else max(durations)
    if total:
        body += mux._el(mux._DURATION, struct.pack(">d", total))
    info = mux._el(mux._INFO, bytes(body))
    renumber, track_bytes = [], b""
    for n, entry in enumerate(entries, 1):
        renumber.append(n if mux._track_field(entry, mux._TRACK_NUMBER) != n else None)
        track_bytes += mux._renumber_track(entry, n)
    tracks = mux._el(mux._TRACKS, track_bytes)
    types = [mux._track_field(e, mux._TRACK_TYPE) for e in entries]
    cue_src = types.index(1) if 1 in types else 0
    tags = mux._webm_tags(meta or {})
    attachments = mux._webm_attachments(meta or {})

    merged = sorted(((int(round(t0 * 1e9 / scale)), i, b0, b1) for i, c in enumerate(chosen)
                     for t0, t1, b0, b1 in c), key=lambda x: (x[0], x[1]))

    def cue_point(ts, pos):
        ts = max(0, ts - rebase)
        return mux._el(0xBB, mux._uint(0xB3, ts) + mux._el(0xB7, mux._uint(0xF7, cue_src + 1, 1) + mux._uint(0xF1, pos)))

    def seek(eid, pos):
        return mux._el(0x4DBB, mux._el(0x53AB, mux._id_bytes(eid)) + mux._uint(0x53AC, pos))

    n_cues = sum(1 for _, i, _, _ in merged if i == cue_src)
    cues_size = len(mux._el(mux._CUES, b"\0" * (len(cue_point(0, 0)) * n_cues)))
    listed = [(mux._INFO, info), (mux._TRACKS, tracks)]
    if tags:
        listed.append((mux._TAGS, tags))
    if attachments:
        listed.append((mux._ATTACHMENTS, attachments))
    seekhead_size = len(mux._el(mux._SEEKHEAD, b"".join(seek(e, 0) for e, _ in listed) + seek(mux._CUES, 0)))
    positions, pos = {}, seekhead_size
    for eid, data in listed:
        positions[eid] = pos
        pos += len(data)
    positions[mux._CUES] = pos
    pos += cues_size
    points = bytearray()
    pieces = []
    for ts, i, b0, b1 in merged:
        if i == cue_src:
            points += cue_point(ts, pos)
        pieces.append((i, b0, b1))
        pos += b1 - b0 + 1
    segment_size = pos
    seekhead = mux._el(mux._SEEKHEAD, b"".join(seek(e, positions[e]) for e, _ in listed) +
                       seek(mux._CUES, positions[mux._CUES]))
    cues = mux._el(mux._CUES, bytes(points))
    header = (heads[0][0] + mux._id_bytes(mux._SEGMENT) + mux._size8(segment_size) + seekhead +
              b"".join(d for _, d in listed) + cues)

    plan = [header]
    for i, b0, b1 in pieces:
        number = renumber[i]

        def patch(buf, number=number):
            if number is not None:
                mux._patch_cluster_tracks(buf, number)
            if rebase:
                _retime_head(buf, rebase)
        needs = number is not None or rebase
        # Renumbering touches every block, so that cluster is fetched whole (audio: small).
        plan.append((remotes[i], b0, b1, patch if needs else None, number is None))
    return plan, len(header) + sum(b1 - b0 + 1 for _, b0, b1 in pieces)


# -- output --------------------------------------------------------------------------------------

class Output:
    """A planned file. chunks() yields its bytes in order; size is exact (None for MP3)."""

    def __init__(self, filename, ext, plan, size, connections, transcode=None, prefix=b""):
        self.filename = filename
        self.ext = ext
        self.mime = MIME.get(ext, "application/octet-stream")
        self.plan = plan
        self.size = size
        self.connections = connections
        self.transcode = transcode          # ffmpeg command for MP3, fed by the plan
        self.prefix = prefix                # ID3 tag before the transcoded audio
        self.fetched = 0

    def _container_chunks(self):
        fetcher = _Fetcher(self.connections)
        tasks = []
        for piece in self.plan:
            if isinstance(piece, (bytes, bytearray)):
                tasks.append((piece, None, 0, 0, None))
                continue
            remote, b0, b1, patch, split = piece
            if not split:
                tasks.append((None, remote, b0, b1, patch))
                continue
            first = True
            for a in range(b0, b1 + 1, CHUNK):
                tasks.append((None, remote, a, min(b1, a + CHUNK - 1), patch if first else None))
                first = False
        window = self.connections + 4      # chunks held at once: memory stays ~80 MiB
        pending = collections.deque()
        it = iter(tasks)

        def fill():
            while len(pending) < window:
                t = next(it, None)
                if t is None:
                    return
                literal, remote, a, b, patch = t
                pending.append((t, None if literal is not None else fetcher.submit(remote, a, b)))

        try:
            fill()
            while pending:
                (literal, remote, a, b, patch), fut = pending.popleft()
                data = literal if fut is None else fut.result()
                fill()
                if patch:
                    patch(data)
                self.fetched = fetcher.count.n
                yield data
        finally:
            fetcher.close()

    def chunks(self):
        if not self.transcode:
            yield from self._container_chunks()
            return
        proc = subprocess.Popen(self.transcode, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        failure = []

        def feed():
            try:
                for block in self._container_chunks():
                    proc.stdin.write(block)
            except BrokenPipeError:
                pass
            except BaseException as e:            # surface fetch errors to the reader
                failure.append(e)
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass

        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()
        try:
            if self.prefix:
                yield self.prefix
            while True:
                block = proc.stdout.read(256 * 1024)
                if not block:
                    break
                yield block
            proc.wait()
            if failure:
                raise failure[0]
            if proc.returncode != 0:
                raise FastDlpError(f"ffmpeg failed: {proc.stderr.read().decode(errors='replace')[-300:]}")
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            proc.stdout.close()
            proc.stderr.close()


# -- straight into the final file --------------------------------------------------------------------

class _Mapped:
    """download._fetch_chunk writes at source offsets of one remote file; the output keeps that
    file's fragments at other places (interleaved with the other track). Writes are split
    across fragment boundaries and land where the muxed file wants them."""

    def __init__(self, fh, segments):
        self.fh = fh
        self.segments = segments           # sorted (src_start, src_end, dst_start)
        self.starts = [seg[0] for seg in segments]
        self.pos = 0

    def seek(self, src_pos):
        self.pos = src_pos

    def write(self, b):
        view = memoryview(b)
        while len(view):
            i = bisect.bisect_right(self.starts, self.pos) - 1
            src0, src1, dst0 = self.segments[i]
            if not src0 <= self.pos <= src1:
                raise FastDlpError(f"byte {self.pos} is outside the planned fragments")
            n = min(len(view), src1 - self.pos + 1)
            self.fh.seek(dst0 + self.pos - src0)
            self.fh.write(view[:n])
            self.pos += n
            view = view[n:]


def write_file(plan, path, connections=16, range_mode="param", show_progress=True):
    """Download a plan straight into path: every piece goes to its final offset, in parallel.

    There are no part files and no merge pass: the header is written once, the fragments land
    where the muxed file wants them, and afterwards only fragment headers are patched in place
    (a few KiB each: moof, cluster timestamp; whole audio clusters when the track number
    changes). Each byte is downloaded and written once."""
    pieces, pos = [], 0                   # (dst, literal | (remote, b0, b1, patch, split))
    for piece in plan:
        pieces.append((pos, piece))
        pos += len(piece) if isinstance(piece, (bytes, bytearray)) else piece[2] - piece[1] + 1
    total = pos
    remote_bytes = sum(p[2] - p[1] + 1 for _, p in pieces if not isinstance(p, (bytes, bytearray)))
    # Requests follow each remote file's own byte order (contiguous runs, sized on the fly by
    # download.fetch), not one request per fragment: per-fragment requests were 2.5x slower on 4K.
    by_remote = {}
    for dst, piece in pieces:
        if not isinstance(piece, (bytes, bytearray)):
            by_remote.setdefault(id(piece[0]), (piece[0], []))[1].append((piece[1], piece[2], dst))
    runs = []
    for remote, segments in by_remote.values():
        segments.sort()
        run = None
        for src0, src1, dst in segments:
            if run and src0 == run[2] + 1:
                run[2] = src1
            else:
                run = [remote.stream, src0, src1, segments]
                runs.append(run)

    allocate(path, total)
    # One handle stays open from start to end: reopening a big fresh file makes Windows wait
    # for the antivirus to scan all of it first (seconds per GB) before the header patches.
    hold = open(path, "r+b")
    try:
        _write_planned(hold, path, pieces, runs, by_remote, remote_bytes, connections, range_mode, show_progress)
    finally:
        hold.close()
    return total


def _write_planned(hold, path, pieces, runs, by_remote, remote_bytes, connections, range_mode, show_progress):
    for dst, piece in pieces:
        if isinstance(piece, (bytes, bytearray)):
            hold.seek(dst)
            hold.write(piece)
    hold.flush()

    def writer_for(handles, segments):
        fh = handles.get("out")
        if fh is None:
            fh = handles["out"] = open(path, "r+b", buffering=0)
        return _Mapped(fh, segments)

    for remote, _ in by_remote.values():
        remote.stream.done = 0
    with download._Progress(remote_bytes, show_progress) as progress:
        download.fetch([tuple(r) for r in runs], writer_for, connections, range_mode, progress)
    if progress.done < remote_bytes:
        raise FastDlpError(f"incomplete download ({progress.done}/{remote_bytes} bytes)")

    fh = hold                      # sees the workers' bytes: they wrote through the OS cache
    for dst, piece in pieces:
        if isinstance(piece, (bytes, bytearray)) or not piece[3]:
            continue
        remote, b0, b1, patch, split = piece
        fh.seek(dst)
        if not split:
            n = b1 - b0 + 1                              # whole cluster (track renumbering)
        elif remote.kind == "mp4":
            n = min(b1 - b0 + 1, struct.unpack(">I", fh.read(4))[0])  # the moof
            fh.seek(dst)
        else:
            n = min(b1 - b0 + 1, 64)                     # cluster id, size and timestamp
        buf = bytearray(fh.read(n))
        patch(buf)
        fh.seek(dst)
        fh.write(buf)


def plan_for(video, chosen, meta, start=None, end=None):
    """(plan, size, kind) for these formats, or raises FastDlpError when they cannot be planned."""
    with ThreadPoolExecutor(len(chosen)) as pool:              # headers + indexes, in parallel
        remotes = list(pool.map(lambda f: Remote(video, f), chosen))
    kinds = {r.kind for r in remotes}
    if len(kinds) != 1:
        raise FastDlpError("video and audio are in different containers")
    kind = kinds.pop()
    builder = _mp4_plan if kind == "mp4" else _webm_plan
    plan, size = builder(remotes, meta, start, end, video.duration or 0)
    return plan, size, kind


# -- choosing formats ------------------------------------------------------------------------------

def pick(video, opts):
    """[video_format, audio_format] / [audio] / [video] for the options."""
    all_formats = [f for f in video.formats if fmt.size(f) > 0 and "initRange" in f]
    audios = [f for f in all_formats if fmt.kind(f) == "audio"]
    videos = [f for f in all_formats if fmt.kind(f) == "video"]
    mode = opts["mode"]

    def best_audio(ext=None):
        pool = [f for f in audios if ext is None or fmt.ext(f) == ext] or audios
        if not pool:
            raise FastDlpError("no audio formats")
        return max(pool, key=lambda f: fmt._audio_key(f, None))

    def best_video(container):
        pool = [f for f in videos if fmt.ext(f) == container]
        if opts.get("codec"):
            pool = [f for f in pool if fmt.codec(f).startswith(opts["codec"])] or pool
        if opts.get("height"):
            exact = [f for f in pool if f.get("height") == opts["height"]
                     and (not opts.get("fps") or f.get("fps") == opts["fps"])]
            pool = exact or [f for f in pool if (f.get("height") or 0) <= opts["height"]] or pool
        if not pool:
            raise FastDlpError(f"no {container.upper()} video formats")
        return max(pool, key=lambda f: fmt._video_key(f, opts.get("codec")))

    if mode == "audio":
        af = opts["audio_format"]
        return [best_audio({"m4a": "m4a", "opus": "webm", "mp3": None}[af])]
    container = opts["container"]
    v = best_video(container)
    if mode == "video_only":
        return [v]
    return [v, best_audio("m4a" if container == "mp4" else "webm")]


def mp3_command(ffmpeg, quality="320k"):
    """ffmpeg reading the planned audio on stdin, writing MP3 frames (no tags) to stdout."""
    rate = ["-q:a", quality[1:]] if quality.upper().startswith("V") else ["-b:a", quality]
    return [ffmpeg, "-loglevel", "error", "-nostdin", "-i", "pipe:0", "-map", "0:a:0", "-vn",
            "-c:a", "libmp3lame", *rate, "-map_metadata", "-1", "-id3v2_version", "0",
            "-write_id3v1", "0", "-write_xing", "0", "-f", "mp3", "pipe:1"]


def prepare(video, opts, meta, ffmpeg=None):
    """Plan the output for these options. Fetches only the formats' headers and indexes."""
    chosen = pick(video, opts)
    remotes = [Remote(video, f) for f in chosen]
    kinds = {r.kind for r in remotes}
    if len(kinds) != 1:
        raise FastDlpError("video and audio are in different containers")
    kind = kinds.pop()
    start, end = opts.get("start"), opts.get("end")
    duration = video.duration or 0
    builder = _mp4_plan if kind == "mp4" else _webm_plan
    mode = opts["mode"]
    if mode == "audio" and opts["audio_format"] == "mp3":
        if not ffmpeg:
            raise FastDlpError("MP3 needs ffmpeg on the server")
        plan, _ = builder(remotes, None, start, end, duration)
        cmd = mp3_command(ffmpeg, opts.get("mp3_quality") or "320k")
        return Output(None, "mp3", plan, None, opts["connections"], transcode=cmd,
                      prefix=mp3.id3v23(meta or {})), chosen
    plan, size = builder(remotes, meta, start, end, duration)
    if mode == "audio":
        ext = "m4a" if kind == "mp4" else "opus"
    else:
        ext = kind
    out = Output(None, ext, plan, size, opts["connections"])
    debug(f"stream plan: {len(plan)} pieces, {size} bytes, itags {[r.itag for r in remotes]}")
    return out, chosen


def file_ext(ext):
    """Extension written to disk: Opus in WebM is a .webm file (a .opus file is Ogg)."""
    return "webm" if ext == "opus" else ext


def filename(template, fields, ext):
    try:
        name = template.format_map(collections.defaultdict(lambda: "NA", fields))
    except (ValueError, IndexError, AttributeError):
        name = fields.get("title") or fields.get("id") or "video"
    return sanitize_filename(name) + "." + file_ext(ext)
