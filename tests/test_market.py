"""AH Tool Logger import + market analytics. Run: python tests/test_market.py"""
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ahtool import db, market  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print("FAIL:", msg)


# ---- diff_scans unit tests
# rows: (item, suffix, count, buyout, min_bid, bid, time_left, owner)
prev = [
    (1, 0, 20, 2000, 1500, 0, 4, "Alice"),   # stays
    (1, 0, 20, 2000, 1500, 0, 4, "Alice"),   # one of the two disappears -> sold
    (2, 0, 1, 50000, 40000, 0, 1, "Bob"),    # short time left, gap 1h -> expired
    (3, 0, 5, 500, 400, 0, 3, "Carl"),       # Carl reposts item 3 cheaper -> relisted
    (4, 0, 1, 900, 800, 0, 4, ""),           # owner unknown, still there with owner -> matched
    (5, 0, 2, 0, 300, 350, 2, "Dan"),        # bid only, had a bid, gone -> sold at bid
]
cur = [
    (1, 0, 20, 2000, 1500, 0, 4, "Alice"),
    (3, 0, 5, 450, 400, 0, 4, "Carl"),
    (4, 0, 1, 900, 800, 0, 4, "Eve"),
]
res = sorted(market.diff_scans(prev, cur, 3600))
kinds = {(r[0], r[5]) for r in res}
check((1, "sold") in kinds, "item 1 sold: %s" % res)
check((2, "expired") in kinds, "item 2 expired: %s" % res)
check((3, "relisted") in kinds, "item 3 relisted: %s" % res)
check(not any(r[0] == 4 for r in res), "item 4 matched despite missing owner: %s" % res)
check(any(r[0] == 5 and r[5] == "sold" and r[3] == 175 for r in res), "item 5 sold at bid 350/2: %s" % res)
check(len(res) == 4, "4 gone auctions: %s" % res)

# ---- buy_cost
c = market.buy_cost([(100, 20), (90, 5), (200, 20)], 30)
check(c["bought"] == 45 and c["total"] == 90 * 5 + 100 * 20 + 200 * 20 and c["short"] == 0, "buy_cost %s" % c)
c = market.buy_cost([(100, 5)], 10)
check(c["short"] == 5 and c["bought"] == 5, "buy_cost short %s" % c)

# ---- full ingest from a fake AHToolLogger.lua
tmp = tempfile.mkdtemp()
dbp = os.path.join(tmp, "t.db")
conn = db.connect(dbp)
conn.execute("INSERT INTO items(id, name, quality, vendor_sell) VALUES(2589, 'Linen Cloth', 1, 13), "
             "(2592, 'Wool Cloth', 1, 33), (11287, 'Lesser Magic Wand', 2, 508)")
conn.commit()
now = int(time.time())


def scan(t, rows, nm=""):
    d = ";".join(",".join(str(x) for x in r) for r in rows)
    return '{ ["t"] = %d, ["realm"] = "PvP", ["faction"] = "Alliance", ["char"] = "Me", ["src"] = "addon", '            '["total"] = %d, ["n"] = %d, ["d"] = "%s", ["nm"] = "%s" }' % (t, len(rows), len(rows), d, nm)


s1 = [(2589, 0, 20, 2000, 1000, 0, 4, "Alice")] * 3 + [(2592, 0, 10, 3000, 2000, 0, 4, "Bob")]
s2 = [(2589, 0, 20, 2000, 1000, 0, 4, "Alice")] * 1 + [(2592, 0, 10, 3000, 2000, 0, 4, "Bob"),
                                                       (2592, 0, 5, 1000, 900, 0, 4, "Zed")]
lua = """AHToolLoggerDB = {
["v"] = 1,
["scans"] = { %s, %s },
["owned"] = { ["PvP-Me"] = { ["t"] = %d, ["realm"] = "PvP", ["char"] = "Me", ["d"] = "2592,0,10,4000,3000,0,4,Me,0" } },
["mail"] = {
  ["sold|Linen Cloth|Buyer|2000|20|1"] = { ["t"] = %d, ["type"] = "sold", ["item"] = "Linen Cloth", ["player"] = "Buyer",
     ["price"] = 2000, ["fee"] = 100, ["count"] = 20, ["char"] = "Me" },
  ["expired|Wool Cloth||0|10|1"] = { ["t"] = %d, ["type"] = "expired", ["item"] = "Wool Cloth", ["count"] = 10 },
},
["sell"] = { [11287] = 254, [2589] = 13, [2592] = 0, [-5] = 1, [7] = -1, [8] = "x", [999999] = 77 },
}
""" % (scan(now - 7200, s1), scan(now - 3600, s2, "253664:2:Forever Tunic;2589:1:Renamed Cloth;bad;7:x:"),
       now - 100, now - 5000, now - 4000)
lp = os.path.join(tmp, "AHToolLogger.lua")
with open(lp, "w", encoding="utf-8") as f:
    f.write(lua)
cfg = {"logger_savedvariables": lp, "ah_cut": 0.05, "addons_dir": ""}
r = market.ingest(conn, cfg)
check(r.get("scans") == 2 and r.get("sold") == 2 and r.get("mail") == 2, "ingest summary %s" % r)
check(market.ingest(conn, cfg).get("skipped"), "second ingest skipped")
r = market.ingest(conn, cfg, force=True)
check(r.get("scans") == 0 and r.get("mail") == 0, "forced re-ingest adds nothing %s" % r)

# item names from scans (logger 1.1): unknown items get a name, known names are kept
check(market.parse_names("1:2:A:b;x;3:-1:C") == {1: ("A:b", 2), 3: ("C", None)}, "parse_names")
named = {i: tuple(v) for i, *v in conn.execute("SELECT id, name, quality FROM items WHERE id IN (253664, 2589)")}
check(named.get(253664) == ("Forever Tunic", 2), "custom item named from scan %s" % named)
check(named.get(2589) == ("Linen Cloth", 1), "existing name kept %s" % named)
conn.execute("DELETE FROM items WHERE id=253664")
check(market.apply_names(conn) == 1, "names re-applied after an items rebuild")

# vendor sell prices the game reported (logger 1.3, GetItemInfo) win over the static data
vs = dict(conn.execute("SELECT id, vendor_sell FROM items WHERE id IN (11287, 2589, 2592)").fetchall())
check(vs == {11287: 254, 2589: 13, 2592: 0}, "game sell prices applied %s" % vs)
check(conn.execute("SELECT COUNT(*) FROM items WHERE id=999999").fetchone()[0] == 0,
      "a sell price never creates an item")
check(market.parse_sell({1: 5, "2": 6, -1: 3, 3: -1, 4: True, 5: float("nan"), 6: 2 ** 41}) == {1: 5, 2: 6},
      "parse_sell keeps only sane rows")
conn.execute("UPDATE items SET vendor_sell=508 WHERE id=11287")  # a static data rebuild puts the CSV price back
check(market.apply_sell(conn) == 1 and conn.execute("SELECT vendor_sell FROM items WHERE id=11287").fetchone()[0] == 254,
      "game sell price re-applied after a static rebuild")
check(market.apply_sell(conn) == 0, "apply_sell changes nothing the second time")
conn.execute("INSERT INTO item_tooltip(id, icon, data) VALUES(11287, 'x', '{\"id\":11287,\"sell_price\":508}'), "
             "(2592, 'x', '{\"id\":2592,\"sell_price\":33}'), (2589, 'x', 'not json')")
market.apply_sell(conn)
tt = {i: d for i, d in conn.execute("SELECT id, data FROM item_tooltip")}
check(json.loads(tt[11287]).get("sell_price") == 254, "tooltip shows the game's sell price %s" % tt[11287])
check("sell_price" not in json.loads(tt[2592]), "sell price 0: no Sell Price line %s" % tt[2592])
check(tt[2589] == "not json", "broken tooltip data left alone")

# sold only when it could not have run out: a "long" (2-12 h) auction gone after 11 h most likely expired
row = (7, 0, 1, 500, 400, 0, 3, "Ann")
check(market.diff_scans([row], [], 11 * 3600)[0][5] == "expired", "long auction gone after 11 h -> expired")
check(market.diff_scans([row], [], 3600)[0][5] == "sold", "long auction gone after 1 h -> sold")
check(market.diff_scans([row[:6] + (4, "Ann")], [], 11 * 3600)[0][5] == "sold", "very long gone after 11 h -> sold")
check(market.diff_scans([row[:6] + (1, "Ann")], [], 600)[0][5] == "expired", "short auction gone -> may have expired")

# incomplete scans: judged by what the game reported and by the items on sale, not by the number of auctions
cur = conn.cursor()
rf = conn.execute("SELECT realm, faction FROM ah_scans LIMIT 1").fetchone()
prev_items = conn.execute("SELECT COUNT(*) FROM ah_scan_items WHERE scan_id=(SELECT MAX(id) FROM ah_scans)").fetchone()[0]
far = 2 ** 40
check(not market._is_partial(cur, rf[0], rf[1], far, 10, 10, prev_items), "half the auctions, same items -> complete")
check(market._is_partial(cur, rf[0], rf[1], far, 10, 20, prev_items), "read fewer auctions than reported -> partial")
check(market._is_partial(cur, rf[0], rf[1], far, 0, 0, 0), "empty scan -> partial")
check(market._is_partial(cur, rf[0], rf[1], far, 10, 10, 0) == (prev_items > 0), "far fewer items -> partial")

# vendor flips: every listing of the latest full scan under what a vendor pays (Wool Cloth: game price 0 -> none)
conn.execute("UPDATE items SET vendor_sell=150 WHERE id=2589")  # Linen: listed at 100/unit in both scans
vf = market.vendor_flips(conn, cfg)
it = {e["item_id"]: e for e in vf["items"]}
check(list(it) == [2589] and it[2589]["qty"] == 20 and it[2589]["listings"] == 1, "vendor flips: one listing %s" % vf)
check(it[2589]["cost"] == 2000 and it[2589]["revenue"] == 3000 and it[2589]["profit"] == 1000 and it[2589]["game_price"],
      "vendor flips: cost, revenue, profit %s" % it[2589])
check(vf["totals"]["profit"] == 1000 and vf["totals"]["profit_game_price"] == 1000 and vf["totals"]["qty"] == 20,
      "vendor flips totals %s" % vf["totals"])
from ahtool import analytics  # noqa: E402
conn.execute("INSERT OR REPLACE INTO price_current VALUES('PvP', 2589, 100, 20, 2511, 1)")
analytics.invalidate()
dl = {d["item_id"]: d for d in analytics.deals(conn, cfg, realm="PvP")}
check(dl.get(2589, {}).get("vendor_profit_total") == 1000 and dl[2589]["vendor_qty"] == 20,
      "deals row carries the total vendor profit of the scan %s" % dl.get(2589))
conn.execute("UPDATE items SET vendor_sell=13 WHERE id=2589")
check(market.vendor_flips(conn, cfg)["totals"]["items"] == 0, "nothing under vendor price -> empty")

# the client's hotfix cache grows while you play (new Forever prices): tooltips are rebuilt when it changes
from ahtool import staticdata, tooltipdata  # noqa: E402
conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('tooltip_version', ?)", (tooltipdata.VERSION,))
dbc = os.path.join(tmp, "DBCache.bin")
with open(dbc, "wb") as f:
    f.write(b"x" * 10)
tcfg = {"dbcache_path": dbc}
check(staticdata.tooltips_current(conn) is True, "without cfg only the version counts")
check(not staticdata.tooltips_current(conn, tcfg), "built before the cache was tracked -> rebuild once")
conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('dbcache_sig', ?)", (staticdata.dbcache_sig(tcfg),))
check(staticdata.tooltips_current(conn, tcfg), "same hotfix cache -> current")
with open(dbc, "ab") as f:
    f.write(b"more hotfixes")
check(not staticdata.tooltips_current(conn, tcfg), "hotfix cache grew -> rebuild")
check(staticdata.dbcache_sig({"dbcache_path": os.path.join(tmp, "missing")}) == "", "no cache file -> empty signature")
conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('dbcache_sig', '')")
check(staticdata.tooltips_current(conn, {"dbcache_path": ""}), "no cache configured and none recorded -> current")

m = market.item_market(conn, cfg, 2592, qty=12)
check(m["summary"]["qty"] == 15 and m["summary"]["sellers"] == 2, "summary %s" % m["summary"])
check(m["cost"]["total"] == 4000 and m["cost"]["bought"] == 15, "cost %s" % m["cost"])
check(m["depth"][0]["unit"] == 200 and m["depth"][-1]["cum_qty"] == 15, "depth %s" % m["depth"])
s = market.item_market(conn, cfg, 2589)["sales"]
check(s["totals"]["sold"] == 40 and s["totals"]["avg_unit"] == 100, "sales %s" % s["totals"])

a = market.activity(conn, cfg)
check(a["items"] and a["items"][0]["item_id"] == 2589 and a["items"][0]["sold"] == 40, "activity %s" % a["items"])
check(a["sellers"] and a["sellers"][0]["owner"] == "Bob", "sellers %s" % a["sellers"])

my = market.my_market(conn, cfg)
check(my["totals"]["sold_net"] == 1900 and my["totals"]["expired"] == 1, "my totals %s" % my["totals"])
check(my["auctions"] and my["auctions"][0]["status"] == "undercut" and my["auctions"][0]["lowest_other"] == 200,
      "my auctions %s" % my["auctions"])
lin = [i for i in my["items"] if i["name"] == "Linen Cloth"][0]
check(lin["item_id"] == 2589, "mail name -> item id %s" % lin)

st = market.logger_status(conn, cfg)
check(st["scans"] == 2 and st["mail_records"] == 2, "status %s" % st)
conn.close()

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
