"""Endpoint tests against data/test.db fixture. Run: python tests/test_api.py"""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import make_fixture_db  # noqa: E402
from ahtool import server  # noqa: E402

DB = os.path.join(ROOT, "data", "test.db")
make_fixture_db.build(DB)


def connect():
    try:  # use the real db.connect when it exists (tests only: fall back to plain sqlite)
        from ahtool import db
        return db.connect(DB)
    except Exception:
        c = sqlite3.connect(DB)
        c.row_factory = sqlite3.Row
        return c


cfg = {"port": 0, "ah_cut": 0.05, "deal_threshold": 0.25, "baseline_days": 14, "trend_min_profit": 50}
web = tempfile.mkdtemp()
with open(os.path.join(web, "index.html"), "w", encoding="utf-8") as f:
    f.write("<html>INDEX</html>")
with open(os.path.join(web, "app.js"), "w") as f:
    f.write("x=1")
srv = server.make_server(cfg, connect, port=0, web_dir=web)
BASE = "http://127.0.0.1:%d" % srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


def get(path, method="GET"):
    req = urllib.request.Request(BASE + path, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def j(path):
    st, h, b = get(path)
    assert st == 200, (path, st, b)
    assert h["Cache-Control"] == "no-store"
    return json.loads(b)


fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name, extra if not cond else "")
    if not cond:
        fails.append(name)


s = j("/api/status")
check("status", s["realm"] == "PvP" and s["items_priced"] == 10 and s["latest_day"] == 2511
      and s["latest_date"] == "2026-11-16" and s["recipes_loaded"] == 4, s)

r = j("/api/items/search?q=linen")
check("search name", [x["id"] for x in r][:2] == [2589, 2996] or {x["id"] for x in r} == {2589, 2996, 2576}, r)
check("search id", j("/api/items/search?q=284712")[0]["id"] == 284712)
r = j("/api/items/search?limit=3")
check("search empty = most valuable", r[0]["id"] == 19019 and len(r) == 3, r)
bw = j("/api/items/search?q=bruise")[0]
check("search baseline/discount", bw["baseline"] == 1000 and abs(bw["discount"] - 0.4) < 1e-9, bw)

d = j("/api/item/2453")
check("item detail", d["name"] == "Bruiseweed" and d["current"]["price"] == 600
      and d["current"]["stale"] is False and d["stats"]["baseline"] == 1000
      and d["stats"]["trend7_pct"] == -40.0 and len(d["history"]) == 30
      and d["history"][-1]["date"] == "2026-11-16" and len(d["snapshots"]) == 4
      and d["used_in"][0]["count"] == 1 and d["used_in"][0]["profession"] == "Alchemy"
      and d["postings"][0]["price"] == 900, d["stats"])
check("item stats", d["stats"]["min30"] == 600 and d["stats"]["max30"] == 1000 and d["stats"]["median30"] == 1000
      and d["stats"]["volatility"] and d["stats"]["days_of_data"] == 30)
check("item crafted_by", [c["spell_id"] for c in j("/api/item/118")["crafted_by"]] == [2329])
check("item stale", j("/api/item/1206")["current"]["stale"] is True)
check("item 404", get("/api/item/55555")[0] == 404)

p = j("/api/professions")
check("professions", len(p) == 13 and next(x for x in p if x["id"] == 4)["name_no"] == "Alchemy"
      and next(x for x in p if x["id"] == 8)["recipe_count"] == 2
      and next(x for x in p if x["id"] == 8)["best_profit"] == 515, p[7])

rs = {x["spell_id"]: x for x in j("/api/professions/8")}
b = rs[2963]
# hand: cost 2*100=200; sell 300; profit 300*.95-200=85; margin .425
check("recipe bolt", b["cost"] == 200 and b["sell"] == 300 and abs(b["profit"] - 85) < 1e-6
      and abs(b["margin"] - 0.425) < 1e-6 and b["cost_complete"], b)
v = rs[2385]
# hand: 3*300 + thread vendor 10 = 910; profit 1500*.95-910=515
check("recipe vest vendor reagent", v["cost"] == 910 and v["profit"] == 515
      and [x["source"] for x in v["reagents"]] == ["ah", "vendor"] or
      {x["item_id"]: x["source"] for x in v["reagents"]} == {2996: "ah", 2320: "vendor"}, v)
a = {x["spell_id"]: x for x in j("/api/professions/4")}[2329]
# hand: 600+50 (vial vendor only) = 650; 1500*.95-650 = 775
check("recipe potion current", a["cost"] == 650 and a["profit"] == 775 and a["cost_complete"], a)
check("potion deal flag", {x["item_id"]: x["is_deal"] for x in a["reagents"]}[2453] is True)
ab = {x["spell_id"]: x for x in j("/api/professions/4?mode=baseline")}[2329]
# hand: 1000+50 = 1050; 1425-1050 = 375
check("recipe potion baseline", ab["cost"] == 1050 and ab["profit"] == 375, ab)
e = j("/api/professions/10")[0]
check("enchant no sell", e["created_item_id"] is None and e["sell"] is None and e["profit"] is None
      and e["name"].startswith("Enchant") and e["cost"] == 200, e)

dl = j("/api/deals")
ids = [x["item_id"] for x in dl]
check("deals set", ids == [4306, 2453], ids)
uv = dl[0]
check("deal under_vendor", uv["reasons"] == ["under_vendor"] and uv["vendor_profit"] == 300, uv)
# browse: AH categories with counts, filters, slot from the tooltip
br = j("/api/browse?listed=0")
cats = {c["name"]: c for c in br["categories"]}
check("browse categories", {"Trade Goods", "Armor", "Weapon", "Consumable"} <= set(cats), list(cats))
check("browse total", br["total"] == sum(c["count"] for c in br["categories"]), br["total"])
tg = {s["name"]: s["count"] for s in cats["Trade Goods"]["subclasses"]}
check("browse subclasses", tg.get("Cloth", 0) >= 2 and "Herb" in tg, tg)
w = j("/api/browse?listed=0&class=2&sub=7")
check("browse weapon class+sub", [x["item_id"] for x in w["items"]] == [19019] and w["items"][0]["slot"], w["items"])
check("browse slot filter", [x["item_id"] for x in j("/api/browse?listed=0&class=2&sub=7&slot=Two-Hand")["items"]] == [], "")
check("browse search", [x["item_id"] for x in j("/api/browse?listed=0&q=linen")["items"]] and
      all("linen" in x["name"].lower() for x in j("/api/browse?listed=0&q=linen")["items"]), "")
check("browse quality", all(x["quality"] >= 2 for x in j("/api/browse?listed=0&quality=2")["items"]), "")
check("browse min level", j("/api/browse?listed=0&min_level=60")["total"] <= 1, "")
check("deal under_vendor: no logger scan -> no total", uv.get("vendor_profit_total") is None and "vendor_qty" in uv, uv)
bd = dl[1]
check("deal below_baseline", bd["reasons"] == ["below_baseline", "cheap_reagent"] and bd["baseline"] == 1000
      and abs(bd["discount"] - 0.4) < 1e-9 and bd["professions"] == ["Alchemy"]
      and abs(bd["score"] - 0.4 * 3.0004340774793188) < 1e-3, bd)  # log10(1001)
check("deals reason filter", [x["item_id"] for x in j("/api/deals?reason=under_vendor")] == [4306])
check("deals threshold", [x["item_id"] for x in j("/api/deals?threshold=0.5")] == [4306])
check("deals min_value", [x["item_id"] for x in j("/api/deals?min_value=800")] == [2453])

m = j("/api/stats/market")
# live = 9 items (Moss Agate stale)
check("market totals", m["totals"]["items"] == 9 and m["totals"]["listed_quantity"] ==
      5000 + 200 + 40 + 3 + 30 + 120 + 25 + 1 + 2, m["totals"])
check("market classes", any(c["class_name"] == "Trade Goods" for c in m["by_class"])
      and any(c["class_name"] == "Unknown" for c in m["by_class"]), m["by_class"])
check("market movers", m["movers_down"][0]["id"] == 2453 and m["movers_down"][0]["trend7_pct"] == -40.0
      and m["most_valuable"][0]["id"] == 19019 and m["most_listed"][0]["id"] == 2589 and len(m["scan_days"]) >= 30)
check("market quality", {q["quality"] for q in m["by_quality"]} == {1, 5})

po = j("/api/postings")
pp = {x["item_id"]: x for x in po}
check("postings", pp[2453]["current_price"] == 600 and abs(pp[2453]["diff_pct"] - 50.0) < 1e-6
      and pp[999999]["current_price"] is None and pp[999999]["diff_pct"] is None, po)

check("realm param", j("/api/status?realm=Nope")["items_priced"] == 0)
check("api 404", get("/api/nope")[0] == 404 and "error" in json.loads(get("/api/nope")[2]))
st, h, body = get("/")
check("static index", st == 200 and b"INDEX" in body and h["Content-Type"].startswith("text/html"))
st, h, body = get("/app.js")
check("static js type", st == 200 and "javascript" in h["Content-Type"])
st, h, body = get("/deals/whatever")
check("spa fallback", st == 200 and b"INDEX" in body)
check("static missing file 404", get("/missing.css")[0] == 404)
check("traversal blocked", get("/..%2f..%2fconfig.json")[0] in (403, 404, 200) and b"savedvariables" not in get("/..%2f..%2fconfig.json")[2])

# ====================================================================== v2
import base64  # noqa: E402
import time  # noqa: E402
from ahtool import alerts as alerts_mod, analytics, notify, shopping  # noqa: E402


def req(method, path, body=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    rq = urllib.request.Request(BASE + path, data=data, method=method,
                                headers={"Content-Type": "application/json"} if data is not None else {})
    try:
        with urllib.request.urlopen(rq) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def ok(method, path, body=None):
    st, d = req(method, path, body)
    assert st == 200, (method, path, st, d)
    return d


# Auctionator import string format (contract example)
check("auctionator term format", shopping.auctionator_term("Linen Cloth", 20 * 1.15, 20)
      == '"Linen Cloth";;0;0;0;0;0;0;0;0.0023;;;;20')
check("auctionator import prefix", shopping.build_import([shopping.auctionator_term("Linen Cloth", 23, 20)])
      == 'AH Tool^"Linen Cloth";;0;0;0;0;0;0;0;0.0023;;;;20')

# toast script: text only travels as base64, command is UTF-16LE base64
sc = notify.build_script("it's \"x\"", "$(evil) `n 'q'")
check("notify script safe", "evil" not in sc and "$(" not in sc.split("$title")[0])
check("notify encode", base64.b64decode(notify.encode_command("aæ")) == "aæ".encode("utf-16-le"))

# defaults
check("myprofessions empty", ok("GET", "/api/myprofessions") == [])
check("status unseen_alerts", j("/api/status")["unseen_alerts"] == 0)
check("status follows ?realm=", j("/api/status?realm=Elsewhere")["realm"] == "Elsewhere")
check("settings defaults", ok("GET", "/api/settings") == {"notify_windows": True, "notify_vendor_flip_min": 0,
                                                          "notify_my_reagents": False, "shopping_max_markup": 0.15})
check("professions mine default", all(x["mine"] is False and x["my_skill"] is None for x in j("/api/professions")))
check("recipe can_craft default", all(r["can_craft"] is False and r["known"] is None for r in j("/api/professions/8")))
check("mycrafts empty", j("/api/mycrafts") == [])
check("watch null", j("/api/item/2589")["watch"] is None)
check("deals for_my_crafts false", all(d["for_my_crafts"] is False for d in j("/api/deals")))

# my professions + can_craft rules
r = ok("PUT", "/api/myprofessions", [{"profession_id": 8, "skill": 30}, {"profession_id": 4, "skill": 300}])
check("put myprofessions", [(x["profession_id"], x["name"], x["skill"]) for x in r]
      == [(4, "Alchemy", 300), (8, "Tailoring", 30)], r)
check("get myprofessions", ok("GET", "/api/myprofessions") == r)
p = {x["id"]: x for x in j("/api/professions")}
check("professions mine/my_skill", p[8]["mine"] and p[8]["my_skill"] == 30 and p[4]["my_skill"] == 300 and not p[2]["mine"])
rs = {x["spell_id"]: x for x in j("/api/professions/8")}
check("can_craft by skill", rs[2963]["can_craft"] is True and rs[2385]["can_craft"] is False, rs[2385]["skill"])
check("mine=1 filter", [x["spell_id"] for x in j("/api/professions/8?mine=1")] == [2963])
mc = j("/api/mycrafts")
check("mycrafts content", [x["spell_id"] for x in mc] == [2329, 2963] and mc[0]["profession_name"] == "Alchemy"
      and all(x["can_craft"] for x in mc), [x["spell_id"] for x in mc])
check("mycrafts limit/mode", len(j("/api/mycrafts?limit=1")) == 1 and j("/api/mycrafts?mode=baseline")[0]["spell_id"] == 2329)
# known overrides
check("known 1", ok("PUT", "/api/known/2385", {"known": 1}) == {"spell_id": 2385, "known": 1})
check("known 0", ok("PUT", "/api/known/2963", {"known": 0})["known"] == 0)
rs = {x["spell_id"]: x for x in j("/api/professions/8")}
check("known overrides skill", rs[2385]["can_craft"] is True and rs[2385]["known"] == 1
      and rs[2963]["can_craft"] is False and rs[2963]["known"] == 0)
check("item crafted_by has can_craft", j("/api/item/2996")["crafted_by"][0]["can_craft"] is False)
check("known null deletes", ok("PUT", "/api/known/2963", {"known": None})["known"] is None
      and {x["spell_id"]: x for x in j("/api/professions/8")}[2963]["can_craft"] is True)
ok("PUT", "/api/known/2385", {"known": None})
dl = {d["item_id"]: d for d in j("/api/deals")}
check("deals for_my_crafts", dl[2453]["for_my_crafts"] is True and dl[4306]["for_my_crafts"] is False)

# validation -> 400 / 404
for name, m, path, body in [
        ("bad prof id", "PUT", "/api/myprofessions", [{"profession_id": 99, "skill": 10}]),
        ("bad skill", "PUT", "/api/myprofessions", [{"profession_id": 8, "skill": 999}]),
        ("not a list", "PUT", "/api/myprofessions", {"a": 1}),
        ("bad known", "PUT", "/api/known/2963", {"known": 5}),
        ("bad qty", "PUT", "/api/shopping/2963", {"qty": "x"}),
        ("bad max_price", "PUT", "/api/watchlist/2589", {"max_price": -5}),
        ("bad setting key", "PUT", "/api/settings", {"nope": 1}),
        ("bad setting type", "PUT", "/api/settings", {"notify_windows": "yes"})]:
    check("400 " + name, req(m, path, body)[0] == 400)
check("400 invalid json", req("PUT", "/api/settings", raw=b"{nope")[0] == 400)
check("404 unknown recipe", req("PUT", "/api/shopping/123456789", {"qty": 1})[0] == 404
      and req("PUT", "/api/known/123456789", {"known": 1})[0] == 404)
check("404 delete unknown watch", req("DELETE", "/api/watchlist/424242")[0] == 404)
check("404 unknown put", req("PUT", "/api/nope", {})[0] == 404)

# shopping list
sh = ok("GET", "/api/shopping")
check("shopping empty", sh["recipes"] == [] and sh["reagents"] == [] and sh["auctionator_import"] == "")
ok("PUT", "/api/shopping/2385", {"qty": 2})
ok("PUT", "/api/shopping/2963", {"qty": 5})
sh = ok("GET", "/api/shopping")
rg = {x["item_id"]: x for x in sh["reagents"]}
# 2385 x2: 6 bolts + 2 thread; 2963 x5: 10 linen. Bolt 300 (AH), thread vendor 10, linen 100
check("shopping reagents", rg[2996]["need"] == 6 and rg[2320]["need"] == 2 and rg[2589]["need"] == 10
      and rg[2320]["source"] == "vendor" and rg[2996]["source"] == "ah" and rg[2996]["total"] == 1800
      and rg[2589]["total"] == 1000 and rg[2589]["available"] == 5000, rg)
check("shopping totals", sh["totals"]["cost"] == 910 * 2 + 200 * 5 and sh["totals"]["sell"] == 1500 * 2 + 300 * 5
      and abs(sh["totals"]["profit"] - (515 * 2 + 85 * 5)) < 1e-6, sh["totals"])
sr = {x["spell_id"]: x for x in sh["recipes"]}
check("shopping recipes", sr[2385]["qty"] == 2 and sr[2385]["total_cost"] == 1820 and sr[2385]["cost_each"] == 910
      and sr[2385]["profession_name"] == "Tailoring" and sr[2963]["total_profit"] == 425
      and sr[2963]["profit_each"] == 85 and sr[2963]["sell_each"] == 300 and sr[2963]["created_item_id"] == 2996, sr)
check("shopping vendor_reagents", [x["item_id"] for x in sh["vendor_reagents"]] == [2320])
imp = sh["auctionator_import"]
base_linen = rg[2589]["baseline"]
check("shopping import", imp.startswith("AH Tool^") and imp.count("^") == 2 and "Coarse Thread" not in imp
      and '"Bolt of Linen Cloth";;0;0;0;0;0;0;0;0.0345;;;;6' in imp
      and '"Linen Cloth";;0;0;0;0;0;0;0;%s;;;;10' % repr(round(max(100, base_linen) * 1.15 / 10000, 4)) in imp, imp)
ok("PUT", "/api/settings", {"shopping_max_markup": 0})
check("markup setting", '"Bolt of Linen Cloth";;0;0;0;0;0;0;0;0.03;;;;6' in ok("GET", "/api/shopping")["auctionator_import"])
ok("PUT", "/api/settings", {"shopping_max_markup": 0.15})
ok("PUT", "/api/shopping/2385", {"qty": 0})
check("shopping qty<=0 removes", [x["spell_id"] for x in ok("GET", "/api/shopping")["recipes"]] == [2963])
ok("DELETE", "/api/shopping")
check("shopping cleared", ok("GET", "/api/shopping")["recipes"] == [])

# watchlist + alert dedupe
ok("PUT", "/api/settings", {"notify_windows": False})  # never toast during tests
w = ok("PUT", "/api/watchlist/2589", {"max_price": 120, "note": "linen"})
check("watch put", w == {"max_price": 120, "enabled": True, "note": "linen"}, w)
check("item watch field", j("/api/item/2589")["watch"] == w)
wl = {x["item_id"]: x for x in ok("GET", "/api/watchlist")}
check("watchlist get", wl[2589]["price"] == 100 and wl[2589]["triggered"] is True and wl[2589]["name"] == "Linen Cloth"
      and wl[2589]["max_price"] == 120 and wl[2589]["enabled"] is True and "baseline" in wl[2589]
      and "discount" in wl[2589] and wl[2589]["available"] == 5000, wl)
ok("PUT", "/api/watchlist/2453", {})        # NULL max_price -> deal rule
ok("PUT", "/api/watchlist/2996", {"max_price": 100})  # 300 > 100 never triggers
cx = connect()


def setprice(iid, price):
    cx.execute("UPDATE price_current SET price=? WHERE realm='PvP' AND item_id=?", (price, iid))
    cx.commit()
    analytics.invalidate()


def ev(**kw):
    analytics.invalidate()
    return alerts_mod.evaluate(cx, cfg, notify_user=False, **kw)


a1 = ev()
check("alert fires first time", sorted((a["item_id"], a["kind"]) for a in a1) == [(2453, "watch"), (2589, "watch")], a1)
msg = {a["item_id"]: a["message"] for a in a1}
check("alert message", msg[2589] == "Linen Cloth 1s <= 1s 20c", msg)
check("alert same price -> none", ev() == [])
setprice(2589, 90)
a3 = ev()
check("alert lower price -> alert", [a["item_id"] for a in a3] == [2589] and a3[0]["price"] == 90, a3)
check("alert same lower price -> none", ev() == [])
setprice(2589, 95)
check("alert higher but still true -> none", ev() == [])
setprice(2589, 200)  # condition false -> reset
check("alert condition false -> none", ev() == [])
row = cx.execute("SELECT last_alert_price, last_alert_ts FROM watchlist WHERE item_id=2589").fetchone()
check("alert state reset", row[0] is None and row[1] is None, tuple(row))
setprice(2589, 110)
check("alert after reset fires again (even at higher price)", [a["item_id"] for a in ev()] == [2589])
r24 = ev(now=int(time.time()) + 25 * 3600)
check("alert 24h re-fire (all active watches)", sorted(a["item_id"] for a in r24) == [2453, 2589], r24)
ok("PUT", "/api/watchlist/2589", {"enabled": False})
setprice(2589, 50)
check("disabled -> no alert, not triggered", ev() == []
      and {x["item_id"]: x for x in ok("GET", "/api/watchlist")}[2589]["triggered"] is False)
ok("PUT", "/api/watchlist/2589", {"enabled": True})
setprice(2589, 100)
check("re-enable resets dedupe", [a["item_id"] for a in ev()] == [2589])

al = ok("GET", "/api/alerts")
check("alerts list", al[0]["id"] > al[-1]["id"] and not al[0]["seen"] and al[0]["name"] and "kind" in al[0]
      and "threshold" in al[0] and "message" in al[0], al[0])
check("alerts limit", len(ok("GET", "/api/alerts?limit=2")) == 2)
n_un = j("/api/status")["unseen_alerts"]
check("status unseen_alerts counts", n_un == len(al) and n_un > 0, n_un)
ok("POST", "/api/alerts/seen")
check("alerts seen", j("/api/status")["unseen_alerts"] == 0 and all(a["seen"] for a in ok("GET", "/api/alerts")))

# POST /api/alerts/evaluate (forced)
setprice(2589, 80)
res = ok("POST", "/api/alerts/evaluate?notify=0")
check("evaluate endpoint", res["new"] == 1 and res["alerts"][0]["item_id"] == 2589, res)
check("evaluate endpoint idempotent", ok("POST", "/api/alerts/evaluate?notify=0")["new"] == 0)

# vendor flip + my reagent rules
ok("PUT", "/api/settings", {"notify_vendor_flip_min": 200})
vf = ok("POST", "/api/alerts/evaluate?notify=0")
check("vendor flip alert", [(a["kind"], a["item_id"]) for a in vf["alerts"]] == [("vendor_flip", 4306)], vf)
check("vendor flip dedupe", ok("POST", "/api/alerts/evaluate?notify=0")["new"] == 0)
ok("PUT", "/api/settings", {"notify_vendor_flip_min": 0})
ok("PUT", "/api/settings", {"notify_my_reagents": True})
mr = ok("POST", "/api/alerts/evaluate?notify=0")
check("my reagent alert", [(a["kind"], a["item_id"]) for a in mr["alerts"]] == [("my_reagent", 2453)], mr)
check("my reagent dedupe", ok("POST", "/api/alerts/evaluate?notify=0")["new"] == 0)
ok("PUT", "/api/settings", {"notify_my_reagents": False})

# watch delete
ok("DELETE", "/api/watchlist/2453")
check("watch deleted", 2453 not in [x["item_id"] for x in ok("GET", "/api/watchlist")] and j("/api/item/2453")["watch"] is None)

# unexportable names (quote / caret)
make_fixture_db.add_extras(cx)
analytics.invalidate()
ok("PUT", "/api/shopping/9001", {"qty": 3})
sh = ok("GET", "/api/shopping")
check("unexportable", sorted(x["item_id"] for x in sh["unexportable"]) == [7001, 7002]
      and sh["auctionator_import"].startswith('AH Tool^"Linen Cloth";;0;0;0;0;0;0;0;')
      and "Odd" not in sh["auctionator_import"] and "Caret" not in sh["auctionator_import"]
      and {x["item_id"] for x in sh["reagents"]} >= {7001, 7002}, sh["unexportable"])
ok("DELETE", "/api/shopping")

# tooltips
tt = j("/api/tooltip/19019")
texts = [l["text"] for l in tt["lines"]]
check("tooltip static lines", tt["name"] == "Thunderfury" and tt["quality"] == 5 and tt["icon"] == "inv_sword_39"
      and tt["icon_url"].endswith("/medium/inv_sword_39.jpg") and not tt["custom"]
      and texts[:4] == ["Binds when picked up", "Unique", "One-Hand", "44 - 115 Damage"]
      and tt["lines"][2]["right"] == "Sword" and tt["lines"][3]["right"] == "Speed 1.90"
      and "+16 - 30 Nature Damage" in texts and "(53.9 damage per second)" in texts
      and "Durability 125 / 125" in texts and "Requires Level 60" in texts
      and {"text": "Item Level 80", "style": "yellow"} in tt["lines"]
      and {"text": "Chance on hit: Blasts your enemy with lightning.", "style": "green"} in tt["lines"]
      and {"text": '"Flavour."', "style": "yellow"} in tt["lines"]
      and tt["lines"][-1] == {"text": "Sell Price:", "style": "white", "money": 255355}, tt)
check("tooltip AH section", tt["ah"]["price"] == 5000000 and tt["ah"]["available"] == 1
      and tt["ah"]["used_in"]["count"] == 0 and tt["ah"]["crafted_by"] == [], tt["ah"])
tl = j("/api/tooltip/2589")
check("tooltip fallback (no static row)", tl["name"] == "Linen Cloth" and tl["icon"] == "inv_misc_questionmark"
      and tl["ah"]["price"] == 80 and tl["ah"]["baseline"] == 100  # 80: setprice() above and tl["ah"]["used_in"]["count"] == 3
      and set(tl["ah"]["used_in"]["professions"]) == {"Tailoring", "Enchanting", "Alchemy"}
      and tl["lines"][-1].get("money") == 13, tl)
tb = j("/api/tooltip/2996")
check("tooltip crafted_by reagents", tb["ah"]["crafted_by"][0]["profession"] == "Tailoring"
      and tb["ah"]["crafted_by"][0]["reagents"] == [{"item_id": 2589, "name": "Linen Cloth", "quality": 1, "count": 2,
                                                     "unit_price": 80, "source": "ah"}], tb["ah"])
tc = j("/api/tooltip/284712")
check("tooltip custom item", tc["name"] is None and tc["custom"] and tc["ah"]["price"] == 900, tc)
check("tooltip 404", get("/api/tooltip/999999")[0] == 404 and get("/api/tooltip/abc")[0] == 404)
import time as _t  # noqa: E402
_t0 = _t.perf_counter()
for _ in range(20):
    j("/api/tooltip/19019")
check("tooltip fast (cached)", (_t.perf_counter() - _t0) / 20 < 0.02, (_t.perf_counter() - _t0) / 20)

# ====================================================================== v4: robust prices, trends, leveling routes
make_fixture_db.add_v4(cx)
analytics.invalidate()
cfg["trend_min_profit"] = 50
CUT = 0.05
DAY = make_fixture_db.D
ctx4 = analytics.get_ctx(cx, cfg)

# Hampel filter
pts = [(d, 100) for d in range(10)]
pts[5] = (5, 5000)
cl = analytics._clean(pts, ctx4.p)
check("hampel flags spike", [d for d, _, o in cl if o] == [5], cl)
check("hampel needs >= 5 points",
      not any(o for _, _, o in analytics._clean([(0, 100), (1, 5000), (2, 100), (3, 100)], ctx4.p)))
check("hampel keeps small noise",
      not any(o for _, _, o in analytics._clean([(d, 90 if d % 2 else 110) for d in range(14)], ctx4.p)))
check("hampel keeps level shift",
      not any(o for _, _, o in analytics._clean([(d, 500 if d < 7 else 1000) for d in range(14)], ctx4.p)))
ser = analytics.daily_series(ctx4, 8001)
check("daily_series(ctx)", [d for d, _, o in ser if o] == [DAY - 5] and len(ser) == 30, ser[-8:])
check("daily_series(conn) full",
      [(d, p, o) for d, p, o in analytics.daily_series(cx, 8001, cfg) if o] == [(DAY - 5, 5000, True)])
d1 = j("/api/item/8001")
check("spike excluded from stats", d1["stats"]["baseline"] == 100 and d1["stats"]["max30"] == 100
      and d1["stats"]["avg30"] == 100 and d1["stats"]["outlier_days"] == 1
      and [h["outlier"] for h in d1["history"]].count(True) == 1
      and [h for h in d1["history"] if h["outlier"]][0]["low"] == 5000
      and d1["stats"]["days_of_data"] == 30, d1["stats"])
check("item current confidence/flag",
      d1["current"]["confidence"] == "high" and d1["current"]["price_flag"] is None, d1["current"])

# thin_high / thin_low
check("price_flag", analytics.price_flag(ctx4, 8002) == "thin_high" and analytics.price_flag(ctx4, 8003) == "thin_low"
      and analytics.price_flag(ctx4, 8004) is None and analytics.price_flag(ctx4, 8001) is None)
check("buy_price thin_high -> baseline", analytics.buy_price(ctx4, 8002) == (1000, "ah")
      and analytics.buy_price(ctx4, 8002, "baseline") == (1000, "ah")
      and analytics.buy_price(ctx4, 8003) == (300, "ah") and analytics.buy_price(ctx4, 2320) == (10, "vendor")
      and analytics.buy_price(ctx4, 3371) == (50, "vendor") and analytics.buy_price(ctx4, 424242) == (None, "none"))
check("sell_price thin_high -> baseline", analytics.sell_price(ctx4, 8002) == 1000)
check("sell_price capped at 1.25x baseline", analytics.sell_price(ctx4, 8004) == 1250
      and analytics.sell_price(ctx4, 8004, "baseline") == 1000 and analytics.sell_price(ctx4, 8001) == 100)
check("sell_price no baseline -> current", analytics.sell_price(ctx4, 284712) == 900)
prof4 = {x["spell_id"]: x for x in j("/api/professions/4")}
r9101 = prof4[9101]
# hand: cost 2*100=200; sell = baseline 1000 (NOT the thin 5000); profit 1000*.95-200 = 750
check("recipe uses sell_price (thin_high)", r9101["sell"] == 1000 and r9101["cost"] == 200
      and abs(r9101["profit"] - 750) < 1e-6 and r9101["sell_price_flag"] == "thin_high"
      and r9101["sell_confidence"] == "low" and r9101["confidence"] == "low", r9101)
r9102 = prof4[9102]
check("recipe sell capped", r9102["sell"] == 1250 and abs(r9102["profit"] - (1250 * .95 - 100)) < 1e-6
      and r9102["confidence"] == "high" and r9102["reagents"][0]["confidence"] == "high"
      and r9102["reagents"][0]["price_flag"] is None, r9102)
check("recipe has trend fields", all(k in r9102 for k in ("spark", "profit_trend_pct", "confidence", "sell_confidence")))
rv = {x["spell_id"]: x for x in j("/api/professions/8")}[2385]
check("vendor reagent confidence", {g["item_id"]: g["confidence"] for g in rv["reagents"]}[2320] == "high")
pf = next(x for x in j("/api/professions") if x["id"] == 4)
check("professions reliable fields", pf["best_profit_reliable"] is not None
      and pf["best_profit_reliable"] <= pf["best_profit"] and pf["reliable_count"] <= pf["priced_count"])
check("item crafted_by has new fields", "spark" in j("/api/item/8002")["crafted_by"][0]
      and j("/api/item/8002")["crafted_by"][0]["sell"] == 1000)

# confidence levels
check("confidence high", analytics.confidence(ctx4, 8001) == "high" and analytics.confidence(ctx4, 8003) == "high")
check("confidence medium (low qty)", analytics.confidence(ctx4, 8005) == "medium")
check("confidence low (few days)", analytics.confidence(ctx4, 8006) == "low")
check("confidence low (thin_high)", analytics.confidence(ctx4, 8002) == "low")
check("confidence low (avg qty < 2)", analytics.confidence(ctx4, 8010) == "low")
check("search rows have confidence/flag", {x["id"]: (x["confidence"], x["price_flag"])
                                           for x in j("/api/items/search?q=Thin%20Gem")}[8002] == ("low", "thin_high"))

# deals: low-confidence below_baseline hidden unless include_low
dl4 = {x["item_id"]: x for x in j("/api/deals")}
dl4l = {x["item_id"]: x for x in j("/api/deals?include_low=1")}
check("deals hide low confidence", 8010 not in dl4 and 8010 in dl4l, sorted(dl4l))
check("deals fields", dl4l[8010]["confidence"] == "low" and dl4l[8010]["price_flag"] == "thin_low"
      and 8003 in dl4 and dl4[8003]["confidence"] == "high" and dl4[8003]["price_flag"] == "thin_low")

# profit trend
el = [500 + 10 * k for k in range(30)]
prof = [e * (1 - CUT) - 100 for e in el]
a3 = sum(prof[-3:]) / 3
a7_10 = sum(prof[-10:-3]) / 7
pct = (a3 - a7_10) / abs(a7_10) * 100
t = j("/api/recipe/9103/trend?days=30")
check("trend endpoint", t["spell_id"] == 9103 and t["name"] == "Trend Elixir" and len(t["points"]) == 30
      and t["points"][-1]["date"] == "2026-11-16" and t["points"][-1]["cost"] == 100
      and t["points"][-1]["sell"] == 790 and abs(t["points"][-1]["profit"] - prof[-1]) < 0.01
      and abs(t["points"][0]["profit"] - prof[0]) < 0.01
      and abs(t["points"][-1]["margin"] - prof[-1] / 100) < 1e-3
      and abs(t["profit_trend_pct"] - pct) < 0.06 and abs(t["avg_profit_7"] - sum(prof[-7:]) / 7) < 0.06
      and abs(t["avg_profit_30"] - sum(prof) / 30) < 0.06, t["points"][-1])
check("trend days param", len(j("/api/recipe/9103/trend?days=10")["points"]) == 10)
r9103 = prof4[9103]
check("recipe spark", len(r9103["spark"]) == 14 and abs(r9103["spark"][-1] - prof[-1]) < 0.06
      and abs(r9103["spark"][0] - prof[-14]) < 0.06 and abs(r9103["profit_trend_pct"] - pct) < 0.06
      and r9103["profit_trend_pct"] > 0, r9103["spark"])
check("falling trend negative", prof4[9104]["profit_trend_pct"] < 0)
t2 = j("/api/recipe/7418/trend")  # enchant: no product -> all profit null
check("trend no product", all(p["profit"] is None and p["sell"] is None for p in t2["points"])
      and t2["profit_trend_pct"] is None)
check("trend 404", get("/api/recipe/1/trend")[0] == 404)
# a spike in an input (8001, 5 days ago) does not poison the trend of recipes using it
t3 = j("/api/recipe/9102/trend")
check("trend ignores outlier day", all(p["cost"] == 100 for p in t3["points"]),
      [p["cost"] for p in t3["points"] if p["cost"] != 100])

# trending crafts
tc = j("/api/trending-crafts?limit=20")
check("trending up/down", 9103 in [r["spell_id"] for r in tc["up"]] and 9104 in [r["spell_id"] for r in tc["down"]]
      and [r["profit_trend_pct"] for r in tc["up"]] == sorted((r["profit_trend_pct"] for r in tc["up"]), reverse=True)
      and all(r["profit_trend_pct"] > 0 for r in tc["up"]) and all(r["profit_trend_pct"] < 0 for r in tc["down"])
      and "profession_name" in tc["up"][0], [(r["spell_id"], r["profit_trend_pct"]) for r in tc["up"]])
check("trending limit", len(j("/api/trending-crafts?limit=1")["up"]) == 1)
ok("PUT", "/api/myprofessions", [])
check("trending mine=1 empty without professions", j("/api/trending-crafts?mine=1") == {"up": [], "down": []})
ok("PUT", "/api/myprofessions", [{"profession_id": 4, "skill": 300}])
tm = j("/api/trending-crafts?mine=1")
check("trending mine=1", 9103 in [r["spell_id"] for r in tm["up"]] and all(r["can_craft"] for r in tm["up"] + tm["down"]))
ok("PUT", "/api/myprofessions", [])
ok("PUT", "/api/shopping/9103", {"qty": 2})
shp = ok("GET", "/api/shopping")
check("shopping recipe spark/confidence", shp["recipes"][0]["spark"] == r9103["spark"]
      and shp["recipes"][0]["confidence"] == "high" and "confidence" in shp["reagents"][0], shp["recipes"])
ok("DELETE", "/api/shopping")

# leveling routes (module owned by the leveling agent)
try:
    import ahtool.leveling  # noqa: F401
    have_lv = True
except ImportError:
    have_lv = False
st, body = req("GET", "/api/leveling/4?start=1&target=100")
if have_lv:
    check("leveling route", st == 200 and "steps" in body and body["profession_id"] == 4, (st, body))
    st, body = req("POST", "/api/leveling/4/to-shopping?start=1&target=100")
    check("leveling to-shopping", st == 200 and "added" in body, (st, body))
    ok("DELETE", "/api/shopping")
else:
    check("leveling route 501 when module missing", st == 501 and "error" in body, (st, body))
    check("leveling to-shopping 501", req("POST", "/api/leveling/4/to-shopping")[0] == 501)

# gathering professions: what can be gathered, resolved by item name
g = ok("GET", "/api/professions/5/gather")
check("herbalism gather list resolves Bruiseweed", any(r["name"] == "Bruiseweed" and r["skill"] == 100 and r["where"] for r in g), g)
check("gather rows carry prices", all("price" in r and "baseline" in r for r in g))
check("non-gathering profession has no gather list", ok("GET", "/api/professions/4/gather") == [])
pl = {p["id"]: p for p in ok("GET", "/api/professions")}
check("gather_count counts resolved items", pl[5]["gather_count"] == len(g) and pl[4]["gather_count"] == 0, pl[5])

# clean up user state
ok("PUT", "/api/myprofessions", [])
check("myprofessions cleared", ok("GET", "/api/myprofessions") == [])
cx.close()

print("\n%d failure(s)" % len(fails))
srv.shutdown()
sys.exit(1 if fails else 0)
