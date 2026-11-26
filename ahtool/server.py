"""HTTP server: JSON API + static files from web/."""
import base64
import hashlib
import html as html_mod
import json
import mimetypes
import os
import re
import http.client
import io
import socket
import sys
import threading
import time
import traceback
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse, urlsplit

from . import alerts as alerts_mod
from . import analytics, gathering, market, notify, shopping, tooltip, userstate, webstate

# Web version: endpoints that only make sense on the PC that runs WoW, or that would expose your own trades.
WEB_BLOCKED = {
    ("POST", "/api/ingest"), ("POST", "/api/notify/test"), ("POST", "/api/alerts/evaluate"),
    ("GET", "/api/postings"), ("GET", "/api/market/my"),
    ("HEAD", "/api/postings"), ("HEAD", "/api/market/my"),
}
MAX_BODY = 64 << 10          # a normal API call: settings, a watchlist entry, ... (all a few hundred bytes)
# POST /api/x carries the visitor's whole browser state. Measured: a typical one is ~10-20 kB, one filled to every
# webstate.LIMITS (5000 known recipes, 500 watched items with 500-char notes, 200 alerts, 2000 alert states) ~630 kB.
MAX_TUNNEL_BODY = 1 << 20
MAX_TUNNEL_PARSE = 8         # /api/x bodies parsed at once (json.loads can need ~25x the body size in RAM)
TUNNEL_WAIT = 10             # seconds to wait for a parse slot before answering 503
# One sync batch unpacked. sync.py cuts row data into batches of MAX_BATCH_ROWS = 150k rows: measured 6.6-7.3 MB of
# JSON for prices/snapshots, ~10.5 MB for raw auctions; the static item/recipe/tooltip batch of a Classic client is
# ~10-15 MB. The server accepts at most 2x150k rows per batch (one huge scan), ~21 MB. 24 MB covers all of that;
# json.loads of a hostile batch can take ~25x its size in RAM, so pushes are also parsed one at a time (_PUSH_LOCK).
# Override with AHTOOL_MAX_SYNC_MB if ever needed.
MAX_SYNC_JSON = int(os.environ.get("AHTOOL_MAX_SYNC_MB") or 24) << 20
MAX_SYNC_BODY = MAX_SYNC_JSON  # the gzip body can never legitimately be larger than what it unpacks to
SYNC_CONTENT_TYPE = "application/gzip"  # what sync._request sends; plain application/json is accepted as well
PUSH_WAIT = 15               # seconds a push waits for the one running before it is told 503 "busy, retry"
# Sync bodies are received (and unpacked) before the push waits for _PUSH_LOCK, so a client sending slowly holds only
# its own read slot. MAX_PUSH_READS bounds the bodies held in memory at once (each up to MAX_SYNC_BODY packed +
# MAX_SYNC_JSON unpacked); one more slot is kept for owner keys; every key has at most one push in flight.
MAX_PUSH_READS = 3
PULL_SLOTS = 2               # /api/sync/pull pages built at once (each can need ~100 MB while it is serialised)
PULL_WAIT = 10               # seconds a pull waits for a slot before it is told 503 "busy, retry" (the client retries)
SOCKET_TIMEOUT = 30          # seconds of silence before a connection is dropped (idle keep-alive, every recv)
HEADER_TIMEOUT = 10          # request line + headers must arrive within this many seconds of the first byte
BODY_TIMEOUT_MIN = 10        # a body must arrive within max(MIN, size / BODY_MIN_RATE) seconds, at most MAX
BODY_TIMEOUT_MAX = 120
BODY_MIN_RATE = 64 << 10     # bytes/s
MAX_REQUEST_LINE = 8 << 10   # longer -> 414
MAX_HEADER_BYTES = 32 << 10  # all header lines together; more -> 431
DRAIN_MAX = 4 << 20          # an oversized body up to this size is read away before the 413, so the client sees it
LINGER_SECONDS = 2           # larger: answer, half-close and keep reading this long before closing (no RST)
MAX_WEB_CONNECTIONS = 128    # concurrent connections served at once in the web version
MAX_CONN_PER_IP = int(os.environ.get("AHTOOL_MAX_PER_IP") or 16)  # web version, per client address
# Behind a trusted reverse proxy the per-address cap applies to the X-Forwarded-For address; every TCP peer (the
# proxy itself, or anyone reaching the backend port directly) still gets this larger cap on connections.
MAX_CONN_PER_PROXY = int(os.environ.get("AHTOOL_MAX_PER_PROXY") or 64)
UNSAFE_METHODS = ("POST", "PUT", "DELETE")
DEFAULT_PORTS = {"http": 80, "https": 443}
_CL_RE = re.compile(r"[0-9]{1,15}")



class _PushGate:
    """The one slot for parsing + applying a sync batch. Owners waiting for it go before contributors."""

    def __init__(self):
        self._cv = threading.Condition()
        self._busy = False
        self._owners_waiting = 0

    def acquire(self, owner=False, timeout=None):
        end = None if timeout is None else time.monotonic() + timeout
        with self._cv:
            if owner:
                self._owners_waiting += 1
            try:
                while self._busy or (not owner and self._owners_waiting):
                    left = None if end is None else end - time.monotonic()
                    if left is not None and left <= 0:
                        return False
                    self._cv.wait(left)
                self._busy = True
                return True
            finally:
                if owner:
                    self._owners_waiting -= 1
                    self._cv.notify_all()  # a contributor may go now if no other owner waits

    def release(self):
        with self._cv:
            self._busy = False
            self._cv.notify_all()


# At most one sync push is parsed and applied at a time (memory: json.loads of a batch); a bounded number of bodies
# is received/held at once (_PUSH_READS, one more for owners); at most one push per key is in flight; a bounded
# number of pull pages is built at once; a bounded number of /api/x bodies is parsed at once.
_PUSH_LOCK = _PushGate()
_PUSH_READS = threading.BoundedSemaphore(MAX_PUSH_READS)
_PUSH_READS_OWNER = threading.BoundedSemaphore(1)
_PUSH_INFLIGHT = set()
_PUSH_INFLIGHT_LOCK = threading.Lock()
_PULL_SLOTS = threading.BoundedSemaphore(PULL_SLOTS)
_TUNNEL_SLOTS = threading.BoundedSemaphore(MAX_TUNNEL_PARSE)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "web")

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")


def default_connect():
    from . import db
    return db.connect()


# Inline event handlers the frontend needs, allowed via 'unsafe-hashes'. Empty: the frontend uses none.
INLINE_HANDLERS = ()
_INLINE_SCRIPT_RE = re.compile(rb"<script\b(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script\s*>", re.S | re.I)


def _sha256(b):
    return "'sha256-%s'" % base64.b64encode(hashlib.sha256(b).digest()).decode("ascii")


def csp_for_html(html, extra_origins=()):
    """Content-Security-Policy for a page: inline <script>s are allowed by hash, computed from the served HTML.
    extra_origins: script + connect origins of the optional page-statistics tracker (web version only)."""
    hashes = [_sha256(m.group(1)) for m in _INLINE_SCRIPT_RE.finditer(html)]
    extra = list(extra_origins)
    handlers = ["'unsafe-hashes'"] + [_sha256(h.encode("utf-8")) for h in INLINE_HANDLERS] if INLINE_HANDLERS else []
    return "; ".join([
        "default-src 'self'",
        " ".join(["script-src 'self'"] + extra + hashes + handlers),
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src 'self' https://fonts.gstatic.com",
        "img-src 'self' data: https://wow.zamimg.com",
        " ".join(["connect-src 'self'"] + extra),
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ])


CSP_API = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"  # JSON, scripts, styles, ...
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


def _reject_constant(name):
    raise ValueError("NaN/Infinity are not allowed")


def loads_strict(raw):
    """json.loads for request bodies: UTF-8 only, no NaN/Infinity."""
    return json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)


def gunzip_capped(raw, cap):
    """Unpack one gzip member, refusing to produce more than `cap` bytes (gzip bombs)."""
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = d.decompress(raw, cap + 1)
    except zlib.error:
        raise BadRequest("body is not valid gzip")
    if len(out) > cap or d.unconsumed_tail:
        raise TooLarge("sync batch too large")
    if not d.eof or d.unused_data:
        raise BadRequest("body is not valid gzip")
    return out


class NotFound(Exception):
    pass


class BadRequest(Exception):
    pass


class NotImplementedYet(Exception):
    pass


class TooLarge(BadRequest):
    pass


def _leveling_args(qs):
    def num(k, d):
        try:
            return int(float(qs.get(k, d)))
        except ValueError:
            raise BadRequest("%s must be a number" % k)
    mode = qs.get("mode", "baseline")
    recover = qs.get("recover", "sell")
    sources = qs.get("sources", "trainer")
    if recover not in ("sell", "vendor", "none"):
        raise BadRequest("recover must be sell, vendor or none")
    if sources not in ("trainer", "all"):
        raise BadRequest("sources must be trainer or all")
    return dict(start=num("start", 1), target=num("target", 300), mode=mode, recover=recover, sources=sources,
                known_only=qs.get("known_only", "0").lower() in ("1", "true", "yes"),
                cooldowns=qs.get("cooldowns", "0").lower() in ("1", "true", "yes"))


def _leveling():
    try:
        from . import leveling
    except ImportError as e:
        if getattr(e, "name", None) in ("ahtool.leveling", "leveling"):
            raise NotImplementedYet("leveling planner not installed")
        raise
    return leveling


def split_hostport(value, scheme=None):
    """'Host'-style 'name[:port]' -> (lowercase name, port) with the scheme's default port; None if malformed."""
    value = (value or "").strip().lower()
    if not value or any(c in value for c in "/?#@\\ \t"):
        return None
    try:
        u = urlsplit("//" + value)
        port = u.port
    except ValueError:
        return None
    if not u.hostname:
        return None
    if port is None:
        port = DEFAULT_PORTS.get(scheme)
    return u.hostname.rstrip("."), port


def parse_origin(value):
    """'https://name[:port]' -> (scheme, name, port) with the default port filled in; None for null/garbage."""
    value = (value or "").strip()
    try:
        u = urlsplit(value)
        port = u.port
    except ValueError:
        return None
    scheme = u.scheme.lower()
    if scheme not in DEFAULT_PORTS or not u.hostname or u.username or u.password:
        return None
    return scheme, u.hostname.lower().rstrip("."), port or DEFAULT_PORTS[scheme]


def _trusted_proxies(v):
    """AHTOOL_TRUSTED_PROXIES: comma-separated addresses or networks (10.0.0.5, 172.16.0.0/12) of the reverse proxies
    whose X-Forwarded-For is believed. -> list of ip_network, or None (not set: every peer is believed)."""
    import ipaddress
    v = str(v if v is not None else "").strip()
    if not v:
        return None
    out = []
    for part in v.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            raise ValueError("AHTOOL_TRUSTED_PROXIES: not an IP address or network: %r" % part[:60])
    return out


def _peer_in(peer, nets):
    import ipaddress
    try:
        a = ipaddress.ip_address(peer.split("%")[0])
    except ValueError:
        return False
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    return any(a.version == n.version and a in n for n in nets)


def _trusted_hops(v):
    """AHTOOL_TRUST_PROXY: 1/true/yes = one reverse proxy in front, a number = that many; else 0 (XFF ignored)."""
    v = str(v if v is not None else "").strip().lower()
    if v in ("true", "yes", "on"):
        return 1
    return int(v) if v.isdigit() else 0


class _DeadlineSocketIO(socket.SocketIO):
    """Raw socket reader that enforces the handler's absolute deadline (`_rdeadline`), not just a per-recv timeout:
    trickling one byte every few seconds no longer keeps a request open forever. With no deadline set (between
    requests) the idle SOCKET_TIMEOUT applies, and the first byte of a request starts the HEADER_TIMEOUT clock."""

    def __init__(self, sock, handler):
        super().__init__(sock, "rb")
        self._h = handler

    def readinto(self, b):
        h = self._h
        dl = h._rdeadline
        t = SOCKET_TIMEOUT
        if dl is not None:
            t = dl - time.monotonic()
            if t <= 0:
                raise TimeoutError("request not received in time")
        self._sock.settimeout(min(t, SOCKET_TIMEOUT))
        try:
            n = super().readinto(b)
        finally:
            self._sock.settimeout(SOCKET_TIMEOUT)  # writes keep the normal timeout
        if dl is None and n:
            h._rdeadline = time.monotonic() + HEADER_TIMEOUT
        return n


class _HeaderCappedReader(io.BufferedReader):
    """Counts header bytes while the request headers are parsed; over MAX_HEADER_BYTES -> 431 (via LineTooLong,
    which BaseHTTPRequestHandler.parse_request answers itself)."""

    def __init__(self, raw, handler):
        super().__init__(raw)
        self._h = handler

    def readline(self, size=-1):
        line = super().readline(size)
        h = self._h
        if h._in_headers:
            h._header_bytes += len(line)
            if h._header_bytes > MAX_HEADER_BYTES:
                raise http.client.LineTooLong("request headers")
        return line


# Request threads get this much stack. musl (the Alpine image) gives threads only 128 kB, where deeply nested JSON
# overflows the C stack - a segfault - before CPython can raise RecursionError (which the handlers turn into 400).
# glibc and Windows already give threads several MB; this makes every platform behave the same.
THREAD_STACK = 8 << 20


def _ensure_thread_stack():
    # threading.stack_size() is not a pure getter: without an argument it RESETS the size to the default (0). So
    # always set, and put a larger value back if someone had chosen one.
    try:
        old = threading.stack_size(THREAD_STACK)
        if old > THREAD_STACK:
            threading.stack_size(old)
    except (ValueError, RuntimeError):  # platform without adjustable thread stacks
        pass


def make_server(cfg, connect=None, host="127.0.0.1", port=None, web_dir=WEB_DIR, web=False):
    """web=True: the shared web version (see webstate.py and sync.py) instead of the local app."""
    _ensure_thread_stack()
    connect = connect or default_connect
    port = int(port if port is not None else cfg.get("port", 8642))
    web_root = os.path.normpath(web_dir)

    extra_hosts = {h.lower() for h in (cfg.get("allowed_hosts") or []) if isinstance(h, str)}
    # Web version behind a reverse proxy: the browser's Origin is the public address, which the proxy may not pass on
    # in Host/X-Forwarded-Host. AHTOOL_PUBLIC_URL (run_web.py puts it in cfg) names it.
    public_origin = parse_origin(cfg.get("public_url")) if web and cfg.get("public_url") else None
    # Optional page statistics (Umami or compatible), web version only: AHTOOL_ANALYTICS_SCRIPT + _ID. Auto-track is
    # off because the app routes by #hash, which the tracker neither sees nor keeps; app.js reports each page itself.
    # The tracker's origin is added to script-src and connect-src (it posts to <origin>/api/send).
    analytics_tag, analytics_origins = b"", ()
    a_src, a_id = (cfg.get("analytics_script") or "").strip(), (cfg.get("analytics_id") or "").strip()
    a_origin = parse_origin(a_src) if web and a_src and a_id else None
    if a_origin and urlsplit(a_src).scheme == "https":
        a_origin_str = "%s://%s" % (urlsplit(a_src).scheme, urlsplit(a_src).netloc.lower())
        analytics_origins = (a_origin_str,)
        domain = urlsplit(cfg.get("public_url") or "").hostname or ""
        analytics_tag = ('<script defer src="%s" data-website-id="%s" data-auto-track="false"%s></script>\n' % (
            html_mod.escape(a_src), html_mod.escape(a_id),
            ' data-domains="%s"' % html_mod.escape(domain) if domain else "")).encode("utf-8")
    trust_hops = _trusted_hops(cfg.get("trust_proxy")) if web else 0
    # only these TCP peers' X-Forwarded-For is believed (AHTOOL_TRUSTED_PROXIES; unset: any peer's)
    trusted_nets = _trusted_proxies(cfg.get("trusted_proxies", os.environ.get("AHTOOL_TRUSTED_PROXIES"))) \
        if web else None
    if trusted_nets and not trust_hops:
        trust_hops = 1  # naming the proxies means there is one in front
    per_ip_cap = MAX_CONN_PER_IP
    # every TCP peer: the normal cap, or behind a proxy (all visitors share its address) the larger proxy cap
    per_peer_cap = MAX_CONN_PER_PROXY if trust_hops else MAX_CONN_PER_IP

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "AHTool"
        timeout = SOCKET_TIMEOUT  # applied to the socket by StreamRequestHandler.setup()
        _rdeadline = None         # absolute read deadline (time.monotonic()) of the current request part
        _in_headers = False
        _header_bytes = 0
        _linger = False           # unread request data may be in flight: half-close and drain before closing
        _body_pending = 0

        def version_string(self):
            return self.server_version  # no Python version in the Server header

        def setup(self):
            super().setup()
            self.rfile.close()  # replace the plain makefile() reader with the deadline-aware one
            self.connection._io_refs += 1  # what makefile() does; released by rfile.close() in finish()
            self.rfile = _HeaderCappedReader(_DeadlineSocketIO(self.connection, self), self)

        def handle_one_request(self):
            self._rdeadline = None  # idle until the first byte of the next request arrives
            self._in_headers = False
            self._header_bytes = 0
            try:
                super().handle_one_request()
            except ConnectionError:  # the client went away (reset, broken pipe): nothing to answer
                self.close_connection = True

        def parse_request(self):
            if len(self.raw_requestline) > MAX_REQUEST_LINE:
                self.requestline, self.request_version, self.command = "", "", ""
                self.send_error(414)
                return False
            self._in_headers = True
            try:
                if not super().parse_request():
                    return False
            finally:
                self._in_headers = False
            # http.client.parse_headers stops at the first line that is not "Name: value" (e.g. "X-Foo : bar") and
            # silently treats the rest - Content-Length, Transfer-Encoding, ... - as a message body. A proxy may read
            # those headers; we would not, and parse the body as the next request. Refuse such requests, and folded
            # (obs-fold) header values.
            h = self.headers
            if self.request_version == "HTTP/0.9" or not isinstance(h, http.client.HTTPMessage):
                self.send_error(400, "HTTP/0.9 is not supported")
                return False
            if h.defects or h.get_payload() or any("\r" in v or "\n" in v for v in h.values()):
                self.send_error(400, "malformed header line")
                return False
            return True

        def finish(self):
            try:
                super().finish()
            finally:
                if self._linger:
                    self._linger_close()

        def _linger_close(self):
            """We answered without reading the whole request. Closing now with unread data would make the kernel
            send a RST, which can destroy the answer before the client read it. Half-close and read the rest away
            for a moment instead."""
            sock = self.connection
            try:
                sock.shutdown(socket.SHUT_WR)
                end = time.monotonic() + LINGER_SECONDS
                while True:
                    t = end - time.monotonic()
                    if t <= 0:
                        break
                    sock.settimeout(t)
                    if not sock.recv(65536):
                        break
            except OSError:
                pass

        def log_message(self, fmt, *args):
            if web and getattr(self, "path", None) == "/healthz":
                return
            sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

        def send_error(self, code, message=None, explain=None):
            """Errors BaseHTTPRequestHandler answers itself (bad request line, unknown method, header too long, ...):
            JSON with the same security headers as every other answer, and the connection closed."""
            try:
                short = self.responses[code][0]
            except KeyError:
                short = "error"
            self.log_error("code %d, message %s", code, message or short)
            self.close_connection = True
            self._linger = True  # the rest of the request is unread: make sure the client still gets this answer
            if not getattr(self, "request_version", None) or self.request_version == "HTTP/0.9":
                self.request_version = "HTTP/1.1"  # answer with a status line and headers, never a bare body
            self._json({"error": short.lower()}, code)

        def _send(self, code, body, ctype, extra=None, csp=CSP_API):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", csp)
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            if getattr(self, "command", None) != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, code=200, extra=None):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self._send(code, body, "application/json; charset=utf-8", dict({"Cache-Control": "no-store"}, **(extra or {})))

        def _fail(self, code, msg, extra=None):
            """Error answer that also ends the connection (the request may not have been read completely)."""
            self.close_connection = True
            return self._json({"error": msg}, code, extra)

        def _busy(self, msg="busy, retry"):
            return self._fail(503, msg, {"Retry-After": "5"})

        def _static(self, path):
            rel = unquote(path).lstrip("/") or "index.html"
            full = os.path.normpath(os.path.join(web_root, rel))
            if full != web_root and not full.startswith(web_root + os.sep):
                return self._json({"error": "forbidden"}, 403)
            if not os.path.isfile(full):
                if "." in os.path.basename(rel):
                    return self._json({"error": "not found"}, 404)
                full = os.path.join(web_root, "index.html")  # SPA fallback
                if not os.path.isfile(full):
                    return self._json({"error": "web/index.html mangler"}, 404)
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                ctype += "; charset=utf-8"
            with open(full, "rb") as f:
                data = f.read()
            csp = CSP_API
            if ctype.startswith("text/html"):
                if web and os.path.basename(full) == "index.html":
                    data = data.replace(b"<html ", b'<html data-mode="web" ', 1)
                    if analytics_tag:
                        data = data.replace(b"</head>", analytics_tag + b"</head>", 1)
                csp = csp_for_html(data, analytics_origins if web else ())
            self._send(200, data, ctype, {"Cache-Control": "no-store"}, csp=csp)

        def _download(self, name):
            """Web version: GET /download/<name> - only manifest.json and the files it lists (ahtool/downloads.py),
            streamed from downloads_dir, so a 15 MB package does not sit in memory once per visitor."""
            from . import downloads
            ddir = cfg.get("downloads_dir") or ""
            m = downloads.load_manifest(ddir) if ddir else None
            if m is None:
                return self._json({"error": "no downloads on this site"}, 404)
            if name == downloads.MANIFEST:
                return self._json(m)
            names = {f.get("name") for f in m["files"] if isinstance(f, dict)}
            if name not in names or "/" in name or "\\" in name or name.startswith("."):
                return self._json({"error": "not found"}, 404)
            full = os.path.join(ddir, name)
            try:
                f = open(full, "rb")
            except OSError:
                return self._json({"error": "not found"}, 404)
            with f:
                size = os.fstat(f.fileno()).st_size
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Security-Policy", CSP_API)
                for k, v in SECURITY_HEADERS.items():
                    self.send_header(k, v)
                if self.close_connection:
                    self.send_header("Connection", "close")
                self.end_headers()
                if self.command != "HEAD":
                    for chunk in iter(lambda: f.read(256 * 1024), b""):
                        self.wfile.write(chunk)

        # ------------------------------------------------------------ request framing / origin checks

        def _content_length(self):
            """-> declared body length. Refuses Transfer-Encoding and malformed or repeated Content-Length, which
            proxies and this server could otherwise frame differently (request smuggling)."""
            if self.headers.get_all("Transfer-Encoding"):
                raise BadRequest("Transfer-Encoding is not supported")
            vals = self.headers.get_all("Content-Length") or []
            if not vals:
                return 0
            if len(vals) > 1 or not _CL_RE.fullmatch(vals[0].strip()):
                raise BadRequest("invalid Content-Length")
            return int(vals[0].strip())

        def _read_body(self, limit):
            n = self._content_length()
            if n > limit:
                self._discard_body(n)
                raise TooLarge("request body too large")
            self._body_pending = 0
            self._rdeadline = time.monotonic() + min(BODY_TIMEOUT_MAX, max(BODY_TIMEOUT_MIN, n / BODY_MIN_RATE))
            try:
                data = self.rfile.read(n) if n else b""
            except TimeoutError:
                self.close_connection = True
                raise
            if len(data) != n:
                self.close_connection = True
                raise BadRequest("incomplete request body")
            return data

        def _discard_body(self, n):
            """An oversized body we will refuse: read it away first (up to DRAIN_MAX) so the client, still sending,
            gets to read the 413 instead of a broken pipe; larger ones are left to _linger_close."""
            self.close_connection = True
            if n > DRAIN_MAX:
                self._linger = True
                return
            self._rdeadline = time.monotonic() + min(BODY_TIMEOUT_MAX, max(BODY_TIMEOUT_MIN, n / BODY_MIN_RATE))
            try:
                while n > 0:
                    chunk = self.rfile.read(min(n, 1 << 16))
                    if not chunk:
                        break
                    n -= len(chunk)
            except OSError:
                return
            self._body_pending = 0

        def _host_ok(self):
            if web:
                return True
            host = (self.headers.get("Host") or "").strip().lower()
            port = self.server.server_address[1]
            allowed = {"localhost:%d" % port, "127.0.0.1:%d" % port, "[::1]:%d" % port} | extra_hosts
            if port == 80:
                allowed |= {"localhost", "127.0.0.1", "[::1]"}
            return host in allowed

        def _same_origin(self):
            """Browser requests from another site (CSRF) carry an Origin and/or Sec-Fetch-Site saying so. Clients
            that send neither (sync.py, curl, tests) are not browsers and are let through."""
            site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
            if site in ("cross-site", "same-site"):
                return False
            if web and site == "same-origin":
                # Set by the browser itself (scripts cannot), so it holds even when a reverse proxy rewrote Host.
                return True
            origin = self.headers.get("Origin")
            if origin is None:
                return True
            o = parse_origin(origin)
            if o is None:  # "null" (sandboxed frame, file://, redirects) or garbage
                return False
            scheme, name, oport = o
            if public_origin is not None and o == public_origin:
                return True
            hosts = [self.headers.get("Host") or ""]
            if web and self.headers.get("X-Forwarded-Host"):  # behind a reverse proxy that rewrites Host
                hosts.append(self.headers["X-Forwarded-Host"].split(",")[0])
            # ports compared with defaults filled in: "Host: ah.example.net:443" is https://ah.example.net
            return any(split_hostport(h, scheme) == (name, oport) for h in hosts)

        def _client_ip(self):
            """The visitor's address: the TCP peer, or behind trust_hops reverse proxies the address the outermost
            trusted proxy saw (the entry it appended to X-Forwarded-For; earlier entries are client-supplied)."""
            peer = self.client_address[0]
            if not trust_hops or (trusted_nets is not None and not _peer_in(peer, trusted_nets)):
                return peer  # no proxy configured, or this peer is not one of them: its X-Forwarded-For is a claim
            xff = [e.strip() for e in ",".join(self.headers.get_all("X-Forwarded-For") or []).split(",") if e.strip()]
            if not xff:
                return peer
            return xff[-trust_hops] if len(xff) >= trust_hops else xff[0]

        def _ctype_ok(self, allowed, body_len):
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if not ctype and body_len == 0:
                return True  # bodyless POST/DELETE from api.js
            return ctype in allowed

        def _handle(self, method):
            self._body_pending = 0
            try:
                self._body_pending = self._content_length()
            except BadRequest as e:
                return self._fail(400, str(e))
            ip = None
            try:
                if trust_hops:  # behind a proxy the per-address cap can only be applied once the headers are in
                    ip = self._client_ip()
                    if not self.server.ip_enter(ip):
                        ip = None
                        return self._busy("too many connections from your address")
                if not self._host_ok():
                    return self._fail(403, "forbidden host")
                if method in UNSAFE_METHODS and not self._same_origin():
                    return self._fail(403, "cross-origin request refused")
                return self._dispatch(method)
            finally:
                if ip is not None:
                    self.server.ip_leave(ip)
                if self._body_pending:  # body left unread: the next "request" on this connection would be the body
                    self.close_connection = True
                    self._linger = True

        def _dispatch(self, method):
            u = urlparse(self.path)
            path = u.path
            if web and path.startswith("/api/sync/"):
                return self._sync(method, path.rstrip("/"))
            if self._body_pending and method not in UNSAFE_METHODS:
                # GET/HEAD with a body: read it away (small) or drop the connection after answering
                if self._body_pending > MAX_BODY:
                    return self._fail(413, "request body too large")
                try:
                    self._read_body(MAX_BODY)
                except BadRequest as e:
                    return self._fail(400, str(e))
            raw = b""
            if method in UNSAFE_METHODS:
                if not self._ctype_ok(("application/json",), self._body_pending):
                    return self._fail(415, "Content-Type must be application/json")
                try:
                    raw = self._read_body(MAX_TUNNEL_BODY if web and path == "/api/x" else MAX_BODY)
                except TooLarge as e:
                    return self._fail(413, str(e))
                except BadRequest as e:
                    return self._fail(400, str(e))
            if web and path == "/healthz":
                return self._json({"ok": True})
            if web and path == "/api/x":
                return self._tunnel(method, raw)
            if web and path.startswith("/download/"):
                if method not in ("GET", "HEAD"):
                    return self._json({"error": "method not allowed"}, 405)
                return self._download(path[len("/download/"):])
            if not path.startswith("/api/"):
                if method not in ("GET", "HEAD"):
                    return self._json({"error": "method not allowed"}, 405)
                return self._static(path)
            qs = {k: v[-1] for k, v in parse_qs(u.query).items()}
            conn = None
            try:
                conn = connect()
                if web:  # a direct call without the browser's state: an empty visitor
                    webstate.install(conn, None)
                self._json(self._route(method, path.rstrip("/"), qs, conn, raw))
            except NotImplementedYet as e:
                self._json({"error": str(e)}, 501)
            except NotFound as e:
                self._json({"error": str(e) or "not found"}, 404)
            except userstate.Invalid as e:
                self._json({"error": str(e)}, 400)
            except KeyError as e:
                self._json({"error": str(e.args[0]) if e.args else "not found"}, 404)
            except BadRequest as e:
                self._json({"error": str(e)}, 400)
            except Exception:  # noqa
                traceback.print_exc()
                self._json({"error": "internal server error"}, 500)
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

        # ------------------------------------------------------------ web version

        def _tunnel(self, method, raw):
            """POST /api/x {m, p, b, s}: run API call `m p` with body `b` against the visitor's state `s`.
            Answers {r: result, s: new state (only when it changed)}. State travels in the body because it can be
            larger than a header may be."""
            if method != "POST":
                return self._json({"error": "method not allowed"}, 405)
            # Parsing and installing the state is where a body costs the most memory: a bounded number at a time.
            # Afterwards only the small parts of the request are kept.
            if not _TUNNEL_SLOTS.acquire(timeout=TUNNEL_WAIT):
                return self._busy()
            conn = None
            try:
                try:
                    req = loads_strict(raw)
                    m, target = req["m"], req["p"]
                    if m not in ("GET", "POST", "PUT", "DELETE") or not isinstance(target, str):
                        raise ValueError
                except (ValueError, KeyError, TypeError, UnicodeDecodeError, RecursionError):
                    return self._json({"error": "bad request"}, 400)
                u = urlparse(target)
                path = u.path.rstrip("/")
                if not path.startswith("/api/") or path.startswith("/api/sync") or path == "/api/x":
                    return self._json({"error": "unknown endpoint"}, 404)
                qs = {k: v[-1] for k, v in parse_qs(u.query).items()}
                body = req.get("b")
                braw = json.dumps(body).encode("utf-8") if body is not None else b""
                st = req.get("s") if isinstance(req.get("s"), dict) else {}
                last_eval = st.get("eval")
                conn = connect()
                webstate.install(conn, st)
                del req, st, body
            except Exception:  # noqa
                traceback.print_exc()
                if conn is not None:
                    conn.close()
                return self._json({"error": "internal server error"}, 500)
            finally:
                _TUNNEL_SLOTS.release()
            try:
                changed = m != "GET"
                result = self._route(m, path, qs, conn, braw)
                if path == "/api/status":
                    # Alerts are evaluated per browser, once per new batch of prices.
                    marker = webstate.data_marker(conn)
                    if marker and last_eval != marker:
                        if last_eval is not None:
                            alerts_mod.evaluate(conn, cfg, notify_user=False)
                            result["unseen_alerts"] = userstate.unseen_alerts(conn)
                        changed = True
                out = {"r": result}
                if changed:
                    out["s"] = webstate.export(conn)
                    out["s"]["eval"] = webstate.data_marker(conn)
                self._json(out)
            except NotImplementedYet as e:
                self._json({"error": str(e)}, 501)
            except NotFound as e:
                self._json({"error": str(e) or "not found"}, 404)
            except (userstate.Invalid, BadRequest) as e:
                self._json({"error": str(e)}, 400)
            except KeyError as e:
                self._json({"error": str(e.args[0]) if e.args else "not found"}, 404)
            except Exception:  # noqa
                traceback.print_exc()
                self._json({"error": "internal server error"}, 500)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        def _sync(self, method, path):
            """Push endpoints for the local apps (sync.py). Bearer: AHTOOL_SYNC_TOKEN or a key from keys.py."""
            from . import keys, sync
            given = (self.headers.get("Authorization") or "")
            given = given[7:].strip() if given.startswith("Bearer ") else ""
            conn = None
            try:
                conn = connect()
                source = keys.authenticate(conn, cfg, given)
                if source is None:
                    self.close_connection = True
                    return self._json({"error": "unknown or revoked sync key"}, 401)
                if method == "GET" and path == "/api/sync/state":
                    return self._json(sync.remote_state(conn, source))
                if method == "GET" and path == "/api/sync/pull":
                    qs = {k: v[-1] for k, v in parse_qs(urlparse(self.path).query).items()}
                    try:
                        cursor = json.loads(qs.get("cursor") or "{}")
                    except ValueError:
                        raise BadRequest("cursor must be JSON")
                    # A page is built and serialised by a bounded number of requests at once (memory, CPU); it is
                    # sent after the slot is given back, so a slow reader never holds one.
                    if not _PULL_SLOTS.acquire(timeout=PULL_WAIT):
                        return self._busy("busy with other pulls, retry")
                    try:
                        page = sync.pull_page(conn, cursor, qs.get("limit") or sync.PULL_LIMIT, version=qs.get("v"))
                        body = json.dumps(page, ensure_ascii=False).encode("utf-8")
                        del page
                    finally:
                        _PULL_SLOTS.release()
                    return self._send(200, body, "application/json; charset=utf-8", {"Cache-Control": "no-store"})
                if method == "POST" and path == "/api/sync/push":
                    gz = self._ctype_ok((SYNC_CONTENT_TYPE,), -1)
                    if not gz and not self._ctype_ok(("application/json",), -1):
                        return self._fail(415, "Content-Type must be " + SYNC_CONTENT_TYPE)
                    if self._body_pending > MAX_SYNC_BODY:
                        return self._fail(413, "request body too large")
                    return self._push(conn, source, gz)
                return self._json({"error": "unknown endpoint"}, 404)
            except TimeoutError:
                raise  # body not received in time: handle_one_request drops the connection
            except BadRequest as e:
                return self._fail(400, str(e))
            except sync.BadPayload as e:  # our own validation text: names the table/field, no internals
                return self._fail(400, "invalid sync batch: %s" % e)
            except (ValueError, OSError, EOFError):
                traceback.print_exc()
                return self._fail(400, "bad sync request")
            except Exception:  # noqa
                traceback.print_exc()
                return self._fail(500, "internal server error")
            finally:
                if conn is not None:
                    conn.close()

        def _push(self, conn, source, gz):
            """POST /api/sync/push. The body is received and unpacked first, holding only a read slot (a client that
            sends slowly blocks nobody else's push); only parsing and applying take the one _PUSH_LOCK."""
            from . import sync
            owner = bool(source.get("owner"))
            kid = int(source.get("key_id") or 0)
            with _PUSH_INFLIGHT_LOCK:
                if kid in _PUSH_INFLIGHT:
                    return self._busy("another push with this key is still running, retry")
                _PUSH_INFLIGHT.add(kid)
            try:
                slot = _PUSH_READS_OWNER if owner and _PUSH_READS_OWNER.acquire(blocking=False) else None
                if slot is None:
                    if not _PUSH_READS.acquire(timeout=PUSH_WAIT):
                        return self._busy("busy with other pushes, retry")
                    slot = _PUSH_READS
                try:
                    try:
                        raw = self._read_body(MAX_SYNC_BODY)
                        if gz:
                            raw = gunzip_capped(raw, MAX_SYNC_JSON)
                    except TooLarge as e:
                        return self._fail(413, str(e))
                    # cheap upper bound on what json.loads would build, before it runs (a few kB of gzip can
                    # unpack to millions of empty lists)
                    why = sync.too_many_values(raw, owner)
                    if why:
                        return self._fail(413, why)
                    if not _PUSH_LOCK.acquire(owner=owner, timeout=PUSH_WAIT):
                        return self._busy("busy with another push, retry")
                    try:
                        try:
                            payload = sync.loads_strict(raw.decode("utf-8"))
                        except (ValueError, UnicodeDecodeError, RecursionError):
                            raise BadRequest("sync batch is not valid JSON")
                        del raw
                        res = sync.apply(conn, cfg, payload, source)
                        del payload
                    finally:
                        _PUSH_LOCK.release()
                finally:
                    slot.release()
            finally:
                with _PUSH_INFLIGHT_LOCK:
                    _PUSH_INFLIGHT.discard(kid)
            analytics.invalidate()
            return self._json(res)

        # ------------------------------------------------------------ routes

        def _route(self, method, path, qs, conn, raw=b""):
            if web and (method, path) in WEB_BLOCKED:
                raise NotFound("not available in the web version")
            realm = qs.get("realm") or None
            A = analytics
            if realm and web and realm not in A.list_realms(conn):
                realm = None  # the web version shows only realms the owner has data for (contributors add none)
            body = None
            if method in ("POST", "PUT", "DELETE") and raw.strip():
                try:
                    body = loads_strict(raw)
                except (ValueError, UnicodeDecodeError, RecursionError):
                    raise BadRequest("request body is not valid JSON")
            if method == "POST":
                if path == "/api/ingest":
                    from . import ingest
                    res = ingest.ingest(conn, cfg, force=True)
                    try:
                        res["logger"] = market.ingest(conn, cfg, force=True)
                    except Exception:  # noqa
                        traceback.print_exc()
                        res["logger"] = {"error": "AH Tool Logger import failed (see the console)"}
                    A.invalidate()
                    if not res.get("skipped"):
                        alerts_mod.evaluate(conn, cfg)
                    return res
                if path == "/api/alerts/evaluate":
                    A.invalidate()
                    new = alerts_mod.evaluate(conn, cfg, notify_user=qs.get("notify", "1") != "0")
                    return {"new": len(new), "alerts": new}
                if path == "/api/alerts/seen":
                    return userstate.mark_alerts_seen(conn)
                m = re.fullmatch(r"/api/leveling/(\d+)/to-shopping", path)
                if m:
                    lv = _leveling()
                    plan = lv.plan(conn, cfg, int(m.group(1)), **_leveling_args(qs))
                    pairs = lv.plan_to_shopping(plan)
                    n = 0
                    with conn:
                        for sid, crafts in pairs:
                            crafts = int(crafts)
                            if crafts <= 0:
                                continue
                            conn.execute("INSERT INTO shopping_list(spell_id, qty) VALUES(?,?) "
                                         "ON CONFLICT(spell_id) DO UPDATE SET qty=MIN(9999, qty+excluded.qty)",
                                         (int(sid), min(crafts, 9999)))
                            n += 1
                    return {"added": n}
                if path == "/api/notify/test":
                    return notify.send_toast("AH Tool", "Test notification - alerts are working.", wait=True)
                raise NotFound("unknown endpoint")
            if method == "PUT":
                if path == "/api/myprofessions":
                    return userstate.put_my_professions(conn, body)
                if path == "/api/settings":
                    return userstate.put_settings(conn, body)
                m = re.fullmatch(r"/api/(known|shopping|watchlist)/(\d+)", path)
                if m:
                    fn = {"known": userstate.put_known, "shopping": userstate.put_shopping,
                          "watchlist": userstate.put_watch}[m.group(1)]
                    return fn(conn, int(m.group(2)), body)
                raise NotFound("unknown endpoint")
            if method == "DELETE":
                if path == "/api/shopping":
                    return userstate.clear_shopping(conn)
                m = re.fullmatch(r"/api/watchlist/(\d+)", path)
                if m:
                    return userstate.delete_watch(conn, int(m.group(1)))
                raise NotFound("unknown endpoint")
            if method not in ("GET", "HEAD"):
                raise NotFound("unknown endpoint")
            m = re.fullmatch(r"/api/leveling/(\d+)", path)
            if m:
                return _leveling().plan(conn, cfg, int(m.group(1)), **_leveling_args(qs))
            m = re.fullmatch(r"/api/recipe/(\d+)/trend", path)
            if m:
                try:
                    days = int(qs.get("days", 30))
                except ValueError:
                    raise BadRequest("days must be a number")
                r = A.recipe_trend(conn, cfg, int(m.group(1)), days, realm)
                if r is None:
                    raise NotFound("recipe %s not found" % m.group(1))
                return r
            if path == "/api/trending-crafts":
                try:
                    limit = int(qs.get("limit", 20))
                except ValueError:
                    raise BadRequest("limit must be a number")
                return A.trending_crafts(conn, cfg, limit, qs.get("mine") in ("1", "true"),
                                         qs.get("include_low") in ("1", "true"), realm)
            if path == "/api/myprofessions":
                return userstate.list_my_professions(conn)
            if path == "/api/mycrafts":
                try:
                    limit = int(qs.get("limit", 50))
                except ValueError:
                    raise BadRequest("limit must be a number")
                return A.mycrafts(conn, cfg, "baseline" if qs.get("mode") == "baseline" else "current", limit, realm)
            if path == "/api/shopping":
                return shopping.shopping(conn, cfg, realm)
            if path == "/api/watchlist":
                return A.watchlist(conn, cfg, realm)
            if path == "/api/alerts":
                try:
                    limit = int(qs.get("limit", 50))
                except ValueError:
                    raise BadRequest("limit must be a number")
                return A.alerts(conn, cfg, limit, realm)
            if path == "/api/settings":
                return userstate.get_settings(conn)
            if path == "/api/status":
                st = A.status(conn, cfg, realm)
                if web:
                    st["web"] = True
                    st["logger"] = dict(st.get("logger") or {}, installed=True)
                    r = conn.execute("SELECT value FROM main.meta WHERE key='sync_push_ts'").fetchone()
                    st["last_push_ts"] = int(r[0]) if r else None
                return st
            if path == "/api/items/search":
                try:
                    limit = int(qs.get("limit", 50))
                except ValueError:
                    raise BadRequest("limit must be a number")
                return A.search(conn, cfg, qs.get("q", ""), limit, realm)
            m = re.fullmatch(r"/api/item/(\d+)", path)
            if m:
                r = A.item_detail(conn, cfg, int(m.group(1)), realm)
                if r is None:
                    raise NotFound("item %s not found" % m.group(1))
                return r
            m = re.fullmatch(r"/api/item/(\d+)/market", path)
            if m:
                try:
                    qty = int(qs["qty"]) if qs.get("qty") else None
                    days = int(qs.get("days", 14))
                except ValueError:
                    raise BadRequest("qty/days must be numbers")
                return market.item_market(conn, cfg, int(m.group(1)), qty, days, realm)
            if path == "/api/market/activity":
                try:
                    days = int(qs.get("days", 7))
                    limit = int(qs.get("limit", 100))
                except ValueError:
                    raise BadRequest("days/limit must be numbers")
                return market.activity(conn, cfg, days, limit, realm)
            if path == "/api/browse":
                from . import browse
                return browse.browse(conn, cfg, realm, qs.get("class"), qs.get("sub"), qs.get("slot") or None,
                                     qs.get("q", ""), qs.get("min_level"), qs.get("max_level"), qs.get("quality"),
                                     qs.get("listed", "1") not in ("0", "false"), qs.get("limit") or 1000)
            if path == "/api/market/report":
                from . import report
                date = qs.get("date") or None
                if date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                    raise BadRequest("date must be YYYY-MM-DD")
                try:
                    return report.daily(conn, cfg, date, realm)
                except ValueError as e:
                    raise NotFound(str(e))
            if path == "/api/market/my":
                try:
                    days = int(qs.get("days", 30))
                except ValueError:
                    raise BadRequest("days must be a number")
                return market.my_market(conn, cfg, days, realm)
            m = re.fullmatch(r"/api/tooltip/(\d+)", path)
            if m:
                r = tooltip.tooltip(conn, cfg, int(m.group(1)), realm)
                if r is None:
                    raise NotFound("item %s not found" % m.group(1))
                return r
            if path == "/api/professions":
                return A.professions(conn, cfg, realm)
            m = re.fullmatch(r"/api/professions/(\d+)/gather", path)
            if m:
                return gathering.gather_items(conn, cfg, int(m.group(1)), realm)
            m = re.fullmatch(r"/api/professions/(\d+)", path)
            if m:
                return A.profession_recipes_user(conn, cfg, int(m.group(1)), qs.get("mode", "current"), realm,
                                                 mine_only=qs.get("mine") in ("1", "true"))
            if path == "/api/deals":
                try:
                    thr = float(qs["threshold"]) if qs.get("threshold") else None
                    mv = int(float(qs.get("min_value") or 0))
                except ValueError:
                    raise BadRequest("invalid threshold/min_value")
                return A.deals(conn, cfg, thr, mv, qs.get("reason", ""), realm,
                               include_low=qs.get("include_low") in ("1", "true"))
            if path == "/api/stats/market":
                return A.market_stats(conn, cfg, realm)
            if path == "/api/postings":
                return A.postings(conn, cfg, realm)
            raise NotFound("unknown endpoint: " + path)

        def do_GET(self):
            self._handle("GET")

        def do_HEAD(self):
            self._handle("HEAD")

        def do_POST(self):
            self._handle("POST")

        def do_PUT(self):
            self._handle("PUT")

        def do_DELETE(self):
            self._handle("DELETE")

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        # On Windows SO_REUSEADDR lets a second instance bind the same port while an old
        # one keeps answering requests. Use an exclusive bind there so a duplicate start fails.
        allow_reuse_address = os.name != "nt"

        def server_bind(self):
            if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()

        # The web version is public: bound the number of connection threads, in total and per client address.
        # Over a limit a connection gets a plain 503 and is closed instead of starting yet another thread. Behind a
        # trusted reverse proxy every connection comes from the proxy, so the per-address cap is applied per request
        # instead, to the X-Forwarded-For address (Handler._handle); every TCP peer still has MAX_CONN_PER_PROXY
        # connections at most, so reaching the backend port directly with forged X-Forwarded-For gains little.
        slots = threading.BoundedSemaphore(MAX_WEB_CONNECTIONS) if web else None
        per_ip = {}      # per visitor address, per request (behind a proxy)
        per_peer = {}    # per TCP peer, per connection
        per_ip_lock = threading.Lock()
        cap_peer = web

        def _enter(self, table, key, cap):
            with self.per_ip_lock:
                n = table.get(key, 0)
                if n >= cap:
                    return False
                table[key] = n + 1
                return True

        def _leave(self, table, key):
            with self.per_ip_lock:
                n = table.get(key, 0) - 1
                if n > 0:
                    table[key] = n
                else:
                    table.pop(key, None)

        def ip_enter(self, ip):
            return self._enter(self.per_ip, ip, per_ip_cap)

        def ip_leave(self, ip):
            self._leave(self.per_ip, ip)

        def peer_enter(self, ip):
            return self._enter(self.per_peer, ip, per_peer_cap)

        def peer_leave(self, ip):
            self._leave(self.per_peer, ip)

        def _refuse(self, request):
            try:
                request.settimeout(2)
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                                b"Content-Length: 24\r\nX-Content-Type-Options: nosniff\r\n"
                                b"Content-Security-Policy: " + CSP_API.encode() + b"\r\n"
                                b"Connection: close\r\nRetry-After: 5\r\n\r\n{\"error\": \"busy, retry\"}")
            except OSError:
                pass
            self.shutdown_request(request)

        def process_request(self, request, client_address):
            if self.slots is not None and not self.slots.acquire(blocking=False):
                return self._refuse(request)
            if self.cap_peer and not self.peer_enter(client_address[0]):
                self.slots.release()
                return self._refuse(request)
            try:
                super().process_request(request, client_address)
            except BaseException:
                self._release(client_address)
                raise

        def _release(self, client_address):
            if self.cap_peer:
                self.peer_leave(client_address[0])
            if self.slots is not None:
                self.slots.release()

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                self._release(client_address)

    return Server((host, port), Handler)
