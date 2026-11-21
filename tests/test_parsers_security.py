"""Hostile-input hardening for the local-file parsers and market math. Run: python tests/test_parsers_security.py"""
import os
import sys
import tempfile
import time
import warnings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ahtool import cbor, db, luaparse, market, staticdata, tooltipdata  # noqa: E402

warnings.simplefilter("ignore", SyntaxWarning)  # some rejected formulas are odd Python
fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print("FAIL:", msg)


def raises(fn, exc):
    try:
        fn()
    except exc:
        return True
    except Exception as e:  # wrong exception type
        print("  unexpected %r" % e)
        return False
    return False


# ---- luaparse: nesting depth
ok = luaparse.parse(b"X=" + b"{" * 150 + b"}" * 150)
check(isinstance(ok.get("X"), list), "150 nested tables parse")
deep = b"X=" + b"{" * 100000 + b"}" * 100000
check(raises(lambda: luaparse.parse(deep), luaparse.LuaParseError), "deep Lua nesting -> LuaParseError")
deep_keys = b"X=" + b"{[" * 5000
check(raises(lambda: luaparse.parse(deep_keys), luaparse.LuaParseError), "deep Lua key nesting -> LuaParseError")

# ---- luaparse: non-finite / oversized numbers
for lit in (b"1e999", b"-1e999", b"{1e999}", b"{[\"a\"]=2e400}"):
    check(raises(lambda: luaparse.parse(b"X=" + lit), luaparse.LuaParseError), "%r -> LuaParseError" % lit)
check(raises(lambda: luaparse.parse(b"X=" + b"9" * 10000), luaparse.LuaParseError), "huge int literal rejected")
v = luaparse.parse(b'X={a=1.5,b=-3,c=0x1F,d=-inf,e="s"}')["X"]
check(v == {"a": 1.5, "b": -3, "c": 31, "d": float("-inf"), "e": b"s"}, "normal values unchanged %r" % v)

# ---- cbor: nesting depth and malformed input all raise CBORError
check(cbor.loads(b"\x81" * 100 + b"\x01") is not None, "100 nested arrays decode")
check(raises(lambda: cbor.loads(b"\x81" * 100000 + b"\x01"), cbor.CBORError), "deep arrays -> CBORError")
check(raises(lambda: cbor.loads(b"\x9f" * 100000), cbor.CBORError), "deep indefinite arrays -> CBORError")
check(raises(lambda: cbor.loads(b"\xc6" * 100000 + b"\x01"), cbor.CBORError), "deep tags -> CBORError")
check(raises(lambda: cbor.loads(b"\xa1" * 100000), cbor.CBORError), "deep maps -> CBORError")
# indefinite text string whose chunk is an int / a nested indefinite string
check(raises(lambda: cbor.loads(b"\x7f\x01\xff"), cbor.CBORError), "non-string chunk -> CBORError")
check(raises(lambda: cbor.loads(b"\x7f\x7f\xff\xff"), cbor.CBORError), "indefinite chunk -> CBORError")
check(cbor.loads(b"\x7f\x62ab\x61c\xff") == "abc", "valid indefinite string still decodes")
check(raises(lambda: cbor.loads(b"\xfb\x00\x00"), cbor.CBORError), "truncated float -> CBORError")
check(raises(lambda: cbor.loads(b"\x82\x01"), cbor.CBORError), "truncated array -> CBORError")
check(cbor.loads(b"\xa1\x61k\x83\x01\x02\x03") == {"k": [1, 2, 3]}, "normal map decodes")

# ---- market.parse_rows: N* multiplier cap
row = "2589,0,20,2000,1000,0,4,Alice"
check(len(market.parse_rows("3*" + row)) == 3, "3* row expands to 3")
check(len(market.parse_rows("%d*%s" % (market.MAX_ROW_MULT, row))) == market.MAX_ROW_MULT, "cap value allowed")
t0 = time.time()
r = market.parse_rows("999999999999*" + row + ";" + row)
check(len(r) == 1 and time.time() - t0 < 1, "huge multiplier row skipped, rest kept (%d rows)" % len(r))
check(market.parse_rows("0*" + row) == [] and market.parse_rows("-5*" + row) == [], "zero/negative mult skipped")
check(market.parse_rows("2589,0,20,%d,1000,0,4,A" % (2 ** 70)) == [], "out-of-range buyout skipped")
check(market._i(float("inf")) == 0 and market._i(float("-inf"), None) is None, "_i(inf) -> default")
check(market._i(float("nan")) == 0 and market._i(2 ** 70) == 0 and market._i("12") == 12, "_i nan/huge/str")

# ---- staticdata: non-finite vendor prices
tmp = tempfile.mkdtemp()
conn = db.connect(os.path.join(tmp, "t.db"))
n = staticdata.update_vendor_from_cache(conn, {"1": float("inf"), "2": float("nan"), "3": 2 ** 70, "4": 50,
                                               "5": 12.5, "x": 3, str(2 ** 70): 4})
check(n == 2, "vendor cache keeps only finite in-range prices (%d)" % n)
check(staticdata._int(float("inf")) == 0 and staticdata._int("1e999") == 0, "staticdata._int(inf) -> default")

# ---- market: auctions with count=0 must not divide by zero
market.ensure_schema(conn)
now = int(time.time())
conn.execute("INSERT INTO ah_scans(ts,realm,faction,char,source,total,n,partial) VALUES(?,?,?,?,?,?,?,?)",
              (now - 60, "R", "A", "Me", "addon", 3, 3, 0))
sid = conn.execute("SELECT MAX(id) FROM ah_scans").fetchone()[0]
conn.executemany("INSERT INTO ah_auctions(scan_id,item_id,suffix,count,buyout,min_bid,bid,time_left,owner) "
                 "VALUES(?,?,?,?,?,?,?,?,?)",
                 [(sid, 77, 0, 0, 5000, 100, 0, 4, "Zero"), (sid, 77, 0, 2, 400, 100, 0, 4, "Two")])
conn.execute("INSERT INTO my_auctions(char,item_id,count,buyout) VALUES('Me',77,1,300)")
conn.commit()
cfg = {"logger_savedvariables": "", "ah_cut": 0.05, "addons_dir": "", "logger_max_age_h": 24}
try:
    m = market.item_market(conn, cfg, 77, qty=2)
    check(m["cost"] and m["cost"]["bought"] == 2 and m["cost"]["total"] == 400, "count=0 ignored in cost %s" % m["cost"])
except ZeroDivisionError:
    check(False, "item_market count=0 -> ZeroDivisionError")
try:
    _, costs = market.cost_for(conn, cfg, {77: 1})
    check(costs[77]["bought"] == 2, "cost_for skips count=0 %s" % costs)
except ZeroDivisionError:
    check(False, "cost_for count=0 -> ZeroDivisionError")
try:
    market.my_market(conn, cfg)
except ZeroDivisionError:
    check(False, "my_market count=0 -> ZeroDivisionError")
conn.close()

# ---- tooltipdata: safe formula evaluation (no eval, no **)
ev = tooltipdata._arith
check(ev("(1+2)*3/4") == 2.25 and ev("10 - -2") == 12 and ev("7//2") == 3 and ev(" 5") == 5, "basic arithmetic")
check(ev("100.5/2") == 50.25 and ev("-(3)") == -3, "floats / unary minus")
for bad in ("9**9**9**9", "2**3", "__import__('os')", "(1,2)", "1if 1else 2", "x", "1e400*1e400"):
    check(raises(lambda: ev(bad), (ValueError, SyntaxError, TypeError)), "rejected: %r" % bad)
check(raises(lambda: ev("1" + "*99999999" * 10), ValueError), "runaway product rejected")
check(raises(lambda: ev("(" * 400 + "1" + ")" * 400), Exception), "deep parens rejected without crash")
sdb = tooltipdata.SpellDB.__new__(tooltipdata.SpellDB)
sdb.numeric = lambda kind, sid, idx: 10.0
check(sdb._eval("$s1*2+1", 1) == 21.0, "_eval with tokens")
t0 = time.time()
check(sdb._eval("9**9**9**9", 1) is None and sdb._eval("$s1**$s1**$s1**$s1", 1) is None, "_eval rejects **")
check(time.time() - t0 < 1, "power tower rejected quickly")
check(sdb._eval("$s1/0", 1) is None, "division by zero -> None")

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
