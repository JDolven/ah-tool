"""Battle.net Game Data API (ahtool/bnet.py) with a fake client - no network. Run: python tests/test_bnet.py"""
import json
import os
import sys
import tempfile
import threading
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import make_fixture_db  # noqa: E402
from ahtool import bnet, db, market, server  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print("FAIL:", msg)
    else:
        print("PASS", msg)


# ---------------------------------------------------------------- settings: off without a client
check(bnet.settings({}) is None and bnet.settings({"bnet_client_id": "a"}) is None, "no client -> off")
check(bnet.start({}, lambda: None) is None, "start() does nothing without a client")
s = bnet.settings({"bnet_client_id": "a", "bnet_client_secret": "b", "bnet_houses": "Alliance=110,Neutral=116",
                   "bnet_ruleset": "1"})
check(s["houses"] == {"Alliance": 110, "Neutral": 116} and s["ruleset"] == 1 and s["host"] == "eu.api.blizzard.com",
      "settings from environment strings %s" % s)
check(bnet.settings({"bnet_client_id": "a", "bnet_client_secret": "b"})["houses"] == bnet.DEFAULT_HOUSES,
      "default houses")
for bad in ({"bnet_region": "mars"}, {"bnet_houses": {"Pirates": 1}}):
    try:
        bnet.settings(dict({"bnet_client_id": "a", "bnet_client_secret": "b"}, **bad))
        check(False, "refused %s" % bad)
    except bnet.BnetError:
        check(True, "refused %s" % bad)

# ---------------------------------------------------------------- summarise: same rules as a full scan
summ = bnet.summarise(
    [{"item": {"id": 1}, "buyout": 300, "quantity": 3}, {"item": {"id": 1}, "buyout": 500, "quantity": 1},
     {"item": {"id": 2}, "quantity": 1, "bid": 5}],
    [{"item": {"id": 3}, "quantity": 10, "unit_price": 20}, {"item": {"id": 3}, "quantity": 30, "unit_price": 25}])
check(summ[1] == (2, 4, 100, 100), "item auctions: per-unit min and weighted median %s" % (summ[1],))
check(summ[2] == (1, 1, None, None), "bid-only auction counted, no price %s" % (summ[2],))
check(summ[3] == (2, 40, 20, 25), "commodities: unit prices %s" % (summ[3],))


# ---------------------------------------------------------------- fake API
class Fake:
    def __init__(self):
        self.calls = []
        self.lm = "Fri, 27 Nov 2026 18:22:31 GMT"

    def token(self):
        return "t"

    def get(self, path, namespace, if_modified_since=None, timeout=120):
        self.calls.append((path, namespace))
        if path.startswith("/data/wow/item/"):
            iid = int(path.rsplit("/", 1)[1])
            if iid == 2589:
                return 200, {"id": iid, "name": "Linen Cloth", "sell_price": 7, "purchase_price": 0,
                             "purchase_quantity": 1}, None
            if iid == 2604:
                return 200, {"id": iid, "name": "Red Dye", "sell_price": 99, "purchase_price": 250,
                             "purchase_quantity": 5}, None
            return 404, None, None
        if path == "/data/wow/ruleset/1":
            return 200, {"id": 1, "name": "PvP"}, None
        if path.endswith("/commodities"):
            return 200, {"auctions": [{"item": {"id": 2589}, "quantity": 20, "unit_price": 30}]}, self.lm
        if path.startswith("/data/wow/ruleset/1/auctions/"):
            if if_modified_since == self.lm:
                return 304, None, self.lm
            return 200, {"auctions": [{"item": {"id": 2592}, "buyout": 5000, "quantity": 5}]}, self.lm
        return 404, None, None


tmp = tempfile.mkdtemp()
p = os.path.join(tmp, "t.db")
make_fixture_db.build(p)
conn = db.connect(p)
market.ensure_schema(conn)
with conn:
    conn.execute("INSERT OR REPLACE INTO items(id, name, quality, vendor_sell, vendor_buy) VALUES "
                 "(2589, 'Linen Cloth', 1, 13, NULL), (2604, 'Red Dye', 1, 12, 60), (2592, 'Wool Cloth', 1, 33, NULL)")
    conn.execute("INSERT OR IGNORE INTO recipe_reagents VALUES(990001, 2589, 1)")
    conn.execute("INSERT OR IGNORE INTO recipe_reagents VALUES(990001, 2604, 1)")
    conn.execute("INSERT OR REPLACE INTO vendor_items VALUES(2604, 60, 'builtin')")
    conn.execute("INSERT INTO item_sell(id, price, ts) VALUES(2604, 50, 1)")  # the game said 50: it wins
    conn.execute("INSERT OR REPLACE INTO item_tooltip(id, icon, data) VALUES(2589, 'x', '{\"id\":2589,\"sell_price\":13}')")

fake = Fake()
res = bnet.refresh_items(conn, fake, workers=2)
check(res["fetched"] >= 2 and res["found"] == 2, "items fetched %s" % res)
check(bnet.refresh_items(conn, fake)["fetched"] == 0, "fresh item data is not fetched again")
bnet.apply_items(conn)
vs = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT id, vendor_sell, vendor_buy FROM items WHERE id IN (2589, 2604)")}
check(vs[2589][0] == 7, "Blizzard sell price used where the game gave none %s" % vs)
check(vs[2604][0] == 12, "the game's own price (item_sell) is not overwritten by Blizzard's %s" % vs)
check(vs[2604][1] == 50, "vendor buy per unit (250 for 5) %s" % vs)
check(conn.execute("SELECT price FROM vendor_items WHERE item_id=2604").fetchone()[0] == 50,
      "builtin vendor list re-priced")
check(json.loads(conn.execute("SELECT data FROM item_tooltip WHERE id=2589").fetchone()[0])["sell_price"] == 7,
      "tooltip shows Blizzard's sell price")
check(bnet.apply_items(conn) == 0, "applying again changes nothing")

# ---------------------------------------------------------------- house snapshots
s = bnet.settings({"bnet_client_id": "a", "bnet_client_secret": "b", "bnet_houses": {"Alliance": 110}})
check(bnet.snapshot_houses(conn, fake, s, now=1000) == 1, "first snapshot stored")
check(bnet.snapshot_houses(conn, fake, s, now=2000) == 0, "unchanged house (304) -> nothing stored")
snap = conn.execute("SELECT realm, faction, house, auctions, units, items FROM bnet_ah_snapshots").fetchone()
check(tuple(snap) == ("PvP", "Alliance", 110, 2, 25, 2), "snapshot row %s" % (tuple(snap),))
fake.lm = "Fri, 27 Nov 2026 19:22:31 GMT"
for i in range(bnet.KEEP_SNAPSHOTS + 3):
    fake.lm = "lm %d" % i
    bnet.snapshot_houses(conn, fake, s, now=3000 + i)
n = conn.execute("SELECT COUNT(*) FROM bnet_ah_snapshots").fetchone()[0]
check(n == bnet.KEEP_SNAPSHOTS, "old snapshots pruned (%d)" % n)
check(conn.execute("SELECT COUNT(*) FROM bnet_ah_items WHERE snapshot_id NOT IN (SELECT id FROM bnet_ah_snapshots)")
      .fetchone()[0] == 0, "their items pruned too")
check(conn.execute("SELECT COUNT(*) FROM ah_scans WHERE source='bnet'").fetchone()[0] == 0,
      "snapshots never become scans (kept apart for now)")

# ---------------------------------------------------------------- compare with your own scan
c = bnet.compare(conn, "Horde")
check(c["snapshot"] is None, "no snapshot for Horde")
with conn:
    conn.execute("INSERT INTO ah_scans(id, ts, realm, faction, char, source, total, n, partial, raw_kept) "
                 "VALUES(900, 3500, 'PvP', 'Alliance', 'Me', 'addon', 2, 2, 0, 1)")
    conn.execute("INSERT INTO ah_scan_items VALUES(900, 2589, 1, 10, 30, 30, 1), (900, 2592, 1, 5, 800, 800, 1), "
                 "(900, 4306, 1, 1, 50, 50, 1)")
c = bnet.compare(conn, "Alliance")
sm = c["summary"]
check(c["scan"]["id"] == 900 and sm["items_both"] == 2 and sm["items_only_scan"] == 1 and sm["same_min_price"] == 1,
      "compare summary %s" % sm)
d = {x["item_id"]: x for x in c["items"]}
check(d[2592]["diff"] == 0.25 and d[2592]["blizzard"]["min_unit"] == 1000 and d[2592]["scan"]["min_unit"] == 800,
      "per-item difference %s" % d.get(2592))

# ---------------------------------------------------------------- API route
srv = server.make_server({}, lambda: db.connect(p), port=0)
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = "http://127.0.0.1:%d/api/bnet/compare?faction=Alliance" % srv.server_address[1]
with urllib.request.urlopen(url, timeout=10) as r:
    j = json.loads(r.read())
check(j["enabled"] is False and j["summary"]["items_both"] == 2, "API: off without a client, data still shown")
try:
    urllib.request.urlopen(url.replace("Alliance", "Pirates"), timeout=10)
    check(False, "bad faction refused")
except urllib.error.HTTPError as e:
    check(e.code == 400, "bad faction refused (%d)" % e.code)
srv.shutdown()
conn.close()

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
