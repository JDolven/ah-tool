"""Prices from AH Tool Logger scans, realm choice and the empty realm name. Run: python tests/test_scanprices.py"""
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ahtool import analytics, db, keys, market, scanprices, sync  # noqa: E402
from ahtool.ingest import _today  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print("FAIL:", msg)
    else:
        print("PASS", msg)


NOW = int(time.time())
TODAY = _today(NOW)
tmp = tempfile.mkdtemp()


def scan(t, rows, realm="PvP", faction="Alliance", char="Me"):
    d = ";".join(",".join(str(x) for x in r) for r in rows)
    return '{ ["t"] = %d, ["realm"] = "%s", ["faction"] = "%s", ["char"] = "%s", ["src"] = "addon", ' \
           '["total"] = %d, ["n"] = %d, ["d"] = "%s" }' % (t, realm, faction, char, len(rows), len(rows), d)


def write(path, scans):
    with open(path, "w", encoding="utf-8") as f:
        f.write('AHToolLoggerDB = { ["v"] = 1, ["scans"] = { %s } }\n' % ", ".join(scans))


def q(c, sql, a=()):
    return [tuple(r) for r in c.execute(sql, a)]


# ---------------------------------------------------------------- the price key
known = {"PvP", "PvP Alliance"}
check(scanprices.price_realm(known, "PvP", "Alliance") == "PvP Alliance", "faction-keyed Auctionator realm is used")
check(scanprices.price_realm(known, "PvP", "Neutral") == "PvP Neutral", "a neutral house gets its own key")
check(scanprices.price_realm({"PvP"}, "PvP", "Alliance") == "PvP", "old-style Auctionator key (realm only) is kept")
check(scanprices.price_realm(set(), "PvP", "Horde") == "PvP Horde", "without Auctionator data: realm + faction")
check(scanprices.price_realm(known, "", "Alliance") is None, "no realm -> no key")

# ---------------------------------------------------------------- a local app with two Auctionator keys
path = os.path.join(tmp, "local.db")
c = db.connect(path)
market.ensure_schema(c)
OLD = TODAY - 4
for realm in ("PvP", "PvP Alliance"):
    # identical history up to the switch: same items, same old day
    for iid, price in ((2589, 100), (2453, 900), (118, 50)):
        c.execute("INSERT INTO price_current VALUES(?,?,?,?,?,?)", (realm, iid, price, 10, OLD, NOW - 4 * 86400))
        c.execute("INSERT INTO price_daily VALUES(?,?,?,?,?,?)", (realm, iid, OLD, price, price, 10))
    c.execute("INSERT INTO ingests(ts,file_mtime,realm,items,changed) VALUES(?,?,?,3,3)",
              (NOW - 4 * 86400, NOW - 4 * 86400, realm))
# the old key's ingest row is the newest - the old rule picked it
c.execute("INSERT INTO ingests(ts,file_mtime,realm,items,changed) VALUES(?,?,'PvP',3,0)", (NOW - 3600, NOW - 3600))
# a newer Auctionator price for 118 (looked at after the scan): it must stay
c.execute("UPDATE price_current SET price=55, last_day=?, updated_at=? WHERE realm='PvP Alliance' AND item_id=118",
          (TODAY, NOW - 60))
c.commit()

lp = os.path.join(tmp, "AHToolLogger.lua")
cfg = {"logger_savedvariables": lp}
T1, T2 = NOW - 1800, NOW - 900
write(lp, [
    scan(T1, [(2589, 0, 20, 2000, 1500, 0, 4, "Alice"), (2589, 0, 10, 800, 700, 0, 4, "Bob"),
              (2453, 0, 1, 1200, 1000, 0, 4, "Carl"), (118, 0, 1, 70, 60, 0, 4, "Dan")]),
    # newer scan: 2589 cheaper; 2453 gone (not on the AH) - its last price stays
    scan(T2, [(2589, 0, 20, 1400, 1300, 0, 4, "Alice"), (118, 0, 1, 80, 70, 0, 4, "Dan"),
              (765, 0, 1, 30, 20, 0, 4, "Eve"), (765, 0, 1, 31, 20, 0, 4, "Eve")]),
    # a neutral house scan at the same time must not mix in
    scan(T2 + 5, [(2589, 0, 1, 5, 4, 0, 4, "Goblin")], faction="Neutral"),
])
res = market.ingest(c, cfg)
check(res.get("prices", {}).get("scans") == 3, "both Alliance scans and the neutral one priced %s" % res.get("prices"))
check(q(c, "SELECT price, available, last_day, updated_at FROM price_current WHERE realm='PvP Alliance' AND "
           "item_id=2589") == [(70, 20, TODAY, T2)], "current price = newest scan's lowest unit price and quantity")
check(q(c, "SELECT price FROM price_current WHERE realm='PvP Alliance' AND item_id=2453") == [(1200,)],
      "an item missing from the newest scan keeps its last seen price")
check(q(c, "SELECT price FROM price_current WHERE realm='PvP Alliance' AND item_id=118") == [(55,)],
      "a newer Auctionator price is not replaced by an older scan")
check(q(c, "SELECT low, high, available FROM price_daily WHERE realm='PvP Alliance' AND item_id=2589 AND day=?",
        (TODAY,)) == ([(70, 80, 30)] if _today(T1) == TODAY else [(70, 70, 20)]),
      "today's daily row merges the day's scans (lowest low, highest high, most available)")
check(q(c, "SELECT price FROM price_current WHERE realm='PvP' AND item_id=2589") == [(100,)],
      "the old Auctionator key is left alone")
check(q(c, "SELECT price FROM price_current WHERE realm='PvP Neutral' AND item_id=2589") == [(5,)],
      "neutral house prices go to their own key")
check(q(c, "SELECT COUNT(*) FROM snapshots WHERE realm='PvP Alliance' AND item_id=2589") == [(2,)],
      "a snapshot per price change")
check(analytics.get_realm(c) == "PvP Alliance", "default realm = the one with the newest data (%s)"
      % analytics.get_realm(c))
check(analytics.status(c, {})["latest_date"] == analytics.day_to_iso(TODAY), "status shows today's prices")

# importing again changes nothing
n_snap = q(c, "SELECT COUNT(*) FROM snapshots")
res = market.ingest(c, cfg, force=True)
check(res["prices"]["scans"] == 0 and q(c, "SELECT COUNT(*) FROM snapshots") == n_snap, "each scan priced once")

# partial and pulled scans are never priced
c.execute("INSERT INTO ah_scans(ts,realm,faction,char,source,total,n,partial) VALUES(?, 'PvP', 'Alliance', '', "
          "'web', 1, 1, 0)", (NOW - 100,))
wid = c.execute("SELECT MAX(id) FROM ah_scans").fetchone()[0]
c.execute("INSERT INTO ah_scan_items VALUES(?, 2589, 1, 1, 1, 1, 1)", (wid,))
c.commit()
scanprices.derive(c.cursor(), {})
check(q(c, "SELECT price FROM price_current WHERE realm='PvP Alliance' AND item_id=2589") == [(70,)],
      "a scan pulled from the web version is not priced as your own")

# switched off
c2 = db.connect(os.path.join(tmp, "off.db"))
market.ensure_schema(c2)
res = market.ingest(c2, dict(cfg, prices_from_logger=False))
check(q(c2, "SELECT COUNT(*) FROM price_current") == [(0,)], "prices_from_logger=False keeps prices Auctionator-only")
c2.close()

# ---------------------------------------------------------------- empty realm name (GetRealmName() == "")
c3 = db.connect(os.path.join(tmp, "empty.db"))
market.ensure_schema(c3)
# a scan stored with realm '' by the old app
c3.execute("INSERT INTO ah_scans(ts,realm,faction,char,source,total,n,partial,priced) VALUES(?, '', 'Alliance', "
           "'Me', 'addon', 1, 1, 0, 0)", (NOW - 7200,))
sid = c3.execute("SELECT MAX(id) FROM ah_scans").fetchone()[0]
c3.execute("INSERT INTO ah_scan_items VALUES(?, 2589, 1, 3, 300, 300, 1)", (sid,))
c3.commit()
lp3 = os.path.join(tmp, "empty.lua")
write(lp3, [scan(NOW - 9000, [(2589, 0, 1, 400, 300, 0, 4, "A")]),
            scan(NOW - 7200, [(2589, 0, 3, 900, 800, 0, 4, "A")], realm=""),
            scan(NOW - 600, [(2589, 0, 2, 500, 400, 0, 4, "A")], realm="")])
market.ingest(c3, {"logger_savedvariables": lp3})
check(q(c3, "SELECT COUNT(*) FROM ah_scans WHERE realm=''") == [(0,)], "no scan left without a realm")
check(q(c3, "SELECT COUNT(*) FROM ah_scans") == [(3,)], "the repaired old scan is not stored twice")
check(q(c3, "SELECT DISTINCT realm FROM ah_scans") == [("PvP",)], "empty realm names take the file's realm")
check(q(c3, "SELECT price FROM price_current WHERE item_id=2589") == [(250,)],
      "scans that had no realm are priced too (newest: 500 for 2)")
c3.close()

# unchanged file, unpriced scans (an upgrade): the next start prices them
c4 = db.connect(os.path.join(tmp, "upgrade.db"))
market.ensure_schema(c4)
lp4 = os.path.join(tmp, "upgrade.lua")
write(lp4, [scan(NOW - 600, [(2589, 0, 2, 500, 400, 0, 4, "A")])])
market.ingest(c4, {"logger_savedvariables": lp4, "prices_from_logger": False})
check(q(c4, "SELECT COUNT(*) FROM price_current") == [(0,)], "imported without prices first")
res = market.ingest(c4, {"logger_savedvariables": lp4})
check(not res.get("skipped") and q(c4, "SELECT price FROM price_current") == [(250,)],
      "an unchanged file is read again once to price stored scans %s" % res)
check(market.ingest(c4, {"logger_savedvariables": lp4}).get("skipped"), "then skipped as before")
c4.close()

# ---------------------------------------------------------------- web version: the owner's realm follows new data
w = db.connect(os.path.join(tmp, "web.db"))
sync.ensure_schema(w)
keys.create(w, "Own", owner=True)
own = {"name": "Own", "owner": True, "key_id": keys.key_ids(w, "Own")[0]}
SD = (NOW - sync.DAY_EPOCH_TS) // 86400
rows_old = [["PvP", i, 100, 5, SD - 4, NOW - 4 * 86400] for i in (1, 2, 3)]
rows_new = [["PvP Alliance", i, 100, 5, SD, NOW - 60] for i in (1, 2, 3)]
sync.apply(w, {}, {"protocol": sync.PROTOCOL, "price_current": rows_new + rows_old}, own)
check(analytics.get_realm(w) == "PvP Alliance", "same number of items: the realm with the newer data wins (%s)"
      % analytics.get_realm(w))
# a later batch with only the old key's history keeps it
sync.apply(w, {}, {"protocol": sync.PROTOCOL, "price_daily": [["PvP", 1, SD - 6, 90, 110, 5]]}, own)
check(analytics.get_realm(w) == "PvP Alliance", "a batch with only the old key's history does not switch back")
w.close()
c.close()

# ---------------------------------------------------------------- two connections upgrade the same old database
# (the web server's first requests on a fresh volume): both see "priced" missing, the slower ALTER must not fail.
import sqlite3 as _sq  # noqa: E402
import tempfile as _tf  # noqa: E402
from ahtool import schema, scanprices  # noqa: E402
old_db = os.path.join(_tf.mkdtemp(), "old.db")
c1, c2 = _sq.connect(old_db), _sq.connect(old_db)
c1.execute("CREATE TABLE ah_scans(id INTEGER PRIMARY KEY, ts INTEGER)")
c1.commit()
check(schema.add_column(c1, "ah_scans", "priced INTEGER NOT NULL DEFAULT 0"), "first connection adds the column")
c1.commit()
try:
    added = schema.add_column(c2, "ah_scans", "priced INTEGER NOT NULL DEFAULT 0")
    check(added is False, "second connection: column already there is not an error")
except _sq.OperationalError as e:
    check(False, "second connection raised %s" % e)
scanprices.ensure_schema(c2)
check(sum(1 for r in c2.execute("PRAGMA table_info(ah_scans)") if r[1] == "priced") == 1, "exactly one priced column")
try:
    schema.add_column(c2, "no_such_table", "x INTEGER")
    check(False, "other errors still raise")
except _sq.OperationalError:
    check(True, "other errors still raise")
c1.close()
c2.close()

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
