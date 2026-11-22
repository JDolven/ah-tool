"""Creates data/test.db with the contract schema and fake data for API tests.

Hand-verifiable numbers (realm PvP, latest day D = 2511):
  2589 Linen Cloth        price 100, baseline 100 (lows alternate 90/110)
  2996 Bolt of Linen      price 300, baseline 300   <- spell 2963: 2x Linen Cloth
  2320 Coarse Thread      AH 25, vendor 10
  2576 Brown Linen Vest   price 1500 baseline 1500  <- spell 2385: 3x Bolt + 1x Thread
  4306 Silk Cloth         price 500, vendor_sell 800 -> under_vendor, profit 300
  2453 Bruiseweed         price 600, baseline 1000 -> discount 0.4 (below_baseline, cheap_reagent)
  3371 Empty Vial         vendor 50 (no AH price)
  118  Minor Healing Pot  price 1500 baseline 1500 <- spell 2329: 1x Bruiseweed + 1x Vial
  spell 7418 enchant (no created item): 2x Linen Cloth
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = 2511

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY, name TEXT, quality INTEGER, item_level INTEGER, class_id INTEGER, subclass_id INTEGER, icon INTEGER, vendor_sell INTEGER, vendor_buy INTEGER, stack INTEGER);
CREATE TABLE IF NOT EXISTS spells(id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS recipes(spell_id INTEGER PRIMARY KEY, profession_id INTEGER, created_item_id INTEGER, num_created REAL, min_skill INTEGER, low_skill INTEGER, high_skill INTEGER);
CREATE TABLE IF NOT EXISTS recipe_reagents(spell_id INTEGER, item_id INTEGER, count INTEGER, PRIMARY KEY(spell_id, item_id));
CREATE TABLE IF NOT EXISTS vendor_items(item_id INTEGER PRIMARY KEY, price INTEGER, source TEXT);
CREATE TABLE IF NOT EXISTS price_daily(realm TEXT, item_id INTEGER, day INTEGER, low INTEGER, high INTEGER, available INTEGER, PRIMARY KEY(realm, item_id, day));
CREATE TABLE IF NOT EXISTS price_current(realm TEXT, item_id INTEGER, price INTEGER, available INTEGER, last_day INTEGER, updated_at INTEGER, PRIMARY KEY(realm, item_id));
CREATE TABLE IF NOT EXISTS snapshots(realm TEXT, item_id INTEGER, ts INTEGER, price INTEGER, available INTEGER);
CREATE TABLE IF NOT EXISTS ingests(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, file_mtime INTEGER, realm TEXT, items INTEGER, changed INTEGER);
CREATE TABLE IF NOT EXISTS postings(item_id INTEGER, ts INTEGER, price INTEGER, quantity INTEGER, PRIMARY KEY(item_id, ts, price, quantity));
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS my_professions(profession_id INTEGER PRIMARY KEY, skill INTEGER NOT NULL DEFAULT 300);
CREATE TABLE IF NOT EXISTS known_recipes(spell_id INTEGER PRIMARY KEY, known INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS shopping_list(spell_id INTEGER PRIMARY KEY, qty INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS watchlist(item_id INTEGER PRIMARY KEY, max_price INTEGER, enabled INTEGER NOT NULL DEFAULT 1, note TEXT, created_at INTEGER, last_alert_price INTEGER, last_alert_ts INTEGER);
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, item_id INTEGER, kind TEXT, price INTEGER, threshold INTEGER, message TEXT, seen INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS item_tooltip(id INTEGER PRIMARY KEY, icon TEXT, data TEXT);
"""

# id: (name, quality, ilvl, class, subclass, vendor_sell, vendor_buy)
ITEMS = {
    2589: ("Linen Cloth", 1, 5, 7, 5, 13, 0),
    2996: ("Bolt of Linen Cloth", 1, 5, 7, 5, 50, 0),
    2320: ("Coarse Thread", 1, 5, 7, 0, 1, 10),
    2576: ("Brown Linen Vest", 1, 10, 4, 1, 60, 0),
    4306: ("Silk Cloth", 1, 20, 7, 5, 800, 0),
    2453: ("Bruiseweed", 1, 10, 7, 9, 100, 0),
    3371: ("Empty Vial", 1, 1, 7, 0, 2, 50),
    118: ("Minor Healing Potion", 1, 5, 0, 1, 10, 0),
    1206: ("Moss Agate", 2, 15, 7, 10, 500, 0),
    19019: ("Thunderfury", 5, 80, 2, 7, 1000, 0),
}

# id: (price, available, last_day, hist_fn)
def alt(a, b):
    return lambda d, i: a if i % 2 == 0 else b

def const(v):
    return lambda d, i: v

PRICES = {
    2589: (100, 5000, D, alt(90, 110)),
    2996: (300, 200, D, const(300)),
    2320: (25, 40, D, const(25)),
    2576: (1500, 3, D, const(1500)),
    4306: (500, 30, D, const(500)),
    2453: (600, 120, D, None),  # special
    118: (1500, 25, D, const(1500)),
    1206: (700, 10, D - 5, const(700)),   # stale (last_day D-5)
    19019: (5000000, 1, D, const(5000000)),
    284712: (900, 2, D, const(900)),      # custom item without name
}

RECIPES = [
    # spell, prof, created, num, min, low, high
    (2963, 8, 2996, 1, 25, 45, 65),
    (2385, 8, 2576, 1, 40, 60, 80),
    (2329, 4, 118, 1, 1, 55, 75),
    (7418, 10, None, 1, 1, 70, 90),
]
REAGENTS = {
    2963: [(2589, 2)],
    2385: [(2996, 3), (2320, 1)],
    2329: [(2453, 1), (3371, 1)],
    7418: [(2589, 2)],
}
SPELLS = {2963: "Bolt of Linen Cloth", 2385: "Brown Linen Vest", 2329: "Minor Healing Potion",
          7418: "Enchant Bracer - Minor Health"}


TOOLTIP_19019 = ('{"id":19019,"name":"Thunderfury","quality":5,"item_level":80,"required_level":60,"class_id":2,'
                 '"subclass_id":7,"source":["csv","forever"],"bind":"Binds when picked up","unique":true,'
                 '"slot":"One-Hand","type":"Sword","damage":{"min":44,"max":115,"school":"","speed":1.9,"dps":53.9,'
                 '"extra":[{"min":16,"max":30,"school":"Nature"}]},"stats":["+5 Agility","+8 Stamina"],'
                 '"resistances":["+8 Fire Resistance"],"durability":125,"effects":[{"trigger":"Chance on hit",'
                 '"text":"Blasts your enemy with lightning."}],"description":"Flavour.","sell_price":255355}')


def add_extras(conn):
    """v2 test extras (applied by test_api at the end so v1 numbers stay untouched): AH-priced reagents whose
    names contain a double quote / caret (must land in `unexportable`) and a recipe using them + Linen Cloth."""
    conn.execute("INSERT OR REPLACE INTO items VALUES(7001,'Odd \"Quote\" Herb',1,5,7,9,1,10,0,20)")
    conn.execute("INSERT OR REPLACE INTO items VALUES(7002,'Caret^Dust',1,5,7,9,1,10,0,20)")
    for iid in (7001, 7002):
        conn.execute("INSERT OR REPLACE INTO price_current VALUES('PvP',?,?,?,?,?)", (iid, 200, 10, D, 1790000000))
        for k in range(30):
            conn.execute("INSERT OR REPLACE INTO price_daily VALUES('PvP',?,?,?,?,?)", (iid, D - 29 + k, 200, 210, 10))
    conn.execute("INSERT OR REPLACE INTO spells VALUES(9001,'Odd Brew')")
    conn.execute("INSERT OR REPLACE INTO recipes VALUES(9001,4,NULL,1,10,20,30)")
    conn.execute("INSERT OR REPLACE INTO recipe_reagents VALUES(9001,7001,2)")
    conn.execute("INSERT OR REPLACE INTO recipe_reagents VALUES(9001,7002,1)")
    conn.execute("INSERT OR REPLACE INTO recipe_reagents VALUES(9001,2589,1)")
    conn.commit()


V4_ITEMS = {
    8001: "Spike Ore", 8002: "Thin Gem", 8003: "Cheap Rune", 8004: "Capped Charm", 8005: "Shallow Dust",
    8006: "Newcomer Leaf", 8007: "Trend Elixir", 8008: "Trend Herb", 8009: "Falling Potion", 8010: "Lowconf Cheap",
}


def add_v4(conn):
    """v4 extras (applied by test_api at the end so earlier numbers stay untouched). Latest day D.
      8001 Spike Ore     100 all 30 days except day D-5 = 5000 (Hampel outlier), avail 50  -> high confidence
      8002 Thin Gem      1000 x30 avail 10, current 5000 with avail 1 -> thin_high, confidence low
      8003 Cheap Rune    1000 x30 avail 10, current 300 avail 1 -> thin_low
      8004 Capped Charm  1000 x29 + current 2000 avail 20 (not thin) -> sell_price capped at 1250
      8005 Shallow Dust  10 days, avail 3 -> medium;  8006 Newcomer Leaf 2 days -> low
      8007 Trend Elixir  price 500+10k (k=0..29), 8009 Falling Potion 1500-20k, 8008 Trend Herb 100 flat
      8010 Lowconf Cheap 5 days of 1000, current 400 avail 1 -> thin_low + low confidence (deal hidden by default)
      spells: 9101 Thin Gem <- 2x Spike Ore; 9102 Capped Charm <- 1x Spike Ore; 9103 Trend Elixir <- Herb;
              9104 Falling Potion <- Herb"""
    ins_item = "INSERT OR REPLACE INTO items VALUES(?,?,1,5,7,9,1,1,0,20)"
    for iid, n in V4_ITEMS.items():
        conn.execute(ins_item, (iid, n))

    def daily(iid, price_fn, avail, days=30):
        for k in range(days):
            day = D - days + 1 + k
            v = price_fn(day, k)
            conn.execute("INSERT OR REPLACE INTO price_daily VALUES('PvP',?,?,?,?,?)", (iid, day, v, v + 5, avail))

    def cur(iid, price, avail, last_day=D):
        conn.execute("INSERT OR REPLACE INTO price_current VALUES('PvP',?,?,?,?,?)", (iid, price, avail, last_day, 1790000000))

    daily(8001, lambda d, k: 5000 if d == D - 5 else 100, 50); cur(8001, 100, 50)
    daily(8002, lambda d, k: 1000, 10); cur(8002, 5000, 1)
    daily(8003, lambda d, k: 1000, 10); cur(8003, 300, 1)
    daily(8004, lambda d, k: 2000 if d == D else 1000, 20); cur(8004, 2000, 20)
    daily(8005, lambda d, k: 200, 3, days=10); cur(8005, 200, 3)
    daily(8006, lambda d, k: 200, 20, days=2); cur(8006, 200, 20)
    daily(8007, lambda d, k: 500 + 10 * k, 30); cur(8007, 790, 30)
    daily(8008, lambda d, k: 100, 100); cur(8008, 100, 100)
    daily(8009, lambda d, k: 1500 - 20 * k, 30); cur(8009, 920, 30)
    daily(8010, lambda d, k: 400 if d == D else 1000, 1, days=5); cur(8010, 400, 1)
    for sid, name, prof, created, mn in ((9101, "Thin Gem", 4, 8002, 1), (9102, "Capped Charm", 4, 8004, 1),
                                         (9103, "Trend Elixir", 4, 8007, 1), (9104, "Falling Potion", 4, 8009, 1)):
        conn.execute("INSERT OR REPLACE INTO spells VALUES(?,?)", (sid, name))
        conn.execute("INSERT OR REPLACE INTO recipes VALUES(?,?,?,1,?,?,?)", (sid, prof, created, mn, 50, 70))
    for sid, iid, cnt in ((9101, 8001, 2), (9102, 8001, 1), (9103, 8008, 1), (9104, 8008, 1)):
        conn.execute("INSERT OR REPLACE INTO recipe_reagents VALUES(?,?,?)", (sid, iid, cnt))
    conn.commit()


def build(path):
    if os.path.exists(path):
        os.remove(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    for i, (n, q, il, cl, sc, vs, vb) in ITEMS.items():
        c.execute("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?)", (i, n, q, il, cl, sc, 1, vs, vb, 20))
    for s, n in SPELLS.items():
        c.execute("INSERT INTO spells VALUES(?,?)", (s, n))
    for r in RECIPES:
        c.execute("INSERT INTO recipes VALUES(?,?,?,?,?,?,?)", r)
    for s, lst in REAGENTS.items():
        for it, cnt in lst:
            c.execute("INSERT INTO recipe_reagents VALUES(?,?,?)", (s, it, cnt))
    c.execute("INSERT INTO item_tooltip VALUES(19019,'inv_sword_39',?)", (TOOLTIP_19019,))
    c.execute("INSERT INTO vendor_items VALUES(2320,10,'vendor')")
    c.execute("INSERT INTO vendor_items VALUES(3371,50,'vendor')")
    for it, (price, avail, last_day, fn) in PRICES.items():
        for k in range(30):
            day = last_day - 29 + k
            if it == 2453:
                low = 600 if day == last_day else 1000
                high = 1100 if day != last_day else 600
            else:
                low = fn(day, day)
                high = low + 10
            c.execute("INSERT INTO price_daily VALUES('PvP',?,?,?,?,?)", (it, day, low, high, avail))
        c.execute("INSERT INTO price_current VALUES('PvP',?,?,?,?,?)", (it, price, avail, last_day, 1790000000))
    for k, p in enumerate([1100, 1000, 800, 600]):
        c.execute("INSERT INTO snapshots VALUES('PvP',2453,?,?,?)", (1789000000 + k * 1000, p, 100 + k))
    c.execute("INSERT INTO ingests(ts,file_mtime,realm,items,changed) VALUES(1800000000,1799999000,'PvP',10,10)")
    c.execute("INSERT INTO postings VALUES(2453,1799990000,900,5)")
    c.execute("INSERT INTO postings VALUES(118,1799990100,1800,2)")
    c.execute("INSERT INTO postings VALUES(999999,1799990200,50,1)")
    c.commit()
    c.close()


if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "data", "test.db")
    build(p)
    print("wrote", p)
