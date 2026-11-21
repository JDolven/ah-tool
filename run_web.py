"""AH Tool web version: serves the pushed market data to everyone, keeps no personal data.

No config.json, no WoW folder, no watcher: the data arrives from the local app (ahtool/sync.py). Settings come from
the environment:
  AHTOOL_DB          database path (default /data/ah.db)
  AHTOOL_SYNC_TOKEN  shared secret the local app pushes with (sync is off without it)
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
               sync_token=os.environ.get("AHTOOL_SYNC_TOKEN", "").strip())
    if cfg["sync_token"] and len(cfg["sync_token"]) < 32:
        print("AHTOOL_SYNC_TOKEN is shorter than 32 characters - refusing to start with a guessable token.")
        return 2
    conn = db.connect(db_path)
    sync.load_pushed_cfg(conn, cfg)
    conn.close()

    host = os.environ.get("AHTOOL_HOST", "0.0.0.0")
    port = int(os.environ.get("AHTOOL_PORT", "8642"))
    srv = server.make_server(cfg, connect=lambda: db.connect(db_path), host=host, port=port, web=True)
    print("AH Tool web version on http://%s:%d/ (sync %s)" % (host, port, "on" if cfg["sync_token"] else "OFF"),
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
