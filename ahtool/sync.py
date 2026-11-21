"""Push market data from the local AH Tool to the web version, and receive it there.

Only public market data leaves the PC. What is pushed is decided by the explicit column lists below, never by
"everything except": a table or column that is added locally later stays local until it is listed here.

Pushed:   item/recipe/tooltip data, Auctionator prices (price_daily, price_current, snapshots, ingests) and the
          AH Tool Logger full scans (ah_scans, ah_scan_items, ah_auctions, ah_sales).
Never:    postings (Auctionator posting history), my_mail, my_auctions, my_professions, known_recipes,
          shopping_list, watchlist, alerts, settings, paths from config.json.
Masked:   ah_scans.char is blanked, and the seller name of every auction/sale posted by one of your own characters
          is blanked (""), so the web version cannot tell which auctions are yours. Your characters are taken from
          the scans you made, your own auctions and your AH mail, plus `sync_hide_sellers` in config.json for alts
          the logger has never seen.

The protocol is incremental: the client asks the server what it has (GET /api/sync/state) and sends only what is
missing, in batches (POST /api/sync/push, gzip JSON). Run `python -m ahtool.sync` to push by hand, `--full` to
resend everything.
"""
import gzip
import hashlib
import json
import sys
import threading
import time
import urllib.error
import urllib.request

PROTOCOL = 1
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
INGESTS = ("id", "ts", "file_mtime", "realm", "items", "changed")
SCAN = ("ts", "realm", "faction", "source", "total", "n", "partial", "raw_kept", "diffed")
SCAN_ITEMS = ("item_id", "auctions", "qty", "min_unit", "median_unit", "sellers")
AUCTIONS = ("item_id", "suffix", "count", "buyout", "min_bid", "bid", "time_left", "owner")
SALES = ("prev_ts", "item_id", "suffix", "count", "unit_price", "owner", "kind")
META_KEYS = ("last_ingest_ts", "logger_ingest_ts", "tooltip_version")
# Analysis settings that make the web version compute the same numbers as the local one. Never paths.
CFG_KEYS = ("ah_cut", "deal_threshold", "baseline_days", "outlier_mad_k", "outlier_min_rel", "thin_qty",
            "thin_high_factor", "conf_min_days", "conf_min_avg_qty", "trend_min_profit", "logger_keep_raw_scans",
            "logger_max_age_h")


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
        extra = [x for x in extra.split(",")]
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


def build_batches(conn, cfg, remote, full=False):
    """-> list of payload dicts to POST, in order. `remote` is the server's /api/sync/state answer."""
    own = own_names(conn, cfg)
    head = {"protocol": PROTOCOL, "cfg": {k: cfg[k] for k in CFG_KEYS if k in (cfg or {})},
            "meta": {k: v for k, v in conn.execute("SELECT key, value FROM meta") if k in META_KEYS}}
    batches = []

    # --- static item/recipe data: all or nothing, only when it changed
    sh = static_hash(conn)
    if full or remote.get("static_hash") != sh:
        batches.append({"static": {"hash": sh, "tables": {
            t: _rows(conn, "SELECT %s FROM %s" % (",".join(cols), t)) for t, cols in STATIC_TABLES.items()}}})

    # --- Auctionator prices
    prices = {"price_current": {}, "ingests": _rows(conn, "SELECT %s FROM ingests ORDER BY id" % ",".join(INGESTS))}
    for (realm,) in conn.execute("SELECT DISTINCT realm FROM price_current"):
        prices["price_current"][realm] = _rows(
            conn, "SELECT %s FROM price_current WHERE realm=?" % ",".join(PRICE_CURRENT), (realm,))
    rs = remote.get("snapshots") or {}
    since = rs.get("max_ts")
    if not full and since is not None and conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE ts<=?", (since,)).fetchone()[0] == rs.get("count"):
        prices["snapshots"] = {"replace": False, "rows": _rows(
            conn, "SELECT %s FROM snapshots WHERE ts>? ORDER BY ts" % ",".join(SNAPSHOTS), (since,))}
    else:
        prices["snapshots"] = {"replace": True, "rows": _rows(
            conn, "SELECT %s FROM snapshots ORDER BY ts" % ",".join(SNAPSHOTS))}
    batches.append(prices)

    daily = []
    remote_days = remote.get("price_daily") or {}
    for (realm,) in conn.execute("SELECT DISTINCT realm FROM price_daily"):
        rd = remote_days.get(realm) or {}
        wm = rd.get("from_day")
        if full or wm is None or conn.execute("SELECT COUNT(*) FROM price_daily WHERE realm=? AND day<?",
                                              (realm, wm)).fetchone()[0] != rd.get("count_before"):
            wm = -10 ** 9
        daily.extend(_rows(conn, "SELECT %s FROM price_daily WHERE realm=? AND day>=? ORDER BY day, item_id"
                           % ",".join(PRICE_DAILY), (realm, wm)))
    for i in range(0, len(daily), MAX_BATCH_ROWS):
        batches.append({"price_daily": daily[i:i + MAX_BATCH_ROWS]})

    # --- AH Tool Logger scans
    have = {(r[0], r[1], r[2]): (r[3], r[4]) for r in remote.get("scans") or []}
    scans = conn.execute("SELECT id, %s FROM ah_scans ORDER BY ts" % ",".join(SCAN)).fetchall()
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
    return batches


def _request(cfg, method, path, payload=None, timeout=180):
    url = (cfg.get("sync_url") or "").rstrip("/") + path
    headers = {"Authorization": "Bearer %s" % cfg.get("sync_token", ""), "Accept": "application/json",
               # a browser-like UA is not needed; a named one makes the requests easy to find in logs
               "User-Agent": "AHTool-sync/%d" % PROTOCOL}
    data = None
    if payload is not None:
        data = gzip.compress(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"), 6)
        headers["Content-Type"] = "application/gzip"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read()[:300].decode("utf-8", "replace")
        try:
            body = json.loads(body).get("error", body)
        except ValueError:
            pass
        raise SyncError("%s %s -> HTTP %d: %s" % (method, url, e.code, body))
    except (urllib.error.URLError, OSError) as e:
        raise SyncError("%s %s failed: %s" % (method, url, getattr(e, "reason", e)))


def push(conn, cfg, full=False, log=print):
    """Bring the web version up to date. Returns a summary dict."""
    if not cfg.get("sync_url") or not cfg.get("sync_token"):
        raise SyncError("sync_url and sync_token must be set in config.json")
    t0 = time.time()
    remote = _request(cfg, "GET", "/api/sync/state")
    if remote.get("protocol") != PROTOCOL:
        raise SyncError("the web version speaks sync protocol %s, this app %s - update both"
                        % (remote.get("protocol"), PROTOCOL))
    batches = build_batches(conn, cfg, remote, full=full)
    sent = {"batches": 0}
    for b in batches:
        res = _request(cfg, "POST", "/api/sync/push", b)
        sent["batches"] += 1
        for k, v in (res.get("applied") or {}).items():
            sent[k] = sent.get(k, 0) + v
    sent["seconds"] = round(time.time() - t0, 1)
    return sent


class Pusher:
    """Background pusher for run.py: debounces triggers (Auctionator and the logger usually change together on
    /reload) and never runs two pushes at once. Failures are printed and retried on the next trigger."""

    def __init__(self, cfg, connect, delay=8.0, log=print):
        self.cfg, self.connect, self.delay, self.log = cfg, connect, delay, log
        self._ev = threading.Event()
        self._due = 0.0
        self._full = False
        threading.Thread(target=self._run, name="ahtool-sync", daemon=True).start()

    def trigger(self, full=False):
        self._due = time.time() + self.delay
        self._full = self._full or full
        self._ev.set()

    def _run(self):
        while True:
            self._ev.wait()
            while time.time() < self._due:
                time.sleep(max(0.2, self._due - time.time()))
            self._ev.clear()
            full, self._full = self._full, False
            conn = None
            try:
                conn = self.connect()
                res = push(conn, self.cfg, full=full)
                self.log("  Pushed to the web version: %s" % _summary(res))
            except Exception as e:  # noqa - never take the local app down
                self.log("  Push to the web version failed (retried after the next import): %s" % e)
            finally:
                if conn is not None:
                    conn.close()


def _summary(res):
    keys = ("price_daily", "snapshots", "scans", "auctions", "static")
    parts = ["%s %s" % (res[k], k) for k in keys if res.get(k)]
    return ", ".join(parts or ["already up to date"]) + " in %ss" % res.get("seconds")


# ================================================================ server (web version)

def remote_state(conn):
    """What the server has, so the client can send only what is missing."""
    meta = dict(conn.execute("SELECT key, value FROM main.meta WHERE key IN ('sync_static_hash','sync_push_ts')"))
    daily = {}
    for realm, mx in conn.execute("SELECT realm, MAX(day) FROM price_daily GROUP BY realm").fetchall():
        frm = mx - DAY_OVERLAP
        daily[realm] = {"from_day": frm, "count_before": conn.execute(
            "SELECT COUNT(*) FROM price_daily WHERE realm=? AND day<?", (realm, frm)).fetchone()[0]}
    mx, cnt = conn.execute("SELECT MAX(ts), COUNT(*) FROM snapshots").fetchone()
    return {"protocol": PROTOCOL, "static_hash": meta.get("sync_static_hash"),
            "last_push_ts": int(meta["sync_push_ts"]) if meta.get("sync_push_ts") else None,
            "price_daily": daily, "snapshots": {"max_ts": mx, "count": cnt if mx is not None else 0},
            "scans": [list(r) for r in conn.execute(
                "SELECT realm, faction, ts, raw_kept, diffed FROM ah_scans ORDER BY ts")]}


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


def apply(conn, cfg, payload):
    """Apply one pushed batch atomically. `cfg` (the live server config) gets the pushed analysis settings."""
    if not isinstance(payload, dict) or payload.get("protocol") != PROTOCOL:
        raise BadPayload("unsupported sync protocol")
    applied = {}
    new_cfg = None
    cur = conn.cursor()
    cur.execute("BEGIN IMMEDIATE")
    try:
        if "cfg" in payload:
            c = payload["cfg"]
            if not isinstance(c, dict):
                raise BadPayload("cfg must be an object")
            new_cfg = {k: c[k] for k in CFG_KEYS if isinstance(c.get(k), (int, float)) and not isinstance(c[k], bool)}
            cur.execute("INSERT OR REPLACE INTO main.meta(key,value) VALUES('sync_cfg',?)", (json.dumps(new_cfg),))
        if "meta" in payload:
            m = payload["meta"]
            if not isinstance(m, dict):
                raise BadPayload("meta must be an object")
            for k in META_KEYS:
                if isinstance(m.get(k), (str, int)):
                    cur.execute("INSERT OR REPLACE INTO main.meta(key,value) VALUES(?,?)", (k, str(m[k])))
        st = payload.get("static")
        if st is not None:
            tables = (st or {}).get("tables")
            if not isinstance(tables, dict) or set(tables) != set(STATIC_TABLES):
                raise BadPayload("static must contain exactly: %s" % ", ".join(STATIC_TABLES))
            for t, cols in STATIC_TABLES.items():
                rows = _fixed(tables[t], len(cols), t)
                cur.execute("DELETE FROM main.%s" % t)
                _ins(cur, t, cols, rows)
            cur.execute("INSERT OR REPLACE INTO main.meta(key,value) VALUES('sync_static_hash',?)",
                        (str(st.get("hash") or ""),))
            applied["static"] = sum(len(v) for v in tables.values())
        if "price_current" in payload:
            pc = payload["price_current"]
            if not isinstance(pc, dict):
                raise BadPayload("price_current must be {realm: rows}")
            for realm, rows in pc.items():
                rows = _fixed(rows, len(PRICE_CURRENT), "price_current")
                if any(r[0] != realm for r in rows):
                    raise BadPayload("price_current rows must belong to their realm")
                cur.execute("DELETE FROM main.price_current WHERE realm=?", (realm,))
                _ins(cur, "price_current", PRICE_CURRENT, rows)
        if "ingests" in payload:
            rows = _fixed(payload["ingests"], len(INGESTS), "ingests")
            cur.execute("DELETE FROM main.ingests")
            _ins(cur, "ingests", INGESTS, rows)
        if "snapshots" in payload:
            sn = payload["snapshots"]
            rows = _fixed((sn or {}).get("rows"), len(SNAPSHOTS), "snapshots")
            if sn.get("replace"):
                cur.execute("DELETE FROM main.snapshots")
            _ins(cur, "snapshots", SNAPSHOTS, rows)
            applied["snapshots"] = len(rows)
        if "price_daily" in payload:
            rows = _fixed(payload["price_daily"], len(PRICE_DAILY), "price_daily")
            _ins(cur, "price_daily", PRICE_DAILY, rows, "INSERT OR REPLACE")
            applied["price_daily"] = len(rows)

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
            return len(out)

        for s in payload.get("scans") or []:
            row = _fixed([s.get("scan")], len(SCAN), "scan")[0] if isinstance(s, dict) else None
            if row is None:
                raise BadPayload("scans must be objects")
            ts, realm, faction = row[0], row[1], row[2]
            if scan_id([realm, faction, ts]) is not None:
                continue
            # char is always blank here: the scanning character is one of yours
            cur.execute("INSERT INTO main.ah_scans(ts,realm,faction,char,source,total,n,partial,raw_kept,diffed) "
                        "VALUES(?,?,?,'',?,?,?,?,?,?)", row[:3] + row[3:])
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
                cur.execute("UPDATE main.ah_scans SET raw_kept=0, diffed=1 WHERE id=?", (sid,))
            if "sales" in u:
                put_sales(sid, realm, faction, ts, u["sales"])
                cur.execute("UPDATE main.ah_scans SET diffed=1 WHERE id=?", (sid,))
        # Safety net independent of the client: never hold more raw auctions than the local app does.
        keep = int((new_cfg or cfg or {}).get("logger_keep_raw_scans") or 6)
        for realm, faction in cur.execute("SELECT DISTINCT realm, faction FROM main.ah_scans").fetchall():
            for (sid,) in cur.execute("SELECT id FROM main.ah_scans WHERE realm=? AND faction=? AND raw_kept=1 "
                                      "ORDER BY ts DESC LIMIT -1 OFFSET ?", (realm, faction, max(2, keep))).fetchall():
                cur.execute("DELETE FROM main.ah_auctions WHERE scan_id=?", (sid,))
                cur.execute("UPDATE main.ah_scans SET raw_kept=0 WHERE id=?", (sid,))
        cur.execute("INSERT OR REPLACE INTO main.meta(key,value) VALUES('sync_push_ts',?)", (str(int(time.time())),))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if new_cfg and cfg is not None:
        cfg.update(new_cfg)
    return {"ok": True, "applied": applied}


def load_pushed_cfg(conn, cfg):
    """Server start: reuse the analysis settings from the last push."""
    r = conn.execute("SELECT value FROM main.meta WHERE key='sync_cfg'").fetchone()
    if r:
        try:
            cfg.update({k: v for k, v in json.loads(r[0]).items() if k in CFG_KEYS})
        except (ValueError, AttributeError):
            pass


# ================================================================ CLI

def main(argv=None):
    import argparse
    from . import config, db
    ap = argparse.ArgumentParser(description="Push market data to the AH Tool web version.")
    ap.add_argument("--full", action="store_true", help="resend everything (repairs a web version that drifted)")
    ap.add_argument("--dry-run", action="store_true", help="show what would be sent, send nothing")
    a = ap.parse_args(argv)
    cfg = config.load()
    conn = db.connect(cfg["db_path"])
    try:
        if a.dry_run:
            remote = _request(cfg, "GET", "/api/sync/state") if cfg.get("sync_url") else {}
            for i, b in enumerate(build_batches(conn, cfg, remote, full=a.full)):
                size = len(gzip.compress(json.dumps(b, separators=(",", ":")).encode()))
                print("batch %d: %s, %.1f kB gzip" % (i + 1, ", ".join(k for k in b if k not in ("protocol",)),
                                                      size / 1024))
            print("Your characters (masked as seller): %s" % (", ".join(sorted(own_names(conn, cfg))) or "none"))
            return 0
        print("Pushed: %s" % _summary(push(conn, cfg, full=a.full)))
        return 0
    except SyncError as e:
        print("Push failed: %s" % e, file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
