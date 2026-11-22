"""Analytics: pure functions taking a sqlite connection (+cfg) and returning JSON-able dicts.

All prices are copper. Fields named *_pct are percentages (12.5 == 12.5 %),
`discount` is a fraction (0.25 == 25 % below baseline).
"""
import bisect
import datetime
import math
import statistics
import threading

from . import market, userstate

EPOCH = datetime.date(2020, 1, 1)

PROFESSIONS = {
    1: ("First Aid", "First Aid"),
    2: ("Blacksmithing", "Blacksmithing"),
    3: ("Leatherworking", "Leatherworking"),
    4: ("Alchemy", "Alchemy"),
    5: ("Herbalism", "Herbalism"),
    6: ("Cooking", "Cooking"),
    7: ("Mining", "Mining"),
    8: ("Tailoring", "Tailoring"),
    9: ("Engineering", "Engineering"),
    10: ("Enchanting", "Enchanting"),
    11: ("Fishing", "Fishing"),
    12: ("Skinning", "Skinning"),
    13: ("Poisons", "Poisons"),
}

CLASS_NAMES = {
    0: "Consumable", 1: "Container", 2: "Weapon", 4: "Armor", 5: "Reagent",
    6: "Projectile", 7: "Trade Goods", 9: "Recipe", 11: "Quiver",
    12: "Quest", 13: "Key", 15: "Miscellaneous",
}


MAX_SANE_PRICE = 2_147_483_647  # gold cap in copper


def day_to_date(day):
    return EPOCH + datetime.timedelta(days=int(day))


def day_to_iso(day):
    return day_to_date(day).isoformat() if day is not None else None


def _c(cfg, key, default):
    if cfg is None:
        return default
    try:
        v = cfg.get(key, default)
    except AttributeError:
        v = getattr(cfg, key, default)
    return default if v is None else v


# ---------------------------------------------------------------- cache

_lock = threading.RLock()
_generation = 0
_cache = {}


def invalidate():
    """Drop all cached analytics (call after an ingest)."""
    global _generation
    with _lock:
        _generation += 1
        _cache.clear()


def _web_default_realm(conn):
    """Web version (a database that receives sync pushes): the realm the owner pushed last, else the realm with the
    most current prices. Never the newest ingest row: every contributor's push writes one. None in the local app."""
    try:
        meta = dict(conn.execute("SELECT key, value FROM main.meta WHERE key IN ('sync_epoch','sync_owner_realm')"))
    except Exception:  # noqa - no meta table yet
        return None
    if "sync_epoch" not in meta:
        return None
    if meta.get("sync_owner_realm"):
        return meta["sync_owner_realm"]
    r = conn.execute("SELECT realm, COUNT(*) AS n FROM price_current GROUP BY realm ORDER BY n DESC, realm "
                     "LIMIT 1").fetchone()
    return r[0] if r else None


def _is_web_db(conn):
    try:
        return conn.execute("SELECT 1 FROM main.meta WHERE key='sync_epoch'").fetchone() is not None
    except Exception:  # noqa
        return False


def get_realm(conn, realm=None):
    if realm:
        return realm
    r = _web_default_realm(conn)
    if r:
        return r
    # Latest ingest that actually carried prices: Auctionator can also save empty realm keys (e.g. "PvP Alliance"
    # next to "PvP"), which would otherwise become the active realm and hide every price.
    r = conn.execute("SELECT realm FROM ingests WHERE items > 0 ORDER BY id DESC LIMIT 1").fetchone()
    if r and r[0]:
        return r[0]
    r = conn.execute("SELECT realm FROM price_current GROUP BY realm ORDER BY COUNT(*) DESC, realm LIMIT 1").fetchone()
    if r:
        return r[0]
    r = conn.execute("SELECT realm FROM ingests ORDER BY id DESC LIMIT 1").fetchone()
    return r[0] if r and r[0] else None


def list_realms(conn):
    # web version: only realms the owner pushed data for - contributors cannot add entries (sync.owner_realms)
    if _is_web_db(conn):
        from . import sync
        owned = sync.owner_realms(conn)
        if owned is not None:
            d = _web_default_realm(conn)
            return sorted(set(owned) | ({d} if d else set()))
    rs = [r[0] for r in conn.execute("SELECT DISTINCT realm FROM price_current ORDER BY realm")]
    for r in conn.execute("SELECT DISTINCT realm FROM ingests WHERE items > 0"):
        if r[0] and r[0] not in rs:
            rs.append(r[0])
    return rs


class _Ctx:
    pass


def _params(cfg):
    return {
        "k": float(_c(cfg, "outlier_mad_k", 3.5)),
        "min_rel": float(_c(cfg, "outlier_min_rel", 0.5)),
        "thin_qty": int(_c(cfg, "thin_qty", 2)),
        "thin_high": float(_c(cfg, "thin_high_factor", 2.0)),
        "conf_days": int(_c(cfg, "conf_min_days", 7)),
        "conf_qty": float(_c(cfg, "conf_min_avg_qty", 5)),
        "bdays": int(_c(cfg, "baseline_days", 14)),
        "cut": float(_c(cfg, "ah_cut", 0.05)),
        "trend_min_profit": float(_c(cfg, "trend_min_profit", 1000)),
    }


def get_ctx(conn, cfg, realm=None):
    """Cached analytics context (per realm / ingest / config). Rebuilt after `invalidate()` or a new ingest."""
    return _ctx(conn, cfg, realm)


def _ctx(conn, cfg, realm=None):
    realm = get_realm(conn, realm)
    p = _params(cfg)
    mx = conn.execute("SELECT MAX(id) FROM ingests").fetchone()[0]
    key = (realm, mx, tuple(sorted(p.items())), _generation)
    with _lock:
        ctx = _cache.get("ctx")
        if ctx is not None and ctx.key == key:
            return ctx
        ctx = _build_ctx(conn, realm, p)
        ctx.key = key
        _cache.clear()
        _cache["ctx"] = ctx
        return ctx


# ---------------------------------------------------------------- outlier cleaning (Hampel)

def _clean(points, p):
    """points: [(day, price)] ascending -> [(day, price, outlier)].

    Hampel filter: a day is an outlier when it deviates from the median of the other points within +-7 days by
    more than k*1.4826*MAD AND by more than min_rel relative to that median. Needs >= 5 points.
    Extra guard: the day must also differ by more than min_rel from each adjacent day (isolated spike), so level
    shifts, trends and the edges of the series are not discarded."""
    n = len(points)
    if n < 5:
        return [(d, v, False) for d, v in points]
    vals = [v for _, v in points]
    lo_v, hi_v = min(vals), max(vals)
    if hi_v <= lo_v * (1 + p["min_rel"]):  # no deviation can exceed min_rel
        return [(d, v, False) for d, v in points]
    days = [d for d, _ in points]
    k = p["k"] * 1.4826
    out = []
    for i, (d, v) in enumerate(points):
        lo = bisect.bisect_left(days, d - 7)
        hi = bisect.bisect_right(days, d + 7)
        nb = vals[lo:i] + vals[i + 1:hi]
        flag = False
        if len(nb) >= 3:
            med = statistics.median(nb)
            if med > 0:
                dev = abs(v - med)
                mad = statistics.median([abs(x - med) for x in nb])
                flag = dev > k * mad and dev / med > p["min_rel"]
                if flag:
                    # A spike is isolated: it differs from BOTH adjacent days. A price confirmed by an
                    # adjacent day is a level shift / trend (or a series edge), not an outlier.
                    for j in (i - 1, i + 1):
                        if 0 <= j < n and vals[j] > 0 and abs(v - vals[j]) / vals[j] <= p["min_rel"]:
                            flag = False
                            break
        out.append((d, v, flag))
    return out


def _build_ctx(conn, realm, p):
    bdays = p["bdays"]
    ctx = _Ctx()
    ctx.realm = realm
    ctx.bdays = bdays
    ctx.p = p
    ctx.items = {}
    for r in conn.execute("SELECT id,name,quality,item_level,class_id,subclass_id,vendor_sell,vendor_buy FROM items"):
        ctx.items[r[0]] = {"id": r[0], "name": r[1], "quality": r[2] if r[2] is not None else 1,
                           "item_level": r[3], "class_id": r[4], "subclass_id": r[5],
                           "vendor_sell": r[6], "vendor_buy": r[7]}
    ctx.vendor = {r[0]: r[1] for r in conn.execute("SELECT item_id, price FROM vendor_items") if r[1]}
    ctx.cur = {}
    for r in conn.execute("SELECT item_id, price, available, last_day FROM price_current WHERE realm=?", (realm,)):
        # Ignore placeholder listings above the gold cap (214748g) - they are not real prices.
        if r[1] is not None and r[1] > MAX_SANE_PRICE:
            continue
        ctx.cur[r[0]] = (r[1], r[2], r[3])
    r = conn.execute("SELECT MAX(day) FROM price_daily WHERE realm=?", (realm,)).fetchone()
    latest = r[0] if r and r[0] is not None else None
    if latest is None and ctx.cur:
        latest = max(v[2] for v in ctx.cur.values() if v[2] is not None)
    ctx.latest_day = latest
    ctx.ndays = {}
    for r in conn.execute("SELECT item_id, COUNT(*) FROM price_daily WHERE realm=? GROUP BY item_id", (realm,)):
        ctx.ndays[r[0]] = r[1]
    ctx.base = {}
    ctx.series = {}     # iid -> [(day, price, outlier)] for the recent window
    ctx.daymap = {}     # iid -> {day: price} without outliers
    ctx.avail14 = {}    # iid -> avg available over the last 14 days
    ctx.outliers = 0
    ctx.flag = {}
    ctx._conf = {}
    ctx._vol = {}
    ctx.trends = None
    if latest is not None:
        floor = latest - bdays - 60
        raw, av = {}, {}
        for iid, day, lo, hi, a in conn.execute(
                "SELECT item_id, day, low, high, available FROM price_daily "
                "WHERE realm=? AND day>=? ORDER BY item_id, day", (realm, floor)):
            pr = lo if lo is not None else hi
            if pr is not None and pr <= MAX_SANE_PRICE:
                raw.setdefault(iid, []).append((day, pr))
                if a is not None:
                    av.setdefault(iid, []).append((day, a))
        for iid, pts in raw.items():
            cl = _clean(pts, p)
            ctx.series[iid] = cl
            ctx.daymap[iid] = {d: v for d, v, o in cl if not o}
            ctx.outliers += sum(1 for _, _, o in cl if o)
            c = ctx.cur.get(iid)
            if not c or c[2] is None:
                continue
            ld = c[2]
            vals = [v for d, v, o in cl if ld - bdays <= d < ld and not o]
            if len(vals) >= 3:
                ctx.base[iid] = statistics.median(vals)
            a14 = [a for d, a in av.get(iid, ()) if ld - 14 < d <= ld]
            if a14:
                ctx.avail14[iid] = sum(a14) / len(a14)
    for iid in ctx.cur:
        ctx.flag[iid] = _flag(ctx, iid)
    ctx.recipes = {}
    ctx.item_profs = None
    ctx.scan_days = None
    return ctx


def _flag(ctx, iid):
    c = ctx.cur.get(iid)
    if not c or not c[0] or c[1] is None:
        return None
    base = ctx.base.get(iid)
    if not base or c[1] > ctx.p["thin_qty"]:
        return None
    if c[0] > base * ctx.p["thin_high"]:
        return "thin_high"
    if c[0] < base * 0.5:
        return "thin_low"
    return None


def price_flag(ctx, iid):
    """None | "thin_high" | "thin_low" for the current AH price."""
    return ctx.flag.get(iid)


def _volatility(ctx, iid):
    if iid in ctx._vol:
        return ctx._vol[iid]
    ser = ctx.series.get(iid)
    v = None
    if ser:
        last = ser[-1][0]
        prices = [pr for d, pr, o in ser if d > last - 30 and not o]
        if len(prices) >= 2:
            m = statistics.fmean(prices)
            if m > 0:
                v = statistics.pstdev(prices) / m
    ctx._vol[iid] = v
    return v


def confidence(ctx, iid):
    """"high" | "medium" | "low": how far the current/baseline price of this item can be trusted."""
    r = ctx._conf.get(iid)
    if r is not None:
        return r
    p = ctx.p
    days = ctx.ndays.get(iid, 0)
    avg = ctx.avail14.get(iid)
    if avg is None:
        c = ctx.cur.get(iid)
        avg = c[1] if c else None
    vol = _volatility(ctx, iid)
    if days < 3 or (avg is not None and avg < 2) or ctx.flag.get(iid) == "thin_high":
        r = "low"
    elif days >= p["conf_days"] and avg is not None and avg >= p["conf_qty"] and (vol is None or vol < 0.5):
        r = "high"
    else:
        r = "medium"
    ctx._conf[iid] = r
    return r


def daily_series(src, item_id, cfg=None, realm=None):
    """Cleaned daily price series [(day, price, outlier_bool)].

    src is a ctx (recent window, ~baseline_days+60 days) or a sqlite connection (full history)."""
    if isinstance(src, _Ctx):
        return list(src.series.get(item_id, ()))
    rlm = get_realm(src, realm)
    pts = []
    for d, lo, hi in src.execute("SELECT day, low, high FROM price_daily WHERE realm=? AND item_id=? ORDER BY day",
                                 (rlm, item_id)):
        v = lo if lo is not None else hi
        if v is not None:
            pts.append((d, v))
    return _clean(pts, _params(cfg))


def buy_price(ctx, item_id, mode="current"):
    """(unit_copper | None, source) for a reagent: min(AH, vendor). A thin_high AH price falls back to the baseline."""
    c = ctx.cur.get(item_id)
    cur = c[0] if c and c[0] else None
    base = ctx.base.get(item_id)
    if mode == "baseline":
        ah = base if base else cur
    else:
        ah = cur
        if cur is not None and base and ctx.flag.get(item_id) == "thin_high":
            ah = base
    vend = ctx.vendor.get(item_id)
    if ah is not None and (vend is None or ah < vend):
        return ah, "ah"
    if vend is not None:
        return vend, "vendor"
    return None, "none"


def sell_price(ctx, item_id, mode="current"):
    """Unit copper a crafted output can realistically sell for (before the AH cut), or None."""
    c = ctx.cur.get(item_id)
    cur = c[0] if c and c[0] else None
    base = ctx.base.get(item_id)
    if mode == "baseline":
        return base if base else cur
    if cur is None:
        return None
    if base:
        if ctx.flag.get(item_id) == "thin_high":
            return base
        return min(cur, base * 1.25)
    return cur


def _is_stale(ctx, iid):
    c = ctx.cur.get(iid)
    if not c or c[2] is None or ctx.latest_day is None:
        return True
    return c[2] < ctx.latest_day - 2


def _discount(ctx, iid):
    b = ctx.base.get(iid)
    c = ctx.cur.get(iid)
    if not b or not c or not c[0]:
        return None
    return 1 - c[0] / b


def _name(ctx, iid):
    it = ctx.items.get(iid)
    return it["name"] if it else None


def _quality(ctx, iid):
    it = ctx.items.get(iid)
    return it["quality"] if it else 1


def _round(v, n=4):
    return None if v is None else round(v, n)


# ---------------------------------------------------------------- status

def status(conn, cfg=None, realm=None):
    ctx = _ctx(conn, cfg, realm)
    r = conn.execute("SELECT ts, file_mtime FROM ingests WHERE realm=? ORDER BY id DESC LIMIT 1",
                     (ctx.realm,)).fetchone()
    names = conn.execute("SELECT COUNT(*) FROM items WHERE name IS NOT NULL").fetchone()[0]
    recs = conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
    return {
        "realm": ctx.realm,
        "realms": list_realms(conn),
        "last_ingest_ts": r[0] if r else None,
        "file_mtime": r[1] if r else None,
        "items_priced": len(ctx.cur),
        "latest_day": ctx.latest_day,
        "latest_date": day_to_iso(ctx.latest_day),
        "item_names_loaded": names,
        "recipes_loaded": recs,
        "unseen_alerts": userstate.unseen_alerts(conn),
        "logger": market.logger_status(conn, cfg or {}),
    }


# ---------------------------------------------------------------- search

def _row(ctx, iid):
    c = ctx.cur.get(iid)
    price, avail, last_day = c if c else (None, None, None)
    return {
        "id": iid, "name": _name(ctx, iid), "quality": _quality(ctx, iid),
        "price": price, "available": avail, "last_date": day_to_iso(last_day),
        "baseline": _round(ctx.base.get(iid), 1),
        "discount": _round(_discount(ctx, iid)),
        "confidence": confidence(ctx, iid), "price_flag": ctx.flag.get(iid),
    }


def search(conn, cfg=None, q="", limit=50, realm=None):
    ctx = _ctx(conn, cfg, realm)
    q = (q or "").strip()
    limit = max(1, min(int(limit or 50), 500))
    if not q:
        ids = sorted((i for i, c in ctx.cur.items() if c[0] and not _is_stale(ctx, i)),
                     key=lambda i: -ctx.cur[i][0])[:limit]
        return [_row(ctx, i) for i in ids]
    ql = q.lower()
    # an item id: plain ASCII digits only (str.isdigit also accepts '²' etc., and int() refuses > 4300 digits)
    qid = int(q) if q.isascii() and q.isdigit() and len(q) <= 10 else None
    scored = []
    for iid, c in ctx.cur.items():
        nm = _name(ctx, iid)
        s = None
        if qid is not None and qid == iid:
            s = 0
        elif nm and ql in nm.lower():
            nl = nm.lower()
            s = 1 if nl == ql else (2 if nl.startswith(ql) else 3)
        if s is not None:
            scored.append((s, -(c[0] or 0), iid))
    scored.sort()
    return [_row(ctx, i) for _, _, i in scored[:limit]]


# ---------------------------------------------------------------- recipes

def _is_deal(ctx, iid, threshold):
    d = _discount(ctx, iid)
    if d is not None and d >= threshold:
        return True
    it = ctx.items.get(iid)
    c = ctx.cur.get(iid)
    return bool(it and it["vendor_sell"] and c and c[0] and c[0] < it["vendor_sell"])


_CONF_RANK = {"low": 0, "medium": 1, "high": 2}


def _recipe_defs(conn, ctx):
    """All recipes once per ctx: spell_id -> [profession, created_item, num_created, [(item, count)]]."""
    d = getattr(ctx, "defs", None)
    if d is None:
        d = {}
        for sid, prof, created, num in conn.execute(
                "SELECT spell_id, profession_id, created_item_id, num_created FROM recipes"):
            d[sid] = [prof, created, num if num else 1, []]
        for sid, iid, cnt in conn.execute("SELECT spell_id, item_id, count FROM recipe_reagents"):
            if sid in d:
                d[sid][3].append((iid, cnt))
        ctx.defs = d
    return d


def _price_on(ctx, iid, day):
    """Cleaned daily price, forward-filling at most 3 days."""
    dm = ctx.daymap.get(iid)
    if not dm:
        return None
    for k in range(4):
        v = dm.get(day - k)
        if v is not None:
            return v
    return None


def _vendor_value(ctx, created, num):
    """Copper from selling the whole output of one craft to a vendor (0 when unknown / not vendorable)."""
    it = ctx.items.get(created) if created else None
    return (it.get("vendor_sell") or 0) * num if it else 0


def _recipe_points(ctx, rd, days):
    """[(day, cost|None, sell|None, profit|None, margin|None)] for the last `days` days."""
    _, created, num, reag = rd
    cut = ctx.p["cut"]
    vend_value = _vendor_value(ctx, created, num)
    out = []
    latest = ctx.latest_day
    for d in range(latest - days + 1, latest + 1):
        cost = 0
        for iid, cnt in reag:
            ah = _price_on(ctx, iid, d)
            vend = ctx.vendor.get(iid)
            u = ah if vend is None or (ah is not None and ah < vend) else vend
            if u is None:
                cost = None
                break
            cost += u * cnt
        sell = None
        if created:
            pp = _price_on(ctx, created, d)
            if pp is not None:
                sell = pp * num
        profit = margin = None
        if cost is not None and (sell is not None or vend_value):
            # Recovery is the better of the AH sale (after the cut) and selling to a vendor
            profit = max(sell * (1 - cut) if sell is not None else 0, vend_value) - cost
            margin = profit / cost if cost > 0 else None
        out.append((d, cost, sell, profit, margin))
    return out


def _trend_stats(profits):
    """(profit_trend_pct, avg7, avg30) from a chronological list of daily profits (None allowed)."""
    def avg(xs):
        xs = [x for x in xs if x is not None]
        return sum(xs) / len(xs) if xs else None
    a, b = avg(profits[-3:]), avg(profits[-10:-3])
    pct = None
    if a is not None and b is not None and b != 0:
        pct = (a - b) / abs(b) * 100
    return pct, avg(profits[-7:]), avg(profits[-30:])


def _trends(conn, ctx):
    """Bulk profit trend for every recipe with a product, computed once per ctx."""
    with _lock:
        if ctx.trends is None:
            tr = {}
            if ctx.latest_day is not None:
                for sid, rd in _recipe_defs(conn, ctx).items():
                    if not rd[1]:
                        continue
                    profits = [x[3] for x in _recipe_points(ctx, rd, 30)]
                    pct, a7, a30 = _trend_stats(profits)
                    tr[sid] = {"spark": [_round(x, 1) for x in profits[-14:]], "pct": _round(pct, 1),
                               "avg7": _round(a7, 1), "avg30": _round(a30, 1)}
            ctx.trends = tr
        return ctx.trends


def recipe_trend(conn, cfg, spell_id, days=30, realm=None):
    ctx = _ctx(conn, cfg, realm)
    spell_id = int(spell_id)
    rd = _recipe_defs(conn, ctx).get(spell_id)
    if rd is None or ctx.latest_day is None:
        return None
    days = max(2, min(int(days or 30), ctx.bdays + 57))
    pts = _recipe_points(ctx, rd, days)
    pct, a7, a30 = _trend_stats([x[3] for x in pts])
    name = _name(ctx, rd[1]) if rd[1] else None
    if not name:
        r = conn.execute("SELECT name FROM spells WHERE id=?", (spell_id,)).fetchone()
        name = r[0] if r else None
    return {"spell_id": spell_id, "name": name or "Spell #%d" % spell_id,
            "points": [{"date": day_to_iso(d), "cost": _round(c, 2), "sell": _round(sl, 2),
                        "profit": _round(pr, 2), "margin": _round(m)} for d, c, sl, pr, m in pts],
            "profit_trend_pct": _round(pct, 1), "avg_profit_7": _round(a7, 1), "avg_profit_30": _round(a30, 1)}


def _build_recipes(conn, ctx, cfg, where, params, mode):
    cut = ctx.p["cut"]
    thr = float(_c(cfg, "deal_threshold", 0.25))
    trends = _trends(conn, ctx)
    recs = conn.execute(
        "SELECT r.spell_id, r.profession_id, r.created_item_id, r.num_created, r.min_skill, r.low_skill, "
        "r.high_skill, s.name FROM recipes r LEFT JOIN spells s ON s.id=r.spell_id WHERE " + where, params).fetchall()
    reag = {}
    for sid, iid, cnt in conn.execute(
            "SELECT rr.spell_id, rr.item_id, rr.count FROM recipe_reagents rr "
            "JOIN recipes r ON r.spell_id=rr.spell_id WHERE " + where, params):
        reag.setdefault(sid, []).append((iid, cnt))
    out = []
    for sid, prof, created, num, mn, lo, hi, sname in recs:
        num = num if num else 1
        cost = 0
        complete = True
        rl = []
        worst = 2
        for iid, cnt in reag.get(sid, []):
            unit, src = buy_price(ctx, iid, mode)
            total = unit * cnt if unit is not None else None
            if total is None:
                complete = False
            else:
                cost += total
            if src == "ah":
                conf, flag = confidence(ctx, iid), ctx.flag.get(iid)
                worst = min(worst, _CONF_RANK[conf])
            else:
                conf, flag = ("high" if src == "vendor" else None), None
            rl.append({"item_id": iid, "name": _name(ctx, iid), "quality": _quality(ctx, iid),
                       "count": cnt, "unit_price": _round(unit, 2), "source": src,
                       "total": _round(total, 2), "is_deal": _is_deal(ctx, iid, thr),
                       "confidence": conf, "price_flag": flag})
        sell = profit = margin = None
        sell_avail = None
        sell_conf = sell_flag = None
        sell_source = None
        vend_value = _vendor_value(ctx, created, num)
        if created:
            p = sell_price(ctx, created, mode)
            c = ctx.cur.get(created)
            sell_avail = c[1] if c else None
            if p is not None:
                sell = p * num
                sell_conf, sell_flag = confidence(ctx, created), ctx.flag.get(created)
            # Recovery = the better of the AH sale after the cut and the vendor price
            ah_net = sell * (1 - cut) if sell is not None else None
            if ah_net is not None and ah_net >= vend_value:
                sell_source, recovery = "ah", ah_net
                worst = min(worst, _CONF_RANK[sell_conf])
            elif vend_value:
                sell_source, recovery = "vendor", vend_value
            if sell_source and complete:
                profit = recovery - cost
                margin = profit / cost if cost > 0 else None
        cname = _name(ctx, created) if created else None
        tr = trends.get(sid)
        out.append({
            "spell_id": sid, "name": cname or sname or ("Spell #%d" % sid),
            "profession_id": prof,
            "created_item_id": created, "created_name": cname,
            "created_quality": _quality(ctx, created) if created else None,
            "num_created": num, "skill": {"min": mn, "low": lo, "high": hi},
            "reagents": rl, "cost": _round(cost, 2), "cost_complete": complete,
            "sell": _round(sell, 2), "vendor_value": _round(vend_value, 2) if created else None,
            "sell_source": sell_source, "profit": _round(profit, 2), "margin": _round(margin),
            "sell_available": sell_avail,
            "sell_confidence": sell_conf, "sell_price_flag": sell_flag,
            "confidence": ("low", "medium", "high")[worst],
            "spark": tr["spark"] if tr else [],
            "profit_trend_pct": tr["pct"] if tr else None,
            "avg_profit_7": tr["avg7"] if tr else None, "avg_profit_30": tr["avg30"] if tr else None,
        })
    return out


def profession_recipes(conn, cfg, prof_id, mode="current", realm=None):
    ctx = _ctx(conn, cfg, realm)
    mode = "baseline" if mode == "baseline" else "current"
    key = (int(prof_id), mode)
    with _lock:
        if key not in ctx.recipes:
            ctx.recipes[key] = _build_recipes(conn, ctx, cfg, "r.profession_id=?", (int(prof_id),), mode)
        return ctx.recipes[key]


def profession_recipes_user(conn, cfg, prof_id, mode="current", realm=None, mine_only=False):
    """profession_recipes + can_craft/known (computed fresh from user state; cache stays untouched)."""
    recs = userstate.annotate(conn, profession_recipes(conn, cfg, prof_id, mode, realm))
    return [r for r in recs if r["can_craft"]] if mine_only else recs


def mycrafts(conn, cfg=None, mode="current", limit=50, realm=None):
    mine = userstate.my_professions(conn)
    known = userstate.known_map(conn)
    out = []
    for pid in sorted(mine):
        for r in userstate.annotate(conn, profession_recipes(conn, cfg, pid, mode, realm), mine, known):
            if r["can_craft"]:
                r["profession_name"] = PROFESSIONS.get(pid, ("Profession %d" % pid,))[0]
                out.append(r)
    out.sort(key=lambda r: (r["profit"] is None, -(r["profit"] or 0)))
    return out[:max(1, min(int(limit or 50), 1000))]


def professions(conn, cfg=None, realm=None):
    from . import gathering  # gathering imports analytics
    ctx = _ctx(conn, cfg, realm)
    mine = userstate.my_professions(conn)
    counts = {r[0]: r[1] for r in conn.execute("SELECT profession_id, COUNT(*) FROM recipes GROUP BY profession_id")}
    ids = sorted(set(PROFESSIONS) | set(counts))
    out = []
    for pid in ids:
        recs = profession_recipes(conn, cfg, pid, "current", ctx.realm) if counts.get(pid) else []
        priced = [r for r in recs if r["profit"] is not None]
        sure = [r for r in priced if r["confidence"] != "low"]
        names = PROFESSIONS.get(pid, ("Profession %d" % pid, "Profession %d" % pid))
        g = gathering.gather_items(conn, cfg, pid, ctx.realm) if gathering.gather_count(pid) else []
        out.append({
            "id": pid, "name": names[0], "name_no": names[1],
            "mine": pid in mine, "my_skill": mine.get(pid),
            "recipe_count": counts.get(pid, 0), "priced_count": len(priced),
            "gather_count": len(g), "gather_priced": sum(1 for r in g if r["price"]),
            "gather_top_price": max((r["price"] for r in g if r["price"]), default=None),
            "profitable_count": sum(1 for r in priced if r["profit"] > 0),
            "best_profit": max((r["profit"] for r in priced), default=None),
            "reliable_count": len(sure),
            "profitable_reliable_count": sum(1 for r in sure if r["profit"] > 0),
            "best_profit_reliable": max((r["profit"] for r in sure), default=None),
        })
    return out


def _item_profs(conn, ctx):
    if ctx.item_profs is None:
        d = {}
        for iid, pid in conn.execute("SELECT DISTINCT rr.item_id, r.profession_id FROM recipe_reagents rr "
                                     "JOIN recipes r ON r.spell_id=rr.spell_id"):
            d.setdefault(iid, set()).add(pid)
        ctx.item_profs = d
    return ctx.item_profs


# ---------------------------------------------------------------- deals

def deals(conn, cfg=None, threshold=None, min_value=0, reason="", realm=None, limit=500, include_low=False):
    ctx = _ctx(conn, cfg, realm)
    thr = float(threshold if threshold is not None else _c(cfg, "deal_threshold", 0.25))
    min_value = int(min_value or 0)
    profs = _item_profs(conn, ctx)
    my_reag = userstate.my_reagent_items(conn)
    out = []
    for iid, c in ctx.cur.items():
        price, avail, last_day = c
        if not price or _is_stale(ctx, iid):
            continue
        base = ctx.base.get(iid)
        disc = _discount(ctx, iid)
        it = ctx.items.get(iid)
        reasons = []
        vprofit = None
        if it and it["vendor_sell"] and price < it["vendor_sell"]:
            reasons.append("under_vendor")
            vprofit = it["vendor_sell"] - price
        below = disc is not None and disc >= thr
        conf = confidence(ctx, iid)
        if below and conf == "low" and not include_low:
            below = False
        if below:
            reasons.append("below_baseline")
            if iid in profs:
                reasons.append("cheap_reagent")
        if not reasons:
            continue
        if reason and reason not in reasons:
            continue
        if max(price, base or 0) < min_value:
            continue
        score = (disc or 0) * math.log10((base or 0) + 1) if below else 0.0
        out.append(({"item_id": iid, "name": _name(ctx, iid), "quality": _quality(ctx, iid),
                     "price": price, "baseline": _round(base, 1), "discount": _round(disc),
                     "available": avail, "days_of_data": ctx.ndays.get(iid, 0),
                     "reasons": reasons, "vendor_profit": vprofit,
                     "professions": [PROFESSIONS.get(p, (None, "Profession %d" % p))[1] for p in sorted(profs.get(iid, ()))],
                     "score": _round(score), "for_my_crafts": iid in my_reag,
                     "confidence": conf, "price_flag": ctx.flag.get(iid)},
                    (0 if vprofit else 1, -(vprofit or 0), -score)))
    out.sort(key=lambda t: t[1])
    return [d for d, _ in out[:limit]]


# ---------------------------------------------------------------- item detail

def _stats(ctx, iid, prices):
    """prices: list of daily prices (ascending by day) for the last 30 days."""
    c = ctx.cur.get(iid)
    cur = c[0] if c else None
    base = ctx.base.get(iid)
    s = {"baseline": _round(base, 1), "avg7": None, "avg30": None, "min30": None, "max30": None,
         "median30": None, "days_of_data": ctx.ndays.get(iid, 0), "volatility": None, "trend7_pct": None}
    if prices:
        s["avg7"] = _round(statistics.fmean(prices[-7:]), 1)
        s["avg30"] = _round(statistics.fmean(prices), 1)
        s["min30"] = min(prices)
        s["max30"] = max(prices)
        s["median30"] = _round(statistics.median(prices), 1)
        if len(prices) >= 2:
            m = statistics.fmean(prices)
            if m > 0:
                s["volatility"] = _round(statistics.pstdev(prices) / m)
    if base and cur:
        s["trend7_pct"] = _round((cur - base) / base * 100, 2)
    return s


def item_detail(conn, cfg, item_id, realm=None):
    ctx = _ctx(conn, cfg, realm)
    item_id = int(item_id)
    it = ctx.items.get(item_id)
    c = ctx.cur.get(item_id)
    hist = conn.execute("SELECT day, low, high, available FROM price_daily WHERE realm=? AND item_id=? ORDER BY day",
                        (ctx.realm, item_id)).fetchall()
    if not it and not c and not hist:
        return None
    last = hist[-1][0] if hist else None
    cleaned = _clean([(h[0], h[1] if h[1] is not None else h[2]) for h in hist
                      if (h[1] if h[1] is not None else h[2]) is not None], ctx.p)
    outl = {d for d, _, o in cleaned if o}
    p30 = [p for d, p, o in cleaned if last is not None and d > last - 30 and not o]
    snaps = conn.execute("SELECT ts, price, available FROM snapshots WHERE realm=? AND item_id=? "
                         "ORDER BY ts DESC LIMIT 500", (ctx.realm, item_id)).fetchall()[::-1]
    used = []
    for sid, sname, pid, cnt in conn.execute(
            "SELECT r.spell_id, COALESCE(ci.name, s.name), r.profession_id, rr.count FROM recipe_reagents rr "
            "JOIN recipes r ON r.spell_id=rr.spell_id LEFT JOIN spells s ON s.id=r.spell_id "
            "LEFT JOIN items ci ON ci.id=r.created_item_id WHERE rr.item_id=? ORDER BY r.profession_id, 2", (item_id,)):
        used.append({"spell_id": sid, "name": sname, "profession": PROFESSIONS.get(pid, (None, None))[1],
                     "profession_id": pid, "count": cnt})
    post = conn.execute("SELECT ts, price, quantity FROM postings WHERE item_id=? ORDER BY ts DESC LIMIT 200",
                        (item_id,)).fetchall()
    return {
        "id": item_id, "name": it["name"] if it else None, "quality": it["quality"] if it else 1,
        "item_level": it["item_level"] if it else None, "class_id": it["class_id"] if it else None,
        "vendor_sell": it["vendor_sell"] if it else None, "vendor_buy": it["vendor_buy"] if it else None,
        "current": {"price": c[0] if c else None, "available": c[1] if c else None,
                    "last_date": day_to_iso(c[2]) if c else None, "stale": _is_stale(ctx, item_id),
                    "confidence": confidence(ctx, item_id), "price_flag": ctx.flag.get(item_id)},
        "stats": dict(_stats(ctx, item_id, p30), outlier_days=len(outl)),
        "history": [{"date": day_to_iso(h[0]), "low": h[1] if h[1] is not None else h[2], "high": h[2],
                     "available": h[3], "outlier": h[0] in outl} for h in hist],
        "snapshots": [{"ts": s[0], "price": s[1], "available": s[2]} for s in snaps],
        "crafted_by": userstate.annotate(conn, _build_recipes(conn, ctx, cfg, "r.created_item_id=?", (item_id,), "current")),
        "used_in": used,
        "postings": [{"ts": p[0], "price": p[1], "quantity": p[2]} for p in post],
        "watch": userstate.get_watch(conn, item_id),
    }


# ---------------------------------------------------------------- market stats

def _mini(ctx, iid):
    c = ctx.cur[iid]
    b = ctx.base.get(iid)
    return {"id": iid, "name": _name(ctx, iid), "quality": _quality(ctx, iid), "price": c[0],
            "available": c[1], "baseline": _round(b, 1),
            "trend7_pct": _round((c[0] - b) / b * 100, 2) if b and c[0] else None}


def market_stats(conn, cfg=None, realm=None):
    ctx = _ctx(conn, cfg, realm)
    live = [i for i, c in ctx.cur.items() if c[0] and not _is_stale(ctx, i)]
    totals = {"items": len(live),
              "listed_quantity": sum(ctx.cur[i][1] or 0 for i in live),
              "market_value": sum(ctx.cur[i][0] * (ctx.cur[i][1] or 0) for i in live)}
    byq, byc = {}, {}
    for i in live:
        p, a, _ = ctx.cur[i]
        v = p * (a or 0)
        q = _quality(ctx, i)
        e = byq.setdefault(q, [0, 0]); e[0] += 1; e[1] += v
        it = ctx.items.get(i)
        cl = it["class_id"] if it and it["class_id"] is not None else -1
        e = byc.setdefault(cl, [0, 0]); e[0] += 1; e[1] += v
    movers = [i for i in live if ctx.base.get(i) and ctx.base[i] >= 100 and ctx.ndays.get(i, 0) >= 5
              and ctx.flag.get(i) != "thin_high"]
    tr = lambda i: (ctx.cur[i][0] - ctx.base[i]) / ctx.base[i]
    movers.sort(key=tr)
    down = [_mini(ctx, i) for i in movers if tr(i) < 0][:20]
    up = [_mini(ctx, i) for i in reversed(movers) if tr(i) > 0][:20]
    if ctx.scan_days is None:
        rows = conn.execute("SELECT day, COUNT(*) FROM price_daily WHERE realm=? GROUP BY day ORDER BY day DESC LIMIT 60",
                            (ctx.realm,)).fetchall()[::-1]
        ctx.scan_days = [{"date": day_to_iso(d), "items": n} for d, n in rows]
    return {
        "totals": totals,
        "by_quality": [{"quality": q, "count": v[0], "value": v[1]} for q, v in sorted(byq.items())],
        "by_class": [{"class_id": k, "class_name": CLASS_NAMES.get(k, "Unknown"),
                      "count": v[0], "value": v[1]}
                     for k, v in sorted(byc.items(), key=lambda kv: -kv[1][1])],
        "movers_up": up, "movers_down": down,
        "most_listed": [_mini(ctx, i) for i in sorted(live, key=lambda i: -(ctx.cur[i][1] or 0))[:20]],
        "most_valuable": [_mini(ctx, i) for i in sorted(live, key=lambda i: -ctx.cur[i][0])[:20]],
        "scan_days": ctx.scan_days,
    }


# ---------------------------------------------------------------- postings

def postings(conn, cfg=None, realm=None, limit=300):
    ctx = _ctx(conn, cfg, realm)
    out = []
    for iid, ts, price, qty in conn.execute(
            "SELECT item_id, ts, price, quantity FROM postings ORDER BY ts DESC LIMIT ?", (limit,)):
        c = ctx.cur.get(iid)
        cp = c[0] if c else None
        out.append({"item_id": iid, "name": _name(ctx, iid), "ts": ts, "price": price, "quantity": qty,
                    "current_price": cp,
                    "diff_pct": _round((price - cp) / cp * 100, 2) if cp and price is not None else None})
    return out


# ---------------------------------------------------------------- watchlist / alerts views

def watch_triggered(ctx, cfg, iid, max_price):
    """Alert condition for a watch row (price <= max_price, or deal when max_price is NULL)."""
    c = ctx.cur.get(iid)
    if not c or not c[0] or _is_stale(ctx, iid):
        return False
    if max_price is not None:
        return c[0] <= max_price
    return _is_deal(ctx, iid, float(_c(cfg, "deal_threshold", 0.25)))


def watchlist(conn, cfg=None, realm=None):
    ctx = _ctx(conn, cfg, realm)
    out = []
    for iid, mp, en, note in conn.execute(
            "SELECT item_id, max_price, enabled, note FROM watchlist ORDER BY created_at, item_id"):
        c = ctx.cur.get(iid)
        out.append({"item_id": iid, "name": _name(ctx, iid), "quality": _quality(ctx, iid),
                    "max_price": mp, "enabled": bool(en), "note": note,
                    "price": c[0] if c else None, "baseline": _round(ctx.base.get(iid), 1),
                    "discount": _round(_discount(ctx, iid)), "available": c[1] if c else None,
                    "triggered": bool(en) and watch_triggered(ctx, cfg, iid, mp)})
    return out


def alerts(conn, cfg=None, limit=50, realm=None):
    ctx = _ctx(conn, cfg, realm)
    limit = max(1, min(int(limit or 50), 500))
    return [{"id": r[0], "ts": r[1], "item_id": r[2], "name": _name(ctx, r[2]), "quality": _quality(ctx, r[2]),
             "kind": r[3], "price": r[4], "threshold": r[5], "message": r[6], "seen": bool(r[7])}
            for r in conn.execute("SELECT id, ts, item_id, kind, price, threshold, message, seen FROM alerts "
                                  "ORDER BY id DESC LIMIT ?", (limit,))]


# ---------------------------------------------------------------- trending crafts

def trending_crafts(conn, cfg=None, limit=20, mine=False, include_low=False, realm=None):
    """{up:[recipe], down:[recipe]}: biggest positive / negative profit_trend_pct among recipes with a real profit."""
    ctx = _ctx(conn, cfg, realm)
    limit = max(1, min(int(limit or 20), 200))
    min_profit = ctx.p["trend_min_profit"]
    counts = [r[0] for r in conn.execute("SELECT DISTINCT profession_id FROM recipes ORDER BY 1")]
    mp = userstate.my_professions(conn)
    known = userstate.known_map(conn)
    cand = []
    for pid in counts:
        for r in userstate.annotate(conn, profession_recipes(conn, cfg, pid, "current", ctx.realm), mp, known):
            if r["profit_trend_pct"] is None or r["profit"] is None:
                continue
            if mine and not r["can_craft"]:
                continue
            if not include_low and r["confidence"] == "low":
                continue
            if max(r["avg_profit_7"] or 0, r["avg_profit_30"] or 0) < min_profit:
                continue
            r["profession_name"] = PROFESSIONS.get(pid, ("Profession %d" % pid,))[0]
            cand.append(r)
    up = sorted((r for r in cand if r["profit_trend_pct"] > 0), key=lambda r: -r["profit_trend_pct"])[:limit]
    down = sorted((r for r in cand if r["profit_trend_pct"] < 0), key=lambda r: r["profit_trend_pct"])[:limit]
    return {"up": up, "down": down}
