"""Alert rule evaluation (run after every non-skipped ingest)."""
import json
import time

from . import analytics, notify, userstate

DAY = 24 * 3600


def fmt_copper(c):
    c = int(c)
    g, rem = divmod(c, 10000)
    s, cp = divmod(rem, 100)
    parts = []
    if g:
        parts.append("%dg" % g)
    if s:
        parts.append("%ds" % s)
    if cp or not parts:
        parts.append("%dc" % cp)
    return " ".join(parts)


def _due(last_price, last_ts, price, now):
    """Dedupe: fire when never alerted, price dropped below the last alerted price, or last alert older than 24h."""
    return last_price is None or price < last_price or last_ts is None or last_ts < now - DAY


def _meta_state(conn, key):
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if not r:
        return None, None
    try:
        d = json.loads(r[0])
        return d["price"], d["ts"]
    except (ValueError, KeyError, TypeError):
        return None, None


def evaluate(conn, cfg=None, notify_user=True, now=None, realm=None):
    """Evaluate all rules; insert alerts; return list of new alert dicts. Sends one toast if enabled."""
    now = int(now if now is not None else time.time())
    ctx = analytics._ctx(conn, cfg, realm)
    settings = userstate.get_settings(conn)
    thr = float(analytics._c(cfg, "deal_threshold", 0.25))
    new = []

    def add(iid, kind, price, threshold, message):
        conn.execute("INSERT INTO alerts(ts,item_id,kind,price,threshold,message,seen) VALUES(?,?,?,?,?,?,0)",
                     (now, iid, kind, price, threshold, message))
        new.append({"item_id": iid, "kind": kind, "price": price, "threshold": threshold, "message": message})

    def label(iid):
        return analytics._name(ctx, iid) or "Item #%d" % iid

    # --- watchlist
    for iid, mp, en, lp, lts in conn.execute(
            "SELECT item_id, max_price, enabled, last_alert_price, last_alert_ts FROM watchlist").fetchall():
        c = ctx.cur.get(iid)
        price = c[0] if c else None
        if en and analytics.watch_triggered(ctx, cfg, iid, mp):
            if _due(lp, lts, price, now):
                if mp is not None:
                    msg = "%s %s <= %s" % (label(iid), fmt_copper(price), fmt_copper(mp))
                else:
                    d = analytics._discount(ctx, iid)
                    msg = "%s %s (deal%s)" % (label(iid), fmt_copper(price),
                                              ", -%d%%" % round(d * 100) if d and d > 0 else "")
                add(iid, "watch", price, mp, msg)
                conn.execute("UPDATE watchlist SET last_alert_price=?, last_alert_ts=? WHERE item_id=?",
                             (price, now, iid))
        elif lp is not None or lts is not None:
            conn.execute("UPDATE watchlist SET last_alert_price=NULL, last_alert_ts=NULL WHERE item_id=?", (iid,))

    # --- vendor flips / my reagents (state in meta: alertstate:<kind>:<item>)
    flip_min = int(settings.get("notify_vendor_flip_min") or 0)
    rule_hits = {}  # (kind, iid) -> (price, threshold, message)
    if flip_min > 0:
        for iid, c in ctx.cur.items():
            it = ctx.items.get(iid)
            if c[0] and not analytics._is_stale(ctx, iid) and it and it["vendor_sell"] \
                    and c[0] < it["vendor_sell"] and it["vendor_sell"] - c[0] >= flip_min:
                vp = it["vendor_sell"] - c[0]
                rule_hits[("vendor_flip", iid)] = (c[0], flip_min, "%s %s: vendor flip +%s" % (
                    label(iid), fmt_copper(c[0]), fmt_copper(vp)))
    if settings.get("notify_my_reagents"):
        for iid in userstate.my_reagent_items(conn):
            c = ctx.cur.get(iid)
            d = analytics._discount(ctx, iid)
            if c and c[0] and not analytics._is_stale(ctx, iid) and d is not None and d >= thr:
                rule_hits[("my_reagent", iid)] = (c[0], ctx.base.get(iid), "%s %s (-%d%%, reagent for your crafts)" % (
                    label(iid), fmt_copper(c[0]), round(d * 100)))
    prefix = "alertstate:"
    existing = {}
    for k, v in conn.execute("SELECT key, value FROM meta WHERE key LIKE ?", (prefix + "%",)):
        existing[k] = v
    for (kind, iid), (price, threshold, msg) in rule_hits.items():
        key = "%s%s:%d" % (prefix, kind, iid)
        lp, lts = _meta_state(conn, key) if key in existing else (None, None)
        if _due(lp, lts, price, now):
            add(iid, kind, price, int(threshold) if threshold is not None else None, msg)
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                         (key, json.dumps({"price": price, "ts": now})))
    for key in existing:  # condition false -> reset
        parts = key[len(prefix):].split(":")
        try:
            hit = (parts[0], int(parts[1])) in rule_hits
        except (IndexError, ValueError):
            hit = False
        if not hit:
            conn.execute("DELETE FROM meta WHERE key=?", (key,))
    conn.commit()

    if new and notify_user and settings.get("notify_windows"):
        shown = [a["message"] for a in new[:5]]
        body = ", ".join(shown) + (", +%d more" % (len(new) - 5) if len(new) > 5 else "")
        notify.send_toast("AH Tool: %d alert%s" % (len(new), "" if len(new) == 1 else "s"), body)
    return new
