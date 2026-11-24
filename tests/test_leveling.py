"""Leveling planner tests on a small synthetic DB. Run: python tests/test_leveling.py

Fixture (profession 8, realm PvP, all prices flat for 10 days so baseline == current):
  1001 Cloth        AH 10
  1002 Bolt         AH 50       <- spell 100 "Bolt": 2x Cloth, colours 1/10/15  (crafted bolt = 20 < 50)
  1003 Thread       vendor 5
  2001 Shirt        (vendor_sell 1) <- spell 101: 1 Bolt + 1 Thread, 1/20/40     (cost 25)
  2002 Pants                    <- spell 102: 3 Bolt + 1 Thread, 30/45/70    (cost 65)
  2003 Robe                     <- spell 103: 1 Cloth, 10/60/80, taught by item 3001 "Pattern: Robe"
  3001 Pattern: Robe AH 100 (patched per test)
"""
import json
import math
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ahtool import analytics, db, leveling  # noqa: E402

D = 2511
REALM = "PvP"
CFG = {"ah_cut": 0.05, "baseline_days": 14, "deal_threshold": 0.25}

ITEMS = {  # id: (name, vendor_sell)
    1001: ("Cloth", 1), 1002: ("Bolt", 5), 1003: ("Thread", 1),
    2001: ("Shirt", 1), 2002: ("Pants", 2), 2003: ("Robe", 3), 3001: ("Pattern: Robe", 1),
}
PRICES = {1001: (10, 1000), 1002: (50, 1000), 3001: (100, 20)}
RECIPES = [  # spell, created, min, low, high, reagents
    (100, 1002, 1, 10, 15, [(1001, 2)]),
    (101, 2001, 1, 20, 40, [(1002, 1), (1003, 1)]),
    (102, 2002, 30, 45, 70, [(1002, 3), (1003, 1)]),
    (103, 2003, 10, 60, 80, [(1001, 1)]),
    (104, 2003, 5, 0, 0, [(1001, 1)]),          # custom recipe without colour data -> ignored
]

TMP = tempfile.mkdtemp(prefix="lvltest_")
DBP = os.path.join(TMP, "lvl_fixture.db")


def build():
    conn = db.connect(DBP)
    with conn:
        for iid, (nm, vs) in ITEMS.items():
            conn.execute("INSERT INTO items(id,name,quality,vendor_sell) VALUES(?,?,1,?)", (iid, nm, vs))
        conn.execute("INSERT INTO vendor_items(item_id,price,source) VALUES(1003,5,'test')")
        for sid, created, mn, lo, hi, reag in RECIPES:
            conn.execute("INSERT INTO spells(id,name) VALUES(?,?)", (sid, "Spell %d" % sid))
            conn.execute("INSERT INTO recipes VALUES(?,?,?,?,?,?,?)", (sid, 8, created, 1, mn, lo, hi))
            for iid, cnt in reag:
                conn.execute("INSERT INTO recipe_reagents VALUES(?,?,?)", (sid, iid, cnt))
        conn.execute("INSERT INTO item_tooltip(id,icon,data) VALUES(3001,'x',?)",
                     (json.dumps({"id": 3001, "name": "Pattern: Robe", "teaches": 103}),))
        conn.execute("INSERT INTO ingests(ts,file_mtime,realm,items,changed) VALUES(1,1,?,1,1)", (REALM,))
    for iid, (p, avail) in PRICES.items():
        set_price(conn, iid, p, avail)
    return conn


def set_price(conn, iid, price, avail):
    with conn:
        conn.execute("INSERT OR REPLACE INTO price_current VALUES(?,?,?,?,?,1)", (REALM, iid, price, avail, D))
        for d in range(D - 9, D + 1):
            conn.execute("INSERT OR REPLACE INTO price_daily VALUES(?,?,?,?,?,?)", (REALM, iid, d, price, price, avail))
        conn.execute("INSERT INTO ingests(ts,file_mtime,realm,items,changed) VALUES(2,2,?,1,1)", (REALM,))
    analytics.invalidate()


def P(conn, **kw):
    kw.setdefault("recover", "none")
    return leveling.plan(conn, CFG, 8, **kw)


def check_contiguous(pl, start, end):
    cur = start
    for s in pl["steps"]:
        assert s["from"] == cur, (s["from"], cur)
        assert s["to"] > s["from"]
        assert s["crafts"] == math.ceil(s["crafts_expected"] - 1e-6), s
        cur = s["to"]
    assert cur == end, (cur, end)


# ---------------------------------------------------------------- tests

def test_chance_math():
    c = leveling.chance
    assert c(1, 1, 25, 50) == 1.0 and c(24, 1, 25, 50) == 1.0
    assert c(25, 1, 25, 50) == 1.0
    assert abs(c(37, 1, 25, 50) - 13 / 25) < 1e-12
    assert c(50, 1, 25, 50) == 0.0 and c(60, 1, 25, 50) == 0.0
    assert c(29, 30, 45, 70) == 0.0          # below min_skill: unusable
    assert not leveling.colour_ok(5, 0, 0) and not leveling.colour_ok(1, 320, 310)
    assert not leveling.colour_ok(1, 190, 230)   # AtlasLoot "min 1" placeholder
    assert leveling.colour_ok(1, 25, 50)


def test_single_recipe_expected_crafts(conn):
    # 40 -> 50: shirt is grey at 40, bolt grey at 15, robe needs its pattern -> only pants
    pl = P(conn, start=40, target=50)
    assert len(pl["steps"]) == 1, pl["steps"]
    st = pl["steps"][0]
    exp = 5 + 1 + 25 / 24 + 25 / 23 + 25 / 22 + 25 / 21
    assert st["spell_id"] == 102 and st["from"] == 40 and st["to"] == 50
    assert abs(st["crafts_expected"] - round(exp, 2)) < 1e-9, st["crafts_expected"]
    assert st["crafts"] == math.ceil(exp) == 11
    # make vs buy: bolt crafted from 2 cloth (20) instead of AH 50
    bolt = [r for r in st["reagents"] if r["item_id"] == 1002][0]
    assert bolt["source"] == "craft" and bolt["unit_price"] == 20 and bolt["craft_spell_id"] == 100
    assert st["cost_each"] == 65 and st["cost_total"] == 65 * 11
    assert pl["totals"]["cost"] == 715 and pl["totals"]["net"] == 715
    sub = {s["spell_id"]: s for s in pl["subcrafts"]}
    assert sub[100]["crafts"] == 33, sub
    shop = {s["item_id"]: s for s in pl["shopping"]}
    assert shop[1001]["need"] == 66 and shop[1003]["need"] == 11 and shop[1003]["source"] == "vendor"
    assert 1002 not in shop                       # intermediate is in subcrafts, not bought
    assert leveling.plan_to_shopping(pl) == [(102, 11)]
    assert leveling.plan_to_shopping(pl, include_subcrafts=True) == [(102, 11), (100, 33)]
    assert pl["unreachable_from"] is None


def test_buy_when_cheaper(conn):
    set_price(conn, 1002, 15, 1000)
    try:
        pl = P(conn, start=40, target=50)
        bolt = [r for r in pl["steps"][0]["reagents"] if r["item_id"] == 1002][0]
        assert bolt["source"] == "ah" and bolt["unit_price"] == 15
        assert pl["steps"][0]["cost_each"] == 50
        assert not pl["subcrafts"]
    finally:
        set_price(conn, 1002, 50, 1000)


def test_cooldown_recipe_never_used(conn):
    # A cooldown craft must neither level nor make a reagent cheap (Salt Shaker -> Refined Deeprock Salt).
    assert 19566 in leveling.COOLDOWN_SPELLS and 18560 in leveling.COOLDOWN_SPELLS
    leveling.COOLDOWN_SPELLS[100] = 72  # pretend the bolt recipe is on a cooldown
    try:
        pl = P(conn, start=1, target=50)
        assert all(s["spell_id"] != 100 for s in pl["steps"]), pl["steps"]
        assert not pl["subcrafts"], pl["subcrafts"]
        bolts = [r for s in pl["steps"] for r in s["reagents"] if r["item_id"] == 1002]
        assert bolts and all(r["source"] == "ah" and r["unit_price"] == 50 for r in bolts), bolts
        assert any("on a cooldown were not used" in w for w in pl["warnings"]), pl["warnings"]
        assert pl["cooldowns"] is False
        # "Include cooldown crafts": the bolt is crafted again, marked, and the plan says how long it takes
        pl = P(conn, start=40, target=50, cooldowns=True)
        bolt = [r for r in pl["steps"][0]["reagents"] if r["item_id"] == 1002][0]
        assert bolt["source"] == "craft" and bolt["craft_spell_id"] == 100, bolt
        sub = {x["spell_id"]: x for x in pl["subcrafts"]}
        assert sub[100]["cooldown_h"] == 72 and pl["steps"][0]["cooldown_h"] is None, (sub, pl["steps"][0])
        w = [w for w in pl["warnings"] if "cooldown take about" in w]
        assert w and "33 crafts on a 3 days cooldown take about 96 days" in w[0], pl["warnings"]
        assert not any("were not used" in x for x in pl["warnings"])
        pl = P(conn, start=1, target=50, cooldowns="1")
        assert any(s["spell_id"] == 100 and s["cooldown_h"] == 72 for s in pl["steps"]), pl["steps"]
    finally:
        del leveling.COOLDOWN_SPELLS[100]
    pl = P(conn, start=40, target=50)  # back to normal: the bolt is crafted again
    assert [r for r in pl["steps"][0]["reagents"] if r["item_id"] == 1002][0]["source"] == "craft"


def test_merging_reuse_and_determinism(conn):
    pl = P(conn, start=1, target=40)
    check_contiguous(pl, 1, 40)
    ids = [s["spell_id"] for s in pl["steps"]]
    assert all(a != b for a, b in zip(ids, ids[1:])), ids    # merged: no two consecutive equal steps
    assert ids[0] == 100                                      # bolts (20) beat shirts (25) while orange
    # bolts made for skill-ups are reused by later shirts / pants
    assert pl["steps"][0]["kept"] > 0
    reused = sum(r.get("from_earlier_steps", 0) for s in pl["steps"][1:] for r in s["reagents"])
    assert reused == pl["steps"][0]["kept"]
    assert abs(sum(s["net_total"] for s in pl["steps"]) - pl["totals"]["net"]) < 0.05
    pl2 = P(conn, start=1, target=40)
    assert json.dumps(pl, sort_keys=True) == json.dumps(pl2, sort_keys=True)


def test_recipe_fixed_cost(conn):
    # trainer only: robe is never used
    pl = P(conn, start=10, target=40)
    assert 103 not in [s["spell_id"] for s in pl["steps"]] and not pl["recipes_to_buy"]
    base_net = pl["totals"]["net"]
    # cheap pattern (100): saves 15+/point over 30 points -> bought once, counted once
    pl = P(conn, start=10, target=40, sources="all")
    assert [r["spell_id"] for r in pl["recipes_to_buy"]] == [103], pl["recipes_to_buy"]
    assert pl["totals"]["recipes"] == 100
    assert pl["totals"]["net"] < base_net
    robe = [s for s in pl["steps"] if s["spell_id"] == 103]
    assert robe and robe[0]["recipe_item"] == 3001
    assert abs(pl["totals"]["cost"] - (sum(s["cost_total"] for s in pl["steps"]) + 100)) < 0.05
    # expensive pattern: not worth it
    set_price(conn, 3001, 1_000_000, 20)
    try:
        pl = P(conn, start=10, target=40, sources="all")
        assert not pl["recipes_to_buy"] and 103 not in [s["spell_id"] for s in pl["steps"]]
    finally:
        set_price(conn, 3001, 100, 20)
    # known pattern is free even with sources=trainer
    with conn:
        conn.execute("INSERT INTO known_recipes VALUES(103,1)")
    try:
        pl = P(conn, start=10, target=40, known_only=True)
        assert 103 in [s["spell_id"] for s in pl["steps"]] and not pl["recipes_to_buy"]
    finally:
        with conn:
            conn.execute("DELETE FROM known_recipes")


def test_unreachable_gap(conn):
    pl = P(conn, start=1, target=100)
    assert pl["unreachable_from"] == 70, pl["unreachable_from"]
    check_contiguous(pl, 1, 70)
    assert any("70" in w and "recipe drops" in w for w in pl["warnings"]), pl["warnings"]
    # buying the robe pattern extends the reachable range (gap coverage wins even when it costs more)
    pl = P(conn, start=1, target=100, sources="all")
    assert pl["unreachable_from"] == 80 and [r["spell_id"] for r in pl["recipes_to_buy"]] == [103]
    assert any("without skill colour" in w for w in pl["warnings"])


def test_edge_cases(conn):
    pl = P(conn, start=200, target=150)
    assert pl["steps"] == [] and pl["warnings"] and pl["totals"]["net"] == 0
    for bad in (99, 5, "x"):
        try:
            leveling.plan(conn, CFG, bad)
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for %r" % bad)
    for kw in ({"mode": "x"}, {"recover": "x"}, {"sources": "x"}, {"target": 999}):
        try:
            P(conn, **kw)
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for %r" % kw)


def test_market_depth_and_recovery(conn):
    set_price(conn, 1001, 10, 3)
    try:
        pl = P(conn, start=40, target=50)
        assert any("Cloth" in w and "listed" in w for w in pl["warnings"]), pl["warnings"]
    finally:
        set_price(conn, 1001, 10, 1000)
    pl = P(conn, start=40, target=50, recover="vendor")
    st = pl["steps"][0]
    assert st["recover_each"] == 2 and st["net_each"] == 63 and st["recover_source"] == "vendor"
    # Pants have no AH price -> "sell" falls back to vendor_sell
    pl = P(conn, start=40, target=50, recover="sell")
    assert pl["steps"][0]["recover_each"] == 2


def test_sell_depth_cap(conn):
    # Shirt sells for 1000 on the AH with 10 listed (7-day avg) -> cap = ceil(10 * 0.5) = 5 AH units
    set_price(conn, 2001, 1000, 10)
    try:
        pl = P(conn, start=20, target=40, recover="sell")
        shirt = [s for s in pl["steps"] if s["spell_id"] == 101]
        assert shirt, pl["steps"]
        ah = sum(s["ah_units"] for s in shirt)
        vend = sum(s["vendor_units"] for s in shirt)
        assert ah == 5, (ah, vend)
        tot_rec = sum(s["recover_total"] for s in shirt)
        assert abs(tot_rec - (5 * 1000 * 0.95 + vend * 1)) < 0.05, tot_rec
        assert any("Market depth" in w and "Shirt" in w for w in pl["warnings"]), pl["warnings"]
        # uncapped (huge depth factor): every shirt sells on the AH, and the plan looks far more profitable
        cfg = dict(CFG, leveling_sell_depth=1000)
        pl2 = leveling.plan(conn, cfg, 8, start=20, target=40, recover="sell")
        assert all(s["vendor_units"] == 0 for s in pl2["steps"] if s["spell_id"] == 101)
        assert pl2["totals"]["net"] < pl["totals"]["net"]
        # selection accounts for the cap: with the cap the plan should not over-produce shirts beyond what the
        # uncapped plan does, and the result is deterministic
        assert json.dumps(pl, sort_keys=True) == json.dumps(P(conn, start=20, target=40, recover="sell"),
                                                            sort_keys=True)
    finally:
        with conn:
            conn.execute("DELETE FROM price_current WHERE item_id=2001")
            conn.execute("DELETE FROM price_daily WHERE item_id=2001")
        analytics.invalidate()


def test_milestones(conn):
    pl = P(conn, start=1, target=100)
    skills = [m["skill"] for m in pl["milestones"]]
    assert 1 in skills and 50 in skills and 75 in skills and 125 not in skills


def main():
    conn = build()
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    t0 = time.perf_counter()
    try:
        for name, fn in tests:
            try:
                fn() if fn.__code__.co_argcount == 0 else fn(conn)
                print("ok   ", name)
            except Exception as e:  # noqa: BLE001
                failed += 1
                import traceback
                traceback.print_exc()
                print("FAIL ", name, e)
    finally:
        conn.close()
        analytics.invalidate()
        shutil.rmtree(TMP, ignore_errors=True)
    print("%d/%d passed (%.0f ms)" % (len(tests) - failed, len(tests), (time.perf_counter() - t0) * 1000))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
