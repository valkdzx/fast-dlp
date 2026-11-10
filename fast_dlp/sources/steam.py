"""Steam Workshop items and collections.

Items stored the old way (UFS: screenshots, many older games) have a public `file_url`
and download directly. SteamPipe items do not; they are fetched with Valve's steamcmd
(anonymous login works for games that allow it) when it is installed.
"""
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile

from .. import net
from ..utils import FastDlpError, cache_dir, debug, find_tool
from .common import direct, get_json, load_config, save_config

API = "https://api.steampowered.com/ISteamRemoteStorage"
_ID = re.compile(r"steamcommunity\.com/(?:sharedfiles|workshop)/filedetails/?\?(?:[^#]*&)?id=(\d+)", re.I)


def match(url):
    return bool(_ID.search(url))


def details(ids):
    data = {"itemcount": len(ids)}
    data.update({f"publishedfileids[{i}]": x for i, x in enumerate(ids)})
    status, body = get_json(f"{API}/GetPublishedFileDetails/v1/", data=data, method="POST")
    if status != 200:
        raise FastDlpError(f"Steam answered HTTP {status}")
    return body["response"].get("publishedfiledetails") or []


def collection(cid):
    status, body = get_json(f"{API}/GetCollectionDetails/v1/",
                            data={"collectioncount": 1, "publishedfileids[0]": cid}, method="POST")
    if status != 200:
        return []
    found = (body["response"].get("collectiondetails") or [{}])[0]
    return [c["publishedfileid"] for c in found.get("children") or []]


def _job(d):
    item = d["publishedfileid"]
    title = d.get("title") or os.path.splitext(os.path.basename(d.get("filename") or ""))[0] or item
    app = d.get("consumer_app_id")

    def resolve():
        if d.get("file_url"):
            name = os.path.basename(d.get("filename") or "") or f"{item}.bin"
            ext = name.rsplit(".", 1)[-1] if "." in name else "bin"
            return {"kind": "file", "url": d["file_url"], "headers": {}, "ext": ext,
                    "size": int(d.get("file_size") or 0)}
        return {"kind": "steamcmd", "app": app, "item": item}

    return direct("steam", item, title, str(app or ""), resolve, url=f"https://steamcommunity.com/sharedfiles/filedetails/?id={item}")


def expand(url, args):
    item = _ID.search(url).group(1)
    children = collection(item)
    if children:
        found = details(children)
        title = (details([item]) or [{}])[0].get("title") or f"collection {item}"
        return title, [_job(d) for d in found if d.get("result") == 1]
    found = details([item])
    if not found or found[0].get("result") != 1:
        raise FastDlpError(f"Steam Workshop item {item} not found or private")
    return None, [_job(found[0])]


STEAMCMD_URLS = {
    "win32": "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip",
    "linux": "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz",
    "darwin": "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_osx.tar.gz",
}


def _own_steamcmd_dir():
    return os.path.join(cache_dir(), "steamcmd")


def ensure_steamcmd():
    """steamcmd from PATH / bin/, else Valve's official package, fetched once into the cache."""
    found = find_tool("steamcmd")
    if found:
        return found
    folder = _own_steamcmd_dir()
    exe = os.path.join(folder, "steamcmd.exe" if os.name == "nt" else "steamcmd.sh")
    if os.path.exists(exe):
        return exe
    url = STEAMCMD_URLS.get(sys.platform) or STEAMCMD_URLS["linux"]
    print(f"[steam] fetching steamcmd from Valve ({url}) ...", file=sys.stderr)
    status, body = net.request(url, timeout=60)
    if status != 200:
        raise FastDlpError(f"could not download steamcmd: HTTP {status}")
    os.makedirs(folder, exist_ok=True)
    if url.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            z.extractall(folder)
    else:
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as t:
            t.extractall(folder, filter="data")
    if not os.path.exists(exe):
        raise FastDlpError("the steamcmd package did not contain the expected program")
    if os.name != "nt":
        os.chmod(exe, 0o755)
    # First start downloads steamcmd's own runtime (~20-40 MB); do it once, visibly.
    print("[steam] first start: steamcmd updates itself (once, about a minute) ...", file=sys.stderr)
    subprocess.run([exe, "+quit"], capture_output=True)
    return exe


def steam_user(explicit=None):
    """--steam-user, else the account remembered from last time (steamcmd caches its login).
    --steam-user anonymous forgets the remembered account."""
    if explicit and explicit.lower() == "anonymous":
        save_config("steam.json", {})
        return None
    if explicit:
        save_config("steam.json", {"user": explicit})
        return explicit
    return load_config("steam.json").get("user")


def _content_dir(exe, app, item):
    return os.path.join(os.path.dirname(exe), "steamapps", "workshop", "content", str(app), str(item))


def steamcmd_download(app, item, out_dir, name, user=None):
    """Download a SteamPipe Workshop item with steamcmd; returns the folder with its files.

    Anonymous login works for games that allow it (many free and server-side ones).
    Paid games (Wallpaper Engine, ...) need an account that owns the game: steamcmd then asks
    for the password and Steam Guard code itself, in this terminal, and remembers the login.
    """
    exe = ensure_steamcmd()
    login = ["+login", user] if user else ["+login", "anonymous"]
    cmd = [exe, *login, "+workshop_download_item", str(app), str(item), "validate", "+quit"]
    debug("steamcmd: " + " ".join(cmd))
    content = _content_dir(exe, app, item)
    if user:
        print(f"[steam] steamcmd signs in as {user}: if it asks, type the password / Steam Guard "
              "code below (once - steamcmd remembers the login)", file=sys.stderr)
        proc = subprocess.run(cmd)                      # interactive: user types into steamcmd
        out = ""
    else:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        out = proc.stdout or ""
    m = re.search(r'Success\. Downloaded item \d+ to "([^"]+)"', out)
    source = m.group(1) if m else content
    if not os.path.isdir(source) or not os.listdir(source):
        tail = (out.strip().splitlines() or [""])[-1]
        if not user:
            raise FastDlpError(
                f"steamcmd could not download {item} anonymously ({tail or 'refused'}). This game "
                "only gives Workshop files to accounts that own it: run again with "
                "--steam-user YOUR_STEAM_LOGIN (password and Steam Guard are typed into steamcmd "
                "itself, once)")
        interactive = sys.stdin is not None and sys.stdin.isatty()
        hint = ("check the password / Steam Guard code typed above, and that this account owns the game"
                if interactive else
                "steamcmd needs to ask for the password / Steam Guard code: run this command in your "
                "own terminal (once; steamcmd then remembers the login)")
        raise FastDlpError(f"steamcmd could not sign in as {user} or download {item}: {hint}. "
                           "--steam-user anonymous goes back to anonymous downloads")
    target = os.path.join(out_dir, name)
    if os.path.exists(target):
        shutil.rmtree(target)
    shutil.copytree(source, target)
    return target
