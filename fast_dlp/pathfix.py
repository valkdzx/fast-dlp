"""Make the `fast-dlp` command work after `pip install fast-dlp`.

pip writes fast-dlp.exe into Python's Scripts folder, and that folder is often not on PATH:
Python installed without "Add python.exe to PATH", `pip install --user`, Microsoft Store
Python. A wheel cannot run code when it is installed, so the fix runs the first time
fast-dlp runs through Python - `python -m fast_dlp`, which is what works when `fast-dlp`
is "not recognized" - and on request as `fast-dlp path`.

Windows: the folder is appended to the user's PATH (HKCU\\Environment, the value kind kept)
and running programs are told about it, so a new terminal finds `fast-dlp`. Done once; set
FAST_DLP_NO_PATH=1 to never touch PATH. Elsewhere the needed shell line is printed.
"""
import os
import sys
import sysconfig

from .i18n import t

LAUNCHERS = ("fast-dlp.exe", "fast-dlp")
MARKER = "path-added"


def _norm(path):
    return os.path.normcase(os.path.normpath(os.path.expandvars(path.strip().strip('"')))) if path.strip() else ""


def scripts_dir():
    """The folder holding the fast-dlp launcher pip wrote, or None (running from source)."""
    candidates = []
    try:
        candidates.append(sysconfig.get_path("scripts"))
    except KeyError:
        pass
    try:
        user = sysconfig.get_preferred_scheme("user")          # 3.10+
    except AttributeError:
        user = f"{os.name}_user"
    try:
        candidates.append(sysconfig.get_path("scripts", user))
    except KeyError:
        pass
    candidates.append(os.path.dirname(sys.executable))         # venvs on POSIX, some installs
    for folder in candidates:
        if folder and any(os.path.isfile(os.path.join(folder, name)) for name in LAUNCHERS):
            return folder
    return None


def _registry_paths():
    """User and system PATH as stored - what a new terminal gets - on Windows."""
    import winreg
    out = []
    for root, key in ((winreg.HKEY_CURRENT_USER, "Environment"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(root, key) as k:
                out.append(winreg.QueryValueEx(k, "Path")[0])
        except OSError:
            pass
    return ";".join(out)


def on_path(folder):
    entries = os.environ.get("PATH", "")
    if os.name == "nt":
        entries += ";" + _registry_paths()
    want = _norm(folder)
    return any(_norm(p) == want for p in entries.split(os.pathsep if os.name != "nt" else ";"))


def _add_windows(folder):
    import ctypes
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                        winreg.KEY_READ | winreg.KEY_WRITE) as k:
        try:
            value, kind = winreg.QueryValueEx(k, "Path")
        except FileNotFoundError:
            value, kind = "", winreg.REG_EXPAND_SZ
        if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
            kind = winreg.REG_EXPAND_SZ
        parts = [p for p in value.split(";") if p.strip()]
        if any(_norm(p) == _norm(folder) for p in parts):
            return
        winreg.SetValueEx(k, "Path", 0, kind, ";".join(parts + [folder]))
    # Explorer (and terminals started from it) re-read the environment on this message.
    result = ctypes.c_ulong()
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 3000,
                                             ctypes.byref(result))
    os.environ["PATH"] = os.environ.get("PATH", "") + ";" + folder


def _marker():
    from .utils import config_dir
    return os.path.join(config_dir(), MARKER)


def ensure(force=False, out=sys.stderr):
    """Put the launcher's folder on PATH if it is not there. Returns True if PATH changed."""
    if os.environ.get("FAST_DLP_NO_PATH") and not force:
        return False
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        if force:                            # a virtualenv: its Scripts are on PATH while it is active
            print(t("path_venv"), file=out)
        return False
    folder = scripts_dir()
    if not folder:
        if force:
            print(t("path_no_launcher"), file=out)
        return False
    if on_path(folder):
        if force:
            print(t("path_ok", folder=folder), file=out)
        return False
    if os.name != "nt":
        print(t("path_posix", folder=folder), file=out)
        return False
    marker = _marker()
    if os.path.exists(marker) and not force:
        return False                         # added once already: the user took it out on purpose
    try:
        _add_windows(folder)
    except OSError as e:
        print(t("path_failed", folder=folder, error=e), file=out)
        return False
    try:
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(folder + "\n")
    except OSError:
        pass
    print(t("path_added", folder=folder), file=out)
    return True
