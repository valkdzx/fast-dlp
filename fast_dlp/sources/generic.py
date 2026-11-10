"""Any direct http(s) file or .m3u8 playlist - the fallback after every known site."""
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from .. import net
from ..utils import FastDlpError
from .common import direct, ext_of

_HTTP = re.compile(r"^https?://", re.I)
_KNOWN = re.compile(r"(youtube\.com|youtu\.be|spotify\.com)", re.I)


def match(url):
    return bool(_HTTP.match(url)) and not _KNOWN.search(url)


def _probe(url):
    """(status, headers) of a 1-byte ranged GET: works where HEAD is refused."""
    req = urllib.request.Request(url, headers={"User-Agent": net.BROWSER_UA, "Range": "bytes=0-0"})
    try:
        with net._opener.open(req, timeout=20) as resp:
            return resp.status, dict(resp.headers), resp.geturl()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), url


def expand(url, args):
    path = urllib.parse.urlparse(url).path
    name = urllib.parse.unquote(os.path.basename(path)) or "download"
    stem = name.rsplit(".", 1)[0] if "." in name else name
    if path.lower().endswith(".m3u8"):
        spec = {"kind": "hls", "url": url, "headers": {}}
        return None, [direct("web", stem, stem, "", lambda: spec, url=url)]
    status, headers, final = _probe(url)
    ctype = (headers.get("Content-Type") or "").lower()
    if status >= 400:
        raise FastDlpError(f"{url} answered HTTP {status}")
    if "mpegurl" in ctype:
        spec = {"kind": "hls", "url": final, "headers": {}}
        return None, [direct("web", stem, stem, "", lambda: spec, url=url)]
    if "text/html" in ctype:
        raise FastDlpError(f"not a supported site or a direct file: {url}")
    size = 0
    rng = headers.get("Content-Range", "")
    if "/" in rng and rng.rsplit("/", 1)[1].isdigit():
        size = int(rng.rsplit("/", 1)[1])
    cd = headers.get("Content-Disposition") or ""
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd)
    if m:
        name = urllib.parse.unquote(m.group(1))
        stem = name.rsplit(".", 1)[0] if "." in name else name
    spec = {"kind": "file", "url": final, "headers": {}, "ext": ext_of(name, "bin"),
            "size": size, "ranges": status == 206}
    return None, [direct("web", stem, stem, "", lambda: spec, url=url)]
