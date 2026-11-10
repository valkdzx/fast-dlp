"""Signature / n-parameter solving for the TV client.

Same approach as CDisc Music's YoutubeCipher: inside the player's wrapper
function, top-level expression statements (side effects that need a browser)
are blanked, the URL-builder function (the one calling .set("alr","yes")) is
located, and a solver that builds a URL with that function and runs its first
own method is injected. The prepared script runs in an external JS runtime
(deno / node / bun), cached per player id.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse

from . import net
from .utils import FastDlpError, cache_dir, debug, find_tool

_PLAYER_ID = re.compile(r"\\?/s\\?/player\\?/([0-9a-f]{8})\\?/")
_TIMESTAMP = re.compile(r"(?:signatureTimestamp|sts)\s*:\s*(\d{5})")
_PLAYER_ID_TTL = 3600
# kikkia/yt-cipher public instance (yt-dlp ejs behind an HTTP API), ~10 req/s limit.
DEFAULT_REMOTE = "https://cipher.kikkia.dev/"

_SETUP = """var XMLHttpRequest={prototype:{}};
var location={hash:'',host:'www.youtube.com',hostname:'www.youtube.com',href:'https://www.youtube.com/watch?v=fastdlp',origin:'https://www.youtube.com',password:'',pathname:'/watch',port:'',protocol:'https:',search:'?v=fastdlp',username:''};
var document=Object.create(null);
var navigator=Object.create(null);
var window=Object.create(globalThis);window.location=location;window.document=document;window.navigator=navigator;window.XMLHttpRequest=XMLHttpRequest;
var self=window;
var _cdisc={};
"""

_RUNNER = r"""var __isDeno=typeof Deno!=="undefined";
var __args=__isDeno?Deno.args:process.argv.slice(2);
function __read(p){return __isDeno?Deno.readTextFileSync(p):require("fs").readFileSync(p,"utf8");}
var __cd=(new Function(__read(__args[0])))();
var __items=JSON.parse(__read(__args[1]));
var __out=__items.map(function(item){
  var answers={sig:null,n:null};
  var kinds=[];if(item.sig!=null)kinds.push("sig");if(item.n!=null)kinds.push("n");
  kinds.forEach(function(kind){
    var seen={},count=0,last=null,errors=[];
    __cd.solvers.forEach(function(solve){
      try{var inp={};inp[kind]=item[kind];var r=solve(inp)[kind];
        if(r!=null&&!seen[r]){seen[r]=1;count++;last=r;}}catch(e){errors.push(String(e));}
    });
    answers[kind]=count===1?last:null;
    if(count!==1)answers[kind+"_error"]="solved "+count+" ways "+errors.join("; ");
  });
  return answers;
});
console.log(JSON.stringify(__out));
"""

_KEEP_KEYWORDS = {
    "var", "let", "const", "function", "if", "for", "while", "do", "try", "switch",
    "return", "throw", "class", "else", "break", "continue", "catch", "finally",
    "async", "case", "default", "debugger", "with", "import", "export",
}
_REGEX_PREFIX_WORDS = {"return", "typeof", "case", "void", "in", "of", "delete",
                       "instanceof", "new", "else", "do", "throw", "yield", "await"}
_ASSIGN = re.compile(
    r"[A-Za-z_$][\w$]*(?:\s*(?:\.[\w$]+|\[(?:[^\[\]]|\[(?:[^\[\]]|\[[^\[\]]*\])*\])*\]))*\s*"
    r"(?:>>>|<<|>>|\*\*|&&|\|\||\?\?|[-+*/%&|^])?=(?!=)")
_LITERAL = re.compile(r"""(?:"[^"]*"|'[^']*'|\d[\w.]*|true|false|null)\s*$""")
_INTERESTING = re.compile(r"""[{}()\[\];'"`/]""")
_IDENT_CHAR = re.compile(r"[\w$]")


def _skip_string(src, i, quote):
    n = len(src)
    i += 1
    while i < n:
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c == quote:
            return i + 1
        i += 1
    return n


def _skip_regex(src, i):
    n = len(src)
    i += 1
    in_class = False
    while i < n:
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if in_class:
            if c == "]":
                in_class = False
        elif c == "[":
            in_class = True
        elif c == "/":
            i += 1
            while i < n and _IDENT_CHAR.match(src[i]):
                i += 1
            return i
        elif c == "\n":
            return i
        i += 1
    return n


def _slash_starts_regex(src, i):
    j = i - 1
    while j >= 0 and src[j] in " \t\r\n":
        j -= 1
    if j < 0:
        return True
    c = src[j]
    if c in ")]}":
        return False
    if _IDENT_CHAR.match(c):
        k = j
        while k >= 0 and _IDENT_CHAR.match(src[k]):
            k -= 1
        return src[k + 1:j + 1] in _REGEX_PREFIX_WORDS
    return True


class _Scanner:
    """Walks JS source tracking bracket depth, skipping strings, comments, regexes, templates."""

    def __init__(self, src):
        self.src = src

    def statements(self, start, end):
        """Yield (s, e) spans of top-level statements in src[start:end]."""
        src = self.src
        depth = 0
        stmt_start = start
        stmt_keyword = None
        i = start
        template_stack = []  # brace depth at which each ${ was opened
        while i < end:
            m = _INTERESTING.search(src, i, end)
            if not m:
                break
            i = m.start()
            c = src[i]
            if stmt_keyword is None and depth == 0:
                stmt_keyword = self._first_word(stmt_start, i)
            if c in "\"'":
                i = _skip_string(src, i, c)
                continue
            if c == "`":
                i = self._skip_template(i, template_stack, depth)
                continue
            if c == "/":
                nxt = src[i + 1] if i + 1 < end else ""
                if nxt == "/":
                    nl = src.find("\n", i)
                    i = end if nl < 0 else nl
                    continue
                if nxt == "*":
                    close = src.find("*/", i + 2)
                    i = end if close < 0 else close + 2
                    continue
                if _slash_starts_regex(src, i):
                    i = _skip_regex(src, i)
                else:
                    i += 1
                continue
            if c in "([{":
                depth += 1
                i += 1
                continue
            if c in ")]}":
                if c == "}" and template_stack and template_stack[-1] == depth:
                    template_stack.pop()
                    i = self._skip_template(i, template_stack, depth, resume=True)
                    continue
                depth -= 1
                i += 1
                if c == "}" and depth == 0 and stmt_keyword in _BLOCK_STATEMENTS:
                    rest = src[i:i + 12].lstrip()
                    if not re.match(r"(else|catch|finally|while)\b", rest) and not rest.startswith(";"):
                        if not (stmt_keyword == "do"):
                            yield stmt_start, i
                            stmt_start, stmt_keyword = i, None
                continue
            if c == ";":
                i += 1
                if depth == 0:
                    yield stmt_start, i
                    stmt_start, stmt_keyword = i, None
                continue
            i += 1
        if stmt_start < end and src[stmt_start:end].strip():
            yield stmt_start, end

    def _first_word(self, s, e):
        text = _strip_lead(self.src[s:e])
        m = re.match(r"[A-Za-z_$][\w$]*|\S", text)
        return m.group(0) if m else ""

    def _skip_template(self, i, stack, depth, resume=False):
        src = self.src
        n = len(src)
        i += 1
        while i < n:
            c = src[i]
            if c == "\\":
                i += 2
                continue
            if c == "`":
                return i + 1
            if c == "$" and i + 1 < n and src[i + 1] == "{":
                stack.append(depth)
                return i + 2
            i += 1
        return n


_LEAD = re.compile(r"(?:\s+|/\*.*?\*/|//[^\n]*)*", re.S)


def _strip_lead(text):
    """Drop leading whitespace and comments."""
    return text[_LEAD.match(text).end():]


_BLOCK_STATEMENTS = {"function", "if", "else", "for", "while", "try", "switch", "{", "class", "do", "with"}


def _wrapper_body(src):
    """Locate the body of `(function(g){ ... })(_yt_player)`: return (open_brace, close_brace)."""
    m = re.search(r"\(function\(\s*[\w$]+\s*\)\s*\{", src)
    if not m:
        raise FastDlpError("the player is not wrapped in a function")
    open_brace = m.end() - 1
    tail = re.search(r"\}\s*\)\s*\(\s*_yt_player\s*\)\s*;?\s*$", src)
    if not tail:
        raise FastDlpError("the player body ends oddly")
    return open_brace, tail.start()


def prepare_player(src):
    """Return JS source (a function body) that evaluates the player and returns _cdisc."""
    open_brace, close_brace = _wrapper_body(src)
    out = list(src)
    scanner = _Scanner(src)
    names = []
    first = True
    for s, e in scanner.statements(open_brace + 1, close_brace):
        text = _strip_lead(src[s:e]).rstrip()
        if not text:
            continue
        if first:
            first = False
            if re.match(r"var\s+window\s*=\s*this\s*;?$", text):
                _blank(out, s, e)
                continue
        word = scanner._first_word(s, e)
        if word in _KEEP_KEYWORDS:
            if word in ("function", "var", "let", "const"):
                name = _builder_name(text)
                if name:
                    names.append(name)
            continue
        if _ASSIGN.match(text):
            name = _builder_name(text)
            if name:
                names.append(name)
            continue
        if _LITERAL.match(text.rstrip(";")):
            continue
        _blank(out, s, e)
    if not names:
        raise FastDlpError("no URL builder in the player")
    injected = ";_cdisc.solvers=[" + ",".join(_solver_for(n) for n in names) + "];"
    prepared = "".join(out[:close_brace]) + injected + "".join(out[close_brace:])
    return _SETUP + prepared + "\nreturn _cdisc;"


_ALR = re.compile(r"""\.set\(\s*["']alr["']\s*,\s*["']yes["']\s*\)""")


_DIRECT_ALR = re.compile(r"""(?<=[;{])\s*[A-Za-z_$][\w$]*\.set\(\s*["']alr["']\s*,\s*["']yes["']\s*\)\s*[;}]""")


def _builder_name(text):
    if not _ALR.search(text):
        return None
    # Like yt-dlp ejs / CDisc: the a.set("alr","yes") call must be a statement directly in
    # the function's own body, not nested in an inner function or a conditional.
    body = text.find("{")
    if body < 0:
        return None
    direct = False
    for found in _DIRECT_ALR.finditer(text, body):
        between = text[body:found.start()]
        if between.count("{") - between.count("}") == 1:
            direct = True
            break
    if not direct:
        return None
    m = re.match(r"(?:function\s+([\w$]+)\s*\(|(?:var|let|const)\s+([\w$]+)\s*=\s*function\b|"
                 r"([A-Za-z_$][\w$.]*)\s*=\s*function\b)", text)
    if not m:
        return None
    return m.group(1) or m.group(2) or m.group(3)


def _solver_for(builder):
    return ("function(input){"
            "var url=(" + builder + ")('https://youtube.com/watch?v=fastdlp','s',"
            "input.sig?encodeURIComponent(input.sig):undefined);"
            "url.set('n',input.n);"
            "var proto=Object.getPrototypeOf(url);"
            "var keys=Object.keys(proto).concat(Object.getOwnPropertyNames(proto));"
            "for(var i=0;i<keys.length;i++){"
            "if(['constructor','set','get','clone'].indexOf(keys[i])<0){url[keys[i]]();break;}}"
            "var s=url.get('s');var n=url.get('n');"
            "return {sig:s?decodeURIComponent(s):null,n:n==null?null:n};}")


def _blank(out, s, e):
    out[s] = ";"
    for k in range(s + 1, e):
        if out[k] != "\n":
            out[k] = " "


# -- runtime / caching ------------------------------------------------------

def find_runtime(explicit=None):
    for c in (explicit, os.environ.get("FAST_DLP_JS")):
        if c:
            path = shutil.which(c) or (c if os.path.isfile(c) else None)
            if path:
                return path
    for name in ("deno", "node", "bun"):
        path = find_tool(name)
        if path:
            return path
    return None


_CACHE_LOCK = threading.RLock()


def _locked(method):
    """Cache-filling steps run one at a time (parallel playlist jobs share one Cipher)."""
    def wrapper(*args, **kwargs):
        with _CACHE_LOCK:
            return method(*args, **kwargs)
    return wrapper


class Cipher:
    def __init__(self, runtime=None, mode="auto", remote_url=DEFAULT_REMOTE, remote_password=None):
        self.dir = os.path.join(cache_dir(), "player")
        os.makedirs(self.dir, exist_ok=True)
        self.runtime_hint = runtime
        self.mode = mode
        self.remote_url = remote_url
        self.remote_password = remote_password or os.environ.get("FAST_DLP_CIPHER_PASSWORD")
        self._pid = None

    @_locked
    def player_id(self):
        if self._pid:
            return self._pid
        meta_path = os.path.join(self.dir, "current.json")
        try:
            with open(meta_path, encoding="utf-8") as fh:
                meta = json.load(fh)
            if time.time() - meta["fetched"] < _PLAYER_ID_TTL:
                self._pid = meta["id"]
                return self._pid
        except (OSError, ValueError, KeyError):
            pass
        found = _PLAYER_ID.search(net.get_text("https://www.youtube.com/iframe_api"))
        if not found:
            raise FastDlpError("the iframe API named no player")
        self._pid = found.group(1)
        with open(meta_path, "w", encoding="utf-8") as fh:
            json.dump({"id": self._pid, "fetched": time.time()}, fh)
        return self._pid

    @_locked
    def _base_js(self, pid):
        path = os.path.join(self.dir, pid + ".js")
        if not os.path.exists(path):
            src = net.get_text(f"https://www.youtube.com/s/player/{pid}/player_ias.vflset/en_US/base.js")
            with open(path + ".tmp", "w", encoding="utf-8", newline="") as fh:
                fh.write(src)
            os.replace(path + ".tmp", path)
        with open(path, encoding="utf-8", newline="") as fh:
            return fh.read()

    @_locked
    def signature_timestamp(self):
        pid = self.player_id()
        sts_path = os.path.join(self.dir, pid + ".sts")
        try:
            with open(sts_path) as fh:
                return int(fh.read())
        except (OSError, ValueError):
            pass
        m = _TIMESTAMP.search(self._base_js(pid))
        sts = int(m.group(1)) if m else 0
        with open(sts_path, "w") as fh:
            fh.write(str(sts))
        return sts

    @_locked
    def _solver_path(self, pid):
        path = os.path.join(self.dir, pid + ".solver.js")
        if not os.path.exists(path):
            t = time.time()
            prepared = prepare_player(self._base_js(pid))
            with open(path + ".tmp", "w", encoding="utf-8", newline="") as fh:
                fh.write(prepared)
            os.replace(path + ".tmp", path)
            debug(f"prepared player {pid} in {time.time() - t:.2f}s")
        runner = os.path.join(self.dir, "runner.js")
        if not os.path.exists(runner) or open(runner, encoding="utf-8").read() != _RUNNER:
            with open(runner, "w", encoding="utf-8", newline="") as fh:
                fh.write(_RUNNER)
        return path, runner

    def solve(self, items):
        """items: list of {"sig": str|None, "n": str|None}; returns same-length list of answers.
        Local JS runtime first; kikkia's yt-cipher server when that is missing or fails."""
        mode = self.mode
        if mode in ("auto", "local"):
            try:
                return self._solve_local(items)
            except FastDlpError as e:
                if mode == "local" or not self.remote_url:
                    raise
                debug(f"local solver failed ({e}); asking {self.remote_url}")
        return self._solve_remote(items)

    def _solve_remote(self, items):
        endpoint = self.remote_url.rstrip("/") + "/decrypt_signature"
        player_url = f"https://www.youtube.com/s/player/{self.player_id()}/player_ias.vflset/en_US/base.js"
        headers = {"User-Agent": "fast-dlp"}
        if self.remote_password:
            headers["Authorization"] = self.remote_password
        out = []
        t = time.time()
        for item in items:
            status, data = net.post_json(endpoint, {
                "encrypted_signature": item.get("sig") or "",
                "n_param": item.get("n") or "",
                "player_url": player_url,
            }, headers)
            if status == 429:
                time.sleep(1)
                status, data = net.post_json(endpoint, {
                    "encrypted_signature": item.get("sig") or "",
                    "n_param": item.get("n") or "",
                    "player_url": player_url,
                }, headers)
            if status != 200:
                raise FastDlpError(f"remote cipher answered HTTP {status}: {str(data)[:200]}")
            out.append({"sig": data.get("decrypted_signature") or None if item.get("sig") else None,
                        "n": data.get("decrypted_n_sig") or None if item.get("n") else None})
        debug(f"remote cipher solved {len(items)} items in {time.time() - t:.2f}s")
        return out

    def _solve_local(self, items):
        runtime = find_runtime(self.runtime_hint)
        if not runtime:
            raise FastDlpError("deciphering TV stream URLs needs a JS runtime: install Deno "
                               "(winget install DenoLand.Deno) or Node.js, or pass --js-runtime")
        pid = self.player_id()
        solver, runner = self._solver_path(pid)
        fd, inp = tempfile.mkstemp(suffix=".json", prefix="fastdlp-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(items, fh)
            name = os.path.basename(runtime).lower()
            cmd = [runtime]
            if name.startswith("deno"):
                cmd += ["run", "--allow-read", "--no-prompt", "-q"]
            cmd += [runner, solver, inp]
            t = time.time()
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60)
            debug(f"JS solve ({os.path.basename(runtime)}) {len(items)} items in {time.time() - t:.2f}s")
        finally:
            os.unlink(inp)
        if proc.returncode != 0:
            raise FastDlpError(f"JS solver failed: {proc.stderr.strip()[-800:]}")
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def resolve_formats(self, formats):
        """Fill f["url"] for formats that have signatureCipher and/or an n parameter."""
        todo = []
        for f in formats:
            if "signatureCipher" in f or "cipher" in f:
                parts = urllib.parse.parse_qs(f.get("signatureCipher") or f.get("cipher"))
                url = parts["url"][0]
                todo.append((f, url, parts["s"][0], parts.get("sp", ["sig"])[0]))
            elif f.get("url") and _query_param(f["url"], "n"):
                todo.append((f, f["url"], None, None))
        if not todo:
            return
        n_values = sorted({urllib.parse.unquote(_query_param(u, "n")) for _, u, _, _ in todo if _query_param(u, "n")})
        items = [{"sig": s, "n": None} for _, _, s, _ in todo if s]
        items += [{"sig": None, "n": n} for n in n_values]
        answers = self.solve(items)
        sig_answers = iter(answers[:len(items) - len(n_values)])
        n_map = dict(zip(n_values, (a["n"] for a in answers[len(items) - len(n_values):])))
        for f, url, s, sp in todo:
            if s:
                a = next(sig_answers)
                if not a.get("sig"):
                    raise FastDlpError(f"signature not solved: {a.get('sig_error')}")
                url += ("&" if "?" in url else "?") + sp + "=" + urllib.parse.quote(a["sig"], safe="")
            n = _query_param(url, "n")
            if n:
                solved = n_map.get(urllib.parse.unquote(n))
                if solved:
                    url = re.sub(r"([?&])n=" + re.escape(n) + r"(?=&|$)",
                                 lambda m: m.group(1) + "n=" + urllib.parse.quote(solved, safe=""), url, count=1)
                else:
                    debug("n parameter not solved; download may be throttled")
            f["url"] = url


def _query_param(url, key):
    q = url.find("?")
    if q < 0:
        return None
    for pair in url[q + 1:].split("&"):
        if pair.startswith(key + "="):
            return pair[len(key) + 1:]
    return None
