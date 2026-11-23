"""Downloads for the web version (ahtool/downloads.py), /download/ on the server and the first-start config
(ahtool/firstrun.py). Run: python tests/test_downloads.py"""
import io
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ahtool import db, downloads, firstrun, server  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print("FAIL:", msg)
    else:
        print("PASS", msg)


tmp = tempfile.mkdtemp()

# ---------------------------------------------------------------- a fake source tree (with local data that must stay out)
src = os.path.join(tmp, "src")
for rel, text in {
    "run.py": "print('hi')\n", "start.bat": "@echo off\npython run.py\n", "config.example.json": "{}\n",
    "README.md": "r\n", "LICENSE": "l\n", "ahtool/__init__.py": "", "ahtool/x.py": "x = 1\n",
    "ahtool/__pycache__/x.cpython-312.pyc": "junk", "web/index.html": "<html></html>",
    "addon/AHToolLogger/AHToolLogger.toc": "## Title: t\n", "addon/AHToolLogger/AHToolLogger.lua": "-- l\n",
    "config.json": '{"sync_token": "SECRET"}', "data/ah.db": "SECRET", "web/.hidden": "SECRET",
}.items():
    p = os.path.join(src, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)

# a fake embeddable Python
embed = os.path.join(tmp, "python-3.13.5-embed-amd64.zip")
with zipfile.ZipFile(embed, "w") as z:
    z.writestr("python.exe", b"MZ")
    z.writestr("python313.dll", b"MZ")
    z.writestr("python313.zip", b"PK")
    z.writestr("python313._pth", "python313.zip\r\n.\r\n\r\n# Uncomment to run site.main() automatically\r\n#import site\r\n")

out = os.path.join(tmp, "out")
m = downloads.build(out, embed, "3.13.5", root=src)
check(m["python"] and m["python"]["version"] == "3.13.5", "manifest names the Python version %s" % m["python"])
check([f["name"] for f in m["files"]] == [downloads.WINDOWS_ZIP, downloads.ADDON_ZIP], "manifest lists both zips")

with zipfile.ZipFile(os.path.join(out, downloads.ADDON_ZIP)) as z:
    names = sorted(z.namelist())
check(names == ["AHToolLogger/AHToolLogger.lua", "AHToolLogger/AHToolLogger.toc"], "addon zip: AHToolLogger/ on top %s" % names)

with zipfile.ZipFile(os.path.join(out, downloads.WINDOWS_ZIP)) as z:
    names = set(z.namelist())
    blob = b"".join(z.read(n) for n in names)
    pth = z.read("AHTool/python/python313._pth").decode()
    bat = z.read("AHTool/start.bat")
for n in ("AHTool/run.py", "AHTool/start.bat", "AHTool/config.example.json", "AHTool/LICENSE", "AHTool/ahtool/x.py",
          "AHTool/web/index.html", "AHTool/addon/AHToolLogger/AHToolLogger.toc", "AHTool/python/python.exe"):
    check(n in names, "windows zip has %s" % n)
check(not any("config.json" == os.path.basename(n) or "/data/" in n or "__pycache__" in n or ".hidden" in n
              for n in names), "windows zip has no config.json, data/, caches or hidden files")
check(b"SECRET" not in blob, "no local secrets in the windows zip")
check(pth.splitlines()[:3] == ["python313.zip", ".", ".."], "._pth gets the app folder: %r" % pth)
check(b"\r\n" in bat and b"\n" not in bat.replace(b"\r\n", b""), "start.bat has CRLF line ends")

again = os.path.join(tmp, "out2")
downloads.build(again, embed, "3.13.5", root=src)
check(downloads.load_manifest(again)["files"][0]["sha256"] == m["files"][0]["sha256"], "same source -> same zip")

nopy = downloads.build(os.path.join(tmp, "out3"), None, "3.13.5", root=src)
check(nopy["python"] is None, "without an embeddable zip: python is None")

bad = os.path.join(tmp, "bad.zip")
with zipfile.ZipFile(bad, "w") as z:
    z.writestr("python.exe", b"MZ")
    z.writestr("python311._pth", ".\r\n")
try:
    downloads.build(os.path.join(tmp, "out4"), bad, root=src)
    check(False, "Python 3.11 embed refused")
except downloads.BuildError:
    check(True, "Python 3.11 embed refused")

check(downloads.patch_pth("a.zip\n.\n..\n").count("..") == 1, "._pth patch is idempotent")

# ---------------------------------------------------------------- the real tree builds too
# Only from a full checkout: the web image's server stage has no addon/, run.py or start.bat (its downloads stage
# builds the real zips from them, and fails the image build if that goes wrong).
if os.path.isfile(os.path.join(downloads.ROOT, "addon", "AHToolLogger", "AHToolLogger.toc")):
    real = downloads.build(os.path.join(tmp, "real"))
    with zipfile.ZipFile(os.path.join(tmp, "real", downloads.WINDOWS_ZIP)) as z:
        rn = set(z.namelist())
    check("AHTool/ahtool/downloads.py" in rn and "AHTool/web/js/pages/download.js" in rn
          and "AHTool/addon/AHToolLogger/AHToolLogger.lua" in rn, "repository builds a complete package")
    check(not any(n.startswith("AHTool/tests/") or n.endswith("config.json") or "/data/" in n for n in rn),
          "repository package has no tests, config.json or data")
else:
    print("SKIP the real tree: no addon/ here (web image server stage)")

# ---------------------------------------------------------------- server: /download/ on the web version
webdir = tempfile.mkdtemp()
with open(os.path.join(webdir, "index.html"), "w", encoding="utf-8") as f:
    f.write("<!doctype html><html lang=\"en\"><head></head><body></body></html>")
with open(os.path.join(out, "other.zip"), "wb") as f:
    f.write(b"not listed")
web_db = os.path.join(tmp, "web.db")
srv = server.make_server({"web": True, "downloads_dir": out}, lambda: db.connect(web_db), port=0, web_dir=webdir, web=True)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % srv.server_address[1]


def get(path, method="GET", base=None):
    req = urllib.request.Request((base or BASE) + path, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


code, body, h = get("/download/manifest.json")
check(code == 200 and json.loads(body)["python"]["version"] == "3.13.5", "manifest served (%s)" % code)
code, body, h = get("/download/" + downloads.WINDOWS_ZIP)
with open(os.path.join(out, downloads.WINDOWS_ZIP), "rb") as f:
    want = f.read()
check(code == 200 and body == want, "windows zip served byte for byte (%s)" % code)
check(h.get("Content-Type") == "application/zip" and "attachment" in (h.get("Content-Disposition") or ""),
      "zip served as an attachment")
check(zipfile.ZipFile(io.BytesIO(body)).testzip() is None, "served zip is valid")
code, body, h = get("/download/" + downloads.ADDON_ZIP, method="HEAD")
check(code == 200 and body == b"" and int(h.get("Content-Length")) == os.path.getsize(os.path.join(out, downloads.ADDON_ZIP)),
      "HEAD gives the size, no body")
for bad_path in ("/download/other.zip", "/download/..%2Fout2%2Fmanifest.json", "/download/../web.db", "/download/",
                 "/download/%2e%2e/manifest.json"):
    code, body, _ = get(bad_path)
    check(code == 404, "%s -> 404 (%s)" % (bad_path, code))
req = urllib.request.Request(BASE + "/download/" + downloads.ADDON_ZIP, data=b"{}", method="POST",
                             headers={"Content-Type": "application/json", "Origin": BASE})
try:
    urllib.request.urlopen(req, timeout=10)
    check(False, "POST to a download refused")
except urllib.error.HTTPError as e:
    check(e.code in (403, 405), "POST to a download refused (%s)" % e.code)

srv2 = server.make_server({"web": True, "downloads_dir": os.path.join(tmp, "nothing")}, lambda: db.connect(web_db),
                          port=0, web_dir=webdir, web=True)
threading.Thread(target=srv2.serve_forever, daemon=True).start()
code, _, _ = get("/download/manifest.json", base="http://127.0.0.1:%d" % srv2.server_address[1])
check(code == 404, "no downloads built -> 404 (%s)" % code)

srv3 = server.make_server({"downloads_dir": out}, lambda: db.connect(web_db), port=0, web_dir=webdir)
threading.Thread(target=srv3.serve_forever, daemon=True).start()
code, body, _ = get("/download/" + downloads.WINDOWS_ZIP, base="http://127.0.0.1:%d" % srv3.server_address[1])
check(body != want, "local app does not serve downloads (%s)" % code)

# ---------------------------------------------------------------- first start: find the WoW folder
wow = os.path.join(tmp, "World of Warcraft")


def mk(*parts):
    p = os.path.join(wow, *parts)
    os.makedirs(p, exist_ok=True)
    return p


mk("_forever_", "Interface", "AddOns")
mk("_forever_", "WTF", "Account", "SavedVariables")  # account-wide folder, not an account
check(firstrun.detect([wow]) is None, "no account yet -> nothing found")
mk("_forever_", "WTF", "Account", "ACC1", "SavedVariables")
r = firstrun.detect([wow])
check(r and r[0].endswith(os.path.join("ACC1", "SavedVariables", "Auctionator.lua")) and r[2], "one account, no data yet %s" % (r,))
mk("_forever_", "WTF", "Account", "ACC2", "SavedVariables")
check(firstrun.detect([wow]) is None, "two accounts without data -> ask")
with open(os.path.join(wow, "_forever_", "WTF", "Account", "ACC2", "SavedVariables", "Auctionator.lua"), "w") as f:
    f.write("x")
r = firstrun.detect([wow])
check(r and "ACC2" in r[0] and r[1] == os.path.join(wow, "_forever_", "Interface", "AddOns") and not r[2],
      "the account with Auctionator data wins %s" % (r,))

app = os.path.join(tmp, "app")
os.makedirs(app)
with open(os.path.join(app, "config.example.json"), "w") as f:
    json.dump({"savedvariables": "C:\\Path", "addons_dir": "C:\\Path", "port": 8642}, f)
check(firstrun.ensure_config(app, [wow], open_editor=False), "found -> app can start")
with open(os.path.join(app, "config.json")) as f:
    c = json.load(f)
check("ACC2" in c["savedvariables"] and c["port"] == 8642, "config.json written from the example with the paths")
check(firstrun.ensure_config(app, [], open_editor=False), "existing config.json is left alone")
os.remove(os.path.join(app, "config.json"))
check(not firstrun.ensure_config(app, [], open_editor=False), "not found -> user must edit")
check(os.path.exists(os.path.join(app, "config.json")), "... and config.json was created for that")

for s in (srv, srv2, srv3):
    s.shutdown()
print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
