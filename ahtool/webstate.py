"""Per-browser user state for the web version.

In the web version nothing personal is stored on the server. My professions, known recipes, the shopping list, the
watchlist, settings and alert history live in each visitor's browser (localStorage) and travel with every API call
(POST /api/x, see server.py). For that one request they are loaded into TEMP tables with the same names as the real
ones. SQLite looks in the temp schema first, so all existing analytics/leveling/alert code runs unchanged against
the visitor's own state, and nothing is ever written to the shared database. After a write the temp tables are read
back and returned to the browser, which stores them.

The tables that hold your own trades (postings, my_mail, my_auctions) are shadowed by empty temp tables too, so even
a database that somehow contained them would serve nothing from them.
"""
import json

from . import userstate

LIMITS = {"professions": 20, "known": 5000, "shopping": 500, "watch": 500, "alerts": 200, "alertstate": 2000}
INT_MAX = 2 ** 31 - 1

TEMP_SCHEMA = """
CREATE TEMP TABLE my_professions(profession_id INTEGER PRIMARY KEY, skill INTEGER NOT NULL DEFAULT 300);
CREATE TEMP TABLE known_recipes(spell_id INTEGER PRIMARY KEY, known INTEGER NOT NULL);
CREATE TEMP TABLE shopping_list(spell_id INTEGER PRIMARY KEY, qty INTEGER NOT NULL);
CREATE TEMP TABLE watchlist(item_id INTEGER PRIMARY KEY, max_price INTEGER, enabled INTEGER NOT NULL DEFAULT 1,
    note TEXT, created_at INTEGER, last_alert_price INTEGER, last_alert_ts INTEGER);
CREATE TEMP TABLE alerts(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, item_id INTEGER, kind TEXT,
    price INTEGER, threshold INTEGER, message TEXT, seen INTEGER NOT NULL DEFAULT 0);
CREATE TEMP TABLE settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TEMP TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TEMP TABLE postings(item_id INTEGER, ts INTEGER, price INTEGER, quantity INTEGER);
CREATE TEMP TABLE my_mail(key TEXT PRIMARY KEY, ts INTEGER, kind TEXT, item_name TEXT, item_id INTEGER,
    count INTEGER, price INTEGER, buyout INTEGER, deposit INTEGER, fee INTEGER, player TEXT, char TEXT, realm TEXT);
CREATE TEMP TABLE my_auctions(char_key TEXT, ts INTEGER, char TEXT, realm TEXT, item_id INTEGER,
    suffix INTEGER, count INTEGER, buyout INTEGER, min_bid INTEGER, bid INTEGER, time_left INTEGER,
    sale_status INTEGER);
"""
ALERTSTATE = "alertstate:"


def _int(v, lo=-INT_MAX, hi=INT_MAX):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = int(v)
    return v if lo <= v <= hi else None


def _id_map(d, limit):
    """{"123": x} from JSON -> [(123, x)], dropping anything that is not a positive int key."""
    out = []
    if isinstance(d, dict):
        for k, v in list(d.items())[:limit]:
            try:
                ik = int(k)
            except (TypeError, ValueError):
                continue
            if 0 < ik <= INT_MAX:
                out.append((ik, v))
    return out


def install(conn, state):
    """Create the temp tables for this connection and fill them from the browser's state (invalid parts dropped)."""
    conn.executescript(TEMP_SCHEMA)
    conn.execute("INSERT INTO temp.meta SELECT key, value FROM main.meta WHERE key NOT LIKE ?", (ALERTSTATE + "%",))
    s = state if isinstance(state, dict) else {}
    rows = []
    for pid, skill in _id_map(s.get("professions"), LIMITS["professions"]):
        sk = _int(skill, 0, userstate.SKILL_MAX)
        if sk is not None:
            rows.append((pid, sk))
    conn.executemany("INSERT INTO temp.my_professions VALUES(?,?)", rows)
    conn.executemany("INSERT INTO temp.known_recipes VALUES(?,?)",
                     [(sid, k) for sid, k in _id_map(s.get("known"), LIMITS["known"]) if k in (0, 1)
                      and not isinstance(k, bool)])
    conn.executemany("INSERT INTO temp.shopping_list VALUES(?,?)",
                     [(sid, q) for sid, q in ((sid, _int(q, 1, 9999)) for sid, q in
                                              _id_map(s.get("shopping"), LIMITS["shopping"])) if q])
    rows = []
    for iid, w in _id_map(s.get("watch"), LIMITS["watch"]):
        if not isinstance(w, dict):
            continue
        note = w.get("note")
        rows.append((iid, _int(w.get("max_price"), 0), 0 if w.get("enabled") in (0, False) else 1,
                     note[:500] if isinstance(note, str) else None, _int(w.get("created_at"), 0),
                     _int(w.get("last_alert_price"), 0), _int(w.get("last_alert_ts"), 0)))
    conn.executemany("INSERT INTO temp.watchlist VALUES(?,?,?,?,?,?,?)", rows)
    st = s.get("settings")
    if isinstance(st, dict):
        clean = {}
        for k, v in st.items():
            try:
                clean.update(_valid_setting(conn, k, v))
            except userstate.Invalid:
                pass
        conn.executemany("INSERT INTO temp.settings VALUES(?,?)", [(k, json.dumps(v)) for k, v in clean.items()])
    rows = []
    for a in (s.get("alerts") if isinstance(s.get("alerts"), list) else [])[-LIMITS["alerts"]:]:
        if not isinstance(a, dict) or _int(a.get("id"), 1) is None:
            continue
        msg = a.get("message")
        kind = a.get("kind")
        rows.append((_int(a["id"], 1), _int(a.get("ts"), 0), _int(a.get("item_id"), 0),
                     kind[:20] if isinstance(kind, str) else None, _int(a.get("price"), 0),
                     _int(a.get("threshold"), 0), msg[:300] if isinstance(msg, str) else None,
                     1 if a.get("seen") else 0))
    conn.executemany("INSERT OR IGNORE INTO temp.alerts VALUES(?,?,?,?,?,?,?,?)", rows)
    ast = s.get("alertstate")
    if isinstance(ast, dict):
        rows = []
        for k, v in list(ast.items())[:LIMITS["alertstate"]]:
            if isinstance(k, str) and isinstance(v, dict) and len(k) < 80:
                p, t = _int(v.get("price"), 0), _int(v.get("ts"), 0)
                if p is not None and t is not None:
                    rows.append((ALERTSTATE + k, json.dumps({"price": p, "ts": t})))
        conn.executemany("INSERT OR REPLACE INTO temp.meta VALUES(?,?)", rows)
    conn.commit()


def _valid_setting(conn, k, v):
    """Reuse userstate's validation without touching any table."""
    if k not in userstate.DEFAULT_SETTINGS:
        raise userstate.Invalid(k)
    if k in ("notify_windows", "notify_my_reagents"):
        if not isinstance(v, bool):
            raise userstate.Invalid(k)
        return {k: v}
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        raise userstate.Invalid(k)
    if k == "shopping_max_markup":
        if v > 10:
            raise userstate.Invalid(k)
        return {k: float(v)}
    return {k: int(v)}


def export(conn):
    """Read the temp tables back into the JSON shape the browser stores."""
    out = {"v": 1}
    out["professions"] = {str(p): s for p, s in conn.execute("SELECT profession_id, skill FROM temp.my_professions")}
    out["known"] = {str(s): k for s, k in conn.execute("SELECT spell_id, known FROM temp.known_recipes")}
    out["shopping"] = {str(s): q for s, q in conn.execute("SELECT spell_id, qty FROM temp.shopping_list")}
    out["watch"] = {}
    for r in conn.execute("SELECT item_id, max_price, enabled, note, created_at, last_alert_price, last_alert_ts "
                          "FROM temp.watchlist"):
        w = {"max_price": r[1], "enabled": r[2], "note": r[3], "created_at": r[4],
             "last_alert_price": r[5], "last_alert_ts": r[6]}
        out["watch"][str(r[0])] = {k: v for k, v in w.items() if v is not None}
    out["settings"] = {}
    for k, v in conn.execute("SELECT key, value FROM temp.settings"):
        try:
            out["settings"][k] = json.loads(v)
        except ValueError:
            pass
    out["alerts"] = [{"id": r[0], "ts": r[1], "item_id": r[2], "kind": r[3], "price": r[4], "threshold": r[5],
                      "message": r[6], "seen": r[7]}
                     for r in conn.execute("SELECT id, ts, item_id, kind, price, threshold, message, seen FROM "
                                           "(SELECT * FROM temp.alerts ORDER BY id DESC LIMIT ?) ORDER BY id",
                                           (LIMITS["alerts"],))]
    out["alertstate"] = {}
    for k, v in conn.execute("SELECT key, value FROM temp.meta WHERE key LIKE ?", (ALERTSTATE + "%",)):
        try:
            out["alertstate"][k[len(ALERTSTATE):]] = json.loads(v)
        except ValueError:
            pass
    return out


def data_marker(conn):
    """Changes whenever new prices arrive; alerts are evaluated once per marker per browser."""
    r = conn.execute("SELECT MAX(id) FROM main.ingests").fetchone()
    return str(r[0]) if r and r[0] is not None else None
