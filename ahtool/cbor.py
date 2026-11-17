"""Minimal full CBOR decoder. Text and byte strings both decode to str (utf-8, errors='replace')."""
import struct


class CBORError(ValueError):
    pass


_BREAK = object()


def loads(data):
    val, _ = _dec(data, 0)
    if val is _BREAK:
        raise CBORError("unexpected break")
    return val


def decode(data, pos=0):
    """Returns (value, new_pos)."""
    return _dec(data, pos)


def _arg(b, p, ai):
    if ai < 24:
        return ai, p
    if ai == 24:
        return b[p], p + 1
    if ai == 25:
        return (b[p] << 8) | b[p + 1], p + 2
    if ai == 26:
        return int.from_bytes(b[p:p + 4], "big"), p + 4
    if ai == 27:
        return int.from_bytes(b[p:p + 8], "big"), p + 8
    raise CBORError("bad additional info %d" % ai)


def _hashable(k):
    if isinstance(k, list):
        return tuple(_hashable(x) for x in k)
    if isinstance(k, dict):
        return tuple((_hashable(a), _hashable(c)) for a, c in k.items())
    return k


def _dec(b, p):
    try:
        return _dec2(b, p)
    except IndexError:
        raise CBORError("truncated")


def _dec2(b, p):
    ib = b[p]
    p += 1
    mt = ib >> 5
    ai = ib & 31
    if mt == 7:
        if ai < 20:
            return ai, p
        if ai == 20:
            return False, p
        if ai == 21:
            return True, p
        if ai in (22, 23):
            return None, p
        if ai == 24:
            return b[p], p + 1
        if ai == 25:
            return struct.unpack(">e", bytes(b[p:p + 2]))[0], p + 2
        if ai == 26:
            return struct.unpack(">f", bytes(b[p:p + 4]))[0], p + 4
        if ai == 27:
            return struct.unpack(">d", bytes(b[p:p + 8]))[0], p + 8
        if ai == 31:
            return _BREAK, p
        raise CBORError("bad simple value")
    if ai == 31:  # indefinite length
        if mt in (2, 3):
            chunks = []
            while b[p] != 0xFF:
                v, p = _dec2(b, p)
                chunks.append(v)
            return "".join(chunks), p + 1
        if mt == 4:
            r = []
            while b[p] != 0xFF:
                v, p = _dec2(b, p)
                r.append(v)
            return r, p + 1
        if mt == 5:
            r = {}
            while b[p] != 0xFF:
                k, p = _dec2(b, p)
                v, p = _dec2(b, p)
                r[_hashable(k)] = v
            return r, p + 1
        raise CBORError("bad indefinite length")
    n, p = _arg(b, p, ai)
    if mt == 0:
        return n, p
    if mt == 1:
        return -1 - n, p
    if mt in (2, 3):
        if p + n > len(b):
            raise CBORError("truncated string")
        return bytes(b[p:p + n]).decode("utf-8", "replace"), p + n
    if mt == 4:
        r = []
        for _ in range(n):
            v, p = _dec2(b, p)
            r.append(v)
        return r, p
    if mt == 5:
        r = {}
        for _ in range(n):
            k, p = _dec2(b, p)
            v, p = _dec2(b, p)
            r[_hashable(k)] = v
        return r, p
    return _dec2(b, p)  # tag (major 6): ignored
