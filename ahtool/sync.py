"""Push market data from local AH Tools to the shared web version, and receive it there.

Several people can push to the same web version, each with their own key (see keys.py). Each pusher's data is
merged, never replaces anyone else's.

Only public market data leaves a PC. What is pushed is decided by the explicit column lists below, never by
"everything except": a table or column that is added locally later stays local until it is listed here.

Pushed:   item/recipe/tooltip data, Auctionator prices (price_daily, price_current, snapshots) and the AH Tool Logger
          full scans (ah_scans, ah_scan_items, ah_auctions, ah_sales).
Never:    postings (Auctionator posting history), my_mail, my_auctions, my_professions, known_recipes,
          shopping_list, watchlist, alerts, settings, paths from config.json.
Masked:   ah_scans.char is blanked, and the seller name of every auction/sale posted by one of the pusher's own
          characters is blanked (""), so the web version cannot tell which auctions are whose. The characters are
          taken from the scans, own auctions and AH mail on that PC, plus `sync_hide_sellers` in its config.json.

Merging (server): price_daily keeps the lowest low / highest high / highest available per realm+item+day,
price_current keeps the newest observation per item, snapshots and scans are unique by key. Analysis settings and
the static item/recipe data come only from the owner (AHTOOL_SYNC_TOKEN or an owner key); a contributor's static
data is used only while the web version has none.

Incremental: each PC remembers in its own DB what it has pushed to which web version (meta `sync:<url>`), tied to
the web database's random `epoch`, so a reset web database gets everything again. `python -m ahtool.sync --full`
resends everything.

Two-way: every PC also pulls what the others pushed (GET /api/sync/pull). The server stamps every row a push changes
with that push's sequence number (`seq`), and each PC remembers the last one it has (meta `pull:<url>`), so only new
rows travel. Pulled prices are merged with the same rules as on the server; pulled scans are stored with
source='web', are never pushed back, and are never diffed against your own scans (their sales come with them).
"""
import gzip
import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .ingest import MERGE_DAILY

PROTOCOL = 2
MAX_BATCH_ROWS = 150000
DAY_OVERLAP = 3  # days of price_daily that are always resent (Auctionator updates today and recent days)

STATIC_TABLES = {
    "items": ("id", "name", "quality", "item_level", "class_id", "subclass_id", "icon", "vendor_sell", "vendor_buy",
              "stack"),
    "spells": ("id", "name"),
    "recipes": ("spell_id", "profession_id", "created_item_id", "num_created", "min_skill", "low_skill",
                "high_skill"),
    "recipe_reagents": ("spell_id", "item_id", "count"),
    "vendor_items": ("item_id", "price", "source"),
    "item_tooltip": ("id", "icon", "data"),
}
PRICE_DAILY = ("realm", "item_id", "day", "low", "high", "available")
PRICE_CURRENT = ("realm", "item_id", "price", "available", "last_day", "updated_at")
SNAPSHOTS = ("realm", "item_id", "ts", "price", "available")
SCAN = ("ts", "realm", "faction", "source", "total", "n", "partial", "raw_kept", "diffed")
SCAN_ITEMS = ("item_id", "auctions", "qty", "min_unit", "median_unit", "sellers")
AUCTIONS = ("item_id", "suffix", "count", "buyout", "min_bid", "bid", "time_left", "owner")
SALES = ("prev_ts", "item_id", "suffix", "count", "unit_price", "owner", "kind")
META_MAX_KEYS = ("last_ingest_ts", "logger_ingest_ts")  # merged as the newest of all pushers
# Analysis settings that make the web version compute the same numbers as the owner's app. Never paths.
CFG_KEYS = ("ah_cut", "deal_threshold", "baseline_days", "outlier_mad_k", "outlier_min_rel", "thin_qty",
            "thin_high_factor", "conf_min_days", "conf_min_avg_qty", "trend_min_profit", "logger_keep_raw_scans",
            "logger_max_age_h")

SEQ_TABLES = ("price_daily", "price_current", "ah_scans")
SERVER_SCHEMA = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_snapshots ON snapshots(realm, item_id, ts);
CREATE INDEX IF NOT EXISTS ix_price_daily_seq ON price_daily(seq);
CREATE INDEX IF NOT EXISTS ix_price_current_seq ON price_current(seq);
CREATE INDEX IF NOT EXISTS ix_ah_scans_seq ON ah_scans(seq);
CREATE TABLE IF NOT EXISTS sync_keys(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
    hash TEXT NOT NULL UNIQUE, role TEXT NOT NULL DEFAULT 'contributor', created_at INTEGER NOT NULL,
    revoked_at INTEGER, last_push_ts INTEGER, pushes INTEGER NOT NULL DEFAULT 0);
"""


class SyncError(Exception):
    pass


# ================================================================ client (local app)

def own_names(conn, cfg):
    """Lower-cased names of your own characters (without realm suffix)."""
    names = set()
    for q in ("SELECT DISTINCT char FROM ah_scans", "SELECT DISTINCT char FROM my_auctions",
              "SELECT DISTINCT char FROM my_mail"):
        try:
            names.update(r[0] for r in conn.execute(q) if r[0])
        except Exception:  # noqa - table missing in an old DB
            pass
    extra = (cfg or {}).get("sync_hide_sellers") or []
    if isinstance(extra, str):
        extra = extra.split(",")
    names.update(extra)
    return {_base_name(n) for n in names if n and _base_name(n)}


def _base_name(name):
    return str(name).split("-")[0].strip().lower()


def _mask(owner, own):
    if owner and _base_name(owner) in own:
        return ""
    return owner or ""


def static_hash(conn):
    h = hashlib.sha256()
    for t, cols in STATIC_TABLES.items():
        h.update(t.encode())
        for r in conn.execute("SELECT %s FROM %s ORDER BY 1, 2" % (",".join(cols), t)):
            h.update(json.dumps(list(r), separators=(",", ":")).encode())
    return h.hexdigest()


def _rows(conn, sql, params=()):
    return [list(r) for r in conn.execute(sql, params)]


def _local_key(cfg):
    return "sync:" + hashlib.sha1((cfg.get("sync_url") or "").rstrip("/").encode()).hexdigest()[:16]


def local_state(conn, cfg):
    r = conn.execute("SELECT value FROM meta WHERE key=?", (_local_key(cfg),)).fetchone()
    try:
        s = json.loads(r[0]) if r else {}
    except ValueError:
        s = {}
    return s if isinstance(s, dict) else {}


def save_local_state(conn, cfg, state):
    with conn:
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", (_local_key(cfg), json.dumps(state)))


def build_batches(conn, cfg, remote, full=False, mine=None):
    """-> (list of payload dicts to POST in order, the local sync state to save once all of them succeeded).

    `remote` is the server's /api/sync/state answer, `mine` what this PC pushed before (local_state)."""
    own = own_names(conn, cfg)
    mine = dict(mine or {})
    if full or mine.get("epoch") != remote.get("epoch"):
        mine = {}
    new_state = {"epoch": remote.get("epoch"), "daily": dict(mine.get("daily") or {}), "snap_ts": mine.get("snap_ts")}
    meta = {k: v for k, v in conn.execute("SELECT key, value FROM meta") if k in META_MAX_KEYS}
    head = {"protocol": PROTOCOL, "meta": meta}
    if remote.get("accept_cfg"):
        head["cfg"] = {k: cfg[k] for k in CFG_KEYS if k in (cfg or {})}
    batches = []

    # --- static item/recipe data: all or nothing, only when it changed and the server wants it from us
    if remote.get("accept_static"):
        sh = static_hash(conn)
        if full or remote.get("static_hash") != sh:
            batches.append({"static": {"hash": sh, "tables": {
                t: _rows(conn, "SELECT %s FROM %s" % (",".join(cols), t)) for t, cols in STATIC_TABLES.items()}},
                "meta": dict(meta, **{k: v for k, v in conn.execute(
                    "SELECT key, value FROM meta WHERE key='tooltip_version'")})})

    # --- Auctionator prices
    prices = {"price_current": _rows(conn, "SELECT %s FROM price_current" % ",".join(PRICE_CURRENT))}
    since = mine.get("snap_ts")
    prices["snapshots"] = _rows(conn, "SELECT %s FROM snapshots WHERE ts>? ORDER BY ts" % ",".join(SNAPSHOTS),
                                (since if since is not None else -1,))
    if prices["snapshots"]:
        new_state["snap_ts"] = prices["snapshots"][-1][2]
    batches.append(prices)

    daily = []
    for realm, mx in conn.execute("SELECT realm, MAX(day) FROM price_daily GROUP BY realm").fetchall():
        wm = (mine.get("daily") or {}).get(realm)
        frm = wm - DAY_OVERLAP if isinstance(wm, int) else -10 ** 9
        daily.extend(_rows(conn, "SELECT %s FROM price_daily WHERE realm=? AND day>=? ORDER BY day, item_id"
                           % ",".join(PRICE_DAILY), (realm, frm)))
        new_state["daily"][realm] = mx
    for i in range(0, len(daily), MAX_BATCH_ROWS):
        batches.append({"price_daily": daily[i:i + MAX_BATCH_ROWS]})

    # --- AH Tool Logger scans
    have = {(r[0], r[1], r[2]): (r[3], r[4]) for r in remote.get("scans") or []}
    # only your own scans: pulled ones (source 'web') came from the server and belong to whoever scanned them
    scans = conn.execute("SELECT id, %s FROM ah_scans WHERE COALESCE(source,'')<>'web' ORDER BY ts"
                         % ",".join(SCAN)).fetchall()
    key_of = {s[0]: (s[2], s[3], s[1]) for s in scans}  # id -> (realm, faction, ts)

    def sales_for(sid):
        out = []
        for r in conn.execute("SELECT prev_scan_id, item_id, suffix, count, unit_price, owner, kind FROM ah_sales "
                              "WHERE scan_id=?", (sid,)):
            prev = key_of.get(r[0])
            out.append([prev[2] if prev else None, r[1], r[2], r[3], r[4], _mask(r[5], own), r[6]])
        return out

    cur, cur_rows = [], 0
    updates = []
    for s in scans:
        sid, row = s[0], list(s[1:])
        key = (row[1], row[2], row[0])
        raw_kept, diffed = row[7], row[8]
        if key in have and not full:
            r_raw, r_diffed = have[key]
            upd = {}
            if r_raw and not raw_kept:
                upd["drop_raw"] = True
            if diffed and not r_diffed:
                upd["sales"] = sales_for(sid)
            if upd:
                upd["key"] = list(key)
                updates.append(upd)
            continue
        item = {"scan": row,
                "items": _rows(conn, "SELECT %s FROM ah_scan_items WHERE scan_id=?" % ",".join(SCAN_ITEMS), (sid,)),
                "auctions": [], "sales": sales_for(sid)}
        if raw_kept:
            item["auctions"] = [r[:7] + [_mask(r[7], own)] for r in _rows(
                conn, "SELECT %s FROM ah_auctions WHERE scan_id=?" % ",".join(AUCTIONS), (sid,))]
        n = len(item["items"]) + len(item["auctions"]) + len(item["sales"])
        if cur and cur_rows + n > MAX_BATCH_ROWS:
            batches.append({"scans": cur})
            cur, cur_rows = [], 0
        cur.append(item)
        cur_rows += n
    if cur:
        batches.append({"scans": cur})
    if updates:
        batches.append({"scan_updates": updates})

    batches[0] = dict(head, **batches[0])
    for b in batches[1:]:
        b["protocol"] = PROTOCOL
    return batches, new_state


def _request(cfg, method, path, payload=None, timeout=180):
    url = (cfg.get("sync_url") or "").rstrip("/") + path
    headers = {"Authorization": "Bearer %s" % cfg.get("sync_token", ""), "Accept": "application/json",
               "User-Agent": "AHTool-sync/%d" % PROTOCOL}
    data = None
    if payload is not None:
        data = gzip.compress(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"), 6)
        headers["Content-Type"] = "application/gzip"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        body = e.read()[:300].decode("utf-8", "replace")
        try:
            body = json.loads(body).get("error", body)
        except ValueError:
            pass
        raise SyncError("%s %s -> HTTP %d: %s" % (method, url, e.code, body))
    except (urllib.error.URLError, OSError) as e:
        raise SyncError("%s %s failed: %s" % (method, url, getattr(e, "reason", e)))
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError:
        # typically a login page: the web version's host is behind a login that does not let the sync path through
        raise SyncError("%s %s did not answer with JSON - is the site behind a login?" % (method, url))


def push(conn, cfg, full=False):
    """Bring the web version up to date with this PC's data. Returns a summary dict."""
    if not cfg.get("sync_url") or not cfg.get("sync_token"):
        raise SyncError("sync_url and sync_token must be set in config.json")
    t0 = time.time()
    remote = _request(cfg, "GET", "/api/sync/state")
    if remote.get("protocol") != PROTOCOL:
        raise SyncError("the web version speaks sync protocol %s, this app %s - update the older one"
                        % (remote.get("protocol"), PROTOCOL))
    batches, new_state = build_batches(conn, cfg, remote, full=full, mine=local_state(conn, cfg))
    sent = {"batches": 0, "as": remote.get("you")}
    for b in batches:
        res = _request(cfg, "POST", "/api/sync/push", b)
        sent["batches"] += 1
        for k, v in (res.get("applied") or {}).items():
            sent[k] = sent.get(k, 0) + v if isinstance(v, int) else v
    save_local_state(conn, cfg, new_state)
    sent["seconds"] = round(time.time() - t0, 1)
    return sent


class Syncer:
    """Background sync for run.py.

    trigger(): push soon (debounced: Auctionator and the logger usually change together on /reload), then pull.
    Every `sync_pull_minutes` (default 5, 0 = never) it pulls what other players pushed. Never two at once; failures
    are printed and retried next time. on_pulled(result) is called when a pull changed local data."""

    def __init__(self, cfg, connect, on_pulled=None, delay=8.0, log=print):
        self.cfg, self.connect, self.on_pulled, self.delay, self.log = cfg, connect, on_pulled, delay, log
        try:
            minutes = float(cfg.get("sync_pull_minutes", 5))
        except (TypeError, ValueError):
            minutes = 5.0
        self.pull_every = minutes * 60 if cfg.get("sync_pull", True) and minutes > 0 else None
        self._ev = threading.Event()
        self._push_due = None
        self._pull_due = time.time() + 2 if self.pull_every else None
        self._full = False
        threading.Thread(target=self._run, name="ahtool-sync", daemon=True).start()

    def trigger(self, full=False):
        self._push_due = time.time() + self.delay
        self._full = self._full or full
        self._ev.set()

    def _run(self):
        while True:
            dues = [d for d in (self._push_due, self._pull_due) if d is not None]
            wait = max(0.2, min(dues) - time.time()) if dues else None
            self._ev.wait(wait)
            self._ev.clear()
            now = time.time()
            do_push = self._push_due is not None and now >= self._push_due
            do_pull = self.pull_every is not None and (do_push or (self._pull_due and now >= self._pull_due))
            if not (do_push or do_pull):
                continue
            conn = None
            try:
                conn = self.connect()
                if do_push:
                    self._push_due = None
                    full, self._full = self._full, False
                    try:
                        self.log("  Pushed to the web version: %s" % _summary(push(conn, self.cfg, full=full)))
                    except Exception as e:  # noqa - never take the local app down
                        self.log("  Push to the web version failed (retried after the next import): %s" % e)
                if do_pull:
                    self._pull_due = time.time() + self.pull_every
                    try:
                        res = pull(conn, self.cfg)
                        if res["prices"] or res["scans"]:
                            self.log("  Pulled from the web version: %d price rows, %d scans from other players."
                                     % (res["prices"], res["scans"]))
                            if self.on_pulled:
                                self.on_pulled(res)
                    except Exception as e:  # noqa
                        self.log("  Pull from the web version failed (retried in %d min): %s"
                                 % (self.pull_every // 60, e))
            except Exception as e:  # noqa - e.g. the local DB is locked
                self.log("  Sync failed: %s" % e)
            finally:
                if conn is not None:
                    conn.close()


Pusher = Syncer  # older name


def _summary(res):
    keys = ("price_daily", "snapshots", "scans", "auctions", "static")
    parts = ["%s %s" % (res[k], k) for k in keys if res.get(k)]
    if res.get("static_ignored"):
        parts.append("item data kept from the owner")
    who = " as %s" % res["as"] if res.get("as") else ""
    return ", ".join(parts or ["already up to date"]) + "%s in %ss" % (who, res.get("seconds"))


# ================================================================ server (web version)

def ensure_schema(conn):
    # Server only: which push last changed a row, for /api/sync/pull. The local app's tables never get it.
    for t in SEQ_TABLES:
        if "seq" not in [r[1] for r in conn.execute("PRAGMA main.table_info(%s)" % t)]:
            conn.execute("ALTER TABLE main.%s ADD COLUMN seq INTEGER NOT NULL DEFAULT 0" % t)
    conn.executescript(SERVER_SCHEMA)
    if not conn.execute("SELECT 1 FROM main.meta WHERE key='sync_epoch'").fetchone():
        with conn:
            conn.execute("INSERT OR IGNORE INTO main.meta(key,value) VALUES('sync_epoch',?)", (os.urandom(8).hex(),))


def remote_state(conn, source):
    """What the server has, and what it accepts from this pusher."""
    ensure_schema(conn)
    meta = dict(conn.execute("SELECT key, value FROM main.meta WHERE key IN "
                             "('sync_static_hash','sync_push_ts','sync_epoch')"))
    has_static = bool(meta.get("sync_static_hash"))
    return {"protocol": PROTOCOL, "epoch": meta.get("sync_epoch"), "you": source["name"],
            "static_hash": meta.get("sync_static_hash"),
            "accept_static": source["owner"] or not has_static, "accept_cfg": source["owner"],
            "last_push_ts": int(meta["sync_push_ts"]) if meta.get("sync_push_ts") else None,
            "scans": [list(r) for r in conn.execute(
                "SELECT realm, faction, ts, raw_kept, diffed FROM main.ah_scans ORDER BY ts")]}


class BadPayload(ValueError):
    pass


def _fixed(rows, n, what):
    if not isinstance(rows, list):
        raise BadPayload("%s must be a list" % what)
    for r in rows:
        if not isinstance(r, list) or len(r) != n:
            raise BadPayload("%s: every row must have %d columns" % (what, n))
        for v in r:
            if isinstance(v, (dict, list)):
                raise BadPayload("%s: nested values are not allowed" % what)
    return rows


def _ins(cur, table, cols, rows, verb="INSERT"):
    if rows:
        cur.executemany("%s INTO main.%s(%s) VALUES(%s)" % (verb, table, ",".join(cols), ",".join("?" * len(cols))),
                        rows)


def apply(conn, cfg, payload, source):
    """Apply one pushed batch atomically, merged with what other pushers sent.

    source: {"name", "owner": bool, "key_id": int|None}. `cfg` (the live server config) gets the owner's settings."""
    if not isinstance(payload, dict) or payload.get("protocol") != PROTOCOL:
        raise BadPayload("unsupported sync protocol - update the AH Tool on this PC")
    ensure_schema(conn)
    applied = {}
    new_cfg = None
    cur = conn.cursor()
    cur.execute("BEGIN IMMEDIATE")
    try:
        r = cur.execute("SELECT value FROM main.meta WHERE key='sync_seq'").fetchone()
        seq = (int(r[0]) if r and str(r[0]).isdigit() else 0) + 1
        cur.execute("INSERT OR REPLACE INTO main.meta(key,value) VALUES('sync_seq',?)", (str(seq),))
        if "cfg" in payload and source["owner"]:
            c = payload["cfg"]
            if not isinstance(c, dict):
                raise BadPayload("cfg must be an object")
            new_cfg = {k: c[k] for k in CFG_KEYS if isinstance(c.get(k), (int, float)) and not isinstance(c[k], bool)}
            cur.execute("INSERT OR REPLACE INTO main.meta(key,value) VALUES('sync_cfg',?)", (json.dumps(new_cfg),))
        if "meta" in payload:
            m = payload["meta"]
            if not isinstance(m, dict):
                raise BadPayload("meta must be an object")
            for k in META_MAX_KEYS:
                try:
                    v = int(m[k])
                except (KeyError, TypeError, ValueError):
                    continue
                old = cur.execute("SELECT value FROM main.meta WHERE key=?", (k,)).fetchone()
                if not old or not old[0].isdigit() or int(old[0]) < v:
                    cur.execute("INSERT OR REPLACE INTO main.meta(key,value) VALUES(?,?)", (k, str(v)))
        st = payload.get("static")
        if st is not None:
            tables = (st or {}).get("tables")
            if not isinstance(tables, dict) or set(tables) != set(STATIC_TABLES):
                raise BadPayload("static must contain exactly: %s" % ", ".join(STATIC_TABLES))
            has = cur.execute("SELECT value FROM main.meta WHERE key='sync_static_hash'").fetchone()
            if source["owner"] or not (has and has[0]):
                for t, cols in STATIC_TABLES.items():
                    rows = _fixed(tables[t], len(cols), t)
                    cur.execute("DELETE FROM main.%s" % t)
                    _ins(cur, t, cols, rows)
                cur.execute("INSERT OR REPLACE INTO main.meta(key,value) VALUES('sync_static_hash',?)",
                            (str(st.get("hash") or ""),))
                tv = (payload.get("meta") or {}).get("tooltip_version")
                if isinstance(tv, (str, int)):
                    cur.execute("INSERT OR REPLACE INTO main.meta(key,value) VALUES('tooltip_version',?)", (str(tv),))
                applied["static"] = sum(len(v) for v in tables.values())
            else:
                applied["static_ignored"] = True

        before = conn.total_changes
        realms = {}
        if "price_current" in payload:
            rows = _fixed(payload["price_current"], len(PRICE_CURRENT), "price_current")
            # newest observation wins; unchanged rows are not touched, so they do not count as new data
            cur.executemany(
                "INSERT INTO main.price_current(realm,item_id,price,available,last_day,updated_at,seq) "
                "VALUES(?,?,?,?,?,?,%d) "
                "ON CONFLICT(realm,item_id) DO UPDATE SET price=excluded.price, available=excluded.available, "
                "last_day=excluded.last_day, updated_at=excluded.updated_at, seq=excluded.seq " % seq +
                "WHERE (COALESCE(excluded.last_day,0), COALESCE(excluded.updated_at,0)) >= "
                "(COALESCE(price_current.last_day,0), COALESCE(price_current.updated_at,0)) "
                "AND (excluded.price IS NOT price_current.price OR excluded.available IS NOT price_current.available "
                "OR excluded.last_day IS NOT price_current.last_day)", rows)
            for r in rows:
                realms[r[0]] = realms.get(r[0], 0) + 1
        if "price_daily" in payload:
            rows = _fixed(payload["price_daily"], len(PRICE_DAILY), "price_daily")
            cur.executemany(
                "INSERT INTO main.price_daily(realm,item_id,day,low,high,available,seq) VALUES(?,?,?,?,?,?,%d) "
                "ON CONFLICT(realm,item_id,day) DO UPDATE SET seq=excluded.seq, %s" % (seq, MERGE_DAILY), rows)
            applied["price_daily"] = len(rows)
            for r in rows:
                realms.setdefault(r[0], 0)
        if "snapshots" in payload:
            rows = _fixed(payload["snapshots"], len(SNAPSHOTS), "snapshots")
            _ins(cur, "snapshots", SNAPSHOTS, rows, "INSERT OR IGNORE")
            applied["snapshots"] = len(rows)
        changed = conn.total_changes - before
        if changed:
            # One ingest row per push that changed prices: the analytics cache and the per-browser alert check
            # key on MAX(ingests.id).
            realm = max(realms, key=lambda k: realms[k]) if realms else None
            cur.execute("INSERT INTO main.ingests(ts,file_mtime,realm,items,changed) VALUES(?,NULL,?,?,?)",
                        (int(time.time()), realm, realms.get(realm, 0), changed))

        def scan_id(key):
            if not isinstance(key, list) or len(key) != 3:
                raise BadPayload("scan key must be [realm, faction, ts]")
            r = cur.execute("SELECT id FROM main.ah_scans WHERE realm=? AND faction=? AND ts=?", key).fetchone()
            return r[0] if r else None

        def put_sales(sid, realm, faction, ts, rows):
            rows = _fixed(rows, len(SALES), "sales")
            cur.execute("DELETE FROM main.ah_sales WHERE scan_id=?", (sid,))
            out = []
            for prev_ts, iid, suffix, count, unit, owner, kind in rows:
                prev = scan_id([realm, faction, prev_ts]) if prev_ts is not None else None
                out.append((sid, prev, ts, iid, suffix, count, unit, owner or "", kind))
            cur.executemany("INSERT INTO main.ah_sales(scan_id,prev_scan_id,ts,item_id,suffix,count,unit_price,owner,"
                            "kind) VALUES(?,?,?,?,?,?,?,?,?)", out)

        for s in payload.get("scans") or []:
            row = _fixed([s.get("scan")], len(SCAN), "scan")[0] if isinstance(s, dict) else None
            if row is None:
                raise BadPayload("scans must be objects")
            ts, realm, faction = row[0], row[1], row[2]
            if scan_id([realm, faction, ts]) is not None:
                continue
            # char is always blank here: the scanning character belongs to the pusher
            cur.execute("INSERT INTO main.ah_scans(ts,realm,faction,char,source,total,n,partial,raw_kept,diffed,seq) "
                        "VALUES(?,?,?,'',?,?,?,?,?,?,?)", row + [seq])
            sid = cur.lastrowid
            items = _fixed(s.get("items") or [], len(SCAN_ITEMS), "scan items")
            cur.executemany("INSERT INTO main.ah_scan_items(scan_id,%s) VALUES(?,%s)"
                            % (",".join(SCAN_ITEMS), ",".join("?" * len(SCAN_ITEMS))), [[sid] + r for r in items])
            auctions = _fixed(s.get("auctions") or [], len(AUCTIONS), "auctions")
            cur.executemany("INSERT INTO main.ah_auctions(scan_id,%s) VALUES(?,%s)"
                            % (",".join(AUCTIONS), ",".join("?" * len(AUCTIONS))), [[sid] + r for r in auctions])
            put_sales(sid, realm, faction, ts, s.get("sales") or [])
            applied["scans"] = applied.get("scans", 0) + 1
            applied["auctions"] = applied.get("auctions", 0) + len(auctions)
        for u in payload.get("scan_updates") or []:
            if not isinstance(u, dict):
                raise BadPayload("scan_updates must be objects")
            sid = scan_id(u.get("key"))
            if sid is None:
                continue
            realm, faction, ts = u["key"]
            if u.get("drop_raw"):
                cur.execute("DELETE FROM main.ah_auctions WHERE scan_id=?", (sid,))
                cur.execute("UPDATE main.ah_scans SET raw_kept=0, diffed=1, seq=? WHERE id=?", (seq, sid))
            if "sales" in u:
                put_sales(sid, realm, faction, ts, u["sales"])
                cur.execute("UPDATE main.ah_scans SET diffed=1, seq=? WHERE id=?", (seq, sid))
        # Never hold more raw auctions than the owner's app does, whoever pushed them.
        keep = int((new_cfg or cfg or {}).get("logger_keep_raw_scans") or 6)
        for realm, faction in cur.execute("SELECT DISTINCT realm, faction FROM main.ah_scans").fetchall():
            for (sid,) in cur.execute("SELECT id FROM main.ah_scans WHERE realm=? AND faction=? AND raw_kept=1 "
                                      "ORDER BY ts DESC LIMIT -1 OFFSET ?", (realm, faction, max(2, keep))).fetchall():
                cur.execute("DELETE FROM main.ah_auctions WHERE scan_id=?", (sid,))
                cur.execute("UPDATE main.ah_scans SET raw_kept=0, seq=? WHERE id=?", (seq, sid))
        now = int(time.time())
        cur.execute("INSERT OR REPLACE INTO main.meta(key,value) VALUES('sync_push_ts',?)", (str(now),))
        if source.get("key_id"):
            cur.execute("UPDATE main.sync_keys SET last_push_ts=?, pushes=pushes+1 WHERE id=?", (now, source["key_id"]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if new_cfg and cfg is not None:
        cfg.update(new_cfg)
    return {"ok": True, "applied": applied}


PULL_LIMIT = 50000


def pull_page(conn, cursor, limit=PULL_LIMIT):
    """Server: rows changed after `cursor`, oldest change first, at most ~limit rows per table."""
    ensure_schema(conn)
    c = cursor if isinstance(cursor, dict) else {}

    def pair(k):
        v = c.get(k)
        try:
            return int(v[0]), int(v[1])
        except (TypeError, ValueError, IndexError):
            return -1, -1

    limit = max(100, min(int(limit or PULL_LIMIT), PULL_LIMIT))
    epoch = conn.execute("SELECT value FROM main.meta WHERE key='sync_epoch'").fetchone()[0]
    out = {"protocol": PROTOCOL, "epoch": epoch, "cursor": dict(c), "more": False}

    def page(key, cols, table):
        rows = conn.execute("SELECT seq, rowid, %s FROM main.%s WHERE (seq, rowid) > (?, ?) ORDER BY seq, rowid "
                            "LIMIT ?" % (",".join(cols), table), pair(key) + (limit,)).fetchall()
        if rows:
            out["cursor"][key] = [rows[-1][0], rows[-1][1]]
        out["more"] = out["more"] or len(rows) >= limit
        return [list(r[2:]) for r in rows]

    out["price_daily"] = page("pd", PRICE_DAILY, "price_daily")
    out["price_current"] = page("pc", PRICE_CURRENT, "price_current")
    try:
        sn = int(c.get("sn", -1))
    except (TypeError, ValueError):
        sn = -1
    rows = conn.execute("SELECT rowid, %s FROM main.snapshots WHERE rowid>? ORDER BY rowid LIMIT ?"
                        % ",".join(SNAPSHOTS), (sn, limit)).fetchall()
    if rows:
        out["cursor"]["sn"] = rows[-1][0]
    out["more"] = out["more"] or len(rows) >= limit
    out["snapshots"] = [list(r[1:]) for r in rows]

    scans, budget = [], limit
    for r in conn.execute("SELECT seq, id, %s FROM main.ah_scans WHERE (seq, id) > (?, ?) ORDER BY seq, id"
                          % ",".join(SCAN), pair("sc")).fetchall():
        if scans and budget <= 0:
            out["more"] = True
            break
        sid, row = r[1], list(r[2:])
        item = {"scan": row,
                "items": _rows(conn, "SELECT %s FROM main.ah_scan_items WHERE scan_id=?" % ",".join(SCAN_ITEMS),
                               (sid,)),
                "auctions": _rows(conn, "SELECT %s FROM main.ah_auctions WHERE scan_id=?" % ",".join(AUCTIONS),
                                  (sid,)) if row[7] else None,
                "sales": _rows(conn, "SELECT p.ts, s.item_id, s.suffix, s.count, s.unit_price, s.owner, s.kind "
                                     "FROM main.ah_sales s LEFT JOIN main.ah_scans p ON p.id=s.prev_scan_id "
                                     "WHERE s.scan_id=?", (sid,))}
        budget -= len(item["items"]) + len(item["auctions"] or []) + len(item["sales"]) + 1
        scans.append(item)
        out["cursor"]["sc"] = [r[0], r[1]]
    out["scans"] = scans
    return out


# ================================================================ client: pulling from the web version

def _pull_key(cfg):
    return "pull:" + hashlib.sha1((cfg.get("sync_url") or "").rstrip("/").encode()).hexdigest()[:16]


def apply_pull(conn, page, keep_raw=6):
    """Merge one pulled page into the local DB. Returns {"prices": changed rows, "scans": new/updated scans}."""
    cur = conn.cursor()
    cur.execute("BEGIN IMMEDIATE")
    try:
        before = conn.total_changes
        rows = _fixed(page.get("price_current") or [], len(PRICE_CURRENT), "price_current")
        cur.executemany(
            "INSERT INTO price_current(realm,item_id,price,available,last_day,updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(realm,item_id) DO UPDATE SET price=excluded.price, available=excluded.available, "
            "last_day=excluded.last_day, updated_at=excluded.updated_at "
            "WHERE (COALESCE(excluded.last_day,0), COALESCE(excluded.updated_at,0)) >= "
            "(COALESCE(price_current.last_day,0), COALESCE(price_current.updated_at,0)) "
            "AND (excluded.price IS NOT price_current.price OR excluded.available IS NOT price_current.available "
            "OR excluded.last_day IS NOT price_current.last_day)", rows)
        realms = {}
        for r in rows:
            realms[r[0]] = realms.get(r[0], 0) + 1
        rows = _fixed(page.get("price_daily") or [], len(PRICE_DAILY), "price_daily")
        cur.executemany("INSERT INTO price_daily(realm,item_id,day,low,high,available) VALUES(?,?,?,?,?,?) "
                        "ON CONFLICT(realm,item_id,day) DO UPDATE SET " + MERGE_DAILY, rows)
        for r in rows:
            realms.setdefault(r[0], 0)
        rows = _fixed(page.get("snapshots") or [], len(SNAPSHOTS), "snapshots")
        cur.executemany("INSERT INTO snapshots(realm,item_id,ts,price,available) SELECT ?,?,?,?,? WHERE NOT EXISTS "
                        "(SELECT 1 FROM snapshots WHERE realm=? AND item_id=? AND ts=?)",
                        [r + r[:3] for r in rows])
        prices = conn.total_changes - before

        def local_scan(realm, faction, ts):
            return cur.execute("SELECT id, COALESCE(source,''), raw_kept FROM ah_scans WHERE realm=? AND faction=? "
                               "AND ts=?", (realm, faction, ts)).fetchone()

        def put_sales(sid, realm, faction, ts, sales):
            cur.execute("DELETE FROM ah_sales WHERE scan_id=?", (sid,))
            out = []
            for prev_ts, iid, suffix, count, unit, owner, kind in _fixed(sales or [], len(SALES), "sales"):
                prev = local_scan(realm, faction, prev_ts) if prev_ts is not None else None
                out.append((sid, prev[0] if prev else None, ts, iid, suffix, count, unit, owner or "", kind))
            cur.executemany("INSERT INTO ah_sales(scan_id,prev_scan_id,ts,item_id,suffix,count,unit_price,owner,kind) "
                            "VALUES(?,?,?,?,?,?,?,?,?)", out)

        n_scans = 0
        for s in page.get("scans") or []:
            row = _fixed([s.get("scan")], len(SCAN), "scan")[0]
            ts, realm, faction = row[0], row[1], row[2]
            have = local_scan(realm, faction, ts)
            if have and have[1] != "web":
                continue  # your own scan coming back: yours is the original
            auctions = s.get("auctions")
            if have:
                sid = have[0]
                if auctions is None and have[2]:
                    cur.execute("DELETE FROM ah_auctions WHERE scan_id=?", (sid,))
                    cur.execute("UPDATE ah_scans SET raw_kept=0 WHERE id=?", (sid,))
            else:
                cur.execute("INSERT INTO ah_scans(ts,realm,faction,char,source,total,n,partial,raw_kept,diffed) "
                            "VALUES(?,?,?,'','web',?,?,?,?,1)", (ts, realm, faction, row[4], row[5], row[6],
                                                                1 if auctions is not None else 0))
                sid = cur.lastrowid
                items = _fixed(s.get("items") or [], len(SCAN_ITEMS), "scan items")
                cur.executemany("INSERT INTO ah_scan_items(scan_id,%s) VALUES(?,%s)"
                                % (",".join(SCAN_ITEMS), ",".join("?" * len(SCAN_ITEMS))), [[sid] + r for r in items])
                cur.executemany("INSERT INTO ah_auctions(scan_id,%s) VALUES(?,%s)"
                                % (",".join(AUCTIONS), ",".join("?" * len(AUCTIONS))),
                                [[sid] + r for r in _fixed(auctions or [], len(AUCTIONS), "auctions")])
            put_sales(sid, realm, faction, ts, s.get("sales"))
            n_scans += 1
        if n_scans:  # pulled raw auctions are kept like your own: the newest few per realm/faction
            for realm, faction in cur.execute("SELECT DISTINCT realm, faction FROM ah_scans WHERE source='web'"
                                              ).fetchall():
                for (sid,) in cur.execute("SELECT id FROM ah_scans WHERE realm=? AND faction=? AND source='web' AND "
                                          "raw_kept=1 ORDER BY ts DESC LIMIT -1 OFFSET ?",
                                          (realm, faction, max(2, keep_raw))).fetchall():
                    cur.execute("DELETE FROM ah_auctions WHERE scan_id=?", (sid,))
                    cur.execute("UPDATE ah_scans SET raw_kept=0 WHERE id=?", (sid,))
        if prices:
            # file_mtime NULL: ingest.py compares the Auctionator file only with its own imports
            realm = max(realms, key=lambda k: realms[k]) if realms else None
            cur.execute("INSERT INTO ingests(ts,file_mtime,realm,items,changed) VALUES(?,NULL,?,?,?)",
                        (int(time.time()), realm, realms.get(realm, 0), prices))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"prices": prices, "scans": n_scans}


def pull(conn, cfg, full=False, max_pages=1000):
    """Fetch what everyone pushed since the last pull. Returns {"prices", "scans", "pages"}."""
    if not cfg.get("sync_url") or not cfg.get("sync_token"):
        raise SyncError("sync_url and sync_token must be set in config.json")
    r = conn.execute("SELECT value FROM meta WHERE key=?", (_pull_key(cfg),)).fetchone()
    try:
        st = json.loads(r[0]) if r and not full else {}
    except ValueError:
        st = {}
    total = {"prices": 0, "scans": 0, "pages": 0}
    keep = int(cfg.get("logger_keep_raw_scans") or 6)
    for _ in range(max_pages):
        q = urllib.parse.urlencode({"cursor": json.dumps(st.get("cursor") or {}, separators=(",", ":"))})
        page = _request(cfg, "GET", "/api/sync/pull?" + q)
        if page.get("protocol") != PROTOCOL:
            raise SyncError("the web version speaks sync protocol %s, this app %s" % (page.get("protocol"), PROTOCOL))
        if st.get("epoch") not in (None, page.get("epoch")):
            st = {}  # the web database was reset: start over
            continue
        res = apply_pull(conn, page, keep)
        total["prices"] += res["prices"]
        total["scans"] += res["scans"]
        total["pages"] += 1
        st = {"epoch": page.get("epoch"), "cursor": page.get("cursor") or {}}
        with conn:
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", (_pull_key(cfg), json.dumps(st)))
        if not page.get("more"):
            break
    return total


def load_pushed_cfg(conn, cfg):
    """Server start: reuse the owner's analysis settings from the last push."""
    r = conn.execute("SELECT value FROM main.meta WHERE key='sync_cfg'").fetchone()
    if r:
        try:
            cfg.update({k: v for k, v in json.loads(r[0]).items() if k in CFG_KEYS})
        except (ValueError, AttributeError):
            pass


# ================================================================ CLI (local app)

def main(argv=None):
    import argparse
    from . import config, db
    ap = argparse.ArgumentParser(description="Push market data to the AH Tool web version.")
    ap.add_argument("--full", action="store_true", help="resend everything (repairs a web version that drifted)")
    ap.add_argument("--dry-run", action="store_true", help="show what would be sent, send nothing")
    ap.add_argument("--push-only", action="store_true", help="only send your data")
    ap.add_argument("--pull-only", action="store_true", help="only fetch other players' data")
    a = ap.parse_args(argv)
    cfg = config.load()
    conn = db.connect(cfg["db_path"])
    try:
        if a.dry_run:
            remote = _request(cfg, "GET", "/api/sync/state") if cfg.get("sync_url") else {}
            batches, _ = build_batches(conn, cfg, remote, full=a.full, mine=local_state(conn, cfg))
            for i, b in enumerate(batches):
                size = len(gzip.compress(json.dumps(b, separators=(",", ":")).encode()))
                print("batch %d: %s, %.1f kB gzip" % (i + 1, ", ".join(k for k in b if k != "protocol"),
                                                      size / 1024))
            if remote.get("you"):
                print("The web version knows this PC as: %s" % remote["you"])
            print("Your characters (masked as seller): %s" % (", ".join(sorted(own_names(conn, cfg))) or "none"))
            return 0
        if not a.pull_only:
            print("Pushed: %s" % _summary(push(conn, cfg, full=a.full)))
        if not a.push_only:
            res = pull(conn, cfg, full=a.full)
            print("Pulled: %d price rows, %d scans from other players (%d page(s))."
                  % (res["prices"], res["scans"], res["pages"]))
        return 0
    except SyncError as e:
        print("Push failed: %s" % e, file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
