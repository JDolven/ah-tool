"""Blizzard Battle.net Game Data API (optional) - WoW: Forever item data and auction houses.

Nothing here runs unless a client is configured (develop.battle.net -> API Access -> Create Client):
  local app: config.json  "bnet_client_id", "bnet_client_secret"
  web version: environment AHTOOL_BNET_CLIENT_ID, AHTOOL_BNET_CLIENT_SECRET (run_web.py)
Optional: "bnet_region" (eu), "bnet_ruleset" (1 = PvP; 2 Normal, 3 Roleplay, 15 Roleplay PvP), "bnet_houses"
(default: the ruleset's own houses, see RULESET_HOUSES - every ruleset lists all of them, but each house belongs to
one ruleset), "bnet_realm" (realm key the snapshots are compared under; default the ruleset's name, e.g. "PvP").

What it does (a background thread, see start()):
* Item data (static-forever-<region>): the whole item catalogue through the item search (1000 items per request,
  about 18 requests), refreshed weekly -> bnet_items: name, quality, vendor sell and buy price. Applied to
  items.vendor_sell / vendor_buy where the game itself (AH Tool Logger, item_sell) has no price - the logger is what
  the client shows right now, the API is the fallback - and names custom Forever items the static data lacks. A few
  custom items are missing from the API and keep what the logger or the hotfix cache says.
* Auction houses (dynamic-forever-<region>): an hourly snapshot of each configured house (items + commodities),
  summarised per item -> bnet_ah_snapshots / bnet_ah_items. No seller names: the API has none. These are kept apart
  from your own scans (ah_scans) for now; compare() puts the newest snapshot next to your newest full scan.
"""
import base64
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API_HOSTS = {"eu": "eu.api.blizzard.com", "us": "us.api.blizzard.com", "kr": "kr.api.blizzard.com",
             "tw": "tw.api.blizzard.com"}
TOKEN_URL = "https://oauth.battle.net/token"
# Alliance / Horde / Blackwater (neutral) house of each ruleset. 110/113 matched the PvP auction house in game; the
# neutral ones follow the same pattern in the API's house list (112/115/118 are always empty).
RULESET_HOUSES = {1: {"Alliance": 110, "Horde": 113, "Neutral": 116},
                  2: {"Alliance": 111, "Horde": 114, "Neutral": 117},
                  3: {"Alliance": 119, "Horde": 120, "Neutral": 121},
                  15: {"Alliance": 123, "Horde": 124, "Neutral": 125}}
DEFAULT_HOUSES = RULESET_HOUSES[1]
TIME_LEFT = {"SHORT": 1, "MEDIUM": 2, "LONG": 3, "VERY_LONG": 4}
ITEM_MAX_AGE_S = 7 * 86400
ITEM_PAGE = 1000              # the item search's largest page
QUALITY = {"POOR": 0, "COMMON": 1, "UNCOMMON": 2, "RARE": 3, "EPIC": 4, "LEGENDARY": 5, "ARTIFACT": 6, "HEIRLOOM": 7}
SNAPSHOT_CHECK_S = 600        # look for a new house snapshot every 10 minutes (they change about hourly)
KEEP_SNAPSHOTS = 72           # per house

SCHEMA = """
CREATE TABLE IF NOT EXISTS bnet_items(id INTEGER PRIMARY KEY, name TEXT, quality INTEGER, sell INTEGER,
    buy INTEGER, buy_qty INTEGER, ts INTEGER);
CREATE TABLE IF NOT EXISTS bnet_ah_snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, realm TEXT,
    faction TEXT, house INTEGER, last_modified TEXT, auctions INTEGER, units INTEGER, items INTEGER);
CREATE INDEX IF NOT EXISTS idx_bnet_snap ON bnet_ah_snapshots(realm, faction, ts);
CREATE TABLE IF NOT EXISTS bnet_ah_items(snapshot_id INTEGER, item_id INTEGER, auctions INTEGER, qty INTEGER,
    min_unit INTEGER, median_unit INTEGER, PRIMARY KEY(snapshot_id, item_id));
"""


class BnetError(Exception):
    pass


def log(*a):
    print("[bnet]", *a, file=sys.stderr, flush=True)


def ensure_schema(conn):
    conn.executescript(SCHEMA)


def settings(cfg):
    """-> dict with the API settings, or None when no client is configured (then nothing of this module runs)."""
    cfg = cfg or {}
    cid, secret = str(cfg.get("bnet_client_id") or "").strip(), str(cfg.get("bnet_client_secret") or "").strip()
    if not cid or not secret:
        return None
    region = str(cfg.get("bnet_region") or "eu").strip().lower()
    if region not in API_HOSTS:
        raise BnetError("bnet_region must be one of %s" % ", ".join(sorted(API_HOSTS)))
    ruleset = int(cfg.get("bnet_ruleset") or 1)
    houses = cfg.get("bnet_houses") or RULESET_HOUSES.get(ruleset)
    if not houses:
        raise BnetError("bnet_ruleset %d has no known houses - set bnet_houses" % ruleset)
    if isinstance(houses, str):  # "Alliance=110,Horde=113,Neutral=116" (environment variable)
        houses = dict(p.split("=", 1) for p in houses.split(",") if "=" in p)
    houses = {str(f).strip(): int(h) for f, h in houses.items() if str(h).strip()}
    for f in houses:
        if f not in ("Alliance", "Horde", "Neutral"):
            raise BnetError("bnet_houses: faction must be Alliance, Horde or Neutral (got %r)" % f)
    return {"client_id": cid, "client_secret": secret, "region": region, "host": API_HOSTS[region],
            "ruleset": ruleset, "houses": houses,
            "realm": str(cfg.get("bnet_realm") or "").strip()}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # never carry the token anywhere else
        return None


_opener = urllib.request.build_opener(_NoRedirect)


class Client:
    def __init__(self, s):
        self.s = s
        self._token, self._exp = None, 0

    def token(self):
        if self._token and time.time() < self._exp - 300:
            return self._token
        basic = base64.b64encode(("%s:%s" % (self.s["client_id"], self.s["client_secret"])).encode()).decode()
        req = urllib.request.Request(TOKEN_URL, data=b"grant_type=client_credentials", method="POST", headers={
            "Authorization": "Basic " + basic, "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with _opener.open(req, timeout=30) as r:
                d = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise BnetError("token request refused (%d) - check bnet_client_id / bnet_client_secret" % e.code)
        except (OSError, ValueError) as e:
            raise BnetError("token request failed: %s" % e)
        if not d.get("access_token"):
            raise BnetError("token response without access_token")
        self._token, self._exp = d["access_token"], time.time() + int(d.get("expires_in") or 3600)
        return self._token

    def get(self, path, namespace, if_modified_since=None, timeout=120, params=None):
        """-> (status, json | None, last_modified). 404 and 304 are answers, not errors."""
        q = urllib.parse.urlencode(dict({"namespace": "%s-forever-%s" % (namespace, self.s["region"]),
                                         "locale": "en_GB"}, **(params or {})))
        url = "https://%s%s?%s" % (self.s["host"], path, q)
        for attempt in range(4):
            headers = {"Authorization": "Bearer " + self.token(), "Accept-Encoding": "gzip"}
            if if_modified_since:
                headers["If-Modified-Since"] = if_modified_since
            try:
                with _opener.open(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
                    raw = r.read()
                    if r.headers.get("Content-Encoding") == "gzip":
                        import gzip
                        raw = gzip.decompress(raw)
                    return r.status, json.loads(raw), r.headers.get("Last-Modified")
            except urllib.error.HTTPError as e:
                if e.code in (304, 404):
                    return e.code, None, e.headers.get("Last-Modified")
                if e.code == 401 and attempt == 0:
                    self._token = None  # expired early: get a new one once
                    continue
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise BnetError("%s -> HTTP %d" % (path, e.code))
            except (OSError, ValueError) as e:
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise BnetError("%s failed: %s" % (path, e))
        raise BnetError("%s failed" % path)


# ---------------------------------------------------------------- item data

def _en(v):
    """A localised name: the plain string (locale=en_GB) or the en_GB/en_US entry of a {locale: name} dict."""
    if isinstance(v, dict):
        v = v.get("en_GB") or v.get("en_US")
    return v[:255] if isinstance(v, str) and v else None


def refresh_items(conn, cli, max_age_s=ITEM_MAX_AGE_S, now=None, force=False):
    """The whole item catalogue through the item search, ITEM_PAGE items per request in id order, when it is older
    than max_age_s (or force). Returns {"pages", "items"} ({"skipped": True} when fresh)."""
    ensure_schema(conn)
    now = int(now or time.time())
    last_ts = conn.execute("SELECT MIN(ts) FROM bnet_items").fetchone()[0]
    if not force and last_ts and now - last_ts < max_age_s:
        return {"skipped": True}
    last, pages, rows = 0, 0, []
    while pages < 1000:
        st, d, _ = cli.get("/data/wow/search/item", "static", params={
            "orderby": "id", "_pageSize": ITEM_PAGE, "id": "[%d,]" % (last + 1)})
        res = (d or {}).get("results") if st == 200 else None
        if not res:
            break
        pages += 1
        for x in res:
            it = x.get("data") if isinstance(x, dict) else None
            iid = _int((it or {}).get("id"))
            if not iid:
                continue
            last = max(last, iid)
            rows.append((iid, _en(it.get("name")), QUALITY.get((it.get("quality") or {}).get("type")),
                         _int(it.get("sell_price")), _int(it.get("purchase_price")),
                         max(_int(it.get("purchase_quantity")) or 1, 1), now))
        if len(res) < ITEM_PAGE:
            break
    if rows:
        with conn:
            conn.execute("DELETE FROM bnet_items")
            conn.executemany("INSERT OR REPLACE INTO bnet_items VALUES(?,?,?,?,?,?,?)", rows)
    return {"pages": pages, "items": len(rows)}


def _int(v):
    try:
        n = int(v)
        return n if 0 <= n < 2 ** 40 else None
    except (TypeError, ValueError):
        return None


def apply_items(conn):
    """Blizzard's item data into items (and the tooltip): names of unnamed items, vendor sell prices where the game
    itself (item_sell) has none, vendor buy prices - which also re-price the builtin vendor list (thread, vials, ...).
    Returns the number of items changed."""
    try:
        ensure_schema(conn)
        have_sell = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='item_sell'").fetchone()
        skip = "AND id NOT IN (SELECT id FROM item_sell)" if have_sell else ""
        before = conn.execute("SELECT total_changes()").fetchone()[0]
        # custom Forever items the static data has no name for (in recipes or on the AH): Blizzard's name
        conn.execute("INSERT OR IGNORE INTO items(id, name, quality) SELECT b.id, b.name, b.quality FROM bnet_items b "
                     "WHERE b.name IS NOT NULL AND (b.id IN (SELECT item_id FROM price_current) OR b.id IN "
                     "(SELECT item_id FROM recipe_reagents) OR b.id IN (SELECT created_item_id FROM recipes))")
        conn.execute("UPDATE items SET name=(SELECT b.name FROM bnet_items b WHERE b.id=items.id), "
                     "quality=COALESCE(quality, (SELECT b.quality FROM bnet_items b WHERE b.id=items.id)) "
                     "WHERE (name IS NULL OR name='') AND id IN (SELECT id FROM bnet_items WHERE name IS NOT NULL)")
        conn.execute("UPDATE items SET vendor_sell=(SELECT b.sell FROM bnet_items b WHERE b.id=items.id) "
                     "WHERE id IN (SELECT id FROM bnet_items WHERE sell IS NOT NULL) %s AND vendor_sell "
                     "IS NOT (SELECT b.sell FROM bnet_items b WHERE b.id=items.id)" % skip)
        conn.execute("UPDATE items SET vendor_buy=(SELECT b.buy / b.buy_qty FROM bnet_items b WHERE b.id=items.id) "
                     "WHERE id IN (SELECT id FROM bnet_items WHERE buy > 0 AND buy_qty > 0) AND "
                     "vendor_buy IS NOT (SELECT b.buy / b.buy_qty FROM bnet_items b WHERE b.id=items.id)")
        changed = conn.execute("SELECT total_changes()").fetchone()[0] - before
        conn.execute("UPDATE vendor_items SET price=(SELECT i.vendor_buy FROM items i WHERE i.id=vendor_items.item_id) "
                     "WHERE source='builtin' AND item_id IN (SELECT id FROM items WHERE vendor_buy > 0) AND price IS NOT "
                     "(SELECT i.vendor_buy FROM items i WHERE i.id=vendor_items.item_id)")
        conn.execute("UPDATE item_tooltip SET data=(SELECT CASE WHEN b.sell > 0 "
                     "THEN json_set(item_tooltip.data, '$.sell_price', b.sell) "
                     "ELSE json_remove(item_tooltip.data, '$.sell_price') END FROM bnet_items b "
                     "WHERE b.id=item_tooltip.id) "
                     "WHERE id IN (SELECT id FROM bnet_items WHERE sell IS NOT NULL) %s AND "
                     "json_valid(data) AND COALESCE(json_extract(data, '$.sell_price'), 0) IS NOT "
                     "(SELECT b.sell FROM bnet_items b WHERE b.id=item_tooltip.id)" % skip)
        conn.commit()
        return changed
    except Exception as e:  # noqa - never break the caller (static data build, sync) over this
        log("applying item data failed: %r" % (e,))
        return 0


# ---------------------------------------------------------------- auction houses

def summarise(auctions, commodities):
    """API auction lists -> {item_id: (auctions, qty, min_unit, median_unit)} (same rules as a full scan)."""
    per = {}
    for a in auctions or ():
        iid, q = (a.get("item") or {}).get("id"), int(a.get("quantity") or 1)
        b = a.get("buyout")
        if iid and q > 0:
            per.setdefault(iid, []).append((b / q if b else None, q))
    for a in commodities or ():
        iid, q, u = (a.get("item") or {}).get("id"), int(a.get("quantity") or 0), a.get("unit_price")
        if iid and q > 0:
            per.setdefault(iid, []).append((float(u) if u else None, q))
    out = {}
    for iid, rs in per.items():
        priced = sorted((u, q) for u, q in rs if u)
        med = None
        if priced:
            half, acc = sum(q for _, q in priced) / 2.0, 0
            for u, q in priced:
                acc += q
                if acc >= half:
                    med = int(round(u))
                    break
        out[iid] = (len(rs), sum(q for _, q in rs), int(priced[0][0]) if priced else None, med)
    return out


def realm_name(cli, s):
    if s["realm"]:
        return s["realm"]
    st, d, _ = cli.get("/data/wow/ruleset/%d" % s["ruleset"], "dynamic")
    name = d.get("name") if st == 200 and isinstance(d, dict) else None
    return name if isinstance(name, str) and name else "Ruleset %d" % s["ruleset"]


def snapshot_houses(conn, cli, s, now=None):
    """Store a new snapshot of every configured house whose data changed. Returns the number stored."""
    ensure_schema(conn)
    realm = realm_name(cli, s)
    stored = 0
    for faction, house in sorted(s["houses"].items()):
        prev = conn.execute("SELECT last_modified FROM bnet_ah_snapshots WHERE house=? AND realm=? ORDER BY ts DESC "
                            "LIMIT 1", (house, realm)).fetchone()
        base = "/data/wow/ruleset/%d/auctions/%d" % (s["ruleset"], house)
        st, items, lm = cli.get(base, "dynamic", if_modified_since=prev[0] if prev else None)
        if st == 304 or (prev and lm and lm == prev[0]):
            continue
        if st != 200 or not isinstance(items, dict):
            log("house %d: HTTP %s" % (house, st))
            continue
        st2, comm, _ = cli.get(base + "/commodities", "dynamic")
        auctions = items.get("auctions") or []
        commodities = (comm or {}).get("auctions") or [] if st2 == 200 else []
        if not auctions and not commodities:
            log("house %d is empty - check bnet_houses" % house)
            continue
        summ = summarise(auctions, commodities)
        ts = int(now or time.time())
        with conn:
            cur = conn.execute("INSERT INTO bnet_ah_snapshots(ts, realm, faction, house, last_modified, auctions, "
                               "units, items) VALUES(?,?,?,?,?,?,?,?)",
                               (ts, realm, faction, house, lm, len(auctions) + len(commodities),
                                sum(v[1] for v in summ.values()), len(summ)))
            sid = cur.lastrowid
            conn.executemany("INSERT INTO bnet_ah_items VALUES(?,?,?,?,?,?)",
                             [(sid, iid) + v for iid, v in summ.items()])
            old = [r[0] for r in conn.execute("SELECT id FROM bnet_ah_snapshots WHERE house=? AND realm=? "
                                              "ORDER BY ts DESC LIMIT -1 OFFSET ?", (house, realm, KEEP_SNAPSHOTS))]
            for oid in old:
                conn.execute("DELETE FROM bnet_ah_items WHERE snapshot_id=?", (oid,))
                conn.execute("DELETE FROM bnet_ah_snapshots WHERE id=?", (oid,))
        stored += 1
        log("house %d (%s %s): %d auctions, %d items" % (house, realm, faction, len(auctions) + len(commodities),
                                                          len(summ)))
    return stored


# ---------------------------------------------------------------- compare with your own scans

def compare(conn, faction="Alliance", realm=None, limit=50):
    """Newest Blizzard snapshot of `faction` next to your newest full scan of the same faction."""
    ensure_schema(conn)
    q = "SELECT * FROM bnet_ah_snapshots WHERE faction=?"
    args = [faction]
    if realm:
        q += " AND realm=?"
        args.append(realm)
    snap = conn.execute(q + " ORDER BY ts DESC LIMIT 1", args).fetchone()
    if not snap:
        return {"enabled": True, "snapshot": None, "scan": None, "note": "no Blizzard snapshot for %s yet" % faction}
    snap = dict(zip([c[0] for c in conn.execute("SELECT * FROM bnet_ah_snapshots LIMIT 0").description], snap))
    from . import market
    scan = market.latest_scan(conn, None, faction)
    out = {"enabled": True,
           "snapshot": {k: snap[k] for k in ("id", "ts", "realm", "faction", "house", "last_modified", "auctions",
                                             "units", "items")},
           "scan": None, "summary": None, "items": []}
    if not scan:
        out["note"] = "no full scan of your own for %s to compare with" % faction
        return out
    out["scan"] = {"id": scan["id"], "ts": scan["ts"], "realm": scan["realm"], "auctions": scan["n"],
                   "source": scan.get("source")}
    b = {r[0]: r[1:] for r in conn.execute("SELECT item_id, auctions, qty, min_unit, median_unit FROM bnet_ah_items "
                                           "WHERE snapshot_id=?", (snap["id"],))}
    s = {r[0]: r[1:] for r in conn.execute("SELECT item_id, auctions, qty, min_unit, median_unit FROM ah_scan_items "
                                           "WHERE scan_id=?", (scan["id"],))}
    both = sorted(set(b) & set(s))
    diffs = []
    same_min = 0
    for iid in both:
        bm, sm = b[iid][2], s[iid][2]
        if bm and sm:
            rel = (bm - sm) / float(sm)
            if bm == sm:
                same_min += 1
            diffs.append((abs(rel), iid, rel))
    rels = sorted(abs(d[2]) for d in diffs)
    names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM items")}
    out["summary"] = {
        "items_both": len(both), "items_only_blizzard": len(set(b) - set(s)), "items_only_scan": len(set(s) - set(b)),
        "same_min_price": same_min, "median_abs_diff": round(rels[len(rels) // 2], 4) if rels else None,
        "within_5pct": sum(1 for r in rels if r <= 0.05),
        "units_blizzard": sum(v[1] or 0 for v in b.values()), "units_scan": sum(v[1] or 0 for v in s.values()),
        "age_gap_s": abs(int(snap["ts"]) - int(scan["ts"]))}
    for _a, iid, rel in sorted(diffs, reverse=True)[:limit]:
        out["items"].append({"item_id": iid, "name": names.get(iid),
                             "blizzard": {"auctions": b[iid][0], "qty": b[iid][1], "min_unit": b[iid][2]},
                             "scan": {"auctions": s[iid][0], "qty": s[iid][1], "min_unit": s[iid][2]},
                             "diff": round(rel, 4)})
    return out


# ---------------------------------------------------------------- background worker

def run_once(conn, cli, s, items=True, houses=True):
    res = {}
    if houses and s["houses"]:
        res["snapshots"] = snapshot_houses(conn, cli, s)
    if items:
        res["items"] = refresh_items(conn, cli)
    # always: a static data rebuild (local) or an owner's static push (web version) replaces the items table
    res["applied"] = apply_items(conn)
    return res


def start(cfg, connect, on_change=None):
    """Start the background thread when a client is configured. Returns the thread or None."""
    try:
        s = settings(cfg)
    except BnetError as e:
        log(e)
        return None
    if not s:
        return None
    cli = Client(s)

    def loop():
        next_items = 0
        while True:
            try:
                conn = connect()
                try:
                    do_items = time.time() >= next_items
                    res = run_once(conn, cli, s, items=do_items)
                    if do_items:
                        next_items = time.time() + 6 * 3600
                        if res.get("items", {}).get("items"):
                            log("item data: %s, %d item(s) re-priced" % (res["items"], res.get("applied", 0)))
                    if on_change and (res.get("snapshots") or res.get("applied")):
                        on_change(res)
                finally:
                    conn.close()
            except BnetError as e:
                log(e)
            except Exception as e:  # noqa - keep the thread alive
                log("unexpected error: %r" % (e,))
            time.sleep(SNAPSHOT_CHECK_S)

    t = threading.Thread(target=loop, name="bnet", daemon=True)
    t.start()
    log("Battle.net API enabled (%s, ruleset %d, houses %s)" % (s["region"], s["ruleset"], s["houses"]))
    return t


def main(argv=None):
    """python -m ahtool.bnet [items|houses|compare [Alliance]] - run one step by hand (local app config)."""
    import argparse
    from . import config, db
    ap = argparse.ArgumentParser(description=main.__doc__)
    ap.add_argument("cmd", choices=("items", "houses", "compare"))
    ap.add_argument("faction", nargs="?", default="Alliance")
    a = ap.parse_args(argv)
    cfg = config.load()
    conn = db.connect(cfg["db_path"])
    if a.cmd == "compare":
        print(json.dumps(compare(conn, a.faction), indent=1, ensure_ascii=False))
        return 0
    s = settings(cfg)
    if not s:
        print("No Battle.net client configured: set bnet_client_id and bnet_client_secret in config.json.")
        return 1
    cli = Client(s)
    if a.cmd == "items":
        print(refresh_items(conn, cli, force=True), "re-priced:", apply_items(conn))
    else:
        print("snapshots stored:", snapshot_houses(conn, cli, s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
