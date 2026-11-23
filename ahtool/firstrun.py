"""First start without config.json: find the WoW folder and write config.json from config.example.json.

Looks in the usual install places (and the Blizzard registry key on Windows) for a client folder with
WTF/Account/<ACCOUNT>/ and Interface/AddOns. When exactly one fits - or one account has Auctionator data - the paths
are filled in and the app starts. Otherwise config.json is written with the example paths and opened for editing.
"""
import json
import os
import string
import sys

FLAVORS = ("_forever_", "_classic_era_")
INSTALL_DIRS = (
    r"Program Files (x86)\World of Warcraft",
    r"Program Files\World of Warcraft",
    r"World of Warcraft",
    r"Games\World of Warcraft",
    r"Blizzard\World of Warcraft",
    r"Program Files (x86)\Battle.net\World of Warcraft",
)


def _registry_installs():
    try:
        import winreg
    except ImportError:
        return []
    out = []
    for key in (r"SOFTWARE\WOW6432Node\Blizzard Entertainment\World of Warcraft",
                r"SOFTWARE\Blizzard Entertainment\World of Warcraft"):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
                p = winreg.QueryValueEx(k, "InstallPath")[0]
        except OSError:
            continue
        p = os.path.normpath(p)
        out.append(os.path.dirname(p) if os.path.basename(p).startswith("_") else p)  # ...\_retail_ -> install
    return out


def candidate_installs():
    if os.name != "nt":
        return []
    found = _registry_installs()
    for d in string.ascii_uppercase[2:]:
        if os.path.isdir(d + ":\\"):
            found += [os.path.join(d + ":\\", rel) for rel in INSTALL_DIRS]
    seen, out = set(), []
    for p in found:
        k = os.path.normcase(p)
        if k not in seen and os.path.isdir(p):
            seen.add(k)
            out.append(p)
    return out


def _accounts(client):
    base = os.path.join(client, "WTF", "Account")
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    return [os.path.join(base, n) for n in names if n != "SavedVariables" and os.path.isdir(os.path.join(base, n))]


def detect(installs=None):
    """-> (savedvariables, addons_dir, note) or None. Prefers Forever, then Classic Era; an account with
    Auctionator.lua wins over one without; among several the most recently written."""
    installs = candidate_installs() if installs is None else installs
    for flavor in FLAVORS:
        clients = [os.path.join(i, flavor) for i in installs if os.path.isdir(os.path.join(i, flavor, "Interface"))]
        options = []
        for client in clients:
            for acc in _accounts(client):
                sv = os.path.join(acc, "SavedVariables", "Auctionator.lua")
                mtime = os.path.getmtime(sv) if os.path.isfile(sv) else -1
                options.append((mtime, sv, os.path.join(client, "Interface", "AddOns")))
        if not options:
            continue
        with_data = [o for o in options if o[0] >= 0]
        if with_data:
            best = max(with_data)
            note = "" if len(with_data) == 1 else "several accounts have Auctionator data - picked the newest"
            return best[1], best[2], note
        if len(options) == 1:
            return options[0][1], options[0][2], "no Auctionator data yet - log in once with Auctionator enabled"
    return None


def ensure_config(root, installs=None, open_editor=True):
    """Make sure root/config.json exists. Returns True when the app can start, False when the user must edit it."""
    path = os.path.join(root, "config.json")
    if os.path.exists(path):
        return True
    with open(os.path.join(root, "config.example.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    found = detect(installs)
    if found:
        cfg["savedvariables"], cfg["addons_dir"], note = found
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    if found:
        print("First start: found your WoW folder and wrote config.json:")
        print("  Auctionator data: %s" % cfg["savedvariables"])
        print("  AddOns folder:    %s" % cfg["addons_dir"])
        if note:
            print("  Note: %s." % note)
        print("  (Edit config.json if this is wrong.)")
        return True
    print("First start: could not find your World of Warcraft folder.")
    print("config.json was created - set \"savedvariables\" (your ...\\WTF\\Account\\<ACCOUNT>\\SavedVariables\\"
          "Auctionator.lua) and \"addons_dir\" (your ...\\Interface\\AddOns), save, and start again.")
    if open_editor and os.name == "nt":
        try:
            import subprocess
            subprocess.Popen(["notepad.exe", path])
        except OSError:
            pass
    return False


if __name__ == "__main__":
    print(detect())
    sys.exit(0)
