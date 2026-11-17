"""Daemon thread polling the SavedVariables mtime and re-ingesting on change."""
import os
import sys
import threading
import time

from . import db as dbmod
from . import ingest as ingest_mod
from . import market


def start(cfg, on_ingest=None, interval=5.0):
    """Start the watcher; returns the thread. on_ingest(summary) is called after each non-skipped ingest."""
    stop = threading.Event()

    sources = [
        ("auctionator", lambda: cfg["savedvariables"], ingest_mod.ingest),
        ("logger", lambda: market.logger_path(cfg), market.ingest),
    ]

    def run():
        conn = None
        last_seen = {}
        while not stop.is_set():
            for name, path_fn, fn in sources:
                try:
                    path = path_fn()
                    if not path or not os.path.isfile(path):
                        continue
                    mt = os.stat(path).st_mtime
                    if conn is None:
                        conn = dbmod.connect(cfg["db_path"])
                    if mt == last_seen.get(name):
                        continue
                    if time.time() - mt < 1.0:  # likely mid-write; wait for next tick
                        continue
                    summary = fn(conn, cfg)  # skips by itself when this mtime is already ingested
                    last_seen[name] = mt
                    if on_ingest and not summary.get("skipped"):
                        try:
                            on_ingest(summary)
                        except Exception as ex:
                            print("[watcher] callback error:", ex, file=sys.stderr)
                except Exception as ex:  # parse error mid-write, db busy ... retry next tick
                    print("[watcher] %s ingest failed, retrying:" % name, repr(ex), file=sys.stderr)
            stop.wait(interval)

    t = threading.Thread(target=run, name="ahtool-watcher", daemon=True)
    t.stop_event = stop
    t.start()
    return t
