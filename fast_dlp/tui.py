"""Terminal UI: pick which entries of an album/playlist to download, set options, then watch
them download side by side. Plain ANSI + msvcrt (Windows) / termios (Unix), no dependencies.

Keys in the picker:
    Up/Down, PgUp/PgDn, Home/End   move
    Space                          tick / untick a track
    Left/Right                     change an option (on track rows: page up / page down)
    A / N / I                      all / none / invert
    Enter                          start            Esc / Q   cancel
"""
import collections
import os
import shutil
import sys
import threading
import time

from .i18n import t

ESC = "\x1b["
# Keys, normalised across platforms
UP, DOWN, LEFT, RIGHT, HOME, END, PGUP, PGDN, ENTER, SPACE, CANCEL = (
    "up", "down", "left", "right", "home", "end", "pgup", "pgdn", "enter", "space", "cancel")


# -- terminal plumbing ---------------------------------------------------------------------

def available():
    return sys.stdin is not None and sys.stdin.isatty() and sys.stdout.isatty()


def _enable_vt():
    if os.name != "nt":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        for std in (-11, -12):                     # stdout, stderr
            h = k.GetStdHandle(std)
            mode = ctypes.c_uint32()
            if k.GetConsoleMode(h, ctypes.byref(mode)):
                k.SetConsoleMode(h, mode.value | 0x0004)   # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


if os.name == "nt":
    import msvcrt

    _WIN = {"H": UP, "P": DOWN, "K": LEFT, "M": RIGHT, "G": HOME, "O": END, "I": PGUP, "Q": PGDN}

    def read_key():
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            return _WIN.get(msvcrt.getwch())
        return _plain(ch)

    class _Raw:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False
else:
    import termios
    import tty

    _SEQ = {"[A": UP, "[B": DOWN, "[D": LEFT, "[C": RIGHT, "[H": HOME, "[F": END, "OH": HOME,
            "OF": END, "[5~": PGUP, "[6~": PGDN, "[1~": HOME, "[4~": END}

    def read_key():
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            import select
            if not select.select([sys.stdin], [], [], 0.03)[0]:
                return CANCEL
            seq = sys.stdin.read(1)
            while seq[-1:] not in "ABCDHF~" and len(seq) < 4:
                seq += sys.stdin.read(1)
            return _SEQ.get(seq)
        return _plain(ch)

    class _Raw:
        def __enter__(self):
            self.fd = sys.stdin.fileno()
            self.old = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
            return self

        def __exit__(self, *exc):
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)
            return False


def _plain(ch):
    if ch in ("\r", "\n"):
        return ENTER
    if ch == " ":
        return SPACE
    if ch in ("\x1b", "\x03"):
        return CANCEL
    return ch.lower()


class _Screen:
    """Alternate screen, hidden cursor; restored on exit even after an exception."""

    def __enter__(self):
        _enable_vt()
        self.out = sys.__stdout__
        self.out.write(ESC + "?1049h" + ESC + "?25l")
        self.out.flush()
        return self

    def draw(self, lines):
        cols, rows = shutil.get_terminal_size((100, 30))
        buf = [ESC + "H"]
        for i in range(rows):
            text = lines[i] if i < len(lines) else ""
            buf.append(_fit(text, cols) + ESC + "K" + ("\r\n" if i < rows - 1 else ""))
        self.out.write("".join(buf))
        self.out.flush()

    def __exit__(self, *exc):
        self.out.write(ESC + "?25h" + ESC + "?1049l")
        self.out.flush()
        return False


def _visible_len(text):
    n, i = 0, 0
    while i < len(text):
        if text[i] == "\x1b":
            j = text.find("m", i)
            i = j + 1 if j >= 0 else len(text)
            continue
        n += 1
        i += 1
    return n


def _fit(text, cols):
    """Cut to the terminal width without splitting an escape sequence."""
    if _visible_len(text) <= cols:
        return text
    out, n, i = [], 0, 0
    while i < len(text) and n < cols - 1:
        if text[i] == "\x1b":
            j = text.find("m", i)
            out.append(text[i:j + 1])
            i = j + 1
            continue
        out.append(text[i])
        n += 1
        i += 1
    return "".join(out) + "…" + ESC + "0m"


def _pad(text, width):
    if len(text) > width:
        return text[:max(0, width - 1)] + "…"
    return text + " " * (width - len(text))


BOLD, DIM, REV, RESET = ESC + "1m", ESC + "2m", ESC + "7m", ESC + "0m"
GREEN, RED, YELLOW, CYAN = ESC + "32m", ESC + "31m", ESC + "33m", ESC + "36m"


def fmt_duration(seconds):
    if not seconds:
        return ""
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}" if seconds >= 3600 \
        else f"{seconds // 60}:{seconds % 60:02d}"


# -- the picker ----------------------------------------------------------------------------

class Option:
    def __init__(self, label, values, current, shown=None, visible=lambda opts: True):
        self.label, self.values, self.shown, self.visible = label, list(values), shown, visible
        self.index = self.values.index(current) if current in self.values else 0

    @property
    def value(self):
        return self.values[self.index]

    def step(self, delta):
        self.index = (self.index + delta) % len(self.values)

    def text(self):
        return (self.shown or {}).get(self.value, str(self.value))


class Picker:
    """State of the selection screen; pure logic, so it can be tested without a terminal."""

    def __init__(self, title, items, options, checked=None):
        self.title = title
        self.items = items                    # [{"label", "right"}]
        self.checked = list(checked) if checked is not None else [True] * len(items)
        self.options = options                # {key: Option}
        self.cursor = 0
        self.top = 0

    def rows(self):
        return len(self.items) + len(self.visible_options())

    def visible_options(self):
        return [(k, o) for k, o in self.options.items() if o.visible(self.options)]

    def on_track(self):
        return self.cursor < len(self.items)

    def handle(self, key, page=10):
        """Returns 'start', 'cancel' or None."""
        last = self.rows() - 1
        if key == UP:
            self.cursor = max(0, self.cursor - 1)
        elif key == DOWN:
            self.cursor = min(last, self.cursor + 1)
        elif key == HOME:
            self.cursor = 0
        elif key == END:
            self.cursor = last
        elif key == PGUP or (key == LEFT and self.on_track()):
            self.cursor = max(0, self.cursor - page)
        elif key == PGDN or (key == RIGHT and self.on_track()):
            self.cursor = min(len(self.items) - 1 if self.on_track() else last, self.cursor + page)
        elif key in (LEFT, RIGHT) and not self.on_track():
            _, opt = self.visible_options()[self.cursor - len(self.items)]
            opt.step(-1 if key == LEFT else 1)
            self.cursor = min(self.cursor, self.rows() - 1)
        elif key == SPACE:
            if self.on_track():
                self.checked[self.cursor] = not self.checked[self.cursor]
                self.cursor = min(len(self.items) - 1, self.cursor + 1)
            else:
                _, opt = self.visible_options()[self.cursor - len(self.items)]
                opt.step(1)
        elif key == "a":
            self.checked = [True] * len(self.items)
        elif key == "n":
            self.checked = [False] * len(self.items)
        elif key == "i":
            self.checked = [not c for c in self.checked]
        elif key == ENTER:
            return "start" if any(self.checked) else None
        elif key in (CANCEL, "q"):
            return "cancel"
        return None

    def lines(self, cols, rows):
        chosen = sum(self.checked)
        tracks, options = t("tracks"), t("options")
        head = [f"{BOLD}{self.title}{RESET}  {DIM}{t('chosen', n=chosen, total=len(self.items))}{RESET}",
                f"{DIM}{t('keys')}{RESET}",
                f"{CYAN}─ {tracks} {'─' * max(0, cols - len(tracks) - 3)}{RESET}"]
        opts = self.visible_options()
        foot = [f"{CYAN}─ {options} {'─' * max(0, cols - len(options) - 3)}{RESET}"]
        width = max(len(o.label) for _, o in opts) if opts else 0
        for j, (_, o) in enumerate(opts):
            row = len(self.items) + j
            line = f"   {o.label.ljust(width)}   ◀ {BOLD}{o.text()}{RESET} ▶"
            foot.append(REV + _strip(line) + RESET if row == self.cursor else line)
        if chosen == 0:
            foot.append(f"{YELLOW}   {t('none_chosen')}{RESET}")
        list_rows = max(3, rows - len(head) - len(foot))
        overflow = len(self.items) > list_rows
        if overflow:
            list_rows -= 1                        # one row for the "… more ↓" line, never over the cursor
        if self.on_track():
            if self.cursor < self.top:
                self.top = self.cursor
            elif self.cursor >= self.top + list_rows:
                self.top = self.cursor - list_rows + 1
        self.top = max(0, min(self.top, max(0, len(self.items) - list_rows)))
        body = []
        num_w = len(str(len(self.items)))
        for i in range(self.top, min(len(self.items), self.top + list_rows)):
            item = self.items[i]
            mark = f"{GREEN}[x]{RESET}" if self.checked[i] else "[ ]"
            right = item.get("right", "")
            label_w = max(10, cols - 12 - num_w - len(right))
            text = f" {'▸' if i == self.cursor else ' '} {mark} {str(i + 1).rjust(num_w)}  " \
                   f"{_pad(item['label'], label_w)} {DIM}{right}{RESET}"
            body.append(REV + _strip(text) + RESET if i == self.cursor else text)
        if overflow:
            above, below = self.top, len(self.items) - self.top - list_rows
            body.append(f"{DIM}   {t('above', n=above) if above else ''}"
                        f"{'   ' if above and below else ''}{t('below', n=below) if below else ''}{RESET}")
        return head + body + foot


def _strip(text):
    out, i = [], 0
    while i < len(text):
        if text[i] == "\x1b":
            i = text.find("m", i) + 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def pick(title, items, options, checked=None):
    """Run the picker. Returns (indices, {key: value}) or None when cancelled."""
    picker = Picker(title, items, options, checked)
    with _Screen() as screen, _Raw():
        while True:
            cols, rows = shutil.get_terminal_size((100, 30))
            screen.draw(picker.lines(cols, rows))
            result = picker.handle(read_key(), page=max(5, rows - 12))
            if result == "cancel":
                return None
            if result == "start":
                chosen = [i for i, c in enumerate(picker.checked) if c]
                return chosen, {k: o.value for k, o in picker.options.items()}


# -- the live board ------------------------------------------------------------------------

class _Log:
    """stderr replacement while the board is up: keeps the last lines, thread-safe."""

    def __init__(self, keep=400):
        self.lines = collections.deque(maxlen=keep)
        self.partial = ""
        self.lock = threading.Lock()

    def write(self, text):
        with self.lock:
            text = self.partial + text.replace("\r", "\n")
            *done, self.partial = text.split("\n")
            self.lines.extend(l for l in done if l.strip())
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return False


class Board:
    """Live table of jobs: queued / working / done / skipped / failed, plus the log tail."""

    SYMBOLS = {"queued": f"{DIM}·{RESET}", "working": f"{YELLOW}↓{RESET}", "done": f"{GREEN}✓{RESET}",
               "skipped": f"{CYAN}={RESET}", "failed": f"{RED}✗{RESET}"}

    def __init__(self, title, labels, numbers=None):
        self.title = title
        self.labels = labels
        self.numbers = [str(n) for n in numbers] if numbers else [str(i + 1) for i in range(len(labels))]
        self.state = ["queued"] * len(labels)
        self.started = [0.0] * len(labels)
        self.note = [""] * len(labels)
        self.lock = threading.Lock()
        self.t0 = time.time()
        self.log = _Log()
        self._stop = threading.Event()

    def set(self, i, state, note=""):
        with self.lock:
            self.state[i] = state
            self.note[i] = note
            if state == "working":
                self.started[i] = time.time()

    def lines(self, cols, rows):
        counts = collections.Counter(self.state)
        spin = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.time() * 10) % 10]
        head = [f"{BOLD}{self.title}{RESET}  {GREEN}{t('n_done', n=counts['done'])}{RESET}  "
                f"{YELLOW}{t('n_working', n=counts['working'])}{RESET}  {DIM}{t('n_queued', n=counts['queued'])}{RESET}"
                + (f"  {CYAN}{t('n_skipped', n=counts['skipped'])}{RESET}" if counts['skipped'] else "")
                + (f"  {RED}{t('n_failed', n=counts['failed'])}{RESET}" if counts['failed'] else "")
                + f"   {DIM}{t('seconds', n=f'{time.time() - self.t0:.0f}')}{RESET}",
                f"{CYAN}{'─' * cols}{RESET}"]
        log_rows = min(6, max(2, rows // 4))
        list_rows = max(3, rows - len(head) - log_rows - 1)
        # keep the active part in view: first non-finished entry near the top
        first_open = next((i for i, s in enumerate(self.state) if s in ("working", "queued")), len(self.state))
        top = max(0, min(first_open - 2, len(self.labels) - list_rows))
        body = []
        num_w = max((len(n) for n in self.numbers), default=1)
        for i in range(top, min(len(self.labels), top + list_rows)):
            state = self.state[i]
            sym = f"{YELLOW}{spin}{RESET}" if state == "working" else self.SYMBOLS[state]
            extra = ""
            if state == "working":
                extra = f"{DIM}{t('seconds', n=f'{time.time() - self.started[i]:.0f}')}{RESET}"
            elif self.note[i]:
                extra = (RED if state == "failed" else DIM) + self.note[i] + RESET
            label_w = max(10, min(70, cols - 12 - num_w))
            body.append(f" {sym} {self.numbers[i].rjust(num_w)}  {_pad(self.labels[i], label_w)}  {extra}")
        with self.log.lock:
            tail = list(self.log.lines)[-log_rows:]
        foot = [f"{CYAN}{'─' * cols}{RESET}"] + [f"{DIM}{l}{RESET}" for l in tail]
        return head + body + foot

    def run(self, work):
        """Run work() with the board on screen; stderr goes to the log while it runs."""
        old_err = sys.stderr
        sys.stderr = self.log
        result = {}
        thread = threading.Thread(target=lambda: result.setdefault("v", work()), daemon=True)
        try:
            with _Screen() as screen:
                thread.start()
                while thread.is_alive():
                    cols, rows = shutil.get_terminal_size((100, 30))
                    screen.draw(self.lines(cols, rows))
                    thread.join(0.1)
                cols, rows = shutil.get_terminal_size((100, 30))
                screen.draw(self.lines(cols, rows))
        finally:
            sys.stderr = old_err
        return result.get("v")
