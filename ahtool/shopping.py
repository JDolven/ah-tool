"""Shopping list computation + Auctionator import string."""
from . import analytics, market, userstate


def _gold_str(copper):
    """Max price in gold for the Auctionator term, rounded to 4 decimals (e.g. 0.0023)."""
    return repr(round(copper / 10000.0, 4))


def auctionator_term(name, max_price_copper, qty):
    """`"<name>";;0;0;0;0;0;0;0;<maxGold>;;;;<qty>` (exact-name search term)."""
    return '"%s";;0;0;0;0;0;0;0;%s;;;;%d' % (name, _gold_str(max_price_copper), qty)


def build_import(terms, list_name="AH Tool"):
    return list_name + "".join("^" + t for t in terms)


def shopping(conn, cfg=None, realm=None):
    ctx = analytics._ctx(conn, cfg, realm)
    markup = float(userstate.get_settings(conn)["shopping_max_markup"])
    rows = conn.execute("SELECT s.spell_id, s.qty, r.profession_id FROM shopping_list s "
                        "JOIN recipes r ON r.spell_id=s.spell_id ORDER BY s.spell_id").fetchall()
    cache = {}
    recipes, agg = [], {}
    tot_cost = tot_sell = tot_profit = 0.0
    for sid, qty, pid in rows:
        if pid not in cache:
            cache[pid] = {r["spell_id"]: r for r in analytics.profession_recipes(conn, cfg, pid, "current", ctx.realm)}
        r = cache[pid].get(sid)
        if r is None:
            continue
        recipes.append({
            "spell_id": sid, "name": r["name"], "created_item_id": r["created_item_id"],
            "created_quality": r["created_quality"],
            "profession_name": analytics.PROFESSIONS.get(pid, ("Profession %d" % pid,))[0],
            "qty": qty, "cost_each": r["cost"], "sell_each": r["sell"], "profit_each": r["profit"],
            "total_cost": r["cost"] * qty if r["cost"] is not None else None,
            "total_profit": r["profit"] * qty if r["profit"] is not None else None,
            "cost_complete": r["cost_complete"],
            "spark": r["spark"], "profit_trend_pct": r["profit_trend_pct"], "confidence": r["confidence"],
        })
        tot_cost += r["cost"] * qty
        if r["sell"] is not None:
            tot_sell += r["sell"] * qty
        if r["profit"] is not None:
            tot_profit += r["profit"] * qty
        for g in r["reagents"]:
            e = agg.setdefault(g["item_id"], {"item_id": g["item_id"], "name": g["name"], "quality": g["quality"],
                                              "need": 0, "unit_price": g["unit_price"], "source": g["source"],
                                              "confidence": g.get("confidence"),
                                              "price_flag": g.get("price_flag")})
            e["need"] += qty * g["count"]
    reagents = []
    for iid, e in sorted(agg.items(), key=lambda kv: (kv[1]["name"] or "~", kv[0])):
        c = ctx.cur.get(iid)
        e["total"] = analytics._round(e["unit_price"] * e["need"], 2) if e["unit_price"] is not None else None
        e["available"] = c[1] if c else None
        e["baseline"] = analytics._round(ctx.base.get(iid), 1)
        e["is_deal"] = analytics._is_deal(ctx, iid, float(analytics._c(cfg, "deal_threshold", 0.25)))
        reagents.append(e)

    # Real cost from the latest AH Tool Logger full scan: buy whole stacks from the cheapest up.
    scan, costs = market.cost_for(conn, cfg or {}, {e["item_id"]: e["need"] for e in reagents if e["source"] == "ah"},
                                  ctx.realm)
    listing_total, listing_short = 0, 0
    for e in reagents:
        lc = costs.get(e["item_id"])
        e["listing"] = lc
        if lc:
            listing_total += lc["total"]
            listing_short += 1 if lc["short"] else 0

    terms, unexportable = [], []
    for e in reagents:
        if e["source"] != "ah":
            continue
        nm = e["name"]
        if not nm or '"' in nm or "^" in nm:
            unexportable.append(dict(e, reason="unnamed item" if not nm else 'name contains " or ^'))
            continue
        c = ctx.cur.get(e["item_id"])
        ref = max((c[0] if c else 0) or 0, ctx.base.get(e["item_id"]) or 0)
        terms.append(auctionator_term(nm, ref * (1 + markup), e["need"]))
    return {
        "recipes": recipes,
        "reagents": reagents,
        "totals": {"cost": analytics._round(tot_cost, 2), "sell": analytics._round(tot_sell, 2),
                   "profit": analytics._round(tot_profit, 2),
                   "listing_cost": listing_total if costs else None, "listing_short_items": listing_short},
        "listing_scan": scan,
        "listing_scan_fresh": bool(costs),
        "auctionator_import": build_import(terms) if terms else "",
        "vendor_reagents": [e for e in reagents if e["source"] == "vendor"],
        "unexportable": unexportable,
    }
