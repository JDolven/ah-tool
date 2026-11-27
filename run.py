"""AH Tool launcher: static data -> ingest -> watcher -> web server -> browser."""
import json
import os
import sys
import threading
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.chdir(ROOT)


def load_cfg():
    try:
        from ahtool import config
        for name in ("load", "load_config", "get_config"):
            fn = getattr(config, name, None)
            if callable(fn):
                return fn()
    except ImportError:
        pass
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def main():
    from ahtool import alerts, analytics, db, firstrun, ingest, server, staticdata, watcher

    if not firstrun.ensure_config(ROOT):
        return
    cfg = load_cfg()
    port = int(cfg.get("port", 8642))
    print("AH Tool starting ...")
    conn = db.connect()
    print("Checking item and recipe data (the first run may take a while) ...")
    try:
        staticdata.ensure(conn, cfg)
    except Exception as e:
        print("  Warning: could not build static data: %s" % e)
    print("Reading Auctionator data ...")
    try:
        print("  Done: %s" % (ingest.ingest(conn, cfg),))
    except Exception as e:
        print("  Warning: import failed: %s" % e)
    try:
        from ahtool import market
        res = market.ingest(conn, cfg)
        if not res.get("missing"):
            print("  AH Tool Logger: %s" % (res,))
    except Exception as e:
        print("  Warning: AH Tool Logger import failed: %s" % e)
    conn.close()
    analytics.invalidate()

    # Optional: Blizzard's Game Data API (bnet_client_id / bnet_client_secret in config.json) - item vendor prices and
    # hourly auction house snapshots to compare with your scans. Off without a client.
    from ahtool import bnet
    if bnet.start(cfg, lambda: db.connect(cfg["db_path"]), on_change=lambda _r: analytics.invalidate()):
        print("Battle.net API: item data and auction house snapshots (compare: python -m ahtool.bnet compare).")

    pusher = None
    if cfg.get("sync_url") and cfg.get("sync_token"):
        from ahtool import sync

        def on_pulled(_res):
            # other players' prices arrived: same as a new import (page refresh, alerts)
            analytics.invalidate()
            try:
                c = db.connect(cfg["db_path"])
                try:
                    new = alerts.evaluate(c, cfg)
                finally:
                    c.close()
                if new:
                    print("  %d new alert(s)." % len(new))
            except Exception as e:
                print("  Alert evaluation failed: %s" % e)

        pusher = sync.Syncer(cfg, lambda: db.connect(cfg["db_path"]), on_pulled=on_pulled)
        print("Syncing market data with the web version at %s (push after each import, pull every %s min)."
              % (cfg["sync_url"], cfg.get("sync_pull_minutes", 5) if cfg.get("sync_pull", True) else "-"))
        pusher.trigger()

    def on_ingest(*_a, **_k):
        analytics.invalidate()
        print("New Auctionator data imported.")
        if pusher:
            pusher.trigger()
        try:
            c = db.connect()
            try:
                new = alerts.evaluate(c, cfg)
            finally:
                c.close()
            if new:
                print("  %d new alert(s)." % len(new))
        except Exception as e:
            print("  Alert evaluation failed: %s" % e)

    watcher.start(cfg, on_ingest=on_ingest)
    print("Watching Auctionator.lua for changes.")
    url = "http://localhost:%d/" % port
    try:
        srv = server.make_server(cfg, port=port)
    except OSError:
        print("\nPort %d is already in use - AH Tool is probably already running in another window." % port)
        print("Close the other AH Tool window (or end its python.exe) and start again.")
        print("Opening the already running instance: %s" % url)
        webbrowser.open(url)
        return
    print("Server running at %s  (Ctrl+C to quit)" % url)
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")


if __name__ == "__main__":
    main()
