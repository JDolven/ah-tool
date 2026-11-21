"""HTTP server: JSON API + static files from web/."""
import base64
import hashlib
import json
import mimetypes
import os
import re
import socket
import sys
import threading
import traceback
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import alerts as alerts_mod
from . import analytics, market, notify, shopping, tooltip, userstate, webstate

# Web version: endpoints that only make sense on the PC that runs WoW, or that would expose your own trades.
WEB_BLOCKED = {
    ("POST", "/api/ingest"), ("POST", "/api/notify/test"), ("POST", "/api/alerts/evaluate"),
    ("GET", "/api/postings"), ("GET", "/api/market/my"),
    ("HEAD", "/api/postings"), ("HEAD", "/api/market/my"),
}
MAX_BODY = 1 << 20          # normal API calls (local app and web version)
# One sync batch unpacked. sync.py cuts row data into batches of MAX_BATCH_ROWS (150k rows, ~15-20 MB of JSON);
# 64 MB leaves room for the static tables in the first batch. Override with AHTOOL_MAX_SYNC_MB if ever needed.
MAX_SYNC_JSON = int(os.environ.get("AHTOOL_MAX_SYNC_MB") or 64) << 20
MAX_SYNC_BODY = MAX_SYNC_JSON  # the gzip body can never legitimately be larger than what it unpacks to
SYNC_CONTENT_TYPE = "application/gzip"  # what sync._request sends; plain application/json is accepted as well
SOCKET_TIMEOUT = 30          # seconds of silence before a connection is dropped (slowloris)
MAX_WEB_CONNECTIONS = 128    # concurrent connections served at once in the web version
UNSAFE_METHODS = ("POST", "PUT", "DELETE")
_CL_RE = re.compile(r"[0-9]{1,15}")

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


def csp_for_html(html):
    """Content-Security-Policy for a page: inline <script>s are allowed by hash, computed from the served HTML."""
    hashes = [_sha256(m.group(1)) for m in _INLINE_SCRIPT_RE.finditer(html)]
    handlers = ["'unsafe-hashes'"] + [_sha256(h.encode("utf-8")) for h in INLINE_HANDLERS] if INLINE_HANDLERS else []
    return "; ".join([
        "default-src 'self'",
        " ".join(["script-src 'self'"] + hashes + handlers),
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src 'self' https://fonts.gstatic.com",
        "img-src 'self' data: https://wow.zamimg.com",
        "connect-src 'self'",
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
                known_only=qs.get("known_only", "0").lower() in ("1", "true", "yes"))


def _leveling():
    try:
        from . import leveling
    except ImportError as e:
        if getattr(e, "name", None) in ("ahtool.leveling", "leveling"):
            raise NotImplementedYet("leveling planner not installed")
        raise
    return leveling


def make_server(cfg, connect=None, host="127.0.0.1", port=None, web_dir=WEB_DIR, web=False):
    """web=True: the shared web version (see webstate.py and sync.py) instead of the local app."""
    connect = connect or default_connect
    port = int(port if port is not None else cfg.get("port", 8642))
    web_root = os.path.normpath(web_dir)

    extra_hosts = {h.lower() for h in (cfg.get("allowed_hosts") or []) if isinstance(h, str)}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "AHTool"
        timeout = SOCKET_TIMEOUT  # applied to the socket by StreamRequestHandler.setup()

        def log_message(self, fmt, *args):
            if web and self.path == "/healthz":
                return
            sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

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
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self._send(code, body, "application/json; charset=utf-8", {"Cache-Control": "no-store"})

        def _fail(self, code, msg):
            """Error answer that also ends the connection (the request may not have been read completely)."""
            self.close_connection = True
            return self._json({"error": msg}, code)

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
                csp = csp_for_html(data)
            self._send(200, data, ctype, {"Cache-Control": "no-store"}, csp=csp)

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
                raise TooLarge("request body too large")
            self._body_pending = 0
            data = self.rfile.read(n) if n else b""
            if len(data) != n:
                self.close_connection = True
                raise BadRequest("incomplete request body")
            return data

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
            origin = self.headers.get("Origin")
            if origin is None:
                return True
            netloc = urlparse(origin.strip()).netloc.lower() if origin.strip().lower() != "null" else ""
            hosts = {(self.headers.get("Host") or "").strip().lower()}
            if web and self.headers.get("X-Forwarded-Host"):  # behind a reverse proxy that rewrites Host
                hosts.add(self.headers["X-Forwarded-Host"].split(",")[0].strip().lower())
            return bool(netloc) and netloc in hosts

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
            try:
                if not self._host_ok():
                    return self._fail(403, "forbidden host")
                if method in UNSAFE_METHODS and not self._same_origin():
                    return self._fail(403, "cross-origin request refused")
                return self._dispatch(method)
            finally:
                if self._body_pending:  # body left unread: the next "request" on this connection would be the body
                    self.close_connection = True

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
                    raw = self._read_body(MAX_BODY)
                except TooLarge as e:
                    return self._fail(413, str(e))
                except BadRequest as e:
                    return self._fail(400, str(e))
            if web and path == "/healthz":
                return self._json({"ok": True})
            if web and path == "/api/x":
                return self._tunnel(method, raw)
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
            try:
                req = loads_strict(raw)
                m, target = req["m"], req["p"]
                if m not in ("GET", "POST", "PUT", "DELETE") or not isinstance(target, str):
                    raise ValueError
            except (ValueError, KeyError, TypeError, UnicodeDecodeError):
                return self._json({"error": "bad request"}, 400)
            u = urlparse(target)
            path = u.path.rstrip("/")
            if not path.startswith("/api/") or path.startswith("/api/sync") or path == "/api/x":
                return self._json({"error": "unknown endpoint"}, 404)
            qs = {k: v[-1] for k, v in parse_qs(u.query).items()}
            body = req.get("b")
            braw = json.dumps(body).encode("utf-8") if body is not None else b""
            conn = None
            try:
                conn = connect()
                webstate.install(conn, req.get("s"))
                changed = m != "GET"
                result = self._route(m, path, qs, conn, braw)
                if path == "/api/status":
                    # Alerts are evaluated per browser, once per new batch of prices.
                    st = req.get("s") if isinstance(req.get("s"), dict) else {}
                    marker = webstate.data_marker(conn)
                    if marker and st.get("eval") != marker:
                        if st.get("eval") is not None:
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
                if conn is not None:
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
                    return self._json(sync.pull_page(conn, cursor, qs.get("limit") or sync.PULL_LIMIT))
                if method == "POST" and path == "/api/sync/push":
                    gz = self._ctype_ok((SYNC_CONTENT_TYPE,), -1)
                    if not gz and not self._ctype_ok(("application/json",), -1):
                        return self._fail(415, "Content-Type must be " + SYNC_CONTENT_TYPE)
                    try:
                        raw = self._read_body(MAX_SYNC_BODY)
                        if gz:
                            raw = gunzip_capped(raw, MAX_SYNC_JSON)
                    except TooLarge as e:
                        return self._fail(413, str(e))
                    try:
                        payload = sync.loads_strict(raw.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        raise BadRequest("sync batch is not valid JSON")
                    del raw
                    res = sync.apply(conn, cfg, payload, source)
                    analytics.invalidate()
                    return self._json(res)
                return self._json({"error": "unknown endpoint"}, 404)
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

        # ------------------------------------------------------------ routes

        def _route(self, method, path, qs, conn, raw=b""):
            if web and (method, path) in WEB_BLOCKED:
                raise NotFound("not available in the web version")
            realm = qs.get("realm") or None
            A = analytics
            body = None
            if method in ("POST", "PUT", "DELETE") and raw.strip():
                try:
                    body = loads_strict(raw)
                except (ValueError, UnicodeDecodeError):
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

        # The web version is public: bound the number of connection threads. Over the limit a connection gets a
        # plain 503 and is closed instead of starting yet another thread.
        slots = threading.BoundedSemaphore(MAX_WEB_CONNECTIONS) if web else None

        def process_request(self, request, client_address):
            if self.slots is not None and not self.slots.acquire(blocking=False):
                try:
                    request.settimeout(2)
                    request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n"
                                    b"Connection: close\r\nRetry-After: 5\r\n\r\n")
                except OSError:
                    pass
                self.shutdown_request(request)
                return
            super().process_request(request, client_address)

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                if self.slots is not None:
                    self.slots.release()

    return Server((host, port), Handler)
