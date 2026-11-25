"""Static data: items/spells (wago.tools CSV), recipes (AtlasLoot), vendor items, custom item names (client DBCache).

Units: items.vendor_sell = SellPrice (per unit, copper); items.vendor_buy = BuyPrice / VendorStackCount
(per unit, copper); items.stack = Stackable (max stack size).
"""
import csv
import math
import os
import re
import struct
import sys
import urllib.request

from . import luaparse

csv.field_size_limit(10 ** 8)

# Well-known Classic vendor-sold reagents (price taken from ItemSparse BuyPrice / VendorStackCount).
BUILTIN_VENDOR_IDS = [
    3371, 3372, 8925, 18256,              # vials
    2320, 2321, 4291, 8343, 14341,        # threads
    2604, 2605, 4340, 4341, 4342, 6260, 2325,  # dyes (red, green, gray, yellow, purple, blue, black)
    4289,                                 # salt
    2880, 3466,                           # weak / strong flux
    2678, 2692, 3713,                     # mild / hot / soothing spices
    159, 1179, 1205, 1708, 1645,          # water, milk, melon juice, sweet nectar, moonberry juice
    4470, 4399, 4400,                     # simple wood, wooden / heavy stock
    10647,                                # engineer's ink
    5140,                                 # flash powder
    17020, 17021, 17026, 17030, 17031, 17032, 17033, 21177,  # class reagents
    5565, 16583,                          # infernal stone, demonic figurine
]


def log(*a):
    print("[staticdata]", *a, file=sys.stderr)


MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024  # the largest DB2 CSV (ItemSparse) is far below this


def _download(cfg, table):
    cache = cfg["cache_dir"]
    os.makedirs(cache, exist_ok=True)
    dest = os.path.join(cache, "%s_%s.csv" % (table, cfg["wago_build"]))
    legacy = os.path.join(cache, "%s.csv" % table)
    if os.path.exists(dest) and os.path.getsize(dest) > 100:
        return dest
    if os.path.exists(legacy) and os.path.getsize(legacy) > 1000:
        os.replace(legacy, dest)
        return dest
    url = "https://wago.tools/db2/%s/csv?build=%s" % (table, cfg["wago_build"])
    log("downloading", url)
    req = urllib.request.Request(url, headers={"User-Agent": "ahtool/1.0"})
    tmp = dest + ".part"
    total = 0
    try:
        with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise IOError("%s: download larger than %d bytes, aborted" % (table, MAX_DOWNLOAD_BYTES))
                f.write(chunk)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, dest)
    return dest


def _int(s, default=0):
    try:
        return int(s)
    except (TypeError, ValueError, OverflowError):
        try:
            return int(float(s))
        except (TypeError, ValueError, OverflowError):
            return default


def read_dbcache_items(path):
    """Parse the WoW client hotfix cache (DBCache.bin) -> {item_id: (name, quality, item_level)}.
    Uses the ItemSearchName records (tableHash 0x198E28B5): name cstring, then quality byte, ilvl byte at +25.
    Returns {} if file missing/unreadable."""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "rb") as f:
            d = f.read()
        out = {}
        p = 44
        n = len(d)
        while p + 32 <= n:
            if d[p:p + 4] != b"XFTH":
                break
            th, rid, size, st = struct.unpack_from("<IIIB", d, p + 16)
            if th == 0x198E28B5 and st == 1 and size > 0:
                rec = d[p + 32:p + 32 + size]
                e = rec.find(b"\0")
                if e > 0:
                    name = rec[:e].decode("utf-8", "replace")
                    q = rec[e + 1] if e + 1 < len(rec) else None
                    lvl = rec[e + 25] if e + 25 < len(rec) else None
                    out[rid] = (name, q, lvl)
            p += 32 + size
        return out
    except Exception as ex:  # best effort
        log("DBCache read failed:", ex)
        return {}


def build_items(conn, cfg):
    sparse = _download(cfg, "ItemSparse")
    itemp = _download(cfg, "Item")
    cls = {}
    with open(itemp, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            cls[_int(r["ID"])] = (_int(r.get("ClassID")), _int(r.get("SubclassID")), _int(r.get("IconFileDataID")))
    rows = {}
    with open(sparse, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            iid = _int(r["ID"])
            c = cls.get(iid, (None, None, None))
            stackcnt = max(_int(r.get("VendorStackCount"), 1), 1)
            rows[iid] = (iid, r.get("Display_lang") or None, _int(r.get("OverallQualityID")),
                         _int(r.get("ItemLevel")), c[0], c[1], c[2], _int(r.get("SellPrice")),
                         _int(r.get("BuyPrice")) // stackcnt, _int(r.get("Stackable"), 1))
    extra = 0
    for iid, (name, q, lvl) in read_dbcache_items(cfg.get("dbcache_path")).items():
        if iid not in rows:  # custom / hotfix-only items
            rows[iid] = (iid, name, q, lvl, None, None, None, None, None, None)
            extra += 1
    conn.execute("DELETE FROM items")
    conn.executemany("INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?)", rows.values())
    conn.commit()
    log("items:", len(rows), "(from client cache only: %d)" % extra)


def build_spells(conn, cfg):
    p = _download(cfg, "SpellName")
    with open(p, newline="", encoding="utf-8") as f:
        rows = [(_int(r["ID"]), r.get("Name_lang") or None) for r in csv.DictReader(f)]
    conn.execute("DELETE FROM spells")
    conn.executemany("INSERT OR REPLACE INTO spells VALUES(?,?)", rows)
    conn.commit()
    log("spells:", len(rows))


_REC = re.compile(r"\[(\d+)\]\s*=\s*\{\s*(nil|\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,"
                  r"\s*\{([^}]*)\}\s*,\s*\{([^}]*)\}\s*(?:,\s*(\d+(?:\.\d+)?))?")


def build_recipes(conn, cfg):
    path = os.path.join(cfg["addons_dir"], "AtlasLootClassic", "Data", "Profession.lua")
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    a = text.index("PROFESSION_DATA.CLASSIC = {")
    b = text.find("PROFESSION_DATA.BCC", a)
    section = text[a:b if b > 0 else len(text)]
    recipes = {}
    reagents = {}
    for line in section.splitlines():
        line = line.split("--", 1)[0]
        m = _REC.search(line)
        if not m:
            continue
        sid = int(m.group(1))
        created = None if m.group(2) == "nil" else int(m.group(2))
        ids = [int(x) for x in re.findall(r"\d+", m.group(7))]
        cnts = [int(x) for x in re.findall(r"\d+", m.group(8))]
        num = float(m.group(9)) if m.group(9) else 1.0
        recipes[sid] = (sid, int(m.group(3)), created, num, int(m.group(4)), int(m.group(5)), int(m.group(6)))
        d = {}
        for i, iid in enumerate(ids):
            d[iid] = d.get(iid, 0) + (cnts[i] if i < len(cnts) else 1)
        reagents[sid] = d
    conn.execute("DELETE FROM recipes")
    conn.execute("DELETE FROM recipe_reagents")
    conn.executemany("INSERT INTO recipes VALUES(?,?,?,?,?,?,?)", recipes.values())
    conn.executemany("INSERT INTO recipe_reagents VALUES(?,?,?)",
                     [(s, i, c) for s, d in reagents.items() for i, c in d.items()])
    conn.commit()
    log("recipes:", len(recipes), "reagent rows:", sum(len(d) for d in reagents.values()))


def build_vendor_builtin(conn):
    """Builtin vendor list priced from items.vendor_buy (per unit). Never overrides auctionator_cache rows."""
    conn.execute("DELETE FROM vendor_items WHERE source='builtin'")
    n = 0
    for iid in BUILTIN_VENDOR_IDS:
        r = conn.execute("SELECT vendor_buy FROM items WHERE id=?", (iid,)).fetchone()
        if r and r[0] and r[0] > 0:
            conn.execute("INSERT OR IGNORE INTO vendor_items VALUES(?,?,'builtin')", (iid, r[0]))
            n += 1
    conn.commit()
    log("builtin vendor items:", n)


def update_vendor_from_cache(conn, vendor_cache):
    """vendor_cache: parsed AUCTIONATOR_VENDOR_PRICE_CACHE ({"itemid": copper per unit}). Returns row count."""
    rows = []
    if isinstance(vendor_cache, dict):
        for k, v in vendor_cache.items():
            try:
                iid = int(k)
            except (TypeError, ValueError, OverflowError):
                continue
            # bool is an int subclass; inf/NaN/huge values would overflow int() or SQLite INTEGER
            if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and 0 < v < 2 ** 62 \
                    and 0 < iid < 2 ** 62:
                rows.append((iid, int(v), "auctionator_cache"))
    conn.executemany("INSERT OR REPLACE INTO vendor_items VALUES(?,?,?)", rows)
    return len(rows)


def build_tooltips(conn, cfg):
    """Item tooltip data (stats, effects, icons; Forever overlays) -> item_tooltip. See ahtool/tooltipdata.py."""
    from . import tooltipdata
    res = tooltipdata.build(conn, cfg, lambda table: _download(cfg, table))
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('dbcache_sig', ?)", (dbcache_sig(cfg),))
    conn.commit()
    return res


def dbcache_sig(cfg):
    """Size and mtime of the client's hotfix cache. It grows whenever the game loads an item the Forever server
    changed (e.g. a lower vendor sell price), so the tooltips - and items.vendor_sell - are rebuilt when it changes."""
    p = (cfg or {}).get("dbcache_path") or ""
    try:
        st = os.stat(p)
    except OSError:
        return ""
    return "%d:%d" % (st.st_size, int(st.st_mtime))


def tooltips_current(conn, cfg=None):
    """True when item_tooltip is built by this version and - given cfg - from the hotfix cache as it is now."""
    from . import tooltipdata
    r = conn.execute("SELECT value FROM meta WHERE key='tooltip_version'").fetchone()
    if not (r and r[0] == tooltipdata.VERSION and _count(conn, "item_tooltip")):
        return False
    if cfg is None:
        return True
    s = conn.execute("SELECT value FROM meta WHERE key='dbcache_sig'").fetchone()
    return (s[0] if s else "") == dbcache_sig(cfg)


def _count(conn, table):
    return conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]


def ensure(conn, cfg, force=False):
    """Build items/spells/recipes/vendor_items if empty (or force). Download failures are logged, not fatal."""
    steps = [("items", lambda: build_items(conn, cfg)),
             ("spells", lambda: build_spells(conn, cfg)),
             ("recipes", lambda: build_recipes(conn, cfg))]
    rebuilt_items = False
    for table, fn in steps:
        if force or _count(conn, table) == 0:
            try:
                fn()
                rebuilt_items = rebuilt_items or table == "items"
            except Exception as ex:
                log("%s build failed: %r" % (table, ex))
    if rebuilt_items:  # custom items named from AH scans (logger) are not in the static data
        from . import market
        market.apply_names(conn)
        conn.commit()
    # Tooltips also patch items.quality/item_level (Forever overrides), so rebuild them after an items rebuild.
    if force or rebuilt_items or not tooltips_current(conn, cfg):
        try:
            build_tooltips(conn, cfg)
        except Exception as ex:
            log("tooltip build failed: %r" % (ex,))
    # Vendor sell prices the game reported (AH Tool Logger) win over the client tables and the hotfix cache.
    from . import market
    if market.apply_sell(conn):
        conn.commit()
    n_builtin = conn.execute("SELECT COUNT(*) FROM vendor_items WHERE source='builtin'").fetchone()[0]
    if force or n_builtin == 0:
        build_vendor_builtin(conn)
    if not _count(conn, "vendor_items") or force:
        sv = cfg.get("savedvariables")
        if sv and os.path.exists(sv):
            try:
                data = luaparse.parse_file(sv)
                update_vendor_from_cache(conn, data.get("AUCTIONATOR_VENDOR_PRICE_CACHE"))
                conn.commit()
            except Exception as ex:
                log("vendor cache read failed: %r" % ex)
