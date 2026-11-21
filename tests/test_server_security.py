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
with open(os.path.join(webdir, "index.html"), "w", encoding="utf-8") as f:
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
code, _, _ = req(LPORT, "PUT", "/api/settings", b" " * (server.MAX_BODY + 1), J)
check(code == 413, "local body over MAX_BODY refused (%s)" % code)
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

for s in (lsrv, wsrv, bsrv, csrv):
    s.shutdown()
print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
