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
               trust_proxy=os.environ.get("AHTOOL_TRUST_PROXY", "").strip())
    if cfg["public_url"] and server.parse_origin(cfg["public_url"]) is None:
        print("AHTOOL_PUBLIC_URL must look like https://ah.example.net (got %r)." % cfg["public_url"])
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
    conn = db.connect(db_path)
    sync.ensure_schema(conn)
    sync.load_pushed_cfg(conn, cfg)
    n_keys = conn.execute("SELECT COUNT(*) FROM sync_keys WHERE revoked_at IS NULL").fetchone()[0]
    conn.close()

    host = os.environ.get("AHTOOL_HOST", "0.0.0.0")
    port = int(os.environ.get("AHTOOL_PORT", "8642"))
    srv = server.make_server(cfg, connect=lambda: db.connect(db_path), host=host, port=port, web=True)
    print("AH Tool web version on http://%s:%d/ (owner token %s, %d active key(s) - manage with: ahkey)"
          % (host, port, "set" if cfg["sync_token"] else "not set", n_keys), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
