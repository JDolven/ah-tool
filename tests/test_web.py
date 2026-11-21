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
from ahtool import analytics, db, keys, market, server, sync  # noqa: E402

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
for t in ("items", "recipes", "recipe_reagents", "price_daily", "price_current", "snapshots",
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
batches, _ = sync.build_batches(lconn, lcfg, sync._request(lcfg, "GET", "/api/sync/state"),
                                mine=sync.local_state(lconn, lcfg))
check(not any("static" in b for b in batches), "static not resent")
check(not any(b.get("scans") for b in batches), "scans not resent")
daily = sum(len(b.get("price_daily") or []) for b in batches)
check(0 < daily < lconn.execute("SELECT COUNT(*) FROM price_daily").fetchone()[0], "only recent days resent (%d)" % daily)
n_ing = w.execute("SELECT COUNT(*) FROM ingests").fetchone()[0]
res = sync.push(lconn, lcfg)
check(not res.get("scans") and not res.get("static") and not res.get("snapshots"), "second push small %s" % res)
check(w.execute("SELECT COUNT(*) FROM ingests").fetchone()[0] == n_ing, "unchanged prices -> no new ingest row")

# a reset web database (new epoch) gets everything again
w.execute("DELETE FROM price_daily")
w.execute("UPDATE meta SET value='other' WHERE key='sync_epoch'")
w.commit()
res = sync.push(lconn, lcfg)
check(w.execute("SELECT COUNT(*) FROM price_daily").fetchone()[0] ==
      lconn.execute("SELECT COUNT(*) FROM price_daily").fetchone()[0], "new epoch -> full resend")

# raw pruned locally -> dropped on the server too
lconn.execute("DELETE FROM ah_auctions WHERE scan_id=(SELECT MIN(id) FROM ah_scans)")
lconn.execute("UPDATE ah_scans SET raw_kept=0 WHERE id=(SELECT MIN(id) FROM ah_scans)")
lconn.commit()
sync.push(lconn, lcfg)
check(w.execute("SELECT COUNT(*) FROM ah_auctions a JOIN ah_scans s ON s.id=a.scan_id WHERE s.raw_kept=0")
      .fetchone()[0] == 0 and w.execute("SELECT SUM(raw_kept) FROM ah_scans").fetchone()[0] == 1, "raw prune synced")

# bad payloads are refused atomically
code, res, _ = call("POST", "/api/sync/push", raw=gzip.compress(b'{"protocol":2,"price_daily":[[1,2]]}'),
                    headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/gzip"})
check(code == 400, "malformed batch -> 400 (%s %s)" % (code, res))
code, res, _ = call("POST", "/api/sync/push", raw=b'{"protocol":2,"static":{"tables":{"items":[]}}}',
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
lconn.execute("UPDATE price_current SET price=price-1, updated_at=updated_at+1 WHERE item_id=2453")
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


# ---------------------------------------------------------------- several pushers with their own keys
kconn = db.connect(web_path)
k_ola = keys.create(kconn, "Ola")
k_own = keys.create(kconn, "Me on laptop", owner=True)
try:
    keys.create(kconn, "Ola")
    check(False, "duplicate active key name refused")
except ValueError:
    check(True, "duplicate active key name refused")
check(kconn.execute("SELECT COUNT(*) FROM sync_keys WHERE hash LIKE 'aht_%'").fetchone()[0] == 0
      and k_ola not in str(kconn.execute("SELECT * FROM sync_keys").fetchall()), "only hashes of keys stored")

# Ola's PC: other character, lower price on an existing day, an older current price, own trades + other items data
ola_path = os.path.join(tmp, "ola.db")
make_fixture_db.build(ola_path)
oconn = db.connect(ola_path)
day, low, high = oconn.execute("SELECT day, low, high FROM price_daily WHERE item_id=2453 ORDER BY day DESC LIMIT 1").fetchone()
oconn.execute("UPDATE price_daily SET low=?, high=? WHERE item_id=2453 AND day=?", (low // 2, high * 2, day))
oconn.execute("UPDATE price_current SET price=1, updated_at=updated_at-100000 WHERE item_id=2453")
oconn.execute("UPDATE items SET name='Ola renamed it' WHERE id=2453")
oconn.execute("INSERT INTO postings VALUES(2453, 1799990000, 12345, 7)")
oconn.commit()
olua = """AHToolLoggerDB = { ["v"] = 1, ["scans"] = { %s },
["mail"] = { ["sold|Y|OlasBuyer|1|1|1"] = { ["t"] = %d, ["type"] = "sold", ["item"] = "Y", ["player"] = "OlasBuyer",
     ["price"] = 1, ["count"] = 1, ["char"] = "Olachar" } } }""" % (
    scan(now - 1800, [(2453, 0, 5, 4800, 4000, 0, 4, "Alice"), (2453, 0, 5, 4700, 4000, 0, 4, "Olachar"),
                      (2453, 0, 5, 4600, 4000, 0, 4, "Me")], "Olachar"), now - 600)
olp = os.path.join(tmp, "Ola.lua")
with open(olp, "w", encoding="utf-8") as f:
    f.write(olua)
ocfg = {"logger_savedvariables": olp, "addons_dir": "", "deal_threshold": 0.9, "sync_url": BASE, "sync_token": k_ola}
market.ingest(oconn, ocfg)
cfg_before = w.execute("SELECT value FROM meta WHERE key='sync_cfg'").fetchone()[0]
code, st_ola, _ = call("GET", "/api/sync/state", headers={"Authorization": "Bearer " + k_ola})
check(code == 200 and st_ola["you"] == "Ola" and not st_ola["accept_static"] and not st_ola["accept_cfg"],
      "contributor state %s" % {k: st_ola.get(k) for k in ("you", "accept_static", "accept_cfg")})
res = sync.push(oconn, ocfg)
check(res.get("scans") == 1 and not res.get("static"), "contributor push %s" % res)
check(w.execute("SELECT value FROM meta WHERE key='sync_cfg'").fetchone()[0] == cfg_before
      and wcfg.get("deal_threshold") == 0.3, "contributor cannot change analysis settings")
check(w.execute("SELECT name FROM items WHERE id=2453").fetchone()[0] != "Ola renamed it",
      "contributor cannot replace item data")
lo, hi = w.execute("SELECT low, high FROM price_daily WHERE item_id=2453 AND day=?", (day,)).fetchone()
check(lo == low // 2 and hi == high * 2, "daily prices merged: lowest low, highest high (%s %s)" % (lo, hi))
check(w.execute("SELECT price FROM price_current WHERE item_id=2453").fetchone()[0] != 1,
      "an older current price does not overwrite a newer one")
check(w.execute("SELECT COUNT(*) FROM ah_scans").fetchone()[0] == 3, "Ola's scan added next to ours")
owners = {r[0] for r in w.execute("SELECT owner FROM ah_auctions")}
check("Olachar" not in owners and "Alice" in owners, "Ola's characters masked by Ola's PC %s" % owners)
check("Me" in owners, "our name in Ola's scan is just another seller to Ola (expected) %s" % owners)
dump = str([r for t in ("price_daily", "postings", "ah_sales", "ah_scans", "meta") for r in w.execute("SELECT * FROM %s" % t)])
check("OlasBuyer" not in dump and "12345" not in dump, "Ola's own trades never arrive")
check(kconn.execute("SELECT pushes, last_push_ts IS NOT NULL FROM sync_keys WHERE name='Ola'").fetchone() == (4, 1)
      or kconn.execute("SELECT pushes FROM sync_keys WHERE name='Ola'").fetchone()[0] > 0, "key usage recorded")

# owner key may send item data and settings
code, st_me, _ = call("GET", "/api/sync/state", headers={"Authorization": "Bearer " + k_own})
check(st_me["accept_static"] and st_me["accept_cfg"], "owner key accepted for static/cfg")

# ---------------------------------------------------------------- two-way: pull what the others pushed
n_ing_local = lconn.execute("SELECT COUNT(*) FROM ingests").fetchone()[0]
own_before = lconn.execute("SELECT COUNT(*) FROM ah_scans WHERE source<>'web'").fetchone()[0]
post_before = lconn.execute("SELECT COUNT(*) FROM postings").fetchone()[0]
cur_before = lconn.execute("SELECT price FROM price_current WHERE item_id=2453").fetchone()[0]
res = sync.pull(lconn, lcfg)
check(res["scans"] == 1 and res["prices"] > 0, "owner pulls Ola's scan and prices %s" % res)
web_scans = lconn.execute("SELECT char, source, diffed FROM ah_scans WHERE source='web'").fetchall()
check(len(web_scans) == 1 and web_scans[0][0] == "" and web_scans[0][2] == 1, "pulled scan stored as web %s" % web_scans)
check(lconn.execute("SELECT COUNT(*) FROM ah_scans WHERE source<>'web'").fetchone()[0] == own_before,
      "own scans not duplicated by the echo")
lo, hi = lconn.execute("SELECT low, high FROM price_daily WHERE item_id=2453 AND day=?", (day,)).fetchone()
check(lo == low // 2 and hi == high * 2, "Ola's prices merged locally (%s %s)" % (lo, hi))
check(lconn.execute("SELECT price FROM price_current WHERE item_id=2453").fetchone()[0] == cur_before,
      "Ola's older current price did not replace ours")
check(lconn.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == post_before, "pull never touches postings")
check(lconn.execute("SELECT COUNT(*) FROM ingests WHERE file_mtime IS NULL").fetchone()[0] == 1
      and lconn.execute("SELECT COUNT(*) FROM ingests").fetchone()[0] == n_ing_local + 1, "one pull ingest row")
res = sync.pull(lconn, lcfg)
check(res["prices"] == 0 and res["scans"] == 0, "second pull changes nothing %s" % res)
check(lconn.execute("SELECT COUNT(*) FROM ingests").fetchone()[0] == n_ing_local + 1, "no ingest row for nothing")

# pulled data is not echoed back, and pushing after a pull changes nothing on the server
batches, _ = sync.build_batches(lconn, lcfg, sync._request(lcfg, "GET", "/api/sync/state"), full=True)
sent_ts = {sc["scan"][0] for b in batches for sc in b.get("scans") or []}
check(now - 1800 not in sent_ts, "pulled scan never pushed back, even with --full")
seq0 = w.execute("SELECT MAX(seq) FROM price_daily").fetchone()[0]
n_srv_ing = w.execute("SELECT COUNT(*) FROM ingests").fetchone()[0]
sync.push(lconn, lcfg)
check(w.execute("SELECT COUNT(*) FROM ingests").fetchone()[0] == n_srv_ing
      and w.execute("SELECT MAX(seq) FROM price_daily").fetchone()[0] == seq0, "echo push changes nothing")

# your next own scan is diffed against your own previous scan, never against a pulled one
lua3 = lua.replace(scan(now - 3600, s2, "Other"), scan(now - 3600, s2, "Other") + ", " +
                   scan(now - 600, [(2453, 0, 5, 5000, 4000, 0, 4, "Alice")] * 2 + [(118, 0, 1, 900, 800, 0, 4, "Bob")], "Me"))
with open(lp, "w", encoding="utf-8") as f:
    f.write(lua3)
market.ingest(lconn, lcfg, force=True)
prev = lconn.execute("SELECT DISTINCT p.source, p.ts FROM ah_sales s JOIN ah_scans c ON c.id=s.scan_id "
                     "JOIN ah_scans p ON p.id=s.prev_scan_id WHERE c.ts=?", (now - 600,)).fetchall()
check(prev and all(r[0] != "web" for r in prev) and prev[0][1] == now - 3600, "own diff skips pulled scans %s" % prev)

# your next Auctionator import never overwrites a newer price pulled from someone else
from ahtool import ingest  # noqa: E402
d118 = oconn.execute("SELECT last_day FROM price_current WHERE item_id=118").fetchone()[0]
oconn.execute("UPDATE price_current SET price=4242, last_day=last_day+1, updated_at=updated_at+1000000 WHERE item_id=118")
oconn.commit()
sync.push(oconn, ocfg)
sync.pull(lconn, lcfg)
check(lconn.execute("SELECT price FROM price_current WHERE item_id=118").fetchone()[0] == 4242, "Ola's newer price pulled")
d2453 = lconn.execute("SELECT last_day FROM price_current WHERE item_id=2453").fetchone()[0]
auc = os.path.join(tmp, "Auctionator.lua")
with open(auc, "w", encoding="utf-8") as f:
    f.write('AUCTIONATOR_PRICE_DATABASE = { ["__dbversion"] = 8, ["PvP"] = {\n'
            ' ["118"] = { ["m"] = 1, ["h"] = { ["%d"] = 1 }, ["a"] = { ["%d"] = 3 } },\n'
            ' ["2453"] = { ["m"] = 777, ["h"] = { ["%d"] = 777, ["%d"] = %d }, ["a"] = { ["%d"] = 9 } },\n'
            '} }\n' % (d118, d118, d2453 + 1, day, low // 2 + 1, d2453 + 1))
lcfg["savedvariables"] = auc
ingest.ingest(lconn, lcfg, force=True)
check(lconn.execute("SELECT price FROM price_current WHERE item_id=118").fetchone()[0] == 4242,
      "older own Auctionator price does not replace the newer pulled one")
check(lconn.execute("SELECT price FROM price_current WHERE item_id=2453").fetchone()[0] == 777,
      "a newer own Auctionator price is imported as before")
check(lconn.execute("SELECT high FROM price_daily WHERE item_id=2453 AND day=?", (day,)).fetchone()[0] == high * 2,
      "own import merges daily prices instead of lowering the pulled high")
lconn.execute("INSERT INTO ingests(ts,file_mtime,realm,items,changed) VALUES(?,NULL,'PvP',1,1)", (now,))
lconn.commit()  # what a pull leaves behind
check(ingest.ingest(lconn, lcfg).get("skipped") is True, "unchanged Auctionator file still skipped after a pull")

# Ola pulls our data too; her own scan stays hers
res = sync.pull(oconn, ocfg)
check(res["scans"] >= 2 and oconn.execute("SELECT COUNT(*) FROM ah_scans WHERE source<>'web'").fetchone()[0] == 1,
      "Ola gets our scans, keeps her own %s" % res)
dump = str(oconn.execute("SELECT * FROM ah_auctions").fetchall())
check("Altsy" not in dump, "our alt (sync_hide_sellers) never reaches Ola")

# a new PC pulls everything, page by page
new_path = os.path.join(tmp, "new.db")
nconn = db.connect(new_path)
k_new = keys.create(kconn, "Kari")
sync.PULL_LIMIT = 100
res = sync.pull(nconn, {"sync_url": BASE, "sync_token": k_new})
sync.PULL_LIMIT = 50000
check(res["pages"] > 1, "paged pull (%d pages)" % res["pages"])
for t in ("price_daily", "price_current", "snapshots"):
    a = nconn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
    b = w.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
    check(a == b, "new PC has all %s (%d/%d)" % (t, a, b))
check(nconn.execute("SELECT COUNT(*) FROM ah_scans").fetchone()[0] == w.execute("SELECT COUNT(*) FROM ah_scans")
      .fetchone()[0], "new PC has all scans")
# web database reset -> the next pull starts over
w.execute("UPDATE meta SET value='reset' WHERE key='sync_epoch'")
w.commit()
nconn.execute("DELETE FROM price_daily")
nconn.commit()
sync.pull(nconn, {"sync_url": BASE, "sync_token": k_new})
check(nconn.execute("SELECT COUNT(*) FROM price_daily").fetchone()[0] == w.execute(
    "SELECT COUNT(*) FROM price_daily").fetchone()[0], "new epoch -> pulled again from the start")
nconn.close()

# revoke -> refused on the next request
check(keys.revoke(kconn, "Ola") == 1, "revoke by name")
code, res, _ = call("GET", "/api/sync/state", headers={"Authorization": "Bearer " + k_ola})
check(code == 401, "revoked key -> 401 (%s)" % code)
try:
    sync.push(oconn, ocfg)
    check(False, "push with revoked key fails")
except sync.SyncError as e:
    check("401" in str(e), "push with revoked key fails: %s" % e)
check(keys.revoke(kconn, "nobody") == 0, "revoking an unknown name does nothing")
code, res, _ = call("GET", "/api/sync/state", headers={"Authorization": "Bearer aht_" + "x" * 43})
check(code == 401, "made-up key -> 401")
oconn.close()
kconn.close()

srv.shutdown()
lconn.close()
w.close()
print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
