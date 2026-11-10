import os
import re
import sys

_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
VIDEO_ID_RE = _VIDEO_ID
_URL_PATTERNS = [
    re.compile(r"(?:v=|/v/|/embed/|/shorts/|/live/|youtu\.be/|/e/)([A-Za-z0-9_-]{11})"),
]


def extract_video_id(text):
    text = text.strip()
    if _VIDEO_ID.match(text):
        return text
    for pattern in _URL_PATTERNS:
        found = pattern.search(text)
        if found:
            return found.group(1)
    return None


def _app_dir(env_var, fallback):
    base = os.environ.get(env_var) or os.path.expanduser(fallback)
    path = os.path.join(base, "fast-dlp")
    os.makedirs(path, exist_ok=True)
    return path


def cache_dir():
    if os.name == "nt":
        return _app_dir("LOCALAPPDATA", "~/AppData/Local")
    return _app_dir("XDG_CACHE_HOME", "~/.cache")


def config_dir():
    if os.name == "nt":
        return _app_dir("APPDATA", "~/AppData/Roaming")
    return _app_dir("XDG_CONFIG_HOME", "~/.config")


_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name, limit=180, edges=True):
    """Make one name safe. edges=False keeps trailing dots/spaces: for a value that ends up
    in the middle of a file name ("кризис." in "кризис. - тупой.mp3"), where they are legal."""
    name = _BAD_CHARS.sub("_", name).strip()
    if edges:
        name = name.rstrip(". ")
    if len(name) > limit:
        name = name[:limit].rstrip(". ") if edges else name[:limit]
    return name or ("video" if edges else "")


def sanitize_path(path):
    """Windows rejects a path component ending in '.' or ' '; fix each component."""
    parts = re.split(r"[\\/]", path)
    fixed = [p.rstrip(". ") or "_" for p in parts]
    return os.path.join(*fixed) if fixed else path


def human_size(n):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024 or unit == "GiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} GiB"


def err(msg):
    print(f"error: {msg}", file=sys.stderr)


def warn(msg):
    print(f"warning: {msg}", file=sys.stderr)


VERBOSE = False


def debug(msg):
    if VERBOSE:
        print(f"[debug] {msg}", file=sys.stderr)


def find_tool(name):
    """Find an executable: PATH, the project's bin/, then winget's package folders
    (a fresh winget install is not on PATH until the shell restarts)."""
    import glob
    import shutil
    found = shutil.which(name)
    if found:
        return found
    exe = name + (".exe" if os.name == "nt" else "")
    project_bin = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", exe)
    if os.path.isfile(project_bin):
        return project_bin
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "")
        for pattern in (os.path.join(local, "Microsoft", "WinGet", "Links", exe),
                        os.path.join(local, "Microsoft", "WinGet", "Packages", "*", exe),
                        os.path.join(local, "Microsoft", "WinGet", "Packages", "*", "*", "bin", exe)):
            hits = sorted(glob.glob(pattern))
            if hits:
                return hits[-1]
    return None


def allocate(path, size):
    """Create path at its final size without writing anything.

    Windows fills a grown file with zeros for real: truncate() to 3 GB writes 3 GB, and a
    write far past the end makes NTFS zero the gap first (28 s for 2 GiB here). That looked
    like a silent download before the real one. An NTFS sparse file takes its size at once;
    other systems already grow files sparsely."""
    with open(path, "wb") as fh:
        if os.name == "nt" and size:
            try:
                import ctypes
                import msvcrt
                from ctypes import wintypes
                k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                                wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                                wintypes.LPVOID]
                k32.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong,
                                                 ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD]
                k32.SetEndOfFile.argtypes = [wintypes.HANDLE]
                handle = msvcrt.get_osfhandle(fh.fileno())
                done = wintypes.DWORD()
                if (k32.DeviceIoControl(handle, 0x000900C4, None, 0, None, 0, ctypes.byref(done), None)  # FSCTL_SET_SPARSE
                        and k32.SetFilePointerEx(handle, size, None, 0) and k32.SetEndOfFile(handle)):
                    return
            except (OSError, AttributeError, ValueError):
                pass                       # FAT32/exFAT and the like: no sparse files
        fh.truncate(size)


class FastDlpError(Exception):
    pass
