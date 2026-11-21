"""Web version: sync from a local DB, privacy of your own trades, per-browser state. Run: python tests/test_web.py"""
import gzip
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import make_fixture_db  # noqa: E402
from ahtool import analytics, db, market, server, sync  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print("FAIL:", msg)
    else:
        print("PASS", msg)


tmp = tempfile.mkdtemp()
TOKEN = "t" * 40

# ---------------------------------------------------------------- local DB: fixture + logger data with own trades
local_path = os.path.join(tmp, "local.db")
make_fixture_db.build(local_path)
lconn = db.connect(local_path)
now = int(time.time())


def scan(t, rows, char="Me"):
    d = ";".join(",".join(str(x) for x in r) for r in rows)
    return '{ ["t"] = %d, ["realm"] = "PvP", ["faction"] = "Alliance", ["char"] = "%s", ["src"] = "addon", ' \
           '["total"] = %d, ["n"] = %d, ["d"] = "%s" }' % (t, char, len(rows), len(rows), d)


s1 = [(2453, 0, 5, 5000, 4000, 0, 4, "Alice")] * 3 + [(2453, 0, 5, 4500, 4000, 0, 4, "Me")] \
    + [(118, 0, 1, 900, 800, 0, 4, "Altsy-PvP")]
s2 = [(2453, 0, 5, 5000, 4000, 0, 4, "Alice")] * 2 + [(118, 0, 1, 900, 800, 0, 4, "Altsy-PvP")]
lua = """AHToolLoggerDB = {
["v"] = 1,
["scans"] = { %s, %s },
["owned"] = { ["PvP-Me"] = { ["t"] = %d, ["realm"] = "PvP", ["char"] = "Me", ["d"] = "2453,0,5,4500,4000,0,4,Me,0" } },
["mail"] = { ["sold|X|SecretBuyer|1|1|1"] = { ["t"] = %d, ["type"] = "sold", ["item"] = "X", ["player"] = "SecretBuyer",
     ["price"] = 4500, ["count"] = 5, ["char"] = "Me" } },
}
""" % (scan(now - 7200, s1), scan(now - 3600, s2, "Other"), now - 100, now - 3000)
lp = os.path.join(tmp, "AHToolLogger.lua")
with open(lp, "w", encoding="utf-8") as f:
    f.write(lua)
lcfg = {"logger_savedvariables": lp, "ah_cut": 0.05, "deal_threshold": 0.3, "baseline_days": 14,
        "trend_min_profit": 50, "addons_dir": "", "sync_hide_sellers": ["Altsy"],
        "savedvariables": "C:\\secret\\path\\Auctionator.lua"}
r = market.ingest(lconn, lcfg)
check(r.get("scans") == 2 and r.get("mail") == 1, "local logger ingest %s" % r)
check(lconn.execute("SELECT COUNT(*) FROM ah_sales WHERE owner='Me'").fetchone()[0] == 1, "local sale by Me exists")

# ---------------------------------------------------------------- web server on an empty DB
web_path = os.path.join(tmp, "web.db")
wcfg = {"web": True, "sync_token": TOKEN}
webdir = tempfile.mkdtemp()
with open(os.path.join(webdir, "index.html"), "w", encoding="utf-8") as f:
    f.write('<!doctype html><html lang="en" data-theme="dark"><body>X</body></html>')
srv = server.make_server(wcfg, lambda: db.connect(web_path), port=0, web_dir=webdir, web=True)
BASE = "http://127.0.0.1:%d" % srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
lcfg.update(sync_url=BASE, sync_token=TOKEN)


def call(method, path, body=None, headers=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=dict(
        {"Content-Type": "application/json"}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"null"), resp
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null"), e


def x(m, p, b=None, s=None):
    code, res, _ = call("POST", "/api/x", {"m": m, "p": p, "b": b, "s": s})
    return code, res


# ---------------------------------------------------------------- sync auth
code, res, _ = call("GET", "/api/sync/state", headers={"Authorization": "Bearer wrong"})
check(code == 401, "wrong token -> 401 (%s)" % code)
code, res, _ = call("GET", "/api/sync/state")
check(code == 401, "no token -> 401 (%s)" % code)
code, res = x("GET", "/api/sync/state")
check(code == 404, "sync is not reachable through the tunnel (%s)" % code)

# ---------------------------------------------------------------- first push
res = sync.push(lconn, lcfg)
check(res.get("static") and res.get("price_daily") and res.get("scans") == 2, "first push %s" % res)
w = sqlite3.connect(web_path)
for t in ("items", "recipes", "recipe_reagents", "price_daily", "price_current", "snapshots", "ingests",
          "ah_scan_items", "item_tooltip"):
    a = lconn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
    b = w.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
    check(a == b, "%s rows equal (%d/%d)" % (t, a, b))
for t in ("postings", "my_mail", "my_auctions", "my_professions", "known_recipes", "shopping_list", "watchlist",
          "alerts", "settings"):
    check(w.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] == 0, "%s stays empty on the server" % t)
check(w.execute("SELECT COUNT(*) FROM ah_scans WHERE char<>''").fetchone()[0] == 0, "scan char blanked")
owners = {r[0] for r in w.execute("SELECT owner FROM ah_auctions UNION SELECT owner FROM ah_sales")}
check("Me" not in owners and "Altsy-PvP" not in owners and "Alice" in owners, "own sellers masked %s" % owners)
dump = b"".join(bytes(str(r), "utf-8") for t in [r[0] for r in w.execute(
    "SELECT name FROM sqlite_master WHERE type='table'")] for r in w.execute("SELECT * FROM %s" % t))
for secret in (b"SecretBuyer", b"Me'", b"Altsy", b"secret\\\\path", b"Other"):
    check(secret not in dump, "%r is nowhere in the web DB" % secret)
check(w.execute("SELECT value FROM meta WHERE key='sync_cfg'").fetchone() is not None, "analysis cfg pushed")
check(wcfg.get("deal_threshold") == 0.3, "server cfg updated from push")
check("savedvariables" not in w.execute("SELECT value FROM meta WHERE key='sync_cfg'").fetchone()[0],
      "no paths in pushed cfg")

# ---------------------------------------------------------------- second push is incremental
batches = sync.build_batches(lconn, lcfg, sync._request(lcfg, "GET", "/api/sync/state"))
check(not any("static" in b for b in batches), "static not resent")
check(not any(b.get("scans") for b in batches), "scans not resent")
daily = sum(len(b.get("price_daily") or []) for b in batches)
check(0 < daily < lconn.execute("SELECT COUNT(*) FROM price_daily").fetchone()[0], "only recent days resent (%d)" % daily)
res = sync.push(lconn, lcfg)
check(not res.get("scans") and not res.get("static"), "second push small %s" % res)

# drift is detected: remove old rows on the server -> full price_daily resend
w.execute("DELETE FROM price_daily WHERE day < (SELECT MIN(day) + 2 FROM price_daily)")
w.commit()
res = sync.push(lconn, lcfg)
check(w.execute("SELECT COUNT(*) FROM price_daily").fetchone()[0] ==
      lconn.execute("SELECT COUNT(*) FROM price_daily").fetchone()[0], "drift repaired by count check")

# raw pruned locally -> dropped on the server too
lconn.execute("DELETE FROM ah_auctions WHERE scan_id=(SELECT MIN(id) FROM ah_scans)")
lconn.execute("UPDATE ah_scans SET raw_kept=0 WHERE id=(SELECT MIN(id) FROM ah_scans)")
lconn.commit()
sync.push(lconn, lcfg)
check(w.execute("SELECT COUNT(*) FROM ah_auctions a JOIN ah_scans s ON s.id=a.scan_id WHERE s.raw_kept=0")
      .fetchone()[0] == 0 and w.execute("SELECT SUM(raw_kept) FROM ah_scans").fetchone()[0] == 1, "raw prune synced")

# bad payloads are refused atomically
code, res, _ = call("POST", "/api/sync/push", raw=gzip.compress(b'{"protocol":1,"price_daily":[[1,2]]}'),
                    headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/gzip"})
check(code == 400, "malformed batch -> 400 (%s %s)" % (code, res))
code, res, _ = call("POST", "/api/sync/push", raw=b'{"protocol":1,"static":{"tables":{"items":[]}}}',
                    headers={"Authorization": "Bearer " + TOKEN})
check(code == 400, "partial static -> 400 (%s)" % code)

# ---------------------------------------------------------------- web endpoints
html = urllib.request.urlopen(BASE + "/").read()
check(b'data-mode="web"' in html, "index marked as web version")
for p in ("/api/postings", "/api/market/my"):
    code, res = x("GET", p)
    check(code == 404, "%s blocked in web version (%s)" % (p, code))
    code, res, _ = call("GET", p)
    check(code == 404, "%s blocked directly too (%s)" % (p, code))
for p in ("/api/ingest", "/api/notify/test"):
    code, res = x("POST", p)
    check(code == 404, "POST %s blocked (%s)" % (p, code))
code, res = x("GET", "/api/item/2453")
check(code == 200 and res["r"]["postings"] == [], "item detail has no postings")
check("s" not in res, "plain GET returns no state")

# status: first visit only sets the eval marker
code, res = x("GET", "/api/status")
check(code == 200 and res["r"].get("web") and res["r"].get("last_push_ts"), "status web %s" % res.get("r"))
st = res.get("s")
check(st and st.get("eval"), "first status sets eval marker")

# write -> state comes back, shared DB untouched
code, res = x("PUT", "/api/myprofessions", [{"profession_id": 4, "skill": 300}], st)
check(code == 200 and res["s"]["professions"] == {"4": 300}, "professions state returned %s" % res.get("s"))
st = res["s"]
code, res = x("GET", "/api/mycrafts", None, st)
check(code == 200 and len(res["r"]) > 0, "mycrafts uses browser state (%d)" % len(res["r"] or []))
code, res = x("GET", "/api/mycrafts", None, None)
check(code == 200 and res["r"] == [], "no state -> no crafts")
code, res = x("PUT", "/api/watchlist/2453", {"max_price": 10 ** 9, "note": "cheap"}, st)
st = res["s"]
check(st["watch"]["2453"]["note"] == "cheap", "watch stored in state")
code, res = x("PUT", "/api/settings", {"notify_my_reagents": True}, st)
st = res["s"]
check(st["settings"] == {"notify_my_reagents": True}, "settings in state %s" % st["settings"])
code, res = x("GET", "/api/professions/4?mine=1", None, st)
check(code == 200, "profession mine")
code, res = x("POST", "/api/leveling/4/to-shopping?start=1&target=50", None, st)
check(code in (200, 501), "to-shopping %s" % code)
if code == 200:
    st = res["s"]
for t in ("my_professions", "watchlist", "settings", "shopping_list", "alerts"):
    check(w.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] == 0, "shared %s still empty after writes" % t)
check(w.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'alertstate:%'").fetchone()[0] == 0, "no alertstate shared")

# new prices -> the next status poll evaluates this browser's alerts
lconn.execute("INSERT INTO ingests(ts,file_mtime,realm,items,changed) VALUES(?,?,'PvP',1,1)", (now, now))
lconn.commit()
sync.push(lconn, lcfg)
code, res = x("GET", "/api/status", None, st)
check(code == 200 and res["r"]["unseen_alerts"] >= 1, "alert evaluated on new data (%s)" % res["r"].get("unseen_alerts"))
check(res.get("s") and res["s"]["alerts"] and res["s"]["watch"]["2453"].get("last_alert_ts"), "alerts in state")
st = res["s"]
code, res = x("GET", "/api/status", None, st)
check("s" not in res and res["r"]["unseen_alerts"] >= 1, "same data -> not evaluated again")
code, res = x("POST", "/api/alerts/seen", None, st)
check(res["s"]["alerts"][0]["seen"] == 1, "alerts marked seen in state")
check(w.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0, "shared alerts still empty")

# garbage state is ignored, not an error
code, res = x("GET", "/api/mycrafts", None, {"professions": {"x": "y", "4": 9999}, "known": [1], "watch": {"1": 5},
                                             "alerts": "no"})
check(code == 200 and res["r"] == [], "garbage state ignored (%s)" % code)
try:  # the server answers 413 without reading the body, so the client may see a broken pipe instead
    code = call("POST", "/api/x", raw=b"x" * (2 << 20))[0]
except urllib.error.URLError:
    code = 413
check(code == 413, "oversized call refused (%s)" % code)

srv.shutdown()
lconn.close()
w.close()
print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
