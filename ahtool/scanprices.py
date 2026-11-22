"""Current and daily prices from AH Tool Logger full scans, so Auctionator's own scanning is optional.

Auctionator only records the items you look at, unless you run its full scan. A logger full scan holds every
auction, and ah_scan_items already has the lowest unit buyout and the quantity per item. Each new own, complete scan
becomes Auctionator-style price rows:

- price_daily: the scan's lowest unit price merged into the day (low = MIN, high = MAX, available = MAX), like
  several Auctionator scans on one day;
- price_current: the lowest unit price and quantity, when the scan is newer than what is stored (Auctionator's
  row, or an older scan) - a newer Auctionator import still wins for the items it saw;
- snapshots: when price or quantity changed.

The realm key follows Auctionator's: "<realm> <faction>" (e.g. "PvP Alliance") when Auctionator stores keys that
way, else "<realm>". Neutral auction houses (Booty Bay, Gadgetzan, Everlook) get "<realm> Neutral", so they never mix
with a faction's prices.

Scans pulled from the web version (source 'web') are someone else's: their prices arrive through the price sync.
"""
from .ingest import MERGE_DAILY, _today

FACTIONS = ("Alliance", "Horde", "Neutral")
SCHEMA_COLUMN = "priced"


def ensure_schema(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(ah_scans)")]
    if cols and SCHEMA_COLUMN not in cols:
        conn.execute("ALTER TABLE ah_scans ADD COLUMN priced INTEGER NOT NULL DEFAULT 0")


def known_realms(cur):
    out = {r[0] for r in cur.execute("SELECT DISTINCT realm FROM price_current") if r[0]}
    out |= {r[0] for r in cur.execute("SELECT DISTINCT realm FROM ingests WHERE items > 0") if r[0]}
    return out


def price_realm(known, realm, faction):
    """Price key for a scan's realm and faction; None when the scan has no realm."""
    if not realm:
        return None
    faction = faction if faction in FACTIONS else None
    if not faction:
        return realm
    keyed = "%s %s" % (realm, faction)
    if keyed in known:
        return keyed
    suffix_style = any(k.startswith(realm + " ") and k[len(realm) + 1:] in FACTIONS for k in known)
    # no Auctionator data yet: use today's Auctionator style; a neutral AH never shares a faction's key
    if suffix_style or faction == "Neutral" or realm not in known:
        return keyed
    return realm


def derive(cur, cfg=None, now=None):
    """Turn own, complete, not yet priced scans into price rows (inside the caller's transaction).
    Returns {"scans": n, "items": n, "changed": n, "realms": {key: items}}."""
    if (cfg or {}).get("prices_from_logger") is False:
        return {"scans": 0, "items": 0, "changed": 0, "realms": {}}
    import time
    now = int(now if now is not None else time.time())
    today = _today(now)
    known = known_realms(cur)
    scans = cur.execute("SELECT id, ts, realm, faction FROM ah_scans WHERE priced=0 AND partial=0 AND "
                        "COALESCE(source,'')<>'web' AND realm<>'' ORDER BY ts").fetchall()
    per_realm = {}
    n_items = n_changed = 0
    for sid, ts, realm, faction in scans:
        key = price_realm(known, realm, faction)
        if not key or not ts or ts > now + 300:
            continue
        day = _today(ts)
        rows = cur.execute("SELECT item_id, min_unit, qty FROM ah_scan_items WHERE scan_id=? AND min_unit>0 "
                           "AND qty>0", (sid,)).fetchall()
        if not rows:
            cur.execute("UPDATE ah_scans SET priced=1 WHERE id=?", (sid,))
            continue
        cur.executemany("INSERT INTO price_daily(realm,item_id,day,low,high,available) VALUES(?,?,?,?,?,?) "
                        "ON CONFLICT(realm,item_id,day) DO UPDATE SET " + MERGE_DAILY,
                        [(key, iid, day, price, price, qty) for iid, price, qty in rows])
        prev = {r[0]: (r[1], r[2], r[3], r[4]) for r in cur.execute(
            "SELECT item_id, price, available, last_day, updated_at FROM price_current WHERE realm=?", (key,))}
        current, snaps = [], []
        for iid, price, qty in rows:
            p = prev.get(iid)
            if p is not None:
                old_day, old_ts = p[2] or 0, p[3] or 0
                future = old_day > today or old_ts > now + 300  # a future-dated row never blocks real data
                if not future and (old_day, old_ts) >= (day, ts):
                    continue
            current.append((key, iid, price, qty, day, ts))
            if p is None or p[0] != price or p[1] != qty:
                snaps.append((key, iid, ts, price, qty))
        cur.executemany("INSERT INTO price_current(realm,item_id,price,available,last_day,updated_at) "
                        "VALUES(?,?,?,?,?,?) ON CONFLICT(realm,item_id) DO UPDATE SET price=excluded.price, "
                        "available=excluded.available, last_day=excluded.last_day, updated_at=excluded.updated_at",
                        current)
        cur.executemany("INSERT INTO snapshots(realm,item_id,ts,price,available) VALUES(?,?,?,?,?)", snaps)
        cur.execute("UPDATE ah_scans SET priced=1 WHERE id=?", (sid,))
        known.add(key)
        per_realm[key] = max(per_realm.get(key, 0), len(rows))
        n_items += len(current)
        n_changed += len(snaps)
    if per_realm:
        # one ingest row per realm (analytics caches key on MAX(ingests.id)); file_mtime NULL like a pull, so the
        # Auctionator import still compares only with its own file
        for key, items in sorted(per_realm.items()):
            cur.execute("INSERT INTO ingests(ts,file_mtime,realm,items,changed) VALUES(?,NULL,?,?,?)",
                        (now, key, items, n_changed))
    return {"scans": len(scans), "items": n_items, "changed": n_changed, "realms": per_realm}
