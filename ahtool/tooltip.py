"""GET /api/tooltip/<id>: Wowhead-style item tooltip as structured JSON + our AH section.

Response:
  {id, name, quality, icon, icon_url, item_level, custom, source:[..],
   lines: [{text, right?, style, money?}],   # style: white|green|yellow|gray|red|q<N>
   ah: {price, baseline, discount, available, last_date, stale, days_of_data,
        used_in: {count, professions:[..]},
        crafted_by: [{spell_id, profession, skill, num_created, reagents:[{item_id,name,quality,count}],
                      cost, cost_complete, profit}],
        teaches: {spell_id, name, profession, created_item_id, created_name, created_quality, reagents:[..]} | null}}

Cached in memory per analytics context (= per ingest / realm); typical cost after warm-up is well below 1 ms.
"""
import json
import threading

from . import analytics
from .tooltipdata import FALLBACK_ICON, ICON_URL

_lock = threading.Lock()
_cache = {}
_cache_key = None
MAX_CACHE = 5000


def _style_q(q):
    return "q%d" % (q if isinstance(q, int) and 0 <= q <= 7 else 1)


def build_lines(s):
    """Static tooltip dict -> ordered display lines (same order as the in-game tooltip)."""
    L = []

    def add(text, style="white", right=None, **kw):
        e = {"text": text, "style": style}
        if right:
            e["right"] = right
        e.update(kw)
        L.append(e)

    if s.get("bind"):
        add(s["bind"])
    if s.get("unique"):
        add("Unique")
    if s.get("begins_quest"):
        add("This Item Begins a Quest")
    if s.get("container"):
        add(s["container"])
    if s.get("slot") or s.get("type"):
        add(s.get("slot") or "", right=s.get("type"))
    dm = s.get("damage")
    if dm:
        speed = dm.get("speed")
        if dm.get("min") is not None:
            school = (" " + dm["school"]) if dm.get("school") else ""
            add("%d - %d%s Damage" % (dm["min"], dm["max"], school),
                right=("Speed %.2f" % speed) if speed else None)
            for x in dm.get("extra") or []:
                add(("+%d - %d %s Damage" % (x["min"], x["max"], x["school"] or "")).replace("  ", " "))
            if dm.get("dps") is not None:
                add("(%.1f damage per second)" % dm["dps"])
        elif speed:
            add("", right="Speed %.2f" % speed)
    if s.get("armor"):
        add("%d Armor" % s["armor"])
    for t in s.get("stats") or []:
        add(t)
    for t in s.get("resistances") or []:
        add(t)
    if s.get("durability"):
        add("Durability %d / %d" % (s["durability"], s["durability"]))
    if s.get("classes"):
        add("Classes: " + s["classes"])
    if s.get("required_level") and s["required_level"] > 1:
        add("Requires Level %d" % s["required_level"])
    if s.get("skill"):
        add(s["skill"])
    if s.get("item_level"):
        add("Item Level %d" % s["item_level"], style="yellow")
    for e in s.get("effects") or []:
        text = "%s: %s" % (e["trigger"], e["text"])
        if e.get("cooldown"):
            text += " (%s)" % e["cooldown"]
        add(text, style="green")
    if s.get("description"):
        add('"%s"' % s["description"], style="yellow")
    if s.get("sell_price"):
        add("Sell Price:", money=s["sell_price"])
    return L


def _static(conn, item_id):
    r = conn.execute("SELECT icon, data FROM item_tooltip WHERE id=?", (item_id,)).fetchone()
    if r is None:
        return None, None
    try:
        return r[0], json.loads(r[1])
    except (TypeError, ValueError):
        return r[0], None


def _recipe_brief(r):
    return {"spell_id": r["spell_id"],
            "profession": analytics.PROFESSIONS.get(r.get("profession_id"), ("Profession",))[0],
            "skill": (r.get("skill") or {}).get("min"),
            "num_created": r.get("num_created"),
            "reagents": [{"item_id": g["item_id"], "name": g["name"], "quality": g["quality"], "count": g["count"],
                          "unit_price": g["unit_price"], "source": g["source"]} for g in r.get("reagents") or []],
            "cost": r.get("cost"), "cost_complete": r.get("cost_complete"), "profit": r.get("profit")}


def _ah(conn, cfg, ctx, item_id, teaches_spell):
    c = ctx.cur.get(item_id)
    base = ctx.base.get(item_id)
    out = {"price": c[0] if c else None, "available": c[1] if c else None,
           "last_date": analytics.day_to_iso(c[2]) if c else None,
           "stale": analytics._is_stale(ctx, item_id) if c else None,
           "baseline": analytics._round(base, 1), "discount": analytics._round(analytics._discount(ctx, item_id)),
           "days_of_data": ctx.ndays.get(item_id, 0)}
    profs = {}
    n = 0
    for pid, cnt in conn.execute("SELECT r.profession_id, COUNT(*) FROM recipe_reagents rr JOIN recipes r "
                                 "ON r.spell_id=rr.spell_id WHERE rr.item_id=? GROUP BY 1", (item_id,)):
        profs[pid] = cnt
        n += cnt
    out["used_in"] = {"count": n, "professions": [analytics.PROFESSIONS.get(p, ("Profession %d" % p,))[0]
                                                  for p in sorted(profs, key=lambda p: -profs[p])]}
    crafted = analytics._build_recipes(conn, ctx, cfg, "r.created_item_id=?", (item_id,), "current")
    out["crafted_by"] = [_recipe_brief(r) for r in crafted[:3]]
    out["teaches"] = None
    if teaches_spell:
        rs = analytics._build_recipes(conn, ctx, cfg, "r.spell_id=?", (int(teaches_spell),), "current")
        if rs:
            r = rs[0]
            t = _recipe_brief(r)
            t.update({"name": r["name"], "created_item_id": r["created_item_id"],
                      "created_name": r["created_name"], "created_quality": r["created_quality"],
                      "sell": r.get("sell")})
            out["teaches"] = t
    return out


def tooltip(conn, cfg, item_id, realm=None):
    """Tooltip dict for item_id, or None when the item is completely unknown."""
    global _cache_key
    item_id = int(item_id)
    ctx = analytics._ctx(conn, cfg, realm)
    key = ctx.key
    with _lock:
        if _cache_key != key:
            _cache.clear()
            _cache_key = key
        hit = _cache.get(item_id)
    if hit is not None:
        return hit
    icon, s = _static(conn, item_id)
    it = ctx.items.get(item_id)
    if s is None:
        if it is None and item_id not in ctx.cur:
            return None
        s = {"id": item_id, "name": it["name"] if it else None, "quality": it["quality"] if it else 1,
             "item_level": it["item_level"] if it else None, "source": ["custom"]}
        if it and it.get("vendor_sell"):
            s["sell_price"] = it["vendor_sell"]
    name = s.get("name") or (it["name"] if it else None)
    quality = s.get("quality")
    if quality is None:
        quality = it["quality"] if it else 1
    icon = icon or FALLBACK_ICON
    res = {
        "id": item_id, "name": name, "quality": quality, "icon": icon, "icon_url": ICON_URL % icon,
        "item_level": s.get("item_level"), "required_level": s.get("required_level"),
        "custom": "csv" not in (s.get("source") or []) and "items_lua" not in (s.get("source") or [])
        and "forever" not in (s.get("source") or []),
        "source": s.get("source") or [],
        "lines": build_lines(s),
        "ah": _ah(conn, cfg, ctx, item_id, s.get("teaches")),
    }
    with _lock:
        if _cache_key == key:
            if len(_cache) >= MAX_CACHE:
                _cache.clear()
            _cache[item_id] = res
    return res


def invalidate():
    global _cache_key
    with _lock:
        _cache.clear()
        _cache_key = None
