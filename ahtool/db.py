import os
import sqlite3

from . import config, market

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY, name TEXT, quality INTEGER, item_level INTEGER,
    class_id INTEGER, subclass_id INTEGER, icon INTEGER, vendor_sell INTEGER, vendor_buy INTEGER, stack INTEGER);
CREATE TABLE IF NOT EXISTS spells(id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS recipes(spell_id INTEGER PRIMARY KEY, profession_id INTEGER, created_item_id INTEGER,
    num_created REAL, min_skill INTEGER, low_skill INTEGER, high_skill INTEGER);
CREATE TABLE IF NOT EXISTS recipe_reagents(spell_id INTEGER, item_id INTEGER, count INTEGER,
    PRIMARY KEY(spell_id, item_id));
CREATE INDEX IF NOT EXISTS idx_reagent_item ON recipe_reagents(item_id);
CREATE TABLE IF NOT EXISTS vendor_items(item_id INTEGER PRIMARY KEY, price INTEGER, source TEXT);
CREATE TABLE IF NOT EXISTS price_daily(realm TEXT, item_id INTEGER, day INTEGER, low INTEGER, high INTEGER,
    available INTEGER, PRIMARY KEY(realm, item_id, day));
CREATE TABLE IF NOT EXISTS price_current(realm TEXT, item_id INTEGER, price INTEGER, available INTEGER,
    last_day INTEGER, updated_at INTEGER, PRIMARY KEY(realm, item_id));
CREATE TABLE IF NOT EXISTS snapshots(realm TEXT, item_id INTEGER, ts INTEGER, price INTEGER, available INTEGER);
CREATE INDEX IF NOT EXISTS idx_snap ON snapshots(realm, item_id, ts);
CREATE TABLE IF NOT EXISTS ingests(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, file_mtime INTEGER,
    realm TEXT, items INTEGER, changed INTEGER);
CREATE TABLE IF NOT EXISTS postings(item_id INTEGER, ts INTEGER, price INTEGER, quantity INTEGER,
    PRIMARY KEY(item_id, ts, price, quantity));
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS my_professions(profession_id INTEGER PRIMARY KEY, skill INTEGER NOT NULL DEFAULT 300);
CREATE TABLE IF NOT EXISTS known_recipes(spell_id INTEGER PRIMARY KEY, known INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS shopping_list(spell_id INTEGER PRIMARY KEY, qty INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS watchlist(item_id INTEGER PRIMARY KEY, max_price INTEGER,
    enabled INTEGER NOT NULL DEFAULT 1, note TEXT, created_at INTEGER,
    last_alert_price INTEGER, last_alert_ts INTEGER);
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, item_id INTEGER, kind TEXT,
    price INTEGER, threshold INTEGER, message TEXT, seen INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS item_tooltip(id INTEGER PRIMARY KEY, icon TEXT, data TEXT);
"""


def connect(path=None):
    if path is None:
        path = os.path.join(config.ROOT, "data", "ah.db")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    conn.executescript(market.SCHEMA)
    conn.commit()
    return conn
