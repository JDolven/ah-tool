"""User state: my professions, known recipes, shopping list, watchlist, settings, alerts (SQLite-backed).

This module only needs a connection (no analytics import at module level) so analytics can use it.
"""
import json
import time

DEFAULT_SETTINGS = {
    "notify_windows": True,
    "notify_vendor_flip_min": 0,
    "notify_my_reagents": False,
    "shopping_max_markup": 0.15,
}

SKILL_MAX = 375


class Invalid(ValueError):
    """Bad user input (server maps this to HTTP 400)."""


# ---------------------------------------------------------------- settings

def get_settings(conn):
    out = dict(DEFAULT_SETTINGS)
    for k, v in conn.execute("SELECT key, value FROM settings"):
        if k in out:
            try:
                out[k] = json.loads(v)
            except (TypeError, ValueError):
                pass
    return out


def put_settings(conn, body):
    if not isinstance(body, dict):
        raise Invalid("body must be a JSON object")
    clean = {}
    for k, v in body.items():
        if k not in DEFAULT_SETTINGS:
            raise Invalid("unknown setting: %s" % k)
        if k in ("notify_windows", "notify_my_reagents"):
            if not isinstance(v, bool):
                raise Invalid("%s must be true/false" % k)
        elif k == "notify_vendor_flip_min":
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                raise Invalid("%s must be a number >= 0" % k)
            v = int(v)
        elif k == "shopping_max_markup":
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or v > 10:
                raise Invalid("%s must be a number between 0 and 10" % k)
            v = float(v)
        clean[k] = v
    with conn:
        for k, v in clean.items():
            conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, json.dumps(v)))
    return get_settings(conn)


# ---------------------------------------------------------------- professions / known

def my_professions(conn):
    """{profession_id: skill}"""
    return {r[0]: r[1] for r in conn.execute("SELECT profession_id, skill FROM my_professions")}


def known_map(conn):
    return {r[0]: r[1] for r in conn.execute("SELECT spell_id, known FROM known_recipes")}


def list_my_professions(conn):
    from .analytics import PROFESSIONS
    return [{"profession_id": p, "name": PROFESSIONS.get(p, ("Profession %d" % p,))[0], "skill": s}
            for p, s in sorted(my_professions(conn).items())]


def put_my_professions(conn, body):
    from .analytics import PROFESSIONS
    if not isinstance(body, list):
        raise Invalid("body must be a JSON array of {profession_id, skill}")
    rows = {}
    for e in body:
        if not isinstance(e, dict):
            raise Invalid("each entry must be an object")
        pid, skill = e.get("profession_id"), e.get("skill", 300)
        if isinstance(pid, bool) or not isinstance(pid, int) or pid not in PROFESSIONS:
            raise Invalid("invalid profession_id: %r" % (pid,))
        if isinstance(skill, bool) or not isinstance(skill, (int, float)) or not 0 <= skill <= SKILL_MAX:
            raise Invalid("skill must be a number 0-%d" % SKILL_MAX)
        rows[pid] = int(skill)
    with conn:
        conn.execute("DELETE FROM my_professions")
        conn.executemany("INSERT INTO my_professions(profession_id, skill) VALUES(?,?)", list(rows.items()))
    return list_my_professions(conn)


def put_known(conn, spell_id, body):
    if not isinstance(body, dict) or "known" not in body:
        raise Invalid("body must be {known: 1|0|null}")
    k = body["known"]
    if k is True:
        k = 1
    elif k is False:
        k = 0
    if isinstance(k, float) or k not in (0, 1, None):
        raise Invalid("known must be 1, 0 or null")
    if not conn.execute("SELECT 1 FROM recipes WHERE spell_id=?", (spell_id,)).fetchone():
        raise KeyError("recipe %d not found" % spell_id)
    with conn:
        if k is None:
            conn.execute("DELETE FROM known_recipes WHERE spell_id=?", (spell_id,))
        else:
            conn.execute("INSERT OR REPLACE INTO known_recipes(spell_id, known) VALUES(?,?)", (spell_id, k))
    return {"spell_id": spell_id, "known": k}


def can_craft(rec, mine, known):
    """rec: recipe dict (profession_id, spell_id, skill.min). mine: {prof: skill}; known: {spell: 0|1}."""
    pid = rec.get("profession_id")
    if pid not in mine:
        return False
    k = known.get(rec["spell_id"])
    if k is not None:
        return k == 1
    return ((rec.get("skill") or {}).get("min") or 0) <= mine[pid]


def annotate(conn, recs, mine=None, known=None):
    """Return shallow copies of cached recipe dicts with can_craft / known added."""
    mine = my_professions(conn) if mine is None else mine
    known = known_map(conn) if known is None else known
    out = []
    for r in recs:
        c = dict(r)
        c["can_craft"] = can_craft(r, mine, known)
        c["known"] = known.get(r["spell_id"])
        out.append(c)
    return out


def my_reagent_items(conn):
    """Item ids that are reagents of any can_craft recipe."""
    mine = my_professions(conn)
    if not mine:
        return set()
    known = known_map(conn)
    ok = set()
    for sid, pid, mn in conn.execute("SELECT spell_id, profession_id, min_skill FROM recipes"):
        if can_craft({"profession_id": pid, "spell_id": sid, "skill": {"min": mn}}, mine, known):
            ok.add(sid)
    return {iid for sid, iid in conn.execute("SELECT spell_id, item_id FROM recipe_reagents") if sid in ok}


# ---------------------------------------------------------------- shopping list

def put_shopping(conn, spell_id, body):
    if not isinstance(body, dict) or "qty" not in body:
        raise Invalid("body must be {qty}")
    q = body["qty"]
    if isinstance(q, bool) or not isinstance(q, (int, float)):
        raise Invalid("qty must be a number")
    q = min(int(q), 9999)
    if not conn.execute("SELECT 1 FROM recipes WHERE spell_id=?", (spell_id,)).fetchone():
        raise KeyError("recipe %d not found" % spell_id)
    with conn:
        if q <= 0:
            conn.execute("DELETE FROM shopping_list WHERE spell_id=?", (spell_id,))
            q = 0
        else:
            conn.execute("INSERT OR REPLACE INTO shopping_list(spell_id, qty) VALUES(?,?)", (spell_id, q))
    return {"spell_id": spell_id, "qty": q}


def clear_shopping(conn):
    with conn:
        conn.execute("DELETE FROM shopping_list")
    return {"cleared": True}


# ---------------------------------------------------------------- watchlist

def get_watch(conn, item_id):
    r = conn.execute("SELECT max_price, enabled, note FROM watchlist WHERE item_id=?", (item_id,)).fetchone()
    if not r:
        return None
    return {"max_price": r[0], "enabled": bool(r[1]), "note": r[2]}


def put_watch(conn, item_id, body):
    if not isinstance(body, dict):
        raise Invalid("body must be a JSON object")
    sets = {}
    if "max_price" in body:
        mp = body["max_price"]
        if mp is not None:
            if isinstance(mp, bool) or not isinstance(mp, (int, float)) or mp < 0 or mp > 2147483647:
                raise Invalid("max_price must be a copper amount >= 0 or null")
            mp = int(mp)
        sets["max_price"] = mp
    if "enabled" in body:
        e = body["enabled"]
        if e is None or e not in (0, 1, True, False):
            raise Invalid("enabled must be true/false")
        sets["enabled"] = 1 if e else 0
    if "note" in body:
        n = body["note"]
        if n is not None and not isinstance(n, str):
            raise Invalid("note must be a string or null")
        sets["note"] = n[:500] if n else n
    with conn:
        if conn.execute("SELECT 1 FROM watchlist WHERE item_id=?", (item_id,)).fetchone():
            if "max_price" in sets or sets.get("enabled") == 1:
                sets["last_alert_price"] = None
                sets["last_alert_ts"] = None
            if sets:
                conn.execute("UPDATE watchlist SET " + ",".join("%s=?" % k for k in sets) + " WHERE item_id=?",
                             list(sets.values()) + [item_id])
        else:
            conn.execute("INSERT INTO watchlist(item_id,max_price,enabled,note,created_at) VALUES(?,?,?,?,?)",
                         (item_id, sets.get("max_price"), sets.get("enabled", 1), sets.get("note"), int(time.time())))
    return get_watch(conn, item_id)


def delete_watch(conn, item_id):
    with conn:
        n = conn.execute("DELETE FROM watchlist WHERE item_id=?", (item_id,)).rowcount
    if not n:
        raise KeyError("item %d is not on the watchlist" % item_id)
    return {"deleted": True}


# ---------------------------------------------------------------- alerts

def unseen_alerts(conn):
    return conn.execute("SELECT COUNT(*) FROM alerts WHERE seen=0").fetchone()[0]


def mark_alerts_seen(conn):
    with conn:
        n = conn.execute("UPDATE alerts SET seen=1 WHERE seen=0").rowcount
    return {"marked": n}
