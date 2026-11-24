"""Profession leveling planner (v4, see CONTRACT.md "Leveling planner").

    plan(conn, cfg, prof_id, start=1, target=300, mode="baseline", recover="sell",
         sources="trainer", known_only=False, cooldowns=False) -> dict
    plan_to_shopping(plan) -> [(spell_id, crafts)]

Model
-----
* Skill-up chance at skill s for a recipe (orange=min_skill, yellow=low_skill, grey=high_skill):
  s < yellow -> 1.0; yellow <= s < grey -> (grey - s) / (grey - yellow); s >= grey -> 0.
  A recipe is usable on point s -> s+1 when min_skill <= s < grey. Recipes without sane colour data
  (low/high 0/null, grey <= yellow or grey <= orange) are ignored.
* Expected net cost of one point with recipe r at skill s:  c_r(s) = net_r / chance_r(s), where
  net_r = reagent cost - recovery. For ranking, a profitable recipe (net_r <= 0) is scored net_r * chance (see
  _point_key) so nearly-grey recipes are not preferred just because more crafts mean more sales. Reported totals
  are always the real expected money (crafts * net). Recovery ("sell") = max(AH sell price * (1 - ah_cut), vendor_sell) * num_created
  when the output's price confidence is not low, else vendor_sell * num_created.
* Make vs buy: a reagent that a (free) recipe of the same profession creates is priced at
  min(buy price, crafted cost). Crafted cost is a fixed point (Bellman-style relaxation, handles conversion
  cycles like essences) recomputed per "epoch" -- the skill ranges between the unlock skills of those
  intermediate recipes, because an intermediate can only be crafted once min_skill <= s.
  Intermediate crafts are NOT counted as skill-ups (conservative).

Algorithm (deterministic)
-------------------------
For a fixed set of available recipes the points are independent: the total expected cost is
sum_s min_r c_r(s), so the per-point greedy is exact. Recipes taught by an item (sources="all") carry a fixed
purchase cost, which turns the problem into uncapacitated facility location. We solve it with a local search on
the exact objective (reach, cost):
  1. start with the free recipes (trainer + already known),
  2. greedy ADD: repeatedly buy the recipe item that extends the reachable range the most, or else lowers
     sum_s min c(s) + fixed costs the most (ties: lower spell_id) until nothing improves,
  3. DROP pass: remove any bought recipe whose removal lowers the objective; repeat 2-3 until stable.
Finally a Viterbi DP picks one recipe per point minimising sum(cost) + a recipe-switch penalty of about one point's
cost (see _choose_path), which removes 1-2 point ping-pong steps between near-equal recipes. Consecutive points with
the same recipe and unit cost are merged into steps. Crafts per step = ceil(sum 1/chance).
Products of earlier steps (bolts, blasting powder, ...) are reused by later steps when that is cheaper than selling
them: the consumer's cost_total is reduced by reuse_saving and the producer's recover_total drops ("kept" units).
"""
import bisect
import json
import math
import threading

from . import analytics

SKILL_MAX = 300
INF = float("inf")
EPS = 1e-9
CONF_RANK = {"high": 0, "medium": 1, "low": 2}
GATHERING = {5, 11, 12}
SWITCH_POINTS = 1.0

# Engineering specialisation recipes (Goblin / Gnomish only) - a character can only have one, so the planner
# never relies on them.
SPECIALISATION = {
    # Goblin
    8895: None, 12715: "Goblin", 12716: "Goblin", 12717: "Goblin", 12718: "Goblin", 12754: "Goblin",
    12755: "Goblin", 12758: "Goblin", 12760: "Goblin", 12908: "Goblin", 13240: "Goblin", 15628: "Goblin",
    23078: "Goblin", 23486: "Goblin",
    # Gnomish
    12759: "Gnomish", 12895: "Gnomish", 12897: "Gnomish", 12899: "Gnomish", 12902: "Gnomish", 12903: "Gnomish",
    12905: "Gnomish", 12906: "Gnomish", 12907: "Gnomish", 15633: "Gnomish", 23096: "Gnomish", 23129: "Gnomish",
    23489: "Gnomish",
}
SPECIALISATION = {k: v for k, v in SPECIALISATION.items() if v}
# Recipes on a cooldown are useless for leveling, and as a make-vs-buy source they would price a reagent far below
# what it really costs (Salt Shaker -> Refined Deeprock Salt made Cured Rugged Hide look hugely profitable).
# Every AtlasLoot Classic recipe with a cooldown in SpellCooldowns (Classic Era 1.15.8 and 1.15.9, wago.tools),
# spell id -> cooldown in hours. The name prefix also catches transmutes the client data does not have (Forever).
COOLDOWN_SPELLS = {
    11479: 24, 11480: 48, 17187: 48,                                    # Transmute: Iron to Gold, Mithril, Arcanite
    17559: 24, 17560: 24, 17561: 24, 17562: 24, 17563: 24, 17564: 24,  # Transmute: elements
    17565: 24, 17566: 24, 25146: 0.17,                                  # ..., Elemental Fire (10 min)
    18560: 96,                                                          # Mooncloth
    19566: 72,                                                          # Salt Shaker (Refined Deeprock Salt)
    21935: 24,                                                          # SnowMaster 9000
    22430: 2,                                                           # Refined Scale of Onyxia
}
COOLDOWN_PREFIXES = ("Transmute",)


def _hours(h):
    return "%g h" % h if h < 24 else "%g day%s" % (h / 24.0, "" if h == 24 else "s")


def cooldown_hours(sid, name=None):
    """Cooldown of a recipe in hours, or None. A transmute the client data lacks is assumed to be 24 h."""
    h = COOLDOWN_SPELLS.get(sid)
    if h:
        return h
    return 24 if (name or "").startswith(COOLDOWN_PREFIXES) else None


# ---------------------------------------------------------------- analytics price functions (v4 contract)
_get_ctx = analytics.get_ctx
_buy_price = analytics.buy_price      # (unit | None, "ah" | "vendor" | "none"), min(AH, vendor)
_sell_price = analytics.sell_price    # unit | None (before the AH cut)
_confidence = analytics.confidence    # "high" | "medium" | "low"


def _cfg(cfg, key, default):
    try:
        v = cfg.get(key, default) if cfg is not None else default
    except AttributeError:
        v = getattr(cfg, key, default)
    return default if v is None else v


# ---------------------------------------------------------------- static data

_teach_lock = threading.Lock()
_teach_cache = {}


def teaches_map(conn):
    """{spell_id: [item_id, ...]} for recipe items (from item_tooltip.data.teaches). Cached per DB content."""
    try:
        path = conn.execute("PRAGMA database_list").fetchone()[2]
        key = (path,) + tuple(conn.execute("SELECT COUNT(*), MAX(id) FROM item_tooltip").fetchone())
    except Exception:
        return {}
    with _teach_lock:
        hit = _teach_cache.get(key)
        if hit is not None:
            return hit
    out = {}
    try:
        rows = conn.execute("SELECT id, json_extract(data, '$.teaches') FROM item_tooltip "
                            "WHERE data LIKE '%\"teaches\"%'").fetchall()
    except Exception:  # sqlite without JSON1
        rows = []
        for iid, d in conn.execute("SELECT id, data FROM item_tooltip WHERE data LIKE '%\"teaches\"%'"):
            try:
                rows.append((iid, json.loads(d).get("teaches")))
            except (ValueError, AttributeError):
                pass
    for iid, sid in rows:
        if isinstance(sid, int) and sid > 0:
            out.setdefault(sid, []).append(iid)
    for v in out.values():
        v.sort()
    with _teach_lock:
        _teach_cache.clear()
        _teach_cache[key] = out
    return out


def chance(s, mn, lo, hi):
    """Skill-up chance at skill s (0 when unusable)."""
    if s < max(mn or 0, 1) or s >= hi:
        return 0.0
    if s < lo:
        return 1.0
    return (hi - s) / (hi - lo)


def colour_ok(mn, lo, hi):
    """Sane skill colours. Also rejects min_skill 1 with yellow >= 100: AtlasLoot uses 1 for unknown learn
    levels (e.g. Elixir of Water Walking 1/190/230, Goblin Radio 1/240/260), which would look orange from skill 1."""
    if not lo or not hi or lo <= 0 or hi <= lo or hi <= max(mn or 0, 1):
        return False
    return not ((mn or 0) <= 1 and lo >= 100)


class _Rec:
    __slots__ = ("sid", "name", "created", "num", "mn", "lo", "hi", "reagents", "teach_items",
                 "known", "free", "fixed", "fixed_item", "colour", "recover", "rec_conf", "rec_src",
                 "ah_unit", "vend_unit")


def _point_key(net, ch):
    """Ranking value of one skill point.

    Crafting costs money (net > 0): the expected net cost net / chance.
    Crafting is profitable (net <= 0): net * chance, i.e. profit weighted by skill-ups per craft. Using
    net / chance here would reward nearly-grey recipes (more crafts = more sales), which is farming, not leveling;
    the profit itself can be made after leveling as well. Continuous at net = 0, so any profitable recipe still beats
    any loss-making one.
    """
    return net / ch if net > 0 else net * ch


def _worst(*cs):
    w = "high"
    for c in cs:
        if c and CONF_RANK.get(c, 1) > CONF_RANK[w]:
            w = c
    return w


def _r2(v):
    return None if v is None else round(v, 2)


def _num(v):
    v = round(v, 2)
    return int(v) if v == int(v) else v


def _bool(v):
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


# ---------------------------------------------------------------- milestones

def _milestones(prof_id, start, target, recs_by_sid):
    if prof_id == 1:
        ms = [(1, "Apprentice First Aid (trainer) - cap 75"),
              (50, "Train Journeyman First Aid (requires level 10) - cap 150"),
              (125, "Expert First Aid: buy the book 'Expert First Aid - Under Wraps' (requires level 20) - cap 225"),
              (225, "Artisan First Aid: quest 'Triage' (Theramore / Hammerfall, requires level 35) - cap 300")]
    elif prof_id == 6:
        ms = [(1, "Apprentice Cooking (trainer) - cap 75"),
              (50, "Train Journeyman Cooking (requires level 10) - cap 150"),
              (125, "Expert Cooking: buy 'Expert Cookbook' (requires level 20) - cap 225"),
              (225, "Artisan Cooking: quest 'Clamlette Surprise' (Dirge, Gadgetzan, requires level 35) - cap 300")]
    else:
        ms = [(1, "Apprentice (trainer) - cap 75"),
              (50, "Train Journeyman (requires level 10) - cap 150"),
              (125, "Train Expert (requires level 20) - cap 225"),
              (200, "Train Artisan (requires level 35) - cap 300")]
        if prof_id == 10:
            for r in sorted(recs_by_sid.values(), key=lambda r: (r.mn, r.sid)):
                if r.created and r.name and r.name.startswith("Runed ") and r.name.endswith(" Rod"):
                    ms.append((max(r.mn, 1), "%s can be made now - higher enchants need it as a tool" % r.name))
    cap_points = {75: "Apprentice cap: you must have trained Journeyman to go past 75",
                  150: "Journeyman cap: Expert needed to go past 150",
                  225: "Expert cap: Artisan needed to go past 225"}
    out = [{"skill": s, "text": t} for s, t in ms if start <= s < target or (s == 1 and start <= 1)]
    out += [{"skill": s, "text": t} for s, t in cap_points.items() if start <= s < target]
    out.sort(key=lambda m: m["skill"])
    return out


# ---------------------------------------------------------------- path choice

def _choose_path(n, ids, tables):
    """Pick one recipe per point minimising sum(cost) + switch penalties (Viterbi DP, deterministic).

    The penalty for changing recipe at point i is SWITCH_POINTS * |best cost at i| + 1 copper, i.e. a switch must
    save about one point's worth of money to be worth an extra step. Returns (choice list, unreachable index|None);
    the path stops at the first point without any option.
    """
    prev = None          # sid -> accumulated cost
    back = []            # per point: {sid: previous sid}
    for i in range(n):
        opts = sorted((sid, tables[sid][i]) for sid in ids if i in tables[sid])
        if not opts:
            return _backtrack(prev, back), i
        best_c = min(c for _, c in opts)
        lam = SWITCH_POINTS * abs(best_c) + 1.0
        cur, bk = {}, {}
        if prev:
            pm_sid, pm_val = min(prev.items(), key=lambda kv: (kv[1], kv[0]))
        for sid, c in opts:
            if not prev:
                cur[sid], bk[sid] = c, None
                continue
            stay = prev.get(sid)
            sw = pm_val + lam
            if stay is not None and stay <= sw + EPS:
                cur[sid], bk[sid] = stay + c, sid
            else:
                cur[sid], bk[sid] = sw + c, pm_sid
        prev = cur
        back.append(bk)
    return _backtrack(prev, back), None


def _backtrack(last, back):
    if not last or not back:
        return []
    sid = min(last.items(), key=lambda kv: (kv[1], kv[0]))[0]
    out = []
    for bk in reversed(back):
        out.append(sid)
        sid = bk[sid]
    out.reverse()
    return out


# ---------------------------------------------------------------- plan

def plan(conn, cfg, prof_id, start=1, target=300, mode="baseline", recover="sell", sources="trainer",
         known_only=False, cooldowns=False):
    """cooldowns=True also uses recipes on a cooldown (Salt Shaker, transmutes, Mooncloth...) as steps and as
    make-vs-buy sources; their steps and sub-crafts carry cooldown_h and a warning says how many days they take."""
    try:
        prof_id = int(prof_id)
        start = int(start if start not in (None, "") else 1)
        target = int(target if target not in (None, "") else SKILL_MAX)
    except (TypeError, ValueError):
        raise ValueError("profession, start and target must be integers")
    mode = mode or "baseline"
    recover = recover or "sell"
    sources = sources or "trainer"
    known_only = _bool(known_only)
    cooldowns = _bool(cooldowns)
    if prof_id not in analytics.PROFESSIONS:
        raise ValueError("unknown profession %r" % prof_id)
    if prof_id in GATHERING:
        raise ValueError("%s has no craftable recipes" % analytics.PROFESSIONS[prof_id][0])
    if mode not in ("baseline", "current"):
        raise ValueError("mode must be 'baseline' or 'current'")
    if recover not in ("sell", "vendor", "none"):
        raise ValueError("recover must be 'sell', 'vendor' or 'none'")
    if sources not in ("trainer", "all"):
        raise ValueError("sources must be 'trainer' or 'all'")
    if not (0 <= start <= SKILL_MAX and 0 <= target <= SKILL_MAX):
        raise ValueError("start and target must be 0-%d" % SKILL_MAX)
    start = max(start, 1)

    cut = float(_cfg(cfg, "ah_cut", 0.05))
    ctx = _get_ctx(conn, cfg)
    items = ctx.items

    def iname(iid):
        it = items.get(iid)
        return it["name"] if it and it.get("name") else None

    def iqual(iid):
        it = items.get(iid)
        return it["quality"] if it else 1

    out = {"profession_id": prof_id, "profession": analytics.PROFESSIONS[prof_id][0],
           "start": start, "target": target, "mode": mode, "recover": recover,
           "sources": sources, "known_only": known_only, "cooldowns": cooldowns,
           "steps": [], "shopping": [], "subcrafts": [], "recipes_to_buy": [], "tools": [], "milestones": [],
           "totals": {"cost": 0, "recover": 0, "net": 0, "crafts": 0, "crafts_expected": 0, "recipes": 0},
           "warnings": [], "unreachable_from": None}
    warnings = out["warnings"]

    # ---- recipes
    rows = conn.execute(
        "SELECT r.spell_id, r.created_item_id, r.num_created, r.min_skill, r.low_skill, r.high_skill, s.name "
        "FROM recipes r LEFT JOIN spells s ON s.id=r.spell_id WHERE r.profession_id=? ORDER BY r.spell_id",
        (prof_id,)).fetchall()
    reag = {}
    for sid, iid, cnt in conn.execute(
            "SELECT rr.spell_id, rr.item_id, rr.count FROM recipe_reagents rr JOIN recipes r "
            "ON r.spell_id=rr.spell_id WHERE r.profession_id=? ORDER BY rr.spell_id, rr.item_id", (prof_id,)):
        if cnt and cnt > 0:
            reag.setdefault(sid, []).append((iid, cnt))
    known = {r[0]: r[1] for r in conn.execute("SELECT spell_id, known FROM known_recipes")}
    teaches = teaches_map(conn)

    buy_cache = {}

    def buy(iid):
        v = buy_cache.get(iid)
        if v is None:
            unit, src = _buy_price(ctx, iid, mode)
            if unit is not None and unit <= 0:
                unit = None
            conf = "high" if src == "vendor" else (_confidence(ctx, iid) if unit is not None else "low")
            v = buy_cache[iid] = (unit, src if unit is not None else "none", conf)
        return v

    recs = {}
    excluded_spec = 0
    excluded_cd = 0
    cd_h = {}
    no_colour_in_range = 0
    unpriced_items = 0
    for sid, created, num, mn, lo, hi, sname in rows:
        r = _Rec()
        r.sid, r.created, r.num = sid, created, (num if num and num > 0 else 1)
        r.mn, r.lo, r.hi = max(mn or 0, 1), lo or 0, hi or 0
        r.name = (iname(created) if created else None) or sname or ("Spell #%d" % sid)
        r.reagents = reag.get(sid, [])
        r.teach_items = teaches.get(sid, [])
        r.known = known.get(sid)
        r.colour = colour_ok(mn, lo, hi)
        r.fixed, r.fixed_item = 0, None
        if not r.reagents:
            continue
        if sid in SPECIALISATION and prof_id == 9:
            excluded_spec += 1
            continue
        h = cooldown_hours(sid, sname)
        if h:
            if not cooldowns:
                if r.mn < target:
                    excluded_cd += 1
                continue
            cd_h[sid] = h
        if r.known == 1:
            r.free = True
        elif known_only and r.known == 0:
            continue
        elif r.teach_items:
            if sources != "all" or known_only:
                r.free = False
                r.fixed = None  # not allowed
            else:
                best = None
                for ti in r.teach_items:
                    u, _src, _c = buy(ti)
                    if u is not None and (best is None or u < best[0]):
                        best = (u, ti)
                if best is None:
                    r.free, r.fixed = False, None
                    if r.colour and r.mn < target and r.hi > start:
                        unpriced_items += 1
                else:
                    r.free, r.fixed, r.fixed_item = False, best[0], best[1]
        else:
            r.free = True
        if not r.colour and r.mn < target and (r.free or r.fixed is not None):
            no_colour_in_range += 1
        recs[sid] = r

    if not recs:
        raise ValueError("no recipes found for %s" % out["profession"])
    out["milestones"] = _milestones(prof_id, start, target, recs)
    if start >= target:
        warnings.append("Start skill %d is already at or above the target %d - nothing to do." % (start, target))
        return out

    # ---- recovery per recipe. AH recovery is capped per created item by market depth:
    # cap = max(1, ceil(avg available over the last 7 days * leveling_sell_depth)); units beyond it are vendored.
    sell_depth = float(_cfg(cfg, "leveling_sell_depth", 0.5))
    caps = {}
    if recover == "sell":
        created_ids = sorted({r.created for r in recs.values() if r.created})
        avg7 = {}
        if ctx.latest_day is not None and created_ids:
            for iid, av in conn.execute(
                    "SELECT item_id, AVG(available) FROM price_daily WHERE realm=? AND day>? AND item_id IN (%s) "
                    "GROUP BY item_id" % ",".join("?" * len(created_ids)),
                    [ctx.realm, ctx.latest_day - 7] + created_ids):
                avg7[iid] = av or 0
        for iid in created_ids:
            av = avg7.get(iid)
            if av is None:
                c = ctx.cur.get(iid)
                av = (c[1] or 0) if c else 0
            caps[iid] = max(1, int(math.ceil(av * sell_depth - 1e-9)))
    for r in recs.values():
        r.recover, r.rec_conf, r.rec_src = 0.0, "high", None
        r.ah_unit, r.vend_unit = None, 0.0
        if not r.created or recover == "none":
            continue
        r.vend_unit = float((items.get(r.created) or {}).get("vendor_sell") or 0)
        if recover == "vendor":
            r.recover, r.rec_src = r.vend_unit * r.num, "vendor" if r.vend_unit else None
            continue
        sp = _sell_price(ctx, r.created, mode)
        conf = _confidence(ctx, r.created) if sp else "low"
        ah = sp * (1 - cut) if sp else None
        if ah is not None and conf != "low" and ah > r.vend_unit:
            r.ah_unit, r.rec_conf, r.rec_src = ah, conf, "ah"
            r.recover = ah * r.num
        elif r.vend_unit:
            r.recover, r.rec_src = r.vend_unit * r.num, "vendor"

    def apply_caps(planned):
        """Set r.recover to the average recovery per craft when `planned` units of the item get made."""
        for r in recs.values():
            if r.ah_unit is None:
                continue
            units = planned.get(r.created, 0.0)
            cap = caps.get(r.created, 1)
            if units <= cap:
                unit = r.ah_unit
            else:
                unit = (cap * r.ah_unit + (units - cap) * r.vend_unit) / units
            r.recover = unit * r.num

    # ---- intermediates (make vs buy): free recipes whose product is a reagent of a usable recipe
    usable = {sid: r for sid, r in recs.items() if r.free or r.fixed is not None}
    needed = set()
    for r in usable.values():
        needed.update(i for i, _ in r.reagents)
    producers = {}
    frontier = set(needed)
    while frontier:
        new = set()
        for r in recs.values():
            if r.free and r.created in frontier and r.sid not in producers.get(r.created, ()):
                producers.setdefault(r.created, []).append(r.sid)
                for i, _ in r.reagents:
                    if i not in needed:
                        needed.add(i)
                        new.add(i)
        frontier = new
    inter_recs = sorted({sid for v in producers.values() for sid in v})
    thresholds = sorted({recs[sid].mn for sid in inter_recs})

    def epoch_of(s):
        return bisect.bisect_right(thresholds, s)

    epoch_prices = {}

    def prices(e):
        """{item: (unit|None, src, craft_spell|None, conf)} with intermediates unlocked up to epoch e."""
        p = epoch_prices.get(e)
        if p is not None:
            return p
        p = {}
        for iid in needed:
            u, src, conf = buy(iid)
            p[iid] = (u, src, None, conf)
        if e > 0:
            lim = thresholds[e - 1]
            active = [recs[sid] for sid in inter_recs if recs[sid].mn <= lim]
            for _ in range(12):
                changed = False
                for r in active:
                    cost, conf, ok = 0.0, "high", True
                    for i, cnt in r.reagents:
                        u = p[i][0]
                        if u is None:
                            ok = False
                            break
                        cost += u * cnt
                        conf = _worst(conf, p[i][3])
                    if not ok:
                        continue
                    unit = cost / r.num
                    cur = p[r.created][0]
                    if cur is None or unit < cur - 1e-6:
                        p[r.created] = (unit, "craft", r.sid, conf)
                        changed = True
                if not changed:
                    break
        epoch_prices[e] = p
        return p

    cost_cache = {}

    def recipe_cost(r, e):
        k = (r.sid, e)
        v = cost_cache.get(k)
        if v is None:
            p = prices(e)
            cost, conf = 0.0, "high"
            for i, cnt in r.reagents:
                u = p[i][0]
                if u is None:
                    v = cost_cache[k] = (None, "low")
                    return v
                cost += u * cnt
                conf = _worst(conf, p[i][3])
            v = cost_cache[k] = (cost, _worst(conf, r.rec_conf))
        return v

    pts = list(range(start, target))
    n = len(pts)
    cands = sorted((r for r in usable.values() if r.colour and r.mn < target and r.hi > start),
                   key=lambda r: r.sid)

    def select():
        # ---- per-point cost tables
        tables = {}  # sid -> {idx: cost_per_point}
        for r in cands:
            t = {}
            for s in range(max(r.mn, start), min(r.hi, target)):
                ch = chance(s, r.mn, r.lo, r.hi)
                if ch <= 0:
                    continue
                cost, _conf = recipe_cost(r, epoch_of(s))
                if cost is None:
                    continue
                t[s - start] = _point_key(cost - r.recover, ch)
            if t:
                tables[r.sid] = t
        free_ids = [r.sid for r in cands if r.free and r.sid in tables]
        buy_ids = [r.sid for r in cands if not r.free and r.sid in tables]

        def best_array(ids):
            a = [INF] * n
            for sid in ids:
                for i, c in tables[sid].items():
                    if c < a[i]:
                        a[i] = c
            return a

        def objective(a, fixed):
            reach = n
            for i, c in enumerate(a):
                if c == INF:
                    reach = i
                    break
            return reach, sum(a[:reach]) + fixed

        # ---- facility-location local search over purchasable recipes
        bought = []
        if buy_ids:
            def fixed_of(ids):
                return sum(recs[s].fixed for s in ids)

            for _round in range(20):
                changed = False
                cur = best_array(free_ids + bought)
                obj = objective(cur, fixed_of(bought))
                while True:  # greedy add
                    best = None
                    gap = obj[0]
                    for sid in buy_ids:
                        if sid in bought:
                            continue
                        t = tables[sid]
                        if gap >= n or gap not in t:
                            save = sum(cur[i] - c for i, c in t.items() if i < gap and c < cur[i])
                            if save <= recs[sid].fixed + EPS:
                                continue
                        a = cur[:]
                        for i, c in t.items():
                            if c < a[i]:
                                a[i] = c
                        o = objective(a, fixed_of(bought) + recs[sid].fixed)
                        if o[0] > obj[0] or (o[0] == obj[0] and o[1] < obj[1] - EPS):
                            key = (-o[0], o[1], sid)
                            if best is None or key < best[0]:
                                best = (key, sid, a, o)
                    if best is None:
                        break
                    bought.append(best[1])
                    cur, obj = best[2], best[3]
                    changed = True
                for sid in sorted(bought):  # drop pass
                    rest = [b for b in bought if b != sid]
                    a = best_array(free_ids + rest)
                    o = objective(a, fixed_of(rest))
                    if o[0] >= obj[0] and o[1] < obj[1] - EPS:
                        bought, cur, obj = rest, a, o
                        changed = True
                if not changed:
                    break
            bought.sort()

        # ---- final per-point choice: DP with a recipe-switch penalty (avoids 1-point ping-pong steps)
        avail_ids = free_ids + bought
        choice, unreachable = _choose_path(n, avail_ids, tables)
        if unreachable is not None:
            unreachable = pts[unreachable]


        return tables, bought, choice, unreachable

    # Selection with the market-depth cap: planned units per item only grow between rounds, so the effective
    # recoveries only fall and the loop converges (deterministically) in a few rounds.
    planned = {}
    for _it in range(12):
        tables, bought, choice, unreachable = select()
        if recover != "sell":
            break
        units = {}
        for i, sid in enumerate(choice):
            r = recs[sid]
            if r.created:
                units[r.created] = units.get(r.created, 0.0) + r.num / chance(pts[i], r.mn, r.lo, r.hi)
        grown = False
        for iid, u in units.items():
            if u > planned.get(iid, 0.0) + 1e-6 and (planned.get(iid, 0.0) > caps.get(iid, 1) or u > caps.get(iid, 1)):
                planned[iid] = u
                grown = True
        if not grown:
            break
        apply_caps(planned)

    # ---- steps
    steps = []
    raw_need = {}           # item -> qty (bought)
    craft_need = {}         # (item, spell) -> [qty, epoch]
    inventory = {}          # item -> [[qty, recover_per_unit, step_index], ...] outputs of earlier steps
    used_bought = set()
    ah_left = {}            # item -> AH units still sellable under the market-depth cap
    capped = {}             # item -> units pushed to vendor by the cap
    worst_low = 0
    i = 0
    while i < len(choice):
        sid = choice[i]
        r = recs[sid]
        e = epoch_of(pts[i])
        cost_each, conf = recipe_cost(r, e)
        j = i
        exp = 0.0
        while j < len(choice) and choice[j] == sid and recipe_cost(r, epoch_of(pts[j]))[0] == cost_each:
            exp += 1.0 / chance(pts[j], r.mn, r.lo, r.hi)
            j += 1
        crafts = int(math.ceil(exp - 1e-9))
        p = prices(e)
        rl = []
        saving = 0.0
        for iid, cnt in r.reagents:
            u, src, csid, _c = p[iid]
            need = cnt * crafts
            d = {"item_id": iid, "name": iname(iid), "quality": iqual(iid), "count": cnt,
                 "need": need, "unit_price": _r2(u), "source": src}
            # reuse products of earlier steps (e.g. bolts / blasting powder made for skill-ups)
            took = 0
            for lot in inventory.get(iid, ()):
                if need - took <= 0:
                    break
                if lot[0] <= 0 or u is None or lot[1] > u:
                    continue
                t = min(lot[0], need - took)
                lot[0] -= t
                took += t
                saving += t * u
                steps[lot[2]]["kept"] += t
                steps[lot[2]]["_lost"] += t * lot[1]
            if took:
                d["from_earlier_steps"] = took
            rest = need - took
            if csid:
                d["craft_spell_id"] = csid
                d["craft_name"] = recs[csid].name
                if rest > 0:
                    cn = craft_need.setdefault((iid, csid), [0.0, e])
                    cn[0] += rest
                    cn[1] = min(cn[1], e)
            elif rest > 0:
                raw_need[iid] = raw_need.get(iid, 0) + rest
            rl.append(d)
        if not r.free:
            used_bought.add(sid)
        if conf == "low":
            worst_low += 1
        produced = crafts * r.num
        ah_units = vendor_units = 0
        if r.ah_unit is not None:
            left = ah_left.setdefault(r.created, caps.get(r.created, 1))
            ah_units = min(left, produced)
            vendor_units = produced - ah_units
            ah_left[r.created] = left - ah_units
            if vendor_units > 0:
                capped[r.created] = capped.get(r.created, 0) + vendor_units
            rec_total = ah_units * r.ah_unit + vendor_units * r.vend_unit
            rec_src = "ah" if not vendor_units else ("mixed" if ah_units else "vendor")
        else:
            rec_total = r.recover * crafts
            rec_src = r.rec_src
            if r.rec_src == "vendor":
                vendor_units = produced
        rec_each = rec_total / crafts if crafts else 0.0
        if r.created:
            inventory.setdefault(r.created, []).append([produced, rec_each / r.num, len(steps)])
        steps.append({
            "from": pts[i], "to": pts[j - 1] + 1, "spell_id": sid, "name": r.name,
            "created_item_id": r.created, "created_quality": iqual(r.created) if r.created else None,
            "num_created": r.num,
            "skill": {"min": r.mn, "low": r.lo, "high": r.hi},
            "chance": {"start": round(chance(pts[i], r.mn, r.lo, r.hi), 4),
                       "end": round(chance(pts[j - 1], r.mn, r.lo, r.hi), 4)},
            "crafts_expected": round(exp, 2), "crafts": crafts,
            "cost_each": cost_each, "recover_each": rec_each, "recover_source": rec_src,
            "net_each": cost_each - rec_each,
            "ah_units": _num(ah_units), "vendor_units": _num(vendor_units),
            "_saving": saving, "_lost": 0.0, "kept": 0,
            "confidence": conf, "recipe_item": None if r.free else r.fixed_item,
            "cooldown_h": cd_h.get(sid),
            "reagents": rl,
        })
        i = j
    for st in steps:
        saving, lost = st.pop("_saving"), st.pop("_lost")
        ct = st["cost_each"] * st["crafts"] - saving
        rt = st["recover_each"] * st["crafts"] - lost
        st["reuse_saving"] = _r2(saving)
        st["kept"] = _num(st["kept"])
        st["cost_total"], st["recover_total"], st["net_total"] = _r2(ct), _r2(rt), _r2(ct - rt)
        for k in ("cost_each", "recover_each", "net_each"):
            st[k] = _r2(st[k])
    out["steps"] = steps

    # ---- sub-crafts (intermediates), expanded level by level with global rounding
    sub = {}
    level = craft_need
    for _depth in range(8):
        if not level:
            break
        nxt = {}
        for (iid, csid), (qty, e) in sorted(level.items()):
            cr = recs[csid]
            crafts = int(math.ceil(qty / cr.num - 1e-9))
            ent = sub.setdefault(csid, {"spell_id": csid, "name": cr.name, "item_id": iid,
                                        "quality": iqual(iid), "num_created": cr.num,
                                        "need": 0, "crafts": 0, "unit_cost": None, "_e": e,
                                        "cooldown_h": cd_h.get(csid)})
            ent["need"] += qty
            ent["crafts"] += crafts
            ent["_e"] = min(ent["_e"], e)
            p = prices(e)
            for ri, cnt in cr.reagents:
                u, src, sub_sid, _c = p[ri]
                if sub_sid:
                    k = (ri, sub_sid)
                    v = nxt.setdefault(k, [0.0, e])
                    v[0] += cnt * crafts
                    v[1] = min(v[1], e)
                else:
                    raw_need[ri] = raw_need.get(ri, 0) + cnt * crafts
        level = nxt
    for ent in sub.values():
        e = ent.pop("_e")
        ent["unit_cost"] = _r2(prices(e)[ent["item_id"]][0])
        ent["need"] = int(math.ceil(ent["need"] - 1e-9))
    out["subcrafts"] = sorted(sub.values(), key=lambda x: (recs[x["spell_id"]].mn, x["spell_id"]))

    # ---- shopping (raw reagents only; intermediates are in subcrafts)
    shopping = []
    for iid, need in raw_need.items():
        u, src, conf = buy(iid)
        c = ctx.cur.get(iid)
        shopping.append({"item_id": iid, "name": iname(iid), "quality": iqual(iid), "need": _num(need),
                         "unit_price": _r2(u), "source": src, "total": _r2(u * need) if u is not None else None,
                         "available": c[1] if c else None, "confidence": conf})
    shopping.sort(key=lambda x: (-(x["total"] or 0), x["item_id"]))
    out["shopping"] = shopping

    # ---- recipes to buy
    rtb = []
    for sid in bought:
        if sid in used_bought:
            r = recs[sid]
            u, src, conf = buy(r.fixed_item)
            rtb.append({"item_id": r.fixed_item, "name": iname(r.fixed_item), "quality": iqual(r.fixed_item),
                        "price": _r2(r.fixed), "source": src, "confidence": conf,
                        "spell_id": sid, "recipe_name": r.name, "skill": r.mn})
    rtb.sort(key=lambda x: (x["skill"], x["spell_id"]))
    out["recipes_to_buy"] = rtb

    # ---- totals
    tc = sum(s["cost_total"] for s in steps)
    tr = sum(s["recover_total"] for s in steps)
    trec = sum(x["price"] for x in rtb)
    out["totals"] = {"cost": _r2(tc + trec), "recover": _r2(tr), "net": _r2(tc + trec - tr),
                     "crafts": sum(s["crafts"] for s in steps),
                     "crafts_expected": round(sum(s["crafts_expected"] for s in steps), 2),
                     "recipes": _r2(trec), "subcrafts": sum(x["crafts"] for x in out["subcrafts"])}

    # ---- warnings
    if unreachable is not None:
        out["unreachable_from"] = unreachable
        msg = ("No usable recipe with known prices from skill %d - the plan stops there (%d of %d points planned)."
               % (unreachable, unreachable - start, target - start))
        idx = unreachable - start
        alt = [recs[s] for s in tables if not recs[s].free and s not in bought and idx in tables[s]]
        locked = [r for r in recs.values() if r.colour and r.teach_items and not r.free and r.fixed is None
                  and r.mn <= unreachable < r.hi]
        if sources == "trainer" and (alt or locked):
            msg += " Recipes from items could fill the gap - try including recipe drops."
        warnings.append(msg)
    depth = []
    for x in shopping:
        if x["source"] == "ah" and x["available"] is not None and x["need"] > x["available"]:
            depth.append(x)
    for x in depth[:8]:
        warnings.append("Need %d x %s but only %d listed on the AH - prices may rise."
                        % (x["need"], x["name"] or "Item #%d" % x["item_id"], x["available"]))
    if len(depth) > 8:
        warnings.append("%d more reagents exceed the listed AH quantity." % (len(depth) - 8))
    if worst_low:
        warnings.append("%d step(s) rely on low-confidence prices." % worst_low)
    if no_colour_in_range:
        warnings.append("%d recipe(s) without skill colour data (custom realm recipes) were ignored."
                        % no_colour_in_range)
    if unpriced_items:
        warnings.append("%d recipe item(s) have no known price and were skipped." % unpriced_items)
    if prof_id == 7:
        warnings.append("Mining is levelled mainly by mining ore nodes; this plan only covers smelting.")
    if excluded_spec:
        warnings.append("Goblin/Gnomish specialisation recipes are not used (%d ignored)." % excluded_spec)
    if excluded_cd:
        warnings.append("%d recipe(s) on a cooldown were not used (turn on \"Include cooldown crafts\" to allow "
                        "them)." % excluded_cd)
    for kind, lst in (("step", steps), ("sub-craft", out["subcrafts"])):
        for x in lst:
            if x.get("cooldown_h") and x["crafts"] > 1:
                days = (x["crafts"] - 1) * x["cooldown_h"] / 24.0
                warnings.append("%s (%s): %d crafts on a %s cooldown take about %s."
                                % (x["name"], kind, x["crafts"], _hours(x["cooldown_h"]),
                                   "%.0f days" % days if days >= 2 else "%.0f hours" % (days * 24)))
    if prof_id == 10 and steps:
        # Runed rods are tools: list the ones whose skill is reached by the plan (cost of one craft each)
        top = max(st["skill"]["min"] for st in steps)
        e_last = epoch_of(steps[-1]["from"])
        for r in sorted(recs.values(), key=lambda r: (r.mn, r.sid)):
            if (r.created and r.name.startswith("Runed ") and r.name.endswith(" Rod") and r.free
                    and r.mn <= max(top, start)):
                cost, _conf = recipe_cost(r, e_last)
                out["tools"].append({"spell_id": r.sid, "name": r.name, "item_id": r.created, "skill": r.mn,
                                     "cost": _r2(cost)})
        warnings.append("Enchants need a Runed Rod as a tool (see tools; not included in the totals) and an item"
                        " to enchant.")
    if capped:
        names = ["%s (AH %d, vendor %s)" % (iname(iid) or "Item #%d" % iid, caps.get(iid, 1), _num(v))
                 for iid, v in sorted(capped.items(), key=lambda kv: (-kv[1], kv[0]))]
        warnings.append("Market depth caps AH sales (%.0f%% of the 7-day average listed); the rest is vendored: %s%s."
                        % (sell_depth * 100, ", ".join(names[:6]),
                           " and %d more" % (len(names) - 6) if len(names) > 6 else ""))
    return out


def plan_to_shopping(plan_dict, include_subcrafts=False):
    """[(spell_id, crafts)] for every step (merged per recipe, in plan order).

    Sub-crafts (intermediates such as bolts) are not included by default: the shopping list prices every reagent
    of a recipe on its own, so adding both the bolts and the shirts would count the bolts twice.
    """
    agg = {}
    order = []
    rows = [(s["spell_id"], s["crafts"]) for s in plan_dict.get("steps", [])]
    if include_subcrafts:
        rows += [(s["spell_id"], s["crafts"]) for s in plan_dict.get("subcrafts", [])]
    for sid, n in rows:
        if not n:
            continue
        if sid not in agg:
            order.append(sid)
            agg[sid] = 0
        agg[sid] += int(n)
    return [(sid, agg[sid]) for sid in order]
