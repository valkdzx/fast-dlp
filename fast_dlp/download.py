"""Parallel ranged downloader for googlevideo streams.

Each worker thread keeps its own persistent HTTPS connection per host, so TLS
handshakes are paid once per worker. Chunk sizes follow the measured speed of each
connection (see fetch); bytes are written in place into a file sized up front
without writing zeros (utils.allocate).
"""
import collections
import http.client
import os
import queue
import sys
import threading
import time
import urllib.parse

from .net import SSL_CONTEXT
from .utils import FastDlpError, allocate, debug, human_size

MAX_CHUNK = 8 * 1024 * 1024
MIN_CHUNK = 512 * 1024
READ_BLOCK = 256 * 1024
MAX_ATTEMPTS = 6


class Stream:
    def __init__(self, url, size, path, label, user_agent, refresh=None, headers=None, offset=0):
        self.offset = offset                # byte in the remote file where this stream's byte 0 is
        self.headers = {k: v for k, v in (headers or {}).items() if k.lower() not in ("user-agent", "range")}
        self.url = url
        self.size = size
        self.path = path
        self.label = label
        self.user_agent = user_agent
        self.refresh = refresh          # callable returning a fresh URL, or None
        self.generation = 0
        self.lock = threading.Lock()
        self.done = 0

    def renew(self, seen_generation):
        with self.lock:
            if self.generation != seen_generation:
                return True             # another worker already renewed it
            if not self.refresh:
                return False
            debug(f"{self.label}: refreshing expired URL")
            self.url = self.refresh()
            self.generation += 1
            return True


class _Conn:
    def __init__(self):
        self.conns = {}

    def get(self, host):
        c = self.conns.get(host)
        if c is None:
            from . import net
            if net.PROXY:
                phost, pport, auth, _ = net.PROXY
                c = http.client.HTTPSConnection(phost, pport, timeout=20, context=SSL_CONTEXT)
                c.set_tunnel(host, 443, headers={"Proxy-Authorization": auth} if auth else None)
            else:
                c = http.client.HTTPSConnection(host, timeout=20, context=SSL_CONTEXT)
            self.conns[host] = c
        return c

    def drop(self, host):
        c = self.conns.pop(host, None)
        if c:
            c.close()

    def close(self):
        for c in self.conns.values():
            c.close()


class _Progress:
    def __init__(self, total, enabled):
        self.total = total
        self.done = 0
        self.lock = threading.Lock()
        self.start = time.time()
        self.enabled = enabled and sys.stderr.isatty()
        self._stop = threading.Event()
        self._thread = None

    def add(self, stream, n):
        with self.lock:
            self.done += n
            stream.done += n

    def _line(self, final=False):
        elapsed = max(time.time() - self.start, 1e-6)
        speed = self.done / elapsed
        pct = 100.0 * self.done / self.total if self.total else 100.0
        eta = (self.total - self.done) / speed if speed > 0 else 0
        tail = f"in {elapsed:.1f}s" if final else f"ETA {eta:4.0f}s"
        if not final and not self.done:
            tail = f"waiting for the first bytes {elapsed:.0f}s"
        return (f"\r[download] {pct:5.1f}% of {human_size(self.total)} "
                f"at {human_size(speed)}/s {tail}   ")

    def _write(self, line, end=""):
        # Pad over whatever a longer earlier line left on screen ("waiting for the first bytes").
        width = len(line)
        sys.stderr.write(line + " " * max(0, getattr(self, "_width", 0) - width) + end)
        sys.stderr.flush()
        self._width = max(width, getattr(self, "_width", 0))

    def _run(self):
        while not self._stop.wait(0.25):
            self._write(self._line())

    def __enter__(self):
        if self.enabled:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join()
        if exc[0] is None:
            self._write(self._line(final=True), "\n")
        elif self.enabled:
            sys.stderr.write("\n")


def _fetch_chunk(stream, start, end, conns, fh, progress, range_mode):
    """Download bytes [start, end] of stream into fh. Raises on persistent failure."""
    attempt = 0
    state = [start]
    while True:
        generation = stream.generation
        url = stream.url
        try:
            t0 = time.time()
            _fetch_once(url, stream, state, end, conns, fh, progress, range_mode)
            if state[0] > end:
                if start == 0:
                    debug(f"{stream.label}: first chunk ({(end - start + 1) >> 10} KiB) in {time.time() - t0:.1f}s")
                return
            raise IOError(f"short read at {state[0]}/{end + 1}")
        except _Expired:
            if not stream.renew(generation):
                raise FastDlpError(f"{stream.label}: stream URL expired (HTTP 403)")
            attempt += 1
        except (OSError, http.client.HTTPException) as e:
            attempt += 1
            debug(f"{stream.label} chunk {start}-{end}: {e!r} (attempt {attempt})")
            time.sleep(min(0.25 * 2 ** attempt, 4))
        if attempt >= MAX_ATTEMPTS:
            raise FastDlpError(f"{stream.label}: giving up on bytes {state[0]}-{end}")


class _Expired(Exception):
    pass


def _fetch_once(url, stream, state, end, conns, fh, progress, range_mode, hops=0):
    pos = state[0]
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc
    path = parts.path + ("?" + parts.query if parts.query else "")
    headers = {"User-Agent": stream.user_agent, "Accept": "*/*", "Accept-Encoding": "identity"}
    headers.update(stream.headers)                       # e.g. Cookie / Referer for TikTok's CDN
    if range_mode == "param":
        path += f"&range={pos + stream.offset}-{end + stream.offset}"
    else:
        headers["Range"] = f"bytes={pos + stream.offset}-{end + stream.offset}"
    conn = conns.get(host)
    try:
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
    except (OSError, http.client.HTTPException):
        conns.drop(host)
        raise
    status = resp.status
    if status in (301, 302, 303, 307, 308):
        location = resp.getheader("Location")
        resp.read()
        if not location or hops > 5:
            raise IOError(f"bad redirect {status}")
        return _fetch_once(urllib.parse.urljoin(url, location), stream, state, end, conns, fh,
                           progress, range_mode, hops + 1)
    if status in (403, 410):
        resp.read()
        raise _Expired()
    if status not in (200, 206):
        resp.read()
        raise IOError(f"HTTP {status}")
    if status == 200 and range_mode != "param" and pos + stream.offset > 0:
        resp.close()
        conns.drop(host)
        raise IOError("server ignored Range")
    fh.seek(pos)
    try:
        while pos <= end:
            block = resp.read(min(READ_BLOCK, end - pos + 1))
            if not block:
                break
            fh.write(block)
            pos += len(block)
            state[0] = pos
            progress.add(stream, len(block))
    except (OSError, http.client.HTTPException):
        conns.drop(host)
        raise
    if resp.will_close or not resp.isclosed():
        if not resp.isclosed():
            resp.close()
        conns.drop(host)


# -- dynamic chunks ------------------------------------------------------------------------------
# Chunk sizes follow the network: each connection measures its own speed on every chunk and
# takes the next one sized to last about TARGET_SECONDS. A slow line (60 Mbit/s over 16
# connections is ~0.5 MB/s each) gets ~1 MiB pieces, so no connection sits on a big tail; a
# fast one gets up to MAX_CHUNK (googlevideo throttles single ranges over ~10 MB). Near the
# end pieces shrink to a fair share of what is left, so the connections finish together.

TARGET_SECONDS = 2.0
START_CHUNK = 1024 * 1024        # first piece of each connection: measures its speed


class _Work:
    """What is left to fetch, as runs [stream, start, end, writer_key]; handed out in order."""

    def __init__(self, runs, connections):
        self.runs = collections.deque([list(r) for r in runs if r[2] >= r[1]])
        self.left = sum(r[2] - r[1] + 1 for r in self.runs)
        self.connections = max(1, connections)
        self.lock = threading.Lock()

    def take(self, rate):
        """Next (stream, start, end, key) sized for a connection doing `rate` bytes/s."""
        with self.lock:
            if not self.runs:
                return None
            want = START_CHUNK if not rate else rate * TARGET_SECONDS
            fair = -(-self.left // self.connections)
            size = int(max(MIN_CHUNK, min(MAX_CHUNK, want, fair)))
            run = self.runs[0]
            stream, start, end, key = run
            stop = min(end, start + size - 1)
            if stop == end:
                self.runs.popleft()
            else:
                run[1] = stop + 1
            self.left -= stop - start + 1
            return stream, start, stop, key


def fetch(runs, writer_for, connections=16, range_mode="header", progress=None):
    """Fetch every run with `connections` workers and dynamic chunk sizes.

    runs: [(stream, start, end, key)]; writer_for(handles, key) -> object with seek/write for
    that key (handles is the worker's own dict, for open files)."""
    work = _Work(runs, connections)
    errors = []

    def worker():
        conns, handles, rate = _Conn(), {}, None
        try:
            while not errors:
                job = work.take(rate)
                if job is None:
                    return
                stream, start, stop, key = job
                t = time.time()
                _fetch_chunk(stream, start, stop, conns, writer_for(handles, key), progress, range_mode)
                speed = (stop - start + 1) / max(time.time() - t, 1e-3)
                rate = speed if rate is None else 0.5 * rate + 0.5 * speed
        except BaseException as e:      # surface to the main thread
            errors.append(e)
        finally:
            conns.close()
            for fh in handles.values():
                try:
                    fh.close()
                except OSError:
                    pass

    n = max(1, min(connections, -(-work.left // MIN_CHUNK)))
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(n)]
    for t in threads:
        t.start()
    try:
        for t in threads:
            while t.is_alive():
                t.join(0.2)
    except KeyboardInterrupt:
        errors.append(KeyboardInterrupt())
        raise
    if errors:
        raise errors[0]


def download(streams, connections=16, range_mode="header", show_progress=True, keep_open=False):
    """Download all streams concurrently into their .path files (dynamic chunk sizes).

    keep_open: return {path: handle} opened before the download, for the caller to finish the
    file through (tags) and close. Reopening a big file that was just written makes Windows
    wait for the antivirus scan first: 4-7 s for 3 GB."""
    total = sum(s.size for s in streams)
    held = {}
    for s in streams:
        os.makedirs(os.path.dirname(os.path.abspath(s.path)), exist_ok=True)
        allocate(s.path, s.size)
        if keep_open:
            held[s.path] = open(s.path, "r+b")

    def writer_for(handles, s):
        fh = handles.get(s.path)
        if fh is None:
            fh = handles[s.path] = open(s.path, "r+b", buffering=0)
        return fh

    try:
        with _Progress(total, show_progress) as progress:
            fetch([(s, 0, s.size - 1, s) for s in streams], writer_for, connections, range_mode, progress)
        for s in streams:
            if s.done < s.size:
                raise FastDlpError(f"{s.label}: incomplete ({s.done}/{s.size} bytes)")
    except BaseException:
        for fh in held.values():
            fh.close()
        raise
    return held
