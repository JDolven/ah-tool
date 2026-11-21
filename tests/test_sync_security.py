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
lc.execute("INSERT INTO price_current VALUES('Home', 1, 1, 1, 1, 1)")
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

tgt.shutdown()
rd.shutdown()
srv.shutdown()
w.close()
print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
