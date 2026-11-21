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
the static item/recipe data come only from the owner (AHTOOL_SYNC_TOKEN or an owner key), also while the web version
has none. Every row is validated column by column (see SPEC below), on the server and when pulling; the server
remembers which key pushed each row (src_key), so only that key or an owner can change a scan later, and
`ahkey purge` can remove a key's data.

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
PRICE_MAX = 10 ** 11          # copper; 10M gold, above any game's gold cap
QTY_MAX = 10 ** 9
TS_MIN = 1_000_000_000        # 2001: older timestamps are garbage
DAY_EPOCH_TS = 1577836800     # 2020-01-01 UTC: day 0 of Auctionator's day numbers
FUTURE_TS_SLACK = 300         # a pushed timestamp may be at most 5 minutes in the future (clock skew)
PULL_TS_SLACK = 3600          # pulled rows: the server already checked them, allow more skew between PCs
MAX_STR = 64
SERVER_MAX_ROWS = 2 * MAX_BATCH_ROWS  # per pushed batch (static data excluded); one huge scan may exceed the client cap
STATIC_MAX_ROWS = 2_000_000
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
STACK = _I(1, 10 ** 6)
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
    now = int(time.time() if now is None else now)
    return {"ts": now + slack, "day": (now - DAY_EPOCH_TS) // 86400 + 1}


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

    # --- Auctionator prices (rows the server would refuse - wrong types, future dates - stay local)
    lim = _limits()
    since = mine.get("snap_ts")
    snaps = _rows(conn, "SELECT %s FROM snapshots WHERE ts>? ORDER BY ts" % ",".join(SNAPSHOTS),
                  (since if since is not None else -1,))
    if snaps and isinstance(snaps[-1][2], int):
        new_state["snap_ts"] = min(snaps[-1][2], lim["ts"])
    current = _valid_rows(_rows(conn, "SELECT %s FROM price_current" % ",".join(PRICE_CURRENT)), "price_current", lim)
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
        daily.extend(_valid_rows(_rows(conn, "SELECT %s FROM price_daily WHERE realm=? AND day>=? ORDER BY day, "
                                       "item_id" % ",".join(PRICE_DAILY), (realm, frm)), "price_daily", lim))
        new_state["daily"][realm] = min(mx, lim["day"]) if isinstance(mx, int) else mx
    for i in range(0, len(daily), MAX_BATCH_ROWS):
        batches.append({"price_daily": daily[i:i + MAX_BATCH_ROWS]})

    # --- AH Tool Logger scans
    have = {(r[0], r[1], r[2]): (r[3], r[4]) for r in remote.get("scans") or []}
    # only your own scans: pulled ones (source 'web') came from the server and belong to whoever scanned them
    scans = [s for s in conn.execute("SELECT id, %s FROM ah_scans WHERE COALESCE(source,'')<>'web' ORDER BY ts"
                                     % ",".join(SCAN)).fetchall() if _bad_row(list(s[1:]), SPEC["scan"], lim) is None]
    key_of = {s[0]: (s[2], s[3], s[1]) for s in scans}  # id -> (realm, faction, ts)

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
        if cur and cur_rows + n > MAX_BATCH_ROWS:
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
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _opener.open(req, timeout=timeout) as r:
            body = _read_capped(r)
    except urllib.error.HTTPError as e:
        body = e.read(300).decode("utf-8", "replace")
        try:
            body = json.loads(body).get("error", body)
        except ValueError:
            pass
        raise SyncError("%s %s -> HTTP %d: %s" % (method, url, e.code, body))
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

def ensure_schema(conn):
    # Server only: which push last changed a row (seq, for /api/sync/pull) and which key pushed it (src_key). The
    # local app's tables never get them. Added with ALTER TABLE so an existing web database keeps working.
    for cols, tables, ddl in (("seq", SEQ_TABLES, "seq INTEGER NOT NULL DEFAULT 0"),
                              ("src_key", SRC_TABLES, "src_key INTEGER")):
        for t in tables:
            if cols not in [r[1] for r in conn.execute("PRAGMA main.table_info(%s)" % t)]:
                conn.execute("ALTER TABLE main.%s ADD COLUMN %s" % (t, ddl))
    conn.executescript(SERVER_SCHEMA)
    if not conn.execute("SELECT 1 FROM main.meta WHERE key='sync_epoch'").fetchone():
        with conn:
            conn.execute("INSERT OR IGNORE INTO main.meta(key,value) VALUES('sync_epoch',?)", (os.urandom(8).hex(),))


def _src(source):
    """The src_key value stored for rows this pusher sends: its key id, 0 for AHTOOL_SYNC_TOKEN."""
    return int(source.get("key_id") or 0)


def remote_state(conn, source):
    """What the server has, and what it accepts from this pusher."""
    ensure_schema(conn)
    meta = dict(conn.execute("SELECT key, value FROM main.meta WHERE key IN "
                             "('sync_static_hash','sync_push_ts','sync_epoch')"))
    return {"protocol": PROTOCOL, "epoch": meta.get("sync_epoch"), "you": source["name"],
            "static_hash": meta.get("sync_static_hash"),
            "accept_static": bool(source["owner"]), "accept_cfg": bool(source["owner"]),
            "last_push_ts": int(meta["sync_push_ts"]) if str(meta.get("sync_push_ts") or "").isdigit() else None,
            "scans": [list(r) for r in conn.execute(
                "SELECT realm, faction, ts, raw_kept, diffed FROM main.ah_scans ORDER BY ts")]}


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


def _count_rows(payload):
    n = 0
    for k in ("price_current", "price_daily", "snapshots"):
        n += len(_list(payload.get(k), k))
    for s in _list(payload.get("scans"), "scans"):
        if not isinstance(s, dict):
            raise BadPayload("scans must be objects")
        n += 1 + sum(len(_list(s.get(k), k)) for k in ("items", "auctions", "sales"))
    for u in _list(payload.get("scan_updates"), "scan_updates"):
        if not isinstance(u, dict):
            raise BadPayload("scan_updates must be objects")
        n += 1 + len(_list(u.get("sales"), "sales"))
    return n


SCAN_KEY = (_S(), _S(32), _I(TS_MIN, "ts"))
# a price_current row may always replace one that claims to be from the future (left over from before validation)
_CURRENT_NEWER = ("(COALESCE(excluded.last_day,0), COALESCE(excluded.updated_at,0)) >= "
                  "(COALESCE(price_current.last_day,0), COALESCE(price_current.updated_at,0)) "
                  "OR COALESCE(price_current.last_day,0) > %d OR COALESCE(price_current.updated_at,0) > %d")


def apply(conn, cfg, payload, source):
    """Apply one pushed batch atomically, merged with what other pushers sent.

    source: {"name", "owner": bool, "key_id": int|None}. `cfg` (the live server config) gets the owner's settings.
    Every row is validated first (types, ranges, nothing in the future); one bad row refuses the whole batch."""
    if not isinstance(payload, dict) or payload.get("protocol") != PROTOCOL:
        raise BadPayload("unsupported sync protocol - update the AH Tool on this PC")
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
        r = cur.execute("SELECT value FROM main.meta WHERE key='sync_seq'").fetchone()
        seq = (int(r[0]) if r and str(r[0]).isdigit() else 0) + 1
        cur.execute("INSERT OR REPLACE INTO main.meta(key,value) VALUES('sync_seq',?)", (str(seq),))
        if "cfg" in payload and owner:
            c = payload["cfg"]
            if not isinstance(c, dict):
                raise BadPayload("cfg must be an object")
            new_cfg = {k: c[k] for k in CFG_KEYS if isinstance(c.get(k), (int, float)) and not isinstance(c[k], bool)
                       and math.isfinite(c[k]) and abs(c[k]) < 1e9}
            cur.execute("INSERT OR REPLACE INTO main.meta(key,value) VALUES('sync_cfg',?)", (json.dumps(new_cfg),))
        if "meta" in payload:
            m = payload["meta"]
            if not isinstance(m, dict):
                raise BadPayload("meta must be an object")
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

        before = conn.total_changes
        realms = {}
        if "price_current" in payload:
            rows = _checked(payload["price_current"], "price_current", lim)
            # newest observation wins; unchanged rows are not touched, so they do not count as new data
            cur.executemany(
                "INSERT INTO main.price_current(realm,item_id,price,available,last_day,updated_at,seq,src_key) "
                "VALUES(?,?,?,?,?,?,%d,%d) " % (seq, src) +
                "ON CONFLICT(realm,item_id) DO UPDATE SET price=excluded.price, available=excluded.available, "
                "last_day=excluded.last_day, updated_at=excluded.updated_at, seq=excluded.seq, "
                "src_key=excluded.src_key WHERE (" + _CURRENT_NEWER % (lim["day"], lim["ts"]) + ") "
                "AND (excluded.price IS NOT price_current.price OR excluded.available IS NOT price_current.available "
                "OR excluded.last_day IS NOT price_current.last_day)", rows)
            for r in rows:
                realms[r[0]] = realms.get(r[0], 0) + 1
        if "price_daily" in payload:
            rows = _checked(payload["price_daily"], "price_daily", lim)
            cur.executemany(
                "INSERT INTO main.price_daily(realm,item_id,day,low,high,available,seq,src_key) "
                "VALUES(?,?,?,?,?,?,%d,%d) ON CONFLICT(realm,item_id,day) DO UPDATE SET seq=excluded.seq, "
                "src_key=excluded.src_key, %s" % (seq, src, MERGE_DAILY), rows)
            applied["price_daily"] = len(rows)
            for r in rows:
                realms.setdefault(r[0], 0)
        if "snapshots" in payload:
            rows = _checked(payload["snapshots"], "snapshots", lim)
            _ins(cur, "snapshots", SNAPSHOTS + ("src_key",), [r + [src] for r in rows], "INSERT OR IGNORE")
            applied["snapshots"] = len(rows)
        changed = conn.total_changes - before
        if changed:
            # One ingest row per push that changed prices: the analytics cache and the per-browser alert check
            # key on MAX(ingests.id).
            realm = max(realms, key=lambda k: realms[k]) if realms else None
            cur.execute("INSERT INTO main.ingests(ts,file_mtime,realm,items,changed) VALUES(?,NULL,?,?,?)",
                        (int(time.time()), realm, realms.get(realm, 0), changed))
            if owner and realm:
                # the web version's default realm follows the owner, never a contributor (analytics.get_realm)
                cur.execute("INSERT OR REPLACE INTO main.meta(key,value) VALUES('sync_owner_realm',?)", (realm,))

        def scan_row(key):
            if not isinstance(key, list) or _bad_row(key, SCAN_KEY, lim):
                raise BadPayload("scan key must be [realm, faction, ts]")
            return cur.execute("SELECT id, src_key FROM main.ah_scans WHERE realm=? AND faction=? AND ts=?",
                               key).fetchone()

        def put_sales(sid, realm, faction, ts, rows):
            cur.execute("DELETE FROM main.ah_sales WHERE scan_id=?", (sid,))
            out = []
            for prev_ts, iid, suffix, count, unit, owner_, kind in rows:
                prev = scan_row([realm, faction, prev_ts]) if prev_ts is not None else None
                out.append((sid, prev[0] if prev else None, ts, iid, suffix, count, unit, owner_ or "", kind))
            cur.executemany("INSERT INTO main.ah_sales(scan_id,prev_scan_id,ts,item_id,suffix,count,unit_price,owner,"
                            "kind) VALUES(?,?,?,?,?,?,?,?,?)", out)

        for s in _list(payload.get("scans"), "scans"):
            row = _checked([s.get("scan")], "scan", lim)[0]
            items = _checked(_list(s.get("items"), "scan items"), "scan items", lim)
            auctions = _checked(_list(s.get("auctions"), "auctions"), "auctions", lim)
            sales = _checked(_list(s.get("sales"), "sales"), "sales", lim)
            ts, realm, faction = row[0], row[1], row[2]
            if scan_row([realm, faction, ts]) is not None:
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
        # Never hold more raw auctions than the owner's app does, whoever pushed them.
        try:
            keep = max(2, min(int((new_cfg or cfg or {}).get("logger_keep_raw_scans") or 6), 1000))
        except (TypeError, ValueError, OverflowError):
            keep = 6
        for realm, faction in cur.execute("SELECT DISTINCT realm, faction FROM main.ah_scans").fetchall():
            for (sid,) in cur.execute("SELECT id FROM main.ah_scans WHERE realm=? AND faction=? AND raw_kept=1 "
                                      "ORDER BY ts DESC LIMIT -1 OFFSET ?", (realm, faction, keep)).fetchall():
                cur.execute("DELETE FROM main.ah_auctions WHERE scan_id=?", (sid,))
                cur.execute("UPDATE main.ah_scans SET raw_kept=0, seq=? WHERE id=?", (seq, sid))
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
    """Server (ahkey purge): delete every row the given sync keys pushed or last changed. -> {table: rows}.

    Rows pushed before src_key existed (NULL) are not tracked and stay. PCs that already pulled the rows keep them."""
    ensure_schema(conn)
    ids = sorted({int(k) for k in key_ids})
    out = {}
    if not ids:
        return out
    q = ",".join("?" * len(ids))
    cur = conn.cursor()
    cur.execute("BEGIN IMMEDIATE")
    try:
        sids = [r[0] for r in cur.execute("SELECT id FROM main.ah_scans WHERE src_key IN (%s)" % q, ids)]
        for i in range(0, len(sids), 500):
            part = sids[i:i + 500]
            sq = ",".join("?" * len(part))
            for t in ("ah_auctions", "ah_scan_items", "ah_sales"):
                out[t] = out.get(t, 0) + cur.execute("DELETE FROM main.%s WHERE scan_id IN (%s)" % (t, sq),
                                                     part).rowcount
            cur.execute("UPDATE main.ah_sales SET prev_scan_id=NULL WHERE prev_scan_id IN (%s)" % sq, part)
        for t in ("ah_scans", "price_daily", "price_current", "snapshots"):
            out[t] = cur.execute("DELETE FROM main.%s WHERE src_key IN (%s)" % (t, q), ids).rowcount
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
    """Merge one pulled page into the local DB. Returns {"prices": changed rows, "scans": new/updated scans}.

    Every row is validated like on the server (types, ranges, nothing in the future); one bad row refuses the page
    (BadPayload) and nothing of it is stored."""
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
        scans.append((_checked([s.get("scan")], "scan", lim)[0],
                      _checked(_list(s.get("items"), "scan items"), "scan items", lim),
                      None if auctions is None else _checked(_list(auctions, "auctions"), "auctions", lim),
                      _checked(_list(s.get("sales"), "sales"), "sales", lim)))
    cur = conn.cursor()
    cur.execute("BEGIN IMMEDIATE")
    try:
        before = conn.total_changes
        cur.executemany(
            "INSERT INTO price_current(realm,item_id,price,available,last_day,updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(realm,item_id) DO UPDATE SET price=excluded.price, available=excluded.available, "
            "last_day=excluded.last_day, updated_at=excluded.updated_at "
            "WHERE (" + _CURRENT_NEWER % (lim["day"], lim["ts"]) + ") "
            "AND (excluded.price IS NOT price_current.price OR excluded.available IS NOT price_current.available "
            "OR excluded.last_day IS NOT price_current.last_day)", pc)
        realms = {}
        for r in pc:
            realms[r[0]] = realms.get(r[0], 0) + 1
        cur.executemany("INSERT INTO price_daily(realm,item_id,day,low,high,available) VALUES(?,?,?,?,?,?) "
                        "ON CONFLICT(realm,item_id,day) DO UPDATE SET " + MERGE_DAILY, pd)
        for r in pd:
            realms.setdefault(r[0], 0)
        cur.executemany("INSERT INTO snapshots(realm,item_id,ts,price,available) SELECT ?,?,?,?,? WHERE NOT EXISTS "
                        "(SELECT 1 FROM snapshots WHERE realm=? AND item_id=? AND ts=?)",
                        [r + r[:3] for r in sn])
        prices = conn.total_changes - before

        def local_scan(realm, faction, ts):
            return cur.execute("SELECT id, COALESCE(source,''), raw_kept FROM ah_scans WHERE realm=? AND faction=? "
                               "AND ts=?", (realm, faction, ts)).fetchone()

        def put_sales(sid, realm, faction, ts, sales):
            cur.execute("DELETE FROM ah_sales WHERE scan_id=?", (sid,))
            out = []
            for prev_ts, iid, suffix, count, unit, owner, kind in sales:
                prev = local_scan(realm, faction, prev_ts) if prev_ts is not None else None
                out.append((sid, prev[0] if prev else None, ts, iid, suffix, count, unit, owner or "", kind))
            cur.executemany("INSERT INTO ah_sales(scan_id,prev_scan_id,ts,item_id,suffix,count,unit_price,owner,kind) "
                            "VALUES(?,?,?,?,?,?,?,?,?)", out)

        n_scans = 0
        for row, items, auctions, sales in scans:
            ts, realm, faction = row[0], row[1], row[2]
            have = local_scan(realm, faction, ts)
            if have and have[1] != "web":
                continue  # your own scan coming back: yours is the original
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
                cur.executemany("INSERT INTO ah_scan_items(scan_id,%s) VALUES(?,%s)"
                                % (",".join(SCAN_ITEMS), ",".join("?" * len(SCAN_ITEMS))), [[sid] + r for r in items])
                cur.executemany("INSERT INTO ah_auctions(scan_id,%s) VALUES(?,%s)"
                                % (",".join(AUCTIONS), ",".join("?" * len(AUCTIONS))),
                                [[sid] + r for r in auctions or []])
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
    return {"prices": prices, "scans": n_scans}


def pull(conn, cfg, full=False, max_pages=300):
    """Fetch what everyone pushed since the last pull. Returns {"prices", "scans", "pages"}.

    At most `max_pages` pages per call; the rest follows on the next pull (the cursor is saved after each page)."""
    if not cfg.get("sync_url") or not cfg.get("sync_token"):
        raise SyncError("sync_url and sync_token must be set in config.json")
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
        q = urllib.parse.urlencode({"cursor": json.dumps(st.get("cursor") or {}, separators=(",", ":"))})
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
