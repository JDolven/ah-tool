"""Daily market report: one 24-hour window of the auction house, summarised (AH Tool Logger full scans).

    daily(conn, cfg, date=None, realm=None) -> dict

The window ends at the last full scan of `date` (local date, ISO) or at the latest full scan, and starts 24 hours
earlier. The 24 hours before that are "the day before". Everything comes from data the app already stores:

* listings through the day: auctions per full scan (ah_scans.n)
* gold traded / units sold: estimated sales (ah_sales, kind 'sold') of the scan pairs that end inside the window.
  Scan pairs only cover the time between two scans, so the totals are scaled to 24 hours by the covered time.
* turnover: share of the listings of the last-but-one scan that were gone in the last scan
* prices vs the day before: per item the median of the per-scan median unit buyout (ah_scan_items) in each window.
  Without scans the day before, Auctionator's daily price (price_daily, low else high) of the two days is used.
* group moves: median price change of the busy items per trade-goods group
"""
import datetime
import re
import statistics
import time
from collections import defaultdict

from . import analytics, market

DAY_S = 86400
TOP_N = 5
MOVERS_N = 3
BUSY_QTY = 10        # average listed quantity (scans) / max available (Auctionator) for an item to count as "busy"
FLAT = 0.005         # +-0.5 % counts as unchanged
ICON_URL = "https://wow.zamimg.com/images/wow/icons/medium/%s.jpg"
_ICON_RE = re.compile(r"[A-Za-z0-9_\-]+")  # icon names that are safe to put into the URL

# (key, label, class_id, subclass_id or None)
GROUPS = [
    ("herbs", "Herbs", 7, 9),
    ("metal", "Metal & Stone", 7, 7),
    ("cloth", "Cloth", 7, 5),
    ("leather", "Leather", 7, 6),
    ("enchanting", "Enchanting mats", 7, 12),
    ("elemental", "Elemental", 7, 10),
    ("meat", "Cooking", 7, 8),
    ("parts", "Parts", 7, 1),
    ("consumables", "Consumables", 0, None),
]


def _date(ts):
    return datetime.date.fromtimestamp(ts).isoformat()


def _median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _r(v, n=4):
    return None if v is None else round(v, n)


def _scans(conn, realm):
    q = "SELECT id, ts, n, faction, realm FROM ah_scans WHERE partial=0"
    args = ()
    if realm:
        q += " AND realm=?"
        args = (realm,)
    return [dict(r) for r in conn.execute(q + " ORDER BY ts", args)]


def _covered(scans, window):
    """Seconds covered by the scan pairs (previous full scan -> scan) that end in the window."""
    ts_all = [s["ts"] for s in scans]
    covered = 0
    for s in window:
        i = ts_all.index(s["ts"])
        if i > 0 and s["ts"] - ts_all[i - 1] <= market.MAX_DIFF_GAP:
            covered += s["ts"] - ts_all[i - 1]
    return covered


def _sales(conn, scan_ids):
    """Sold auctions of the scan pairs ending in scan_ids -> (per item {sold, value}, sold auctions)."""
    if not scan_ids:
        return {}, 0
    marks = ",".join("?" * len(scan_ids))
    per = defaultdict(lambda: {"sold": 0, "value": 0})
    auctions = 0
    for iid, cnt, val, n in conn.execute(
            "SELECT item_id, SUM(count), SUM(count*unit_price), COUNT(*) FROM ah_sales "
            "WHERE scan_id IN (%s) AND kind='sold' GROUP BY item_id" % marks, scan_ids):
        per[iid]["sold"] = cnt or 0
        per[iid]["value"] = val or 0
        auctions += n
    return per, auctions


def _scan_prices(conn, scan_ids):
    """{item_id: (median of per-scan median unit, average listed qty, scans seen)} over scan_ids."""
    if not scan_ids:
        return {}
    marks = ",".join("?" * len(scan_ids))
    acc = defaultdict(lambda: ([], []))
    for iid, med, qty in conn.execute(
            "SELECT item_id, median_unit, qty FROM ah_scan_items WHERE scan_id IN (%s)" % marks, scan_ids):
        acc[iid][0].append(med)
        acc[iid][1].append(qty or 0)
    n = len(scan_ids)
    return {iid: (_median(meds), sum(qtys) / n, len(qtys)) for iid, (meds, qtys) in acc.items()}


def _auctionator_prices(conn, day):
    """Fallback without scans the day before: Auctionator daily price -> {item_id: (price, available, 1)}."""
    realm = analytics.get_realm(conn)
    out = {}
    if not realm:
        return out
    for iid, low, high, avail in conn.execute(
            "SELECT item_id, low, high, available FROM price_daily WHERE realm=? AND day=?", (realm, day)):
        p = low or high
        if p:
            out[iid] = (p, avail or 0, 1)
    return out


def _gone(conn, win):
    """Share of the listings of the last-but-one scan that were gone in the last scan of the window."""
    if not win:
        return None
    last = win[-1]
    row = conn.execute("SELECT prev_scan_id, COUNT(*), SUM(kind='sold'), SUM(kind='expired'), SUM(kind='relisted') "
                       "FROM ah_sales WHERE scan_id=? GROUP BY prev_scan_id", (last["id"],)).fetchone()
    if not row or not row[0]:
        return None
    prev = conn.execute("SELECT ts, n FROM ah_scans WHERE id=?", (row[0],)).fetchone()
    if not prev or not prev[1]:
        return None
    return {"auctions": row[1], "of": prev[1], "pct": _r(row[1] / prev[1]), "from_ts": prev[0], "to_ts": last["ts"],
            "sold": row[2] or 0, "expired": row[3] or 0, "relisted": row[4] or 0}


def _swing(conn, win, items):
    """Busy item with the biggest gap between its cheapest and dearest per-scan median price in the window."""
    if len(win) < 3:
        return None
    ids = [s["id"] for s in win]
    ts_of = {s["id"]: s["ts"] for s in win}
    marks = ",".join("?" * len(ids))
    per = defaultdict(list)
    for sid, iid, med, qty in conn.execute(
            "SELECT scan_id, item_id, median_unit, qty FROM ah_scan_items WHERE scan_id IN (%s) AND median_unit>0 "
            "AND qty>=?" % marks, ids + [BUSY_QTY]):
        per[iid].append((med, ts_of[sid]))
    best = None
    for iid, pts in per.items():
        if len(pts) < max(3, len(ids) // 2):
            continue
        lo, hi = min(pts), max(pts)
        ratio = hi[0] / lo[0]
        if ratio >= 1.5 and (best is None or ratio > best[0]):
            best = (ratio, iid, lo, hi)
    if not best:
        return None
    ratio, iid, lo, hi = best
    items.add(iid)
    return {"item_id": iid, "ratio": _r(ratio, 2), "low": lo[0], "low_ts": lo[1], "high": hi[0], "high_ts": hi[1]}


def daily(conn, cfg, date=None, realm=None):
    market.ensure_schema(conn)
    realm = market._realm_filter(conn, realm)
    scans = _scans(conn, realm)
    base = {"installed": market.addon_installed(cfg), "dates": [], "window": None}
    if not scans:
        return base
    faction = scans[-1]["faction"]
    scans = [s for s in scans if s["faction"] == faction]
    dates = sorted({_date(s["ts"]) for s in scans}, reverse=True)
    base["dates"] = dates
    if date:
        on_day = [s for s in scans if _date(s["ts"]) == date]
        if not on_day:
            raise ValueError("no full scan on %s" % date)
        end = on_day[-1]["ts"]
    else:
        end = scans[-1]["ts"]
    start, prev_start = end - DAY_S, end - 2 * DAY_S
    win = [s for s in scans if start < s["ts"] <= end]
    prev = [s for s in scans if prev_start < s["ts"] <= start]
    win_ids = [s["id"] for s in win]
    prev_ids = [s["id"] for s in prev]
    items = set()

    # ---- listings through the day
    listings = [{"ts": s["ts"], "auctions": s["n"]} for s in win]
    peak = max(win, key=lambda s: s["n"])
    low = min(win, key=lambda s: s["n"])

    # ---- estimated sales
    sales, sold_auctions = _sales(conn, win_ids)
    psales, _ = _sales(conn, prev_ids)
    covered, pcovered = _covered(scans, win), _covered(scans, prev)
    scale = DAY_S / covered if covered else None
    pscale = DAY_S / pcovered if pcovered else None
    gold_obs = sum(v["value"] for v in sales.values())
    units_obs = sum(v["sold"] for v in sales.values())
    gold_day = gold_obs * scale if scale else None
    pgold_day = sum(v["value"] for v in psales.values()) * pscale if pscale else None

    def sale_row(iid, v):
        items.add(iid)
        return {"item_id": iid, "sold": v["sold"], "value": v["value"],
                "sold_day": _r(v["sold"] * scale, 1) if scale else None,
                "value_day": int(round(v["value"] * scale)) if scale else None,
                "avg_unit": _r(v["value"] / v["sold"], 1) if v["sold"] else None}

    by_gold = [sale_row(i, v) for i, v in sorted(sales.items(), key=lambda kv: -kv[1]["value"])[:TOP_N] if v["value"]]
    by_units = [sale_row(i, v) for i, v in sorted(sales.items(), key=lambda kv: -kv[1]["sold"])[:TOP_N] if v["sold"]]

    # ---- most listed (last scan of the window)
    most_listed = []
    for iid, qty, auctions, sellers, mn in conn.execute(
            "SELECT item_id, qty, auctions, sellers, min_unit FROM ah_scan_items WHERE scan_id=? "
            "ORDER BY qty DESC LIMIT ?", (win[-1]["id"], TOP_N)):
        items.add(iid)
        most_listed.append({"item_id": iid, "qty": qty, "auctions": auctions, "sellers": sellers, "min_unit": mn})

    # ---- prices vs the day before
    if prev_ids:
        price_src = "scans"
        cur_p, old_p = _scan_prices(conn, win_ids), _scan_prices(conn, prev_ids)
    else:
        price_src = "auctionator"
        day = (datetime.date.fromtimestamp(end) - analytics.EPOCH).days
        cur_p, old_p = _auctionator_prices(conn, day), _auctionator_prices(conn, day - 1)
    changes = {}
    for iid, (p, q, _n) in cur_p.items():
        o = old_p.get(iid)
        if not o or not p or not o[0] or q < BUSY_QTY or o[1] < BUSY_QTY:
            continue
        changes[iid] = (p / o[0] - 1, o[0], p)
    ranked = sorted(changes.items(), key=lambda kv: kv[1][0])

    def mover(iid, c):
        items.add(iid)
        return {"item_id": iid, "pct": _r(c[0]), "before": int(round(c[1])), "now": int(round(c[2]))}

    up = [mover(i, c) for i, c in reversed(ranked[-MOVERS_N:]) if c[0] > FLAT]
    down = [mover(i, c) for i, c in ranked[:MOVERS_N] if c[0] < -FLAT]

    # ---- group moves
    meta = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT id, class_id, subclass_id FROM items")} if changes else {}

    def group(key, label, ids):
        pcts = [changes[i][0] for i in ids]
        if len(pcts) < 3:
            return None
        med = statistics.median(pcts)
        return {"key": key, "label": label, "items": len(pcts), "pct": _r(med),
                "cheaper": sum(1 for p in pcts if p < -FLAT), "pricier": sum(1 for p in pcts if p > FLAT)}

    groups = [g for g in [group("all", "All busy goods", list(changes))] if g]
    for key, label, cls, sub in GROUPS:
        ids = [i for i in changes if meta.get(i, (None, None))[0] == cls and (sub is None or meta[i][1] == sub)]
        g = group(key, label, ids)
        if g:
            groups.append(g)

    # ---- highlights
    swing = _swing(conn, win, items)
    sold_all = conn.execute("SELECT SUM(kind='sold'), SUM(kind='expired') FROM ah_sales WHERE scan_id IN (%s)"
                            % ",".join("?" * len(win_ids)), win_ids).fetchone() if win_ids else (0, 0)
    deal = None
    try:
        for d in analytics.deals(conn, cfg, reason="below_baseline", limit=50):
            if d.get("confidence") == "high" and (d.get("available") or 0) >= BUSY_QTY:
                deal = {"item_id": d["item_id"], "price": d["price"], "baseline": d["baseline"],
                        "discount": d["discount"]}
                items.add(d["item_id"])
                break
    except Exception:  # the report must not fail because of Auctionator data
        deal = None

    # ---- item names, quality and icons
    info = {}
    if items:
        ids = sorted(items)
        marks = ",".join("?" * len(ids))
        for iid, name, q in conn.execute("SELECT id, name, quality FROM items WHERE id IN (%s)" % marks, ids):
            info[iid] = {"name": name, "quality": q if q is not None else 1, "icon_url": None}
        try:
            for iid, icon in conn.execute("SELECT id, icon FROM item_tooltip WHERE id IN (%s)" % marks, ids):
                if isinstance(icon, str) and _ICON_RE.fullmatch(icon):
                    info.setdefault(iid, {"name": None, "quality": 1})["icon_url"] = ICON_URL % icon
        except Exception:  # item_tooltip may not exist yet
            pass

    def fill(rows):
        for r in rows:
            i = info.get(r["item_id"], {})
            r["name"] = i.get("name")
            r["quality"] = i.get("quality", 1)
            r["icon_url"] = i.get("icon_url")
        return rows

    for rows in (by_gold, by_units, most_listed, up, down):
        fill(rows)
    for one in (swing, deal):
        if one:
            fill([one])

    return {
        **base,
        "date": _date(end),
        "realm": win[-1]["realm"], "faction": faction,
        "window": {"start": start, "end": end, "scans": len(win), "prev_scans": len(prev),
                   "first_ts": win[0]["ts"], "covered_s": covered, "prev_covered_s": pcovered},
        "listings": listings,
        "peak": {"ts": peak["ts"], "auctions": peak["n"]},
        "low": {"ts": low["ts"], "auctions": low["n"]},
        "latest": {"ts": win[-1]["ts"], "auctions": win[-1]["n"]},
        "gone": _gone(conn, win),
        "totals": {"gold_day": int(round(gold_day)) if gold_day is not None else None,
                   "gold_observed": gold_obs, "units_day": int(round(units_obs * scale)) if scale else None,
                   "units_observed": units_obs, "sold_auctions": sold_auctions, "items_sold": len(sales),
                   "gold_change": _r(gold_day / pgold_day - 1) if gold_day is not None and pgold_day else None,
                   "sell_through": _r(sold_all[0] / (sold_all[0] + sold_all[1]))
                   if sold_all and (sold_all[0] or 0) + (sold_all[1] or 0) else None},
        "top_gold": by_gold, "top_units": by_units, "most_listed": most_listed,
        "price_source": price_src, "price_items": len(changes),
        "cheaper": sum(1 for c in changes.values() if c[0] < -FLAT),
        "pricier": sum(1 for c in changes.values() if c[0] > FLAT),
        "up": up, "down": down, "groups": groups,
        "swing": swing, "deal": deal,
        "generated_ts": int(time.time()),
    }
