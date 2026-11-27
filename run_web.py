"""AH Tool web version: serves the pushed market data to everyone, keeps no personal data.

No config.json, no WoW folder, no watcher: the data arrives from the local app (ahtool/sync.py). Settings come from
the environment:
  AHTOOL_DB          database path (default /data/ah.db)
  AHTOOL_SYNC_TOKEN  the owner's push token (optional; more people get their own keys with `ahkey`)
  AHTOOL_PUBLIC_URL  the site's address (https://ah.example.net): printed by `ahkey create` for config.json, and
                     the browser Origin accepted for the site's own POSTs behind a reverse proxy
  AHTOOL_TRUST_PROXY 1 (or the number of proxies) when behind a reverse proxy that appends X-Forwarded-For: the
                     per-address connection cap then uses that address. Never set it when clients connect directly.
  AHTOOL_MAX_PER_IP  concurrent connections/requests per client address (default 16)
  AHTOOL_MAX_SYNC_MB largest unpacked sync batch accepted (default 24)
  AHTOOL_HOST/PORT   listen address (default 0.0.0.0:8642)
  AHTOOL_BNET_CLIENT_ID / AHTOOL_BNET_CLIENT_SECRET   optional Battle.net API client (develop.battle.net): hourly
                     snapshots of the auction houses and Blizzard's item vendor prices (ahtool/bnet.py). Off without.
  AHTOOL_BNET_REGION (eu), AHTOOL_BNET_RULESET (1 = PvP), AHTOOL_BNET_HOUSES ("Alliance=110,Horde=113,Neutral=116"),
  AHTOOL_BNET_REALM  (realm key of the snapshots; default the ruleset's name)
  AHTOOL_REALM_ALIASES  "Old key=Canonical key;..." - realm keys that are the same auction house (Auctionator's old
                     key, the game's realm name in logger prices). Pushed prices are stored under the canonical key,
                     and what is already stored under an alias is merged into it at start (sync.merge_realm_aliases)
  AHTOOL_DOWNLOADS   folder with the downloads built by `python -m ahtool.downloads build` (default: downloads/
                     next to this file; the Docker image builds them). Served at /download/, shown on #/download
  AHTOOL_ANALYTICS_SCRIPT / AHTOOL_ANALYTICS_ID   optional Umami tracker (script URL + website id); only counted
                     on the AHTOOL_PUBLIC_URL host
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)


def main():
    from ahtool import config, db, server, sync

    db_path = os.environ.get("AHTOOL_DB", "/data/ah.db")
    cfg = dict(config.DEFAULTS, root=ROOT, db_path=db_path, web=True,
               sync_token=os.environ.get("AHTOOL_SYNC_TOKEN", "").strip(),
               public_url=os.environ.get("AHTOOL_PUBLIC_URL", "").strip(),
               trust_proxy=os.environ.get("AHTOOL_TRUST_PROXY", "").strip(),
               analytics_script=os.environ.get("AHTOOL_ANALYTICS_SCRIPT", "").strip(),
               analytics_id=os.environ.get("AHTOOL_ANALYTICS_ID", "").strip(),
               downloads_dir=os.environ.get("AHTOOL_DOWNLOADS", "").strip() or os.path.join(ROOT, "downloads"))
    for k in ("client_id", "client_secret", "region", "ruleset", "houses", "realm"):
        v = os.environ.get("AHTOOL_BNET_" + k.upper(), "").strip()
        if v:
            cfg["bnet_" + k] = v
    if cfg["public_url"] and server.parse_origin(cfg["public_url"]) is None:
        print("AHTOOL_PUBLIC_URL must look like https://ah.example.net (got %r)." % cfg["public_url"])
        return 2
    if cfg["analytics_script"] and not cfg["analytics_script"].startswith("https://"):
        print("AHTOOL_ANALYTICS_SCRIPT must be an https:// URL (got %r)." % cfg["analytics_script"])
        return 2
    if cfg["sync_token"] and len(cfg["sync_token"]) < 32:
        print("AHTOOL_SYNC_TOKEN is shorter than 32 characters - refusing to start with a guessable token.")
        return 2
    data_dir = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(data_dir, exist_ok=True)
    if not os.access(data_dir, os.W_OK) or (os.path.exists(db_path) and not os.access(db_path, os.W_OK)):
        print("%s is not writable by uid %d. If the volume was created by an older image that ran as root, run once:\n"
              "  docker run --rm -v <volume>:/data alpine chown -R %d:%d /data"
              % (db_path, os.getuid(), os.getuid(), os.getgid()))
        return 2
    try:
        cfg["realm_aliases"] = sync.realm_aliases(os.environ.get("AHTOOL_REALM_ALIASES", "").strip())
    except ValueError as e:
        print("AHTOOL_REALM_ALIASES: %s" % e)
        return 2
    conn = db.connect(db_path)
    sync.ensure_schema(conn)
    sync.load_pushed_cfg(conn, cfg)
    merged = sync.merge_realm_aliases(conn, cfg["realm_aliases"])
    if merged:
        print("Realm aliases: merged %s into their canonical realm (%d price rows changed)."
              % (", ".join("%s (%d rows)" % kv for kv in sorted(merged["aliases"].items())), merged["changed"]),
              flush=True)
    n_keys = conn.execute("SELECT COUNT(*) FROM sync_keys WHERE revoked_at IS NULL").fetchone()[0]
    conn.close()

    host = os.environ.get("AHTOOL_HOST", "0.0.0.0")
    port = int(os.environ.get("AHTOOL_PORT", "8642"))
    srv = server.make_server(cfg, connect=lambda: db.connect(db_path), host=host, port=port, web=True)
    from ahtool import analytics, bnet
    try:
        bnet.settings(cfg)
    except bnet.BnetError as e:
        print("AHTOOL_BNET_*: %s" % e)
        return 2
    bnet.start(cfg, lambda: db.connect(db_path), on_change=lambda _r: analytics.invalidate())
    print("AH Tool web version on http://%s:%d/ (owner token %s, %d active key(s) - manage with: ahkey)"
          % (host, port, "set" if cfg["sync_token"] else "not set", n_keys), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
