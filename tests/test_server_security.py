"""HTTP hardening: Host/Origin checks, request framing, body limits, security headers, error text.
Run: python tests/test_server_security.py"""
import gzip
import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import make_fixture_db  # noqa: E402
from ahtool import db, server  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print("FAIL:", msg)
    else:
        print("PASS", msg)


tmp = tempfile.mkdtemp()
DB = os.path.join(tmp, "test.db")
make_fixture_db.build(DB)
INLINE = "\n    try { document.documentElement.dataset.x = '1'; } catch (e) {}\n  "
webdir = tempfile.mkdtemp()
with open(os.path.join(webdir, "index.html"), "w", encoding="utf-8", newline="\n") as f:  # no CRLF on Windows
    f.write('<!doctype html><html lang="en"><head><script>%s</script><script src="app.js"></script></head>'
            '<body>INDEX</body></html>' % INLINE)
with open(os.path.join(webdir, "app.js"), "w") as f:
    f.write("x=1")


def start(cfg, web=False, connect=None):
    srv = server.make_server(cfg, connect or (lambda: db.connect(DB)), port=0, web_dir=webdir, web=web)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


cfg = {"port": 0, "ah_cut": 0.05, "deal_threshold": 0.25, "baseline_days": 14, "trend_min_profit": 50}
lsrv, LPORT = start(cfg)
TOKEN = "t" * 40
wsrv, WPORT = start({"web": True, "sync_token": TOKEN}, web=True, connect=lambda: db.connect(os.path.join(tmp, "w.db")))


def req(port, method, path, body=None, headers=None, host=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    c.putheader("Host", host if host is not None else "127.0.0.1:%d" % port)
    for k, v in (headers or {}).items():
        c.putheader(k, v)
    if body is not None:
        c.putheader("Content-Length", str(len(body)))
    c.endheaders(body)
    r = c.getresponse()
    data = r.read()
    c.close()
    try:
        data = json.loads(data)
    except ValueError:
        pass
    return r.status, r, data


def raw_exchange(port, payload, wait=1.0):
    """Send raw bytes, read until the server closes or `wait` passes. -> (bytes received, closed by server)."""
    s = socket.create_connection(("127.0.0.1", port), timeout=wait)
    s.sendall(payload)
    out, closed = b"", False
    end = time.time() + wait
    try:
        while time.time() < end:
            chunk = s.recv(65536)
            if not chunk:
                closed = True
                break
            out += chunk
    except socket.timeout:
        pass
    s.close()
    return out, closed


J = {"Content-Type": "application/json"}

# ---------------------------------------------------------------- Host check (DNS rebinding), local app only
for h in ("127.0.0.1:%d" % LPORT, "localhost:%d" % LPORT, "[::1]:%d" % LPORT, "LOCALHOST:%d" % LPORT):
    code, _, _ = req(LPORT, "GET", "/api/settings", host=h)
    check(code == 200, "local Host %s accepted (%s)" % (h, code))
for h in ("evil.example:%d" % LPORT, "evil.example", "127.0.0.1", "localhost:1", "127.0.0.1.evil.example:%d" % LPORT, ""):
    code, _, d = req(LPORT, "GET", "/api/settings", host=h)
    check(code == 403, "local Host %r refused (%s)" % (h, code))
    code, _, d = req(LPORT, "GET", "/", host=h)
    check(code == 403, "local static with Host %r refused (%s)" % (h, code))
code, _, _ = req(WPORT, "GET", "/healthz", host="ah.example.org")
check(code == 200, "web version accepts its public Host (%s)" % code)

# ---------------------------------------------------------------- CSRF: Origin / Sec-Fetch-Site / Content-Type
code, _, _ = req(LPORT, "POST", "/api/alerts/seen", b"{}", dict(J, Origin="http://evil.example"))
check(code == 403, "cross-origin POST refused (%s)" % code)
code, _, _ = req(LPORT, "POST", "/api/alerts/seen", b"{}", dict(J, Origin="null"))
check(code == 403, "Origin null refused (%s)" % code)
code, _, _ = req(LPORT, "PUT", "/api/settings", b"{}", dict(J, Origin="http://localhost:%d" % LPORT),
                 host="127.0.0.1:%d" % LPORT)
check(code == 403, "Origin of another loopback name refused (%s)" % code)
code, _, _ = req(LPORT, "DELETE", "/api/shopping", None, {"Sec-Fetch-Site": "cross-site"})
check(code == 403, "Sec-Fetch-Site cross-site DELETE refused (%s)" % code)
code, _, _ = req(LPORT, "POST", "/api/alerts/seen", b"{}",
                 dict(J, Origin="http://127.0.0.1:%d" % LPORT, **{"Sec-Fetch-Site": "same-origin"}))
check(code == 200, "same-origin POST accepted (%s)" % code)
code, _, _ = req(LPORT, "POST", "/api/alerts/seen", b"{}", J)
check(code == 200, "POST without Origin (non-browser client) accepted (%s)" % code)
code, _, _ = req(LPORT, "POST", "/api/alerts/seen", b"{}", {"Content-Type": "text/plain"})
check(code == 415, "text/plain POST refused (%s)" % code)
code, _, _ = req(LPORT, "PUT", "/api/settings", b"{}", {"Content-Type": "application/x-www-form-urlencoded"})
check(code == 415, "form-encoded PUT refused (%s)" % code)
code, _, _ = req(LPORT, "PUT", "/api/settings", b"{}", {})
check(code == 415, "PUT with body but no Content-Type refused (%s)" % code)
code, _, _ = req(LPORT, "PUT", "/api/settings", b"{}", {"Content-Type": "application/json; charset=utf-8"})
check(code == 200, "application/json; charset=utf-8 accepted (%s)" % code)
code, _, _ = req(LPORT, "DELETE", "/api/shopping", None, {})
check(code == 200, "bodyless DELETE without Content-Type accepted, like api.js sends it (%s)" % code)
code, _, _ = req(WPORT, "POST", "/api/x", json.dumps({"m": "GET", "p": "/api/settings"}).encode(),
                 dict(J, Origin="https://evil.example"), host="ah.example.org")
check(code == 403, "web: cross-origin tunnel call refused (%s)" % code)
code, _, d = req(WPORT, "POST", "/api/x", json.dumps({"m": "GET", "p": "/api/settings"}).encode(),
                 dict(J, Origin="https://ah.example.org", **{"Sec-Fetch-Site": "same-origin"}), host="ah.example.org")
check(code == 200 and "r" in d, "web: same-origin tunnel call works (%s)" % code)
code, _, d = req(WPORT, "POST", "/api/x", json.dumps({"m": "GET", "p": "/api/settings"}).encode(),
                 dict(J, Origin="https://ah.example.org", **{"X-Forwarded-Host": "ah.example.org"}),
                 host="127.0.0.1:%d" % WPORT)
check(code == 200, "web: Origin matching X-Forwarded-Host behind a proxy works (%s)" % code)
code, _, _ = req(WPORT, "POST", "/api/x", b'{"m":"GET","p":"/api/settings"}', {"Content-Type": "text/plain"})
check(code == 415, "web: text/plain tunnel call refused (%s)" % code)

# ---------------------------------------------------------------- body limits and JSON strictness


def send_and_read(port, head, body, wait=10):
    """Send `head` + `body` while reading the answer at the same time, so an early answer (413) is never lost to a
    broken pipe on our side. -> (first status line, closed by server)."""
    s = socket.create_connection(("127.0.0.1", port), timeout=wait)
    s.sendall(head)

    def pump():
        try:
            s.sendall(body)
        except OSError:
            pass
    t = threading.Thread(target=pump, daemon=True)
    t.start()
    out, closed = b"", False
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                closed = True
                break
            out += chunk
    except OSError:
        pass
    s.close()
    t.join(5)
    return out.split(b"\r\n")[0], closed


code, _, _ = req(LPORT, "PUT", "/api/settings", b" " * (server.MAX_BODY + 1), J)
check(code == 413, "local body over MAX_BODY refused (%s)" % code)
# round 2: the 413 must reach a client that sends the whole body before reading (http.client), every time
codes = []
for _ in range(20):
    try:
        codes.append(req(LPORT, "PUT", "/api/settings", b" " * (server.MAX_BODY + 1), J)[0])
    except OSError as e:
        codes.append(type(e).__name__)
check(codes == [413] * 20, "oversized PUT: 413 delivered 20/20 times (%s)" % sorted(set(map(str, codes))))
big = server.DRAIN_MAX + (1 << 20)  # too big to read away: answered, half-closed, drained for LINGER_SECONDS
try:
    code = req(LPORT, "PUT", "/api/settings", b" " * big, J)[0]
except OSError as e:
    code = type(e).__name__
check(code == 413, "very large PUT (%d MB): 413 still delivered (%s)" % (big >> 20, code))
line, closed = send_and_read(LPORT, b"PUT /api/settings HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Type: application/json"
                             b"\r\nContent-Length: %d\r\n\r\n" % (LPORT, big), b" " * big)
check(line.startswith(b"HTTP/1.1 413") and closed, "very large PUT, raw: 413 and closed (%r)" % line)
code, _, d = req(LPORT, "PUT", "/api/settings", b'{"shopping_max_markup": NaN}', J)
check(code == 400, "NaN in a JSON body refused (%s %s)" % (code, d))

# ---------------------------------------------------------------- request framing (smuggling / desync)
out, closed = raw_exchange(LPORT, b"POST /api/alerts/seen HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Type: application/json"
                                  b"\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n" % LPORT)
check(out.startswith(b"HTTP/1.1 400") and closed and out.count(b"HTTP/1.1") == 1,
      "Transfer-Encoding refused with 400 and the connection closed (%r)" % out[:40])
out, closed = raw_exchange(LPORT, b"GET /api/settings HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nTransfer-Encoding: chunked\r\n"
                                  b"Content-Length: 5\r\n\r\n0\r\n\r\n" % LPORT)
check(out.startswith(b"HTTP/1.1 400") and closed, "TE + CL on GET refused (%r)" % out[:40])
for cl in (b"Content-Length: 2\r\nContent-Length: 2", b"Content-Length: -1", b"Content-Length: 1x", b"Content-Length: +2",
           b"Content-Length: 2, 2"):
    out, closed = raw_exchange(LPORT, b"POST /api/alerts/seen HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                                      b"Content-Type: application/json\r\n%s\r\n\r\n{}" % (LPORT, cl))
    check(out.startswith(b"HTTP/1.1 400") and closed, "bad framing %r refused (%r)" % (cl, out[:40]))
# a GET with a body: the body is consumed, so a request hidden in it is never run
hidden = b"DELETE /api/shopping HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n" % LPORT
out, closed = raw_exchange(LPORT, b"GET /api/settings HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Length: %d\r\n\r\n%s"
                           % (LPORT, len(hidden), hidden))
check(out.startswith(b"HTTP/1.1 200") and out.count(b"HTTP/1.1 ") == 1,
      "GET body consumed, request smuggled in it not run (%d responses)" % out.count(b"HTTP/1.1 "))
out, closed = raw_exchange(LPORT, b"GET /api/settings HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Length: 4\r\n\r\nabcd"
                                  b"GET /api/settings HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n" % (LPORT, LPORT))
check(out.count(b"HTTP/1.1 200") == 2, "keep-alive still works after a GET with a small body")
out, closed = raw_exchange(LPORT, b"GET /api/settings HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Length: %d\r\n\r\n"
                           % (LPORT, server.MAX_BODY + 10))
check(out.startswith(b"HTTP/1.1 413") and closed, "GET with a huge body: 413 and closed (%r)" % out[:40])
out, closed = raw_exchange(WPORT, b"GET /api/sync/state HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n%s"
                           % (len(hidden), hidden))
check(out.count(b"HTTP/1.1 ") == 1 and closed, "sync GET with unread body closes the connection")
out, closed = raw_exchange(LPORT, b"POST /api/alerts/seen HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Type: application/json"
                                  b"\r\nOrigin: http://evil.example\r\nContent-Length: %d\r\n\r\n%s" % (LPORT, len(hidden), hidden))
check(out.startswith(b"HTTP/1.1 403") and out.count(b"HTTP/1.1 ") == 1 and closed,
      "refused request with unread body closes the connection")

# ---------------------------------------------------------------- security headers / CSP
code, r, body = req(LPORT, "GET", "/")
csp = r.getheader("Content-Security-Policy") or ""
check(code == 200 and r.getheader("X-Content-Type-Options") == "nosniff" and r.getheader("X-Frame-Options") == "DENY"
      and r.getheader("Referrer-Policy") == "strict-origin-when-cross-origin", "security headers on index")
want = server._sha256(INLINE.encode("utf-8"))
for part in ("default-src 'self'", "frame-ancestors 'none'", "base-uri 'none'", "object-src 'none'",
             "connect-src 'self'", want, "img-src 'self' data: https://wow.zamimg.com"):
    check(part in csp, "CSP contains %s" % part)
script_src = csp.split("script-src")[1].split(";")[0]
check("'unsafe-inline'" not in script_src and "http" not in script_src and "'unsafe-hashes'" not in script_src,
      "script-src: only 'self' and hashes (%s)" % script_src)
code, r, _ = req(LPORT, "GET", "/api/settings")
check(r.getheader("X-Content-Type-Options") == "nosniff" and "default-src 'none'" in
      (r.getheader("Content-Security-Policy") or ""), "security headers on JSON")
code, r, _ = req(LPORT, "GET", "/app.js")
check(r.getheader("X-Frame-Options") == "DENY", "security headers on static files")
code, r, _ = req(WPORT, "GET", "/", host="ah.example.org")
check("sha256-" in (r.getheader("Content-Security-Policy") or "") and r.getheader("X-Frame-Options") == "DENY",
      "web version index has CSP")
# the real index.html: every inline script is covered by the computed hash
with open(os.path.join(ROOT, "web", "index.html"), "rb") as f:
    real = f.read()
real_csp = server.csp_for_html(real)
inline = server._INLINE_SCRIPT_RE.findall(real)
check(all(server._sha256(s) in real_csp for s in inline), "real index.html: %d inline script(s) hashed" % len(inline))
check(lsrv.RequestHandlerClass.timeout == server.SOCKET_TIMEOUT,
      "socket timeout set (%ss)" % lsrv.RequestHandlerClass.timeout)

# ---------------------------------------------------------------- sync push: content type, gzip cap
AUTH = {"Authorization": "Bearer " + TOKEN}
code, _, _ = req(WPORT, "POST", "/api/sync/push", gzip.compress(b"{}"), dict(AUTH, **{"Content-Type": "text/plain"}))
check(code == 415, "push with text/plain refused (%s)" % code)
code, _, _ = req(WPORT, "POST", "/api/sync/push", gzip.compress(b"{}"), {"Content-Type": "application/gzip"})
check(code == 401, "push without key refused (%s)" % code)
bomb = gzip.compress(b"\0" * (server.MAX_SYNC_JSON + 1024), 9)
t0 = time.time()
code, _, d = req(WPORT, "POST", "/api/sync/push", bomb, dict(AUTH, **{"Content-Type": "application/gzip"}))
check(code == 413, "gzip bomb (%d kB -> %d MB) refused (%s %s, %.1fs)"
      % (len(bomb) // 1024, server.MAX_SYNC_JSON >> 20, code, d, time.time() - t0))
code, _, d = req(WPORT, "POST", "/api/sync/push", gzip.compress(b'{"protocol": 2, "x": Infinity}'),
                 dict(AUTH, **{"Content-Type": "application/gzip"}))
check(code == 400, "Infinity in a push refused (%s %s)" % (code, d))
code, _, d = req(WPORT, "POST", "/api/sync/push", b"not gzip", dict(AUTH, **{"Content-Type": "application/gzip"}))
check(code == 400 and "Error" not in json.dumps(d), "bad gzip refused without exception text (%s %s)" % (code, d))
try:
    server.gunzip_capped(gzip.compress(b"{}") + b"junk", 100)
    check(False, "trailing garbage after gzip refused")
except server.BadRequest:
    check(True, "trailing garbage after gzip refused")
check(server.gunzip_capped(gzip.compress(b'{"a":1}'), 100) == b'{"a":1}', "gunzip_capped round trip")

# ---------------------------------------------------------------- no exception text to clients


def broken():
    raise RuntimeError("secret-detail /home/someone/db")


bsrv, BPORT = start(cfg, connect=broken)
code, _, d = req(BPORT, "GET", "/api/settings")
check(code == 500 and "secret" not in json.dumps(d) and "RuntimeError" not in json.dumps(d),
      "500 answer has no exception text (%s)" % d)

# ---------------------------------------------------------------- web version: bounded connection threads
old = server.MAX_WEB_CONNECTIONS
server.MAX_WEB_CONNECTIONS = 1
csrv, CPORT = start({"web": True}, web=True, connect=lambda: db.connect(os.path.join(tmp, "w.db")))
server.MAX_WEB_CONNECTIONS = old
hold = socket.create_connection(("127.0.0.1", CPORT))
time.sleep(0.3)
out, closed = raw_exchange(CPORT, b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
check(out.startswith(b"HTTP/1.1 503"), "over the connection limit: 503 (%r)" % out[:30])
hold.close()
time.sleep(0.3)
out, closed = raw_exchange(CPORT, b"GET /healthz HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
check(out.startswith(b"HTTP/1.1 200"), "slot freed after the connection ends (%r)" % out[:30])

# ================================================================ round 2

# ---------------------------------------------------------------- 1. memory: sync batch cap, one push at a time, /api/x
check(server.MAX_SYNC_JSON == 24 << 20 or os.environ.get("AHTOOL_MAX_SYNC_MB"),
      "sync batch cap 24 MB (%d MB)" % (server.MAX_SYNC_JSON >> 20))
GZ = dict(AUTH, **{"Content-Type": "application/gzip"})
old_wait = server.PUSH_WAIT
server.PUSH_WAIT = 0.3
server._PUSH_LOCK.acquire()
try:
    t0 = time.time()
    code, r, d = req(WPORT, "POST", "/api/sync/push", gzip.compress(b'{"protocol": 2}'), GZ)
    check(code == 503 and r.getheader("Retry-After") and "retry" in d.get("error", ""),
          "push while another is parsed: 503 busy, retry (%s %s, %.1fs)" % (code, d, time.time() - t0))
finally:
    server._PUSH_LOCK.release()
    server.PUSH_WAIT = old_wait
code, _, d = req(WPORT, "POST", "/api/sync/push", gzip.compress(b'{"protocol": 2}'), GZ)
check(code != 503, "push lock released again (%s)" % code)
results = []


def bomb_push():
    try:
        results.append(req(WPORT, "POST", "/api/sync/push", bomb, GZ)[0])
    except OSError as e:
        results.append(type(e).__name__)


ts = [threading.Thread(target=bomb_push) for _ in range(3)]
[t.start() for t in ts]
[t.join() for t in ts]
check(results.count(413) >= 1 and all(r in (413, 503) for r in results),
      "parallel gzip bombs with one key: refused (413) or told to wait (503, one push per key) (%s)" % results)
check(server.MAX_BODY <= 64 << 10 and server.MAX_TUNNEL_BODY >= 1 << 20, "body caps: API %d kB, /api/x %d kB"
      % (server.MAX_BODY >> 10, server.MAX_TUNNEL_BODY >> 10))
XH = dict(J, Origin="https://ah.example.org", **{"Sec-Fetch-Site": "same-origin"})
# a state filled to webstate.LIMITS is ~630 kB: over MAX_BODY, must still pass through the tunnel
big_state = {"known": {str(20000 + i): 1 for i in range(5000)},
             "watch": {str(10000 + i): {"max_price": 10 ** 9, "note": "n" * 500} for i in range(500)}}
xbody = json.dumps({"m": "GET", "p": "/api/watchlist", "s": big_state}).encode()
check(len(xbody) > server.MAX_BODY, "big state is over MAX_BODY (%d kB)" % (len(xbody) >> 10))
code, _, d = req(WPORT, "POST", "/api/x", xbody, XH, host="ah.example.org")
check(code == 200 and "r" in d, "web: a maxed-out browser state passes /api/x (%s)" % code)
code, _, d = req(WPORT, "POST", "/api/x", b" " * (server.MAX_TUNNEL_BODY + 1), XH, host="ah.example.org")
check(code == 413, "web: /api/x body over MAX_TUNNEL_BODY refused (%s)" % code)
code, _, d = req(WPORT, "PUT", "/api/settings", b" " * (server.MAX_BODY + 1), XH, host="ah.example.org")
check(code == 413, "web: direct API body over MAX_BODY refused (%s)" % code)
code, _, d = req(WPORT, "POST", "/api/x", b"[" * 100000 + b"]" * 100000, XH, host="ah.example.org")
check(code == 400, "web: deeply nested /api/x body -> 400, not 500 (%s)" % code)
old_wait = server.TUNNEL_WAIT
server.TUNNEL_WAIT = 0.3
held = 0
for _ in range(server.MAX_TUNNEL_PARSE):  # a handler may still be finishing the previous request: wait for its slot
    held += server._TUNNEL_SLOTS.acquire(timeout=5)
try:
    code, r, d = req(WPORT, "POST", "/api/x", json.dumps({"m": "GET", "p": "/api/settings"}).encode(), XH,
                     host="ah.example.org")
    check(code == 503 and r.getheader("Retry-After"), "web: all %d /api/x parse slots busy -> 503 (%s)" % (held, code))
finally:
    for _ in range(held):
        server._TUNNEL_SLOTS.release()
    server.TUNNEL_WAIT = old_wait
code, _, d = req(WPORT, "POST", "/api/x", json.dumps({"m": "GET", "p": "/api/settings"}).encode(), XH,
                 host="ah.example.org")
check(code == 200, "web: /api/x works again once slots are free (%s)" % code)

# ---------------------------------------------------------------- 2. slowloris: absolute deadlines, per-address cap
old_ht, old_bt = server.HEADER_TIMEOUT, server.BODY_TIMEOUT_MIN
server.HEADER_TIMEOUT, server.BODY_TIMEOUT_MIN = 1.0, 1.0


def trickle(port, first, step=b"a", every=0.25, limit=6.0):
    """Send `first`, then one byte every `every` s (well inside the per-recv timeout). -> seconds until the server
    dropped the connection (or `limit`)."""
    s = socket.create_connection(("127.0.0.1", port), timeout=limit)
    t0 = time.time()
    s.sendall(first)
    try:
        while time.time() - t0 < limit:
            s.sendall(step)
            time.sleep(every)
            s.setblocking(False)
            try:
                if s.recv(65536) == b"":
                    break
            except BlockingIOError:
                pass
            finally:
                s.setblocking(True)
    except OSError:
        pass
    s.close()
    return time.time() - t0


try:
    dt = trickle(WPORT, b"GET / HTTP/1.1\r\nX-a: ")
    check(dt < 3, "trickled headers dropped after the header deadline (%.1fs, deadline %ss, per-recv timeout %ss)"
          % (dt, server.HEADER_TIMEOUT, server.SOCKET_TIMEOUT))
    dt = trickle(LPORT, b"PUT /api/settings HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Type: application/json\r\n"
                 b"Content-Length: 1000\r\n\r\n" % LPORT, b" ")
    check(dt < 3, "trickled body dropped after the body deadline (%.1fs)" % dt)
    # an idle keep-alive connection is not cut by the header deadline: the clock starts at the first byte
    c = http.client.HTTPConnection("127.0.0.1", LPORT, timeout=5)
    c.connect()
    codes = []
    for _ in range(2):
        time.sleep(1.5)
        c.request("GET", "/api/settings")
        r = c.getresponse()
        r.read()
        codes.append(r.status)
    c.close()
    check(codes == [200, 200], "idle keep-alive longer than the header deadline still served, twice (%s)" % codes)
finally:
    server.HEADER_TIMEOUT, server.BODY_TIMEOUT_MIN = old_ht, old_bt

old_cap = server.MAX_CONN_PER_IP
server.MAX_CONN_PER_IP = 2
psrv, PPORT = start({"web": True}, web=True, connect=lambda: db.connect(os.path.join(tmp, "w.db")))
server.MAX_CONN_PER_IP = old_cap
holds = [socket.create_connection(("127.0.0.1", PPORT)) for _ in range(2)]
time.sleep(0.3)
out, closed = raw_exchange(PPORT, b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
check(out.startswith(b"HTTP/1.1 503") and b"nosniff" in out, "per-address cap: 3rd connection from one IP -> 503 (%r)"
      % out[:30])
holds.pop().close()
time.sleep(0.3)
out, closed = raw_exchange(PPORT, b"GET /healthz HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
check(out.startswith(b"HTTP/1.1 200"), "per-address slot freed (%r)" % out[:30])
for h in holds:
    h.close()

# behind a trusted proxy: the cap applies per X-Forwarded-For address (the entry the proxy appended), per request
gate = threading.Event()


def gated():
    gate.wait(10)
    return db.connect(os.path.join(tmp, "w.db"))


server.MAX_CONN_PER_IP = 1
tsrv, TPORT = start({"web": True, "trust_proxy": "1"}, web=True, connect=gated)
server.MAX_CONN_PER_IP = old_cap
inflight = threading.Thread(target=req, args=(TPORT, "GET", "/api/settings", None,
                                              {"X-Forwarded-For": "9.9.9.9, 1.2.3.4"}), daemon=True)
inflight.start()
time.sleep(0.3)
code, _, _ = req(TPORT, "GET", "/healthz", None, {"X-Forwarded-For": "1.2.3.4"})
check(code == 503, "trusted proxy: 2nd request from the same forwarded address -> 503 (%s)" % code)
code, _, _ = req(TPORT, "GET", "/healthz", None, {"X-Forwarded-For": "1.2.3.4, 5.6.7.8"})
check(code == 200, "trusted proxy: other forwarded address served, spoofed left entries ignored (%s)" % code)
code, _, _ = req(TPORT, "GET", "/healthz", None, {"X-Forwarded-For": "5.6.7.8, 1.2.3.4"})
check(code == 503, "trusted proxy: a forged first X-Forwarded-For entry does not dodge the cap (%s)" % code)
code, _, _ = req(TPORT, "GET", "/healthz")
check(code == 200, "trusted proxy: several connections from the proxy itself are fine (%s)" % code)
gate.set()
inflight.join(5)
for _ in range(20):  # the handler leaves the count right after its answer went out
    code, _, _ = req(TPORT, "GET", "/healthz", None, {"X-Forwarded-For": "1.2.3.4"})
    if code == 200:
        break
    time.sleep(0.1)
check(code == 200, "trusted proxy: forwarded address freed after its request (%s)" % code)
check(server._trusted_hops("1") == 1 and server._trusted_hops("2") == 2 and server._trusted_hops("") == 0
      and server._trusted_hops("no") == 0 and server._trusted_hops("true") == 1, "AHTOOL_TRUST_PROXY parsing")

# ---------------------------------------------------------------- 3. reverse proxy: Origin vs Host / public URL
X = json.dumps({"m": "GET", "p": "/api/settings"}).encode()
nginx_host = "127.0.0.1:%d" % WPORT
code, _, _ = req(WPORT, "POST", "/api/x", X, dict(J, Origin="https://ah.example.net", **{"Sec-Fetch-Site": "same-origin"}),
                 host=nginx_host)
check(code == 200, "web: nginx default (Host rewritten, no X-Forwarded-Host) + Sec-Fetch-Site same-origin (%s)" % code)
code, _, _ = req(WPORT, "POST", "/api/x", X, dict(J, Origin="https://ah.example.net"), host="ah.example.net:443")
check(code == 200, "web: Host ah.example.net:443 matches Origin https://ah.example.net (%s)" % code)
code, _, _ = req(WPORT, "POST", "/api/x", X, dict(J, Origin="http://ah.example.net"), host="ah.example.net:80")
check(code == 200, "web: Host :80 matches an http Origin (%s)" % code)
code, _, _ = req(WPORT, "POST", "/api/x", X, dict(J, Origin="https://ah.example.net"), host="ah.example.net:8443")
check(code == 403, "web: different port is another origin (%s)" % code)
code, _, _ = req(WPORT, "POST", "/api/x", X, dict(J, Origin="https://ah.example.net"), host=nginx_host)
check(code == 403, "web: Host rewritten, no Sec-Fetch-Site, no public URL configured -> refused (%s)" % code)
code, _, _ = req(WPORT, "POST", "/api/x", X, dict(J, Origin="https://evil.example", **{"Sec-Fetch-Site": "cross-site"}),
                 host=nginx_host)
check(code == 403, "web: cross-site still refused behind a proxy (%s)" % code)
code, _, _ = req(WPORT, "POST", "/api/x", X, dict(J, Origin="https://ah.example.net", **{"Sec-Fetch-Site": "same-site"}),
                 host=nginx_host)
check(code == 403, "web: same-site (a sibling subdomain) refused (%s)" % code)
code, _, _ = req(WPORT, "POST", "/api/x", X, dict(J, Origin="null"), host=nginx_host)
check(code == 403, "web: Origin null refused (%s)" % code)
usrv, UPORT = start({"web": True, "public_url": "https://ah.example.net/"}, web=True,
                    connect=lambda: db.connect(os.path.join(tmp, "w.db")))
code, _, _ = req(UPORT, "POST", "/api/x", X, dict(J, Origin="https://ah.example.net"), host="127.0.0.1:%d" % UPORT)
check(code == 200, "web: Origin = AHTOOL_PUBLIC_URL accepted whatever Host the proxy sends (%s)" % code)
code, _, _ = req(UPORT, "POST", "/api/x", X, dict(J, Origin="https://ah.example.net:443"), host="127.0.0.1")
check(code == 200, "web: public URL compared with default ports (%s)" % code)
for bad in ("http://ah.example.net", "https://ah.example.net:8443", "https://evil.example", "https://ah.example.net.evil"):
    code, _, _ = req(UPORT, "POST", "/api/x", X, dict(J, Origin=bad), host="127.0.0.1:%d" % UPORT)
    check(code == 403, "web: Origin %s is not the public URL (%s)" % (bad, code))
code, _, _ = req(LPORT, "PUT", "/api/settings", b"{}", dict(J, Origin="http://evil.example", **{"Sec-Fetch-Site": "same-origin"}))
check(code == 403, "local app: Origin still checked even with Sec-Fetch-Site same-origin (%s)" % code)
check(server.split_hostport("Ah.Example.Net:443", "https") == ("ah.example.net", 443)
      and server.split_hostport("[::1]:8642") == ("::1", 8642) and server.split_hostport("a:b") is None
      and server.parse_origin("https://x.y") == ("https", "x.y", 443) and server.parse_origin("null") is None
      and server.parse_origin("https://u@x.y") is None, "host/origin normalisation helpers")

# ---------------------------------------------------------------- 4. malformed header lines
for name, hdrs in (("space before colon", b"X-Foo : bar\r\nContent-Length: 2\r\n"),
                   ("space before colon, TE hidden", b"X-Foo : bar\r\nTransfer-Encoding: chunked\r\n"),
                   ("line without colon", b"garbage\r\nContent-Length: 2\r\n"),
                   ("obs-fold", b"Content-Length: 2\r\nX-Long: a\r\n b\r\n"),
                   ("empty name", b":x\r\nContent-Length: 2\r\n")):
    out, closed = raw_exchange(LPORT, b"PUT /api/settings HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Type: application/json"
                                      b"\r\n%s\r\n{}GET /api/settings HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n"
                               % (LPORT, hdrs, LPORT))
    check(out.startswith(b"HTTP/1.1 400") and out.count(b"HTTP/1.1 ") == 1 and closed,
          "malformed header (%s): 400, closed, nothing after it parsed (%r)" % (name, out[:30]))
out, closed = raw_exchange(LPORT, b"GET /api/settings HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n%s\r\n"
                           % (LPORT, b"".join(b"X-%d: %s\r\n" % (i, b"a" * 1000) for i in range(40))))
check(out.startswith(b"HTTP/1.1 431") and closed, "headers over MAX_HEADER_BYTES: 431 (%r)" % out[:30])

# ---------------------------------------------------------------- 5. base-class error pages carry the security headers
for name, raw, want in (("bad request line", b"GARBAGE\r\n\r\n", b"400"),
                        ("bad version", b"GET / HTTP/9.9\r\nHost: x\r\n\r\n", b"505"),
                        ("unknown method", b"PATCH / HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n" % LPORT, b"501"),
                        ("long URL", b"GET /" + b"a" * (server.MAX_REQUEST_LINE + 10) + b" HTTP/1.1\r\n\r\n", b"414"),
                        ("long header", b"GET / HTTP/1.1\r\nX: " + b"a" * 70000 + b"\r\n\r\n", b"431"),
                        ("HTTP/0.9", b"GET /\r\n\r\n", b"400")):
    for port in (LPORT, WPORT):
        out, closed = raw_exchange(port, raw)
        head = out.split(b"\r\n\r\n")[0]
        check(out.startswith(b"HTTP/1.1 " + want) and b"X-Content-Type-Options: nosniff" in head
              and b"X-Frame-Options: DENY" in head and b"Content-Security-Policy: default-src 'none'" in head
              and b"application/json" in head and b"<html" not in out.lower() and b"Python" not in head and closed,
              "%s (%s): %s with security headers, JSON, closed (%r)"
              % (name, "web" if port == WPORT else "local", want.decode(), out[:60]))

# ================================================================ round 3
from ahtool import keys, sync  # noqa: E402

_kc = db.connect(os.path.join(tmp, "w.db"))
sync.ensure_schema(_kc)
K_MAL = keys.create(_kc, "R3Mal")
K_CAT = keys.create(_kc, "R3Cat")
_kc.close()
MAL = {"Authorization": "Bearer " + K_MAL, "Content-Type": "application/gzip"}
CAT = {"Authorization": "Bearer " + K_CAT, "Content-Type": "application/gzip"}
OK_BATCH = gzip.compress(b'{"protocol": 2}')


def trickle_push(headers, length=1 << 20):
    """A push whose body arrives slowly: headers and a few bytes, the socket left open."""
    s = socket.create_connection(("127.0.0.1", WPORT))
    s.sendall(b"POST /api/sync/push HTTP/1.1\r\nHost: x\r\n" + b"".join(
        b"%s: %s\r\n" % (k.encode(), v.encode()) for k, v in headers.items())
        + b"Content-Length: %d\r\n\r\n\x1f\x8b" % length)
    return s


# ---------------------------------------------------------------- R3.1 a slow body holds no one else's push
slow = trickle_push(MAL)
time.sleep(0.3)
check(not server._PUSH_LOCK._busy, "a push still receiving its body does not hold the parse/apply lock")
t0 = time.time()
code, _, d = req(WPORT, "POST", "/api/sync/push", OK_BATCH, GZ)
check(code == 200 and time.time() - t0 < 3, "owner push while a contributor trickles a body: served (%s %.1fs)"
      % (code, time.time() - t0))
code, _, d = req(WPORT, "POST", "/api/sync/push", OK_BATCH, CAT)
check(code == 200, "another contributor's push is served too (%s %s)" % (code, d))
code, r, d = req(WPORT, "POST", "/api/sync/push", OK_BATCH, MAL)
check(code == 503 and r.getheader("Retry-After") and "this key" in d.get("error", ""),
      "a second push with the same key while one is in flight: 503 retry (%s %s)" % (code, d))
slow.close()
for _ in range(30):
    code, _, d = req(WPORT, "POST", "/api/sync/push", OK_BATCH, MAL)
    if code == 200:
        break
    time.sleep(0.1)
check(code == 200, "the key's push slot is freed when the slow push ends (%s)" % code)

# all contributor read slots taken: contributors wait/503, an owner still has its own slot
old_wait = server.PUSH_WAIT
server.PUSH_WAIT = 0.3
held = sum(server._PUSH_READS.acquire(timeout=5) for _ in range(server.MAX_PUSH_READS))
try:
    code, r, _ = req(WPORT, "POST", "/api/sync/push", OK_BATCH, CAT)
    check(code == 503 and r.getheader("Retry-After"), "read slots all busy: contributor push 503 (%s)" % code)
    code, _, _ = req(WPORT, "POST", "/api/sync/push", OK_BATCH, GZ)
    check(code == 200, "read slots all busy: owner push uses the slot kept for owners (%s)" % code)
finally:
    for _ in range(held):
        server._PUSH_READS.release()
    server.PUSH_WAIT = old_wait

# the parse/apply lock: a waiting owner goes before a waiting contributor
gate = server._PushGate()
gate.acquire()
order = []


def waiter(owner, delay):
    time.sleep(delay)
    if gate.acquire(owner=owner, timeout=5):
        order.append("owner" if owner else "contributor")
        time.sleep(0.05)
        gate.release()


ws = [threading.Thread(target=waiter, args=(False, 0)), threading.Thread(target=waiter, args=(True, 0.2))]
[t.start() for t in ws]
time.sleep(0.4)
gate.release()
[t.join() for t in ws]
check(order == ["owner", "contributor"], "push lock: owner first (%s)" % order)
free = gate.acquire(timeout=0)
if free:
    gate.release()
check(free, "push lock is free afterwards")

# ---------------------------------------------------------------- R3.2 pulls: bounded number at once
old_pw = server.PULL_WAIT
server.PULL_WAIT = 0.3
held = sum(server._PULL_SLOTS.acquire(timeout=5) for _ in range(server.PULL_SLOTS))
try:
    code, r, d = req(WPORT, "GET", "/api/sync/pull?cursor=%7B%7D&v=3", None, AUTH)
    check(code == 503 and r.getheader("Retry-After"), "all pull slots busy: 503 with Retry-After (%s %s)" % (code, d))
finally:
    for _ in range(held):
        server._PULL_SLOTS.release()
    server.PULL_WAIT = old_pw
code, _, d = req(WPORT, "GET", "/api/sync/pull?cursor=%7B%7D&v=3", None, AUTH)
check(code == 200 and d.get("pull_version") == 3 and "scan_deletes" in d, "pull slot freed, v=3 page (%s)" % code)

# ---------------------------------------------------------------- R3.7 value bomb from a contributor
vbomb = gzip.compress(b"[" + b"[]," * 4_000_000 + b"[]]", 9)
code, _, d = req(WPORT, "POST", "/api/sync/push", vbomb, CAT)
check(code == 413 and "too many values" in d.get("error", ""),
      "contributor: %d kB gzip of '[],' refused before json.loads (%s %s)" % (len(vbomb) >> 10, code, d))

# ---------------------------------------------------------------- R3.6 proxy mode: per-peer cap, trusted proxies
old_px = server.MAX_CONN_PER_PROXY
server.MAX_CONN_PER_PROXY = 2
xsrv, XPORT = start({"web": True, "trust_proxy": "1"}, web=True, connect=lambda: db.connect(os.path.join(tmp, "w.db")))
server.MAX_CONN_PER_PROXY = old_px
holds = [socket.create_connection(("127.0.0.1", XPORT)) for _ in range(2)]
time.sleep(0.3)
out, closed = raw_exchange(XPORT, b"GET /healthz HTTP/1.1\r\nHost: x\r\nX-Forwarded-For: 10.9.9.9\r\n\r\n")
check(out.startswith(b"HTTP/1.1 503"), "proxy mode: a TCP peer still has a connection cap (%r)" % out[:30])
for h in holds:
    h.close()

gate2 = threading.Event()


def gated2():
    gate2.wait(10)
    return db.connect(os.path.join(tmp, "w.db"))


old_cap = server.MAX_CONN_PER_IP
server.MAX_CONN_PER_IP = 1
ysrv, YPORT = start({"web": True, "trust_proxy": "1", "trusted_proxies": "10.0.0.1, 192.168.0.0/16"}, web=True,
                    connect=gated2)
zsrv, ZPORT = start({"web": True, "trusted_proxies": "127.0.0.1"}, web=True, connect=gated2)
server.MAX_CONN_PER_IP = old_cap
for port, want, label in ((YPORT, 503, "peer not in AHTOOL_TRUSTED_PROXIES: its X-Forwarded-For is ignored"),
                          (ZPORT, 200, "peer in AHTOOL_TRUSTED_PROXIES (implies one proxy): X-Forwarded-For used")):
    gate2.clear()
    t = threading.Thread(target=req, args=(port, "GET", "/api/settings", None, {"X-Forwarded-For": "1.2.3.4"}),
                         daemon=True)
    t.start()
    time.sleep(0.3)
    code, _, _ = req(port, "GET", "/healthz", None, {"X-Forwarded-For": "5.6.7.8"})
    check(code == want, "%s (%s)" % (label, code))
    gate2.set()
    t.join(5)
check(server._peer_in("::ffff:10.0.0.1", server._trusted_proxies("10.0.0.1"))
      and server._peer_in("192.168.3.4", server._trusted_proxies("10.0.0.1, 192.168.0.0/16"))
      and not server._peer_in("10.0.0.2", server._trusted_proxies("10.0.0.1"))
      and server._trusted_proxies("") is None, "AHTOOL_TRUSTED_PROXIES parsing and matching")
try:
    server._trusted_proxies("10.0.0.1, proxy.local")
    check(False, "AHTOOL_TRUSTED_PROXIES with a host name refused")
except ValueError:
    check(True, "AHTOOL_TRUSTED_PROXIES with a host name refused")
for s_ in (xsrv, ysrv, zsrv):
    s_.shutdown()

for s in (lsrv, wsrv, bsrv, csrv, psrv, tsrv, usrv):
    s.shutdown()
print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
