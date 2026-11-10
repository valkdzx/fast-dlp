"""Small stdlib HTTP helpers for API calls (downloads use download.py)."""
import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

SSL_CONTEXT = ssl.create_default_context()
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")

_opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=SSL_CONTEXT))
PROXY = None            # (host, port, auth_header or None) for the downloader's CONNECT tunnels


def proxy_handlers():
    """Extra urllib handlers for code that builds its own opener (cookie jars...)."""
    if not PROXY:
        return []
    return [urllib.request.ProxyHandler({"http": PROXY[3], "https": PROXY[3]})]


def set_proxy(url):
    """Send everything through an HTTP(S) proxy: http://[user:pass@]host:port"""
    global _opener, PROXY
    import base64
    parsed = urllib.parse.urlsplit(url if "://" in url else "http://" + url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("only http:// proxies are supported (SOCKS needs a library)")
    auth = None
    if parsed.username:
        token = base64.b64encode(f"{urllib.parse.unquote(parsed.username)}:"
                                 f"{urllib.parse.unquote(parsed.password or '')}".encode()).decode()
        auth = "Basic " + token
    PROXY = (parsed.hostname, parsed.port or 8080, auth, url)
    _opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": url, "https": url}),
                                          urllib.request.HTTPSHandler(context=SSL_CONTEXT))

# YouTube judges each address on its own: a home connection's IPv6 address (one per
# device, and the one Windows prefers) can be bot-checked while the shared IPv4 one
# is served fine. Stream URLs are bound to the address that asked for them, so the
# whole process switches family at once - API calls and downloads alike.
_original_getaddrinfo = socket.getaddrinfo
_family = 0


def set_family(family):
    """0 for the system default, socket.AF_INET or socket.AF_INET6 to force one."""
    global _family
    _family = family

    def getaddrinfo(host, port, family=0, *args, **kwargs):
        return _original_getaddrinfo(host, port, _family or family, *args, **kwargs)

    socket.getaddrinfo = getaddrinfo if family else _original_getaddrinfo


def family():
    return _family


def has_ipv4():
    try:
        return bool(_original_getaddrinfo("www.youtube.com", 443, socket.AF_INET))
    except OSError:
        return False


def request(url, data=None, headers=None, method=None, timeout=20):
    """Return (status, body_bytes). Never raises on HTTP status codes."""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def get_text(url, headers=None, timeout=20):
    hdrs = {"User-Agent": BROWSER_UA}
    hdrs.update(headers or {})
    status, body = request(url, headers=hdrs, timeout=timeout)
    if status != 200:
        raise IOError(f"{url} answered {status}")
    return body.decode("utf-8", "replace")


def post_json(url, payload, headers=None, timeout=20):
    hdrs = {"content-type": "application/json"}
    hdrs.update(headers or {})
    status, body = request(url, json.dumps(payload).encode(), hdrs, "POST", timeout)
    try:
        parsed = json.loads(body or b"{}")
    except ValueError:
        parsed = {}
    return status, parsed


def post_form(url, fields, headers=None, timeout=20):
    hdrs = {"content-type": "application/x-www-form-urlencoded"}
    hdrs.update(headers or {})
    data = urllib.parse.urlencode(fields).encode()
    status, body = request(url, data, hdrs, "POST", timeout)
    try:
        parsed = json.loads(body or b"{}")
    except ValueError:
        parsed = {}
    return status, parsed
