"""Daily market report (ahtool/report.py). Run: python tests/test_report.py"""
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ahtool import db, market, report  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print("FAIL:", msg)


tmp = tempfile.mkdtemp()
conn = db.connect(os.path.join(tmp, "t.db"))
# herbs (class 7 / subclass 9) and cloth (7 / 5)
conn.executemany("INSERT INTO items(id, name, quality, class_id, subclass_id) VALUES(?,?,1,7,?)",
                 [(765, "Silverleaf", 9), (2447, "Peacebloom", 9), (2450, "Briarthorn", 9), (2589, "Linen Cloth", 5)])
conn.commit()
now = int(time.time())
H = 3600


def rows(prices, keep=None):
    """prices {item: unit}; 4 stacks of 20 per item (Alice x2, Bob x2); keep {item: stacks left}."""
    out = []
    for iid, unit in prices.items():
        n = (keep or {}).get(iid, 4)
        for k in range(n):
            out.append((iid, 0, 20, unit * 20, unit * 10, 0, 4, "Alice" if k < 2 else "Bob"))
    return out


def scan(t, rs):
    d = ";".join(",".join(str(x) for x in r) for r in rs)
    return '{ ["t"] = %d, ["realm"] = "Forever", ["faction"] = "Alliance", ["char"] = "Me", ["src"] = "addon", ' \
           '["total"] = %d, ["n"] = %d, ["d"] = "%s" }' % (t, len(rs), len(rs), d)


before = {765: 100, 2447: 100, 2450: 100, 2589: 50}
after = {765: 150, 2447: 120, 2450: 80, 2589: 50}
scans = [
    scan(now - 40 * H, rows(before)),                       # the day before
    scan(now - 30 * H, rows(before)),
    scan(now - 20 * H, rows(after)),                        # report window (24h before the last scan)
    scan(now - 10 * H, rows(after, {2589: 2})),             # 2 stacks of Linen sold (very long time left)
    scan(now - 1 * H, rows(after, {2589: 2, 765: 3})),      # 1 stack of Silverleaf sold
]
lp = os.path.join(tmp, "AHToolLogger.lua")
with open(lp, "w", encoding="utf-8") as f:
    f.write('AHToolLoggerDB = { ["v"] = 1, ["scans"] = { %s }, ["owned"] = {}, ["mail"] = {} }\n' % ", ".join(scans))
cfg = {"logger_savedvariables": lp, "ah_cut": 0.05, "addons_dir": ""}
r = market.ingest(conn, cfg)
check(r.get("scans") == 5, "ingest %s" % r)

rep = report.daily(conn, cfg)
w = rep["window"]
check(w and w["scans"] == 3 and w["prev_scans"] == 2, "window %s" % w)
check(rep["date"] == report._date(now - H), "date %s" % rep["date"])
check([p["auctions"] for p in rep["listings"]] == [16, 14, 13], "listings %s" % rep["listings"])
check(rep["peak"]["auctions"] == 16 and rep["low"]["auctions"] == 13, "peak/low %s %s" % (rep["peak"], rep["low"]))

g = rep["gone"]
check(g and g["auctions"] == 1 and g["of"] == 14 and g["sold"] == 1, "gone %s" % g)

t = rep["totals"]
# observed: 2 x 20 Linen at 50c + 20 Silverleaf at 150c = 2000 + 3000 copper; the scan pairs ending in the
# window cover 10 + 10 + 9 hours
check(t["gold_observed"] == 5000 and t["units_observed"] == 60, "totals observed %s" % t)
check(w["covered_s"] == 29 * H, "covered %s" % w["covered_s"])
check(t["gold_day"] == round(5000 * 24 / 29), "gold per day %s" % t["gold_day"])
check(t["sell_through"] == 1.0, "sell-through %s" % t["sell_through"])
check(rep["top_gold"][0]["item_id"] == 765 and rep["top_gold"][0]["name"] == "Silverleaf", "top gold %s" % rep["top_gold"])
check(rep["top_units"][0]["item_id"] == 2589 and rep["top_units"][0]["sold"] == 40, "top units %s" % rep["top_units"])
check(rep["most_listed"][0]["qty"] == 80, "most listed %s" % rep["most_listed"])

check(rep["price_source"] == "scans", "price source %s" % rep["price_source"])
check([m["item_id"] for m in rep["up"]] == [765, 2447], "up %s" % rep["up"])
check(rep["up"][0]["pct"] == 0.5 and rep["up"][0]["before"] == 100 and rep["up"][0]["now"] == 150, "up pct %s" % rep["up"])
check([m["item_id"] for m in rep["down"]] == [2450] and rep["down"][0]["pct"] == -0.2, "down %s" % rep["down"])
check(rep["cheaper"] == 1 and rep["pricier"] == 2, "cheaper/pricier %s/%s" % (rep["cheaper"], rep["pricier"]))
groups = {x["key"]: x for x in rep["groups"]}
check("herbs" in groups and groups["herbs"]["pct"] == 0.2 and groups["herbs"]["pricier"] == 2
      and groups["herbs"]["cheaper"] == 1, "groups %s" % rep["groups"])
check("cloth" not in groups, "groups need 3+ items %s" % rep["groups"])
check(groups["all"]["items"] == 4, "all busy goods %s" % groups.get("all"))

# an earlier date: window ends at the last scan of that day
d0 = report._date(now - 40 * H)
check(d0 in rep["dates"], "dates %s" % rep["dates"])
old = report.daily(conn, cfg, date=d0)
last_on_d0 = max(t for t in (now - h * H for h in (40, 30, 20, 10, 1)) if report._date(t) == d0)
check(old["window"]["end"] == last_on_d0 and old["date"] == d0, "earlier date window %s" % old["window"])
try:
    report.daily(conn, cfg, date="2001-01-01")
    check(False, "unknown date raises")
except ValueError:
    pass

# no scans at all
empty = db.connect(os.path.join(tmp, "e.db"))
e = report.daily(empty, cfg)
check(e["window"] is None and e["dates"] == [], "empty %s" % e)
conn.close()
empty.close()

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
