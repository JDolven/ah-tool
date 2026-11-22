// Mock API for offline frontend development. Activated with ?mock=1.
// Generates deterministic, realistic Classic AH data and applies the same
// analytics rules as the backend (see CONTRACT.md).

const REALM = 'PvP';
const LATEST_DAY = 2511; // 2026-11-16
const HISTORY_DAYS = 75;
const BASELINE_DAYS = 14;
const AH_CUT = 0.05;

// ---------------------------------------------------------------- seeded rng
function rng(seed) {
  let s = seed >>> 0;
  return () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function dayToDate(day) {
  const d = new Date(Date.UTC(2020, 0, 1) + day * 86400000);
  return d.toISOString().slice(0, 10);
}

// ---------------------------------------------------------------- items
// [id, name, quality, ilvl, class_id, subclass, vendor_sell, vendor_buy, stack, basePrice (copper, null = never on AH), avail, last price factor]
const ITEM_ROWS = [
  [2589, 'Linen Cloth', 1, 5, 7, 5, 13, 0, 20, 85, 420, 0.12],
  [2996, 'Bolt of Linen Cloth', 1, 10, 7, 5, 40, 0, 20, 260, 140, 1.0],
  [2592, 'Wool Cloth', 1, 15, 7, 5, 33, 0, 20, 140, 380, 0.95],
  [4306, 'Silk Cloth', 1, 25, 7, 5, 150, 0, 20, 950, 610, 0.7],
  [4305, 'Bolt of Silk Cloth', 1, 30, 7, 5, 600, 0, 20, 4600, 85, 1.05],
  [4338, 'Mageweave Cloth', 1, 35, 7, 5, 250, 0, 20, 1600, 520, 0.98],
  [14047, 'Runecloth', 1, 50, 7, 5, 400, 0, 20, 3400, 1240, 0.92],
  [14048, 'Bolt of Runecloth', 1, 55, 7, 5, 1600, 0, 20, 17500, 160, 1.02],
  [14256, 'Felcloth', 2, 50, 7, 5, 2000, 0, 20, 48000, 34, 1.0],
  [14342, 'Mooncloth', 2, 60, 7, 5, 4000, 0, 20, 210000, 12, 1.08],
  [2447, 'Peacebloom', 1, 5, 7, 9, 10, 0, 20, 120, 300, 0.9],
  [765, 'Silverleaf', 1, 5, 7, 9, 10, 0, 20, 95, 260, 1.0],
  [2453, 'Bruiseweed', 1, 20, 7, 9, 10, 0, 20, 720, 120, 1.1],
  [2450, 'Briarthorn', 1, 15, 7, 9, 10, 0, 20, 460, 150, 0.95],
  [13464, 'Golden Sansam', 1, 52, 7, 9, 200, 0, 20, 14200, 240, 0.62],
  [13465, 'Mountain Silversage', 1, 56, 7, 9, 250, 0, 20, 18500, 180, 0.97],
  [13466, 'Plaguebloom', 1, 57, 7, 9, 250, 0, 20, 9800, 95, 1.02],
  [13468, 'Black Lotus', 2, 60, 7, 9, 10000, 0, 20, 1150000, 6, 1.35],
  [8846, 'Gromsblood', 1, 50, 7, 9, 400, 0, 20, 3600, 210, 0.7],
  [13423, 'Stonescale Oil', 1, 50, 7, 11, 1000, 0, 20, 4800, 60, 1.0],
  [3371, 'Empty Vial', 1, 1, 7, 11, 1, 4, 20, null, 0, 1],
  [3372, 'Leaded Vial', 1, 15, 7, 11, 10, 40, 20, null, 0, 1],
  [8925, 'Crystal Vial', 1, 45, 7, 11, 100, 400, 20, null, 0, 1],
  [118, 'Minor Healing Potion', 1, 5, 0, 1, 5, 0, 5, 320, 90, 1.0],
  [929, 'Healing Potion', 1, 22, 0, 1, 100, 0, 5, 2100, 70, 1.15],
  [13446, 'Major Healing Potion', 1, 55, 0, 1, 1000, 0, 5, 33000, 140, 1.12],
  [13444, 'Major Mana Potion', 1, 59, 0, 1, 1000, 0, 5, 48000, 110, 1.0],
  [13452, 'Elixir of the Mongoose', 1, 56, 0, 2, 1000, 0, 5, 96000, 40, 1.08],
  [13510, 'Flask of the Titans', 1, 60, 0, 3, 5000, 0, 1, 2600000, 4, 1.04],
  [10620, 'Thorium Ore', 1, 50, 7, 7, 250, 0, 20, 9200, 520, 0.97],
  [12359, 'Thorium Bar', 1, 50, 7, 7, 600, 0, 20, 11500, 340, 0.72],
  [12363, 'Arcane Crystal', 2, 60, 7, 7, 2500, 0, 20, 960000, 18, 0.68],
  [12360, 'Arcanite Bar', 2, 60, 7, 7, 5000, 0, 20, 1250000, 9, 1.0],
  [12365, 'Dense Stone', 1, 50, 7, 7, 250, 0, 20, 2400, 380, 1.05],
  [12404, 'Dense Sharpening Stone', 1, 55, 0, 8, 100, 0, 20, 3400, 140, 1.0],
  [1251, 'Linen Bandage', 1, 1, 0, 7, 10, 0, 20, 140, 160, 1.0],
  [6451, 'Heavy Silk Bandage', 1, 30, 0, 7, 100, 0, 20, 1500, 80, 1.0],
  [14530, 'Heavy Runecloth Bandage', 1, 58, 0, 7, 500, 0, 20, 9200, 220, 0.95],
  [14344, 'Large Brilliant Shard', 3, 60, 7, 12, 0, 0, 20, 125000, 42, 0.64],
  [12811, 'Righteous Orb', 2, 60, 7, 11, 0, 0, 20, 145000, 22, 1.0],
  [20725, 'Nexus Crystal', 4, 60, 7, 12, 0, 0, 20, 920000, 5, 0.42],
  [2672, 'Stringy Wolf Meat', 1, 5, 7, 8, 4, 0, 10, 55, 120, 1.0],
  [2678, 'Mild Spices', 1, 5, 7, 8, 0, 10, 10, null, 0, 1],
  [2680, 'Spiced Wolf Meat', 1, 10, 0, 5, 6, 0, 20, 130, 60, 1.0],
  [12803, 'Living Essence', 2, 55, 7, 10, 2000, 0, 10, 42000, 26, 0.93],
  [7076, 'Essence of Earth', 2, 45, 7, 10, 1000, 0, 10, 31000, 30, 1.0],
  [7078, 'Essence of Fire', 2, 45, 7, 10, 1000, 0, 10, 36000, 24, 0.66],
  [15994, 'Thorium Widget', 1, 52, 7, 1, 1500, 0, 20, 22000, 15, 1.0],
  [15993, 'Thorium Grenade', 1, 55, 0, 8, 750, 0, 20, 21000, 40, 1.03],
  [14551, "Edgemaster's Handguards", 4, 50, 4, 2, 23100, 0, 1, 4800000, 1, 0.95],
  [12640, 'Lionheart Helm', 4, 65, 4, 4, 70000, 0, 1, 9500000, 1, 1.0],
  [18335, 'Pristine Black Diamond', 2, 60, 7, 0, 0, 0, 20, 160000, 2, 0.5],
  [2770, 'Copper Ore', 1, 10, 7, 7, 5, 0, 20, 75, 640, 0.15],
  [2840, 'Copper Bar', 1, 10, 7, 7, 10, 0, 20, 110, 220, 1.0],
  [7909, 'Aquamarine', 2, 45, 7, 0, 1000, 0, 20, 8900, 32, 1.0],
  [284712, null, 3, 0, null, null, 0, 0, 1, 450000, 2, 1.0],
  // v4 – thin market showcase
  [9206, 'Elixir of Giants', 1, 46, 0, 2, 300, 0, 5, 9500, 6, 3.2],
  // v4 – tailoring leveling path
  [2997, 'Bolt of Woolen Cloth', 1, 15, 7, 5, 100, 0, 20, 520, 90, 1.0],
  [4238, 'Linen Bag', 1, 15, 1, 0, 87, 0, 1, 900, 40, 1.0],
  [2580, 'Reinforced Linen Cape', 2, 12, 4, 1, 74, 0, 1, 1800, 6, 1.0],
  [2587, 'Gray Woolen Shirt', 1, 20, 4, 0, 150, 0, 1, 900, 8, 1.0],
  [4314, 'Double-stitched Woolen Shoulders', 2, 22, 4, 1, 250, 0, 1, 3200, 5, 0.9],
  [7048, 'Azure Silk Hood', 2, 29, 4, 1, 400, 0, 1, 4500, 7, 1.0],
  [7050, 'Silk Headband', 2, 32, 4, 1, 500, 0, 1, 3800, 4, 1.0],
  [4334, 'Formal White Shirt', 1, 34, 4, 0, 600, 0, 1, 2500, 1, 1.0],
  [4339, 'Bolt of Mageweave', 1, 40, 7, 5, 1000, 0, 20, 7200, 140, 1.0],
  [7062, 'Crimson Silk Pantaloons', 2, 41, 4, 1, 1000, 0, 1, 9500, 5, 1.0],
  [9998, 'Black Mageweave Vest', 2, 41, 4, 1, 1100, 0, 1, 8000, 9, 1.0],
  [10003, 'Black Mageweave Gloves', 2, 43, 4, 1, 800, 0, 1, 7600, 6, 1.0],
  [10024, 'Black Mageweave Headband', 2, 49, 4, 1, 1300, 0, 1, 9800, 4, 1.0],
  [13856, 'Runecloth Belt', 2, 55, 4, 1, 2000, 0, 1, 19000, 6, 1.0],
  [14046, 'Runecloth Bag', 2, 55, 1, 0, 2500, 0, 1, 98000, 22, 1.0],
  [13863, 'Runecloth Gloves', 2, 58, 4, 1, 2400, 0, 1, 26000, 5, 1.0],
  [13860, 'Runecloth Cloak', 2, 56, 4, 1, 2300, 0, 1, 21000, 4, 1.0],
  [14472, 'Pattern: Runecloth Cloak', 2, 56, 9, 2, 1000, 0, 1, 18000, 3, 1.0],
  [8170, 'Rugged Leather', 1, 50, 7, 6, 375, 0, 20, 3800, 300, 1.0],
  [6291, 'Raw Brilliant Smallfish', 1, 5, 0, 0, 1, 0, 20, 12, 260, 0.9],
  [13422, 'Stonescale Eel', 1, 45, 7, 11, 40, 0, 20, 1900, 180, 1.0],
  [2318, 'Light Leather', 1, 10, 7, 6, 15, 0, 20, 140, 520, 0.95],
  [2320, 'Coarse Thread', 1, 5, 7, 5, 2, 10, 20, null, 0, 1],
  [2321, 'Fine Thread', 1, 15, 7, 5, 25, 100, 20, null, 0, 1],
  [4340, 'Gray Dye', 1, 15, 7, 11, 8, 35, 20, null, 0, 1],
  [6260, 'Blue Dye', 1, 15, 7, 11, 12, 50, 20, null, 0, 1],
  [2324, 'Bleach', 1, 15, 7, 11, 6, 25, 20, null, 0, 1],
  [2604, 'Red Dye', 1, 15, 7, 11, 12, 50, 20, null, 0, 1],
  [4291, 'Silken Thread', 1, 30, 7, 5, 125, 500, 20, null, 0, 1],
  [8343, 'Heavy Silken Thread', 1, 40, 7, 5, 500, 2000, 20, null, 0, 1],
  [14341, 'Rune Thread', 1, 50, 7, 5, 1250, 5000, 20, null, 0, 1],
  // v4 – alchemy leveling path (the profit case)
  [858, 'Lesser Healing Potion', 1, 13, 0, 1, 25, 0, 5, 900, 60, 1.0],
  [785, 'Mageroyal', 1, 20, 7, 9, 25, 0, 20, 650, 140, 1.0],
  [3385, 'Lesser Mana Potion', 1, 24, 0, 1, 30, 0, 5, 1900, 50, 1.0],
  [3357, 'Liferoot', 1, 35, 7, 9, 75, 0, 20, 2100, 110, 1.0],
  [3356, 'Kingsblood', 1, 30, 7, 9, 60, 0, 20, 1700, 130, 1.0],
  [1710, 'Greater Healing Potion', 1, 35, 0, 1, 250, 0, 5, 5200, 70, 1.0],
  [3820, 'Stranglekelp', 1, 25, 7, 9, 40, 0, 20, 900, 150, 1.0],
  [3821, 'Goldthorn', 1, 37, 7, 9, 150, 0, 20, 2600, 120, 1.0],
  [8949, 'Elixir of Agility', 1, 37, 0, 2, 200, 0, 5, 6800, 30, 1.0],
  [8838, 'Sungrass', 1, 46, 7, 9, 200, 0, 20, 3900, 160, 1.0],
  [3358, "Khadgar's Whisker", 1, 37, 7, 9, 175, 0, 20, 3000, 110, 1.0],
  [3928, 'Superior Healing Potion', 1, 45, 0, 1, 250, 0, 5, 11500, 90, 1.0],
  [8836, "Arthas' Tears", 1, 45, 7, 9, 250, 0, 20, 1600, 90, 1.0],
  [9154, 'Elixir of Detect Undead', 1, 46, 0, 2, 250, 0, 5, 4200, 12, 1.0],
  [9187, 'Elixir of Greater Agility', 1, 48, 0, 2, 300, 0, 5, 14500, 40, 1.0],
  [8839, 'Blindweed', 1, 47, 7, 9, 150, 0, 20, 2300, 150, 1.0],
  [13443, 'Superior Mana Potion', 1, 49, 0, 1, 400, 0, 5, 16500, 70, 1.0],
  // v4 – enchanting (plan stops: Lesser Magic Essence has no AH price)
  [10940, 'Strange Dust', 1, 10, 7, 12, 0, 0, 20, 450, 300, 1.0],
  [6218, 'Runed Copper Rod', 1, 10, 2, 0, 125, 0, 1, null, 0, 1],
  [10938, 'Lesser Magic Essence', 2, 10, 7, 12, 0, 0, 20, null, 0, 1],
];

const ITEMS = new Map();
for (const r of ITEM_ROWS) {
  ITEMS.set(r[0], {
    id: r[0], name: r[1], quality: r[2], item_level: r[3], class_id: r[4], subclass_id: r[5],
    vendor_sell: r[6], vendor_buy: r[7] || null, stack: r[8], base: r[9], avail: r[10], factor: r[11],
  });
}

const VENDOR = new Map([
  [3371, 4], [3372, 40], [8925, 400], [2678, 10],
  [2320, 10], [2321, 100], [4340, 35], [6260, 50], [2324, 25], [2604, 50], [4291, 500], [8343, 2000], [14341, 5000],
]);

// v4: thin markets / outliers
const THIN_QTY = 2;
const AVAIL_NOW = new Map([[9206, 1], [20725, 1], [18335, 1]]); // forced current quantity
const OUTLIER_SPIKES = new Map([[14342, 6.5], [13464, 4.2], [12360, 0.16], [4339, 5]]); // one odd day each

const CLASS_NAMES = {
  0: 'Consumable', 1: 'Container', 2: 'Weapon', 4: 'Armor', 7: 'Trade Goods', 9: 'Recipe', 15: 'Miscellaneous',
};

// ---------------------------------------------------------------- price history
const DAILY = new Map(); // id -> [{day, low, high, available}]
const CURRENT = new Map(); // id -> {price, available, last_day}
const SNAPSHOTS = new Map();

for (const it of ITEMS.values()) {
  if (it.base == null) continue;
  const rnd = rng(it.id * 7919);
  const rows = [];
  // some items have shorter history; one item is stale
  const startOffset = it.id === 18335 ? 8 : it.id === 284712 ? 20 : HISTORY_DAYS;
  const lastDay = it.id === 12640 ? LATEST_DAY - 5 : LATEST_DAY;
  let level = it.base * (0.92 + rnd() * 0.16);
  const drift = it.id === 13468 ? 0.006 : it.id === 7078 ? -0.006 : 0;
  for (let day = lastDay - startOffset; day <= lastDay; day++) {
    if (rnd() < 0.12 && day !== lastDay) continue; // missed scan days
    const target = it.base * (1 + drift * (day - lastDay));
    level = level * (1 + (rnd() - 0.5) * 0.08);
    level = level * 0.7 + target * 0.3;
    const low = Math.max(1, Math.round(level * (0.94 + rnd() * 0.04)));
    const high = Math.max(low, Math.round(level * (1.0 + rnd() * 0.12)));
    const available = Math.max(1, Math.round(it.avail * (0.6 + rnd() * 0.8)));
    rows.push({ day, low, high, available });
  }
  const last = rows[rows.length - 1];
  // Apply the designed "today" factor relative to base (creates deals)
  const priceNow = Math.max(1, Math.round(it.base * it.factor * (0.98 + rnd() * 0.04)));
  last.low = priceNow;
  last.high = Math.max(last.high, priceNow);
  if (AVAIL_NOW.has(it.id)) last.available = AVAIL_NOW.get(it.id);
  if (OUTLIER_SPIKES.has(it.id) && rows.length > 12) {
    const o = rows[rows.length - 9];
    o.low = Math.round(it.base * OUTLIER_SPIKES.get(it.id));
    o.high = Math.max(o.low, Math.round(o.low * 1.03));
    o.available = 1;
    o.outlier = true;
  }
  CURRENT.set(it.id, { price: priceNow, available: last.available, last_day: last.day });
  DAILY.set(it.id, rows);

  const snaps = [];
  const nowTs = Math.floor(Date.UTC(2026, 10, 16, 21, 30) / 1000);
  let p = priceNow;
  for (let i = 0; i < 6 + Math.floor(rnd() * 8); i++) {
    snaps.push({ ts: nowTs - i * (3600 * (6 + Math.floor(rnd() * 20))), price: p, available: Math.max(1, Math.round(it.avail * (0.6 + rnd() * 0.8))) });
    p = Math.max(1, Math.round(p * (1 + (rnd() - 0.45) * 0.12)));
  }
  SNAPSHOTS.set(it.id, snaps);
}

// ---------------------------------------------------------------- recipes
const PROFESSIONS = [
  [1, 'First Aid', 'First Aid'],
  [2, 'Blacksmithing', 'Blacksmithing'],
  [3, 'Leatherworking', 'Leatherworking'],
  [4, 'Alchemy', 'Alchemy'],
  [5, 'Herbalism', 'Herbalism'],
  [6, 'Cooking', 'Cooking'],
  [7, 'Mining', 'Mining'],
  [8, 'Tailoring', 'Tailoring'],
  [9, 'Engineering', 'Engineering'],
  [10, 'Enchanting', 'Enchanting'],
  [11, 'Fishing', 'Fishing'],
  [12, 'Skinning', 'Skinning'],
  [13, 'Poisons', 'Poisons'],
];

// [spell_id, spell name, prof, created, num, min, low, high, [[item, count]...]]
const RECIPES = [
  [3275, 'Linen Bandage', 1, 1251, 1, 1, 30, 60, [[2589, 1]]],
  [7929, 'Heavy Silk Bandage', 1, 6451, 1, 165, 185, 205, [[4306, 2]]],
  [18630, 'Heavy Runecloth Bandage', 1, 14530, 1, 290, 310, 330, [[14047, 2]]],
  [2963, 'Bolt of Linen Cloth', 8, 2996, 1, 1, 25, 50, [[2589, 2]]],
  [3839, 'Bolt of Silk Cloth', 8, 4305, 1, 125, 135, 145, [[4306, 4]]],
  [18401, 'Bolt of Runecloth', 8, 14048, 1, 250, 255, 270, [[14047, 4]]],
  [18560, 'Mooncloth', 8, 14342, 1, 250, 290, 310, [[14256, 2]]],
  [2330, 'Minor Healing Potion', 4, 118, 1, 1, 55, 95, [[2447, 1], [765, 1], [3371, 1]]],
  [3447, 'Healing Potion', 4, 929, 1, 55, 110, 150, [[2453, 1], [2450, 1], [3372, 1]]],
  [17556, 'Major Healing Potion', 4, 13446, 1, 275, 290, 310, [[13464, 2], [13465, 1], [8925, 1]]],
  [17580, 'Major Mana Potion', 4, 13444, 1, 295, 310, 330, [[13463, 3], [13467, 2], [8925, 1]]],
  [17571, 'Elixir of the Mongoose', 4, 13452, 1, 280, 295, 315, [[13465, 2], [13466, 2], [8925, 1]]],
  [17635, 'Flask of the Titans', 4, 13510, 1, 300, 315, 335, [[8846, 30], [13423, 10], [13468, 1]]],
  [17187, 'Transmute: Arcanite', 4, 12360, 1, 275, 275, 295, [[12359, 1], [12363, 1]]],
  [16153, 'Smelt Thorium', 7, 12359, 1, 230, 250, 270, [[10620, 1]]],
  [2657, 'Smelt Copper', 7, 2840, 1, 1, 25, 47, [[2770, 1]]],
  [16641, 'Dense Sharpening Stone', 2, 12404, 1, 250, 255, 260, [[12365, 1]]],
  [19800, 'Thorium Grenade', 9, 15993, 3, 280, 300, 320, [[15994, 3], [12359, 3], [14047, 3]]],
  [19791, 'Thorium Widget', 9, 15994, 1, 260, 280, 300, [[12359, 3], [14047, 1]]],
  [20034, 'Enchant Weapon - Crusader', 10, null, 1, 300, 320, 340, [[14344, 4], [12811, 2]]],
  [20025, 'Enchant Chest - Greater Stats', 10, null, 1, 300, 320, 340, [[14344, 4], [20725, 1], [12803, 1]]],
  [2546, 'Spiced Wolf Meat', 6, 2680, 1, 10, 50, 90, [[2672, 1], [2678, 1]]],
  // v4 – tailoring path
  [3755, 'Linen Bag', 8, 4238, 1, 45, 70, 95, [[2996, 3], [2320, 3]]],
  [2397, 'Reinforced Linen Cape', 8, 2580, 1, 60, 85, 110, [[2996, 2], [2321, 3]]],
  [2964, 'Bolt of Woolen Cloth', 8, 2997, 1, 75, 90, 105, [[2592, 3]]],
  [2406, 'Gray Woolen Shirt', 8, 2587, 1, 100, 110, 140, [[2997, 2], [2321, 1], [4340, 1]]],
  [3848, 'Double-stitched Woolen Shoulders', 8, 4314, 1, 110, 125, 160, [[2997, 3], [2321, 2]]],
  [8760, 'Azure Silk Hood', 8, 7048, 1, 145, 155, 180, [[4305, 2], [6260, 2], [2321, 1]]],
  [8762, 'Silk Headband', 8, 7050, 1, 160, 170, 195, [[4305, 3], [2321, 2]]],
  [3871, 'Formal White Shirt', 8, 4334, 1, 170, 175, 200, [[4305, 3], [2324, 2], [4291, 1]]],
  [3865, 'Bolt of Mageweave', 8, 4339, 1, 175, 180, 185, [[4338, 5]]],
  [8799, 'Crimson Silk Pantaloons', 8, 7062, 1, 185, 195, 215, [[4305, 4], [4291, 2], [2604, 2]]],
  [12048, 'Black Mageweave Vest', 8, 9998, 1, 205, 215, 235, [[4339, 2], [4291, 2]]],
  [12053, 'Black Mageweave Gloves', 8, 10003, 1, 215, 225, 245, [[4339, 2], [4291, 2]]],
  [12072, 'Black Mageweave Headband', 8, 10024, 1, 230, 240, 260, [[4339, 3], [8343, 2]]],
  [18402, 'Runecloth Belt', 8, 13856, 1, 255, 270, 290, [[14048, 3], [14341, 1]]],
  [18405, 'Runecloth Bag', 8, 14046, 1, 260, 275, 305, [[14048, 5], [8170, 2], [14341, 1]]],
  [18417, 'Runecloth Gloves', 8, 13863, 1, 275, 290, 315, [[14048, 4], [8170, 4], [14341, 1]]],
  [18422, 'Runecloth Cloak', 8, 13860, 1, 260, 275, 300, [[14048, 4], [14341, 1]]],
  // v4 – alchemy path
  [2337, 'Lesser Healing Potion', 4, 858, 1, 55, 85, 125, [[118, 1], [2450, 1]]],
  [3173, 'Lesser Mana Potion', 4, 3385, 1, 120, 145, 185, [[785, 1], [3820, 1], [3372, 1]]],
  [7181, 'Greater Healing Potion', 4, 1710, 1, 155, 175, 215, [[3357, 1], [3356, 1], [3372, 1]]],
  [11449, 'Elixir of Agility', 4, 8949, 1, 185, 205, 245, [[3820, 1], [3821, 1], [3372, 1]]],
  [11457, 'Superior Healing Potion', 4, 3928, 1, 215, 230, 270, [[8838, 1], [3358, 1], [8925, 1]]],
  [11460, 'Elixir of Detect Undead', 4, 9154, 1, 230, 240, 260, [[8836, 1], [8925, 1]]],
  [11467, 'Elixir of Greater Agility', 4, 9187, 1, 240, 260, 290, [[8838, 1], [3821, 1], [8925, 1]]],
  [17553, 'Superior Mana Potion', 4, 13443, 1, 260, 275, 315, [[8838, 2], [8839, 2], [8925, 1]]],
  [11472, 'Elixir of Giants', 4, 9206, 1, 245, 265, 285, [[8846, 1], [8838, 1], [8925, 1]]],
  // v4 – enchanting
  [7421, 'Runed Copper Rod', 10, 6218, 1, 1, 5, 10, [[2840, 1], [10940, 1]]],
  [7418, 'Enchant Bracer - Minor Health', 10, null, 1, 1, 70, 110, [[10940, 1]]],
  [7457, 'Enchant Bracer - Minor Stamina', 10, null, 1, 50, 80, 120, [[10940, 3]]],
  [7745, 'Enchant 2H Weapon - Minor Impact', 10, null, 1, 100, 130, 170, [[10940, 4], [10938, 1]]],
];

// items referenced only by recipes (no AH data) – still need names
ITEMS.set(13463, { id: 13463, name: 'Dreamfoil', quality: 1, item_level: 54, class_id: 7, subclass_id: 9, vendor_sell: 200, vendor_buy: null, stack: 20, base: null });
ITEMS.set(13467, { id: 13467, name: 'Icecap', quality: 1, item_level: 58, class_id: 7, subclass_id: 9, vendor_sell: 250, vendor_buy: null, stack: 20, base: null });

const POSTINGS = [
  [13446, 1763240000 + 31536000, 34500, 5],
  [13446, 1763150000 + 31536000, 33900, 5],
  [14530, 1763241000 + 31536000, 8900, 20],
  [14048, 1763100000 + 31536000, 18900, 4],
  [12360, 1763220000 + 31536000, 1190000, 1],
  [13452, 1762990000 + 31536000, 105000, 2],
  [14551, 1762900000 + 31536000, 5200000, 1],
  [12404, 1763230000 + 31536000, 3300, 20],
];

// ---------------------------------------------------------------- analytics
function median(arr) {
  if (!arr.length) return null;
  const s = arr.slice().sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : Math.round((s[m - 1] + s[m]) / 2);
}

function baselineFor(id) {
  const cur = CURRENT.get(id);
  const rows = DAILY.get(id);
  if (!cur || !rows) return null;
  const vals = rows.filter((r) => !r.outlier && r.day < cur.last_day && r.day >= cur.last_day - BASELINE_DAYS).map((r) => r.low ?? r.high);
  return vals.length >= 3 ? median(vals) : null;
}

// ---- v4: robust pricing helpers (mirror analytics.buy_price / sell_price / confidence)
function priceFlag(id) {
  const cur = CURRENT.get(id);
  const b = baselineFor(id);
  if (!cur || cur.available > THIN_QTY || !b) return null;
  if (cur.price > b * 2) return 'thin_high';
  if (cur.price < b * 0.5) return 'thin_low';
  return null;
}

function volatility(id) {
  const cur = CURRENT.get(id);
  const rows = (DAILY.get(id) || []).filter((r) => !r.outlier);
  if (!cur) return null;
  const v = rows.filter((r) => r.day > cur.last_day - 30).map((r) => r.low);
  if (v.length < 2) return null;
  const avg = v.reduce((a, b) => a + b, 0) / v.length;
  return Math.sqrt(v.reduce((s, x) => s + (x - avg) ** 2, 0) / v.length) / avg;
}

function confidence(id) {
  const cur = CURRENT.get(id);
  if (!cur) return VENDOR.has(id) ? 'high' : 'low';
  const rows = (DAILY.get(id) || []).filter((r) => !r.outlier);
  const recent = rows.filter((r) => r.day > cur.last_day - 14);
  const avgQ = recent.length ? recent.reduce((s, r) => s + r.available, 0) / recent.length : 0;
  const vol = volatility(id);
  if (rows.length < 3 || avgQ < 2 || priceFlag(id) === 'thin_high') return 'low';
  if (rows.length >= 7 && avgQ >= 5 && vol != null && vol < 0.5) return 'high';
  return 'medium';
}

const CONF_RANK = { high: 0, medium: 1, low: 2 };
function worstConf(list) {
  let w = 'high';
  for (const c of list) if (c && CONF_RANK[c] > CONF_RANK[w]) w = c;
  return w;
}

function ahBuy(id, mode) {
  const cur = CURRENT.get(id);
  if (!cur) return null;
  const b = baselineFor(id);
  if (mode === 'baseline') return b ?? cur.price;
  if (priceFlag(id) === 'thin_high' && b) return b;
  return cur.price;
}

function sellPrice(id, mode = 'current') {
  const cur = CURRENT.get(id);
  if (!cur) return null;
  const b = baselineFor(id);
  if (mode === 'baseline') return b ?? cur.price;
  if (priceFlag(id) === 'thin_high' && b) return b;
  return b ? Math.min(cur.price, Math.round(b * 1.25)) : cur.price;
}

/** Cleaned daily price with forward fill (max 3 days), vendor price when cheaper. */
function priceOn(id, day) {
  let ah = null;
  const rows = DAILY.get(id);
  if (rows) {
    for (let i = rows.length - 1; i >= 0; i--) {
      const r = rows[i];
      if (r.day > day || r.outlier) continue;
      if (r.day >= day - 3) ah = r.low ?? r.high;
      break;
    }
  }
  const v = VENDOR.has(id) ? VENDOR.get(id) : null;
  if (ah == null) return v;
  return v != null ? Math.min(ah, v) : ah;
}

function recipeDay(r, day) {
  const [, , , created, numCreated, , , , regs] = r;
  let cost = 0;
  for (const [iid, count] of regs) {
    const p = priceOn(iid, day);
    if (p == null) return { cost: null, sell: null, profit: null, margin: null };
    cost += p * count;
  }
  if (!created) return { cost, sell: null, profit: null, margin: null };
  const sp = priceOn(created, day);
  if (sp == null) return { cost, sell: null, profit: null, margin: null };
  const sell = sp * numCreated;
  const profit = Math.round(sell * (1 - AH_CUT) - cost);
  return { cost, sell, profit, margin: cost > 0 ? profit / cost : null };
}

function avgNonNull(arr) {
  const v = arr.filter((x) => x != null);
  return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null;
}

function sparkFor(r) {
  const out = [];
  for (let d = LATEST_DAY - 13; d <= LATEST_DAY; d++) out.push(recipeDay(r, d).profit);
  return out;
}

function trendPct(spark) {
  const recent = avgNonNull(spark.slice(-3));
  const before = avgNonNull(spark.slice(-10, -3));
  if (recent == null || before == null || before === 0) return null;
  return Math.round(((recent - before) / Math.abs(before)) * 1000) / 10;
}

function recipeTrend(spellId, days) {
  const r = RECIPES.find((x) => x[0] === spellId);
  if (!r) throw notFound('Unknown recipe');
  const points = [];
  for (let d = LATEST_DAY - days + 1; d <= LATEST_DAY; d++) points.push({ date: dayToDate(d), ...recipeDay(r, d) });
  const profits = points.map((p) => p.profit);
  const createdItem = r[3] ? ITEMS.get(r[3]) : null;
  return {
    spell_id: spellId,
    name: createdItem && createdItem.name ? createdItem.name : r[1],
    points,
    profit_trend_pct: trendPct(sparkFor(r)),
    avg_profit_7: avgNonNull(profits.slice(-7)) != null ? Math.round(avgNonNull(profits.slice(-7))) : null,
    avg_profit_30: avgNonNull(profits.slice(-30)) != null ? Math.round(avgNonNull(profits.slice(-30))) : null,
  };
}

function discountFor(id) {
  const b = baselineFor(id);
  const cur = CURRENT.get(id);
  if (!b || !cur) return null;
  return 1 - cur.price / b;
}

function avgOver(id, days) {
  const cur = CURRENT.get(id);
  const rows = DAILY.get(id) || [];
  if (!cur) return null;
  const v = rows.filter((r) => !r.outlier && r.day > cur.last_day - days).map((r) => r.low);
  return v.length ? Math.round(v.reduce((a, b) => a + b, 0) / v.length) : null;
}

function trend7(id) {
  const rows = (DAILY.get(id) || []).filter((r) => !r.outlier);
  if (rows.length < 4) return null;
  const last = rows[rows.length - 1];
  const prev = rows.filter((r) => r.day <= last.day - 7).pop() || rows[0];
  if (!prev || prev === last) return null;
  return Math.round(((last.low - prev.low) / prev.low) * 1000) / 10;
}

function unitCost(itemId, mode) {
  const ah = ahBuy(itemId, mode);
  const vend = VENDOR.has(itemId) ? VENDOR.get(itemId) : null;
  if (ah == null && vend == null) return { unit_price: null, source: 'none' };
  if (ah == null || (vend != null && vend <= ah)) return { unit_price: vend, source: 'vendor' };
  return { unit_price: ah, source: 'ah' };
}

function isDealItem(id) {
  const d = discountFor(id);
  return d != null && d >= 0.25;
}

function recipeObj(r, mode = 'current') {
  const [spell_id, spellName, profId, created, numCreated, min, low, high, regs] = r;
  const createdItem = created ? ITEMS.get(created) : null;
  let cost = 0;
  let complete = true;
  const reagents = regs.map(([iid, count]) => {
    const it = ITEMS.get(iid) || { name: null, quality: 1 };
    const u = unitCost(iid, mode);
    if (u.unit_price == null) complete = false;
    const total = u.unit_price != null ? u.unit_price * count : null;
    cost += total || 0;
    return {
      item_id: iid, name: it.name, quality: it.quality, count, unit_price: u.unit_price, source: u.source, total, is_deal: isDealItem(iid),
      confidence: u.source === 'vendor' ? 'high' : confidence(iid), price_flag: u.source === 'ah' ? priceFlag(iid) : null,
    };
  });
  let sell = null;
  let profit = null;
  let margin = null;
  const cur = created ? CURRENT.get(created) : null;
  if (cur) {
    const unit = sellPrice(created, mode);
    sell = Math.round(unit * numCreated);
    profit = Math.round(sell * (1 - AH_CUT) - cost);
    margin = cost > 0 ? profit / cost : null;
  }
  const sellConf = created ? confidence(created) : null;
  const spark = sparkFor(r);
  return {
    spell_id,
    profession_id: profId,
    name: createdItem && createdItem.name ? createdItem.name : spellName,
    created_item_id: created,
    created_name: createdItem ? createdItem.name : null,
    created_quality: createdItem ? createdItem.quality : null,
    num_created: numCreated,
    skill: { min, low, high },
    reagents,
    cost,
    cost_complete: complete,
    sell,
    profit,
    margin,
    sell_available: cur ? cur.available : null,
    sell_confidence: sellConf,
    sell_price_flag: created ? priceFlag(created) : null,
    confidence: worstConf([...reagents.map((g) => g.confidence), created ? sellConf : null]),
    spark,
    profit_trend_pct: trendPct(spark),
    can_craft: canCraft(r),
    known: KNOWN.has(spell_id) ? KNOWN.get(spell_id) : null,
  };
}

function profName(id) {
  const p = PROFESSIONS.find((x) => x[0] === id);
  return p ? p[2] : `Profession ${id}`;
}

function itemRow(id) {
  const it = ITEMS.get(id);
  const cur = CURRENT.get(id);
  return {
    id,
    name: it ? it.name : null,
    quality: it ? it.quality : 1,
    price: cur ? cur.price : null,
    available: cur ? cur.available : null,
    baseline: baselineFor(id),
    trend7_pct: trend7(id),
  };
}

// ---------------------------------------------------------------- endpoints
let ingestTs = Math.floor(Date.now() / 1000) - 1260;

function status() {
  return {
    unseen_alerts: ALERTS.filter((a) => !a.seen).length,
    realm: REALM,
    realms: [REALM],
    last_ingest_ts: ingestTs,
    file_mtime: ingestTs - 30,
    items_priced: CURRENT.size,
    latest_day: LATEST_DAY,
    latest_date: dayToDate(LATEST_DAY),
    item_names_loaded: true,
    recipes_loaded: true,
  };
}

function search(q, limit) {
  const ql = q.trim().toLowerCase();
  let rows = [...ITEMS.values()];
  if (ql) {
    rows = rows.filter((it) => String(it.id) === ql || String(it.id).startsWith(ql) || (it.name || '').toLowerCase().includes(ql));
  } else {
    rows = rows.filter((it) => CURRENT.has(it.id)).sort((a, b) => CURRENT.get(b.id).price - CURRENT.get(a.id).price);
  }
  return rows.slice(0, limit).map((it) => {
    const cur = CURRENT.get(it.id);
    return {
      id: it.id, name: it.name, quality: it.quality,
      price: cur ? cur.price : null, available: cur ? cur.available : null,
      last_date: cur ? dayToDate(cur.last_day) : null,
      baseline: baselineFor(it.id), discount: discountFor(it.id),
      confidence: cur ? confidence(it.id) : null, price_flag: priceFlag(it.id),
    };
  });
}

function item(id) {
  const it = ITEMS.get(id);
  if (!it) {
    const e = new Error('Unknown item (404)');
    e.status = 404;
    throw e;
  }
  const cur = CURRENT.get(id);
  const rows = DAILY.get(id) || [];
  const last30 = cur ? rows.filter((r) => !r.outlier && r.day > cur.last_day - 30).map((r) => r.low) : [];
  const avg30 = avgOver(id, 30);
  const sd = last30.length > 1 ? Math.sqrt(last30.reduce((s, v) => s + (v - avg30) ** 2, 0) / last30.length) : null;
  return {
    id,
    name: it.name,
    quality: it.quality,
    item_level: it.item_level,
    class_id: it.class_id,
    vendor_sell: it.vendor_sell,
    vendor_buy: it.vendor_buy,
    current: cur
      ? {
          price: cur.price, available: cur.available, last_date: dayToDate(cur.last_day), stale: cur.last_day < LATEST_DAY - 2,
          confidence: confidence(id), price_flag: priceFlag(id),
        }
      : { price: null, available: null, last_date: null, stale: true, confidence: null, price_flag: null },
    stats: {
      baseline: baselineFor(id),
      avg7: avgOver(id, 7),
      avg30,
      min30: last30.length ? Math.min(...last30) : null,
      max30: last30.length ? Math.max(...last30) : null,
      median30: median(last30),
      days_of_data: rows.filter((r) => !r.outlier).length,
      outlier_days: rows.filter((r) => r.outlier).length,
      volatility: sd != null && avg30 ? sd / avg30 : null,
      trend7_pct: trend7(id),
    },
    history: rows.map((r) => ({ date: dayToDate(r.day), low: r.low, high: r.high, available: r.available, ...(r.outlier ? { outlier: true } : {}) })),
    snapshots: SNAPSHOTS.get(id) || [],
    crafted_by: RECIPES.filter((r) => r[3] === id).map((r) => recipeObj(r)),
    used_in: RECIPES.filter((r) => r[8].some(([iid]) => iid === id)).map((r) => ({
      spell_id: r[0], name: r[1], profession_id: r[2], profession: profName(r[2]), count: r[8].find(([iid]) => iid === id)[1],
    })),
    postings: POSTINGS.filter((p) => p[0] === id).map((p) => ({ ts: p[1], price: p[2], quantity: p[3] })),
    watch: WATCH.has(id) ? { max_price: WATCH.get(id).max_price, enabled: WATCH.get(id).enabled, note: WATCH.get(id).note } : null,
  };
}

// name -> [skill, where, kind] for the gathering lists (a few of the real ones, see ahtool/gathering.py)
const GATHER = {
  5: [['Peacebloom', 1, 'Starting zones', 'herb'], ['Silverleaf', 1, 'Starting zones', 'herb'],
    ['Mageroyal', 50, 'Barrens, Westfall, Darkshore, Silverpine', 'herb'], ['Briarthorn', 70, 'Barrens, Darkshore, Redridge, Hillsbrad', 'herb'],
    ['Stranglekelp', 85, 'Under water along coasts', 'herb'], ['Bruiseweed', 100, 'Barrens, Ashenvale, Wetlands, Hillsbrad', 'herb'],
    ['Kingsblood', 125, 'Ashenvale, Hillsbrad, Arathi, Stranglethorn', 'herb'], ['Liferoot', 150, 'Near water: Wetlands, Arathi, Stranglethorn, Dustwallow', 'herb'],
    ['Goldthorn', 170, 'Arathi, Stranglethorn, Badlands, Feralas', 'herb'], ["Khadgar's Whisker", 185, 'Arathi, Stranglethorn, Swamp of Sorrows, Feralas', 'herb'],
    ["Arthas' Tears", 220, 'Felwood, Plaguelands', 'herb'], ['Sungrass', 230, "Feralas, Hinterlands, Un'Goro, Silithus, Plaguelands", 'herb'],
    ['Blindweed', 235, "Swamp of Sorrows, Un'Goro", 'herb'], ['Gromsblood', 250, 'Felwood, Desolace, Blasted Lands, Ashenvale', 'herb'],
    ['Golden Sansam', 260, "Feralas, Azshara, Un'Goro, Burning Steppes", 'herb'], ['Mountain Silversage', 280, "Winterspring, Un'Goro, Azshara, Burning Steppes", 'herb'],
    ['Plaguebloom', 285, 'Plaguelands, Felwood', 'herb'], ['Black Lotus', 300, 'Rare: Winterspring, Burning Steppes, Silithus, Eastern Plaguelands', 'herb']],
  7: [['Copper Ore', 1, 'Copper vein: starting zones, Barrens, Westfall', 'ore'], ['Aquamarine', 155, 'Gold and mithril deposits (gem)', 'gem'],
    ['Thorium Ore', 245, 'Small thorium vein (245), rich thorium vein (275)', 'ore'], ['Dense Stone', 245, 'Thorium veins', 'stone'],
    ['Arcane Crystal', 275, 'Rich thorium vein (rare)', 'gem']],
  11: [['Raw Brilliant Smallfish', 1, 'Inland waters of the starting zones', 'fish'], ['Stonescale Eel', 205, 'Schools along coasts: Tanaris, Feralas, Azshara', 'fish']],
  12: [['Light Leather', 1, 'Beasts up to about level 25', 'leather'], ['Rugged Leather', 250, 'Beasts about level 50-60', 'leather']],
};
const GATHER_APPROX = new Set([11, 12]);

function gatherRows(pid) {
  const byName = new Map([...ITEMS.values()].map((it) => [it.name, it.id]));
  return (GATHER[pid] || []).filter(([n]) => byName.has(n)).map(([n, skill, where, kind]) => {
    const id = byName.get(n);
    const r = itemRow(id);
    const base = r.baseline;
    return { ...r, discount: base && r.price ? (base - r.price) / base : null, confidence: 'high', price_flag: null,
      skill, skill_approx: GATHER_APPROX.has(pid), where, kind, vendor_sell: ITEMS.get(id).vendor_sell };
  });
}

function professions() {
  return PROFESSIONS.map(([id, name, name_no]) => {
    const recs = RECIPES.filter((r) => r[2] === id).map((r) => recipeObj(r));
    const profits = recs.filter((r) => r.profit != null && r.cost_complete).map((r) => r.profit);
    const reliable = recs.filter((r) => r.confidence !== 'low');
    const relProfits = reliable.filter((r) => r.profit != null && r.cost_complete).map((r) => r.profit);
    return {
      id, name, name_no,
      recipe_count: recs.length,
      priced_count: recs.filter((r) => r.cost_complete).length,
      profitable_count: profits.filter((p) => p > 0).length,
      best_profit: profits.length ? Math.max(...profits) : null,
      reliable_count: reliable.length,
      profitable_reliable_count: relProfits.filter((p) => p > 0).length,
      best_profit_reliable: relProfits.length ? Math.max(...relProfits) : null,
      mine: MY_PROFS.has(id),
      my_skill: MY_PROFS.has(id) ? MY_PROFS.get(id) : null,
      gather_count: gatherRows(id).length,
      gather_priced: gatherRows(id).filter((r) => r.price).length,
      gather_top_price: Math.max(0, ...gatherRows(id).map((r) => r.price || 0)) || null,
    };
  });
}

function profession(id, mode, mine) {
  return RECIPES.filter((r) => r[2] === id && (!mine || canCraft(r))).map((r) => recipeObj(r, mode));
}

function deals(threshold, minValue, reason, includeLow) {
  const out = [];
  for (const [id, cur] of CURRENT) {
    const it = ITEMS.get(id);
    const baseline = baselineFor(id);
    const discount = baseline ? 1 - cur.price / baseline : null;
    const reasons = [];
    let vendor_profit = null;
    if (it.vendor_sell && cur.price < it.vendor_sell) {
      reasons.push('under_vendor');
      vendor_profit = it.vendor_sell - cur.price;
    }
    const profs = [...new Set(RECIPES.filter((r) => r[8].some(([iid]) => iid === id)).map((r) => profName(r[2])))];
    const conf = confidence(id);
    if (discount != null && discount >= threshold && (includeLow || conf !== 'low')) {
      reasons.push('below_baseline');
      if (profs.length) reasons.push('cheap_reagent');
    }
    if (!reasons.length) continue;
    if (minValue && (baseline ?? cur.price) < minValue) continue;
    if (reason && !reasons.includes(reason)) continue;
    const score = reasons.includes('under_vendor')
      ? 100 + vendor_profit / 100
      : discount * Math.log10(baseline + 1);
    out.push({
      item_id: id, name: it.name, quality: it.quality, price: cur.price, baseline, discount,
      available: cur.available, days_of_data: (DAILY.get(id) || []).length, reasons, vendor_profit,
      professions: reasons.includes('cheap_reagent') ? profs : [], score: Math.round(score * 1000) / 1000,
      for_my_crafts: myReagentIds().has(id),
      confidence: conf, price_flag: priceFlag(id),
    });
  }
  return out.sort((a, b) => b.score - a.score);
}

function market() {
  const rows = [...CURRENT.keys()].map(itemRow);
  const byQ = new Map();
  const byC = new Map();
  let value = 0;
  let qty = 0;
  for (const r of rows) {
    const it = ITEMS.get(r.id);
    const v = r.price * r.available;
    value += v;
    qty += r.available;
    const q = byQ.get(r.quality) || { quality: r.quality, count: 0, value: 0 };
    q.count++; q.value += v; byQ.set(r.quality, q);
    const cid = it.class_id;
    const c = byC.get(cid) || { class_id: cid, class_name: CLASS_NAMES[cid] || 'Unknown', count: 0, value: 0 };
    c.count++; c.value += v; byC.set(cid, c);
  }
  const withTrend = rows.filter((r) => r.trend7_pct != null);
  const days = new Map();
  for (const list of DAILY.values()) for (const d of list) days.set(d.day, (days.get(d.day) || 0) + 1);
  return {
    totals: { items: rows.length, listed_quantity: qty, market_value: value },
    by_quality: [...byQ.values()].sort((a, b) => a.quality - b.quality),
    by_class: [...byC.values()].sort((a, b) => b.value - a.value),
    movers_up: withTrend.filter((r) => r.trend7_pct > 0).sort((a, b) => b.trend7_pct - a.trend7_pct).slice(0, 15),
    movers_down: withTrend.filter((r) => r.trend7_pct < 0).sort((a, b) => a.trend7_pct - b.trend7_pct).slice(0, 15),
    most_listed: rows.slice().sort((a, b) => b.available - a.available).slice(0, 15),
    most_valuable: rows.slice().sort((a, b) => b.price - a.price).slice(0, 15),
    scan_days: [...days.entries()].sort((a, b) => a[0] - b[0]).map(([d, n]) => ({ date: dayToDate(d), items: n })),
  };
}

function postings() {
  return POSTINGS.map(([id, ts, price, quantity]) => {
    const it = ITEMS.get(id);
    const cur = CURRENT.get(id);
    return {
      item_id: id, name: it ? it.name : null, ts, price, quantity,
      current_price: cur ? cur.price : null,
      diff_pct: cur ? Math.round(((price - cur.price) / cur.price) * 1000) / 10 : null,
    };
  });
}

// ---------------------------------------------------------------- v2 user state (in-memory, stateful)
const EMPTY = new URLSearchParams(typeof location !== 'undefined' ? location.search : '').get('mockempty') === '1';
const NOW = () => Math.floor(Date.now() / 1000);

const MY_PROFS = new Map(EMPTY ? [] : [[4, 300], [8, 265]]); // profession_id -> skill
const KNOWN = new Map(EMPTY ? [] : [[17635, 0], [18560, 1]]); // spell_id -> 1|0
const SHOP = new Map(EMPTY ? [] : [[17556, 10], [18401, 4], [2330, 20]]); // spell_id -> qty
const WATCH = new Map();
const ALERTS = [];
let alertSeq = 0;
const SETTINGS = { notify_windows: true, notify_vendor_flip_min: 0, notify_my_reagents: false, shopping_max_markup: 0.15 };

function moneyPlain(c) {
  const g = Math.floor(c / 10000);
  const s = Math.floor((c % 10000) / 100);
  const cc = c % 100;
  return (g ? g + 'g ' : '') + (g || s ? s + 's ' : '') + cc + 'c';
}

function alertMessage(kind, it, price, thr) {
  const name = it && it.name ? it.name : `Item #${it ? it.id : '?'}`;
  if (kind === 'watch' && thr != null) return `${name} ${moneyPlain(price)} ≤ ${moneyPlain(thr)}`;
  if (kind === 'watch') return `${name} is a deal at ${moneyPlain(price)}`;
  if (kind === 'vendor_flip') return `${name} ${moneyPlain(price)} – under vendor price (+${moneyPlain(Math.max(0, (it.vendor_sell || 0) - price))})`;
  if (kind === 'my_reagent') return `${name} ${moneyPlain(price)} – cheap reagent for your crafts`;
  return `${name} ${moneyPlain(price)}`;
}

if (!EMPTY) {
  const t0 = NOW();
  WATCH.set(13464, { max_price: 10000, enabled: 1, note: 'Major Healing Potion mats', created_at: t0 - 86400 * 3, last_alert_price: 8900, last_alert_ts: t0 - 1300 });
  WATCH.set(13468, { max_price: 1000000, enabled: 1, note: '', created_at: t0 - 86400 * 6, last_alert_price: null, last_alert_ts: null });
  WATCH.set(12363, { max_price: null, enabled: 1, note: 'Any deal', created_at: t0 - 86400, last_alert_price: null, last_alert_ts: null });
  WATCH.set(14047, { max_price: 2500, enabled: 0, note: 'Bandages', created_at: t0 - 86400 * 9, last_alert_price: null, last_alert_ts: null });
  const seed = [
    [13464, 'watch', 0.62, 10000, t0 - 1300, 0],
    [2589, 'vendor_flip', 0.12, 1, t0 - 1320, 0],
    [12363, 'watch', 0.68, null, t0 - 1340, 0],
    [8846, 'my_reagent', 0.7, 3600, t0 - 86400 - 200, 1],
    [13464, 'watch', 0.7, 10000, t0 - 86400 * 2, 1],
  ];
  for (const [id, kind, f, thr, ts, seen] of seed) {
    const it = ITEMS.get(id);
    const price = Math.round(it.base * f);
    ALERTS.push({ id: ++alertSeq, ts, item_id: id, kind, price, threshold: thr, message: alertMessage(kind, it, price, thr), seen: !!seen });
  }
}

function canCraft(r) {
  const [spell_id, , profId, , , min] = r;
  if (!MY_PROFS.has(profId)) return false;
  if (KNOWN.has(spell_id)) return KNOWN.get(spell_id) === 1;
  return !min || min <= MY_PROFS.get(profId);
}

function myReagentIds() {
  const ids = new Set();
  for (const r of RECIPES) if (canCraft(r)) for (const [iid] of r[8]) ids.add(iid);
  return ids;
}

function badRequest(msg) {
  const e = new Error(msg + ' (400)');
  e.status = 400;
  return e;
}

function notFound(msg) {
  const e = new Error(msg + ' (404)');
  e.status = 404;
  return e;
}

function myProfessions() {
  return [...MY_PROFS.entries()].map(([profession_id, skill]) => ({ profession_id, name: profName(profession_id), skill }));
}

function myCrafts(mode, limit) {
  return RECIPES.filter(canCraft)
    .map((r) => ({ ...recipeObj(r, mode), profession_name: profName(r[2]) }))
    .sort((a, b) => (b.profit ?? -Infinity) - (a.profit ?? -Infinity))
    .slice(0, limit);
}

function shopping() {
  const recipes = [];
  const need = new Map();
  const totals = { cost: 0, sell: 0, profit: 0 };
  for (const [spell, qty] of SHOP) {
    const raw = RECIPES.find((x) => x[0] === spell);
    if (!raw) continue;
    const r = recipeObj(raw);
    recipes.push({
      spell_id: r.spell_id, name: r.name, created_item_id: r.created_item_id, created_quality: r.created_quality,
      profession_name: profName(raw[2]), qty, cost_each: r.cost, sell_each: r.sell, profit_each: r.profit,
      total_cost: r.cost * qty, total_profit: r.profit != null ? r.profit * qty : null, cost_complete: r.cost_complete,
      spark: r.spark, profit_trend_pct: r.profit_trend_pct, confidence: r.confidence,
    });
    totals.cost += r.cost * qty;
    totals.sell += (r.sell || 0) * qty;
    if (r.profit != null) totals.profit += r.profit * qty;
    for (const [iid, count] of raw[8]) need.set(iid, (need.get(iid) || 0) + count * qty);
  }
  const all = [];
  const unexportable = [];
  const terms = [];
  for (const [iid, n] of need) {
    const it = ITEMS.get(iid) || { name: null, quality: 1 };
    const u = unitCost(iid);
    const cur = CURRENT.get(iid);
    const baseline = baselineFor(iid);
    const row = {
      item_id: iid, name: it.name, quality: it.quality, need: n, unit_price: u.unit_price, source: u.source,
      total: u.unit_price != null ? u.unit_price * n : null, available: cur ? cur.available : null, baseline, is_deal: isDealItem(iid),
      confidence: u.source === 'ah' ? confidence(iid) : u.source === 'vendor' ? 'high' : 'low', price_flag: u.source === 'ah' ? priceFlag(iid) : null,
    };
    all.push(row);
    if (u.source === 'ah' && it.name) {
      const maxG = Math.round(((Math.max(u.unit_price, baseline || 0) * (1 + SETTINGS.shopping_max_markup)) / 10000) * 10000) / 10000;
      terms.push(`"${it.name}";;0;0;0;0;0;0;0;${maxG};;;;${n}`);
    } else if (u.source !== 'vendor') {
      unexportable.push({ ...row, reason: !it.name ? 'Unknown item name' : 'No AH price' });
    }
  }
  all.sort((a, b) => (b.total || 0) - (a.total || 0));
  return {
    recipes, reagents: all, totals,
    auctionator_import: terms.length ? `AH Tool^${terms.join('^')}` : '',
    vendor_reagents: all.filter((r) => r.source === 'vendor'), unexportable,
  };
}

function watchlist() {
  return [...WATCH.entries()].map(([id, w]) => {
    const it = ITEMS.get(id) || { name: null, quality: 1 };
    const cur = CURRENT.get(id);
    const price = cur ? cur.price : null;
    const discount = discountFor(id);
    const deal = (discount != null && discount >= 0.25) || (cur && it.vendor_sell && cur.price < it.vendor_sell);
    const triggered = !!w.enabled && price != null && (w.max_price != null ? price <= w.max_price : !!deal);
    return {
      item_id: id, name: it.name, quality: it.quality, max_price: w.max_price, enabled: w.enabled, note: w.note,
      price, baseline: baselineFor(id), discount, available: cur ? cur.available : null, triggered,
    };
  });
}

function setWatch(id, body) {
  if (!ITEMS.has(id)) throw badRequest('Unknown item');
  if (!body || typeof body !== 'object') throw badRequest('Body must be an object');
  const w = WATCH.get(id) || { max_price: null, enabled: 1, note: '', created_at: NOW(), last_alert_price: null, last_alert_ts: null };
  if ('max_price' in body) {
    if (body.max_price !== null && !(Number.isInteger(body.max_price) && body.max_price >= 0)) throw badRequest('max_price must be copper ≥ 0 or null');
    w.max_price = body.max_price;
  }
  if ('enabled' in body) w.enabled = body.enabled ? 1 : 0;
  if ('note' in body) w.note = body.note == null ? '' : String(body.note);
  WATCH.set(id, w);
  return { ok: true };
}

function alerts(limit) {
  return ALERTS.slice().sort((a, b) => b.ts - a.ts || b.id - a.id).slice(0, limit).map((a) => {
    const it = ITEMS.get(a.item_id) || { name: null, quality: 1 };
    return { ...a, name: it.name, quality: it.quality };
  });
}

/** Evaluate watch rules like the backend does after an ingest. Watched prices wiggle each time. */
function evaluateAlerts() {
  const t = NOW();
  let fired = 0;
  for (const [id, w] of WATCH) {
    if (!w.enabled) continue;
    const cur = CURRENT.get(id);
    if (!cur) continue;
    cur.price = Math.max(1, Math.round(cur.price * (0.93 + Math.random() * 0.1)));
    const it = ITEMS.get(id);
    const d = discountFor(id);
    const cond = w.max_price != null ? cur.price <= w.max_price : (d != null && d >= 0.25) || (it.vendor_sell && cur.price < it.vendor_sell);
    if (!cond) {
      w.last_alert_price = null;
      w.last_alert_ts = null;
      continue;
    }
    if (w.last_alert_price == null || cur.price < w.last_alert_price || t - w.last_alert_ts > 86400) {
      ALERTS.push({ id: ++alertSeq, ts: t, item_id: id, kind: 'watch', price: cur.price, threshold: w.max_price, message: alertMessage('watch', it, cur.price, w.max_price), seen: false });
      w.last_alert_price = cur.price;
      w.last_alert_ts = t;
      fired++;
    }
  }
  return fired;
}

// ---------------------------------------------------------------- tooltip (/api/tooltip/<id>)
const MOCK_ICONS = {
  2589: 'inv_fabric_linen_01', 2996: 'inv_fabric_linen_02', 4306: 'inv_fabric_silk_01', 14047: 'inv_fabric_purplefire_01',
  13446: 'inv_potion_54', 118: 'inv_potion_49', 13464: 'inv_misc_herb_sansamroot', 13468: 'inv_misc_herb_blacklotus',
  8925: 'inv_drink_06', 3371: 'inv_drink_06',
};
// Rich static tooltip lines for a few showcase items (the real backend builds these from wago.tools + Forever data).
const MOCK_TT_LINES = {
  13446: [
    ['Requires Level 45'], ['Item Level 55', 'yellow'],
    ['Use: Restores 1050 to 1750 health. (2 Min Cooldown)', 'green'],
  ],
  14342: [['Item Level 60', 'yellow'], ['"This cloth glows faintly with moonlight."', 'yellow']],
  284712: [['Binds when equipped'], ['Head', 'white', 'Leather'], ['Requires Level 31'], ['Item Level 36', 'yellow']],
};

function tooltipMock(id) {
  const it = ITEMS.get(id);
  if (!it) return null;
  const cur = CURRENT.get(id);
  const lines = [];
  const add = (text, style = 'white', right) => lines.push(right ? { text, style, right } : { text, style });
  if (MOCK_TT_LINES[id]) MOCK_TT_LINES[id].forEach(([t, st, r]) => add(t, st, r));
  else if (it.item_level) add(`Item Level ${it.item_level}`, 'yellow');
  if (it.vendor_sell) lines.push({ text: 'Sell Price:', style: 'white', money: it.vendor_sell });
  const usedIn = RECIPES.filter((r) => r[8].some(([iid]) => iid === id));
  const profsUsed = [...new Set(usedIn.map((r) => profName(r[2])))];
  const brief = (r) => {
    const o = recipeObj(r);
    return {
      spell_id: o.spell_id, profession: profName(o.profession_id), skill: o.skill.min, num_created: o.num_created,
      reagents: o.reagents.map((g) => ({ item_id: g.item_id, name: g.name, quality: g.quality, count: g.count, unit_price: g.unit_price, source: g.source })),
      cost: o.cost, cost_complete: o.cost_complete, profit: o.profit,
    };
  };
  const icon = MOCK_ICONS[id] || 'inv_misc_questionmark';
  return {
    id, name: it.name, quality: it.quality, icon, icon_url: `https://wow.zamimg.com/images/wow/icons/medium/${icon}.jpg`,
    item_level: it.item_level || null, required_level: null, custom: !it.name || id === 284712,
    source: it.name ? ['csv'] : ['custom'],
    lines,
    ah: {
      price: cur ? cur.price : null, available: cur ? cur.available : null,
      last_date: cur ? dayToDate(cur.last_day) : null, stale: cur ? cur.last_day < LATEST_DAY - 2 : null,
      baseline: baselineFor(id), discount: discountFor(id), days_of_data: (DAILY.get(id) || []).length,
      used_in: { count: usedIn.length, professions: profsUsed },
      crafted_by: RECIPES.filter((r) => r[3] === id).slice(0, 3).map(brief),
      teaches: null,
    },
  };
}

// ---------------------------------------------------------------- v4: trending crafts
function trendingCrafts(limit, mine, includeLow) {
  const all = RECIPES.filter((r) => !mine || canCraft(r))
    .map((r) => ({ ...recipeObj(r), profession_name: profName(r[2]) }))
    .filter((r) => r.profit_trend_pct != null && (includeLow || r.confidence !== 'low'));
  const avgP = (r) => avgNonNull(r.spark) ?? -Infinity;
  return {
    up: all.filter((r) => r.profit_trend_pct > 0 && avgP(r) > 0).sort((a, b) => b.profit_trend_pct - a.profit_trend_pct).slice(0, limit),
    down: all.filter((r) => r.profit_trend_pct < 0).sort((a, b) => a.profit_trend_pct - b.profit_trend_pct).slice(0, limit),
  };
}

// ---------------------------------------------------------------- v4: leveling planner (template based)
// The real planner searches every recipe; the mock walks a hand-made route per profession and
// prices it with the same rules (skill-up chance, make-vs-buy, recovery, recipe purchases).
const LEVEL_ROUTES = {
  8: {
    steps: [
      [1, 45, 2963], [45, 70, 3755], [70, 75, 2397], [75, 105, 2964], [105, 110, 2406], [110, 125, 3848],
      [125, 145, 3839], [145, 160, 8760], [160, 170, 8762], [170, 175, 3871], [175, 185, 3865], [185, 205, 8799],
      [205, 215, 12048], [215, 230, 12053], [230, 250, 12072], [250, 260, 18401], [260, 275, 18402],
      [275, 285, 18405], [285, 300, 18417],
    ],
    drops: { 18402: { spell: 18422, item: 14472 } }, // with sources=all the Runecloth Cloak pattern is cheaper
  },
  4: {
    steps: [
      [1, 60, 2330], [60, 110, 2337], [110, 140, 3447], [140, 155, 3173], [155, 185, 7181], [185, 215, 11449],
      [215, 230, 11457], [230, 250, 11460], [250, 265, 11467], [265, 285, 17553], [285, 300, 17556],
    ],
  },
  10: {
    steps: [[1, 90, 7418], [90, 110, 7457]],
    tools: [7421],
    stop: 110,
    stopWhy: 'Enchant 2H Weapon - Minor Impact needs Lesser Magic Essence, which has no AH price',
  },
};

const MILESTONES = [
  [50, 'Journeyman – train at 50 (requires level 10)'],
  [75, 'Apprentice cap – Journeyman needed to go past 75'],
  [125, 'Expert – train at 125 (requires level 20)'],
  [150, 'Journeyman cap – Expert needed to go past 150'],
  [200, 'Artisan – train at 200 (requires level 35)'],
  [225, 'Expert cap – Artisan needed to go past 225'],
];

function skillChance(s, low, high) {
  if (s < low) return 1;
  if (s < high) return (high - s) / (high - low);
  return 0;
}

function craftCost(spellId, mode, seen = new Set()) {
  const r = RECIPES.find((x) => x[0] === spellId);
  if (!r || seen.has(spellId)) return null;
  seen.add(spellId);
  let cost = 0;
  for (const [iid, count] of r[8]) {
    const u = levelUnit(iid, r[2], mode, seen);
    if (u.unit_price == null) return null;
    cost += u.unit_price * count;
  }
  return Math.round(cost / (r[4] || 1));
}

/** Make vs buy: cheapest of AH/vendor and crafting it in the same profession. */
function levelUnit(iid, profId, mode, seen = new Set()) {
  const buy = unitCost(iid, mode);
  const maker = RECIPES.find((x) => x[2] === profId && x[3] === iid);
  if (maker) {
    const c = craftCost(maker[0], mode, new Set(seen));
    if (c != null && (buy.unit_price == null || c < buy.unit_price)) return { unit_price: c, source: 'craft', spell_id: maker[0] };
  }
  return buy;
}

function levelingPlan(profId, opts) {
  const { mode, recover, sources } = opts;
  const prof = PROFESSIONS.find((p) => p[0] === profId);
  if (!prof) throw notFound('Unknown profession');
  const start = Math.max(1, Math.min(374, opts.start));
  const target = Math.max(start + 1, Math.min(375, opts.target));
  const route = LEVEL_ROUTES[profId];
  const warnings = [];
  const steps = [];
  const recipesToBuy = [];
  const need = new Map(); // item -> {need, unit, source, spell}
  let reached = start;
  if (route) {
    for (const [from, to, baseSpell] of route.steps) {
      const a = Math.max(from, start);
      const b = Math.min(to, target);
      if (a >= b) continue;
      let spell = baseSpell;
      const drop = sources === 'all' && route.drops && route.drops[baseSpell];
      if (drop) spell = drop.spell;
      const raw = RECIPES.find((x) => x[0] === spell);
      const o = recipeObj(raw, mode);
      const [, , , created, num, min, low, high] = raw;
      let expected = 0;
      for (let s = a; s < b; s++) expected += 1 / skillChance(s, low, high);
      const crafts = Math.ceil(expected - 1e-9);
      const reagents = raw[8].map(([iid, count]) => {
        const it = ITEMS.get(iid) || { name: null, quality: 1 };
        const u = levelUnit(iid, profId, mode);
        return {
          item_id: iid, name: it.name, quality: it.quality, count, unit_price: u.unit_price, source: u.source,
          confidence: u.source === 'ah' ? confidence(iid) : 'high', _spell: u.spell_id,
          ...(u.source === 'craft' ? { craft_spell_id: u.spell_id, craft_name: (RECIPES.find((x) => x[0] === u.spell_id) || [])[1] || null } : {}),
        };
      });
      const costEach = reagents.reduce((s, g) => s + (g.unit_price || 0) * g.count, 0);
      let recoverEach = 0;
      let recoverSource = null;
      const createdIt = created ? ITEMS.get(created) : null;
      if (created && recover === 'sell') {
        const sp = sellPrice(created, mode);
        if (sp != null && confidence(created) !== 'low') {
          recoverEach = Math.round(sp * num * (1 - AH_CUT));
          recoverSource = 'ah';
        } else {
          recoverEach = (createdIt.vendor_sell || 0) * num;
          recoverSource = 'vendor';
          warnings.push(`${createdIt.name}: too little sale data – vendor price used for recovery.`);
        }
      } else if (created && recover === 'vendor') {
        recoverEach = (createdIt.vendor_sell || 0) * num;
        recoverSource = 'vendor';
      }
      const conf = worstConf([...reagents.map((g) => g.confidence), created && recover === 'sell' ? confidence(created) : null]);
      if (conf === 'low') warnings.push(`${o.name} (${a}–${b}): thin price data – check the AH before buying.`);
      for (const g of reagents) {
        const e = need.get(g.item_id) || { need: 0, unit: g.unit_price, source: g.source, spell: g._spell };
        e.need += g.count * crafts;
        need.set(g.item_id, e);
        delete g._spell;
      }
      if (drop) {
        const it = ITEMS.get(drop.item);
        const u = unitCost(drop.item, mode);
        recipesToBuy.push({
          item_id: drop.item, name: it.name, quality: it.quality, price: u.unit_price, spell_id: spell, source: u.source,
          confidence: confidence(drop.item), recipe_name: o.name, skill: min,
        });
      }
      steps.push({
        from: a, to: b, spell_id: spell, name: o.name, created_item_id: created, created_quality: o.created_quality,
        skill: { min, low, high }, crafts_expected: Math.round(expected * 10) / 10, crafts, num_created: num,
        chance: { start: Math.round(skillChance(a, low, high) * 100) / 100, end: Math.round(skillChance(b - 1, low, high) * 100) / 100 },
        recover_source: recoverSource, recipe_item: drop ? drop.item : null, kept: 0, reuse_saving: 0,
        // mock market-depth cap: products beyond 3x the listed quantity go to the vendor
        ...(recoverSource === 'ah' && CURRENT.get(created) && crafts * num > CURRENT.get(created).available * 3
          ? { ah_units: CURRENT.get(created).available * 3, vendor_units: crafts * num - CURRENT.get(created).available * 3 }
          : {}),
        cost_each: costEach, recover_each: recoverEach, net_each: costEach - recoverEach,
        cost_total: costEach * crafts, recover_total: recoverEach * crafts, net_total: (costEach - recoverEach) * crafts,
        confidence: conf, reagents,
      });
      reached = b;
    }
  }
  let unreachable = null;
  if (reached < target) {
    unreachable = reached;
    if (route && route.stop && reached >= route.stop) warnings.push(`No usable priced recipe from skill ${reached}: ${route.stopWhy}.`);
    else if (!route) warnings.push(`No usable recipe with skill colours and prices from skill ${reached}.`);
    else warnings.push(`No usable priced recipe from skill ${reached}.`);
  }
  // shopping: buy what is bought; intermediates you craft are expanded into their materials
  const shop = new Map();
  const addShop = (iid, n, unit, source) => {
    const e = shop.get(iid) || { need: 0, unit, source };
    e.need += n;
    shop.set(iid, e);
  };
  const subcrafts = [];
  for (const [iid, e] of need) {
    if (e.source !== 'craft') addShop(iid, e.need, e.unit, e.source);
    if (e.source === 'craft') {
      const r = RECIPES.find((x) => x[0] === e.spell);
      const times = Math.ceil(e.need / (r[4] || 1));
      const it = ITEMS.get(iid) || { name: null, quality: 1 };
      subcrafts.push({ spell_id: e.spell, name: r[1], item_id: iid, quality: it.quality, num_created: r[4], need: e.need, crafts: times, unit_cost: e.unit });
      for (const [sub, c] of r[8]) {
        const u = unitCost(sub, mode);
        addShop(sub, c * times, u.unit_price, u.source);
      }
    }
  }
  const shopping = [...shop.entries()].map(([iid, e]) => {
    const it = ITEMS.get(iid) || { name: null, quality: 1 };
    const cur = CURRENT.get(iid);
    return {
      item_id: iid, name: it.name, quality: it.quality, need: e.need, unit_price: e.unit, source: e.source,
      total: e.unit != null ? e.unit * e.need : null, available: cur ? cur.available : null,
      confidence: e.source === 'ah' ? confidence(iid) : 'high',
    };
  }).sort((a, b) => (a.source === 'craft') - (b.source === 'craft') || (b.total || 0) - (a.total || 0));
  const recipeCost = recipesToBuy.reduce((s, r) => s + (r.price || 0), 0);
  const totals = {
    cost: steps.reduce((s, x) => s + x.cost_total, 0) + recipeCost,
    recover: steps.reduce((s, x) => s + x.recover_total, 0),
    crafts: steps.reduce((s, x) => s + x.crafts, 0),
    crafts_expected: Math.round(steps.reduce((s, x) => s + x.crafts_expected, 0) * 10) / 10,
    recipes: recipeCost,
    subcrafts: subcrafts.reduce((s, x) => s + x.crafts, 0),
  };
  const tools = ((route && route.tools) || []).map((sp) => {
    const r = RECIPES.find((x) => x[0] === sp);
    const it = ITEMS.get(r[3]) || { name: null };
    return { spell_id: sp, name: it.name || r[1], item_id: r[3], skill: r[5], cost: r[8].reduce((s, [iid, c]) => s + (unitCost(iid, mode).unit_price || 0) * c, 0) };
  }).filter((t) => t.skill < target);
  totals.net = totals.cost - totals.recover;
  return {
    profession_id: profId, profession: prof[1], start, target, mode, recover, sources, known_only: !!opts.known_only,
    steps, shopping, recipes_to_buy: recipesToBuy, subcrafts, tools,
    milestones: MILESTONES.filter(([s]) => s > start && s < target).map(([skill, text]) => ({ skill, text })),
    totals, warnings: [...new Set(warnings)], unreachable_from: unreachable,
  };
}

function levelOpts(q) {
  const mine = MY_PROFS.get(Number(q.get('_prof')));
  return {
    start: q.has('start') && q.get('start') !== '' ? Number(q.get('start')) : mine ?? 1,
    target: Number(q.get('target')) || 300,
    mode: q.get('mode') === 'current' ? 'current' : 'baseline',
    recover: ['sell', 'vendor', 'none'].includes(q.get('recover')) ? q.get('recover') : 'sell',
    sources: q.get('sources') === 'all' ? 'all' : 'trainer',
    known_only: q.get('known_only') === '1',
  };
}

// ---------------------------------------------------------------- router
export function mockApi(method, fullPath, body) {
  const url = new URL(fullPath, 'http://mock.local');
  const p = url.pathname;
  const q = url.searchParams;
  let m;
  if (method === 'POST' && p === '/api/ingest') {
    ingestTs = Math.floor(Date.now() / 1000);
    const fired = evaluateAlerts();
    return { realm: REALM, items: CURRENT.size, changed: 17, skipped: false, alerts: fired };
  }
  // ---- v2 writes
  if (method === 'PUT' && p === '/api/myprofessions') {
    if (!Array.isArray(body)) throw badRequest('Body must be a list');
    const next = new Map();
    for (const x of body) {
      const id = Number(x.profession_id);
      const skill = Math.round(Number(x.skill));
      if (!PROFESSIONS.some((pp) => pp[0] === id) || !isFinite(skill)) throw badRequest('Bad profession');
      next.set(id, Math.max(0, Math.min(375, skill)));
    }
    MY_PROFS.clear();
    for (const [k, v] of next) MY_PROFS.set(k, v);
    return myProfessions();
  }
  if (method === 'PUT' && (m = p.match(/^\/api\/known\/(\d+)$/))) {
    const k = body ? body.known : undefined;
    if (!RECIPES.some((r) => r[0] === Number(m[1]))) throw notFound('Unknown spell');
    if (k === null) KNOWN.delete(Number(m[1]));
    else if (k === 1 || k === 0) KNOWN.set(Number(m[1]), k);
    else throw badRequest('known must be 1, 0 or null');
    return { ok: true };
  }
  if (method === 'PUT' && (m = p.match(/^\/api\/shopping\/(\d+)$/))) {
    const qty = Math.round(Number(body && body.qty));
    if (!isFinite(qty)) throw badRequest('qty must be a number');
    if (qty <= 0) SHOP.delete(Number(m[1]));
    else SHOP.set(Number(m[1]), qty);
    return { ok: true };
  }
  if (method === 'DELETE' && p === '/api/shopping') {
    SHOP.clear();
    return { ok: true };
  }
  if (method === 'PUT' && (m = p.match(/^\/api\/watchlist\/(\d+)$/))) return setWatch(Number(m[1]), body);
  if (method === 'DELETE' && (m = p.match(/^\/api\/watchlist\/(\d+)$/))) {
    if (!WATCH.has(Number(m[1]))) throw notFound('Not watched');
    WATCH.delete(Number(m[1]));
    return { ok: true };
  }
  if (method === 'POST' && p === '/api/alerts/seen') {
    ALERTS.forEach((a) => (a.seen = true));
    return { ok: true };
  }
  if (method === 'PUT' && p === '/api/settings') {
    if (!body || typeof body !== 'object') throw badRequest('Body must be an object');
    for (const k of Object.keys(body)) if (k in SETTINGS) SETTINGS[k] = body[k];
    return { ...SETTINGS };
  }
  if (method === 'POST' && (m = p.match(/^\/api\/leveling\/(\d+)\/to-shopping$/))) {
    q.set('_prof', m[1]);
    const plan = levelingPlan(Number(m[1]), levelOpts(q));
    for (const s of plan.steps) SHOP.set(s.spell_id, (SHOP.get(s.spell_id) || 0) + s.crafts);
    return { added: plan.steps.length };
  }
  if (method === 'POST' && p === '/api/alerts/evaluate') return { fired: evaluateAlerts() };
  if (method === 'POST' && p === '/api/notify/test') return { ok: true, sent: SETTINGS.notify_windows };

  if (p === '/api/status') return status();
  if (p === '/api/items/search') return search(q.get('q') || '', Number(q.get('limit')) || 50);
  if ((m = p.match(/^\/api\/item\/(\d+)$/))) return item(Number(m[1]));
  if ((m = p.match(/^\/api\/tooltip\/(\d+)$/))) {
    const t = tooltipMock(Number(m[1]));
    if (!t) throw notFound(`item ${m[1]} not found`);
    return t;
  }
  if (p === '/api/professions') return professions();
  if ((m = p.match(/^\/api\/professions\/(\d+)\/gather$/))) return gatherRows(Number(m[1]));
  if ((m = p.match(/^\/api\/professions\/(\d+)$/))) return profession(Number(m[1]), q.get('mode') || 'current', q.get('mine') === '1');
  if (p === '/api/deals') return deals(Number(q.get('threshold') ?? 0.25), Number(q.get('min_value') || 0), q.get('reason') || '', q.get('include_low') === '1');
  if ((m = p.match(/^\/api\/recipe\/(\d+)\/trend$/))) return recipeTrend(Number(m[1]), Math.max(7, Math.min(90, Number(q.get('days')) || 30)));
  // ---- v5 AH Tool Logger
  if ((m = p.match(/^\/api\/item\/(\d+)\/market$/))) return mockItemMarket(Number(m[1]));
  if (p === '/api/market/activity') return mockActivity(Number(q.get('days')) || 7);
  if (p === '/api/market/my') return mockMy(Number(q.get('days')) || 30);
  if (p === '/api/market/report') return mockReport(q.get('date') || '');
  if (p === '/api/trending-crafts') return trendingCrafts(Number(q.get('limit')) || 20, q.get('mine') === '1', q.get('include_low') === '1');
  if ((m = p.match(/^\/api\/leveling\/(\d+)$/))) {
    q.set('_prof', m[1]);
    return levelingPlan(Number(m[1]), levelOpts(q));
  }
  if (p === '/api/stats/market') return market();
  if (p === '/api/postings') return postings();
  if (p === '/api/myprofessions') return myProfessions();
  if (p === '/api/mycrafts') return myCrafts(q.get('mode') || 'current', Number(q.get('limit')) || 50);
  if (p === '/api/shopping') return shopping();
  if (p === '/api/watchlist') return watchlist();
  if (p === '/api/alerts') return alerts(Number(q.get('limit')) || 50);
  if (p === '/api/settings') return { ...SETTINGS };
  throw new Error(`Unknown endpoint (404): ${p}`);
}

// ---------------------------------------------------------------- v5 mocks (AH Tool Logger)
const cp = (id) => (CURRENT.get(id) ? CURRENT.get(id).price : 1000);
const NOW_S = () => Math.floor(Date.now() / 1000);
const MOCK_SCAN = () => ({ id: 9, ts: NOW_S() - 5400, age_s: 5400, auctions: 18432, realm: REALM, faction: 'Alliance', char: 'Mockadin' });
const SELLERS = ['Goldrush', 'Stackz', 'Thriftina', 'Bargrim', 'Velvetpaw', 'Ironcoin'];
function mockItemMarket(id) {
  const cur = CURRENT.get(id);
  const base = cur ? cur.price : 5000;
  const listings = [];
  for (let i = 0; i < 9; i++) {
    const unit = Math.round(base * (1 + i * 0.07));
    const count = [1, 5, 10, 20][i % 4];
    listings.push({ unit, count, buyout: unit * count, owner: SELLERS[i % SELLERS.length], time_left: 4 - (i % 3), time_left_text: ['< 30m', '30m - 2h', '2h - 8h', '8h - 24h'][3 - (i % 3)], has_bid: i === 4, auctions: 1 + (i % 3) });
  }
  let cum = 0;
  const depth = listings.map((l) => ({ unit: l.unit, cum_qty: (cum += l.count * l.auctions) }));
  const series = [];
  for (let d = 9; d >= 0; d--) {
    const dt = new Date(Date.now() - d * 86400000).toISOString().slice(0, 10);
    const sold = 8 + ((d * 7 + id) % 13);
    series.push({ date: dt, sold, expired: (d * 3) % 5, relisted: 1, value: sold * base, avg_unit: base });
  }
  const sold = series.reduce((a, b) => a + b.sold, 0);
  const expired = series.reduce((a, b) => a + b.expired, 0);
  return {
    item_id: id, scan: MOCK_SCAN(), installed: true, listings, depth, cost: null,
    summary: { auctions: listings.reduce((a, l) => a + l.auctions, 0), qty: cum, sellers: SELLERS.length, min_unit: base, median_unit: Math.round(base * 1.2), value: depth.length * base * 10 },
    sales: { days: 14, series, coverage: { scans: 24, scan_pairs: 22, days_observed: 10 }, totals: { sold, expired, value: sold * base, avg_unit: base, per_day: Math.round((sold / 10) * 10) / 10, sell_through: sold / (sold + expired) } },
    supply: [],
  };
}
function mockActivity(days) {
  const items = [...CURRENT.entries()].slice(0, 40).map(([id, c], i) => {
    c = [c.price];
    const sold = 5 + ((i * 17) % 60);
    return { item_id: id, name: ITEMS.get(id) ? ITEMS.get(id).name : null, quality: ITEMS.get(id) ? ITEMS.get(id).quality : 1, sold, value: sold * c[0], avg_unit: c[0], per_day: Math.round((sold / days) * 10) / 10, expired: i % 7, sell_through: sold / (sold + (i % 7)), sellers: 1 + (i % 5), listed_now: 10 + i * 3, min_unit_now: c[0], days_of_supply: Math.round(((10 + i * 3) / (sold / days)) * 10) / 10 };
  });
  return {
    days, installed: true, scan: MOCK_SCAN(), coverage: { scans: 24, scan_pairs: 22, days_observed: Math.min(days, 10) },
    totals: { value: items.reduce((a, b) => a + b.value, 0), sold_auctions: 812, expired_auctions: 301, relisted_auctions: 77 },
    items,
    sellers: SELLERS.map((o, i) => ({ owner: o, auctions: 400 - i * 50, items: 120 - i * 15, value: (9 - i) * 1500000 })),
    scans: Array.from({ length: 8 }, (_, i) => ({ ts: NOW_S() - 5400 - i * 4 * 3600, auctions: 18000 + i * 113, partial: i === 5, char: 'Mockadin', source: 'addon' })),
  };
}
function mockMy(days) {
  const ids = [...CURRENT.keys()].slice(0, 6);
  const name = (id) => (ITEMS.get(id) ? ITEMS.get(id).name : null);
  const auctions = ids.map((id, i) => ({ item_id: id, name: name(id), quality: 1, char: 'Mockadin', count: 20, buyout: cp(id) * 20, unit: cp(id), bid: 0, time_left: 3, time_left_text: '2h - 8h', lowest_other: Math.round(cp(id) * (i % 2 ? 0.95 : 1.05)), cheaper_qty: i % 2 ? 40 : 0, status: i === 0 ? 'sold' : i % 2 ? 'undercut' : 'lowest', ts: NOW_S() - 3600 }));
  const log = ids.flatMap((id, i) => [
    { ts: NOW_S() - i * 7200, kind: 'sold', item_id: id, name: name(id), quality: 1, count: 20, price: cp(id) * 20, unit: cp(id), fee: cp(id), player: 'Buyerman', char: 'Mockadin' },
    { ts: NOW_S() - i * 9000 - 500, kind: i % 3 ? 'expired' : 'bought', item_id: id, name: name(id), quality: 1, count: 5, price: i % 3 ? 0 : cp(id) * 5, unit: i % 3 ? null : cp(id), fee: 0, player: null, char: 'Mockadin' },
  ]);
  const items = ids.map((id) => ({ item_id: id, name: name(id), quality: 1, sold: 20, sold_value: cp(id) * 20, sold_net: cp(id) * 19, bought: 0, bought_value: 0, expired: 5, cancelled: 0, sell_through: 0.8, avg_sold_unit: cp(id) }));
  const sum = (k) => items.reduce((a, b) => a + b[k], 0);
  return { days, installed: true, totals: { sold_value: sum('sold_value'), sold_net: sum('sold_net'), fees: sum('sold_value') - sum('sold_net'), bought_value: 0, sold: 6, bought: 2, expired: 4, cancelled: 0 }, items, log, auctions, auctions_ts: NOW_S() - 3600, scan: MOCK_SCAN() };
}

// Daily market report: 20 hourly-ish scans over the last 24 hours, movers from the mock price history.
function mockReport(date) {
  const day = (ts) => new Date(ts * 1000).toISOString().slice(0, 10);
  const end0 = NOW_S() - 1800;
  const dates = [0, 1, 2, 3].map((d) => day(end0 - d * 86400));
  const back = Math.max(0, dates.indexOf(date));
  const end = end0 - back * 86400;
  const start = end - 86400;
  const r = rng(77 + back);
  const listings = Array.from({ length: 20 }, (_, i) => {
    const ts = start + 3600 + Math.round(i * (86400 - 3600) / 19);
    const h = new Date(ts * 1000).getHours();
    const wave = Math.sin(((h - 9) / 24) * Math.PI * 2);
    return { ts, auctions: Math.round(21000 + wave * 2600 + (r() - 0.5) * 700) };
  });
  const peak = listings.reduce((a, b) => (b.auctions > a.auctions ? b : a));
  const low = listings.reduce((a, b) => (b.auctions < a.auctions ? b : a));
  const latest = listings[listings.length - 1];
  const info = (id) => {
    const it = ITEMS.get(id);
    const icon = MOCK_ICONS[id];
    return { name: it ? it.name : null, quality: it ? it.quality : 1, icon_url: icon ? `https://wow.zamimg.com/images/wow/icons/medium/${icon}.jpg` : null };
  };
  const ids = [...CURRENT.keys()];
  const sold = ids.map((id, i) => {
    const units = Math.round(20 + r() * 900 / (1 + i % 9));
    const unit = CURRENT.get(id).price;
    return { item_id: id, ...info(id), sold: units, value: units * unit, sold_day: Math.round(units * 1.2), value_day: Math.round(units * unit * 1.2), avg_unit: unit };
  });
  const top_gold = [...sold].sort((a, b) => b.value - a.value).slice(0, 5);
  const top_units = [...sold].sort((a, b) => b.sold - a.sold).slice(0, 5);
  const changes = ids.map((id) => {
    const h = DAILY.get(id) || [];
    const a = h[h.length - 2];
    const b = h[h.length - 1];
    if (!a || !b) return null;
    const before = a.low || a.high;
    const now = b.low || b.high;
    return { item_id: id, ...info(id), before, now, pct: Math.round((now / before - 1) * 10000) / 10000 };
  }).filter(Boolean);
  changes.sort((a, b) => b.pct - a.pct);
  const up = changes.filter((c) => c.pct > 0.005).slice(0, 3);
  const down = changes.filter((c) => c.pct < -0.005).slice(-3).reverse();
  const groupDefs = [['all', 'All busy goods', () => true], ['herbs', 'Herbs', (it) => it.class_id === 7 && it.subclass_id === 9], ['cloth', 'Cloth', (it) => it.class_id === 7 && it.subclass_id === 5], ['metal', 'Metal & Stone', (it) => it.class_id === 7 && it.subclass_id === 7], ['consumables', 'Consumables', (it) => it.class_id === 0]];
  const groups = groupDefs.map(([key, label, f]) => {
    const cs = changes.filter((c) => ITEMS.get(c.item_id) && f(ITEMS.get(c.item_id)));
    if (cs.length < 3) return null;
    const ps = cs.map((c) => c.pct).sort((a, b) => a - b);
    return { key, label, items: cs.length, pct: ps[Math.floor(ps.length / 2)], cheaper: ps.filter((x) => x < -0.005).length, pricier: ps.filter((x) => x > 0.005).length };
  }).filter(Boolean);
  const prev = listings[listings.length - 2];
  const goneN = Math.round(prev.auctions * 0.18);
  const swingId = ids[3];
  const dealId = ids.find((id) => (discountFor(id) || 0) > 0.25) || ids[0];
  const goldDay = sold.reduce((a, b) => a + b.value_day, 0);
  return {
    installed: true, dates, date: day(end), realm: 'Forever', faction: 'Alliance',
    window: { start, end, scans: 20, prev_scans: 19, first_ts: listings[0].ts, covered_s: 82800, prev_covered_s: 79200 },
    listings, peak, low, latest,
    gone: { auctions: goneN, of: prev.auctions, pct: Math.round((goneN / prev.auctions) * 10000) / 10000, from_ts: prev.ts, to_ts: latest.ts, sold: Math.round(goneN * 0.7), expired: Math.round(goneN * 0.2), relisted: goneN - Math.round(goneN * 0.7) - Math.round(goneN * 0.2) },
    totals: { gold_day: goldDay, gold_observed: Math.round(goldDay / 1.2), units_day: sold.reduce((a, b) => a + b.sold_day, 0), units_observed: sold.reduce((a, b) => a + b.sold, 0), sold_auctions: 5321, items_sold: sold.length, gold_change: 0.12, sell_through: 0.78 },
    top_gold, top_units,
    most_listed: [...ids].sort((a, b) => CURRENT.get(b).available - CURRENT.get(a).available).slice(0, 5).map((id) => ({ item_id: id, ...info(id), qty: CURRENT.get(id).available * 12, auctions: CURRENT.get(id).available, sellers: 7, min_unit: CURRENT.get(id).price })),
    price_source: 'scans', price_items: changes.length, cheaper: changes.filter((c) => c.pct < -0.005).length, pricier: changes.filter((c) => c.pct > 0.005).length,
    up, down, groups,
    swing: { item_id: swingId, ...info(swingId), ratio: 3.4, low: Math.round(CURRENT.get(swingId).price * 0.6), low_ts: listings[4].ts, high: Math.round(CURRENT.get(swingId).price * 2.04), high_ts: listings[15].ts },
    deal: { item_id: dealId, ...info(dealId), price: CURRENT.get(dealId).price, baseline: baselineFor(dealId), discount: discountFor(dealId) },
    generated_ts: NOW_S(),
  };
}
