"""Realm aliases on the web version (AHTOOL_REALM_ALIASES) and the realm picker. Run: python tests/test_realm_alias.py"""
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ahtool import analytics, db, sync  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print("FAIL:", msg)
    else:
        print("PASS", msg)


# ---------------------------------------------------------------- parsing
A = sync.realm_aliases("PvP=PvP Alliance; CAMWOW EU5 Realm 4 PvP Alliance = PvP Alliance ;")
check(A == {"PvP": "PvP Alliance", "CAMWOW EU5 Realm 4 PvP Alliance": "PvP Alliance"}, "parse %s" % A)
check(sync.realm_aliases("") == {} and sync.realm_aliases(None) == {}, "empty = no aliases")
for bad in ("PvP", "PvP=PvP", "A=B;B=C", "A=B;A=C", "=B", "A=", "x" * 70 + "=B"):
    try:
        sync.realm_aliases(bad)
        check(False, "refused %r" % bad[:20])
    except ValueError:
        check(True, "refused %r" % bad[:20])

# ---------------------------------------------------------------- stored data merged at start
tmp = tempfile.mkdtemp()
path = os.path.join(tmp, "web.db")
conn = db.connect(path)
sync.ensure_schema(conn)
lim = sync._limits()
today, now = lim["today"], int(time.time())
OWN = {"name": "owner", "owner": True, "key_id": None}


def push(cfg, **kw):
    return sync.apply(conn, cfg, dict({"protocol": sync.PROTOCOL}, **kw), OWN)


# old Auctionator key, the current key and the logger key, as they arrived before aliases existed
push({}, price_daily=[["PvP", 2589, today - 10, 100, 120, 5], ["PvP Alliance", 2589, today - 10, 100, 120, 5],
                      ["PvP Alliance", 2589, today, 90, 95, 7],
                      ["CAMWOW EU5 Realm 4 PvP Alliance", 2589, today, 80, 99, 9]],
     price_current=[["PvP", 2589, 120, 5, today - 10, now - 900000],
                    ["PvP Alliance", 2589, 95, 7, today, now - 600],
                    ["CAMWOW EU5 Realm 4 PvP Alliance", 2589, 80, 9, today, now - 60]],
     snapshots=[["CAMWOW EU5 Realm 4 PvP Alliance", 2589, now - 60, 80, 9], ["PvP Alliance", 2589, now - 600, 95, 7]])
conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('sync_owner_realm','PvP Alliance')")
conn.commit()
before = analytics.list_realms(conn)
check("PvP" not in before, "stale old key hidden from the picker even without aliases %s" % before)

seq0 = int(conn.execute("SELECT value FROM meta WHERE key='sync_seq'").fetchone()[0])
res = sync.merge_realm_aliases(conn, A)
check(set(res.get("aliases", {})) == set(A), "both aliases merged %s" % res)
realms = {t: sorted({r[0] for r in conn.execute("SELECT DISTINCT realm FROM %s" % t)})
          for t in ("price_daily", "price_daily_src", "price_current", "price_current_src", "snapshots", "ingests")}
check(all(v == ["PvP Alliance"] for k, v in realms.items() if k != "ingests"), "only the canonical key left %s" % realms)
lo, hi, av = conn.execute("SELECT low, high, available FROM price_daily WHERE realm='PvP Alliance' AND item_id=2589 "
                          "AND day=?", (today,)).fetchone()
check((lo, hi, av) == (80, 99, 9), "daily merged: lowest low, highest high, most listed (%s %s %s)" % (lo, hi, av))
p = conn.execute("SELECT price, seq FROM price_current WHERE realm='PvP Alliance' AND item_id=2589").fetchone()
check(p[0] == 80, "current price = the newest observation (%s)" % p[0])
check(p[1] > seq0, "merged rows get a new seq, so PCs pull them")
check(conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 2, "snapshots moved, none lost")
check(analytics.list_realms(conn) == ["PvP Alliance"], "picker: one realm %s" % analytics.list_realms(conn))
check(sync.merge_realm_aliases(conn, A) == {}, "second start: nothing left to merge")

# ---------------------------------------------------------------- later pushes land under the canonical key
cfg = {"realm_aliases": A}
push(cfg, price_daily=[["CAMWOW EU5 Realm 4 PvP Alliance", 2589, today, 70, 100, 3]],
     price_current=[["CAMWOW EU5 Realm 4 PvP Alliance", 2589, 70, 3, today, now - 10]],
     snapshots=[["PvP", 2589, now - 10, 70, 3]])
check(conn.execute("SELECT COUNT(*) FROM price_daily WHERE realm<>'PvP Alliance'").fetchone()[0] == 0
      and conn.execute("SELECT COUNT(*) FROM price_current WHERE realm<>'PvP Alliance'").fetchone()[0] == 0
      and conn.execute("SELECT COUNT(*) FROM snapshots WHERE realm<>'PvP Alliance'").fetchone()[0] == 0,
      "pushed alias rows stored under the canonical key")
lo, hi = conn.execute("SELECT low, high FROM price_daily WHERE realm='PvP Alliance' AND item_id=2589 AND day=?",
                      (today,)).fetchone()
check((lo, hi) == (70, 100), "and merged with what was there (%s %s)" % (lo, hi))
check(analytics.list_realms(conn) == ["PvP Alliance"], "picker still one realm")
conn.close()

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
