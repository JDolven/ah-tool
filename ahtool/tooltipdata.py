"""Static item-tooltip data (built once, stored in SQLite table `item_tooltip`).

Sources, lowest to highest precedence:
  1. wago.tools CSVs (build cfg["wago_build"]): ItemSparse, Item, ItemEffect, Spell, SpellEffect, SpellMisc,
     SpellDuration, SpellAuraOptions, SpellRadius, SpellName, ManifestInterfaceData (icons).
  2. Polestar_ItemDB/Items.lua  (Blizzard Era tables, newer build) - numeric overlay.
  3. Client hotfix cache DBCache.bin (what the Forever server sends): ItemSparse/Item header fields
     (name, quality, item level, required level, slot, class, bonding, unique, sell price, speed, icon).
  4. Polestar_ItemDB/Forever.lua (Forever stats) - wins over everything.

Each row stores a JSON "static tooltip": {name, quality, icon, item_level, required_level, bind, unique, slot,
type, damage, armor, stats, resistances, durability, classes, skill, effects, description, sell_price, ...}.
The API (ahtool.tooltip) turns it into display lines and adds the AH section.
"""
import csv
import json
import os
import re
import struct
import sys

csv.field_size_limit(10 ** 8)

VERSION = "4"
ICON_URL = "https://wow.zamimg.com/images/wow/icons/medium/%s.jpg"
FALLBACK_ICON = "inv_misc_questionmark"

SLOTS = {1: "Head", 2: "Neck", 3: "Shoulder", 4: "Shirt", 5: "Chest", 6: "Waist", 7: "Legs", 8: "Feet",
         9: "Wrist", 10: "Hands", 11: "Finger", 12: "Trinket", 13: "One-Hand", 14: "Off Hand", 15: "Ranged",
         16: "Back", 17: "Two-Hand", 18: "Bag", 19: "Tabard", 20: "Chest", 21: "Main Hand", 22: "Off Hand",
         23: "Held In Off-hand", 24: "Projectile", 25: "Thrown", 26: "Ranged", 27: "Quiver", 28: "Relic"}
WEAPON_TYPES = {0: "Axe", 1: "Axe", 2: "Bow", 3: "Gun", 4: "Mace", 5: "Mace", 6: "Polearm", 7: "Sword",
                8: "Sword", 10: "Staff", 13: "Fist Weapon", 14: "Miscellaneous", 15: "Dagger", 16: "Thrown",
                17: "Spear", 18: "Crossbow", 19: "Wand", 20: "Fishing Pole"}
ARMOR_TYPES = {1: "Cloth", 2: "Leather", 3: "Mail", 4: "Plate", 6: "Shield", 7: "Libram", 8: "Idol", 9: "Totem"}
PROJECTILE_TYPES = {2: "Arrow", 3: "Bullet"}
SCHOOLS = {0: "", 1: "Holy", 2: "Fire", 3: "Nature", 4: "Frost", 5: "Shadow", 6: "Arcane"}
STATS = {0: "Mana", 1: "Health", 3: "Agility", 4: "Strength", 5: "Intellect", 6: "Spirit", 7: "Stamina"}
BONDING = {1: "Binds when picked up", 2: "Binds when equipped", 3: "Binds when used", 4: "Quest Item"}
CLASSES = [(1, "Warrior"), (2, "Paladin"), (3, "Hunter"), (4, "Rogue"), (5, "Priest"), (7, "Shaman"),
           (8, "Mage"), (9, "Warlock"), (11, "Druid")]
ALL_CLASS_MASK = sum(1 << (c - 1) for c, _ in CLASSES)
SKILLS = {164: "Blacksmithing", 165: "Leatherworking", 171: "Alchemy", 182: "Herbalism", 185: "Cooking",
          186: "Mining", 197: "Tailoring", 202: "Engineering", 333: "Enchanting", 356: "Fishing",
          393: "Skinning", 129: "First Aid", 40: "Poisons", 762: "Riding", 633: "Lockpicking"}
TRIGGERS = {0: "Use", 1: "Equip", 2: "Chance on hit", 4: "Use", 5: "Use", 6: "Use"}

# Polestar tokens that map to plain white stats / green equip lines.
TOKEN_STATS = {"str": "Strength", "agi": "Agility", "sta": "Stamina", "int": "Intellect", "spi": "Spirit"}
SCHOOL_TOKENS = {"spHoly": "Holy", "spFire": "Fire", "spNature": "Nature", "spFrost": "Frost",
                 "spShadow": "Shadow", "spArcane": "Arcane"}
# Equip auras that Polestar tokens describe (spell aura id -> token family). Equip spells made only of these
# are replaced by token-generated lines when an overlay exists, so changed Forever values show correctly.
STAT_AURAS = {13, 135, 52, 54, 55, 57, 71, 99, 124, 85, 84, 49, 47, 51, 158}
AURA_DEFENSE_SKILL = 30  # with misc value 95


def log(*a):
    print("[tooltipdata]", *a, file=sys.stderr)


def _int(s, default=0):
    try:
        return int(s)
    except (TypeError, ValueError):
        try:
            return int(float(s))
        except (TypeError, ValueError):
            return default


def _float(s, default=0.0):
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def _rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        yield from csv.DictReader(f)


# ---------------------------------------------------------------- Polestar_ItemDB (Lua)

_ENTRY = re.compile(r'^\[(\d+)\]="([^"]*)"', re.M)


def parse_polestar_entry(s):
    """'<inv> <ilvl> <req> <quality> <class> <subclass> [tokens]' -> dict (None if malformed)."""
    parts = s.split()
    if len(parts) < 6:
        return None
    try:
        head = [int(x) for x in parts[:6]]
    except ValueError:
        return None
    d = {"inv": head[0], "ilvl": head[1], "req": head[2], "quality": head[3], "class": head[4],
         "subclass": head[5], "tok": {}}
    for t in parts[6:]:
        m = re.fullmatch(r"([A-Za-z]+)(-?\d+)?", t)
        if m:
            d["tok"][m.group(1)] = int(m.group(2)) if m.group(2) is not None else 1
    return d


def read_polestar(path):
    """Parse the item block (first DB.Register({...}) table) of Items.lua / Forever.lua -> {id: entry}."""
    if not path or not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    a = text.find("DB.Register(")
    if a < 0:
        return {}
    b = text.find("\n})", a)
    block = text[a:b if b > 0 else len(text)]
    out = {}
    for m in _ENTRY.finditer(block):
        e = parse_polestar_entry(m.group(2))
        if e:
            out[int(m.group(1))] = e
    return out


def read_forever_names(path):
    """Forever.lua leveling `names = { [id]="Name", ... }` table (names of Forever items)."""
    if not path or not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    a = text.find("\nnames = {")
    if a < 0:
        return {}
    b = text.find("\n}", a)
    return {int(m.group(1)): m.group(2) for m in _ENTRY.finditer(text[a:b])}


# ---------------------------------------------------------------- DBCache.bin hotfixes

def read_dbcache_hotfixes(path):
    """Item (0x50238EC2) and ItemSparse (0x919BE54E) hotfix records -> {id: dict}.

    Layouts verified against the CSV for ~5000 unchanged items: Item = int32 class, u8 subclass, u8 material,
    u8 inventoryType, u8 sheathe, 4 bytes, i8 soundOverride, int32 icon. ItemSparse = 5 cstrings then a fixed
    302-byte block (Stackable@156, MaxCount@160, SellPrice@180, ItemDelay u16@272, ItemLevel u16@280,
    AllowableClass i16@282, Bonding@293, InventoryType@299, Quality@300; RequiredLevel@298).
    Armor, damage and stat amounts are not stored there (the client computes them), so they are not used."""
    out = {}
    if not path or not os.path.exists(path):
        return out
    try:
        with open(path, "rb") as f:
            d = f.read()
    except OSError:
        return out
    p, n = 44, len(d)
    while p + 32 <= n:
        if d[p:p + 4] != b"XFTH":
            break
        th, rid, size, st = struct.unpack_from("<IIIB", d, p + 16)
        rec = d[p + 32:p + 32 + size]
        p += 32 + size
        if st != 1 or not size:
            continue
        try:
            if th == 0x50238EC2 and size >= 17:
                e = out.setdefault(rid, {})
                e["class"] = struct.unpack_from("<i", rec, 0)[0]
                e["subclass"] = rec[4]
                e["inv"] = rec[6]
                icon = struct.unpack_from("<i", rec, 13)[0]
                if icon > 0:
                    e["icon"] = icon
            elif th == 0x919BE54E:
                q = 0
                strs = []
                for _ in range(5):
                    z = rec.index(b"\0", q)
                    strs.append(rec[q:z].decode("utf-8", "replace"))
                    q = z + 1
                b = rec[q:]
                if len(b) != 302:
                    continue
                e = out.setdefault(rid, {})
                e["name"] = strs[4] or None
                e["description"] = strs[0] or None
                e["maxcount"] = struct.unpack_from("<i", b, 160)[0]
                e["sell"] = struct.unpack_from("<i", b, 180)[0]
                e["delay"] = struct.unpack_from("<H", b, 272)[0]
                e["ilvl"] = struct.unpack_from("<H", b, 280)[0]
                e["allowable_class"] = struct.unpack_from("<h", b, 282)[0]
                e["bonding"] = b[293]
                e["req"] = b[298]
                e["sparse_inv"] = b[299]
                e["quality"] = b[300]
        except (ValueError, struct.error):
            continue
    return out


# ---------------------------------------------------------------- spell descriptions

def _fmt_num(v):
    v = abs(v)
    if abs(v - round(v)) < 1e-6:
        return str(int(round(v)))
    return ("%.1f" % v).rstrip("0").rstrip(".")


def fmt_duration(ms):
    s = ms / 1000.0
    if s < 60:
        return "%s sec" % _fmt_num(s)
    if s < 3600:
        return "%s min" % _fmt_num(s / 60)
    h = s / 3600
    return "%s hour%s" % (_fmt_num(h), "" if abs(h - 1) < 1e-6 else "s")


def fmt_cooldown(ms):
    s = ms / 1000.0
    if s < 60:
        return "%s Sec Cooldown" % _fmt_num(s)
    if s < 3600:
        return "%s Min Cooldown" % _fmt_num(s / 60)
    h = s / 3600
    return "%s Hour%s Cooldown" % (_fmt_num(h), "" if abs(h - 1) < 1e-6 else "s")


class SpellDB:
    def __init__(self, cache_csv):
        self.desc = {}
        for r in _rows(cache_csv("Spell")):
            d = r.get("Description_lang") or ""
            if d:
                self.desc[_int(r["ID"])] = d
        self.name = {}
        for r in _rows(cache_csv("SpellName")):
            self.name[_int(r["ID"])] = r.get("Name_lang") or ""
        self.eff = {}
        for r in _rows(cache_csv("SpellEffect")):
            if _int(r.get("DifficultyID")) != 0:
                continue
            sid = _int(r["SpellID"])
            self.eff.setdefault(sid, {})[_int(r["EffectIndex"])] = (
                _int(r["EffectBasePoints"]), _int(r["EffectDieSides"]), _int(r["EffectAuraPeriod"]),
                _int(r["EffectChainTargets"]), _int(r.get("EffectRadiusIndex_0")), _int(r["Effect"]),
                _int(r["EffectAura"]), _int(r.get("EffectMiscValue_0")), _int(r["EffectTriggerSpell"]))
        durs = {_int(r["ID"]): _int(r["Duration"]) for r in _rows(cache_csv("SpellDuration"))}
        self.dur = {}
        for r in _rows(cache_csv("SpellMisc")):
            if _int(r.get("DifficultyID")) != 0:
                continue
            di = _int(r["DurationIndex"])
            if di and durs.get(di, 0) > 0:
                self.dur[_int(r["SpellID"])] = durs[di]
        self.aura = {}
        for r in _rows(cache_csv("SpellAuraOptions")):
            if _int(r.get("DifficultyID")) != 0:
                continue
            self.aura[_int(r["SpellID"])] = (_int(r["ProcChance"]), _int(r["CumulativeAura"]),
                                             _int(r["ProcCharges"]))
        try:
            self.radius = {_int(r["ID"]): _float(r["Radius"]) for r in _rows(cache_csv("SpellRadius"))}
        except Exception:
            self.radius = {}

    # -- values
    def _effect(self, sid, idx):
        return self.eff.get(sid, {}).get(max(idx - 1, 0))

    def value_range(self, sid, idx):
        e = self._effect(sid, idx)
        if not e:
            return None
        bp, ds = e[0], e[1]
        if ds <= 0:
            return (bp, bp)
        return (bp + 1, bp + ds)

    def token(self, kind, sid, idx):
        """Resolve one $-token value for spell sid. Returns a string or None (unresolvable)."""
        if kind in ("s", "S", "m", "M", "w", "W", "b"):
            if kind == "b":
                return None
            vr = self.value_range(sid, idx)
            if vr is None:
                return None
            lo, hi = vr
            if kind in ("m",):
                return _fmt_num(lo)
            if kind in ("M",):
                return _fmt_num(hi)
            if lo == hi or kind in ("w", "W"):
                return _fmt_num(lo)
            return "%s to %s" % (_fmt_num(lo), _fmt_num(hi))
        if kind in ("d", "D"):
            ms = self.dur.get(sid)
            return fmt_duration(ms) if ms else None
        if kind in ("t", "T"):
            e = self._effect(sid, idx)
            return _fmt_num(e[2] / 1000.0) if e and e[2] else None
        if kind in ("o", "O"):
            e = self._effect(sid, idx)
            ms = self.dur.get(sid)
            vr = self.value_range(sid, idx)
            if not e or not ms or not vr:
                return None
            if e[2]:
                ticks = ms // e[2]
            elif e[6] in (84, 85):  # MOD_REGEN / MOD_POWER_REGEN: value is per 5 sec
                ticks = ms / 5000.0
            else:
                return None
            lo, hi = int(abs(vr[0]) * ticks), int(abs(vr[1]) * ticks)
            return _fmt_num(lo) if lo == hi else "%s to %s" % (_fmt_num(lo), _fmt_num(hi))
        if kind in ("a", "A"):
            e = self._effect(sid, idx)
            r = self.radius.get(e[4]) if e else None
            return _fmt_num(r) if r else None
        if kind in ("x", "X"):
            e = self._effect(sid, idx)
            return str(e[3]) if e and e[3] else None
        if kind in ("h", "H"):
            a = self.aura.get(sid)
            return str(a[0]) if a and a[0] else None
        if kind in ("u", "U"):
            a = self.aura.get(sid)
            return str(a[1]) if a and a[1] else None
        if kind in ("n", "N"):
            a = self.aura.get(sid)
            return str(a[2]) if a and a[2] else None
        if kind in ("q", "Q"):
            e = self._effect(sid, idx)
            return str(e[7]) if e else None
        if kind == "z":
            return "your home"
        return None

    def numeric(self, kind, sid, idx):
        """Plain number for arithmetic inside ${...}."""
        if kind in ("s", "S", "m", "M", "w", "W"):
            vr = self.value_range(sid, idx)
            if vr is None:
                return None
            return vr[1] if kind in ("M",) else vr[0]
        if kind in ("d", "D"):
            ms = self.dur.get(sid)
            return ms / 1000.0 if ms else None
        if kind in ("t", "T"):
            e = self._effect(sid, idx)
            return e[2] / 1000.0 if e and e[2] else None
        if kind in ("a", "A", "x", "X", "h", "H", "u", "U", "n", "N", "q", "Q", "o", "O"):
            v = self.token(kind, sid, idx)
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        return None

    # -- description rendering
    _TOK = re.compile(r"\$(\d*)([a-zA-Z])(\d?)")

    def describe(self, sid, depth=0):
        text = self.desc.get(sid) or ""
        if not text:
            return ""
        return self._render(text, sid, depth)

    def _render(self, text, sid, depth):
        out = []
        i, n = 0, len(text)
        while i < n:
            c = text[i]
            if c != "$":
                out.append(c)
                i += 1
                continue
            rest = text[i + 1:]
            # conditionals: $?cond[a]?cond2[b][else]  -> else branch (we cannot evaluate the condition)
            if rest.startswith("?"):
                j, branches = i + 1, []
                while j < n and text[j] == "?":
                    k = text.find("[", j)
                    if k < 0:
                        break
                    grp, j = _bracket(text, k)
                    branches.append(grp)
                if j < n and text[j] == "[":
                    grp, j = _bracket(text, j)
                    out.append(self._render(grp, sid, depth))
                i = max(j, i + 2)
                continue
            if rest.startswith("{"):
                k = _matching(text, i + 1, "{", "}")
                expr = text[i + 2:k] if k > 0 else ""
                i = (k + 1) if k > 0 else n
                m = re.match(r"\.(\d)", text[i:])
                digits = None
                if m:
                    digits = int(m.group(1))
                    i += 2
                v = self._eval(expr, sid)
                if v is not None:
                    out.append(("%." + str(digits) + "f") % abs(v) if digits is not None else _fmt_num(v))
                continue
            if rest.startswith("@"):
                m = re.match(r"@(spelldesc|spellname|spellicon|spellaura|spelltooltip)(\d+)", rest)
                if m:
                    ref = int(m.group(2))
                    if m.group(1) == "spelldesc" and depth < 2:
                        out.append(self.describe(ref, depth + 1))
                    elif m.group(1) == "spellname":
                        out.append(self.name.get(ref, ""))
                    i += 1 + len(m.group(0))
                else:
                    i += 1
                continue
            if rest.startswith("<"):
                k = text.find(">", i)
                i = k + 1 if k > 0 else n
                continue
            m = re.match(r"/(\d+);(\d*)([a-zA-Z])(\d?)", rest)
            if m:  # $/1000;s1  -> value / 1000
                div = int(m.group(1)) or 1
                ref = int(m.group(2)) if m.group(2) else sid
                v = self.numeric(m.group(3), ref, int(m.group(4) or 1))
                if v is not None:
                    out.append(_fmt_num(v / div))
                i += 1 + len(m.group(0))
                continue
            m = re.match(r"([lLgG])([^:;]*):([^;]*);", rest)
            if m:  # $lsingular:plural;  $gmale:female;
                out.append(m.group(3) if m.group(1) in "lL" else m.group(2))
                i += 1 + len(m.group(0))
                continue
            m = self._TOK.match(text, i)
            if m:
                ref = int(m.group(1)) if m.group(1) else sid
                v = self.token(m.group(2), ref, int(m.group(3) or 1))
                if v is not None:
                    out.append(v)
                i = m.end()
                continue
            m = re.match(r"[A-Za-z]+", rest)  # $PL, $proccooldown ... -> strip
            i += 1 + (len(m.group(0)) if m else 0)
        return _clean("".join(out))

    def _eval(self, expr, sid):
        def sub(m):
            ref = int(m.group(1)) if m.group(1) else sid
            v = self.numeric(m.group(2), ref, int(m.group(3) or 1))
            if v is None:
                raise ValueError
            return repr(float(v))
        try:
            e = re.sub(r"\$(\d*)([a-zA-Z])(\d?)", sub, expr)
        except ValueError:
            return None
        if not re.fullmatch(r"[0-9.+\-*/() ]+", e or "x"):
            return None
        try:
            return float(eval(e, {"__builtins__": {}}, {}))  # noqa: S307 - digits/operators only
        except Exception:
            return None


def _matching(text, start, open_c, close_c):
    depth = 0
    for k in range(start, len(text)):
        if text[k] == open_c:
            depth += 1
        elif text[k] == close_c:
            depth -= 1
            if depth == 0:
                return k
    return -1


def _bracket(text, k):
    """text[k] == '[' -> (content, index after ']')."""
    e = _matching(text, k, "[", "]")
    if e < 0:
        return text[k + 1:], len(text)
    return text[k + 1:e], e + 1


def _clean(s):
    s = s.replace("\r", "")
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r" +([.,;:%)])", r"\1", s)
    s = re.sub(r"\(\s*\)", "", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# ---------------------------------------------------------------- token -> lines

def token_equip_lines(tok):
    out = []
    sp, heal = tok.get("sp", 0), tok.get("heal", 0)
    if heal and sp and heal > sp:
        out.append("Increases healing done by up to %d and damage done by up to %d for all magical spells "
                   "and effects." % (heal, sp))
    elif sp and (heal == sp or not heal):
        out.append("Increases damage and healing done by magical spells and effects by up to %d." % sp
                   if heal == sp else "Increases damage done by magical spells and effects by up to %d." % sp)
    elif heal:
        out.append("Increases healing done by spells and effects by up to %d." % heal)
    for k, school in SCHOOL_TOKENS.items():
        if tok.get(k):
            out.append("Increases damage done by %s spells and effects by up to %d." % (school, tok[k]))
    simple = [
        ("crit", "Improves your chance to get a critical strike by %d%%."),
        ("hit", "Improves your chance to hit by %d%%."),
        ("spellCrit", "Improves your chance to get a critical strike with spells by %d%%."),
        ("spellHit", "Improves your chance to hit with spells by %d%%."),
        ("ap", "+%d Attack Power."),
        ("rap", "+%d ranged Attack Power."),
        ("defense", "Increased Defense +%d."),
        ("dodge", "Increases your chance to dodge an attack by %d%%."),
        ("parry", "Increases your chance to parry an attack by %d%%."),
        ("block", "Increases your chance to block attacks with a shield by %d%%."),
        ("blockValue", "Increases the block value of your shield by %d."),
        ("mpr", "Restores %d mana per 5 sec."),
        ("hpr", "Restores %d health per 5 sec."),
    ]
    for k, fmt in simple:
        if tok.get(k):
            out.append(fmt % tok[k])
    return out


def class_names(mask):
    if mask is None or mask <= 0 or (mask & ALL_CLASS_MASK) == ALL_CLASS_MASK:
        return None
    names = [n for cid, n in CLASSES if mask & (1 << (cid - 1))]
    return ", ".join(names) if names else None


def type_name(cls, sub, inv):
    if cls == 2:
        return WEAPON_TYPES.get(sub)
    if cls == 4:
        if inv == 16:
            return None  # cloaks show only "Back"
        return ARMOR_TYPES.get(sub)
    if cls == 6:
        return PROJECTILE_TYPES.get(sub)
    return None


# ---------------------------------------------------------------- build

def _is_stat_spell(spells, sid):
    effs = spells.eff.get(sid)
    if not effs:
        return False
    for e in effs.values():
        effect, aura, misc = e[5], e[6], e[7]
        if effect != 6:
            return False
        if aura in STAT_AURAS:
            continue
        if aura == AURA_DEFENSE_SKILL and misc == 95:
            continue
        return False
    return True


def build(conn, cfg, download):
    """Build item_tooltip. `download(table)` returns a local CSV path (staticdata._download)."""
    sparse_p = download("ItemSparse")
    item_p = download("Item")
    spells = SpellDB(download)
    icons = {}
    for r in _rows(download("ManifestInterfaceData")):
        fn = r.get("FileName") or ""
        if fn.lower().endswith(".blp"):
            icons[_int(r["ID"])] = fn[:-4].lower()

    addons = cfg.get("addons_dir") or ""
    pdir = os.path.join(addons, "Polestar_ItemDB")
    items_lua = read_polestar(os.path.join(pdir, "Items.lua"))
    forever = read_polestar(os.path.join(pdir, "Forever.lua"))
    forever_names = read_forever_names(os.path.join(pdir, "Forever.lua"))
    hot = read_dbcache_hotfixes(cfg.get("dbcache_path"))
    log("Items.lua:", len(items_lua), "Forever.lua:", len(forever), "DBCache hotfix items:", len(hot))

    item_csv = {}
    for r in _rows(item_p):
        item_csv[_int(r["ID"])] = r

    effects = {}
    for r in _rows(download("ItemEffect")):
        effects.setdefault(_int(r["ParentItemID"]), []).append(
            (_int(r["LegacySlotIndex"]), _int(r["TriggerType"]), _int(r["SpellID"]),
             max(_int(r["CoolDownMSec"]), _int(r["CategoryCoolDownMSec"]))))

    base = {}
    for r in _rows(sparse_p):
        iid = _int(r["ID"])
        it = item_csv.get(iid, {})
        stats = []
        for k in range(10):
            st = _int(r.get("StatModifier_bonusStat_%d" % k), -1)
            amt = _int(r.get("StatModifier_bonusAmount_%d" % k))
            if st >= 0 and amt:
                stats.append((st, amt))
        dmg = []
        for k in range(5):
            lo, hi = _int(r.get("MinDamage_%d" % k)), _int(r.get("MaxDamage_%d" % k))
            if lo or hi:
                dmg.append((lo, hi, _int(it.get("DamageType_%d" % k)) if it else (_int(r.get("DamageType")) if k == 0 else 0)))
        base[iid] = {
            "name": r.get("Display_lang") or None,
            "description": r.get("Description_lang") or None,
            "quality": _int(r.get("OverallQualityID")),
            "ilvl": _int(r.get("ItemLevel")),
            "req": _int(r.get("RequiredLevel")),
            "inv": _int(r.get("InventoryType")),
            "class": _int(it.get("ClassID")) if it else None,
            "subclass": _int(it.get("SubclassID")) if it else None,
            "icon": _int(it.get("IconFileDataID")) if it else None,
            "bonding": _int(r.get("Bonding")),
            "maxcount": _int(r.get("MaxCount")),
            "armor": _int(r.get("Resistances_0")),
            "res": [(_int(r.get("Resistances_%d" % k)), k) for k in range(1, 7) if _int(r.get("Resistances_%d" % k))],
            "stats": stats,
            "dmg": dmg,
            "delay": _int(r.get("ItemDelay")),
            "durability": _int(r.get("MaxDurability")),
            "allowable_class": _int(r.get("AllowableClass"), -1),
            "skill": _int(r.get("RequiredSkill")),
            "skill_rank": _int(r.get("RequiredSkillRank")),
            "slots": _int(r.get("ContainerSlots")),
            "quest": _int(r.get("StartQuestID")),
            "sell": _int(r.get("SellPrice")),
            "src": ["csv"],
        }

    names_db = {r[0]: (r[1], r[2], r[3], r[4], r[5], r[6], r[7])
                for r in conn.execute("SELECT id, name, quality, item_level, class_id, subclass_id, icon, vendor_sell "
                                      "FROM items")}

    all_ids = set(base) | set(items_lua) | set(forever) | set(hot) | set(names_db)
    rows = []
    stat = {"items": 0, "csv": 0, "items_lua": 0, "forever": 0, "hotfix": 0, "with_icon": 0, "with_effects": 0}
    quality_updates = []
    for iid in all_ids:
        b = base.get(iid)
        if b is None:
            nd = names_db.get(iid)
            b = {"name": nd[0] if nd else None, "description": None,
                 "quality": nd[1] if nd and nd[1] is not None else 1, "ilvl": nd[2] if nd else 0, "req": 0,
                 "inv": 0, "class": nd[3] if nd else None, "subclass": nd[4] if nd else None,
                 "icon": nd[5] if nd else None, "bonding": 0, "maxcount": 0, "armor": 0, "res": [], "stats": [],
                 "dmg": [], "delay": 0, "durability": 0, "allowable_class": -1, "skill": 0, "skill_rank": 0,
                 "slots": 0, "quest": 0, "sell": nd[6] if nd else 0, "src": ["custom"]}
        else:
            stat["csv"] += 1
        tok = None
        # Items.lua (Blizzard Era tables, newer build)
        ov = items_lua.get(iid)
        if ov:
            _apply_overlay(b, ov)
            tok = ov["tok"]
            b["src"].append("items_lua")
            stat["items_lua"] += 1
        # client hotfix cache (header fields only)
        h = hot.get(iid)
        if h:
            for k in ("name", "description"):
                if h.get(k) and not b.get(k):
                    b[k] = h[k]
            if "name" in h and h["name"]:
                b["name"] = h["name"]
            for k in ("quality", "ilvl", "req", "bonding", "maxcount", "sell", "delay", "allowable_class"):
                if k in h:
                    b[k] = h[k]
            if "sparse_inv" in h:
                b["inv"] = h["sparse_inv"]
            for k in ("class", "subclass"):
                if k in h:
                    b[k] = h[k]
            if h.get("icon"):
                b["icon"] = h["icon"]
            b["src"].append("hotfix")
            stat["hotfix"] += 1
        # Forever.lua wins
        fv = forever.get(iid)
        if fv:
            _apply_overlay(b, fv)
            tok = fv["tok"]
            b["src"].append("forever")
            stat["forever"] += 1
            if not b.get("name") and forever_names.get(iid):
                b["name"] = forever_names[iid]
        data = _finish(iid, b, tok, effects.get(iid, []), spells)
        icon_name = icons.get(b.get("icon") or 0)
        if icon_name:
            stat["with_icon"] += 1
        if data.get("effects"):
            stat["with_effects"] += 1
        stat["items"] += 1
        rows.append((iid, icon_name, json.dumps(data, ensure_ascii=False, separators=(",", ":"))))
        nd = names_db.get(iid)
        if nd and (fv or h) and (nd[1] != data["quality"] or nd[2] != data["item_level"] or
                                 (not nd[0] and data.get("name"))):
            quality_updates.append((data["quality"], data["item_level"], data.get("name"), iid))
    conn.execute("DELETE FROM item_tooltip")
    conn.executemany("INSERT INTO item_tooltip(id, icon, data) VALUES(?,?,?)", rows)
    # Keep the list views consistent with the tooltip: Forever/hotfix quality, item level and missing names.
    conn.executemany("UPDATE items SET quality=?, item_level=?, name=COALESCE(name, ?) WHERE id=?", quality_updates)
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('tooltip_version', ?)", (VERSION,))
    conn.commit()
    stat["items_table_updates"] = len(quality_updates)
    log("tooltips:", stat)
    return stat


def _apply_overlay(b, ov):
    b["inv"] = ov["inv"]
    b["ilvl"] = ov["ilvl"]
    b["req"] = ov["req"]
    b["quality"] = ov["quality"]
    b["class"] = ov["class"]
    b["subclass"] = ov["subclass"]
    t = ov["tok"]
    b["armor"] = t.get("armor", 0)
    b["stats"] = [(st, t[k]) for k, st in (("str", 4), ("agi", 3), ("sta", 7), ("int", 5), ("spi", 6)) if t.get(k)]
    if "dmin" in t or "dmax" in t:
        extra = b["dmg"][1:] if b.get("dmg") else []
        school = b["dmg"][0][2] if b.get("dmg") else 0
        b["dmg"] = [(t.get("dmin", 0), t.get("dmax", 0), school)] + extra
    elif ov["class"] != 2:
        b["dmg"] = []
    if t.get("speed"):
        b["delay"] = t["speed"]
    if "cls" in t:
        b["allowable_class"] = t["cls"]
    if t.get("uniq"):
        b["maxcount"] = 1


def _finish(iid, b, tok, effs, spells):
    cls, sub, inv = b.get("class"), b.get("subclass"), b.get("inv") or 0
    d = {"id": iid, "name": b.get("name"), "quality": b.get("quality") if b.get("quality") is not None else 1,
         "item_level": b.get("ilvl") or None, "required_level": b.get("req") or None,
         "class_id": cls, "subclass_id": sub, "source": b["src"]}
    if b.get("bonding") in BONDING:
        d["bind"] = BONDING[b["bonding"]]
    if b.get("maxcount") == 1:
        d["unique"] = True
    if inv in SLOTS and (cls in (2, 4, 6, 11, 1) or cls is None):
        if not (cls in (1, 11) and inv == 18):
            d["slot"] = SLOTS[inv]
    tn = type_name(cls, sub, inv)
    if tn:
        d["type"] = tn
    if cls in (1, 11) and b.get("slots"):
        d["container"] = "%d Slot %s" % (b["slots"], "Quiver" if cls == 11 and sub == 2 else
                                         "Ammo Pouch" if cls == 11 else "Bag")
    delay = b.get("delay") or 0
    if b.get("dmg") and cls == 2:
        lo, hi, school = b["dmg"][0]
        dm = {"min": lo, "max": hi, "school": SCHOOLS.get(school, "")}
        if delay:
            dm["speed"] = round(delay / 1000.0, 2)
            total = (lo + hi) / 2.0 + sum((x[0] + x[1]) / 2.0 for x in b["dmg"][1:])
            dm["dps"] = round(total / (delay / 1000.0), 1)
        dm["extra"] = [{"min": x[0], "max": x[1], "school": SCHOOLS.get(x[2], "")} for x in b["dmg"][1:]]
        d["damage"] = dm
    elif cls == 2 and delay:
        d["damage"] = {"min": None, "max": None, "speed": round(delay / 1000.0, 2), "school": "", "extra": []}
    if b.get("armor"):
        d["armor"] = b["armor"]
    if b.get("stats"):
        d["stats"] = ["%+d %s" % (amt, STATS[st]) for st, amt in b["stats"] if st in STATS]
    if b.get("res"):
        d["resistances"] = ["%+d %s Resistance" % (v, SCHOOLS[k]) for v, k in b["res"] if SCHOOLS.get(k)]
    if b.get("durability"):
        d["durability"] = b["durability"]
    cn = class_names(b.get("allowable_class"))
    if cn:
        d["classes"] = cn
    if b.get("skill") in SKILLS and b.get("skill_rank"):
        d["skill"] = "Requires %s (%d)" % (SKILLS[b["skill"]], b["skill_rank"])
    if b.get("quest"):
        d["begins_quest"] = True
    # effects
    lines = []
    teaches = None
    for slot, trig, sid, cd in sorted(effs):
        label = TRIGGERS.get(trig)
        if not label:
            continue
        for e in spells.eff.get(sid, {}).values():
            if e[5] == 36 and e[8]:  # LEARN_SPELL
                teaches = e[8]
        if trig == 1 and tok is not None and _is_stat_spell(spells, sid):
            continue  # replaced by overlay token lines
        text = spells.describe(sid)
        if not text and teaches and trig in (0, 6):
            text = "Teaches you how to make %s." % (spells.name.get(teaches) or "this recipe")
        if not text:
            continue
        e = {"trigger": label, "text": text}
        if label == "Use" and cd and cd > 1500:
            e["cooldown"] = fmt_cooldown(cd)
        lines.append(e)
    if tok:
        lines = [{"trigger": "Equip", "text": t} for t in token_equip_lines(tok)] + lines
    if lines:
        d["effects"] = lines
    if teaches:
        d["teaches"] = teaches
    if b.get("description"):
        d["description"] = b["description"]
    if b.get("sell"):
        d["sell_price"] = b["sell"]
    return d
