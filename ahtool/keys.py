"""Sync keys for the web version: one per person who pushes data from their own AH Tool.

Run inside the web container (it reads AHTOOL_DB):

    docker exec ahtool ahkey create "Ola"           # prints the key once, plus the lines for config.json
    docker exec ahtool ahkey create "Me" --owner    # owner: also sends analysis settings and item/recipe data
    docker exec ahtool ahkey list
    docker exec ahtool ahkey revoke "Ola"           # takes effect on the next request

Only a SHA-256 of each key is stored, so a key cannot be shown again - create a new one if it is lost. Keys are
256-bit random, so a fast hash is enough (there is nothing to brute-force).

Contributors push prices and scans, merged with everyone else's. Their own characters are masked on their PC like
yours, and their own trades never leave it. Analysis settings and the static item/recipe data come only from owners
(and from AHTOOL_SYNC_TOKEN, which is always owner).
"""
import argparse
import hashlib
import hmac
import os
import secrets
import sys
import time

PREFIX = "aht_"


def _hash(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def authenticate(conn, cfg, given):
    """-> source dict {"name", "owner", "key_id"} or None."""
    if not given:
        return None
    env = (cfg or {}).get("sync_token") or ""
    if env and hmac.compare_digest(given.encode(), env.encode()):
        return {"name": "owner (AHTOOL_SYNC_TOKEN)", "owner": True, "key_id": None}
    if not given.startswith(PREFIX) or len(given) > 200:
        return None
    from . import sync
    sync.ensure_schema(conn)
    r = conn.execute("SELECT id, name, role FROM main.sync_keys WHERE hash=? AND revoked_at IS NULL",
                     (_hash(given),)).fetchone()
    if not r:
        return None
    return {"name": r[1], "owner": r[2] == "owner", "key_id": r[0]}


def create(conn, name, owner=False):
    from . import sync
    sync.ensure_schema(conn)
    name = (name or "").strip()
    if not name or len(name) > 60:
        raise ValueError("give a name of 1-60 characters (who the key is for)")
    if conn.execute("SELECT 1 FROM sync_keys WHERE name=? AND revoked_at IS NULL", (name,)).fetchone():
        raise ValueError("an active key named %r already exists - revoke it first or pick another name" % name)
    key = PREFIX + secrets.token_urlsafe(32)
    with conn:
        conn.execute("INSERT INTO sync_keys(name, hash, role, created_at) VALUES(?,?,?,?)",
                     (name, _hash(key), "owner" if owner else "contributor", int(time.time())))
    return key


def revoke(conn, ident):
    from . import sync
    sync.ensure_schema(conn)
    q = "UPDATE sync_keys SET revoked_at=? WHERE revoked_at IS NULL AND "
    with conn:
        if str(ident).isdigit():
            n = conn.execute(q + "id=?", (int(time.time()), int(ident))).rowcount
        else:
            n = conn.execute(q + "name=?", (int(time.time()), ident)).rowcount
    return n


def rows(conn):
    from . import sync
    sync.ensure_schema(conn)
    return conn.execute("SELECT id, name, role, created_at, last_push_ts, pushes, revoked_at FROM sync_keys "
                        "ORDER BY revoked_at IS NOT NULL, id").fetchall()


def _when(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def main(argv=None):
    from . import db
    ap = argparse.ArgumentParser(prog="ahkey", description="Manage sync keys for the AH Tool web version.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create", help="create a key for a person (shown once)")
    c.add_argument("name")
    c.add_argument("--owner", action="store_true", help="may also send analysis settings and item/recipe data")
    sub.add_parser("list", help="list keys")
    r = sub.add_parser("revoke", help="revoke a key by name or id")
    r.add_argument("ident")
    a = ap.parse_args(argv)

    conn = db.connect(os.environ.get("AHTOOL_DB", "/data/ah.db"))
    try:
        if a.cmd == "create":
            try:
                key = create(conn, a.name, a.owner)
            except ValueError as e:
                print(e, file=sys.stderr)
                return 2
            url = os.environ.get("AHTOOL_PUBLIC_URL", "https://<this site>")
            print("Key for %s (%s). It is shown only now - it cannot be recovered:\n\n  %s\n"
                  % (a.name, "owner" if a.owner else "contributor", key))
            print("Give %s these lines for config.json in their AH Tool folder, then restart start.bat:\n" % a.name)
            print('  "sync_url": "%s",\n  "sync_token": "%s",\n  "sync_hide_sellers": []\n' % (url, key))
            print("sync_hide_sellers: their alts that never scanned, posted or got AH mail on that PC, so those are\n"
                  "masked as sellers too. Their own trades never leave their PC.")
            return 0
        if a.cmd == "list":
            rs = rows(conn)
            if not rs:
                print("No keys yet. Create one with: ahkey create <name>")
                return 0
            print("%-4s %-24s %-12s %-16s %-16s %6s  %s" % ("id", "name", "role", "created", "last push", "pushes",
                                                         "status"))
            for i, n, role, cr, lp, pu, rv in rs:
                print("%-4d %-24s %-12s %-16s %-16s %6d  %s" % (i, n[:24], role, _when(cr), _when(lp), pu,
                                                             "revoked %s" % _when(rv) if rv else "active"))
            return 0
        if a.cmd == "revoke":
            n = revoke(conn, a.ident)
            print("Revoked %d key(s)." % n if n else "No active key with that name or id.")
            return 0 if n else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
