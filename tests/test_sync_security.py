"""Sync hardening: validation of pushed/pulled rows, future timestamps, per-key ownership, purge, static data, realm,
client transport. Run: python tests/test_sync_security.py"""
import gzip
import hashlib
import http.server
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import make_fixture_db  # noqa: E402
from ahtool import analytics, db, keys, server, sync  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print("FAIL:", msg)
    else:
        print("PASS", msg)


tmp = tempfile.mkdtemp()
TOKEN = "o" * 40
NOW = int(time.time())
TODAY = (NOW - sync.DAY_EPOCH_TS) // 86400

web_path = os.path.join(tmp, "web.db")
wcfg = {"web": True, "sync_token": TOKEN}
webdir = tempfile.mkdtemp()
with open(os.path.join(webdir, "index.html"), "w", encoding="utf-8") as f:
    f.write('<!doctype html><html lang="en"><body>X</body></html>')


srv = server.make_server(wcfg, lambda: db.connect(web_path), port=0, web_dir=webdir, web=True)
srv.RequestHandlerClass.log_message = lambda *a: None
BASE = "http://127.0.0.1:%d" % srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

w = db.connect(web_path)
k_a = keys.create(w, "Alice")
k_b = keys.create(w, "Bob")
id_a = keys.key_ids(w, "Alice")[0]
id_b = keys.key_ids(w, "Bob")[0]


def call(method, path, body=None, token=TOKEN, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def push(payload, token=TOKEN):
    return call("POST", "/api/sync/push", dict({"protocol": sync.PROTOCOL}, **payload), token)


def x(m, p):
    req = urllib.request.Request(BASE + "/api/x", data=json.dumps({"m": m, "p": p}).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def count(sql, params=()):
    return w.execute(sql, params).fetchone()[0]


def pc(realm="Home", iid=2589, price=100, day=None, ts=None):
    return [realm, iid, price, 5, TODAY if day is None else day, NOW - 60 if ts is None else ts]


def scan(ts, realm="Home", items=None, auctions=None, sales=None):
    return {"scan": [ts, realm, "Alliance", "addon", 3, 3, 0, 1 if auctions else 0, 0],
            "items": items if items is not None else [[2589, 3, 15, 100, 110, 2]],
            "auctions": auctions or [], "sales": sales or []}


AUC = [[2589, 0, 5, 500, 400, 0, 4, "Seller"]]

# ---------------------------------------------------------------- owner baseline push (legit data accepted)
code, res = push({"price_current": [pc(), pc(iid=2592, price=300)],
                  "price_daily": [["Home", 2589, TODAY - 1, 90, 120, 10], ["Home", 2589, TODAY + 1, 95, 99, 3]],
                  "snapshots": [["Home", 2589, NOW - 60, 100, 5]],
                  "meta": {"last_ingest_ts": str(NOW - 10)}})
check(code == 200, "legit owner push accepted (%s %s)" % (code, res))
check(count("SELECT COUNT(*) FROM price_current WHERE src_key=0") == 2, "owner token rows tagged src_key=0")

# ---------------------------------------------------------------- 1. types / ranges
n_pc = count("SELECT COUNT(*) FROM price_current")
bad = [
    ("string item_id", {"price_current": [pc(iid="2589")]}),
    ("bool price", {"price_current": [pc(price=True)]}),
    ("float price", {"price_current": [pc(price=10.5)]}),
    ("negative price", {"price_current": [pc(price=-5)]}),
    ("zero item_id", {"price_current": [pc(iid=0)]}),
    ("huge item_id", {"price_current": [pc(iid=2 ** 40)]}),
    ("long realm", {"price_current": [pc(realm="R" * 65)]}),
    ("empty realm", {"price_current": [pc(realm="")]}),
    ("price above cap", {"price_daily": [["Home", 2589, TODAY, sync.PRICE_MAX + 1, 1, 1]]}),
    ("low=0", {"price_daily": [["Home", 2589, TODAY, 0, 100, 1]]}),
    ("string day", {"price_daily": [["Home", 2589, str(TODAY), 10, 100, 1]]}),
    ("string snapshot ts", {"snapshots": [["Home", 2589, "x", 1, 1]]}),
    ("XSS item_id in scan items", {"scans": [scan(NOW - 100, items=[["<img src=x onerror=alert(1)>", 1, 1, 1, 1, 1]])]}),
    ("auction count 0", {"scans": [scan(NOW - 100, auctions=[[2589, 0, 0, 500, 400, 0, 4, "S"]])]}),
    ("string auction buyout", {"scans": [scan(NOW - 100, auctions=[[2589, 0, 1, "500", 400, 0, 4, "S"]])]}),
    ("long owner", {"scans": [scan(NOW - 100, auctions=[[2589, 0, 1, 500, 400, 0, 4, "S" * 65]])]}),
    ("string scan ts", {"scans": [scan(str(NOW - 100))]}),
    ("nested value", {"price_current": [["Home", [1], 1, 1, 1, NOW]]}),
    ("wrong column count", {"price_current": [["Home", 1, 1]]}),
    ("sale bad kind type", {"scans": [scan(NOW - 100, sales=[[None, 2589, 0, 1, 10, "", 5]])]}),
    ("valid row + bad row", {"price_current": [pc(iid=7777), pc(iid="x")]}),
]
for what, payload in bad:
    code, res = push(payload, k_a)
    check(code == 400, "%s -> 400 (%s %s)" % (what, code, res))
check(count("SELECT COUNT(*) FROM price_current") == n_pc and count("SELECT COUNT(*) FROM ah_scans") == 0,
      "nothing of a refused batch was stored")
for lit in (b"NaN", b"Infinity", b"-Infinity"):
    raw = b'{"protocol":2,"price_current":[["Home",2589,%s,5,%d,%d]]}' % (lit, TODAY, NOW)
    code, res = call("POST", "/api/sync/push", raw=raw, token=k_a)
    check(code == 400, "%s price -> 400 (%s)" % (lit.decode(), code))
raw = b'{"protocol":2,"meta":{"last_ingest_ts":Infinity}}'
code, res = call("POST", "/api/sync/push", raw=raw, token=k_a)
check(code in (200, 400), "Infinity meta is not a 500 (%s)" % code)
code, res = x("GET", "/api/status")
check(code == 200, "status still answers after the attempts (%s)" % code)
code, res = x("GET", "/api/deals")
check(code == 200, "deals still answer (%s)" % code)

# ---------------------------------------------------------------- 2. future timestamps
future = [
    ("price_current last_day today+5", {"price_current": [pc(day=TODAY + 5)]}),
    ("price_current updated_at +1h", {"price_current": [pc(ts=NOW + 3600)]}),
    ("price_daily day today+3", {"price_daily": [["Home", 2589, TODAY + 3, 1, 2, 3]]}),
    ("snapshot ts +1h", {"snapshots": [["Home", 2589, NOW + 3600, 1, 1]]}),
    ("scan ts +1h", {"scans": [scan(NOW + 3600)]}),
    ("scan ts absurdly old", {"scans": [scan(5)]}),
    ("sale prev_ts in the future", {"scans": [scan(NOW - 100, sales=[[NOW + 9999, 2589, 0, 1, 10, "", "sold"]])]}),
]
for what, payload in future:
    code, res = push(payload, k_a)
    check(code == 400, "%s -> 400 (%s %s)" % (what, code, res))
code, res = push({"meta": {"last_ingest_ts": str(NOW + 10 ** 6), "logger_ingest_ts": NOW + 10 ** 6}}, k_a)
v = int(count("SELECT value FROM meta WHERE key='last_ingest_ts'"))
check(code == 200 and v <= int(time.time()) + sync.FUTURE_TS_SLACK, "future meta clamped (%s)" % v)
check(int(count("SELECT value FROM meta WHERE key='logger_ingest_ts'")) <= int(time.time()) + sync.FUTURE_TS_SLACK,
      "future logger meta clamped")
# a poisoned row left from before validation does not win forever
w.execute("UPDATE price_current SET price=1, last_day=?, updated_at=? WHERE realm='Home' AND item_id=2592",
          (TODAY + 900, NOW + 10 ** 7))
w.commit()
code, res = push({"price_current": [pc(iid=2592, price=310)]})
check(count("SELECT price FROM price_current WHERE realm='Home' AND item_id=2592") == 310,
      "a stored future price_current row is replaced by a valid one")

# ---------------------------------------------------------------- 3. price caps
code, res = push({"price_current": [pc(price=sync.PRICE_MAX)]}, k_a)
check(code == 200, "price at the cap accepted (%s)" % code)
code, res = push({"price_current": [pc(price=sync.PRICE_MAX + 1)]}, k_a)
check(code == 400, "price above the cap refused (%s)" % code)
push({"price_current": [pc(price=100)]})

# ---------------------------------------------------------------- 8. batch row cap
old = sync.SERVER_MAX_ROWS
sync.SERVER_MAX_ROWS = 5
code, res = push({"price_daily": [["Home", 2589, TODAY - k, 10, 20, 1] for k in range(6)]}, k_a)
sync.SERVER_MAX_ROWS = old
check(code == 400 and "rows" in str(res), "batch over the row cap refused (%s %s)" % (code, res))
check(sync.SERVER_MAX_ROWS >= sync.MAX_BATCH_ROWS, "server cap covers the client's batch size")

# ---------------------------------------------------------------- 4. scan ownership + purge
t_a = NOW - 3000
code, res = push({"scans": [scan(t_a, auctions=AUC)]}, k_a)
check(code == 200 and res["applied"].get("scans") == 1, "Alice pushes a scan with raw auctions (%s)" % res)
sid_a = count("SELECT id FROM ah_scans WHERE ts=?", (t_a,))
check(count("SELECT src_key FROM ah_scans WHERE id=?", (sid_a,)) == id_a, "scan tagged with Alice's key id")
code, res = push({"scan_updates": [{"key": ["Home", "Alliance", t_a], "drop_raw": True,
                                    "sales": [[None, 2589, 0, 5, 1, "Fake", "sold"]]}]}, k_b)
check(code == 200 and res["applied"].get("scan_updates_refused") == 1, "Bob's update of Alice's scan refused %s" % res)
check(count("SELECT COUNT(*) FROM ah_auctions WHERE scan_id=?", (sid_a,)) == 1
      and count("SELECT COUNT(*) FROM ah_sales WHERE scan_id=?", (sid_a,)) == 0,
      "Alice's raw auctions and sales untouched by Bob")
code, res = push({"scan_updates": [{"key": ["Home", "Alliance", t_a], "sales": [[None, 2589, 0, 5, 10, "", "sold"]]}]},
                 k_a)
check(code == 200 and count("SELECT COUNT(*) FROM ah_sales WHERE scan_id=?", (sid_a,)) == 1,
      "Alice may update her own scan")
code, res = push({"scan_updates": [{"key": ["Home", "Alliance", t_a], "drop_raw": True}]})
check(code == 200 and count("SELECT COUNT(*) FROM ah_auctions WHERE scan_id=?", (sid_a,)) == 0, "owner may update any scan")
code, res = push({"scan_updates": [{"key": ["Home", "Alliance", t_a], "sales": [[None, "x", 0, 5, 10, "", "sold"]]}]},
                 k_a)
check(code == 400, "invalid sales in a scan update refused (%s)" % code)

# Bob pushes poison; purge removes exactly his rows
t_b = NOW - 2000
code, res = push({"price_daily": [["Home", 4242, TODAY - 2, 1, 1, 1]], "price_current": [pc(iid=4242, price=1)],
                  "snapshots": [["Home", 4242, NOW - 50, 1, 1]], "scans": [scan(t_b, auctions=AUC,
                                                                                 sales=[[t_a, 2589, 0, 1, 1, "", "sold"]])]},
                 k_b)
check(code == 200, "Bob's push accepted (%s %s)" % (code, res))
sid_b = count("SELECT id FROM ah_scans WHERE ts=?", (t_b,))
check(count("SELECT src_key FROM price_daily WHERE item_id=4242") == id_b, "price_daily tagged with Bob's key id")
n_alice = count("SELECT COUNT(*) FROM ah_scans WHERE src_key=?", (id_a,))
counts, ids = keys.purge(w, "Bob")
check(ids == [id_b] and counts.get("ah_scans") == 1 and counts.get("price_daily") == 1
      and counts.get("price_current") == 1 and counts.get("snapshots") == 1, "purge counts %s" % counts)
check(count("SELECT COUNT(*) FROM price_current WHERE item_id=4242") == 0
      and count("SELECT COUNT(*) FROM ah_auctions WHERE scan_id=?", (sid_b,)) == 0
      and count("SELECT COUNT(*) FROM ah_scan_items WHERE scan_id=?", (sid_b,)) == 0
      and count("SELECT COUNT(*) FROM ah_sales WHERE scan_id=?", (sid_b,)) == 0, "Bob's rows and scan children gone")
check(count("SELECT COUNT(*) FROM ah_scans WHERE src_key=?", (id_a,)) == n_alice
      and count("SELECT COUNT(*) FROM price_current WHERE item_id=2589") == 1, "other keys' rows kept")
os.environ["AHTOOL_DB"] = web_path
out = io.StringIO()
_stdout, sys.stdout = sys.stdout, out
try:
    rc = keys.main(["purge", "Alice"])
    rc2 = keys.main(["purge", "Nobody"])
finally:
    sys.stdout = _stdout
check(rc == 0 and "Purged" in out.getvalue() and "still active" in out.getvalue() and rc2 == 1,
      "ahkey purge CLI (%r)" % out.getvalue())
check(count("SELECT COUNT(*) FROM ah_scans WHERE src_key=?", (id_a,)) == 0, "CLI purge removed Alice's scans")

# legacy DB without src_key gets the column (backward compatible migration)
legacy = os.path.join(tmp, "legacy.db")
lc = db.connect(legacy)
lc.execute("INSERT INTO price_current VALUES('Home', 1, 1, 1, ?, ?)", (TODAY, NOW))
lc.commit()
sync.ensure_schema(lc)
cols = [r[1] for r in lc.execute("PRAGMA table_info(price_current)")]
check("src_key" in cols and "seq" in cols and lc.execute("SELECT src_key FROM price_current").fetchone()[0] is None,
      "migration adds nullable src_key to an existing DB")
lc.close()

# ---------------------------------------------------------------- 6. static data owner-only, hash, icons
st_tables = {t: [] for t in sync.STATIC_TABLES}
st_tables["items"] = [[2589, "Linen Cloth", 1, 5, 7, 5, 132889, 13, 0, 20]]
st_tables["item_tooltip"] = [[2589, "inv_fabric_linen_01", "{}"], [2592, "x\" onerror=\"alert(1)", "{}"],
                             [2593, "../../evil", "{}"]]
good_hash = hashlib.sha256(b"x").hexdigest()
code, st_c = call("GET", "/api/sync/state", token=k_a)
check(code == 200 and st_c["accept_static"] is False, "contributor not offered static even while the site has none")
code, res = push({"static": {"hash": good_hash, "tables": st_tables}}, k_a)
check(code == 200 and res["applied"].get("static_ignored") and count("SELECT COUNT(*) FROM items") == 0,
      "contributor static data ignored on an empty site (%s)" % res)
for h in ("", None, "abc", "Z" * 64):
    code, res = push({"static": {"hash": h, "tables": st_tables}})
    check(code == 400, "owner static with hash %r refused (%s)" % (h, code))
bad_tables = dict(st_tables, items=[["2589", "x", 1, 5, 7, 5, 1, 13, 0, 20]])
code, res = push({"static": {"hash": good_hash, "tables": bad_tables}})
check(code == 400, "owner static with a string id refused (%s)" % code)
code, res = push({"static": {"hash": good_hash, "tables": st_tables}})
check(code == 200 and res["applied"].get("static") == 4, "owner static accepted (%s)" % res)
icons = dict(w.execute("SELECT id, icon FROM item_tooltip"))
check(icons == {2589: "inv_fabric_linen_01", 2592: None, 2593: None}, "invalid tooltip icons blanked %s" % icons)

# ---------------------------------------------------------------- 5. default realm not hijackable in web mode
check(analytics.get_realm(w) == "Home", "web default realm is the owner's (%s)" % analytics.get_realm(w))
code, res = push({"price_current": [pc(realm="Evil", iid=i) for i in range(1, 60)]}, k_a)
check(code == 200 and count("SELECT realm FROM ingests ORDER BY id DESC LIMIT 1") == "Evil",
      "contributor push made the newest ingest row 'Evil'")
check(analytics.get_realm(w) == "Home", "web default realm stays the owner's (%s)" % analytics.get_realm(w))
code, res = x("GET", "/api/status")
check(code == 200 and "Evil" not in json.dumps(res.get("r", {}).get("realm")), "status not on Evil (%s)" % (
    res.get("r", {}).get("realm")))
w.execute("DELETE FROM meta WHERE key='sync_owner_realm'")
w.commit()
check(analytics.get_realm(w) == "Evil", "without an owner push: the realm with most data (%s)" % analytics.get_realm(w))
push({"price_current": [pc(price=101)]})
check(analytics.get_realm(w) == "Home", "owner push sets the default realm again")

# local behaviour unchanged: newest ingest row
local_path = os.path.join(tmp, "local.db")
make_fixture_db.build(local_path)
lconn = db.connect(local_path)
lconn.execute("INSERT INTO ingests(ts,file_mtime,realm,items,changed) VALUES(?,1,'Mine',1,1)", (NOW,))
lconn.commit()
check(analytics.get_realm(lconn) == "Mine", "local default realm = newest ingest row, as before")

# ---------------------------------------------------------------- client: apply_pull validates, keeps the realm
def page(**kw):
    return dict({"protocol": sync.PROTOCOL, "price_current": [], "price_daily": [], "snapshots": [], "scans": []}, **kw)


n_before = lconn.execute("SELECT COUNT(*) FROM price_current").fetchone()[0]
for what, pg in (("string item_id", page(price_current=[pc(iid="<b>")])),
                 ("future day", page(price_daily=[["Home", 2589, TODAY + 9, 1, 1, 1]])),
                 ("future scan", page(scans=[scan(NOW + 10 ** 6)])),
                 ("XSS scan item", page(scans=[scan(NOW - 10, items=[["<svg>", 1, 1, 1, 1, 1]])])),
                 ("NaN-ish float", page(price_current=[pc(price=float("nan"))])),
                 ("valid + bad", page(price_current=[pc(iid=5555), pc(iid=5556, price="1")]))):
    try:
        sync.apply_pull(lconn, pg)
        check(False, "apply_pull refuses %s" % what)
    except sync.BadPayload:
        check(True, "apply_pull refuses %s" % what)
check(lconn.execute("SELECT COUNT(*) FROM price_current").fetchone()[0] == n_before
      and lconn.execute("SELECT COUNT(*) FROM ah_scans").fetchone()[0] == 0, "nothing of a refused page stored")
res = sync.apply_pull(lconn, page(price_current=[pc(realm="Evil", iid=i) for i in range(1, 40)],
                                  scans=[scan(NOW - 500, auctions=AUC)]))
check(res["prices"] == 39 and res["scans"] == 1, "valid page applied %s" % res)
check(analytics.get_realm(lconn) == "Mine", "pulled data does not switch the local default realm (%s)"
      % analytics.get_realm(lconn))

# client leaves out local rows the server would refuse
lconn.execute("INSERT INTO price_current VALUES('Mine', 9991, 'oops', 1, ?, ?)", (TODAY, NOW))
lconn.execute("INSERT INTO price_current VALUES('Mine', 9992, 10, 1, ?, ?)", (TODAY + 30, NOW))
lconn.execute("INSERT INTO price_daily VALUES('Mine', 9993, ?, 10, 10, 1)", (TODAY + 30,))
lconn.commit()
batches, _ = sync.build_batches(lconn, {}, {"epoch": "e"})
sent = {r[1] for b in batches for t in ("price_current", "price_daily") for r in b.get(t) or []}
check(not sent & {9991, 9992, 9993} and 2589 in sent, "invalid local rows are not pushed")
lconn.close()

# ---------------------------------------------------------------- 7. client transport
for url, ok in (("http://example.com", False), ("ftp://x", False), ("https://ah.example.com", True),
                ("http://localhost:8642", True), ("http://127.0.0.1:1", True), ("", False)):
    try:
        sync._check_url(url)
        check(ok, "sync_url %r accepted" % url)
    except sync.SyncError:
        check(not ok, "sync_url %r refused" % url)
try:
    sync.push(sqlite3.connect(":memory:"), {"sync_url": "http://evil.example", "sync_token": "aht_x"})
    check(False, "push over plain http refused")
except sync.SyncError as e:
    check("https" in str(e), "push over plain http refused: %s" % e)

seen = []


class Target(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        seen.append(self.headers.get("Authorization"))
        body = b'{"protocol":2}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


tgt = http.server.HTTPServer(("127.0.0.1", 0), Target)
threading.Thread(target=tgt.serve_forever, daemon=True).start()


class Redir(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", "http://127.0.0.1:%d/steal" % tgt.server_address[1])
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


rd = http.server.HTTPServer(("127.0.0.1", 0), Redir)
threading.Thread(target=rd.serve_forever, daemon=True).start()
try:
    sync._request({"sync_url": "http://127.0.0.1:%d" % rd.server_address[1], "sync_token": "aht_secret"}, "GET",
                  "/api/sync/state")
    check(False, "redirect not followed")
except sync.SyncError as e:
    check("redirect" in str(e), "redirect refused: %s" % e)
check(seen == [], "the sync key never reached the redirect target %s" % seen)
try:
    sync._read_capped(io.BytesIO(b"x" * (3 << 20)), limit=2 << 20)
    check(False, "oversized answer refused")
except sync.SyncError:
    check(True, "oversized answer refused")
check(sync._read_capped(io.BytesIO(b"abc"), limit=10) == b"abc", "small answer read")
check(sync.MAX_RESPONSE <= 256 << 20, "response cap is at most 256 MB")
try:
    sync.loads_strict("[NaN]")
    check(False, "loads_strict refuses NaN")
except ValueError:
    check(True, "loads_strict refuses NaN")

# ================================================================ round 2
from ahtool import market, report  # noqa: E402


def fresh_web(name):
    c = db.connect(os.path.join(tmp, name))
    sync.ensure_schema(c)
    keys.create(c, "Own", owner=True)
    keys.create(c, "Mal")
    keys.create(c, "Cat")
    ids = {n: keys.key_ids(c, n)[0] for n in ("Own", "Mal", "Cat")}
    return c, ids


def src(ids, n):
    return {"name": n, "owner": n == "Own", "key_id": ids[n]}


def ap(c, ids, who, **kw):
    return sync.apply(c, {}, dict({"protocol": sync.PROTOCOL}, **kw), src(ids, who))


def refused(fn):
    try:
        fn()
        return False
    except sync.BadPayload:
        return True


def q(c, sql, a=()):
    return [tuple(r) for r in c.execute(sql, a)]


# ---------------------------------------------------------------- R2.1 overflow-safe aggregates, tighter SPEC
c, ids = fresh_web("r21.db")
check(refused(lambda: ap(c, ids, "Mal", scans=[scan(NOW - 60, sales=[[None, 2589, 0, 10 ** 6, 10, "", "sold"]])])),
      "sale count 1e6 refused (STACK_MAX %d)" % sync.STACK_MAX)
check(refused(lambda: ap(c, ids, "Mal", scans=[scan(NOW - 60, sales=[[None, 2589, 0, 5, 10 ** 11, "", "sold"]])])),
      "unit price 1e11 refused (cap 2^31-1)")
check(refused(lambda: ap(c, ids, "Mal", price_current=[pc(price=2 ** 31)])), "price_current 2^31 refused")
ap(c, ids, "Mal", scans=[scan(NOW - 60, sales=[[None, 2589, 0, sync.STACK_MAX, sync.PRICE_MAX, "", "sold"]] * 10)])
# rows from before validation (or a local DB) that would overflow SUM: aggregates still answer
sid = c.execute("SELECT id FROM ah_scans").fetchone()[0]
c.executemany("INSERT INTO ah_sales(scan_id,prev_scan_id,ts,item_id,suffix,count,unit_price,owner,kind) "
              "VALUES(?,NULL,?,2589,0,?,?,'','sold')", [(sid, NOW - 60, 10 ** 6, 10 ** 11)] * 200)
c.commit()
for fn, args in ((market.activity, (c, {}, 7, 100, None)), (report.daily, (c, {}, None, None))):
    try:
        res = fn(*args)
        check(True, "%s answers despite huge sales" % fn.__name__)
    except Exception as e:  # noqa
        check(False, "%s answers despite huge sales (%s)" % (fn.__name__, e))
act = market.activity(c, {}, 7, 100, None)
check(act["totals"]["value"] > 2 ** 63 and isinstance(act["totals"]["value"], int), "activity total is an int")
c.close()

# ---------------------------------------------------------------- R2.2 unknown keys
c, ids = fresh_web("r22.db")
for what, kw in (("top level", {"junk": [1] * 100}), ("scan object", {"scans": [dict(scan(NOW - 60), extra=1)]}),
                 ("scan update", {"scan_updates": [{"key": ["Home", "Alliance", NOW - 60], "x": 1}]}),
                 ("meta", {"meta": {"last_ingest_ts": NOW, "blob": "x" * 1000}})):
    check(refused(lambda: ap(c, ids, "Mal", **kw)), "unknown key in %s refused" % what)
code, res = push({"padding": "x" * 10000}, k_a if False else TOKEN)
check(code == 400, "unknown top-level key over HTTP -> 400 (%s)" % code)
check(not refused(lambda: ap(c, ids, "Own", meta={"last_ingest_ts": str(NOW), "tooltip_version": "1"}, cfg={})),
      "known keys still accepted")
c.close()

# ---------------------------------------------------------------- R2.3 scan keys per pusher, no future scans, cap
c, ids = fresh_web("r23.db")
T = NOW - 1800
ap(c, ids, "Mal", scans=[scan(T, items=[[2589, 1, 1, 1, 1, 1]])])
res = ap(c, ids, "Cat", scans=[scan(T, items=[[2589, 9, 90, 100, 110, 4]])])
check(res["applied"].get("scans") == 1 and q(c, "SELECT COUNT(*) FROM ah_scans WHERE ts=?", (T,)) == [(2,)],
      "a scan at the same realm/faction/ts from another key is stored too")
st_cat = sync.remote_state(c, src(ids, "Cat"))
st_own = sync.remote_state(c, src(ids, "Own"))
check([s[2] for s in st_cat["scans"]] == [T] and st_own["scans"] == [], "state lists only the caller's own scans")
res = ap(c, ids, "Cat", scans=[scan(T)])
check(not res["applied"].get("scans"), "re-pushing your own scan is a no-op")
check(refused(lambda: ap(c, ids, "Mal", scans=[scan(NOW + 120)])), "scan 2 min in the future refused")
check(not refused(lambda: ap(c, ids, "Mal", scans=[scan(NOW + 30)])), "scan within 60 s clock skew accepted")
res = ap(c, ids, "Mal", scans=[scan(NOW - 5000 - i) for i in range(sync.MAX_SCANS_PER_BATCH + 10)])
check(res["applied"].get("scans") == sync.MAX_SCANS_PER_BATCH and res["applied"].get("scans_deferred") == 10,
      "at most %d new scans per batch (%s)" % (sync.MAX_SCANS_PER_BATCH, res["applied"]))
cat_sid = c.execute("SELECT id FROM ah_scans WHERE ts=? AND src_key=?", (T, ids["Cat"])).fetchone()[0]
res = ap(c, ids, "Cat", scan_updates=[{"key": ["Home", "Alliance", T], "sales": [[None, 2589, 0, 1, 5, "", "sold"]]}])
check(q(c, "SELECT COUNT(*) FROM ah_sales WHERE scan_id=?", (cat_sid,)) == [(1,)] and
      not res["applied"].get("scan_updates_refused"), "scan update reaches the pusher's own scan, not the other one")
# the client splits scans into batches of at most MAX_SCANS_PER_BATCH
lb = db.connect(os.path.join(tmp, "r23_local.db"))
lb.executemany("INSERT INTO ah_scans(ts,realm,faction,char,source,total,n,partial,raw_kept,diffed) "
               "VALUES(?,'Home','Alliance','Me','addon',1,1,0,0,1)", [(NOW - 9000 + i,) for i in range(120)])
lb.commit()
bs, _ = sync.build_batches(lb, {}, {"epoch": "e"})
check(max(len(b.get("scans") or []) for b in bs) <= sync.MAX_SCANS_PER_BATCH, "client batches scans by 50")
lb.close()
c.close()

# local: a pulled scan never blocks your own logger scan with the same key
lc = db.connect(os.path.join(tmp, "r23_pc.db"))
own_ts = NOW - 30
sync.apply_pull(lc, page(scans=[{"scan": [own_ts, "Home", "Alliance", "addon", 1, 1, 0, 1, 1],
                                 "items": [[2589, 1, 1, 1, 1, 1]], "auctions": [[2589, 0, 1, 1, 1, 0, 4, "x"]],
                                 "sales": []}]))
lua = os.path.join(tmp, "r23_AHToolLogger.lua")
with open(lua, "w") as f:
    f.write('AHToolLoggerDB = { ["scans"] = { { ["t"] = %d, ["realm"] = "Home", ["faction"] = "Alliance", '
            '["char"] = "Me", ["src"] = "full", ["total"] = 2, ["n"] = 2, ["d"] = "2589,0,20,2000,1500,0,4,Bob;'
            '2589,0,10,900,800,0,4,Ann" } } }\n' % own_ts)
r = market.ingest(lc, {"logger_savedvariables": lua}, force=True)
check(r.get("scans") == 1 and q(lc, "SELECT source, n FROM ah_scans") == [("full", 2)]
      and q(lc, "SELECT COUNT(*) FROM ah_auctions") == [(2,)], "own logger scan replaces a pulled one with its key")

# ---------------------------------------------------------------- R2.4 purge recomputes the merge
c, ids = fresh_web("r24.db")
D = TODAY - 1
ap(c, ids, "Mal", price_daily=[["Home", 4306, D, 1, 600, 10]])
ap(c, ids, "Cat", price_daily=[["Home", 4306, D, 480, 650, 12]])
check(q(c, "SELECT low, high FROM price_daily WHERE item_id=4306") == [(1, 650)], "merged daily: min low / max high")
out = sync.purge_key(c, [ids["Mal"]])
check(q(c, "SELECT low, high, available FROM price_daily WHERE item_id=4306") == [(480, 650, 12)],
      "purge removes the poisoned low even after another key changed the row (%s)" % out)
ap(c, ids, "Own", price_daily=[["Home", 2453, D - k, 500, 900, 10] for k in range(30)])
ap(c, ids, "Mal", price_daily=[["Home", 2453, D - k, 499, 901, None] for k in range(30)])
sync.purge_key(c, [ids["Mal"]])
check(q(c, "SELECT COUNT(*), MIN(low), MAX(high) FROM price_daily WHERE item_id=2453") == [(30, 500, 900)],
      "purge keeps the owner's history the purged key also touched")
ap(c, ids, "Own", price_current=[pc(iid=118, price=1500, ts=NOW - 600)])
ap(c, ids, "Mal", price_current=[pc(iid=118, price=1, ts=NOW - 60)])
check(q(c, "SELECT price FROM price_current WHERE item_id=118") == [(1,)], "newer contributor price shown")
sync.purge_key(c, [ids["Mal"]])
check(q(c, "SELECT price, src_key FROM price_current WHERE item_id=118") == [(1500, 0 if False else ids["Own"])],
      "purge brings back the owner's price_current row")
ap(c, ids, "Mal", price_current=[pc(iid=9, price=5)], price_daily=[["Home", 9, D, 5, 5, 1]])
sync.purge_key(c, [ids["Mal"]])
check(q(c, "SELECT COUNT(*) FROM price_current WHERE item_id=9") == [(0,)] and
      q(c, "SELECT COUNT(*) FROM price_daily WHERE item_id=9") == [(0,)], "rows only the purged key had are gone")
seq_before = int(c.execute("SELECT value FROM meta WHERE key='sync_seq'").fetchone()[0])
check(q(c, "SELECT MAX(seq) FROM price_current")[0][0] <= seq_before, "recomputed rows carry a seq (pull sees them)")

# echo: a PC that pulled the poison never pushes it back as its own, and takes the purge on its next pull
c2, ids2 = fresh_web("r24b.db")
ap(c2, ids2, "Own", price_daily=[["Home", 77, D, 100, 120, 5]], price_current=[pc(iid=77, price=100, ts=NOW - 900)])
ap(c2, ids2, "Mal", price_daily=[["Home", 77, D, 1, 120, 5]], price_current=[pc(iid=77, price=1, ts=NOW - 60)])
pcx = db.connect(os.path.join(tmp, "r24_pc.db"))
pcx.execute("INSERT INTO price_daily VALUES('Home', 77, ?, 100, 120, 5)", (D,))
pcx.execute("INSERT INTO price_current VALUES('Home', 77, 100, 5, ?, ?)", (TODAY, NOW - 900))
pcx.commit()
sync.apply_pull(pcx, sync.pull_page(c2, {}))
check(q(pcx, "SELECT low FROM price_daily WHERE item_id=77") == [(1,)] and
      q(pcx, "SELECT price FROM price_current WHERE item_id=77") == [(1,)], "PC pulled the (poisoned) merge")
bs, _ = sync.build_batches(pcx, {}, {"epoch": "e"})
sent_pc = [r for b in bs for r in b.get("price_current") or [] if r[1] == 77]
sent_pd = [r for b in bs for r in b.get("price_daily") or [] if r[1] == 77]
check(sent_pc == [] and all(r[3] is None for r in sent_pd), "pulled values are not pushed back as the PC's own %s"
      % sent_pd)
sync.purge_key(c2, [ids2["Mal"]])
cur0 = {"pd": [0, 0], "pc": [0, 0]}
sync.apply_pull(pcx, sync.pull_page(c2, cur0))
check(q(pcx, "SELECT price FROM price_current WHERE item_id=77") == [(100,)],
      "after the purge the PC's pulled price_current row takes the recomputed value")
check(q(pcx, "SELECT low FROM price_daily WHERE item_id=77") == [(100,)],
      "the pulled daily row (own values were all within it) takes the recomputed value too")
pcx.close()
c2.close()

# ---------------------------------------------------------------- R2.5 price_current dated tomorrow
ap(c, ids, "Mal", price_current=[pc(iid=5, price=1, day=TODAY + 1, ts=NOW + 200)])
check(q(c, "SELECT updated_at <= ? FROM price_current_src WHERE item_id=5", (int(time.time()),)) == [(1,)],
      "pushed updated_at clamped to the receive time")
ap(c, ids, "Own", price_current=[pc(iid=5, price=100, day=TODAY, ts=NOW - 30)])
check(q(c, "SELECT price, last_day FROM price_current WHERE item_id=5") == [(100, TODAY)],
      "a tomorrow-dated row never beats today's real observation")
ap(c, ids, "Cat", price_current=[pc(iid=6, price=7, day=TODAY + 1, ts=NOW - 10)])
check(q(c, "SELECT price FROM price_current WHERE item_id=6") == [(7,)], "a tomorrow-dated row shows while alone")
lp2 = db.connect(os.path.join(tmp, "r25_pc.db"))
lp2.execute("INSERT INTO price_current VALUES('Home', 5, 100, 5, ?, ?)", (TODAY, NOW - 30))
lp2.commit()
sync.apply_pull(lp2, page(price_current=[["Home", 5, 1, 999, TODAY + 1, NOW]]))
check(q(lp2, "SELECT price FROM price_current WHERE item_id=5") == [(100,)],
      "apply_pull: a pulled tomorrow-dated row does not replace your own observation")
sync.apply_pull(lp2, page(price_current=[["Home", 5, 90, 999, TODAY, NOW]]))
check(q(lp2, "SELECT price FROM price_current WHERE item_id=5") == [(90,)], "a newer pulled row still does")
check(sync.current_newer((1, 1, TODAY, NOW), (2, 2, TODAY + 1, NOW - 10)) and
      not sync.current_newer((1, 1, TODAY + 1, NOW), (2, 2, TODAY, NOW - 10)), "current_newer rule")
lp2.close()
c.close()

# ---------------------------------------------------------------- R2.6 latest_scan prefers the owner / your own
c, ids = fresh_web("r26.db")
ap(c, ids, "Own", scans=[dict(scan(NOW - 3600, auctions=AUC), scan=[NOW - 3600, "Home", "Horde", "a", 1, 1, 0, 1, 0])])
ap(c, ids, "Mal", scans=[scan(NOW - 60, auctions=[[2589, 0, 1, 1, 1, 0, 4, "x"]])])
ls = market.latest_scan(c, "Home")
check(ls and ls["src_key"] == ids["Own"], "web latest_scan prefers the owner's recent scan (%s)" % (ls or {}).get("src_key"))
check(market.latest_scan(c, "Home", faction="Alliance")["src_key"] == ids["Mal"], "faction filter")
check(report.daily(c, {}).get("faction") == "Horde", "report faction from the owner's scans")
c.execute("UPDATE ah_scans SET ts=? WHERE src_key=?", (NOW - market.PREFER_OWN_S - 600, ids["Own"]))
c.commit()
check(market.latest_scan(c, "Home")["src_key"] == ids["Mal"], "falls back to a contributor scan when the owner's is old")
c.close()
lc.execute("INSERT INTO ah_scans(ts,realm,faction,char,source,total,n,partial,raw_kept,diffed) "
           "VALUES(?,'Home','Alliance','','web',1,1,0,1,1)", (NOW - 5,))
lc.commit()
check(market.latest_scan(lc, "Home")["source"] == "full", "local latest_scan prefers your own recent scan")
lc.close()

# ---------------------------------------------------------------- R2.7 raw auctions pruned per key
c, ids = fresh_web("r27.db")
ap(c, ids, "Own", scans=[scan(NOW - 7200, auctions=AUC)])
ap(c, ids, "Mal", scans=[scan(NOW - 100 + i, auctions=[[2589, 0, 1, 1, 1, 0, 4, "x"]]) for i in range(10)])
check(q(c, "SELECT raw_kept, (SELECT COUNT(*) FROM ah_auctions WHERE scan_id=ah_scans.id) FROM ah_scans "
           "WHERE src_key=?", (ids["Own"],)) == [(1, 1)], "a contributor's scans never evict the owner's raw auctions")
check(q(c, "SELECT COUNT(*) FROM ah_scans WHERE src_key=? AND raw_kept=1", (ids["Mal"],)) == [(6,)],
      "the contributor keeps only the newest 6 raw")
c.close()

# ---------------------------------------------------------------- R2.8 legacy rows: pull skips them, sweep, migration
c, ids = fresh_web("r28.db")
ap(c, ids, "Own", price_current=[pc(iid=11)])
c.execute("INSERT INTO price_current(realm,item_id,price,available,last_day,updated_at,seq) "
          "VALUES('Home','2589x',10,1,?,?,99)", (TODAY, NOW))
c.execute("INSERT INTO price_current(realm,item_id,price,available,last_day,updated_at,seq) "
          "VALUES('Home',12,10,1,?,?,100)", (TODAY, NOW))
c.commit()
pg = sync.pull_page(c, {})
check([r[1] for r in pg["price_current"]] == [11, 12] and pg["cursor"]["pc"][0] == 100,
      "pull_page skips an invalid stored row and moves past it")
lp3 = db.connect(os.path.join(tmp, "r28_pc.db"))
check(sync.apply_pull(lp3, pg)["prices"] == 2, "the client takes the page")
lp3.close()
out = sync.sweep_invalid(c)
check(out.get("price_current") == 1 and q(c, "SELECT COUNT(*) FROM price_current") == [(2,)], "sweep_invalid %s" % out)
c.close()
# an old web DB (schema before 3): UNIQUE scans, no per-key tables, a bad row -> migrated once by ensure_schema
old_path = os.path.join(tmp, "r28_old.db")
oc = db.connect(old_path)
for t in ("price_daily", "price_current", "snapshots", "ah_scans"):
    oc.execute("ALTER TABLE %s ADD COLUMN src_key INTEGER" % t)
for t in ("price_daily", "price_current", "ah_scans"):
    oc.execute("ALTER TABLE %s ADD COLUMN seq INTEGER NOT NULL DEFAULT 0" % t)
oc.execute("INSERT INTO meta VALUES('sync_epoch','old')")
PDC = "INSERT INTO price_daily(realm,item_id,day,low,high,available,seq,src_key) VALUES"
oc.execute(PDC + "('Home', 1, ?, 10, 20, 1, 1, 0)", (D,))
oc.execute(PDC + "('Home', 2, ?, 'x', 20, 1, 1, NULL)", (D,))
oc.execute("INSERT INTO price_current(realm,item_id,price,available,last_day,updated_at,seq,src_key) "
           "VALUES('Home', 1, 10, 1, ?, ?, 1, NULL)", (TODAY, NOW))
oc.execute("INSERT INTO ah_scans(ts,realm,faction,char,source,total,n,partial,raw_kept,diffed,seq,src_key) "
           "VALUES(?,'Home','Alliance','','a',1,1,0,0,1,1,NULL)", (NOW - 100,))
oc.commit()
sync.ensure_schema(oc)
check(q(oc, "SELECT value FROM meta WHERE key='sync_schema'") == [(str(sync.SCHEMA_VERSION),)], "schema upgraded")
check(q(oc, "SELECT COUNT(*) FROM price_daily") == [(1,)] and q(oc, "SELECT src_key FROM price_daily_src") == [(0,)]
      and q(oc, "SELECT src_key FROM price_current_src") == [(sync.LEGACY_SRC,)], "bad row swept, per-key tables filled")
check("UNIQUE" not in oc.execute("SELECT sql FROM sqlite_master WHERE name='ah_scans'").fetchone()[0].upper()
      and q(oc, "SELECT COUNT(*) FROM ah_scans") == [(1,)], "ah_scans rebuilt without the global UNIQUE")
check(q(oc, "SELECT value FROM meta WHERE key='sync_epoch'") == [("old",)], "epoch kept")
oc.close()

# ---------------------------------------------------------------- R2.9 cursor overflow
for cur_ in ('{"pd":[1e999,0]}', '{"pd":[99999999999999999999999,0]}', '{"sn":1e400}', '{"sc":[true,1]}',
             '{"pc":[-5,0]}', '[1]'):
    code, res = call("GET", "/api/sync/pull?" + urllib.parse.urlencode({"cursor": cur_}))
    check(code == 400, "pull cursor %s -> 400 (%s)" % (cur_, code))
code, res = call("GET", "/api/sync/pull?" + urllib.parse.urlencode({"cursor": "{}", "limit": "9" * 5000}))
check(code == 200, "absurd pull limit falls back to the default (%s)" % code)

# ---------------------------------------------------------------- R2.10 ingests / realm traces per key
c, ids = fresh_web("r210.db")
ap(c, ids, "Own", price_current=[pc(iid=1)])
for k in range(3):
    ap(c, ids, "Mal", price_current=[pc(realm="Junk%d" % k, iid=1, price=10 + k)])
check(q(c, "SELECT COUNT(*) FROM ingests WHERE src_key=?", (ids["Mal"],)) == [(1,)],
      "a contributor keeps one ingest row, not one per push")
check(analytics.list_realms(c) == ["Home"], "contributor realms are not listed (%s)" % analytics.list_realms(c))
out = sync.purge_key(c, [ids["Mal"]])
check(q(c, "SELECT COUNT(*) FROM ingests WHERE src_key=?", (ids["Mal"],)) == [(0,)]
      and not any(r[0].startswith("Junk") for r in c.execute("SELECT realm FROM ingests WHERE realm IS NOT NULL")),
      "purge removes the key's ingest rows %s" % out)
ap(c, ids, "Own", price_current=[pc(realm="Second", iid=1)])
check(analytics.list_realms(c) == ["Home", "Second"], "owner realms listed (%s)" % analytics.list_realms(c))
c.close()

# ---------------------------------------------------------------- R2.11 item search with odd digits
search_path = os.path.join(tmp, "r211.db")
make_fixture_db.build(search_path)
sconn = db.connect(search_path)
for qq in ("²", "9" * 5000, "٣", "2589"):
    try:
        found = analytics.search(sconn, {}, qq)
        check(qq != "2589" or any(r.get("id") == 2589 for r in found), "search %r answers" % qq[:8])
    except Exception as e:  # noqa
        check(False, "search %r answers (%s)" % (qq[:8], e))
sconn.close()

# ---------------------------------------------------------------- busy web version: 503 is retried
busy = {"left": 2, "hits": 0, "ra": "1"}


class Busy(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        busy["hits"] += 1
        if busy["left"] != 0:
            busy["left"] -= 1
            body = b'{"error":"busy, retry"}'
            self.send_response(503)
            self.send_header("Retry-After", busy["ra"])
        else:
            body = b'{"ok":true,"applied":{}}'
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


bs_srv = http.server.HTTPServer(("127.0.0.1", 0), Busy)
threading.Thread(target=bs_srv.serve_forever, daemon=True).start()
slept = []
_real_sleep, sync._sleep = sync._sleep, slept.append
bcfg = {"sync_url": "http://127.0.0.1:%d" % bs_srv.server_address[1], "sync_token": "aht_x"}
try:
    res = sync._request(bcfg, "POST", "/api/sync/push", {"protocol": 2})
    check(res.get("ok") and busy["hits"] == 3 and slept == [1.0, 1.0], "503 retried with Retry-After %s" % slept)
    busy.update(left=-1, hits=0, ra="junk")
    slept.clear()
    try:
        sync._request(bcfg, "POST", "/api/sync/push", {"protocol": 2})
        check(False, "a web version that stays busy gives up")
    except sync.SyncError as e:
        check("503" in str(e) and sum(slept) <= sync.BUSY_MAX_WAIT and slept[:3] == [1, 2, 4],
              "stays busy -> SyncError after %s s of backoff %s" % (sum(slept), slept))
finally:
    sync._sleep = _real_sleep
    bs_srv.shutdown()

tgt.shutdown()
rd.shutdown()
srv.shutdown()
w.close()
print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
