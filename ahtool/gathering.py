"""What the gathering professions (Herbalism, Mining, Fishing, Skinning) can gather, with AH prices.

The lists are curated Classic Era data. Items are listed by name and resolved to ids through the items table, so
the ids always match the loaded item data; a name that isn't there (e.g. a renamed custom item) is left out.

skill: the skill needed. Exact for herbs and mining nodes. For fishing it is the usual fishing level of the zones
where the fish is caught (you can fish there earlier, with more misses); for skinning it follows from the mob level
(skill = level x 5 above level 20), so both are marked approximate.
"""
from . import analytics as A

HERBALISM, MINING, FISHING, SKINNING = 5, 7, 11, 12
APPROX = {FISHING, SKINNING}

# (item name, skill, where, kind)
DATA = {
    HERBALISM: [
        ("Peacebloom", 1, "Starting zones", "herb"),
        ("Silverleaf", 1, "Starting zones", "herb"),
        ("Earthroot", 15, "Starting zones, Barrens, Westfall", "herb"),
        ("Mageroyal", 50, "Barrens, Westfall, Darkshore, Silverpine", "herb"),
        ("Briarthorn", 70, "Barrens, Darkshore, Redridge, Hillsbrad", "herb"),
        ("Swiftthistle", 70, "Extra drop from Mageroyal and Briarthorn", "herb"),
        ("Stranglekelp", 85, "Under water along coasts", "herb"),
        ("Bruiseweed", 100, "Barrens, Ashenvale, Wetlands, Hillsbrad", "herb"),
        ("Wild Steelbloom", 115, "Stonetalon, Arathi, Thousand Needles, Stranglethorn", "herb"),
        ("Grave Moss", 120, "Duskwood, Desolace, graveyards", "herb"),
        ("Kingsblood", 125, "Ashenvale, Hillsbrad, Arathi, Stranglethorn", "herb"),
        ("Liferoot", 150, "Near water: Wetlands, Arathi, Stranglethorn, Dustwallow", "herb"),
        ("Fadeleaf", 160, "Swamp of Sorrows, Stranglethorn, Arathi, Dustwallow", "herb"),
        ("Goldthorn", 170, "Arathi, Stranglethorn, Badlands, Feralas", "herb"),
        ("Khadgar's Whisker", 185, "Arathi, Stranglethorn, Swamp of Sorrows, Feralas", "herb"),
        ("Wintersbite", 195, "Alterac Mountains", "herb"),
        ("Firebloom", 205, "Searing Gorge, Blasted Lands, Tanaris", "herb"),
        ("Purple Lotus", 210, "Tanaris, Feralas, Hinterlands, Azshara", "herb"),
        ("Wildvine", 210, "Extra drop from Purple Lotus and other high herbs", "herb"),
        ("Arthas' Tears", 220, "Felwood, Plaguelands", "herb"),
        ("Sungrass", 230, "Feralas, Hinterlands, Un'Goro, Silithus, Plaguelands", "herb"),
        ("Blindweed", 235, "Swamp of Sorrows, Un'Goro", "herb"),
        ("Ghost Mushroom", 245, "Caves: Hinterlands, Maraudon, Un'Goro", "herb"),
        ("Gromsblood", 250, "Felwood, Desolace, Blasted Lands, Ashenvale", "herb"),
        ("Golden Sansam", 260, "Feralas, Azshara, Un'Goro, Burning Steppes", "herb"),
        ("Dreamfoil", 270, "Un'Goro, Azshara, Felwood, Silithus", "herb"),
        ("Mountain Silversage", 280, "Winterspring, Un'Goro, Azshara, Burning Steppes", "herb"),
        ("Plaguebloom", 285, "Plaguelands, Felwood", "herb"),
        ("Icecap", 290, "Winterspring", "herb"),
        ("Black Lotus", 300, "Rare: Winterspring, Burning Steppes, Silithus, Eastern Plaguelands", "herb"),
        ("Bloodvine", 300, "Zul'Gurub", "herb"),
    ],
    MINING: [
        ("Copper Ore", 1, "Copper vein: starting zones, Barrens, Westfall", "ore"),
        ("Rough Stone", 1, "Copper vein", "stone"),
        ("Malachite", 1, "Copper vein (gem)", "gem"),
        ("Tigerseye", 1, "Copper vein (gem)", "gem"),
        ("Shadowgem", 65, "Tin vein (gem)", "gem"),
        ("Tin Ore", 65, "Tin vein: Redridge, Wetlands, Stonetalon, Ashenvale", "ore"),
        ("Coarse Stone", 65, "Tin vein", "stone"),
        ("Incendicite Ore", 65, "Incendicite vein: Wetlands, Thousand Needles", "ore"),
        ("Moss Agate", 65, "Tin vein (gem)", "gem"),
        ("Lesser Moonstone", 65, "Tin and silver veins (gem)", "gem"),
        ("Silver Ore", 75, "Silver vein (rare, near tin)", "ore"),
        ("Lesser Bloodstone Ore", 75, "Lesser Bloodstone deposit: Arathi Highlands", "ore"),
        ("Iron Ore", 125, "Iron deposit: Arathi, Stranglethorn, Desolace, Badlands", "ore"),
        ("Heavy Stone", 125, "Iron deposit", "stone"),
        ("Jade", 125, "Iron deposit (gem)", "gem"),
        ("Citrine", 125, "Iron and gold deposits (gem)", "gem"),
        ("Gold Ore", 155, "Gold vein (rare, near iron/mithril)", "ore"),
        ("Aquamarine", 155, "Gold and mithril deposits (gem)", "gem"),
        ("Mithril Ore", 175, "Mithril deposit: Hinterlands, Tanaris, Feralas, Searing Gorge", "ore"),
        ("Solid Stone", 175, "Mithril deposit", "stone"),
        ("Star Ruby", 175, "Mithril and truesilver deposits (gem)", "gem"),
        ("Truesilver Ore", 230, "Truesilver deposit (rare, near mithril/thorium)", "ore"),
        ("Dark Iron Ore", 230, "Dark Iron deposit: Searing Gorge, Blackrock Depths, Molten Core", "ore"),
        ("Thorium Ore", 245, "Small thorium vein (245), rich thorium vein (275)", "ore"),
        ("Dense Stone", 245, "Thorium veins", "stone"),
        ("Blue Sapphire", 245, "Thorium veins (gem)", "gem"),
        ("Large Opal", 245, "Thorium veins (gem)", "gem"),
        ("Huge Emerald", 245, "Thorium veins (gem)", "gem"),
        ("Azerothian Diamond", 245, "Thorium veins (gem)", "gem"),
        ("Arcane Crystal", 275, "Rich thorium vein (rare)", "gem"),
    ],
    FISHING: [
        ("Raw Brilliant Smallfish", 1, "Inland waters of the starting zones", "fish"),
        ("Raw Slitherskin Mackerel", 1, "Coasts of the starting zones", "fish"),
        ("Raw Loch Frenzy", 1, "Loch Modan", "fish"),
        ("Raw Longjaw Mud Snapper", 25, "Inland: Westfall, Loch Modan, Silverpine, Barrens, Darkshore", "fish"),
        ("Raw Rainbow Fin Albacore", 25, "Coast: Westfall, Darkshore, Barrens", "fish"),
        ("Oily Blackmouth", 25, "Schools along coasts: Westfall, Darkshore, Barrens, Wetlands", "fish"),
        ("Deviate Fish", 25, "Barrens: Lushwater Oasis and other oases", "fish"),
        ("Raw Bristle Whisker Catfish", 55, "Inland: Wetlands, Redridge, Hillsbrad, Ashenvale, Duskwood", "fish"),
        ("Raw Sagefish", 55, "Inland: Redridge, Hillsbrad, Wetlands", "fish"),
        ("Raw Rockscale Cod", 75, "Coast: Wetlands, Hillsbrad, Stranglethorn, Desolace", "fish"),
        ("Firefin Snapper", 75, "Schools along coasts: Wetlands, Arathi, Stranglethorn, Desolace", "fish"),
        ("Raw Mithril Head Trout", 130, "Inland: Arathi, Stranglethorn, Alterac, Desolace, Dustwallow", "fish"),
        ("Raw Spotted Yellowtail", 205, "Coast: Tanaris, Feralas, Azshara, Hinterlands", "fish"),
        ("Raw Glossy Mightfish", 205, "Coast: Tanaris, Feralas, Azshara", "fish"),
        ("Stonescale Eel", 205, "Schools along coasts: Tanaris, Feralas, Azshara", "fish"),
        ("Raw Redgill", 205, "Inland: Hinterlands, Feralas, Swamp of Sorrows, Felwood", "fish"),
        ("Raw Nightfin Snapper", 205, "Inland, at night: Feralas, Hinterlands, Felwood, Un'Goro", "fish"),
        ("Raw Sunscale Salmon", 205, "Inland, by day: Feralas, Hinterlands, Felwood, Un'Goro", "fish"),
        ("Raw Greater Sagefish", 205, "Inland: Hinterlands, Feralas, Felwood", "fish"),
        ("Raw Summer Bass", 205, "Coast, summer season", "fish"),
        ("Winter Squid", 205, "Coast, winter season", "fish"),
        ("Darkclaw Lobster", 255, "Coast: Azshara, Feralas, Tanaris", "fish"),
        ("Raw Whitescale Salmon", 280, "Inland: Winterspring, Eastern Plaguelands", "fish"),
    ],
    SKINNING: [
        ("Ruined Leather Scraps", 1, "Beasts of any level", "leather"),
        ("Light Leather", 1, "Beasts up to about level 25", "leather"),
        ("Light Hide", 1, "Beasts up to about level 25", "hide"),
        ("Medium Leather", 100, "Beasts about level 20-35", "leather"),
        ("Medium Hide", 100, "Beasts about level 20-35", "hide"),
        ("Red Whelp Scale", 100, "Red whelps: Redridge, Wetlands", "scale"),
        ("Black Whelp Scale", 100, "Black whelps: Wetlands", "scale"),
        ("Raptor Hide", 150, "Raptors: Wetlands, Arathi, Stranglethorn", "hide"),
        ("Heavy Leather", 150, "Beasts about level 30-45", "leather"),
        ("Heavy Hide", 150, "Beasts about level 30-45", "hide"),
        ("Green Whelp Scale", 200, "Green whelps: Swamp of Sorrows", "scale"),
        ("Thick Leather", 200, "Beasts about level 40-50", "leather"),
        ("Thick Hide", 200, "Beasts about level 40-50", "hide"),
        ("Turtle Scale", 200, "Turtles: Tanaris, Feralas, Hinterlands", "scale"),
        ("Scorpid Scale", 200, "Scorpids: Tanaris, Searing Gorge, Blasted Lands", "scale"),
        ("Rugged Leather", 250, "Beasts about level 50-60", "leather"),
        ("Rugged Hide", 250, "Beasts about level 50-60", "hide"),
        ("Worn Dragonscale", 250, "Dragonkin", "scale"),
        ("Black Dragonscale", 250, "Black dragonkin: Burning Steppes", "scale"),
        ("Green Dragonscale", 250, "Green dragonkin: Swamp of Sorrows, Feralas", "scale"),
        ("Blue Dragonscale", 250, "Blue dragonkin: Azshara, Winterspring", "scale"),
        ("Devilsaur Leather", 250, "Devilsaurs: Un'Goro Crater", "leather"),
        ("Chimera Leather", 250, "Chimaeras: Felwood, Winterspring", "leather"),
        ("Frostsaber Leather", 250, "Frostsabers: Winterspring", "leather"),
        ("Warbear Leather", 250, "Bears: Felwood, Winterspring", "leather"),
        ("Red Dragonscale", 300, "Red dragonkin: Blackrock Spire", "scale"),
        ("Core Leather", 300, "Core hounds: Molten Core", "leather"),
        ("Primal Bat Leather", 300, "Bats: Zul'Gurub", "leather"),
        ("Primal Tiger Leather", 300, "Tigers: Zul'Gurub", "leather"),
    ],
}


def _name_index(ctx):
    """lower-case item name -> [item ids], built once per analytics context."""
    idx = getattr(ctx, "gather_names", None)
    if idx is None:
        idx = {}
        for iid, it in ctx.items.items():
            if it.get("name"):
                idx.setdefault(it["name"].lower(), []).append(iid)
        ctx.gather_names = idx
    return idx


def _resolve(ctx, name):
    ids = _name_index(ctx).get(name.lower())
    if not ids:
        return None
    # Several ids can share a name (old test items, quest copies): prefer the one the AH actually trades.
    priced = [i for i in ids if i in ctx.cur]
    return min(priced or ids)


def gather_count(prof_id):
    return len(DATA.get(prof_id, ()))


def gather_items(conn, cfg, prof_id, realm=None):
    """Rows for one gathering profession, in list order (by skill). [] for other professions."""
    entries = DATA.get(int(prof_id))
    if not entries:
        return []
    ctx = A.get_ctx(conn, cfg, realm)
    out = []
    seen = set()
    for name, skill, where, kind in entries:
        iid = _resolve(ctx, name)
        if iid is None or iid in seen:
            continue
        seen.add(iid)
        row = A._row(ctx, iid)
        it = ctx.items.get(iid) or {}
        row.update({"skill": skill, "skill_approx": int(prof_id) in APPROX, "where": where, "kind": kind,
                    "vendor_sell": it.get("vendor_sell")})
        out.append(row)
    return out
