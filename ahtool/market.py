"""AH Tool Logger data: full scans, market depth, estimated sales, own auctions and AH mail.

The in-game addon (addon/AHToolLogger) writes AHToolLoggerDB to SavedVariables/AHToolLogger.lua:
  scans = [{t, realm, faction, char, src, total, n, d="itemId,suffix,count,buyout,minBid,bid,timeLeft,owner;...",
            nm="itemId:quality:name;..." (logger 1.1+; names custom items missing from the static data)}]
  owned = {"realm-char": {t, realm, faction, char, d="...,saleStatus;..."}}
  mail  = {key: {t, type=sold|bought|expired|cancelled, item, itemId?, player, price, buyout, deposit, fee, count, char}}

Sales are estimated by diffing consecutive full scans: an auction that is gone in the next scan while it still had
time left was most likely bought (or cancelled). Auctions that could have run out are counted as expired, and auctions
whose seller posted the same item again are counted as relisted.
"""
import datetime
import os
import threading
import time
from collections import Counter, defaultdict

from . import luaparse

# Max remaining seconds for GetAuctionItemTimeLeft categories (1 short, 2 medium, 3 long, 4 very long).
# Legacy AH: long = 2-8h. Modern AH (C_AuctionHouse, used by the Forever client): long = 2-12h. Very long is capped by
# the longest posting duration (24h on Forever).
TIME_LEFT_MAX = {1: 30 * 60, 2: 2 * 3600, 3: 12 * 3600, 4: 24 * 3600}
TIME_LEFT_TEXT = {1: "< 30m", 2: "30m - 2h", 3: "2h - 12h", 4: "12h - 24h"}
MAX_DIFF_GAP = 48 * 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS ah_scans(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, realm TEXT, faction TEXT,
    char TEXT, source TEXT, total INTEGER, n INTEGER, partial INTEGER NOT NULL DEFAULT 0,
    raw_kept INTEGER NOT NULL DEFAULT 1, diffed INTEGER NOT NULL DEFAULT 0, UNIQUE(realm, faction, ts));
CREATE TABLE IF NOT EXISTS ah_auctions(scan_id INTEGER, item_id INTEGER, suffix INTEGER, count INTEGER,
    buyout INTEGER, min_bid INTEGER, bid INTEGER, time_left INTEGER, owner TEXT);
CREATE INDEX IF NOT EXISTS idx_ah_auctions ON ah_auctions(scan_id, item_id);
CREATE TABLE IF NOT EXISTS ah_scan_items(scan_id INTEGER, item_id INTEGER, auctions INTEGER, qty INTEGER,
    min_unit INTEGER, median_unit INTEGER, sellers INTEGER, PRIMARY KEY(scan_id, item_id));
CREATE INDEX IF NOT EXISTS idx_ah_scan_items_item ON ah_scan_items(item_id, scan_id);
CREATE TABLE IF NOT EXISTS ah_sales(scan_id INTEGER, prev_scan_id INTEGER, ts INTEGER, item_id INTEGER,
    suffix INTEGER, count INTEGER, unit_price INTEGER, owner TEXT, kind TEXT);
CREATE INDEX IF NOT EXISTS idx_ah_sales_item ON ah_sales(item_id, ts);
CREATE INDEX IF NOT EXISTS idx_ah_sales_ts ON ah_sales(ts);
CREATE TABLE IF NOT EXISTS my_mail(key TEXT PRIMARY KEY, ts INTEGER, kind TEXT, item_name TEXT, item_id INTEGER,
    count INTEGER, price INTEGER, buyout INTEGER, deposit INTEGER, fee INTEGER, player TEXT, char TEXT, realm TEXT);
CREATE INDEX IF NOT EXISTS idx_my_mail_ts ON my_mail(ts);
CREATE TABLE IF NOT EXISTS my_auctions(char_key TEXT, ts INTEGER, char TEXT, realm TEXT, item_id INTEGER,
    suffix INTEGER, count INTEGER, buyout INTEGER, min_bid INTEGER, bid INTEGER, time_left INTEGER,
    sale_status INTEGER);
CREATE TABLE IF NOT EXISTS ah_item_names(id INTEGER PRIMARY KEY, name TEXT, quality INTEGER, ts INTEGER);
CREATE TABLE IF NOT EXISTS item_sell(id INTEGER PRIMARY KEY, price INTEGER NOT NULL, ts INTEGER);
"""

_lock = threading.Lock()


def ensure_schema(conn):
    conn.executescript(SCHEMA)
    from . import scanprices
    scanprices.ensure_schema(conn)


def _realm_fallback(cur, data):
    """The realm to use when the game reported none (GetRealmName() has returned "" on Forever): the newest realm
    named in the logger file, else the newest own scan's, else Auctionator's realm without its faction suffix."""
    best = (-1, "")
    for group in ("scans", "owned", "mail"):
        vals = data.get(group)
        vals = vals.values() if isinstance(vals, dict) else _seq(vals)
        for v in vals:
            if isinstance(v, dict) and _s(v.get("realm")):
                best = max(best, (_i(v.get("t")), _s(v.get("realm"))))
    if best[1]:
        return best[1]
    r = cur.execute("SELECT realm FROM ah_scans WHERE realm<>'' AND COALESCE(source,'')<>'web' "
                    "ORDER BY ts DESC LIMIT 1").fetchone()
    if r:
        return r[0]
    r = cur.execute("SELECT realm FROM ingests WHERE items>0 AND file_mtime IS NOT NULL ORDER BY id DESC "
                    "LIMIT 1").fetchone()
    if r and r[0]:
        for f in (" Alliance", " Horde", " Neutral"):
            if r[0].endswith(f):
                return r[0][:-len(f)]
        return r[0]
    return ""


def logger_path(cfg):
    p = cfg.get("logger_savedvariables")
    if p:
        return p
    sv = cfg.get("savedvariables") or ""
    return os.path.join(os.path.dirname(sv), "AHToolLogger.lua") if sv else ""


def addon_installed(cfg):
    ad = cfg.get("addons_dir") or ""
    return bool(ad) and os.path.isfile(os.path.join(ad, "AHToolLogger", "AHToolLogger.toc"))


def _cfg(cfg, key, default):
    try:
        v = (cfg or {}).get(key, default)
        return type(default)(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _s(v):
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", "replace")
    return "" if v is None else str(v)


_INT_LIMIT = 2 ** 62  # keep values well inside SQLite's signed 64-bit INTEGER


def _i(v, default=0):
    """int(v), or default for non-numbers, NaN/inf and values SQLite could not store."""
    try:
        n = int(v)
    except (TypeError, ValueError, OverflowError):
        try:
            n = int(float(v))
        except (TypeError, ValueError, OverflowError):
            return default
    return n if -_INT_LIMIT < n < _INT_LIMIT else default


def _seq(v):
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        return [v[k] for k in sorted(v, key=lambda k: (not isinstance(k, int), k if isinstance(k, int) else 0))]
    return []


MAX_ROW_MULT = 1000  # "N*row" repeats a row N times; larger groups are capped (identical commodity stacks)
MAX_SCAN_ROWS = 500000  # rows per scan after expansion (memory bound); a scan cut here is stored as partial
MAX_ROW_COUNT = 10000  # units in one auction (same cap as the sync SPEC); keeps per-item sums far from 2^63


def parse_rows(d, with_status=False):
    """Compact scan string -> [(item_id, suffix, count, buyout, min_bid, bid, time_left, owner[, sale_status])]."""
    out = []
    for row in _s(d).split(";"):
        if not row:
            continue
        mult = 1
        star = row.find("*")
        if 0 < star < row.find(","):
            try:
                mult = int(row[:star])
            except ValueError:
                continue
            if mult <= 0:
                continue  # malformed: skip the row
            mult = min(mult, MAX_ROW_MULT)  # cap, never drop: a dropped group would look like sales later
            row = row[star + 1:]
        f = row.split(",")
        if len(f) < 8:
            continue
        try:
            r = (int(f[0]), int(f[1]), int(f[2]), int(f[3]), int(f[4]), int(f[5]), int(f[6]), f[7])
        except ValueError:
            continue
        if r[0] <= 0 or not 0 < r[2] <= MAX_ROW_COUNT or any(abs(x) >= _INT_LIMIT for x in r[:7]):
            continue
        if with_status:
            r = r + (_i(f[8]) if len(f) > 8 else 0,)
        out.extend([r] * min(mult, MAX_SCAN_ROWS - len(out)))
        if len(out) >= MAX_SCAN_ROWS:
            break
    return out


# ---------------------------------------------------------------- sales estimation

def diff_scans(prev_rows, cur_rows, gap):
    """-> [(item_id, suffix, count, unit_price, owner, kind)] for auctions present in prev but gone in cur.

    kind: sold (gone while it still had time left), expired (could have run out), relisted (seller posted the same
    item again, i.e. probably cancelled + reposted).
    """
    def full(r):
        return (r[0], r[1], r[2], r[3], r[4], r[7])

    def loose(r):
        return (r[0], r[1], r[2], r[3], r[4])

    cur_full = Counter(full(r) for r in cur_rows)
    cur_loose = Counter(loose(r) for r in cur_rows)
    cur_blank = Counter(loose(r) for r in cur_rows if not r[7])

    groups = defaultdict(list)
    for r in prev_rows:
        groups[full(r)].append(r)
    gone = []
    for k, rows in groups.items():
        m = min(len(rows), cur_full.get(k, 0))
        if m:
            cur_full[k] -= m
            cur_loose[k[:5]] -= m
        if len(rows) > m:
            rows.sort(key=lambda r: r[6])  # the ones closest to expiring are the ones that disappeared
            gone.extend(rows[:len(rows) - m])

    # pass 2: owner name missing in one of the scans
    really_gone = []
    for r in gone:
        lk = loose(r)
        if not r[7] and cur_loose.get(lk, 0) > 0:
            cur_loose[lk] -= 1
            continue
        if r[7] and cur_blank.get(lk, 0) > 0 and cur_loose.get(lk, 0) > 0:
            cur_blank[lk] -= 1
            cur_loose[lk] -= 1
            continue
        really_gone.append(r)

    # new auctions per (item, owner) in cur -> relist detection
    prev_full = Counter(full(r) for r in prev_rows)
    new_by_owner = Counter()
    for k, n in Counter(full(r) for r in cur_rows).items():
        extra = n - prev_full.get(k, 0)
        if extra > 0 and k[5]:
            new_by_owner[(k[0], k[5])] += extra

    out = []
    for r in really_gone:
        iid, suffix, count, buyout, min_bid, bid, tl, owner = r[:8]
        could_expire = gap >= TIME_LEFT_MAX.get(tl, 24 * 3600)
        if owner and new_by_owner.get((iid, owner), 0) > 0:
            new_by_owner[(iid, owner)] -= 1
            kind = "relisted"
        elif bid > 0:
            kind = "sold"
        elif could_expire or buyout <= 0:
            kind = "expired"
        else:
            kind = "sold"
        if kind == "sold" and (could_expire or buyout <= 0) and bid > 0:
            price = bid
        else:
            price = buyout if buyout > 0 else bid
        unit = int(round(price / count)) if price > 0 else 0
        out.append((iid, suffix, count, unit, owner, kind))
    return out


# ---------------------------------------------------------------- ingest

def _scan_item_rows(scan_id, rows):
    per = defaultdict(list)
    for r in rows:
        per[r[0]].append(r)
    out = []
    for iid, rs in per.items():
        units = [r[3] / r[2] for r in rs if r[3] > 0]
        qty = sum(r[2] for r in rs)
        min_u = int(min(units)) if units else None
        # quantity-weighted median of the unit buyout
        med = None
        if units:
            pairs = sorted((r[3] / r[2], r[2]) for r in rs if r[3] > 0)
            half, acc = sum(q for _, q in pairs) / 2.0, 0
            for u, q in pairs:
                acc += q
                if acc >= half:
                    med = int(round(u))
                    break
        out.append((scan_id, iid, len(rs), qty, min_u, med, len({r[7] for r in rs if r[7]})))
    return out


def _load_rows(conn, scan_id):
    return [tuple(r) for r in conn.execute(
        "SELECT item_id, suffix, count, buyout, min_bid, bid, time_left, owner FROM ah_auctions WHERE scan_id=?",
        (scan_id,))]


def _diff_pending(conn):
    """Diff every not-yet-diffed scan against the previous complete scan of the same realm/faction."""
    n_sales = 0
    todo = conn.execute("SELECT id, ts, realm, faction FROM ah_scans WHERE diffed=0 AND partial=0 AND raw_kept=1 "
                        "AND COALESCE(source,'')<>'web' ORDER BY ts").fetchall()
    for sid, ts, realm, faction in todo:
        # Only against your own previous scan: scans pulled from the web version (source 'web') carry their own
        # sales, estimated by whoever scanned them, and diffing against them would count those sales twice.
        prev = conn.execute("SELECT id, ts FROM ah_scans WHERE realm=? AND faction=? AND ts<? AND partial=0 "
                            "AND COALESCE(source,'')<>'web' ORDER BY ts DESC LIMIT 1", (realm, faction, ts)).fetchone()
        conn.execute("UPDATE ah_scans SET diffed=1 WHERE id=?", (sid,))
        if not prev or ts - prev[1] > MAX_DIFF_GAP:
            continue
        prev_rows = _load_rows(conn, prev[0])
        if not prev_rows:
            continue
        sales = diff_scans(prev_rows, _load_rows(conn, sid), ts - prev[1])
        conn.executemany("INSERT INTO ah_sales VALUES(?,?,?,?,?,?,?,?,?)",
                         [(sid, prev[0], ts) + s for s in sales])
        n_sales += sum(1 for s in sales if s[5] == "sold")
    return n_sales


def _drop_scan(cur, sid):
    for t in ("ah_auctions", "ah_scan_items", "ah_sales"):
        cur.execute("DELETE FROM %s WHERE scan_id=?" % t, (sid,))
    cur.execute("UPDATE ah_sales SET prev_scan_id=NULL WHERE prev_scan_id=?", (sid,))
    cur.execute("DELETE FROM ah_scans WHERE id=?", (sid,))


def parse_names(nm):
    """Scan field nm = "itemId:quality:name;..." -> {item_id: (name, quality|None)}."""
    out = {}
    for part in _s(nm).split(";"):
        bits = part.split(":", 2)
        if len(bits) != 3 or not bits[2].strip():
            continue
        iid, q = _i(bits[0]), _i(bits[1], -1)
        if iid > 0 and iid not in out:
            out[iid] = (bits[2].strip(), q if 0 <= q <= 7 else None)
    return out


def _store_names(cur, names):
    """Remember item names seen on the AH and name items the static data does not know (custom realm items)."""
    if names:
        now = int(time.time())
        cur.executemany("INSERT INTO ah_item_names(id, name, quality, ts) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE "
                        "SET name=excluded.name, quality=COALESCE(excluded.quality, quality), ts=excluded.ts",
                        [(iid, n, q, now) for iid, (n, q) in names.items()])
    return apply_names(cur)


MAX_SELL_ROWS = 200000


def parse_sell(v):
    """Logger `sell` table {itemId: copper} (GetItemInfo's vendor sell price in the game) -> {item_id: price}."""
    out = {}
    if not isinstance(v, dict):
        return out
    for k, p in v.items():
        if len(out) >= MAX_SELL_ROWS:
            break
        if isinstance(p, bool) or isinstance(k, bool):
            continue
        iid, price = _i(k, -1), _i(p, -1)
        if 0 < iid < 2 ** 31 and 0 <= price < 2 ** 40:
            out[iid] = price
    return out


def _store_sell(cur, prices):
    if prices:
        now = int(time.time())
        cur.executemany("INSERT INTO item_sell(id, price, ts) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE "
                        "SET price=excluded.price, ts=excluded.ts", [(i, p, now) for i, p in prices.items()])
    return apply_sell(cur)


def apply_sell(conn):
    """Vendor sell prices the game reported (AH Tool Logger, GetItemInfo) win over the Classic Era client tables and
    the hotfix cache: Forever changed some (e.g. Lesser Magic Wand). Called after every logger import and after a
    static data rebuild. Returns the number of items changed."""
    try:
        before = conn.execute("SELECT total_changes()").fetchone()[0]
        conn.execute("UPDATE items SET vendor_sell=(SELECT s.price FROM item_sell s WHERE s.id=items.id) "
                     "WHERE id IN (SELECT id FROM item_sell) AND vendor_sell IS NOT "
                     "(SELECT s.price FROM item_sell s WHERE s.id=items.id)")
        changed = conn.execute("SELECT total_changes()").fetchone()[0] - before
        # The tooltip shows the same price ("Sell Price:"); item_tooltip is static data, so the web version gets it.
        conn.execute("UPDATE item_tooltip SET data=(SELECT CASE WHEN s.price > 0 "
                     "THEN json_set(item_tooltip.data, '$.sell_price', s.price) "
                     "ELSE json_remove(item_tooltip.data, '$.sell_price') END FROM item_sell s WHERE s.id=item_tooltip.id) "
                     "WHERE id IN (SELECT id FROM item_sell) AND json_valid(data) AND "
                     "COALESCE(json_extract(data, '$.sell_price'), 0) IS NOT (SELECT s.price FROM item_sell s "
                     "WHERE s.id=item_tooltip.id)")
        return changed
    except Exception:  # table missing (logger never ingested)
        return 0


def apply_names(conn):
    """Copy remembered AH names into items where the item is missing or unnamed. Existing names are never
    overwritten. Also called after a static data rebuild. Returns the number of items named."""
    try:
        before = conn.execute("SELECT total_changes()").fetchone()[0]
        conn.execute("INSERT OR IGNORE INTO items(id, name, quality) "
                     "SELECT id, name, COALESCE(quality, 1) FROM ah_item_names")
        conn.execute("UPDATE items SET name=(SELECT n.name FROM ah_item_names n WHERE n.id=items.id), "
                     "quality=COALESCE(quality, (SELECT n.quality FROM ah_item_names n WHERE n.id=items.id)) "
                     "WHERE (name IS NULL OR name='') AND id IN (SELECT id FROM ah_item_names)")
        return conn.execute("SELECT total_changes()").fetchone()[0] - before
    except Exception:  # table missing (logger never ingested)
        return 0


def _name_map(conn):
    m = {}
    for iid, name in conn.execute("SELECT id, name FROM items WHERE name IS NOT NULL ORDER BY id DESC"):
        m[name.lower()] = iid
    return m


def ingest(conn, cfg, force=False):
    """AHToolLogger.lua -> SQLite. Returns a summary dict ({"skipped": True} when unchanged)."""
    path = logger_path(cfg)
    if not path or not os.path.isfile(path):
        return {"skipped": True, "missing": True}
    mtime = int(os.stat(path).st_mtime)
    with _lock:
        ensure_schema(conn)
        r = conn.execute("SELECT value FROM meta WHERE key='logger_mtime'").fetchone()
        # an unchanged file is read again once when stored scans still need a realm or prices (after an upgrade)
        todo = conn.execute("SELECT 1 FROM ah_scans WHERE COALESCE(source,'')<>'web' AND (realm='' OR "
                            "(priced=0 AND partial=0)) LIMIT 1").fetchone()
        if not force and r and r[0] == str(mtime) and not (todo and (cfg or {}).get("prices_from_logger") is not False):
            return {"skipped": True}
        data = luaparse.parse_file(path).get("AHToolLoggerDB") or {}
        if not isinstance(data, dict):
            data = {}
        keep_raw = max(2, _cfg(cfg, "logger_keep_raw_scans", 6))
        new_scans = new_mail = 0
        cur = conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            names = {}
            for s in _seq(data.get("scans")):
                if isinstance(s, dict):
                    for iid, v in parse_names(s.get("nm")).items():
                        names.setdefault(iid, v)
            new_names = _store_names(cur, names)
            sell_changed = _store_sell(cur, parse_sell(data.get("sell")))
            fallback = _realm_fallback(cur, data)
            if fallback:
                # scans stored with an empty realm before this fix take the realm too (never over an existing one)
                cur.execute("UPDATE OR IGNORE ah_scans SET realm=? WHERE realm='' AND COALESCE(source,'')<>'web'",
                            (fallback,))
                cur.execute("UPDATE my_mail SET realm=? WHERE COALESCE(realm,'')=''", (fallback,))
                cur.execute("UPDATE my_auctions SET realm=? WHERE COALESCE(realm,'')=''", (fallback,))
            for s in sorted((x for x in _seq(data.get("scans")) if isinstance(x, dict)), key=lambda x: _i(x.get("t"))):
                ts, realm, faction = _i(s.get("t")), _s(s.get("realm")) or fallback, _s(s.get("faction"))
                if not ts:
                    continue
                have = cur.execute("SELECT id, COALESCE(source,'') FROM ah_scans WHERE realm=? AND faction=? AND ts=?",
                                   (realm, faction, ts)).fetchone()
                if have and have[1] != "web":
                    continue
                if have:
                    # a scan pulled from the web version (someone else's, or a fake one) never blocks your own
                    _drop_scan(cur, have[0])
                rows = parse_rows(s.get("d"))
                # GetReplicateItemTimeLeft may return 0-based bands; normalise to 1..4
                if rows and min(r[6] for r in rows) == 0 and max(r[6] for r in rows) <= 3:
                    rows = [r[:6] + (r[6] + 1,) + r[7:] for r in rows]
                # Compare with the previous own scan only: a pulled web scan's n is another player's claim.
                prev = cur.execute("SELECT n FROM ah_scans WHERE realm=? AND faction=? AND partial=0 AND ts<? "
                                   "AND COALESCE(source,'')<>'web' ORDER BY ts DESC LIMIT 1",
                                   (realm, faction, ts)).fetchone()
                partial = 1 if ((prev and prev[0] and len(rows) < 0.6 * prev[0]) or not rows
                                or len(rows) >= MAX_SCAN_ROWS) else 0  # a truncated scan must not be diffed
                cur.execute("INSERT INTO ah_scans(ts,realm,faction,char,source,total,n,partial) VALUES(?,?,?,?,?,?,?,?)",
                            (ts, realm, faction, _s(s.get("char")), _s(s.get("src")), _i(s.get("total")),
                             len(rows), partial))
                sid = cur.lastrowid
                cur.executemany("INSERT INTO ah_auctions VALUES(?,?,?,?,?,?,?,?,?)", [(sid,) + r for r in rows])
                cur.executemany("INSERT INTO ah_scan_items VALUES(?,?,?,?,?,?,?)", _scan_item_rows(sid, rows))
                new_scans += 1
            sold = _diff_pending(conn)
            # keep raw auctions only for the newest scans per realm/faction - your own and pulled ones (source
            # 'web') counted separately, so other players' scans never push out the one your next scan diffs against
            for realm, faction, web in cur.execute(
                    "SELECT DISTINCT realm, faction, COALESCE(source,'')='web' FROM ah_scans").fetchall():
                old = [x[0] for x in cur.execute(
                    "SELECT id FROM ah_scans WHERE realm=? AND faction=? AND (COALESCE(source,'')='web')=? AND raw_kept=1 "
                    "ORDER BY ts DESC LIMIT -1 OFFSET ?", (realm, faction, web, keep_raw)).fetchall()]
                for sid in old:
                    cur.execute("DELETE FROM ah_auctions WHERE scan_id=?", (sid,))
                    cur.execute("UPDATE ah_scans SET raw_kept=0, diffed=1 WHERE id=?", (sid,))

            names = None
            mail = data.get("mail")
            for key, m in (mail.items() if isinstance(mail, dict) else []):
                if not isinstance(m, dict):
                    continue
                if cur.execute("SELECT 1 FROM my_mail WHERE key=?", (str(key),)).fetchone():
                    continue
                item = _s(m.get("item"))
                iid = _i(m.get("itemId"), None)
                if not iid and item:
                    if names is None:
                        names = _name_map(conn)
                    iid = names.get(item.lower())
                cur.execute("INSERT INTO my_mail VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (str(key), _i(m.get("t")), _s(m.get("type")), item, iid, _i(m.get("count"), 1),
                             _i(m.get("price")), _i(m.get("buyout")), _i(m.get("deposit")), _i(m.get("fee")),
                             _s(m.get("player")), _s(m.get("char")), _s(m.get("realm")) or fallback))
                new_mail += 1

            owned = data.get("owned")
            n_owned = 0
            for key, o in (owned.items() if isinstance(owned, dict) else []):
                if not isinstance(o, dict):
                    continue
                ts = _i(o.get("t"))
                old = cur.execute("SELECT MAX(ts) FROM my_auctions WHERE char_key=?", (str(key),)).fetchone()[0]
                if old is not None and old >= ts:
                    continue
                cur.execute("DELETE FROM my_auctions WHERE char_key=?", (str(key),))
                rows = parse_rows(o.get("d"), with_status=True)
                cur.executemany("INSERT INTO my_auctions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                [(str(key), ts, _s(o.get("char")), _s(o.get("realm")) or fallback, r[0], r[1], r[2],
                                  r[3], r[4],
                                  r[5], r[6], r[8]) for r in rows])
                n_owned += len(rows)
            from . import scanprices
            priced = scanprices.derive(cur, cfg)
            cur.execute("INSERT OR REPLACE INTO meta VALUES('logger_mtime', ?)", (str(mtime),))
            cur.execute("INSERT OR REPLACE INTO meta VALUES('logger_ingest_ts', ?)", (str(int(time.time())),))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"skipped": False, "scans": new_scans, "sold": sold, "mail": new_mail, "owned": n_owned,
            "names": new_names, "sell": sell_changed, "prices": priced}


# ---------------------------------------------------------------- queries

def _realm_filter(conn, realm):
    """Logger realm names come from GetRealmName(); fall back to all scans when they don't match Auctionator's key."""
    if realm and conn.execute("SELECT 1 FROM ah_scans WHERE realm=? LIMIT 1", (realm,)).fetchone():
        return realm
    return None


PREFER_OWN_S = 6 * 3600  # your own (web version: the owner's) newest scan is used if it is at most this old


def preferred_scans_sql(conn):
    """SQL condition on ah_scans for the scans to prefer: the local app's own scans (not pulled from the web version);
    on the web version the owner's (AHTOOL_SYNC_TOKEN, an owner key, or pushed before keys were tracked)."""
    try:
        web = conn.execute("SELECT 1 FROM meta WHERE key='sync_epoch'").fetchone() is not None
        if web and "src_key" in [r[1] for r in conn.execute("PRAGMA table_info(ah_scans)")]:
            return ("(src_key IS NULL OR src_key<=0 OR src_key IN (SELECT id FROM sync_keys WHERE role='owner'))")
    except Exception:  # noqa - no meta / sync_keys table
        pass
    return "COALESCE(source,'')<>'web'"


def latest_scan(conn, realm=None, faction=None):
    """Newest complete scan with raw auctions. Your own (web version: the owner's) if one is at most PREFER_OWN_S old,
    else anyone's - so scans pushed or pulled from other players never hide yours."""
    ensure_schema(conn)
    realm = _realm_filter(conn, realm)
    q = "SELECT * FROM ah_scans WHERE partial=0 AND raw_kept=1"
    args = []
    if realm:
        q += " AND realm=?"
        args.append(realm)
    if faction:
        q += " AND faction=?"
        args.append(faction)
    r = conn.execute(q + " AND ts>=? AND " + preferred_scans_sql(conn) + " ORDER BY ts DESC LIMIT 1",
                     args + [int(time.time()) - PREFER_OWN_S]).fetchone()
    if not r:
        r = conn.execute(q + " ORDER BY ts DESC LIMIT 1", args).fetchone()
    return dict(r) if r else None


def _scan_info(scan):
    if not scan:
        return None
    return {"id": scan["id"], "ts": scan["ts"], "age_s": int(time.time()) - scan["ts"], "auctions": scan["n"],
            "realm": scan["realm"], "faction": scan["faction"], "char": scan["char"]}


def vendor_flips(conn, cfg=None, realm=None, limit=1000):
    """Every listing in the latest full scan whose buyout is below what a vendor pays for its units: buy it all and
    sell it to a vendor. Per item and in total: units, listings, cost, vendor revenue and profit. No AH cut applies
    (you buy on the AH and sell to an NPC). `game_price` says the vendor price came from the game itself (AH Tool
    Logger) rather than the Classic Era tables, which are wrong for some Forever items."""
    ensure_schema(conn)
    realm = _realm_filter(conn, realm)
    scan = latest_scan(conn, realm)
    out = {"scan": _scan_info(scan), "installed": addon_installed(cfg or {}), "items": [],
           "totals": {"items": 0, "listings": 0, "qty": 0, "cost": 0, "revenue": 0, "profit": 0,
                      "profit_game_price": 0}}
    if not scan:
        return out
    items = {}
    for iid, cnt, buyout, vs, name, quality, game in conn.execute(
            "SELECT a.item_id, a.count, a.buyout, i.vendor_sell, i.name, i.quality, "
            "EXISTS(SELECT 1 FROM item_sell s WHERE s.id=a.item_id) "
            "FROM ah_auctions a JOIN items i ON i.id=a.item_id "
            "WHERE a.scan_id=? AND a.buyout>0 AND a.count>0 AND i.vendor_sell>0 AND a.buyout < a.count*i.vendor_sell",
            (scan["id"],)):
        e = items.get(iid)
        if e is None:
            e = items[iid] = {"item_id": iid, "name": name, "quality": quality if quality is not None else 1,
                              "vendor_sell": vs, "game_price": bool(game), "listings": 0, "qty": 0, "cost": 0,
                              "revenue": 0, "profit": 0, "min_unit": None, "max_unit": None}
        unit = buyout / cnt
        e["listings"] += 1
        e["qty"] += cnt
        e["cost"] += buyout
        e["revenue"] += cnt * vs
        e["profit"] += cnt * vs - buyout
        e["min_unit"] = unit if e["min_unit"] is None else min(e["min_unit"], unit)
        e["max_unit"] = unit if e["max_unit"] is None else max(e["max_unit"], unit)
    lst = sorted(items.values(), key=lambda e: (-e["profit"], e["item_id"]))
    for e in lst:
        e["min_unit"], e["max_unit"] = round(e["min_unit"], 2), round(e["max_unit"], 2)
    t = out["totals"]
    for e in lst:
        t["items"] += 1
        for k in ("listings", "qty", "cost", "revenue", "profit"):
            t[k] += e[k]
        if e["game_price"]:
            t["profit_game_price"] += e["profit"]
    out["items"] = lst[:max(1, int(limit))]
    return out


def buy_cost(stacks, qty):
    """stacks: [(unit, count)] with buyout. Buys whole stacks from the cheapest up until qty is covered."""
    need = max(0, int(qty))
    total = bought = 0
    max_unit = None
    for unit, count in sorted(stacks):
        if bought >= need:
            break
        total += unit * count
        bought += count
        max_unit = unit
    return {"qty": need, "bought": bought, "total": int(round(total)),
            "avg_unit": round(total / bought, 1) if bought else None,
            "max_unit": round(max_unit, 1) if max_unit is not None else None,
            "short": max(0, need - bought), "extra": max(0, bought - need)}


def _stacks(conn, scan_id, item_id):
    return [(b / c, c) for b, c in conn.execute(
        "SELECT buyout, count FROM ah_auctions WHERE scan_id=? AND item_id=? AND buyout>0 AND count>0",
        (scan_id, item_id))]


def cost_for(conn, cfg, needs, realm=None):
    """{item_id: qty} -> {item_id: buy_cost} from the latest full scan (only when it is fresh enough)."""
    scan = latest_scan(conn, realm)
    if not scan:
        return None, {}
    if time.time() - scan["ts"] > _cfg(cfg, "logger_max_age_h", 24.0) * 3600:
        return _scan_info(scan), {}
    return _scan_info(scan), {iid: buy_cost(_stacks(conn, scan["id"], iid), q) for iid, q in needs.items()}


def _day(ts):
    return datetime.date.fromtimestamp(ts).isoformat()


def _since(days):
    return int(time.time()) - int(days) * 86400


def _scan_coverage(conn, realm, since):
    q = "SELECT COUNT(*), MIN(ts), MAX(ts) FROM ah_scans WHERE partial=0 AND ts>=?"
    args = [since]
    if realm:
        q += " AND realm=?"
        args.append(realm)
    n, a, b = conn.execute(q, args).fetchone()
    pairs = conn.execute("SELECT COUNT(DISTINCT scan_id) FROM ah_sales WHERE ts>=?", (since,)).fetchone()[0]
    days = len({_day(t) for (t,) in conn.execute("SELECT DISTINCT ts FROM ah_sales WHERE ts>=?", (since,))})
    return {"scans": n or 0, "scan_pairs": pairs or 0, "days_observed": days, "first_ts": a, "last_ts": b}


def item_market(conn, cfg, item_id, qty=None, days=14, realm=None):
    ensure_schema(conn)
    realm = _realm_filter(conn, realm)
    scan = latest_scan(conn, realm)
    out = {"item_id": item_id, "scan": _scan_info(scan), "installed": addon_installed(cfg),
           "listings": [], "summary": None, "depth": [], "cost": None}
    if scan:
        rows = conn.execute("SELECT count, buyout, min_bid, bid, time_left, owner, suffix FROM ah_auctions "
                            "WHERE scan_id=? AND item_id=?", (scan["id"], item_id)).fetchall()
        grouped = Counter()
        for c, b, mb, bid, tl, owner, suf in rows:
            grouped[(round(b / c, 2) if b > 0 and c > 0 else None, c, b, owner, tl, bid > 0)] += 1
        listings = [{"unit": k[0], "count": k[1], "buyout": k[2], "owner": k[3] or None, "time_left": k[4],
                     "time_left_text": TIME_LEFT_TEXT.get(k[4]), "has_bid": k[5], "auctions": n}
                    for k, n in grouped.items()]
        listings.sort(key=lambda x: (x["unit"] is None, x["unit"] or 0, x["count"]))
        out["listings"] = listings
        stacks = [(b / c, c) for c, b, *_ in rows if b > 0 and c > 0]
        qty_all = sum(r[0] for r in rows)
        units = sorted(stacks)
        cum, depth = 0, []
        for u, c in units:
            cum += c
            if depth and abs(depth[-1]["unit"] - u) < 1e-9:
                depth[-1]["cum_qty"] = cum
            else:
                depth.append({"unit": round(u, 2), "cum_qty": cum})
        out["depth"] = depth
        if rows:
            si = conn.execute("SELECT median_unit FROM ah_scan_items WHERE scan_id=? AND item_id=?",
                              (scan["id"], item_id)).fetchone()
            out["summary"] = {"auctions": len(rows), "qty": qty_all,
                              "sellers": len({r[5] for r in rows if r[5]}),
                              "min_unit": round(units[0][0], 2) if units else None,
                              "median_unit": si[0] if si else None,
                              "value": int(sum(u * c for u, c in stacks))}
        if qty:
            out["cost"] = buy_cost(stacks, qty)
    out["sales"] = item_sales(conn, item_id, days, realm)
    hist = conn.execute(
        "SELECT s.ts, i.qty, i.min_unit, i.median_unit, i.auctions, i.sellers FROM ah_scan_items i "
        "JOIN ah_scans s ON s.id=i.scan_id WHERE i.item_id=? AND s.partial=0 AND s.ts>=? "
        + ("AND s.realm=? " if realm else "") + "ORDER BY s.ts",
        (item_id, _since(days)) + ((realm,) if realm else ())).fetchall()
    out["supply"] = [{"ts": r[0], "qty": r[1], "min_unit": r[2], "median_unit": r[3], "auctions": r[4],
                      "sellers": r[5]} for r in hist]
    return out


def item_sales(conn, item_id, days=14, realm=None):
    since = _since(days)
    per = defaultdict(lambda: {"sold": 0, "expired": 0, "relisted": 0, "value": 0})
    for ts, count, unit, kind in conn.execute(
            "SELECT ts, count, unit_price, kind FROM ah_sales WHERE item_id=? AND ts>=? ORDER BY ts",
            (item_id, since)):
        d = per[_day(ts)]
        d[kind] = d.get(kind, 0) + count
        if kind == "sold":
            d["value"] += unit * count
    series = []
    for day in sorted(per):
        d = per[day]
        series.append({"date": day, "sold": d["sold"], "expired": d["expired"], "relisted": d["relisted"],
                       "value": d["value"], "avg_unit": round(d["value"] / d["sold"], 1) if d["sold"] else None})
    cov = _scan_coverage(conn, realm, since)
    sold = sum(d["sold"] for d in series)
    expired = sum(d["expired"] for d in series)
    value = sum(d["value"] for d in series)
    return {"days": days, "series": series, "coverage": cov,
            "totals": {"sold": sold, "expired": expired, "value": value,
                       "avg_unit": round(value / sold, 1) if sold else None,
                       "per_day": round(sold / cov["days_observed"], 1) if cov["days_observed"] else None,
                       "sell_through": round(sold / (sold + expired), 3) if sold + expired else None}}


def activity(conn, cfg, days=7, limit=100, realm=None):
    """Market-wide: best selling items, sellers in the latest scan, scan coverage."""
    ensure_schema(conn)
    realm = _realm_filter(conn, realm)
    since = _since(days)
    cov = _scan_coverage(conn, realm, since)
    dobs = max(1, cov["days_observed"])
    # TOTAL (a float sum), not SUM: SUM raises "integer overflow" past 2^63, and sales come from other players too
    rows = conn.execute(
        "SELECT a.item_id, TOTAL(CASE WHEN kind='sold' THEN count ELSE 0 END) AS sold, "
        "TOTAL(CASE WHEN kind='sold' THEN count*unit_price ELSE 0 END) AS value, "
        "TOTAL(CASE WHEN kind='expired' THEN count ELSE 0 END) AS expired, "
        "COUNT(DISTINCT CASE WHEN kind='sold' THEN owner END) AS sellers, i.name, i.quality "
        "FROM ah_sales a LEFT JOIN items i ON i.id=a.item_id WHERE a.ts>=? GROUP BY a.item_id "
        "HAVING sold>0 ORDER BY value DESC LIMIT ?", (since, int(limit))).fetchall()
    scan = latest_scan(conn, realm)
    supply = {}
    if scan:
        supply = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT item_id, qty, min_unit FROM ah_scan_items WHERE scan_id=?", (scan["id"],))}
    items = []
    for r in rows:
        sold, exp, value = int(r["sold"]), int(r["expired"]), int(round(r["value"]))
        sup = supply.get(r["item_id"])
        items.append({"item_id": r["item_id"], "name": r["name"], "quality": r["quality"] if r["quality"] is not None else 1,
                      "sold": sold, "value": value, "avg_unit": round(value / sold, 1) if sold else None,
                      "per_day": round(sold / dobs, 1), "expired": exp,
                      "sell_through": round(sold / (sold + exp), 3) if sold + exp else None,
                      "sellers": r["sellers"], "listed_now": sup[0] if sup else 0,
                      "min_unit_now": sup[1] if sup else None,
                      "days_of_supply": round(sup[0] / (sold / dobs), 1) if sup and sold else None})
    sellers = []
    if scan:
        for r in conn.execute(
                "SELECT owner, COUNT(*) AS auctions, COUNT(DISTINCT item_id) AS items, TOTAL(buyout) AS value "
                "FROM ah_auctions WHERE scan_id=? AND owner<>'' GROUP BY owner ORDER BY value DESC LIMIT 50",
                (scan["id"],)):
            sellers.append({"owner": r[0], "auctions": r[1], "items": r[2], "value": int(round(r[3] or 0))})
    totals = conn.execute("SELECT TOTAL(CASE WHEN kind='sold' THEN count*unit_price ELSE 0 END), "
                          "SUM(CASE WHEN kind='sold' THEN 1 ELSE 0 END), SUM(CASE WHEN kind='expired' THEN 1 ELSE 0 END), "
                          "SUM(CASE WHEN kind='relisted' THEN 1 ELSE 0 END) FROM ah_sales WHERE ts>=?",
                          (since,)).fetchone()
    scans = [{"ts": r[0], "auctions": r[1], "partial": bool(r[2]), "char": r[3], "source": r[4]}
             for r in conn.execute("SELECT ts, n, partial, char, source FROM ah_scans ORDER BY ts DESC LIMIT 30")]
    return {"days": days, "coverage": cov, "scan": _scan_info(scan), "installed": addon_installed(cfg),
            "totals": {"value": int(round(totals[0] or 0)), "sold_auctions": totals[1] or 0, "expired_auctions": totals[2] or 0,
                       "relisted_auctions": totals[3] or 0},
            "items": items, "sellers": sellers, "scans": scans}


def my_market(conn, cfg, days=30, realm=None):
    """Own AH mail (sales, purchases, expired) + own auctions vs the latest scan."""
    ensure_schema(conn)
    since = _since(days)
    cut = _cfg(cfg, "ah_cut", 0.05)
    mail = conn.execute("SELECT m.*, i.quality FROM my_mail m LEFT JOIN items i ON i.id=m.item_id "
                        "WHERE m.ts>=? ORDER BY m.ts DESC", (since,)).fetchall()
    per = {}
    tot = {"sold_value": 0, "sold_net": 0, "fees": 0, "bought_value": 0, "sold": 0, "bought": 0, "expired": 0,
           "cancelled": 0}
    log = []
    for m in mail:
        kind = m["kind"]
        key = m["item_id"] or m["item_name"]
        e = per.setdefault(key, {"item_id": m["item_id"], "name": m["item_name"], "quality": m["quality"] or 1,
                                 "sold": 0, "sold_value": 0, "sold_net": 0, "bought": 0, "bought_value": 0,
                                 "expired": 0, "cancelled": 0, "last_ts": m["ts"]})
        cnt = m["count"] or 1
        if kind == "sold":
            net = m["price"] - m["fee"]
            e["sold"] += cnt
            e["sold_value"] += m["price"]
            e["sold_net"] += net
            tot["sold_value"] += m["price"]
            tot["sold_net"] += net
            tot["fees"] += m["fee"]
            tot["sold"] += 1
        elif kind == "bought":
            e["bought"] += cnt
            e["bought_value"] += m["price"]
            tot["bought_value"] += m["price"]
            tot["bought"] += 1
        elif kind in ("expired", "cancelled"):
            e[kind] += cnt
            tot[kind] += 1
        log.append({"ts": m["ts"], "kind": kind, "item_id": m["item_id"], "name": m["item_name"],
                    "quality": m["quality"] or 1, "count": cnt, "price": m["price"],
                    "unit": round(m["price"] / cnt, 1) if m["price"] else None,
                    "fee": m["fee"], "player": m["player"] or None, "char": m["char"]})
    items = []
    for e in per.values():
        posted = e["sold"] + e["expired"] + e["cancelled"]
        e["sell_through"] = round(e["sold"] / posted, 3) if posted else None
        e["avg_sold_unit"] = round(e["sold_value"] / e["sold"], 1) if e["sold"] else None
        items.append(e)
    items.sort(key=lambda e: -e["sold_net"])

    scan = latest_scan(conn, realm)
    mine = conn.execute("SELECT a.*, i.name, i.quality FROM my_auctions a LEFT JOIN items i ON i.id=a.item_id "
                        "ORDER BY i.name").fetchall()
    my_names = {r["char"] for r in mine if r["char"]}
    auctions = []
    for a in mine:
        unit = a["buyout"] / a["count"] if a["buyout"] > 0 and a["count"] else None
        lowest = None
        cheaper = 0
        if scan:
            q = ("SELECT buyout, count, owner FROM ah_auctions WHERE scan_id=? AND item_id=? AND buyout>0 AND count>0")
            others = [(b / c, c) for b, c, o in conn.execute(q, (scan["id"], a["item_id"]))
                      if not (o and o.split("-")[0] in my_names)]
            if others:
                lowest = min(u for u, _ in others)
                if unit is not None:
                    cheaper = sum(c for u, c in others if u < unit - 1e-9)
        status = None
        if a["sale_status"] == 1:
            status = "sold"
        elif unit is not None and lowest is not None:
            status = "undercut" if lowest < unit - 1e-9 else "lowest"
        auctions.append({"item_id": a["item_id"], "name": a["name"], "quality": a["quality"] or 1,
                         "char": a["char"], "count": a["count"], "buyout": a["buyout"],
                         "unit": round(unit, 1) if unit is not None else None, "bid": a["bid"],
                         "time_left": a["time_left"], "time_left_text": TIME_LEFT_TEXT.get(a["time_left"]),
                         "lowest_other": round(lowest, 1) if lowest is not None else None,
                         "cheaper_qty": cheaper, "status": status, "ts": a["ts"],
                         "net_if_sold": int(a["buyout"] * (1 - cut)) if a["buyout"] else None})
    owned_ts = max((a["ts"] for a in auctions), default=None)
    return {"days": days, "installed": addon_installed(cfg), "totals": tot, "items": items, "log": log[:500],
            "auctions": auctions, "auctions_ts": owned_ts, "scan": _scan_info(scan)}


def logger_status(conn, cfg):
    ensure_schema(conn)
    n, last = conn.execute("SELECT COUNT(*), MAX(ts) FROM ah_scans WHERE partial=0").fetchone()
    nm = conn.execute("SELECT COUNT(*) FROM my_mail").fetchone()[0]
    path = logger_path(cfg)
    return {"installed": addon_installed(cfg), "file_exists": bool(path) and os.path.isfile(path),
            "scans": n or 0, "last_scan_ts": last, "mail_records": nm}
