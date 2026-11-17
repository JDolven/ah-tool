"""HTTP server: JSON API + static files from web/."""
import json
import mimetypes
import os
import re
import socket
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import alerts as alerts_mod
from . import analytics, market, notify, shopping, tooltip, userstate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "web")

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")


def default_connect():
    from . import db
    return db.connect()


class NotFound(Exception):
    pass


class BadRequest(Exception):
    pass


class NotImplementedYet(Exception):
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


def make_server(cfg, connect=None, host="127.0.0.1", port=None, web_dir=WEB_DIR):
    connect = connect or default_connect
    port = int(port if port is not None else cfg.get("port", 8642))
    web_root = os.path.normpath(web_dir)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "AHTool"

        def log_message(self, fmt, *args):
            sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

        def _send(self, code, body, ctype, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self._send(code, body, "application/json; charset=utf-8", {"Cache-Control": "no-store"})

        def _static(self, path):
            rel = unquote(path).lstrip("/") or "index.html"
            full = os.path.normpath(os.path.join(web_root, rel))
            if full != web_root and not full.startswith(web_root + os.sep):
                return self._json({"error": "forbidden"}, 403)
            if not os.path.isfile(full):
                if "." in os.path.basename(rel):
                    return self._json({"error": "not found: " + path}, 404)
                full = os.path.join(web_root, "index.html")  # SPA fallback
                if not os.path.isfile(full):
                    return self._json({"error": "web/index.html mangler"}, 404)
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                ctype += "; charset=utf-8"
            with open(full, "rb") as f:
                data = f.read()
            self._send(200, data, ctype, {"Cache-Control": "no-store"})

        def _read_body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def _handle(self, method):
            raw = self._read_body() if method in ("POST", "PUT", "DELETE") else b""
            u = urlparse(self.path)
            path = u.path
            if not path.startswith("/api/"):
                if method not in ("GET", "HEAD"):
                    return self._json({"error": "method not allowed"}, 405)
                return self._static(path)
            qs = {k: v[-1] for k, v in parse_qs(u.query).items()}
            conn = None
            try:
                conn = connect()
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
            except Exception as e:  # noqa
                traceback.print_exc()
                self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

        def _route(self, method, path, qs, conn, raw=b""):
            realm = qs.get("realm") or None
            A = analytics
            body = None
            if method in ("POST", "PUT", "DELETE") and raw.strip():
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    raise BadRequest("request body is not valid JSON")
            if method == "POST":
                if path == "/api/ingest":
                    from . import ingest
                    res = ingest.ingest(conn, cfg, force=True)
                    try:
                        res["logger"] = market.ingest(conn, cfg, force=True)
                    except Exception as e:  # noqa
                        res["logger"] = {"error": str(e)}
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
                return A.status(conn, cfg, realm)
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

    return Server((host, port), Handler)
