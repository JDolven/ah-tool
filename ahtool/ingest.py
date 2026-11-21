"""Auctionator.lua -> SQLite (merge/upsert; history is never deleted)."""
import datetime
import os
import re
import sys
import threading
import time

from . import cbor, config, luaparse, staticdata
from . import db as dbmod

EPOCH = datetime.date(2020, 1, 1)
_INT_RE = re.compile(r"\d+")


def day_to_date(day):
    return EPOCH + datetime.timedelta(days=int(day))


def _decode_blob(v):
    """CBOR bytes -> python, plain Lua table -> itself."""
    if isinstance(v, (bytes, bytearray)):
        return cbor.loads(v)
    return v


def _dmap(x):
    """{day: int} from dict (str/int keys); [] / {} / None -> {}."""
    if not isinstance(x, dict):
        return {}
    out = {}
    for k, v in x.items():
        try:
            out[int(k)] = int(v)
        except (TypeError, ValueError):
            pass
    return out


def _item_id(key):
    if isinstance(key, int):
        return key
    if not isinstance(key, str):
        return None
    if key.isdigit():
        return int(key)
    m = _INT_RE.search(key)
    return int(m.group()) if m and key != "version" else None


def _realm_items(realm_val):
    """-> {item_id: {m,h,l,a}} (normalized)."""
    data = _decode_blob(realm_val)
    if not isinstance(data, dict):
        return {}
    items = {}
    pure = set()
    for k, v in data.items():
        iid = _item_id(k)
        if iid is None:
            continue
        is_pure = isinstance(k, int) or (isinstance(k, str) and k.isdigit())
        if iid in items and iid in pure and not is_pure:
            continue
        try:
            v = _decode_blob(v)
        except cbor.CBORError:
            continue
        if not isinstance(v, dict):
            continue
        items[iid] = v
        if is_pure:
            pure.add(iid)
    return items


MERGE_DAILY = ("low=MIN(COALESCE(price_daily.low,excluded.low), COALESCE(excluded.low,price_daily.low)), "
               "high=MAX(COALESCE(price_daily.high,excluded.high), COALESCE(excluded.high,price_daily.high)), "
               "available=MAX(COALESCE(price_daily.available,excluded.available), "
               "COALESCE(excluded.available,price_daily.available)) "
               "WHERE excluded.low < price_daily.low OR excluded.high > price_daily.high "
               "OR excluded.available > price_daily.available OR price_daily.low IS NULL "
               "OR price_daily.high IS NULL OR price_daily.available IS NULL")

_lock = threading.Lock()  # one Auctionator ingest at a time (watcher thread vs. POST /api/ingest)


def ingest(conn, cfg, force=False):
    with _lock:
        return _ingest(conn, cfg, force)


def _ingest(conn, cfg, force=False):
    path = cfg["savedvariables"]
    st = os.stat(path)
    mtime = int(st.st_mtime)
    # Rows from the web version (sync.pull) have no file_mtime: compare with the last import of the file itself.
    last = conn.execute("SELECT realm, file_mtime FROM ingests WHERE file_mtime IS NOT NULL "
                        "ORDER BY id DESC LIMIT 1").fetchone()
    if not force and last and last["file_mtime"] == mtime:
        return {"realm": last["realm"], "items": 0, "changed": 0, "skipped": True}

    data = luaparse.parse_file(path)
    now = int(time.time())
    pdb = data.get("AUCTIONATOR_PRICE_DATABASE") or {}
    realm_summaries = []
    cur = conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        for realm, rv in pdb.items():
            if not isinstance(realm, str) or realm.startswith("__"):
                continue
            items = _realm_items(rv)
            prev = {r[0]: (r[1], r[2], r[3], r[4]) for r in cur.execute(
                "SELECT item_id, price, available, last_day, updated_at FROM price_current WHERE realm=?", (realm,))}
            daily, current, snaps = [], [], []
            for iid, v in items.items():
                h, l, a = _dmap(v.get("h")), _dmap(v.get("l")), _dmap(v.get("a"))
                days = set(h) | set(l)
                for d in days:
                    hi = h.get(d, l.get(d))
                    daily.append((realm, iid, d, l.get(d, hi), hi, a.get(d)))
                last_day = max(days) if days else 0
                price = v.get("m")
                if not isinstance(price, (int, float)):
                    price = h.get(last_day) if days else None
                if price is None:
                    continue
                price = int(price)
                avail = a.get(last_day)
                p = prev.get(iid)
                if p is not None and (p[2] or 0, p[3] or 0) > (last_day, mtime):
                    continue  # a newer observation from the web version (another player's scan) is kept
                if p is None or p[0] != price or p[1] != avail or p[2] != last_day:
                    current.append((realm, iid, price, avail, last_day, mtime))
                if p is None or p[0] != price or p[1] != avail:
                    snaps.append((realm, iid, mtime, price, avail))
            cur.executemany(
                "INSERT INTO price_daily(realm,item_id,day,low,high,available) VALUES(?,?,?,?,?,?) "
                # Merge, never replace: Auctionator's own values only move this way within a day (lowest low,
                # highest high, most listed), and it keeps prices pulled from the web version intact.
                "ON CONFLICT(realm,item_id,day) DO UPDATE SET " + MERGE_DAILY, daily)
            cur.executemany(
                "INSERT INTO price_current(realm,item_id,price,available,last_day,updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(realm,item_id) DO UPDATE SET price=excluded.price, available=excluded.available, "
                "last_day=excluded.last_day, updated_at=excluded.updated_at", current)
            cur.executemany("INSERT INTO snapshots VALUES(?,?,?,?,?)", snaps)
            cur.execute("INSERT INTO ingests(ts,file_mtime,realm,items,changed) VALUES(?,?,?,?,?)",
                        (now, mtime, realm, len(items), len(snaps)))
            realm_summaries.append({"realm": realm, "items": len(items), "changed": len(snaps)})

        # posting history
        ph = data.get("AUCTIONATOR_POSTING_HISTORY") or {}
        prow = []
        if isinstance(ph, dict):
            for k, lst in ph.items():
                iid = _item_id(k)
                if iid is None:
                    continue
                entries = lst.values() if isinstance(lst, dict) else lst
                if not isinstance(entries, (list, type({}.values()))):
                    continue
                for e in entries:
                    if isinstance(e, dict) and "price" in e and "time" in e:
                        prow.append((iid, int(e["time"]), int(e["price"]), int(e.get("quantity") or 0)))
        cur.executemany("INSERT OR IGNORE INTO postings VALUES(?,?,?,?)", prow)

        # vendor prices seen by Auctionator
        nvend = staticdata.update_vendor_from_cache(cur, data.get("AUCTIONATOR_VENDOR_PRICE_CACHE"))
        cur.execute("INSERT OR REPLACE INTO meta VALUES('last_ingest_ts', ?)", (str(now),))
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    if not realm_summaries:
        return {"realm": None, "items": 0, "changed": 0, "skipped": False, "realms": [],
                "postings": len(prow), "vendor": nvend}
    main = max(realm_summaries, key=lambda s: s["items"])
    return {"realm": main["realm"], "items": sum(s["items"] for s in realm_summaries),
            "changed": sum(s["changed"] for s in realm_summaries), "skipped": False,
            "realms": realm_summaries, "postings": len(prow), "vendor": nvend}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    force = "--force" in argv
    cfg = config.load()
    conn = dbmod.connect(cfg["db_path"])
    t0 = time.time()
    staticdata.ensure(conn, cfg, force="--rebuild-static" in argv)
    t1 = time.time()
    s = ingest(conn, cfg, force=force)
    t2 = time.time()
    print("static %.1fs, ingest %.1fs" % (t1 - t0, t2 - t1))
    print("summary:", s)
    q = lambda sql: conn.execute(sql).fetchone()[0]
    print("items table:", q("SELECT COUNT(*) FROM items"), "| with name:", q("SELECT COUNT(*) FROM items WHERE name IS NOT NULL"))
    print("spells:", q("SELECT COUNT(*) FROM spells"), "recipes:", q("SELECT COUNT(*) FROM recipes"),
          "reagent rows:", q("SELECT COUNT(*) FROM recipe_reagents"))
    print("vendor_items:", q("SELECT COUNT(*) FROM vendor_items"), "(builtin:",
          q("SELECT COUNT(*) FROM vendor_items WHERE source='builtin'"), ")")
    print("price_current:", q("SELECT COUNT(*) FROM price_current"), "price_daily:", q("SELECT COUNT(*) FROM price_daily"),
          "snapshots:", q("SELECT COUNT(*) FROM snapshots"), "postings:", q("SELECT COUNT(*) FROM postings"))
    named = q("SELECT COUNT(*) FROM price_current pc JOIN items i ON i.id=pc.item_id WHERE i.name IS NOT NULL")
    tot = q("SELECT COUNT(*) FROM price_current")
    print("priced items with name: %d/%d (%.1f%%)" % (named, tot, 100.0 * named / max(tot, 1)))
    print("sample:")
    for r in conn.execute("SELECT pc.item_id, i.name, i.quality, pc.price, pc.available, pc.last_day FROM price_current pc "
                          "LEFT JOIN items i ON i.id=pc.item_id ORDER BY pc.price DESC LIMIT 5"):
        print("  ", tuple(r), day_to_date(r["last_day"]))
    for r in conn.execute("SELECT r.spell_id, s.name, ci.name AS created, r.profession_id, r.num_created FROM recipes r "
                          "LEFT JOIN spells s ON s.id=r.spell_id LEFT JOIN items ci ON ci.id=r.created_item_id LIMIT 4"):
        print("  ", tuple(r))
    print("  missing-name priced ids:", [r[0] for r in conn.execute(
        "SELECT pc.item_id FROM price_current pc LEFT JOIN items i ON i.id=pc.item_id WHERE i.name IS NULL LIMIT 10")])


if __name__ == "__main__":
    main()
