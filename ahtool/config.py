"""Load config.json. Adds derived keys: root, db_path, cache_dir, dbcache_path."""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULTS = {
    "savedvariables": "",
    "addons_dir": "",
    "wago_build": "1.15.8.63829",
    "port": 8642,
    "ah_cut": 0.05,
    "deal_threshold": 0.25,
    "baseline_days": 14,
    "logger_keep_raw_scans": 6,
    "logger_max_age_h": 24,
}


def load(path=None):
    path = path or os.path.join(ROOT, "config.json")
    cfg = dict(DEFAULTS)
    if not os.path.exists(path):
        raise FileNotFoundError(
            "config.json not found - copy config.example.json to config.json and set the paths to your "
            "Auctionator.lua (WTF/Account/<ACCOUNT>/SavedVariables) and your Interface/AddOns folder.")
    with open(path, "r", encoding="utf-8") as f:
        cfg.update(json.load(f))
    cfg["root"] = ROOT
    cfg.setdefault("db_path", os.path.join(ROOT, "data", "ah.db"))
    cfg.setdefault("cache_dir", os.path.join(ROOT, "data", "cache"))
    if "dbcache_path" not in cfg:
        # <client>\Interface\AddOns -> <client>\Cache\ADB\enUS\DBCache.bin (client hotfix cache, optional)
        client = os.path.dirname(os.path.dirname(cfg["addons_dir"])) if cfg["addons_dir"] else ""
        cfg["dbcache_path"] = os.path.join(client, "Cache", "ADB", "enUS", "DBCache.bin") if client else ""
    return cfg
