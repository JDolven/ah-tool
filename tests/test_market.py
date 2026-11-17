"""AH Tool Logger import + market analytics. Run: python tests/test_market.py"""
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
conn.execute("INSERT INTO items(id, name, quality) VALUES(2589, 'Linen Cloth', 1), (2592, 'Wool Cloth', 1)")
conn.commit()
now = int(time.time())


def scan(t, rows):
    d = ";".join(",".join(str(x) for x in r) for r in rows)
    return '{ ["t"] = %d, ["realm"] = "PvP", ["faction"] = "Alliance", ["char"] = "Me", ["src"] = "addon", ' \
           '["total"] = %d, ["n"] = %d, ["d"] = "%s" }' % (t, len(rows), len(rows), d)


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
}
""" % (scan(now - 7200, s1), scan(now - 3600, s2), now - 100, now - 5000, now - 4000)
lp = os.path.join(tmp, "AHToolLogger.lua")
with open(lp, "w", encoding="utf-8") as f:
    f.write(lua)
cfg = {"logger_savedvariables": lp, "ah_cut": 0.05, "addons_dir": ""}
r = market.ingest(conn, cfg)
check(r.get("scans") == 2 and r.get("sold") == 2 and r.get("mail") == 2, "ingest summary %s" % r)
check(market.ingest(conn, cfg).get("skipped"), "second ingest skipped")
r = market.ingest(conn, cfg, force=True)
check(r.get("scans") == 0 and r.get("mail") == 0, "forced re-ingest adds nothing %s" % r)

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
