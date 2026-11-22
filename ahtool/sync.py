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

Merging (server): every key's prices are kept apart (price_daily_src, price_current_src); price_daily shows the
lowest low / highest high / highest available over all keys per realm+item+day, price_current the newest observation
per item that is not dated in the future. Snapshots are unique by realm/item/ts, scans by realm/faction/ts per key.
`ahkey purge` deletes a key's rows and recomputes the merge from what the others sent. Analysis settings and
the static item/recipe data come only from the owner (AHTOOL_SYNC_TOKEN or an owner key), also while the web version
has none. Every row is validated column by column (see SPEC below), on the server and when pulling; the server
remembers which key pushed each row (src_key), so only that key or an owner can change a scan later, and
`ahkey purge` can remove a key's data.

Incremental: each PC remembers in its own DB what it has pushed to which web version (meta `sync:<url>`), tied to
the web database's random `epoch`, so a reset web database gets everything again. `python -m ahtool.sync --full`
resends everything.

Two-way: every PC also pulls what the others pushed (GET /api/sync/pull). The server stamps every row a push changes
with that push's sequence number (`seq`), and each PC remembers the last one it has (meta `pull:<url>`), so only new
rows travel. Pulled prices are merged with the same rules as on the server and remembered (sync_pulled_*), so they
are never pushed back as this PC's own; pulled scans are stored with source='web', are never pushed back, never
diffed against your own scans (their sales come with them), and give way to your own scan with the same key.
"""
import gzip
import hashlib
import json
import math
import os
import re
import sqlite3
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
# Server only: which sync key pushed (or last changed) a row, so `ahkey purge` can remove a key's data and only the
# key that pushed a scan (or an owner) may change it later. 0 = AHTOOL_SYNC_TOKEN, NULL = pushed before this existed.
SRC_TABLES = ("price_daily", "price_current", "snapshots", "ah_scans")
SERVER_SCHEMA = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_snapshots ON snapshots(realm, item_id, ts);
CREATE INDEX IF NOT EXISTS ix_price_daily_seq ON price_daily(seq);
CREATE INDEX IF NOT EXISTS ix_price_current_seq ON price_current(seq);
CREATE INDEX IF NOT EXISTS ix_ah_scans_seq ON ah_scans(seq);
CREATE TABLE IF NOT EXISTS sync_keys(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
    hash TEXT NOT NULL UNIQUE, role TEXT NOT NULL DEFAULT 'contributor', created_at INTEGER NOT NULL,
    revoked_at INTEGER, last_push_ts INTEGER, pushes INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sync_scan_deletes(id INTEGER PRIMARY KEY AUTOINCREMENT, seq INTEGER NOT NULL,
    origin INTEGER NOT NULL, realm TEXT, faction TEXT, ts INTEGER);
"""


class SyncError(Exception):
    pass


class BadPayload(ValueError):
    pass


# ================================================================ row validation (server apply + client apply_pull)
#
# SQLite (non-STRICT tables) stores whatever type it is given, so every pushed/pulled row is checked column by column
# before it touches a database: ints are real ints (no bool/float/str) within sane ranges, strings are short, nothing
# lies in the future. A batch with one bad row is refused as a whole. The client uses the same rules to leave out
# local rows the server would refuse, so one odd local row never blocks a whole push.

INT32 = 2 ** 31 - 1
# copper. The game's gold cap is 2^31-1 copper (~214k gold): no buyout, bid or unit price can be higher. Keeping
# prices (and STACK below) this small also keeps sums like SUM(count*unit_price) far away from SQLite's 2^63 limit.
PRICE_MAX = INT32
QTY_MAX = 10 ** 9
STACK_MAX = 10000             # items in one auction / sale (real stacks are at most a few hundred)
TS_MIN = 1_000_000_000        # 2001: older timestamps are garbage
DAY_EPOCH_TS = 1577836800     # 2020-01-01 UTC: day 0 of Auctionator's day numbers
FUTURE_TS_SLACK = 300         # a pushed timestamp may be at most 5 minutes in the future (clock skew)
SCAN_TS_SLACK = 60            # scans are pushed after they happened: at most 1 minute of clock skew
PULL_TS_SLACK = 3600          # pulled rows: the server already checked them, allow more skew between PCs
MAX_STR = 64
SERVER_MAX_ROWS = 2 * MAX_BATCH_ROWS  # per pushed batch (static data excluded); one huge scan may exceed the client cap
MAX_SCANS_PER_BATCH = 50      # new scans stored per pushed batch; the rest waits for the next push
STATIC_MAX_ROWS = 2_000_000
CURSOR_MAX = 2 ** 63 - 1
ICON_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def _I(lo, hi, null=False):
    return ("i", lo, hi, null)


def _S(hi=MAX_STR, null=False, lo=0):
    return ("s", lo, hi, null)


def _N(lo, hi, null=False):  # int or finite float
    return ("n", lo, hi, null)


ID, IDn = _I(1, INT32), _I(1, INT32, True)
INTn = _I(-INT32, INT32, True)
TS, TSn = _I(TS_MIN, "ts"), _I(TS_MIN, "ts", True)
DAY = _I(0, "day")
PRICE, PRICEn, PRICE0n = _I(1, PRICE_MAX), _I(1, PRICE_MAX, True), _I(0, PRICE_MAX, True)
QTY, QTYn = _I(0, QTY_MAX), _I(0, QTY_MAX, True)
STACK = _I(1, STACK_MAX)
FLAG = _I(0, 1)
REALM = _S(lo=1)

SPEC = {
    "price_daily": (REALM, ID, DAY, PRICEn, PRICEn, QTYn),
    "price_current": (REALM, ID, PRICE, QTYn, _I(0, "day", True), TSn),
    "snapshots": (REALM, ID, TS, PRICEn, QTYn),
    "scan": (TS, _S(), _S(32), _S(32, True), QTYn, QTYn, FLAG, FLAG, FLAG),
    "scan items": (ID, QTY, QTY, PRICE0n, PRICE0n, QTYn),
    "auctions": (ID, INTn, STACK, PRICE0n, PRICE0n, PRICE0n, _I(0, 10, True), _S(null=True)),
    "sales": (TSn, ID, INTn, STACK, PRICE0n, _S(null=True), _S(32)),
    "items": (ID, _S(255, True), INTn, INTn, INTn, INTn, INTn, PRICE0n, PRICE0n, INTn),
    "spells": (ID, _S(255, True)),
    "recipes": (ID, INTn, IDn, _N(0, 10 ** 6, True), INTn, INTn, INTn),
    "recipe_reagents": (ID, ID, _I(0, 10 ** 6, True)),
    "vendor_items": (ID, PRICE0n, _S(32, True)),
    "item_tooltip": (ID, _S(255, True), _S(1 << 20, True)),
}


def _limits(slack=FUTURE_TS_SLACK, now=None):
    """ts/day: the latest timestamp / Auctionator day a row may carry. today: the current UTC day (a price_current row
    dated later is "from the future" and never beats one that is not, see _CURRENT_NEWER)."""
    now = int(time.time() if now is None else now)
    return {"ts": now + slack, "day": (now + slack - DAY_EPOCH_TS) // 86400 + 1, "now": now,
            "today": (now - DAY_EPOCH_TS) // 86400}


def _bad_value(v, spec, lim):
    """-> None if v fits spec, else a short reason."""
    kind, lo, hi, null = spec
    if v is None:
        return None if null else "missing value"
    if isinstance(hi, str):
        hi = lim[hi]
    if kind == "s":
        if not isinstance(v, str):
            return "text expected"
        return None if lo <= len(v) <= hi else "text length must be %d-%d" % (lo, hi)
    if isinstance(v, bool):
        return "number expected"
    if kind == "n" and isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            return "number must be finite"
    elif not isinstance(v, int):
        return "integer expected"
    return None if lo <= v <= hi else "out of range %s..%s" % (lo, hi)


def _bad_row(r, specs, lim):
    if not isinstance(r, (list, tuple)) or len(r) != len(specs):
        return "every row must have %d columns" % len(specs)
    for i, (v, sp) in enumerate(zip(r, specs)):
        err = _bad_value(v, sp, lim)
        if err:
            return "column %d: %s (%r)" % (i + 1, err, v if not isinstance(v, str) else v[:40])
    return None


def _checked(rows, what, lim, spec=None):
    """Every row valid, or BadPayload for the whole batch. -> rows as lists."""
    specs = spec or SPEC[what]
    if not isinstance(rows, list):
        raise BadPayload("%s must be a list" % what)
    for r in rows:
        err = _bad_row(r, specs, lim)
        if err:
            raise BadPayload("%s: %s" % (what, err))
    return [list(r) for r in rows]


def _valid_rows(rows, what, lim=None):
    """Client: leave out local rows the server would refuse."""
    lim = lim or _limits()
    return [r for r in rows if _bad_row(r, SPEC[what], lim) is None]


def _safe_icon(v):
    return v if isinstance(v, str) and len(v) <= 128 and ICON_RE.match(v) else None


def _meta_int(v, lim):
    """A pushed meta timestamp -> int clamped to now (+slack), or None."""
    if isinstance(v, str) and v.isdigit() and len(v) <= 12:
        v = int(v)
    if isinstance(v, bool) or not isinstance(v, int) or v < TS_MIN:
        return None
    return min(v, lim["ts"])


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

    # --- Auctionator prices (rows the server would refuse - wrong types, future dates - stay local). Rows that are
    # still exactly what was pulled from the web version are not sent back as this PC's own: they belong to whoever
    # pushed them, and echoing them would keep e.g. purged prices alive under this PC's key.
    _ensure_client_schema(conn)
    lim = _limits()
    since = mine.get("snap_ts")
    snaps = _rows(conn, "SELECT s.realm, s.item_id, s.ts, s.price, s.available FROM snapshots s WHERE s.ts>? AND NOT "
                        "EXISTS (SELECT 1 FROM sync_pulled_snap p WHERE p.realm=s.realm AND p.item_id=s.item_id AND "
                        "p.ts=s.ts) ORDER BY s.ts", (since if since is not None else -1,))
    if snaps and isinstance(snaps[-1][2], int):
        new_state["snap_ts"] = min(snaps[-1][2], lim["ts"])
    current = _valid_rows(_rows(
        conn, "SELECT c.realm, c.item_id, c.price, c.available, c.last_day, c.updated_at FROM price_current c "
              "LEFT JOIN sync_pulled_current p ON p.realm=c.realm AND p.item_id=c.item_id WHERE p.realm IS NULL "
              "OR c.price IS NOT p.price OR c.available IS NOT p.available OR c.last_day IS NOT p.last_day "
              "OR c.updated_at IS NOT p.updated_at"), "price_current", lim)
    snaps = _valid_rows(snaps, "snapshots", lim)
    batches.append({"price_current": current[:MAX_BATCH_ROWS], "snapshots": []})
    for i in range(MAX_BATCH_ROWS, len(current), MAX_BATCH_ROWS):
        batches.append({"price_current": current[i:i + MAX_BATCH_ROWS]})
    for i in range(0, len(snaps), MAX_BATCH_ROWS):
        batches.append({"snapshots": snaps[i:i + MAX_BATCH_ROWS]})

    daily = []
    for realm, mx in conn.execute("SELECT realm, MAX(day) FROM price_daily GROUP BY realm").fetchall():
        wm = (mine.get("daily") or {}).get(realm)
        frm = wm - DAY_OVERLAP if isinstance(wm, int) else -10 ** 9
        # a value that is the one pulled from the web version is left out (NULL): the server keeps this PC's own
        rows = _rows(conn, "SELECT d.realm, d.item_id, d.day, "
                           "CASE WHEN d.low IS p.low THEN NULL ELSE d.low END, "
                           "CASE WHEN d.high IS p.high THEN NULL ELSE d.high END, "
                           "CASE WHEN d.available IS p.available THEN NULL ELSE d.available END "
                           "FROM price_daily d LEFT JOIN sync_pulled_daily p ON p.realm=d.realm AND "
                           "p.item_id=d.item_id AND p.day=d.day WHERE d.realm=? AND d.day>=? ORDER BY d.day, "
                           "d.item_id", (realm, frm))
        daily.extend(_valid_rows([r for r in rows if r[3] is not None or r[4] is not None or r[5] is not None],
                                 "price_daily", lim))
        new_state["daily"][realm] = min(mx, lim["day"]) if isinstance(mx, int) else mx
    for i in range(0, len(daily), MAX_BATCH_ROWS):
        batches.append({"price_daily": daily[i:i + MAX_BATCH_ROWS]})

    # --- AH Tool Logger scans
    have = {(r[0], r[1], r[2]): (r[3], r[4]) for r in remote.get("scans") or []}
    # only your own scans: pulled ones (source 'web') came from the server and belong to whoever scanned them
    scans = [s for s in conn.execute("SELECT id, %s FROM ah_scans WHERE COALESCE(source,'')<>'web' ORDER BY ts"
                                     % ",".join(SCAN)).fetchall() if _bad_row(list(s[1:]), SPEC["scan"], lim) is None]
    key_of = {s[0]: (s[2], s[3], s[1]) for s in scans}  # id -> (realm, faction, ts)
    # the server takes a scan at most SCAN_TS_SLACK in the future: a newer one (this PC's clock is ahead) is sent
    # with a later push, as the server does not list it as pushed yet
    scans = [s for s in scans if s[1] <= lim["now"] + SCAN_TS_SLACK]

    def sales_for(sid):
        out = []
        for r in conn.execute("SELECT prev_scan_id, item_id, suffix, count, unit_price, owner, kind FROM ah_sales "
                              "WHERE scan_id=?", (sid,)):
            prev = key_of.get(r[0])
            out.append([prev[2] if prev else None, r[1], r[2], r[3], r[4], _mask(r[5], own), r[6]])
        return _valid_rows(out, "sales", lim)

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
                "items": _valid_rows(_rows(conn, "SELECT %s FROM ah_scan_items WHERE scan_id=?" % ",".join(SCAN_ITEMS),
                                           (sid,)), "scan items", lim),
                "auctions": [], "sales": sales_for(sid)}
        if raw_kept:
            item["auctions"] = _valid_rows([r[:7] + [_mask(r[7], own)] for r in _rows(
                conn, "SELECT %s FROM ah_auctions WHERE scan_id=?" % ",".join(AUCTIONS), (sid,))], "auctions", lim)
        n = len(item["items"]) + len(item["auctions"]) + len(item["sales"])
        if cur and (cur_rows + n > MAX_BATCH_ROWS or len(cur) >= MAX_SCANS_PER_BATCH):
            batches.append({"scans": cur})
            cur, cur_rows = [], 0
        cur.append(item)
        cur_rows += n
    if cur:
        batches.append({"scans": cur})
    cur, cur_rows = [], 0
    for u in updates:
        n = len(u.get("sales") or []) + 1
        if cur and cur_rows + n > MAX_BATCH_ROWS:
            batches.append({"scan_updates": cur})
            cur, cur_rows = [], 0
        cur.append(u)
        cur_rows += n
    if cur:
        batches.append({"scan_updates": cur})

    batches[0] = dict(head, **batches[0])
    for b in batches[1:]:
        b["protocol"] = PROTOCOL
    return batches, new_state


# Client only: what was last pulled from the web version per row, so a row that still holds exactly that is known to
# be someone else's (not pushed back as this PC's own; replaced by the server's newer merged value on the next pull).
CLIENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_pulled_current(realm TEXT, item_id INTEGER, price INTEGER, available INTEGER,
    last_day INTEGER, updated_at INTEGER, PRIMARY KEY(realm, item_id));
CREATE TABLE IF NOT EXISTS sync_pulled_daily(realm TEXT, item_id INTEGER, day INTEGER, low INTEGER, high INTEGER,
    available INTEGER, PRIMARY KEY(realm, item_id, day));
CREATE TABLE IF NOT EXISTS sync_pulled_snap(realm TEXT, item_id INTEGER, ts INTEGER, PRIMARY KEY(realm, item_id, ts));
"""


def _ensure_client_schema(conn):
    # ah_scans.sync_origin: the web version's id of a pulled scan (NULL: your own, or pulled before this existed)
    cols = [r[1] for r in conn.execute("PRAGMA main.table_info(ah_scans)")]
    if cols and "sync_origin" not in cols:
        with conn:
            conn.execute("ALTER TABLE main.ah_scans ADD COLUMN sync_origin INTEGER")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sync_pulled_snap'").fetchone():
        return
    conn.executescript(CLIENT_SCHEMA)
    with conn:
        # an app that pulled before these tables existed pulls everything once more, so they get filled
        conn.execute("DELETE FROM meta WHERE key LIKE 'pull:%'")


MAX_RESPONSE = 256 << 20  # one answer from the web version (a pull page is far smaller)
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: it would carry the sync key (Authorization) to wherever it points."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SyncError("%s %s -> HTTP %d redirect to %s - refusing to follow it with the sync key; set sync_url to "
                        "the final address" % (req.get_method(), req.full_url, code, newurl))


_opener = urllib.request.build_opener(_NoRedirect)


def _check_url(base):
    u = urllib.parse.urlsplit(base)
    if u.scheme == "https" and u.hostname:
        return
    if u.scheme == "http" and (u.hostname or "").lower() in _LOCAL_HOSTS:
        return
    raise SyncError("sync_url must start with https:// (plain http only for localhost): %s" % base)


def _read_capped(r, limit=MAX_RESPONSE):
    chunks, n = [], 0
    while True:
        b = r.read(1 << 20)
        if not b:
            return b"".join(chunks)
        n += len(b)
        if n > limit:
            raise SyncError("the web version answered with more than %d MB - refusing it" % (limit >> 20))
        chunks.append(b)


def _request(cfg, method, path, payload=None, timeout=180):
    base = (cfg.get("sync_url") or "").rstrip("/")
    _check_url(base)
    url = base + path
    headers = {"Authorization": "Bearer %s" % cfg.get("sync_token", ""), "Accept": "application/json",
               "User-Agent": "AHTool-sync/%d" % PROTOCOL}
    data = None
    if payload is not None:
        data = gzip.compress(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"), 6)
        headers["Content-Type"] = "application/gzip"
    waited, attempt = 0.0, 0
    while True:
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with _opener.open(req, timeout=timeout) as r:
                body = _read_capped(r)
            break
        except urllib.error.HTTPError as e:
            if e.code == 503:
                # the web version handles one push at a time: wait (Retry-After, else backoff) and try again
                delay = _retry_delay(e.headers.get("Retry-After") if e.headers else None, attempt)
                if waited + delay <= BUSY_MAX_WAIT:
                    e.close()
                    _sleep(delay)
                    waited += delay
                    attempt += 1
                    continue
            _http_error(method, url, e)
        except (urllib.error.URLError, OSError) as e:
            raise SyncError("%s %s failed: %s" % (method, url, getattr(e, "reason", e)))
    try:
        out = json.loads(body.decode("utf-8"), parse_constant=_no_constant)
        if not isinstance(out, dict):
            raise ValueError("not an object")
        return out
    except ValueError:
        # typically a login page: the web version's host is behind a login that does not let the sync path through
        raise SyncError("%s %s did not answer with JSON - is the site behind a login?" % (method, url))


BUSY_MAX_WAIT = 60.0  # seconds a request waits in total for a busy (503) web version before giving up
_sleep = time.sleep


def _retry_delay(retry_after, attempt):
    """Seconds to wait before retrying a 503: Retry-After (seconds, 0.5..30), else 1, 2, 4, ... 16 s."""
    try:
        v = float(retry_after) if retry_after is not None and len(str(retry_after)) <= 8 else None
    except ValueError:
        v = None
    if v is None or not math.isfinite(v):
        v = min(2 ** attempt, 16)
    return max(0.5, min(v, 30.0))


def _http_error(method, url, e):
    try:
        body = e.read(300).decode("utf-8", "replace")
    except Exception:  # noqa
        body = ""
    try:
        body = json.loads(body).get("error", body)
    except (ValueError, AttributeError):
        pass
    raise SyncError("%s %s -> HTTP %d: %s" % (method, url, e.code, body))


def _no_constant(name):
    raise ValueError("%s is not valid JSON" % name)


def loads_strict(raw):
    """JSON without NaN/Infinity (Python's json accepts them by default)."""
    return json.loads(raw, parse_constant=_no_constant)


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

# meta sync_schema: 3 = per-key price tables, scans unique per key, invalid rows swept; 4 = deleted scans are recorded
# (sync_scan_deletes) and scans that share their realm/faction/time with another key's are sent once more
SCHEMA_VERSION = 4
LEGACY_SRC = -1     # src_key in the per-key tables for rows pushed before src_key existed

SERVER_SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS price_daily_src(realm TEXT, item_id INTEGER, day INTEGER, src_key INTEGER NOT NULL,
    low INTEGER, high INTEGER, available INTEGER, PRIMARY KEY(realm, item_id, day, src_key));
CREATE INDEX IF NOT EXISTS ix_price_daily_src_key ON price_daily_src(src_key);
CREATE TABLE IF NOT EXISTS price_current_src(realm TEXT, item_id INTEGER, src_key INTEGER NOT NULL, price INTEGER,
    available INTEGER, last_day INTEGER, updated_at INTEGER, PRIMARY KEY(realm, item_id, src_key));
CREATE INDEX IF NOT EXISTS ix_price_current_src_key ON price_current_src(src_key);
CREATE INDEX IF NOT EXISTS ix_ah_scans_seq ON ah_scans(seq);
CREATE INDEX IF NOT EXISTS ix_ah_scans_key ON ah_scans(realm, faction, ts);
CREATE INDEX IF NOT EXISTS ix_ah_scans_src ON ah_scans(src_key);
CREATE UNIQUE INDEX IF NOT EXISTS ux_ah_scans_src ON ah_scans(realm, faction, ts, COALESCE(src_key, -1));
CREATE INDEX IF NOT EXISTS ix_ingests_src ON ingests(src_key);
"""
_SCANS_DDL = ("CREATE TABLE main.%s(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, realm TEXT, faction TEXT, "
              "char TEXT, source TEXT, total INTEGER, n INTEGER, partial INTEGER NOT NULL DEFAULT 0, "
              "raw_kept INTEGER NOT NULL DEFAULT 1, diffed INTEGER NOT NULL DEFAULT 0, seq INTEGER NOT NULL DEFAULT 0, "
              "src_key INTEGER)")


def _log(msg):
    print("sync: %s" % msg, file=sys.stderr, flush=True)


def ensure_schema(conn):
    """Server: columns, tables and indexes the web version needs, and the one-time migration of an older web DB.

    Runs at start (run_web.py) and before every sync request (cheap once the DB is current). Upgrading to schema 3:
    scans become unique per (realm, faction, ts, key) instead of globally, prices get per-key tables (each key's own
    rows; price_daily / price_current hold the merge of them), and rows stored before validation existed are swept
    (sweep_invalid)."""
    try:
        r = conn.execute("SELECT value FROM main.meta WHERE key='sync_schema'").fetchone()
    except sqlite3.OperationalError:
        r = None
    if r and r[0] == str(SCHEMA_VERSION):
        return
    # Server only: which push last changed a row (seq, for /api/sync/pull) and which key pushed it (src_key). The
    # local app's tables never get them. Added with ALTER TABLE so an existing web database keeps working.
    for cols, tables, ddl in (("seq", SEQ_TABLES, "seq INTEGER NOT NULL DEFAULT 0"),
                              ("src_key", SRC_TABLES + ("ingests",), "src_key INTEGER")):
        for t in tables:
            if cols not in [r[1] for r in conn.execute("PRAGMA main.table_info(%s)" % t)]:
                conn.execute("ALTER TABLE main.%s ADD COLUMN %s" % (t, ddl))
    conn.executescript(SERVER_SCHEMA)
    cur = conn.cursor()
    cur.execute("BEGIN IMMEDIATE")
    try:
        r = cur.execute("SELECT value FROM main.meta WHERE key='sync_schema'").fetchone()
        if not (r and r[0] == str(SCHEMA_VERSION)):
            if not (r and str(r[0]).isdigit() and int(r[0]) >= 3):
                _migrate_v3(cur)
            _migrate_v4(cur)
            cur.execute("INSERT OR IGNORE INTO main.meta(key,value) VALUES('sync_epoch',?)", (os.urandom(8).hex(),))
            cur.execute("INSERT OR REPLACE INTO main.meta(key,value) VALUES('sync_schema',?)", (str(SCHEMA_VERSION),))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _migrate_v3(cur):
    sql = cur.execute("SELECT sql FROM main.sqlite_master WHERE type='table' AND name='ah_scans'").fetchone()[0]
    if "UNIQUE" in re.sub(r"\s+", "", sql).upper():
        # the local app's ah_scans is UNIQUE(realm, faction, ts): on the server two keys may each have a scan there
        cols = [r[1] for r in cur.execute("PRAGMA main.table_info(ah_scans)")]
        keep = ",".join(c for c in ("id",) + ("ts", "realm", "faction", "char") + SCAN[3:] + ("seq", "src_key")
                        if c in cols)
        cur.execute("DROP TABLE IF EXISTS main.ah_scans_v3")
        cur.execute(_SCANS_DDL % "ah_scans_v3")
        cur.execute("INSERT INTO main.ah_scans_v3(%s) SELECT %s FROM main.ah_scans" % (keep, keep))
        cur.execute("DROP TABLE main.ah_scans")
        cur.execute("ALTER TABLE main.ah_scans_v3 RENAME TO ah_scans")
    for stmt in SERVER_SCHEMA_V3.strip().split(";"):
        if stmt.strip():
            cur.execute(stmt)
    # Every merged row becomes a LEGACY_SRC row. Its src_key only named the key that last changed it: the values are
    # the merge of everyone (the owner included), so filing them under that key would let `ahkey purge` of it delete
    # the owner's values too. (Versions before this fix did file them under that key; see README, "Purging a key".)
    cur.execute("INSERT OR IGNORE INTO main.price_daily_src(realm,item_id,day,src_key,low,high,available) "
                "SELECT realm,item_id,day,?,low,high,available FROM main.price_daily", (LEGACY_SRC,))
    cur.execute("INSERT OR IGNORE INTO main.price_current_src(realm,item_id,src_key,price,available,last_day,"
                "updated_at) SELECT realm,item_id,?,price,available,last_day,updated_at "
                "FROM main.price_current", (LEGACY_SRC,))
    _sweep(cur)
    _set_owner_realms(cur, _owner_realms_from_data(cur))


def _migrate_v4(cur):
    """Scans that share (realm, faction, ts) with another key's scan get a new seq: PCs fetch them again and then
    hold the one the server now sends for that key (the owner's), repairing copies a clashing scan overwrote.

    The v3 migration filed legacy merged price rows under the key that last changed them; they cannot be told apart
    from that key's own pushes any more, so they stay (documented in README: re-push from the owner after a purge)."""
    clash = cur.execute("SELECT realm, faction, ts FROM main.ah_scans GROUP BY realm, faction, ts "
                        "HAVING COUNT(*) > 1").fetchall()
    if clash:
        seq = _next_seq(cur)
        cur.executemany("UPDATE main.ah_scans SET seq=? WHERE realm IS ? AND faction IS ? AND ts IS ?",
                        [(seq,) + tuple(k) for k in clash])


# ---------------------------------------------------------------- rows stored before validation existed

def sweep_invalid(conn):
    """Server: delete every stored row that the current SPEC refuses (rows pushed before validation existed: string
    ids, huge numbers, future dates, ...), so they can never break a pull or an aggregate. -> {table: rows deleted}.

    Runs once by itself when ensure_schema upgrades a web DB (run_web.py calls ensure_schema at start); can be run
    again any time."""
    ensure_schema(conn)
    cur = conn.cursor()
    cur.execute("BEGIN IMMEDIATE")
    try:
        out = _sweep(cur)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return out


def _bad_rowids(cur, table, cols, spec, lim, key="rowid"):
    return [r[0] for r in cur.execute("SELECT %s, %s FROM main.%s" % (key, ",".join(cols), table))
            if _bad_row(list(r[1:]), spec, lim)]


def _delete_ids(cur, table, ids, key="rowid"):
    n = 0
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        n += cur.execute("DELETE FROM main.%s WHERE %s IN (%s)" % (table, key, ",".join("?" * len(part))),
                         part).rowcount
    return n


def _sweep(cur):
    lim = _limits()
    out = {}
    _touch_reset(cur)
    for table, cols, spec, keys_tbl, nkey in (
            ("price_daily", PRICE_DAILY, SPEC["price_daily"], "_pd_keys", 3),
            ("price_daily_src", PRICE_DAILY, SPEC["price_daily"], "_pd_keys", 3),
            ("price_current", PRICE_CURRENT, SPEC["price_current"], "_pc_keys", 2),
            ("price_current_src", PRICE_CURRENT, SPEC["price_current"], "_pc_keys", 2),
            ("snapshots", SNAPSHOTS, SPEC["snapshots"], None, 0)):
        bad = _bad_rowids(cur, table, cols, spec, lim)
        if not bad:
            continue
        if keys_tbl:
            # merged rows of a valid realm/item(/day) are rebuilt from the per-key rows that are left
            for i in range(0, len(bad), 500):
                part = bad[i:i + 500]
                cur.execute("INSERT OR IGNORE INTO temp.%s SELECT %s FROM main.%s WHERE rowid IN (%s) AND "
                            "typeof(realm)='text' AND typeof(item_id)='integer'%s"
                            % (keys_tbl, ",".join(cols[:nkey]), table, ",".join("?" * len(part)),
                               " AND typeof(day)='integer'" if nkey == 3 else ""), part)
        out[table] = _delete_ids(cur, table, bad)
    seq = None
    if out.get("price_daily") or out.get("price_daily_src") or out.get("price_current") or out.get("price_current_src"):
        seq = _next_seq(cur)
        _recompute_daily(cur, seq)
        _recompute_current(cur, seq, lim)
    # scans: a bad scan row goes with everything that belongs to it; bad child rows go alone
    bad_scans = _bad_rowids(cur, "ah_scans", SCAN, SPEC["scan"], lim, key="id")
    if bad_scans:
        out["ah_scans"] = _drop_scans(cur, bad_scans, out)
    for table, cols, what in (("ah_scan_items", SCAN_ITEMS, "scan items"), ("ah_auctions", AUCTIONS, "auctions")):
        bad = _bad_rowids(cur, table, cols, SPEC[what], lim)
        if bad:
            out[table] = out.get(table, 0) + _delete_ids(cur, table, bad)
    bad = [r[0] for r in cur.execute(
        "SELECT s.rowid, p.ts, s.item_id, s.suffix, s.count, s.unit_price, s.owner, s.kind FROM main.ah_sales s "
        "LEFT JOIN main.ah_scans p ON p.id=s.prev_scan_id") if _bad_row(list(r[1:]), SPEC["sales"], lim)]
    if bad:
        out["ah_sales"] = out.get("ah_sales", 0) + _delete_ids(cur, "ah_sales", bad)
    if any(out.values()):
        _log("removed stored rows that fail validation: %s" % ", ".join("%d %s" % (n, t) for t, n in out.items() if n))
        cur.execute("INSERT INTO main.ingests(ts,file_mtime,realm,items,changed) VALUES(?,NULL,NULL,0,?)",
                    (int(time.time()), sum(out.values())))
    return out


def _drop_scans(cur, sids, out):
    """Delete scans with everything that belongs to them. Each deletion is recorded (sync_scan_deletes) so PCs that
    pulled the scan delete their copy, and the remaining scans with the same realm/faction/time get a new seq: one of
    them may now be the one the server sends for that key (pull_page), and PCs must fetch it."""
    sids = list(sids)
    if not sids:
        return 0
    gone = []
    for i in range(0, len(sids), 500):
        part = sids[i:i + 500]
        sq = ",".join("?" * len(part))
        gone += cur.execute("SELECT id, realm, faction, ts FROM main.ah_scans WHERE id IN (%s)" % sq, part).fetchall()
        for t in ("ah_auctions", "ah_scan_items", "ah_sales"):
            out[t] = out.get(t, 0) + cur.execute("DELETE FROM main.%s WHERE scan_id IN (%s)" % (t, sq),
                                                 part).rowcount
        cur.execute("UPDATE main.ah_sales SET prev_scan_id=NULL WHERE prev_scan_id IN (%s)" % sq, part)
    n = _delete_ids(cur, "ah_scans", sids, key="id")
    if gone:
        seq = _next_seq(cur)
        cur.executemany("INSERT INTO main.sync_scan_deletes(seq, origin, realm, faction, ts) VALUES(?,?,?,?,?)",
                        [(seq,) + tuple(g) for g in gone])
        cur.executemany("UPDATE main.ah_scans SET seq=? WHERE realm IS ? AND faction IS ? AND ts IS ?",
                        [(seq,) + tuple(k) for k in {tuple(g[1:]) for g in gone}])
    return n


# ---------------------------------------------------------------- per-key prices -> merged prices
#
# price_daily_src / price_current_src hold each key's own rows. price_daily / price_current (what the site shows and
# what /api/sync/pull sends) are rebuilt from them for every realm/item(/day) a push or a purge touched:
#   price_daily:   lowest low, highest high, highest available over all keys
#   price_current: the newest observation - (last_day, updated_at) - among the keys' rows that are not from the
#                  future (last_day after today UTC, or updated_at after now; updated_at is clamped to the time the
#                  server received it). A row from the future only shows while there is nothing else.
# So `ahkey purge` removes exactly that key's values: the merge is recomputed from what the other keys sent.

def _touch_reset(cur):
    cur.execute("CREATE TEMP TABLE IF NOT EXISTS _pd_keys(realm TEXT, item_id INTEGER, day INTEGER, "
                "PRIMARY KEY(realm, item_id, day))")
    cur.execute("CREATE TEMP TABLE IF NOT EXISTS _pc_keys(realm TEXT, item_id INTEGER, PRIMARY KEY(realm, item_id))")
    cur.execute("DELETE FROM temp._pd_keys")
    cur.execute("DELETE FROM temp._pc_keys")


def _next_seq(cur):
    r = cur.execute("SELECT value FROM main.meta WHERE key='sync_seq'").fetchone()
    seq = (int(r[0]) if r and str(r[0]).isdigit() else 0) + 1
    cur.execute("INSERT OR REPLACE INTO main.meta(key,value) VALUES('sync_seq',?)", (str(seq),))
    return seq


def _changes(cur):
    return cur.connection.total_changes


def _recompute_daily(cur, seq):
    """Merged price_daily for every key in temp._pd_keys. -> merged rows inserted/changed/deleted."""
    n0 = _changes(cur)
    cur.execute(
        "INSERT INTO main.price_daily(realm,item_id,day,low,high,available,seq,src_key) "
        "SELECT s.realm, s.item_id, s.day, MIN(s.low), MAX(s.high), MAX(s.available), ?, "
        "CASE WHEN COUNT(DISTINCT s.src_key)=1 THEN NULLIF(MAX(s.src_key), ?) END "
        "FROM main.price_daily_src s JOIN temp._pd_keys k ON k.realm=s.realm AND k.item_id=s.item_id AND k.day=s.day "
        "WHERE 1 GROUP BY s.realm, s.item_id, s.day "
        "ON CONFLICT(realm,item_id,day) DO UPDATE SET low=excluded.low, high=excluded.high, "
        "available=excluded.available, seq=excluded.seq, src_key=excluded.src_key "
        "WHERE price_daily.low IS NOT excluded.low OR price_daily.high IS NOT excluded.high "
        "OR price_daily.available IS NOT excluded.available", (seq, LEGACY_SRC))
    cur.execute("DELETE FROM main.price_daily WHERE (realm, item_id, day) IN (SELECT realm, item_id, day FROM "
                "temp._pd_keys) AND NOT EXISTS (SELECT 1 FROM main.price_daily_src s WHERE s.realm=price_daily.realm "
                "AND s.item_id=price_daily.item_id AND s.day=price_daily.day)")
    return _changes(cur) - n0


def _recompute_current(cur, seq, lim):
    """Merged price_current for every key in temp._pc_keys. -> merged rows inserted/changed/deleted."""
    n0 = _changes(cur)
    cur.execute(
        "INSERT INTO main.price_current(realm,item_id,price,available,last_day,updated_at,seq,src_key) "
        "SELECT realm, item_id, price, available, last_day, updated_at, ?, NULLIF(src_key, ?) FROM ("
        "SELECT s.*, ROW_NUMBER() OVER (PARTITION BY s.realm, s.item_id ORDER BY "
        "(COALESCE(s.last_day,0) <= ? AND COALESCE(s.updated_at,0) <= ?) DESC, COALESCE(s.last_day,0) DESC, "
        "COALESCE(s.updated_at,0) DESC, s.src_key) AS rn "
        "FROM main.price_current_src s JOIN temp._pc_keys k ON k.realm=s.realm AND k.item_id=s.item_id) "
        "WHERE rn=1 "
        "ON CONFLICT(realm,item_id) DO UPDATE SET price=excluded.price, available=excluded.available, "
        "last_day=excluded.last_day, updated_at=excluded.updated_at, seq=excluded.seq, src_key=excluded.src_key "
        "WHERE excluded.price IS NOT price_current.price OR excluded.available IS NOT price_current.available "
        "OR excluded.last_day IS NOT price_current.last_day", (seq, LEGACY_SRC, lim["today"], lim["ts"]))
    cur.execute("DELETE FROM main.price_current WHERE (realm, item_id) IN (SELECT realm, item_id FROM temp._pc_keys) "
                "AND NOT EXISTS (SELECT 1 FROM main.price_current_src s WHERE s.realm=price_current.realm "
                "AND s.item_id=price_current.item_id)")
    return _changes(cur) - n0


# ---------------------------------------------------------------- owners

def _owner_keys_sql(col="src_key"):
    """SQL condition: the row was pushed by an owner (AHTOOL_SYNC_TOKEN = 0, an owner key) or before src_key existed."""
    return ("(%s IS NULL OR %s<=0 OR %s IN (SELECT id FROM main.sync_keys WHERE role='owner'))" % (col, col, col))


def _owner_realms(cur):
    r = cur.execute("SELECT value FROM main.meta WHERE key='sync_owner_realms'").fetchone()
    try:
        v = json.loads(r[0]) if r else []
    except ValueError:
        v = []
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _set_owner_realms(cur, realms):
    cur.execute("INSERT OR REPLACE INTO main.meta(key,value) VALUES('sync_owner_realms',?)",
                (json.dumps(sorted(set(realms))[:MAX_OWNER_REALMS]),))


def _owner_realms_from_data(cur):
    rs = set()
    for t in ("price_current_src", "price_daily_src"):
        rs.update(r[0] for r in cur.execute("SELECT DISTINCT realm FROM main.%s WHERE %s" % (t, _owner_keys_sql()))
                  if isinstance(r[0], str))
    rs.update(r[0] for r in cur.execute("SELECT DISTINCT realm FROM main.ah_scans WHERE %s" % _owner_keys_sql())
              if isinstance(r[0], str))
    return rs


MAX_OWNER_REALMS = 100


def owner_realms(conn):
    """Web version: realms the owner pushed data for (analytics.list_realms shows only these), or None if unknown."""
    try:
        r = conn.execute("SELECT value FROM main.meta WHERE key='sync_owner_realms'").fetchone()
    except sqlite3.OperationalError:
        return None
    return _owner_realms(conn) if r else None


def _src(source):
    """The src_key value stored for rows this pusher sends: its key id, 0 for AHTOOL_SYNC_TOKEN."""
    return int(source.get("key_id") or 0)


def remote_state(conn, source):
    """What the server has, and what it accepts from this pusher. `scans` lists only this pusher's own scans (plus
    those from before src_key existed): another key's scan at the same time never makes a PC skip its own."""
    ensure_schema(conn)
    meta = dict(conn.execute("SELECT key, value FROM main.meta WHERE key IN "
                             "('sync_static_hash','sync_push_ts','sync_epoch')"))
    return {"protocol": PROTOCOL, "epoch": meta.get("sync_epoch"), "you": source["name"],
            "static_hash": meta.get("sync_static_hash"),
            "accept_static": bool(source["owner"]), "accept_cfg": bool(source["owner"]),
            "last_push_ts": int(meta["sync_push_ts"]) if str(meta.get("sync_push_ts") or "").isdigit() else None,
            "scans": [list(r) for r in conn.execute(
                "SELECT realm, faction, ts, raw_kept, diffed FROM main.ah_scans WHERE src_key IS ? OR src_key IS NULL "
                "ORDER BY ts", (_src(source),))]}


def _ins(cur, table, cols, rows, verb="INSERT"):
    if rows:
        cur.executemany("%s INTO main.%s(%s) VALUES(%s)" % (verb, table, ",".join(cols), ",".join("?" * len(cols))),
                        rows)


def _list(v, what):
    if v is None:
        return []
    if not isinstance(v, list):
        raise BadPayload("%s must be a list" % what)
    return v


def _keys_ok(obj, allowed, what):
    if not isinstance(obj, dict):
        raise BadPayload("%s must be objects" % what)
    extra = [k for k in obj if k not in allowed]
    if extra:
        raise BadPayload("%s: unknown field %r" % (what, str(extra[0])[:40]))


PUSH_KEYS = ("protocol", "cfg", "meta", "static", "price_current", "price_daily", "snapshots", "scans",
             "scan_updates")
META_KEYS = META_MAX_KEYS + ("tooltip_version",)


def _count_rows(payload):
    n = 0
    for k in ("price_current", "price_daily", "snapshots"):
        n += len(_list(payload.get(k), k))
    for s in _list(payload.get("scans"), "scans"):
        _keys_ok(s, ("scan", "items", "auctions", "sales"), "scans")
        n += 1 + sum(len(_list(s.get(k), k)) for k in ("items", "auctions", "sales"))
    for u in _list(payload.get("scan_updates"), "scan_updates"):
        _keys_ok(u, ("key", "drop_raw", "sales"), "scan_updates")
        n += 1 + len(_list(u.get("sales"), "sales"))
    return n


SCAN_KEY = (_S(), _S(32), _I(TS_MIN, "ts"))
# json.loads builds one object per value: a few kB of gzip can unpack to millions of "[]," (~700 MB of RAM for 24 MB).
# Every pushed row is a list of at most 9 values, so a contributor's batch (no static data) has at most ~10 commas
# and brackets per row. Owner batches carry the static item/recipe/tooltip data (free text) and are not bounded.
VALUES_PER_ROW = 10
CONTRIBUTOR_MAX_VALUES = SERVER_MAX_ROWS * VALUES_PER_ROW + 10000


def too_many_values(raw, owner):
    """Server, before json.loads: -> an error text if a contributor's batch holds more values than any valid batch
    can, else None. Cheap (three bytes.count over the unpacked body)."""
    if owner:
        return None
    n = raw.count(b",") + raw.count(b"[") + raw.count(b"{")
    if n > CONTRIBUTOR_MAX_VALUES:
        return "sync batch has too many values (%d, at most %d)" % (n, CONTRIBUTOR_MAX_VALUES)
    return None
# price_current: a row replaces another if it is newer by (last_day, updated_at), or if the stored one is "from the
# future" (dated after today UTC / updated after now) - such a row must never block real observations.
_CURRENT_NEWER = ("(COALESCE(excluded.last_day,0), COALESCE(excluded.updated_at,0)) >= "
                  "(COALESCE({t}.last_day,0), COALESCE({t}.updated_at,0)) "
                  "OR COALESCE({t}.last_day,0) > %d OR COALESCE({t}.updated_at,0) > %d")


def apply(conn, cfg, payload, source):
    """Apply one pushed batch atomically, merged with what other pushers sent.

    source: {"name", "owner": bool, "key_id": int|None}. `cfg` (the live server config) gets the owner's settings.
    Every row is validated first (types, ranges, nothing in the future); one bad row refuses the whole batch."""
    if not isinstance(payload, dict) or payload.get("protocol") != PROTOCOL:
        raise BadPayload("unsupported sync protocol - update the AH Tool on this PC")
    _keys_ok(payload, PUSH_KEYS, "sync batch")
    n_rows = _count_rows(payload)
    if n_rows > SERVER_MAX_ROWS:
        raise BadPayload("sync batch has %d rows, at most %d are accepted" % (n_rows, SERVER_MAX_ROWS))
    lim = _limits()
    owner = bool(source.get("owner"))
    src = _src(source)
    ensure_schema(conn)
    applied = {}
    new_cfg = None
    cur = conn.cursor()
    cur.execute("BEGIN IMMEDIATE")
    try:
        seq = _next_seq(cur)
        if "cfg" in payload and owner:
            c = payload["cfg"]
            if not isinstance(c, dict) or len(c) > 64:
                raise BadPayload("cfg must be an object")
            new_cfg = {k: c[k] for k in CFG_KEYS if isinstance(c.get(k), (int, float)) and not isinstance(c[k], bool)
                       and math.isfinite(c[k]) and abs(c[k]) < 1e9}
            cur.execute("INSERT OR REPLACE INTO main.meta(key,value) VALUES('sync_cfg',?)", (json.dumps(new_cfg),))
        if "meta" in payload:
            m = payload["meta"]
            _keys_ok(m, META_KEYS, "meta")
            for k in META_MAX_KEYS:
                v = _meta_int(m.get(k), lim)  # never later than now: a future value would win forever
                if v is None:
                    continue
                old = cur.execute("SELECT value FROM main.meta WHERE key=?", (k,)).fetchone()
                if not old or not str(old[0]).isdigit() or int(old[0]) < v or int(old[0]) > lim["ts"]:
                    cur.execute("INSERT OR REPLACE INTO main.meta(key,value) VALUES(?,?)", (k, str(v)))
        st = payload.get("static")
        if st is not None:
            if not owner:
                # item/recipe/tooltip data only ever comes from an owner, even while the web version has none
                applied["static_ignored"] = True
            else:
                tables = st.get("tables") if isinstance(st, dict) else None
                if not isinstance(tables, dict) or set(tables) != set(STATIC_TABLES):
                    raise BadPayload("static must contain exactly: %s" % ", ".join(STATIC_TABLES))
                h = st.get("hash")
                if not isinstance(h, str) or not HASH_RE.match(h):
                    raise BadPayload("static hash missing or invalid")
                if sum(len(_list(v, "static")) for v in tables.values()) > STATIC_MAX_ROWS:
                    raise BadPayload("static data too large")
                checked = {t: _checked(tables[t], t, lim) for t in STATIC_TABLES}
                for row in checked["item_tooltip"]:
                    row[1] = _safe_icon(row[1])  # the icon name ends up in an image URL
                for t, cols in STATIC_TABLES.items():
                    cur.execute("DELETE FROM main.%s" % t)
                    _ins(cur, t, cols, checked[t])
                cur.execute("INSERT OR REPLACE INTO main.meta(key,value) VALUES('sync_static_hash',?)", (h,))
                tv = (payload.get("meta") or {}).get("tooltip_version")
                if (isinstance(tv, str) and len(tv) <= MAX_STR) or (isinstance(tv, int) and not isinstance(tv, bool)):
                    cur.execute("INSERT OR REPLACE INTO main.meta(key,value) VALUES('tooltip_version',?)", (str(tv),))
                applied["static"] = sum(len(v) for v in checked.values())

        changed = 0
        realms = {}
        _touch_reset(cur)
        if "price_current" in payload:
            rows = _checked(payload["price_current"], "price_current", lim)
            for r in rows:
                if r[5] is not None and r[5] > lim["now"]:
                    r[5] = lim["now"]  # "observed in the future" is clamped to when the server received it
            # this key's own row: its newest observation (a row from the future is always replaced)
            cur.executemany(
                "INSERT INTO main.price_current_src(realm,item_id,price,available,last_day,updated_at,src_key) "
                "VALUES(?,?,?,?,?,?,%d) ON CONFLICT(realm,item_id,src_key) DO UPDATE SET price=excluded.price, "
                "available=excluded.available, last_day=excluded.last_day, updated_at=excluded.updated_at WHERE "
                % src + _CURRENT_NEWER.format(t="price_current_src") % (lim["today"], lim["ts"]), rows)
            cur.executemany("INSERT OR IGNORE INTO temp._pc_keys VALUES(?,?)", [r[:2] for r in rows])
            changed += _recompute_current(cur, seq, lim)
            for r in rows:
                realms[r[0]] = realms.get(r[0], 0) + 1
        if "price_daily" in payload:
            rows = _checked(payload["price_daily"], "price_daily", lim)
            cur.executemany(
                "INSERT INTO main.price_daily_src(realm,item_id,day,low,high,available,src_key) "
                "VALUES(?,?,?,?,?,?,%d) ON CONFLICT(realm,item_id,day,src_key) DO UPDATE SET %s"
                % (src, MERGE_DAILY.replace("price_daily.", "price_daily_src.")), rows)
            cur.executemany("INSERT OR IGNORE INTO temp._pd_keys VALUES(?,?,?)", [r[:3] for r in rows])
            changed += _recompute_daily(cur, seq)
            applied["price_daily"] = len(rows)
            for r in rows:
                realms.setdefault(r[0], 0)
        if "snapshots" in payload:
            rows = _checked(payload["snapshots"], "snapshots", lim)
            n0 = _changes(cur)
            _ins(cur, "snapshots", SNAPSHOTS + ("src_key",), [r + [src] for r in rows], "INSERT OR IGNORE")
            changed += _changes(cur) - n0
            applied["snapshots"] = len(rows)
            for r in rows:
                realms.setdefault(r[0], 0)

        def scan_row(key, mine_only=False):
            """-> (id, src_key) of the scan with that key: this pusher's own, else (owners) one from before src_key,
            else (owners, mine_only=False) any."""
            if not isinstance(key, list) or _bad_row(key, SCAN_KEY, lim):
                raise BadPayload("scan key must be [realm, faction, ts]")
            rows = cur.execute("SELECT id, src_key FROM main.ah_scans WHERE realm=? AND faction=? AND ts=? "
                               "ORDER BY src_key IS ? DESC, src_key IS NULL DESC, id", key + [src]).fetchall()
            for r in rows:
                if r[1] == src or r[1] is None or (owner and not mine_only):
                    return r
            return rows[0] if rows and not mine_only else None

        def put_sales(sid, realm, faction, ts, rows):
            cur.execute("DELETE FROM main.ah_sales WHERE scan_id=?", (sid,))
            out = []
            for prev_ts, iid, suffix, count, unit, owner_, kind in rows:
                prev = scan_row([realm, faction, prev_ts]) if prev_ts is not None else None
                out.append((sid, prev[0] if prev else None, ts, iid, suffix, count, unit, owner_ or "", kind))
            cur.executemany("INSERT INTO main.ah_sales(scan_id,prev_scan_id,ts,item_id,suffix,count,unit_price,owner,"
                            "kind) VALUES(?,?,?,?,?,?,?,?,?)", out)

        scans = []
        for s in _list(payload.get("scans"), "scans"):
            row = _checked([s.get("scan")], "scan", lim)[0]
            if row[0] > lim["now"] + SCAN_TS_SLACK:
                # the PC's clock is a little ahead: store it once its time has come (the PC sends it again, as the
                # server does not list it as this key's yet); the rest of the batch is applied
                applied["scans_deferred"] = applied.get("scans_deferred", 0) + 1
                continue
            scans.append((row, _checked(_list(s.get("items"), "scan items"), "scan items", lim),
                          _checked(_list(s.get("auctions"), "auctions"), "auctions", lim),
                          _checked(_list(s.get("sales"), "sales"), "sales", lim)))
        for row, items, auctions, sales in scans:
            ts, realm, faction = row[0], row[1], row[2]
            if scan_row([realm, faction, ts], mine_only=True) is not None:
                continue  # this key already pushed it (or it is from before src_key)
            if applied.get("scans", 0) >= MAX_SCANS_PER_BATCH:
                # the PC sends the rest again with its next push (it only skips scans the server lists as its own)
                applied["scans_deferred"] = applied.get("scans_deferred", 0) + 1
                continue
            # char is always blank here: the scanning character belongs to the pusher
            cur.execute("INSERT INTO main.ah_scans(ts,realm,faction,char,source,total,n,partial,raw_kept,diffed,seq,"
                        "src_key) VALUES(?,?,?,'',?,?,?,?,?,?,?,?)", row + [seq, src])
            sid = cur.lastrowid
            cur.executemany("INSERT INTO main.ah_scan_items(scan_id,%s) VALUES(?,%s)"
                            % (",".join(SCAN_ITEMS), ",".join("?" * len(SCAN_ITEMS))), [[sid] + r for r in items])
            cur.executemany("INSERT INTO main.ah_auctions(scan_id,%s) VALUES(?,%s)"
                            % (",".join(AUCTIONS), ",".join("?" * len(AUCTIONS))), [[sid] + r for r in auctions])
            put_sales(sid, realm, faction, ts, sales)
            applied["scans"] = applied.get("scans", 0) + 1
            applied["auctions"] = applied.get("auctions", 0) + len(auctions)
            realms.setdefault(realm, 0)
        for u in _list(payload.get("scan_updates"), "scan_updates"):
            sales = _checked(u["sales"], "sales", lim) if "sales" in u else None
            r = scan_row(u.get("key"))
            if r is None:
                continue
            if not owner and r[1] != src:
                # only the key that pushed a scan (or an owner) may drop its raw auctions or replace its sales
                applied["scan_updates_refused"] = applied.get("scan_updates_refused", 0) + 1
                continue
            sid = r[0]
            realm, faction, ts = u["key"]
            if u.get("drop_raw"):
                cur.execute("DELETE FROM main.ah_auctions WHERE scan_id=?", (sid,))
                cur.execute("UPDATE main.ah_scans SET raw_kept=0, diffed=1, seq=? WHERE id=?", (seq, sid))
            if sales is not None:
                put_sales(sid, realm, faction, ts, sales)
                cur.execute("UPDATE main.ah_scans SET diffed=1, seq=? WHERE id=?", (seq, sid))
        # Never hold more raw auctions than the owner's app does - counted per key, so one key's scans never push
        # out another's.
        try:
            keep = max(2, min(int((new_cfg or cfg or {}).get("logger_keep_raw_scans") or 6), 1000))
        except (TypeError, ValueError, OverflowError):
            keep = 6
        for realm, faction, skey in cur.execute("SELECT DISTINCT realm, faction, src_key FROM main.ah_scans "
                                                "WHERE raw_kept=1").fetchall():
            for (sid,) in cur.execute("SELECT id FROM main.ah_scans WHERE realm=? AND faction=? AND src_key IS ? AND "
                                      "raw_kept=1 ORDER BY ts DESC LIMIT -1 OFFSET ?",
                                      (realm, faction, skey, keep)).fetchall():
                cur.execute("DELETE FROM main.ah_auctions WHERE scan_id=?", (sid,))
                cur.execute("UPDATE main.ah_scans SET raw_kept=0, seq=? WHERE id=?", (seq, sid))
        if changed:
            # One ingest row per push that changed prices: the analytics cache and the per-browser alert check
            # key on MAX(ingests.id). A contributor keeps only its newest one, so pushes do not pile up rows.
            price_realms = {k: v for k, v in realms.items() if v}
            realm = max(price_realms, key=lambda k: price_realms[k]) if price_realms else (
                min(realms) if realms else None)
            cur.execute("INSERT INTO main.ingests(ts,file_mtime,realm,items,changed,src_key) VALUES(?,NULL,?,?,?,?)",
                        (int(time.time()), realm, realms.get(realm, 0), changed, src))
            if not owner:
                cur.execute("DELETE FROM main.ingests WHERE src_key=? AND id<?", (src, cur.lastrowid))
            if owner and realm:
                # the web version's default realm follows the owner, never a contributor (analytics.get_realm)
                cur.execute("INSERT OR REPLACE INTO main.meta(key,value) VALUES('sync_owner_realm',?)", (realm,))
        if owner and realms:
            # the realm list of the web version: realms the owner has data for (analytics.list_realms)
            have = _owner_realms(cur)
            if set(realms) - set(have):
                _set_owner_realms(cur, set(have) | set(realms))
        now = int(time.time())
        cur.execute("INSERT OR REPLACE INTO main.meta(key,value) VALUES('sync_push_ts',?)", (str(now),))
        if source.get("key_id"):
            cur.execute("UPDATE main.sync_keys SET last_push_ts=?, pushes=pushes+1 WHERE id=?", (now, source["key_id"]))
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        raise BadPayload("rows conflict: %s" % e)
    except Exception:
        conn.rollback()
        raise
    if new_cfg and cfg is not None:
        cfg.update(new_cfg)
    return {"ok": True, "applied": applied}


def purge_key(conn, key_ids):
    """Server (ahkey purge): delete every row the given sync keys pushed. -> {table: rows deleted}.

    Prices: the keys' own rows are deleted and the merged rows are rebuilt from what the other keys sent (a merged
    row only goes when no other key has a value for it). Rows pushed before src_key existed are not tracked and stay.
    PCs that already pulled the rows keep them, but take the rebuilt rows on their next pull wherever they still hold
    exactly what they pulled. Deleted scans are recorded (sync_scan_deletes): PCs delete their pulled copy, or take
    another key's scan for the same time that is now the one sent (see _drop_scans)."""
    ensure_schema(conn)
    ids = sorted({int(k) for k in key_ids})
    out = {}
    if not ids:
        return out
    q = ",".join("?" * len(ids))
    lim = _limits()
    cur = conn.cursor()
    cur.execute("BEGIN IMMEDIATE")
    try:
        seq = _next_seq(cur)
        sids = [r[0] for r in cur.execute("SELECT id FROM main.ah_scans WHERE src_key IN (%s)" % q, ids)]
        out["ah_scans"] = _drop_scans(cur, sids, out)
        _touch_reset(cur)
        cur.execute("INSERT OR IGNORE INTO temp._pd_keys SELECT realm, item_id, day FROM main.price_daily_src "
                    "WHERE src_key IN (%s)" % q, ids)
        cur.execute("INSERT OR IGNORE INTO temp._pc_keys SELECT realm, item_id FROM main.price_current_src "
                    "WHERE src_key IN (%s)" % q, ids)
        out["price_daily"] = cur.execute("DELETE FROM main.price_daily_src WHERE src_key IN (%s)" % q, ids).rowcount
        out["price_current"] = cur.execute("DELETE FROM main.price_current_src WHERE src_key IN (%s)" % q,
                                           ids).rowcount
        out["merged_rows_changed"] = _recompute_daily(cur, seq) + _recompute_current(cur, seq, lim)
        out["snapshots"] = cur.execute("DELETE FROM main.snapshots WHERE src_key IN (%s)" % q, ids).rowcount
        out["ingests"] = cur.execute("DELETE FROM main.ingests WHERE src_key IN (%s)" % q, ids).rowcount
        _set_owner_realms(cur, _owner_realms_from_data(cur))
        if any(out.values()):
            # a new ingest row makes the running web server drop its cached analytics
            cur.execute("INSERT INTO main.ingests(ts,file_mtime,realm,items,changed) VALUES(?,NULL,NULL,0,?)",
                        (int(time.time()), sum(out.values())))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return out


PULL_LIMIT = 50000
PULL_VERSION = 3   # pull pages: 3 = scans carry their server id ("origin"), large scans are split across pages, deletes
CURSOR_KEYS = ("pd", "pc", "sn", "sc", "sp", "sd")
SCAN_DELETE = (ID, _S(), _S(32), _I(TS_MIN, "ts"))  # [origin, realm, faction, ts]
# Two keys can each have a scan at the same (realm, faction, ts) on the server, but a PC holds one per key. Pulls send
# only one of them: an owner's before a contributor's, else the oldest (lowest id). A contributor can therefore never
# replace the owner's scan on other PCs, nor one another contributor pushed first.
_SCAN_WINNER = ("NOT EXISTS (SELECT 1 FROM main.ah_scans o WHERE o.realm=s.realm AND o.faction=s.faction AND "
                "o.ts=s.ts AND o.id<>s.id AND (%s > %s OR (%s = %s AND o.id < s.id)))"
                % ((_owner_keys_sql("o.src_key"), _owner_keys_sql("s.src_key")) * 2))


def _cursor_int(v, what):
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= CURSOR_MAX:
        raise BadPayload("cursor %s must hold integers 0..2^63-1" % what)
    return v


def pull_page(conn, cursor, limit=PULL_LIMIT, version=None):
    """Server: rows changed after `cursor`, oldest change first, at most ~limit rows per table.

    version (the client's ?v=): PULL_VERSION or later gets pages whose scans part holds at most ~limit rows, a large
    scan split over several pages ({"origin", "cont": true, ...} continues it), plus "scan_deletes". Older clients
    get whole scans (their format), still only one scan per realm/faction/time.

    A stored row that fails validation (left from before it existed) is skipped and logged, never sent: one such row
    would make every PC refuse the page forever."""
    ensure_schema(conn)
    if cursor is None:
        cursor = {}
    if not isinstance(cursor, dict):
        raise BadPayload("cursor must be an object")
    try:
        version = int(version) if isinstance(version, int) or (isinstance(version, str) and version.isdigit()
                                                              and len(version) <= 4) else 0
    except (TypeError, ValueError):
        version = 0
    split = version >= PULL_VERSION
    c = {k: cursor[k] for k in CURSOR_KEYS if k in cursor}

    def pair(k):
        v = c.get(k)
        if v is None:
            return -1, -1
        if not isinstance(v, list) or len(v) != 2:
            raise BadPayload("cursor %s must be [seq, id]" % k)
        return _cursor_int(v[0], k), _cursor_int(v[1], k)

    pairs = {k: pair(k) for k in ("pd", "pc", "sc")}
    sn = -1 if c.get("sn") is None else _cursor_int(c["sn"], "sn")
    sd = -1 if c.get("sd") is None else _cursor_int(c["sd"], "sd")
    sp = c.get("sp")
    if sp is not None:
        if not isinstance(sp, list) or len(sp) != 5:
            raise BadPayload("cursor sp must be [id, seq, items, auctions, sales]")
        sp = [_cursor_int(v, "sp") for v in sp]
    try:
        limit = int(limit) if isinstance(limit, int) or (isinstance(limit, str) and limit.isdigit()
                                                        and len(limit) <= 9) else PULL_LIMIT
    except (TypeError, ValueError):
        limit = PULL_LIMIT
    limit = max(100, min(limit or PULL_LIMIT, PULL_LIMIT))
    lim = _limits()
    epoch = conn.execute("SELECT value FROM main.meta WHERE key='sync_epoch'").fetchone()[0]
    out = {"protocol": PROTOCOL, "epoch": epoch, "cursor": dict(c), "more": False}
    out["cursor"].pop("sp", None)
    if split:
        out["pull_version"] = PULL_VERSION
    skipped = {}

    def ok(rows, what):
        good = [r for r in rows if _bad_row(r, SPEC[what], lim) is None]
        if len(good) != len(rows):
            skipped[what] = skipped.get(what, 0) + len(rows) - len(good)
        return good

    def page(key, cols, table):
        rows = conn.execute("SELECT seq, rowid, %s FROM main.%s WHERE (seq, rowid) > (?, ?) ORDER BY seq, rowid "
                            "LIMIT ?" % (",".join(cols), table), pairs[key] + (limit,)).fetchall()
        if rows:
            out["cursor"][key] = [rows[-1][0], rows[-1][1]]
        out["more"] = out["more"] or len(rows) >= limit
        return ok([list(r[2:]) for r in rows], table)

    out["price_daily"] = page("pd", PRICE_DAILY, "price_daily")
    out["price_current"] = page("pc", PRICE_CURRENT, "price_current")
    rows = conn.execute("SELECT rowid, %s FROM main.snapshots WHERE rowid>? ORDER BY rowid LIMIT ?"
                        % ",".join(SNAPSHOTS), (sn, limit)).fetchall()
    if rows:
        out["cursor"]["sn"] = rows[-1][0]
    out["more"] = out["more"] or len(rows) >= limit
    out["snapshots"] = ok([list(r[1:]) for r in rows], "snapshots")

    if split:
        rows = conn.execute("SELECT id, origin, realm, faction, ts FROM main.sync_scan_deletes WHERE id>? ORDER BY id "
                            "LIMIT ?", (sd, limit)).fetchall()
        if rows:
            out["cursor"]["sd"] = rows[-1][0]
        out["more"] = out["more"] or len(rows) >= limit
        good = [list(r[1:]) for r in rows if _bad_row(list(r[1:]), SCAN_DELETE, lim) is None]
        if len(good) != len(rows):
            skipped["scan deletes"] = len(rows) - len(good)
        out["scan_deletes"] = good

    parts = (("items", "SELECT %s FROM main.ah_scan_items WHERE scan_id=? ORDER BY rowid LIMIT ? OFFSET ?"
              % ",".join(SCAN_ITEMS), "scan items"),
             ("auctions", "SELECT %s FROM main.ah_auctions WHERE scan_id=? ORDER BY rowid LIMIT ? OFFSET ?"
              % ",".join(AUCTIONS), "auctions"),
             ("sales", "SELECT p.ts, s.item_id, s.suffix, s.count, s.unit_price, s.owner, s.kind FROM main.ah_sales s "
                       "LEFT JOIN main.ah_scans p ON p.id=s.prev_scan_id WHERE s.scan_id=? ORDER BY s.rowid "
                       "LIMIT ? OFFSET ?", "sales"))
    scans, budget, first = [], limit, True
    for r in conn.execute("SELECT s.seq, s.id, %s FROM main.ah_scans s WHERE (s.seq, s.id) > (?, ?) AND %s "
                          "ORDER BY s.seq, s.id LIMIT ?" % (",".join("s." + x for x in SCAN), _SCAN_WINNER),
                          pairs["sc"] + (limit + 1,)).fetchall():
        if budget <= 0:
            out["more"] = True
            break
        seq, sid, row = r[0], r[1], list(r[2:])
        resume = first and split and sp is not None and sp[:2] == [sid, seq]
        first = False
        if _bad_row(row, SPEC["scan"], lim):
            skipped["scan"] = skipped.get("scan", 0) + 1
            out["cursor"]["sc"] = [seq, sid]
            budget -= 1
            continue
        item = {"origin": sid}
        if resume:
            item["cont"] = True
        else:
            item["scan"] = row
        offs = list(sp[2:]) if resume else [0, 0, 0]
        done = True
        for i, (name, sql, what) in enumerate(parts):
            if name == "auctions" and not row[7]:
                if not resume:
                    item["auctions"] = None
                continue
            if not split:  # older clients: the whole scan
                item[name] = ok(_rows(conn, sql, (sid, -1, 0)), what)
                budget -= len(item[name])
                continue
            take = max(budget, 0) if done else 0
            got = _rows(conn, sql, (sid, take + 1, offs[i])) if done else []
            if len(got) > take:
                got, done = got[:take], False
            offs[i] += len(got)
            budget -= len(got)
            item[name] = ok(got, what)
        budget -= 1
        scans.append(item)
        if not done:
            item["more"] = True
            out["cursor"]["sp"] = [sid, seq] + offs
            out["more"] = True
            break
        out["cursor"]["sc"] = [seq, sid]
    out["scans"] = scans
    if skipped:
        _log("pull: left out stored rows that fail validation (run sync.sweep_invalid): %s"
             % ", ".join("%d %s" % (n, t) for t, n in skipped.items()))
    return out


# ================================================================ client: pulling from the web version

def _pull_key(cfg):
    return "pull:" + hashlib.sha1((cfg.get("sync_url") or "").rstrip("/").encode()).hexdigest()[:16]


def _mn(a, b):
    return b if a is None else (a if b is None else min(a, b))


def _mx(a, b):
    return b if a is None else (a if b is None else max(a, b))


def _merge3(a, b):
    return (_mn(a[0], b[0]), _mx(a[1], b[1]), _mx(a[2], b[2]))


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def current_is_future(last_day, updated_at, lim=None):
    """A price_current row dated after today (UTC) or updated after now (+slack): it never blocks a real observation."""
    lim = lim or _limits()
    return _num(last_day) > lim["today"] or _num(updated_at) > lim["ts"]


def current_newer(new, old, lim=None):
    """price_current rule, the same on the server, in apply_pull and (should be) in ingest.py: may observation `new`
    replace stored row `old`? Both (price, available, last_day, updated_at). Newest by (last_day, updated_at) wins,
    but a row from the future (current_is_future) never wins over one that is not, and is always replaceable."""
    if old is None or current_is_future(old[2], old[3], lim):
        return True
    if current_is_future(new[2], new[3], lim):
        return False
    return (_num(new[2]), _num(new[3])) >= (_num(old[2]), _num(old[3]))


def apply_pull(conn, page, keep_raw=6):
    """Merge one pulled page into the local DB. Returns {"prices": changed rows, "scans": new/updated scans}.

    Every row is validated like on the server (types, ranges, nothing in the future); one bad row refuses the page
    (BadPayload) and nothing of it is stored.

    Prices: what was pulled is remembered per row (sync_pulled_*). A local row that still holds exactly what was
    pulled last time is someone else's data and simply takes the server's new value (so a purge on the server reaches
    this PC too); a row with your own observations in it is merged with the same rules as on the server."""
    if not isinstance(page, dict):
        raise BadPayload("pull page must be an object")
    lim = _limits(PULL_TS_SLACK)
    pc = _checked(_list(page.get("price_current"), "price_current"), "price_current", lim)
    pd = _checked(_list(page.get("price_daily"), "price_daily"), "price_daily", lim)
    sn = _checked(_list(page.get("snapshots"), "snapshots"), "snapshots", lim)
    scans = []
    for s in _list(page.get("scans"), "scans"):
        if not isinstance(s, dict):
            raise BadPayload("scans must be objects")
        auctions = s.get("auctions")
        origin = s.get("origin")  # the server's id of the scan (servers before PULL_VERSION 3 send none)
        if origin is not None and _bad_value(origin, ID, lim):
            raise BadPayload("scans: origin must be a positive integer")
        cont = s.get("cont") is True  # more rows of a scan an earlier entry started
        if cont and origin is None:
            raise BadPayload("scans: a continued scan needs its origin")
        scans.append((None if cont else _checked([s.get("scan")], "scan", lim)[0],
                      _checked(_list(s.get("items"), "scan items"), "scan items", lim),
                      None if auctions is None else _checked(_list(auctions, "auctions"), "auctions", lim),
                      _checked(_list(s.get("sales"), "sales"), "sales", lim), origin, cont))
    deletes = _checked(_list(page.get("scan_deletes"), "scan_deletes"), "scan deletes", lim, SCAN_DELETE)
    _ensure_client_schema(conn)
    cur = conn.cursor()
    cur.execute("BEGIN IMMEDIATE")
    try:
        prices = 0
        realms = {}
        for realm, iid, price, avail, last_day, upd in pc:
            new = (price, avail, last_day, upd)
            old = cur.execute("SELECT price, available, last_day, updated_at FROM price_current WHERE realm=? AND "
                              "item_id=?", (realm, iid)).fetchone()
            rec = cur.execute("SELECT price, available, last_day, updated_at FROM sync_pulled_current WHERE realm=? "
                              "AND item_id=?", (realm, iid)).fetchone()
            old = tuple(old) if old else None
            if old is None:
                cur.execute("INSERT INTO price_current(realm,item_id,price,available,last_day,updated_at) "
                            "VALUES(?,?,?,?,?,?)", (realm, iid) + new)
                prices += 1
            elif old != new and ((rec is not None and tuple(rec) == old) or current_newer(new, old, lim)):
                cur.execute("UPDATE price_current SET price=?, available=?, last_day=?, updated_at=? WHERE realm=? "
                            "AND item_id=?", new + (realm, iid))
                prices += old[:3] != new[:3]
            cur.execute("INSERT OR REPLACE INTO sync_pulled_current VALUES(?,?,?,?,?,?)", (realm, iid) + new)
            realms[realm] = realms.get(realm, 0) + 1
        for realm, iid, day, low, high, avail in pd:
            new = (low, high, avail)
            old = cur.execute("SELECT low, high, available FROM price_daily WHERE realm=? AND item_id=? AND day=?",
                              (realm, iid, day)).fetchone()
            rec = cur.execute("SELECT low, high, available FROM sync_pulled_daily WHERE realm=? AND item_id=? AND "
                              "day=?", (realm, iid, day)).fetchone()
            old, rec = (tuple(old) if old else None), (tuple(rec) if rec else None)
            if old is None:
                cur.execute("INSERT INTO price_daily(realm,item_id,day,low,high,available) VALUES(?,?,?,?,?,?)",
                            (realm, iid, day) + new)
                prices += 1
                rec = new
            else:
                if rec is not None and rec == old:
                    val, rec = new, new  # only pulled data in it: the server's merge replaces it
                else:
                    val, rec = _merge3(old, new), (new if rec is None else _merge3(rec, new))
                if val != old:
                    cur.execute("UPDATE price_daily SET low=?, high=?, available=? WHERE realm=? AND item_id=? AND "
                                "day=?", val + (realm, iid, day))
                    prices += 1
            cur.execute("INSERT OR REPLACE INTO sync_pulled_daily VALUES(?,?,?,?,?,?)", (realm, iid, day) + rec)
            realms.setdefault(realm, 0)
        n0 = conn.total_changes
        cur.executemany("INSERT INTO snapshots(realm,item_id,ts,price,available) SELECT ?,?,?,?,? WHERE NOT EXISTS "
                        "(SELECT 1 FROM snapshots WHERE realm=? AND item_id=? AND ts=?)",
                        [r + r[:3] for r in sn])
        prices += conn.total_changes - n0
        cur.executemany("INSERT OR IGNORE INTO sync_pulled_snap VALUES(?,?,?)", [r[:3] for r in sn])

        def local_scan(realm, faction, ts):
            return cur.execute("SELECT id, COALESCE(source,''), raw_kept, sync_origin FROM ah_scans WHERE realm=? AND "
                               "faction=? AND ts=?", (realm, faction, ts)).fetchone()

        def sales_rows(sid, realm, faction, ts, sales):
            out = []
            for prev_ts, iid, suffix, count, unit, owner, kind in sales:
                prev = local_scan(realm, faction, prev_ts) if prev_ts is not None else None
                out.append((sid, prev[0] if prev else None, ts, iid, suffix, count, unit, owner or "", kind))
            cur.executemany("INSERT INTO ah_sales(scan_id,prev_scan_id,ts,item_id,suffix,count,unit_price,owner,kind) "
                            "VALUES(?,?,?,?,?,?,?,?,?)", out)

        def put_sales(sid, realm, faction, ts, sales):
            cur.execute("DELETE FROM ah_sales WHERE scan_id=?", (sid,))
            sales_rows(sid, realm, faction, ts, sales)

        def add_rows(sid, items, auctions):
            cur.executemany("INSERT OR REPLACE INTO ah_scan_items(scan_id,%s) VALUES(?,%s)"
                            % (",".join(SCAN_ITEMS), ",".join("?" * len(SCAN_ITEMS))), [[sid] + r for r in items])
            cur.executemany("INSERT INTO ah_auctions(scan_id,%s) VALUES(?,%s)"
                            % (",".join(AUCTIONS), ",".join("?" * len(AUCTIONS))), [[sid] + r for r in auctions or []])

        # scans the web version deleted (ahkey purge, invalid rows): only the pulled copy of exactly that scan goes
        n_deleted = 0
        for origin, realm, faction, ts in deletes:
            have = local_scan(realm, faction, ts)
            if have and have[1] == "web" and have[3] in (None, origin):
                for t in ("ah_auctions", "ah_scan_items", "ah_sales"):
                    cur.execute("DELETE FROM %s WHERE scan_id=?" % t, (have[0],))
                cur.execute("UPDATE ah_sales SET prev_scan_id=NULL WHERE prev_scan_id=?", (have[0],))
                cur.execute("DELETE FROM ah_scans WHERE id=?", (have[0],))
                n_deleted += 1

        n_scans = 0
        for row, items, auctions, sales, origin, cont in scans:
            if cont:
                # the rest of a large scan: appended to the pulled copy of that same scan (if this PC took it)
                have = cur.execute("SELECT id, raw_kept, realm, faction, ts FROM ah_scans WHERE source='web' AND "
                                   "sync_origin=?", (origin,)).fetchone()
                if have:
                    add_rows(have[0], items, auctions if have[1] else None)
                    sales_rows(have[0], have[2], have[3], have[4], sales)
                continue
            ts, realm, faction = row[0], row[1], row[2]
            have = local_scan(realm, faction, ts)
            if have and have[1] != "web":
                continue  # your own scan coming back: yours is the original
            if origin is not None:
                # The server sends one scan per realm/faction/time (the owner's first), so the pulled copy takes
                # whatever it sends now - a scan another key pushed for the same time is never mixed into it.
                if have:
                    sid = have[0]
                    cur.execute("DELETE FROM ah_auctions WHERE scan_id=?", (sid,))
                    cur.execute("DELETE FROM ah_scan_items WHERE scan_id=?", (sid,))
                    cur.execute("UPDATE ah_scans SET char='', total=?, n=?, partial=?, raw_kept=?, diffed=1, "
                                "sync_origin=? WHERE id=?", (row[4], row[5], row[6], 1 if auctions is not None else 0,
                                                            origin, sid))
                else:
                    cur.execute("INSERT INTO ah_scans(ts,realm,faction,char,source,total,n,partial,raw_kept,diffed,"
                                "sync_origin) VALUES(?,?,?,'','web',?,?,?,?,1,?)",
                                (ts, realm, faction, row[4], row[5], row[6], 1 if auctions is not None else 0, origin))
                    sid = cur.lastrowid
                add_rows(sid, items, auctions)
            elif have:  # a server from before PULL_VERSION 3
                sid = have[0]
                if auctions is None and have[2]:
                    cur.execute("DELETE FROM ah_auctions WHERE scan_id=?", (sid,))
                    cur.execute("UPDATE ah_scans SET raw_kept=0 WHERE id=?", (sid,))
            else:
                cur.execute("INSERT INTO ah_scans(ts,realm,faction,char,source,total,n,partial,raw_kept,diffed) "
                            "VALUES(?,?,?,'','web',?,?,?,?,1)", (ts, realm, faction, row[4], row[5], row[6],
                                                                1 if auctions is not None else 0))
                sid = cur.lastrowid
                add_rows(sid, items, auctions)
            put_sales(sid, realm, faction, ts, sales)
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
            # file_mtime NULL: ingest.py compares the Auctionator file only with its own imports. The realm stays the
            # one this PC already shows (analytics.get_realm reads the newest ingest row), so other players' data
            # never switches your default realm; only a PC without any ingest yet takes the pulled realm.
            last = cur.execute("SELECT realm FROM ingests WHERE realm IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
            realm = last[0] if last else (max(realms, key=lambda k: realms[k]) if realms else None)
            cur.execute("INSERT INTO ingests(ts,file_mtime,realm,items,changed) VALUES(?,NULL,?,?,?)",
                        (int(time.time()), realm, realms.get(realm, 0), prices))
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        raise BadPayload("pulled rows conflict: %s" % e)
    except Exception:
        conn.rollback()
        raise
    return {"prices": prices, "scans": n_scans, "scans_deleted": n_deleted}


def pull(conn, cfg, full=False, max_pages=300):
    """Fetch what everyone pushed since the last pull. Returns {"prices", "scans", "pages"}.

    At most `max_pages` pages per call; the rest follows on the next pull (the cursor is saved after each page)."""
    if not cfg.get("sync_url") or not cfg.get("sync_token"):
        raise SyncError("sync_url and sync_token must be set in config.json")
    _ensure_client_schema(conn)
    r = conn.execute("SELECT value FROM meta WHERE key=?", (_pull_key(cfg),)).fetchone()
    try:
        st = json.loads(r[0]) if r and not full else {}
    except ValueError:
        st = {}
    if not isinstance(st, dict):
        st = {}
    total = {"prices": 0, "scans": 0, "pages": 0}
    try:
        keep = max(2, min(int(cfg.get("logger_keep_raw_scans") or 6), 1000))
    except (TypeError, ValueError, OverflowError):
        keep = 6
    for _ in range(max_pages):
        q = urllib.parse.urlencode({"cursor": json.dumps(st.get("cursor") or {}, separators=(",", ":")),
                                    "v": PULL_VERSION})
        page = _request(cfg, "GET", "/api/sync/pull?" + q)
        if page.get("protocol") != PROTOCOL:
            raise SyncError("the web version speaks sync protocol %s, this app %s" % (page.get("protocol"), PROTOCOL))
        if st.get("epoch") not in (None, page.get("epoch")):
            st = {}  # the web database was reset: start over
            continue
        try:
            res = apply_pull(conn, page, keep)
        except BadPayload as e:
            raise SyncError("the web version sent data this app refuses (nothing of that page was stored): %s" % e)
        total["prices"] += res["prices"]
        total["scans"] += res["scans"]
        total["pages"] += 1
        cursor = page.get("cursor")
        st = {"epoch": page.get("epoch"), "cursor": cursor if isinstance(cursor, dict) else {}}
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
