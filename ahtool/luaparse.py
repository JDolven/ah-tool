"""Byte-level parser for WoW SavedVariables Lua files.

parse(data: bytes) -> {NAME: value}
Values: dict (keyed or mixed; positional entries get int keys 1..n) / list (purely positional table)
/ {} (empty table) / int / float / bool / None / bytes (all strings: raw bytes, so CBOR blobs stay intact).
Table string keys are returned as str (utf-8, errors='replace'); numeric keys as int.
"""
import math
import re

_SPACE = re.compile(rb"[ \t\r\n]*")
_LONG_OPEN = re.compile(rb"\[(=*)\[")
_NAME = re.compile(rb"[A-Za-z_][A-Za-z0-9_]*")
_NUM = re.compile(rb"-?(?:0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)")
_INT = re.compile(rb"-?\d+")
_DQ = re.compile(rb'"((?:[^"\\]|\\.)*)"', re.S)
_SQ = re.compile(rb"'((?:[^'\\]|\\.)*)'", re.S)
_LONG = re.compile(rb"\[(=*)\[\n?(.*?)\]\1\]", re.S)
_ESC = re.compile(rb"\\(\d{1,3}|x[0-9a-fA-F]{2}|.)", re.S)
_SIMPLE = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"a": b"\x07", b"b": b"\x08", b"f": b"\x0c",
           b"v": b"\x0b", b"\\": b"\\", b'"': b'"', b"'": b"'", b"\n": b"\n", b"\r": b"\r"}


class LuaParseError(ValueError):
    pass


MAX_DEPTH = 200  # table nesting limit (deeper input would hit Python's recursion limit)


def _unesc_fn(m):
    g = m.group(1)
    if g[:1].isdigit():
        return bytes([int(g) & 255])
    if g[:1] == b"x" and len(g) == 3:
        return bytes([int(g[1:], 16)])
    return _SIMPLE.get(g, g)


def _unescape(s):
    return _ESC.sub(_unesc_fn, s) if b"\\" in s else s


class _P:
    def __init__(self, data):
        self.d = data
        self.n = len(data)

    def ws(self, p):
        """Skip whitespace and comments in linear time (the old regex rescanned to EOF for every unterminated
        `--[[`, which is quadratic). An unterminated long comment is an error, as in Lua."""
        d, n = self.d, self.n
        while True:
            p = _SPACE.match(d, p).end()
            if d[p:p + 2] != b"--":
                return p
            m = _LONG_OPEN.match(d, p + 2)
            if m:
                close = b"]" + m.group(1) + b"]"
                e = d.find(close, m.end())
                if e < 0:  # Lua rejects this too; treating it as a line comment would rescan to EOF each time
                    raise LuaParseError("unterminated long comment at %d" % p)
                p = e + len(close)
            else:
                e = d.find(b"\n", p + 2)
                p = n if e < 0 else e + 1

    def value(self, p, depth=0):
        d = self.d
        p = self.ws(p)
        if p >= self.n:
            raise LuaParseError("unexpected EOF")
        c = d[p]
        if c == 0x7B:  # {
            if depth >= MAX_DEPTH:
                raise LuaParseError("tables nested too deeply at %d" % p)
            return self.table(p + 1, depth + 1)
        if c == 0x22:
            m = _DQ.match(d, p)
            if not m:
                raise LuaParseError("bad string at %d" % p)
            return _unescape(m.group(1)), m.end()
        if c == 0x27:
            m = _SQ.match(d, p)
            if not m:
                raise LuaParseError("bad string at %d" % p)
            return _unescape(m.group(1)), m.end()
        if c == 0x5B:
            m = _LONG.match(d, p)
            if m:
                return m.group(2), m.end()
        m = _NUM.match(d, p)
        if m:
            t = m.group()
            if b"x" in t or b"X" in t:
                return int(t, 16), m.end()
            try:
                if _INT.fullmatch(t):
                    return int(t), m.end()
                f = float(t)
            except ValueError:  # e.g. an integer literal beyond int_max_str_digits
                raise LuaParseError("bad number at %d" % p)
            if not math.isfinite(f):  # 1e999 etc.: WoW never writes these; int() on them would overflow
                raise LuaParseError("number out of range at %d" % p)
            return f, m.end()
        m = _NAME.match(d, p)
        if m:
            t = m.group()
            if t == b"true":
                return True, m.end()
            if t == b"false":
                return False, m.end()
            if t == b"nil":
                return None, m.end()
        if d[p:p + 4] == b"-inf":  # kept for compatibility (WoW serializes -math.huge so); ingest range-checks
            return float("-inf"), p + 4
        raise LuaParseError("unexpected %r at %d" % (d[p:p + 20], p))

    def table(self, p, depth=1):
        d = self.d
        keyed = {}
        pos = []
        while True:
            p = self.ws(p)
            c = d[p:p + 1]
            if c == b"}":
                p += 1
                break
            if c == b"":
                raise LuaParseError("unterminated table")
            if c == b"[" and not _LONG.match(d, p):
                k, p = self.value(p + 1, depth)
                p = self.ws(p)
                if d[p:p + 1] != b"]":
                    raise LuaParseError("expected ] at %d" % p)
                p = self.ws(p + 1)
                if d[p:p + 1] != b"=":
                    raise LuaParseError("expected = at %d" % p)
                v, p = self.value(p + 1, depth)
                if isinstance(k, bytes):
                    k = k.decode("utf-8", "replace")
                elif isinstance(k, float) and k.is_integer():
                    k = int(k)
                keyed[k] = v
            else:
                m = _NAME.match(d, p)
                q = self.ws(m.end()) if m else 0
                if m and d[q:q + 1] == b"=" and d[q + 1:q + 2] != b"=" and \
                        m.group() not in (b"true", b"false", b"nil"):
                    v, p = self.value(q + 1, depth)
                    keyed[m.group().decode("utf-8", "replace")] = v
                else:
                    v, p = self.value(p, depth)
                    pos.append(v)
            p = self.ws(p)
            if d[p:p + 1] in (b",", b";"):
                p += 1
        if not keyed:
            return (pos if pos else {}), p
        for i, v in enumerate(pos, 1):
            keyed.setdefault(i, v)
        return keyed, p

    def top(self):
        out = {}
        p = self.ws(0)
        while p < self.n:
            m = _NAME.match(self.d, p)
            if not m:
                raise LuaParseError("expected name at %d" % p)
            p = self.ws(m.end())
            if self.d[p:p + 1] != b"=":
                raise LuaParseError("expected = at %d" % p)
            v, p = self.value(p + 1)
            out[m.group().decode()] = v
            p = self.ws(p)
            if self.d[p:p + 1] == b";":
                p = self.ws(p + 1)
        return out


def parse(data):
    return _P(data).top()


def parse_file(path):
    with open(path, "rb") as f:
        return parse(f.read())
