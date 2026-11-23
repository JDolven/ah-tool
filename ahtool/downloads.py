"""Downloads offered on the web version: the AH Tool Logger addon and a ready-to-run Windows package.

Built when the Docker image is built:
    python -m ahtool.downloads build <out_dir> [--python-version 3.13.5 [--python-sha256 <hex>]]

  ahtool-logger-addon.zip  AHToolLogger/...  - unzip into Interface/AddOns
  ahtool-windows.zip       AHTool/...        - the app, start.bat and the addon; with --python-embed also python/,
                                               the official Windows embeddable Python, so nothing has to be installed
  manifest.json            what was built (names, sizes, sha256, Python version) - the download page reads it

The web version serves exactly the files named in manifest.json at /download/<name> (server.py). The packages are
built from explicit file lists, so local data (config.json, data/) can never end up in them.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ADDON_ZIP = "ahtool-logger-addon.zip"
WINDOWS_ZIP = "ahtool-windows.zip"
MANIFEST = "manifest.json"
APP_DIR = "AHTool"

# What goes into the Windows package (relative to ROOT). Directories are walked; nothing else is included.
APP_FILES = ("run.py", "start.bat", "config.example.json", "README.md", "LICENSE")
APP_DIRS = ("ahtool", "web", "addon")
SKIP_DIRS = {"__pycache__"}
SKIP_EXT = {".pyc", ".pyo", ".db"}

# Fixed timestamp inside the zips (SOURCE_DATE_EPOCH if set): the same source gives the same file.
_EPOCH = int(os.environ.get("SOURCE_DATE_EPOCH") or 0) or int(time.mktime((2026, 1, 1, 0, 0, 0, 0, 0, -1)))
_DATE = time.localtime(_EPOCH)[:6]


class BuildError(Exception):
    pass


def _walk(base):
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for fn in sorted(filenames):
            if fn.startswith(".") or os.path.splitext(fn)[1].lower() in SKIP_EXT:
                continue
            yield os.path.join(dirpath, fn)


def _add(zf, arcname, data, exe=False):
    zi = zipfile.ZipInfo(arcname.replace(os.sep, "/"), _DATE)
    zi.compress_type = zipfile.ZIP_DEFLATED
    zi.external_attr = (0o755 if exe else 0o644) << 16
    zf.writestr(zi, data)


def _add_file(zf, arcname, path):
    with open(path, "rb") as f:
        data = f.read()
    if arcname.endswith(".bat"):  # cmd.exe wants CRLF
        data = re.sub(rb"\r?\n", b"\r\n", data)
    _add(zf, arcname, data)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _embed_version(names):
    """'python313._pth' -> '3.13' (the embeddable zip names its files after the version)."""
    for n in names:
        m = re.fullmatch(r"python3(\d+)\._pth", n)
        if m:
            return "3." + m.group(1)
    return None


def _check_embed(zf):
    names = [n for n in zf.namelist() if not n.endswith("/")]
    if "python.exe" not in names or not _embed_version(names):
        raise BuildError("not a Windows embeddable Python zip (python.exe and python3xx._pth missing)")
    minor = int(_embed_version(names).split(".")[1])
    if minor < 12:
        raise BuildError("AH Tool needs Python 3.12 or newer (got %s)" % _embed_version(names))
    return names


def patch_pth(text):
    """The embeddable Python only searches the folders in its ._pth file. Add the app folder (python/..), so
    `python\\python.exe run.py` finds the ahtool package also without run.py's own sys.path line."""
    lines = text.splitlines()
    if ".." not in (ln.strip() for ln in lines):
        at = next((i + 1 for i, ln in enumerate(lines) if ln.strip() == "."), len(lines))
        lines.insert(at, "..")
    return "\r\n".join(lines) + "\r\n"


def build(out_dir, python_embed=None, python_version=None, root=ROOT):
    """Build the zips and manifest.json into out_dir. Returns the manifest."""
    os.makedirs(out_dir, exist_ok=True)
    addon_src = os.path.join(root, "addon", "AHToolLogger")
    if not os.path.isfile(os.path.join(addon_src, "AHToolLogger.toc")):
        raise BuildError("addon/AHToolLogger not found under %s" % root)

    # Addon: AHToolLogger/... at the top of the zip.
    with zipfile.ZipFile(os.path.join(out_dir, ADDON_ZIP), "w") as zf:
        for p in _walk(addon_src):
            _add_file(zf, os.path.join("AHToolLogger", os.path.relpath(p, addon_src)), p)

    # Windows package: AHTool/... (+ AHTool/python/ when an embeddable Python is given).
    embed = None
    with zipfile.ZipFile(os.path.join(out_dir, WINDOWS_ZIP), "w") as zf:
        for rel in APP_FILES:
            p = os.path.join(root, rel)
            if not os.path.isfile(p):
                raise BuildError("missing %s" % rel)
            _add_file(zf, os.path.join(APP_DIR, rel), p)
        for d in APP_DIRS:
            base = os.path.join(root, d)
            for p in _walk(base):
                _add_file(zf, os.path.join(APP_DIR, os.path.relpath(p, root)), p)
        if python_embed:
            with zipfile.ZipFile(python_embed) as ez:
                names = _check_embed(ez)
                for n in sorted(names):
                    data = ez.read(n)
                    if re.fullmatch(r"python3\d+\._pth", n):
                        data = patch_pth(data.decode("utf-8")).encode("utf-8")
                    _add(zf, "%s/python/%s" % (APP_DIR, n), data)
            embed = {"version": python_version or _embed_version(names), "sha256": _sha256(python_embed)}

    files = []
    for name, title in ((WINDOWS_ZIP, "AH Tool for Windows"), (ADDON_ZIP, "AH Tool Logger addon")):
        p = os.path.join(out_dir, name)
        files.append({"name": name, "title": title, "size": os.path.getsize(p), "sha256": _sha256(p)})
    manifest = {"built": int(time.time()), "python": embed, "files": files}
    with open(os.path.join(out_dir, MANIFEST), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)
    return manifest


def load_manifest(out_dir):
    """-> manifest dict, or None when nothing was built (local app, or an image built without downloads)."""
    try:
        with open(os.path.join(out_dir, MANIFEST), encoding="utf-8") as f:
            m = json.load(f)
        return m if isinstance(m, dict) and isinstance(m.get("files"), list) else None
    except (OSError, ValueError):
        return None


def embed_url(version):
    return "https://www.python.org/ftp/python/%s/python-%s-embed-amd64.zip" % (version, version)


def fetch_embed(version, dest, sha256=None, timeout=120):
    """Download the embeddable Python `version` from python.org to dest. -> dest, or None when it could not be
    downloaded (the package is then built without Python). A wrong sha256 raises BuildError: never ship that."""
    import urllib.request
    try:
        with urllib.request.urlopen(embed_url(version), timeout=timeout) as r:
            data = r.read()
    except OSError as e:
        print("downloads: could not download Python %s (%s) - building without it." % (version, e))
        return None
    got = hashlib.sha256(data).hexdigest()
    if sha256 and got.lower() != sha256.strip().lower():
        raise BuildError("Python %s embeddable zip has sha256 %s, expected %s" % (version, got, sha256))
    with open(dest, "wb") as f:
        f.write(data)
    print("downloads: Python %s embeddable zip, sha256 %s%s" % (version, got, "" if sha256 else " (not pinned)"))
    return dest


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build the downloads the web version offers.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("out_dir")
    b.add_argument("--python-embed", help="a python-3.x.y-embed-amd64.zip you already have")
    b.add_argument("--python-version", help="Python version to download from python.org (e.g. 3.13.5) when "
                                             "--python-embed is not given; also the version shown on the page")
    b.add_argument("--python-sha256", help="expected sha256 of the downloaded zip (recommended)")
    a = ap.parse_args(argv)
    embed = a.python_embed if a.python_embed and os.path.isfile(a.python_embed) else None
    if a.python_embed and not embed:
        print("No embeddable Python at %s - the Windows package will need Python installed." % a.python_embed)
    tmp = None
    try:
        if not embed and not a.python_embed and a.python_version:
            os.makedirs(a.out_dir, exist_ok=True)
            tmp = os.path.join(a.out_dir, ".python-embed.zip")
            embed = fetch_embed(a.python_version, tmp, a.python_sha256)
        m = build(a.out_dir, embed, a.python_version)
    except (BuildError, zipfile.BadZipFile) as e:
        print("downloads: %s" % e)
        return 1
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)
    try:
        m = build(a.out_dir, embed, a.python_version)
    except (BuildError, zipfile.BadZipFile) as e:
        print("downloads: %s" % e)
        return 1
    for f in m["files"]:
        print("%-26s %8d kB  sha256 %s" % (f["name"], f["size"] // 1024, f["sha256"]))
    print("Python included: %s" % (m["python"]["version"] if m["python"] else "no"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
