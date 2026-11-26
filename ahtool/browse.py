"""Browse items by Auction House category (#/browse): class -> subclass -> slot, like the in-game Auction House.

    browse(conn, cfg, realm=None, cls=None, sub=None, slot=None, q="", min_level=None, max_level=None,
           quality=None, listed=True, limit=500) -> {"categories": [...], "items": [...], "total": n}

Every item with an AH price for the realm is placed by its item class and subclass (items table; for custom Forever
items the tooltip data or what AH Tool Logger read from the game, item_class), and armor and weapons also by
slot. Categories carry counts for the current filters except the category itself, so the tree shows where items are.
"""
import json

from . import analytics

# name, subclasses (id, name) in the in-game order. Subclasses without items are hidden.
CLASSES = [
    (2, "Weapon", [(0, "One-Handed Axes"), (1, "Two-Handed Axes"), (2, "Bows"), (3, "Guns"),
                   (4, "One-Handed Maces"), (5, "Two-Handed Maces"), (6, "Polearms"), (7, "One-Handed Swords"),
                   (8, "Two-Handed Swords"), (10, "Staves"), (13, "Fist Weapons"), (15, "Daggers"),
                   (16, "Thrown"), (18, "Crossbows"), (19, "Wands"), (20, "Fishing Poles"), (14, "Miscellaneous")]),
    (4, "Armor", [(1, "Cloth"), (2, "Leather"), (3, "Mail"), (4, "Plate"), (6, "Shields"), (7, "Librams"),
                  (8, "Idols"), (9, "Totems"), (0, "Miscellaneous")]),
    (1, "Container", [(0, "Bag"), (1, "Soul Bag"), (2, "Herb Bag"), (3, "Enchanting Bag"), (4, "Engineering Bag")]),
    (0, "Consumable", [(1, "Potion"), (2, "Elixir"), (3, "Flask"), (4, "Scroll"), (5, "Food & Drink"),
                       (6, "Item Enhancement"), (7, "Bandage"), (0, "Consumable"), (8, "Other")]),
    (7, "Trade Goods", [(5, "Cloth"), (6, "Leather"), (7, "Metal & Stone"), (8, "Meat"), (9, "Herb"),
                        (10, "Elemental"), (12, "Enchanting"), (1, "Parts"), (2, "Explosives"), (3, "Devices"),
                        (4, "Jewelcrafting"), (0, "Trade Goods"), (11, "Other")]),
    (6, "Projectile", [(2, "Arrow"), (3, "Bullet")]),
    (11, "Quiver", [(2, "Quiver"), (3, "Ammo Pouch")]),
    (9, "Recipe", [(0, "Book"), (1, "Leatherworking"), (2, "Tailoring"), (3, "Engineering"), (4, "Blacksmithing"),
                   (5, "Cooking"), (6, "Alchemy"), (7, "First Aid"), (8, "Enchanting"), (9, "Fishing")]),
    (5, "Reagent", []),
    (15, "Miscellaneous", [(0, "Junk"), (1, "Reagent"), (2, "Companion Pets"), (3, "Holiday"), (5, "Mount"),
                           (4, "Other")]),
    (12, "Quest", []),
    (13, "Key", []),
    (-1, "Unknown", []),
]
CLASS_ORDER = {c: i for i, (c, _n, _s) in enumerate(CLASSES)}

# Armor slots in the in-game order (the Auction House's third level); weapons list theirs too.
SLOT_ORDER = ["Head", "Neck", "Shoulder", "Back", "Chest", "Shirt", "Tabard", "Wrist", "Hands", "Waist", "Legs",
              "Feet", "Finger", "Trinket", "Held In Off-hand", "Off Hand", "One-Hand", "Main Hand", "Two-Hand",
              "Ranged", "Thrown", "Relic"]
SLOTTED = {2, 4}

# GetItemInfo's equip location (AH Tool Logger) -> the tooltip's slot names
EQUIP_LOC = {
    "INVTYPE_HEAD": "Head", "INVTYPE_NECK": "Neck", "INVTYPE_SHOULDER": "Shoulder", "INVTYPE_BODY": "Shirt",
    "INVTYPE_CHEST": "Chest", "INVTYPE_ROBE": "Chest", "INVTYPE_WAIST": "Waist", "INVTYPE_LEGS": "Legs",
    "INVTYPE_FEET": "Feet", "INVTYPE_WRIST": "Wrist", "INVTYPE_HAND": "Hands", "INVTYPE_FINGER": "Finger",
    "INVTYPE_TRINKET": "Trinket", "INVTYPE_CLOAK": "Back", "INVTYPE_WEAPON": "One-Hand", "INVTYPE_SHIELD": "Off Hand",
    "INVTYPE_2HWEAPON": "Two-Hand", "INVTYPE_WEAPONMAINHAND": "Main Hand", "INVTYPE_WEAPONOFFHAND": "Off Hand",
    "INVTYPE_HOLDABLE": "Held In Off-hand", "INVTYPE_RANGED": "Ranged", "INVTYPE_RANGEDRIGHT": "Ranged",
    "INVTYPE_THROWN": "Thrown", "INVTYPE_RELIC": "Relic", "INVTYPE_TABARD": "Tabard",
}

MAX_LIMIT = 2000


def _meta(conn, ctx):
    """{item_id: (class, subclass, slot, required_level)} for every priced item; cached on the analytics context."""
    m = getattr(ctx, "browse_meta", None)
    if m is not None:
        return m
    ids = list(ctx.cur)
    tips = {}
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        for iid, data in conn.execute("SELECT id, data FROM item_tooltip WHERE id IN (%s)" % ",".join("?" * len(chunk)),
                                      chunk):
            try:
                tips[iid] = json.loads(data)
            except (TypeError, ValueError):
                pass
    game = {}
    try:
        for iid, c, s, loc in conn.execute("SELECT id, class_id, subclass_id, equip_loc FROM item_class"):
            game[iid] = (c, s, loc)
    except Exception:  # noqa - no logger data yet
        pass
    m = {}
    for iid in ids:
        it = ctx.items.get(iid) or {}
        t = tips.get(iid) or {}
        g = game.get(iid)
        cls = it.get("class_id")
        sub = it.get("subclass_id")
        if cls is None:
            cls, sub = t.get("class_id"), t.get("subclass_id")
        if cls is None and g:
            cls, sub = g[0], g[1]
        slot = t.get("slot") or (EQUIP_LOC.get(g[2]) if g else None)
        m[iid] = (cls if cls is not None else -1, sub, slot, t.get("required_level"))
    ctx.browse_meta = m
    return m


def _num(v):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def browse(conn, cfg=None, realm=None, cls=None, sub=None, slot=None, q="", min_level=None, max_level=None,
           quality=None, listed=True, limit=500):
    ctx = analytics.get_ctx(conn, cfg, realm)
    meta = _meta(conn, ctx)
    cls, sub = _num(cls), _num(sub)
    min_level, max_level, quality = _num(min_level), _num(max_level), _num(quality)
    limit = max(1, min(_num(limit) or 500, MAX_LIMIT))
    q = (q or "").strip().lower()
    qid = _num(q) if q.isdigit() else None

    def keep(iid, m):
        """Filters other than the category."""
        if listed and analytics._is_stale(ctx, iid):
            return False
        it = ctx.items.get(iid) or {}
        if quality is not None and (it.get("quality") if it.get("quality") is not None else 1) < quality:
            return False
        lvl = m[3]
        if min_level is not None and (lvl or 0) < min_level:
            return False
        if max_level is not None and (lvl or 0) > max_level:
            return False
        if q and iid != qid and q not in (analytics._name(ctx, iid) or "").lower():
            return False
        return True

    counts, sub_counts, slot_counts = {}, {}, {}
    rows = []
    for iid, m in meta.items():
        if not keep(iid, m):
            continue
        c, s, sl = m[0], m[1], m[2]
        counts[c] = counts.get(c, 0) + 1
        sub_counts[(c, s)] = sub_counts.get((c, s), 0) + 1
        if c in SLOTTED and sl:
            slot_counts[(c, s, sl)] = slot_counts.get((c, s, sl), 0) + 1
        if cls is not None and c != cls:
            continue
        if sub is not None and s != sub:
            continue
        if slot and sl != slot:
            continue
        rows.append(iid)

    cats = []
    for c, name, subs in CLASSES:
        if not counts.get(c):
            continue
        known = {s for s, _n in subs}
        sub_list = []
        if not subs:  # the game shows no subclasses for these (Reagent, Quest, Key)
            cats.append({"id": c, "name": name, "count": counts[c], "subclasses": []})
            continue
        for s, sname in subs + sorted((s, "Other (%s)" % s) for (cc, s) in sub_counts
                                      if cc == c and s not in known and s is not None):
            n = sub_counts.get((c, s), 0)
            if not n:
                continue
            slots = sorted(((sl, k) for (cc, ss, sl), k in slot_counts.items() if cc == c and ss == s),
                           key=lambda x: (SLOT_ORDER.index(x[0]) if x[0] in SLOT_ORDER else 99, x[0]))
            sub_list.append({"id": s, "name": sname, "count": n,
                             "slots": [{"name": sl, "count": k} for sl, k in slots]})
        cats.append({"id": c, "name": name, "count": counts[c], "subclasses": sub_list})

    out = []
    for iid in rows:
        c = ctx.cur.get(iid) or (None, None, None)
        it = ctx.items.get(iid) or {}
        base = ctx.base.get(iid)
        m = meta[iid]
        out.append({"item_id": iid, "name": analytics._name(ctx, iid), "quality": analytics._quality(ctx, iid),
                    "item_level": it.get("item_level"), "required_level": m[3], "class_id": m[0],
                    "subclass_id": m[1], "slot": m[2], "price": c[0], "available": c[1],
                    "last_date": analytics.day_to_iso(c[2]), "stale": analytics._is_stale(ctx, iid),
                    "baseline": analytics._round(base, 1), "discount": analytics._round(analytics._discount(ctx, iid)),
                    "vendor_sell": it.get("vendor_sell") or None, "confidence": analytics.confidence(ctx, iid)})
    out.sort(key=lambda r: (-(r["available"] or 0), r["item_id"]))
    return {"categories": cats, "items": out[:limit], "total": len(out), "limit": limit}
