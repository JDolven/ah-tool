# AH Tool – shared contract

Local program that reads Auctionator SavedVariables, stores history in SQLite, serves a local web UI.
**Python 3.12 stdlib only** (no pip installs). Frontend: vanilla HTML/CSS/JS + Chart.js from
`web/vendor/chart.umd.js` (Chart.js 4.4.1, vendored). UI language: **English**.

Root: the repository folder.

```
ahtool/
  config.json            # paths (see below)
  run.py                 # entry: build static data if missing, ingest, start watcher + server, open browser
  start.bat              # double-click launcher: python run.py
  ahtool/__init__.py
  ahtool/config.py       # load config.json
  ahtool/db.py           # schema + connect()          (DATA agent)
  ahtool/luaparse.py     # SavedVariables Lua parser   (DATA agent)
  ahtool/cbor.py         # CBOR decoder                (DATA agent)
  ahtool/ingest.py       # Auctionator.lua -> SQLite   (DATA agent)
  ahtool/staticdata.py   # items/recipes/spells build  (DATA agent)
  ahtool/watcher.py      # poll file mtime, re-ingest  (DATA agent)
  ahtool/analytics.py    # stats, deals, professions   (API agent)
  ahtool/server.py       # http.server JSON API + static files (API agent)
  web/                   # static frontend              (WEB agent)
  data/ah.db             # SQLite (gitignored-ish)
  data/cache/            # downloaded CSVs
```

config.json:
```json
{
  "savedvariables": "C:\\Path\\To\\World of Warcraft\\_forever_\\WTF\\Account\\YOURACCOUNT\\SavedVariables\\Auctionator.lua",
  "addons_dir": "C:\\Path\\To\\World of Warcraft\\_forever_\\Interface\\AddOns",
  "wago_build": "1.15.8.63829",
  "port": 8642,
  "ah_cut": 0.05,
  "deal_threshold": 0.25,
  "baseline_days": 14
}
```

## Source data facts (verified)

- `AUCTIONATOR_PRICE_DATABASE = { ["__dbversion"]=8, ["<realm>"] = <Lua string containing CBOR bytes> | <Lua table> }`.
  Realm here is `"PvP"`. The realm value is a CBOR blob on logout, but may be a plain Lua table (current realm kept
  deserialized). Handle both. Also each item value may itself be a CBOR string (legacy) – handle.
- CBOR: strings are encoded as **byte strings** (major type 2), maps as map, empty tables as empty array `[]`.
  Decoded realm map: `{"<dbKey>": {"m": int, "h": {"<day>": int}, "l": {"<day>": int} | [], "a": {"<day>": int} | []}, "version": 2}`.
  - dbKey is item ID as string for Classic (e.g. "2988"); be defensive: keys like "g:123:..." → item_id = first int found; skip non-item keys ("version").
  - `m` = minimum buyout (copper) at the latest scan. `h[day]` = highest of the day's scanned minimums,
    `l[day]` = lowest (only present when different), `a[day]` = max available quantity seen that day.
  - day = days since 2020-01-01 00:00 **local time**; date = 2020-01-01 + day. (2511 ≈ 2026-11-16).
- Lua string escaping in SavedVariables: `\\`, `\"`, `\n`, `\r`, `\t`, `\<newline>` (= newline), `\ddd` decimal (1-3 digits),
  plus raw high bytes (non-UTF8). Read file as **bytes**.
- `AUCTIONATOR_POSTING_HISTORY = { ["<itemId>"] = { {price=, quantity=, time=<unix>} ... }, ["__dbversion"]=? }`
- `AUCTIONATOR_VENDOR_PRICE_CACHE` – inspect; vendor prices Auctionator has seen (use as vendor-buy source if it is buy prices).
- Recipes: `<addons_dir>\AtlasLootClassic\Data\Profession.lua`, table `PROFESSION_DATA.CLASSIC = {` (ends before
  `PROFESSION_DATA.BCC`). Line format:
  `[spellID] = { createdItemID|nil, profID, minLvl, lowLvl, highLvl, { reagentIDs }, { counts }, numCreatedItems? },`
  profID: 1 First Aid, 2 Blacksmithing, 3 Leatherworking, 4 Alchemy, 5 Herbalism, 6 Cooking, 7 Mining, 8 Tailoring,
  9 Engineering, 10 Enchanting, 11 Fishing, 12 Skinning, 13 Poisons. (`name_no` is kept for compatibility and equals the English name.)
- Item names etc.: `https://wago.tools/db2/ItemSparse/csv?build=<wago_build>` (verified 200, ~8.5MB; columns include
  ID, Display_lang (name), OverallQualityID, SellPrice, BuyPrice, ItemLevel, VendorStackCount, MaxCount, Stackable…).
  Item class/subclass: `.../db2/Item/csv` (ClassID, SubclassID, IconFileDataID). Spell names: `.../db2/SpellName/csv`.
  Cache downloads in data/cache/. Some realm items are custom (e.g. 284712) and won't be in CSVs → name `null`
  (UI shows "Item #284712").

## SQLite schema (`data/ah.db`) – DATA agent creates, API agent reads

```sql
items(id INTEGER PRIMARY KEY, name TEXT, quality INTEGER, item_level INTEGER, class_id INTEGER, subclass_id INTEGER,
      icon INTEGER, vendor_sell INTEGER, vendor_buy INTEGER, stack INTEGER)
spells(id INTEGER PRIMARY KEY, name TEXT)
recipes(spell_id INTEGER PRIMARY KEY, profession_id INTEGER, created_item_id INTEGER, num_created REAL,
        min_skill INTEGER, low_skill INTEGER, high_skill INTEGER)
recipe_reagents(spell_id INTEGER, item_id INTEGER, count INTEGER, PRIMARY KEY(spell_id, item_id))
vendor_items(item_id INTEGER PRIMARY KEY, price INTEGER, source TEXT)   -- items buyable from NPC vendors; price = copper per unit
price_daily(realm TEXT, item_id INTEGER, day INTEGER, low INTEGER, high INTEGER, available INTEGER,
            PRIMARY KEY(realm, item_id, day))   -- low = l[day] if present else h[day]; high = h[day]
price_current(realm TEXT, item_id INTEGER, price INTEGER, available INTEGER, last_day INTEGER, updated_at INTEGER,
              PRIMARY KEY(realm, item_id))      -- price = m; available = a[last_day]; last_day = max day in h
snapshots(realm TEXT, item_id INTEGER, ts INTEGER, price INTEGER, available INTEGER)  -- appended only when price/available changed vs previous ingest
ingests(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, file_mtime INTEGER, realm TEXT, items INTEGER, changed INTEGER)
postings(item_id INTEGER, ts INTEGER, price INTEGER, quantity INTEGER, PRIMARY KEY(item_id, ts, price, quantity))
meta(key TEXT PRIMARY KEY, value TEXT)
```
Ingest is **upsert / merge**: never delete price_daily rows (Auctionator prunes old days; we keep them forever).
Skip ingest if file_mtime equals last ingest's file_mtime. Use WAL mode. All prices are copper.

Python API exported by DATA agent:
- `ahtool.db.connect(path=None) -> sqlite3.Connection` (row_factory=sqlite3.Row, creates schema)
- `ahtool.ingest.ingest(conn, cfg, force=False) -> dict` summary `{realm, items, changed, skipped}`
- `ahtool.staticdata.ensure(conn, cfg, force=False)` – build items/spells/recipes/vendor_items if empty
- `ahtool.watcher.start(cfg, on_ingest=None)` – daemon thread polling every 5s
- `ahtool.ingest.day_to_date(day) -> datetime.date`

## Analytics rules (API agent)

- daily_price(day) = low (fallback high). baseline = **median** daily_price over the last `baseline_days` days
  **before** current last_day (need ≥3 days, else baseline = null).
- discount = 1 − current/baseline. Stale if last_day < latest day in DB for realm − 2.
- Unit cost of a reagent = min(AH current price, vendor_items price) – source `"ah"`/`"vendor"`/`"none"`.
  Recipe cost = Σ count × unit cost; `cost_complete` false if any reagent has no price.
- Sell value = AH current price of created item × num_created; vendor_value = items.vendor_sell × num_created.
  profit = max(sell × (1 − ah_cut), vendor_value) − cost; margin = profit/cost. `sell_source` = "ah" | "vendor" | null
  says which one was used (vendor also when there is no AH price). The daily profit trend uses the same rule.
  No created item (e.g. enchants) → sell/profit null. Use baseline instead of current for "market price" view option.
- Deals (`/api/deals`) reasons:
  - `under_vendor`: current price × (1) < items.vendor_sell → guaranteed profit selling to vendor (profit = vendor_sell − price).
  - `below_baseline`: discount ≥ threshold.
  - `cheap_reagent`: below_baseline AND item is a reagent in ≥1 recipe (list professions).
  Score for sorting: discount × log10(baseline+1) (under_vendor ranked top by profit).

## HTTP API (port 8642; all JSON; prices in copper; dates ISO "YYYY-MM-DD")

- `GET /api/status` → `{realm, realms:[..], last_ingest_ts, file_mtime, items_priced, latest_day, latest_date, item_names_loaded, recipes_loaded}`
- `POST /api/ingest` → ingest summary (force=true)
- `GET /api/items/search?q=&limit=50` → `[{id,name,quality,price,available,last_date,baseline,discount}]` (q matches name substring or id; empty q → most valuable listed)
- `GET /api/item/<id>` → `{id,name,quality,item_level,class_id,vendor_sell,vendor_buy,current:{price,available,last_date,stale},
   stats:{baseline,avg7,avg30,min30,max30,median30,days_of_data,volatility,trend7_pct}, history:[{date,low,high,available}],
   snapshots:[{ts,price,available}], crafted_by:[recipe objects], used_in:[{spell_id,name,profession,count}], postings:[{ts,price,quantity}]}`
- `GET /api/professions` → `[{id,name,name_no,recipe_count,priced_count,profitable_count,best_profit}]`
- `GET /api/professions/<id>?mode=current|baseline` → `[recipe]`, recipe =
  `{spell_id,name,created_item_id,created_name,created_quality,num_created,skill:{min,low,high},
    reagents:[{item_id,name,quality,count,unit_price,source,total,is_deal}], cost,cost_complete,sell,vendor_value,sell_source,profit,margin,sell_available}`
  (name = created item name or spell name)
- `GET /api/deals?threshold=0.25&min_value=0&reason=` → `[{item_id,name,quality,price,baseline,discount,available,days_of_data,
   reasons:[..], vendor_profit, professions:[..], score}]`
- `GET /api/stats/market` → `{totals:{items,listed_quantity,market_value}, by_quality:[{quality,count,value}],
   by_class:[{class_id,class_name,count,value}], movers_up:[item+trend7_pct], movers_down:[..], most_listed:[..],
   most_valuable:[..], scan_days:[{date,items}] }`
- `GET /api/postings` → `[{item_id,name,ts,price,quantity,current_price,diff_pct}]`
- Static: `/` → web/index.html, other paths → web/*. Unknown /api → 404 JSON `{error}`.

## Web pages (WEB agent) – single-page app with hash routing in web/
`#/` Overview (dashboard), `#/deals` Deals, `#/professions` + `#/professions/<id>`, `#/item/<id>`,
`#/stats` Statistics, `#/postings` My auctions. Format copper as `12g 34s 56c` with gold/silver/copper colors.
Item quality colors: 0 #9d9d9d, 1 #ffffff/text, 2 #1eff00, 3 #0070dd, 4 #a335ee, 5 #ff8000.
Links to `https://www.wowhead.com/classic/item=<id>` as secondary. Light + dark theme.

---
# v2 features: My professions, Shopping list, Watchlist + notifications

UI language: **English** everywhere (game terms in original English). User state lives in SQLite (not browser storage)
so the background watcher can use it.

## New tables (add to db.py schema, CREATE IF NOT EXISTS)
```sql
my_professions(profession_id INTEGER PRIMARY KEY, skill INTEGER NOT NULL DEFAULT 300)
known_recipes(spell_id INTEGER PRIMARY KEY, known INTEGER NOT NULL)   -- 1 = I know it, 0 = hide/don't know (overrides skill rule)
shopping_list(spell_id INTEGER PRIMARY KEY, qty INTEGER NOT NULL)
watchlist(item_id INTEGER PRIMARY KEY, max_price INTEGER,             -- copper; NULL = alert on any "deal" for this item
          enabled INTEGER NOT NULL DEFAULT 1, note TEXT, created_at INTEGER,
          last_alert_price INTEGER, last_alert_ts INTEGER)
alerts(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, item_id INTEGER, kind TEXT, price INTEGER,
       threshold INTEGER, message TEXT, seen INTEGER NOT NULL DEFAULT 0)
settings(key TEXT PRIMARY KEY, value TEXT)  -- JSON values
```
settings keys: `notify_windows` (bool, default true), `notify_vendor_flip_min` (copper, default 0 = off: alert on under_vendor
deals with vendor_profit ≥ this), `notify_my_reagents` (bool, default false: alert when a reagent used by my craftable
recipes is a below_baseline deal), `shopping_max_markup` (float, default 0.15).

## Rules
- **can_craft(recipe)** = profession in my_professions AND (known_recipes.known=1 OR (no known_recipes row AND min_skill ≤ my skill)).
  known=0 → never. Recipes with min_skill 0/null count as craftable by skill.
- **Alerts** are evaluated after every non-skipped ingest (hook into the watcher callback in run.py AND POST /api/ingest):
  - watch item with max_price: condition price ≤ max_price. With NULL max_price: condition = item is a below_baseline deal
    (deal_threshold) or under_vendor.
  - vendor flip & my-reagent rules per settings.
  - Dedupe: fire when condition true AND (last_alert_price NULL OR price < last_alert_price OR last_alert_ts older than 24h).
    When condition false → reset last_alert_price/ts to NULL. Insert into `alerts`.
  - Windows toast (if notify_windows): one toast per ingest summarising up to 5 alerts ("Linen Cloth 18c ≤ 20c, +3 more").
    Implement in `ahtool/notify.py` with stdlib only: run `powershell -NoProfile -ExecutionPolicy Bypass -Command <script>`
    using WinRT `Windows.UI.Notifications.ToastNotificationManager` (ToastText02 template) with AppId
    `{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe`. Non-blocking (Popen), never crash; pass
    text safely (escape for PowerShell single-quoted strings, or base64 -EncodedCommand).
- **Shopping list**: per reagent, need = Σ qty × count. unit_price/source as recipe cost rules. Totals: cost, sell
  (Σ qty × sell), profit (after AH cut). `auctionator_import` string for Auctionator's shopping list import box
  (Import → paste), format `AH Tool^<term>^<term>...` where term = `"<Item Name>";;0;0;0;0;0;0;0;<maxPriceGold>;;;;<qty>`
  (fields: "exact name";category;minIlvl;maxIlvl;minLvl;maxLvl;minCrafted;maxCrafted;minPriceGold;maxPriceGold;quality;tier;expansion;quantity).
  maxPriceGold = round(max(unit AH price, baseline or 0) × (1+shopping_max_markup) / 10000, 4). Only reagents with source
  "ah" and a known name go in; vendor reagents and unnamed ones are listed in `vendor_reagents` / `unexportable`.

## New/changed API
- `GET /api/myprofessions` → `[{profession_id,name,skill}]`; `PUT /api/myprofessions` body `[{profession_id,skill}]` (replace all).
- `PUT /api/known/<spell_id>` body `{known: 1|0|null}` (null = delete override).
- Recipe objects gain `can_craft` (bool) and `known` (1|0|null). `GET /api/professions` items gain `mine` (bool), `my_skill`.
  `GET /api/professions/<id>?mine=1` → only can_craft recipes.
- `GET /api/mycrafts?mode=current|baseline&limit=50` → craftable recipes across my professions, sorted by profit desc
  (recipe objects + `profession_name`).
- `GET /api/shopping` → `{recipes:[{spell_id,name,created_item_id,created_quality,profession_name,qty,cost_each,sell_each,profit_each,
   total_cost,total_profit,cost_complete}], reagents:[{item_id,name,quality,need,unit_price,source,total,available,baseline,is_deal}],
   totals:{cost,sell,profit}, auctionator_import, vendor_reagents:[..same shape..], unexportable:[..]}`
- `PUT /api/shopping/<spell_id>` body `{qty}` (qty ≤ 0 removes); `DELETE /api/shopping` clears.
- `GET /api/watchlist` → `[{item_id,name,quality,max_price,enabled,note,price,baseline,discount,available,triggered}]`
- `PUT /api/watchlist/<item_id>` body `{max_price?,enabled?,note?}` (upsert); `DELETE /api/watchlist/<item_id>`.
- `GET /api/alerts?limit=50` → `[{id,ts,item_id,name,quality,kind,price,threshold,message,seen}]`; `POST /api/alerts/seen` marks all seen.
  `GET /api/status` gains `unseen_alerts`.
- `GET /api/settings` / `PUT /api/settings` (partial object merge).
- `POST /api/notify/test` → sends a test Windows toast.
- Item detail gains `watch: {max_price,enabled,note}|null`. Deal objects gain `for_my_crafts` (bool: reagent of a can_craft recipe).
Request bodies are JSON; bad input → 400 `{error}`. All writes invalidate analytics cache where relevant.

## New UI (routes)
- `#/my` **My Professions**: pick professions + skill (0–300 slider/number), list of recipes per my profession with
  known/unknown toggle; "Craftable now" table (from /api/mycrafts) with "Add to shopping list".
- `#/shopping` **Shopping List**: recipes with qty steppers, aggregated reagents (AH vs vendor), totals, profit, big
  "Copy Auctionator import" button + short how-to (Auctionator → Shopping → Import → paste).
- `#/watchlist` **Watchlist & Alerts**: watched items with editable max price (gold/silver/copper input), current price,
  triggered badge; alert history; notification settings incl. "Send test notification"; also browser Notification API
  toast when /api/status shows new unseen_alerts while the page is open (ask permission on a button click).
- Everywhere: "Watch" button on item page and deal rows; "Add to shopping list" on recipe rows (profession pages);
  "Mine only" toggle on profession pages; Overview shows "Best crafts for you" (mycrafts) when my professions are set,
  and an alert bell with unseen count in the topbar.

---
# v3: Item tooltips (Wowhead-style hover)

- Static data: `ahtool/tooltipdata.py` (called from `staticdata.ensure` / `build_tooltips`; rebuilt when
  `meta.tooltip_version` changes or with `--rebuild-static`). Table (CREATE IF NOT EXISTS):
  `item_tooltip(id INTEGER PRIMARY KEY, icon TEXT, data TEXT)` – `icon` = icon file name (ManifestInterfaceData),
  `data` = JSON static tooltip. Precedence: wago CSV < Polestar `Items.lua` < client hotfix cache (DBCache.bin
  Item/ItemSparse header fields) < Polestar `Forever.lua`. The build also patches `items.quality/item_level`
  (and missing names) for Forever/hotfix items so list colours match the tooltip.
- `GET /api/tooltip/<id>` → `{id,name,quality,icon,icon_url,item_level,required_level,custom,source:[..],
   lines:[{text,right?,style,money?}], ah:{price,baseline,discount,available,last_date,stale,days_of_data,
   used_in:{count,professions}, crafted_by:[{spell_id,profession,skill,num_created,reagents:[{item_id,name,quality,count,
   unit_price,source}],cost,cost_complete,profit}], teaches:{...same + name,created_item_id,created_name,created_quality,sell}|null}}`
  `style` ∈ white|green|yellow|gray|red|q<N>; `money` (copper) only on the "Sell Price:" line. Unknown id → 404.
  Cached in memory per analytics context (per ingest/realm).
- Frontend: `web/js/tooltip.js` – one delegated hover handler for every element with `data-item-id`
  (`util.ttAttr(id)`; `itemLink` adds it). 150 ms delay, follows the mouse, flips at viewport edges, hides on
  scroll/leave/click, disabled for touch. `?tt=<id>` pins a tooltip (screenshots), `?theme=light|dark` forces a theme.

---
# v4: Robust prices (outliers), profit trend, leveling planner

Ownership: **ANALYTICS agent** owns `ahtool/analytics.py`, `ahtool/server.py`, `config.json` keys, tests.
**LEVELING agent** owns new `ahtool/leveling.py` only (+ its own tests file `tests/test_leveling.py`).
**WEB agent** owns `web/`. Nobody deletes `data/ah.db`; user-state tables in the real DB must not be written by tests.

## Robust pricing (ANALYTICS)
Auctionator only gives one min price per scan + daily max quantity, so outliers come from thin markets
(e.g. a single item listed at 600g) and odd days.
- Config (config.json, defaults): `outlier_mad_k: 3.5`, `outlier_min_rel: 0.5`, `thin_qty: 2`, `thin_high_factor: 2.0`,
  `conf_min_days: 7`, `conf_min_avg_qty: 5`.
- **Daily outliers (Hampel filter):** for each item's daily_price series, compare each day to the median of the
  surrounding +-7 days (excluding itself). Flag it if |p - med| > k * 1.4826 * MAD **and** |p - med|/med > min_rel.
  Flagged days are excluded from the baseline, avg/min/max/median and the trend. History output marks them `outlier: true`.
  Need >= 5 points to filter at all.
- **Thin current price:** current is `thin_high` when available <= thin_qty AND baseline exists AND price > baseline * thin_high_factor.
  current is `thin_low` when available <= thin_qty AND price < baseline * 0.5. thin_low is still a valid buy deal, but it
  gets flagged.
- **Price functions.** The other modules MUST use these:
  - `analytics.get_ctx(conn, cfg, realm=None)` -> the cached ctx
  - `analytics.buy_price(ctx, item_id, mode="current") -> (unit_copper|None, source)`. Used for reagents. It is min(AH, vendor).
    AH = current, or baseline when mode="baseline". When current is thin_high, use the baseline instead.
  - `analytics.sell_price(ctx, item_id, mode="current") -> unit_copper|None`. Used for crafted output (before the AH cut).
    current mode: min(current, baseline * 1.25) when a baseline exists, else current. When current is thin_high, use the baseline.
    baseline mode: baseline.
  - `analytics.confidence(ctx, item_id) -> "high"|"medium"|"low"`
    - high: days_of_data >= conf_min_days AND avg available (last 14d) >= conf_min_avg_qty AND volatility < 0.5
    - low: days < 3 OR avg available < 2 OR current thin_high
    - medium: everything else
  - `analytics.daily_series(ctx_or_conn, item_id) -> [(day, price, outlier_bool)]` (cleaned)
- Add `confidence` and `price_flag` (null|"thin_high"|"thin_low") to: search rows, item detail current, deals, and recipe
  reagents and created item (`sell_confidence`). A recipe gets `confidence` = the worst of its reagents and its output.
- Deals: drop `below_baseline` deals whose baseline confidence is low, unless `?include_low=1`. Item detail stats come
  from the cleaned series.

## Profit trend (ANALYTICS)
- Per recipe per day (last 30 days): cost_d = sum of count * reagent daily price (cleaned; vendor price when cheaper; forward-fill
  the last known price, max 3 days), sell_d = created daily price * num_created, profit_d = sell_d * (1-ah_cut) - cost_d,
  margin_d = profit_d / cost_d. A day where any value is missing -> null.
- `GET /api/recipe/<spell_id>/trend?days=30` -> `{spell_id, name, points:[{date,cost,sell,profit,margin}], profit_trend_pct, avg_profit_7, avg_profit_30}`
- Recipe objects (profession lists, mycrafts, shopping, item crafted_by) gain `spark: [profit_d ... last 14 days]` (nulls allowed),
  `profit_trend_pct` ((avg profit last 3 days - avg profit of days 4-10) / |avg of days 4-10| * 100), and `confidence`.
  Compute this in bulk once per ctx, so a profession with ~500 recipes stays fast (<300 ms).

## Leveling planner (LEVELING) - `leveling.plan(conn, cfg, prof_id, start=1, target=300, mode="baseline",
   recover="sell", sources="trainer", known_only=False) -> dict`
- Skill-up chance at skill s for a recipe with (orange=min, yellow=low, grey=high):
  s < yellow -> 1.0; yellow <= s < grey -> (grey - s)/(grey - yellow); s >= grey -> 0 (unusable). A recipe is usable when
  min_skill <= s. Skip recipes whose low/high are 0/null (custom Forever data without colours).
- Expected crafts per point = 1/chance. Net cost per craft = cost - recovery. recover="sell":
  sell_price * num_created * (1 - ah_cut), but only when the sell confidence is not low; otherwise use vendor_sell.
  recover="vendor": items.vendor_sell * num. recover="none": 0. Enchanting usually has no product, so recovery is 0 there.
- **Make vs buy:** when a reagent can be crafted in the same profession (e.g. Bolt of Linen Cloth), use
  min(buy price, crafted cost). Crafting that intermediate also gives skill-ups while it is not grey, which is a nice
  touch but optional. At minimum, use the cheaper of buy and craft for the reagent price, and list the intermediate in
  the shopping output.
- **Recipe sources:** sources="trainer" uses only recipes that are NOT taught by an item. The mapping item -> taught spell
  already exists in the tooltip data (see `ahtool/tooltipdata.py` / `item_tooltip` table `teaches`). sources="all" also
  allows item-taught recipes: it adds the recipe item's AH price (buy_price) once as a fixed cost, and lists it under
  `recipes_to_buy`. known_only=True means only recipes where known_recipes.known=1 or the trainer rule applies.
- **Algorithm:** for each point s in [start, target) pick the recipe that minimises expected net cost for that point.
  Fixed recipe-item costs must be amortised sensibly: e.g. iterate (greedy with the fixed cost counted only when first
  chosen, then re-run with chosen recipes as already-bought until stable), or a DP over s with a small set of purchased
  recipes. Document the choice. Merge consecutive points that use the same recipe into steps.
  Round crafts up per step (ceil of the expected crafts) and also report the expected value.
- **Mark trainer milestones:** Apprentice up to 75, Journeyman at 50 (needs level 10) to 150, Expert at 125 (level 20)
  to 225, Artisan at 200 (level 35) to 300.
- **Return value:**
  `{profession_id, profession, start, target, mode, recover,
    steps:[{from,to,spell_id,name,created_item_id,created_quality,skill:{min,low,high},crafts_expected,crafts,
            cost_each,recover_each,net_each,cost_total,recover_total,net_total,confidence,reagents:[{item_id,name,count,unit_price,source}]}],
    shopping:[{item_id,name,quality,need,unit_price,source,total,available,confidence}],
    recipes_to_buy:[{item_id,name,price,spell_id}],
    milestones:[{skill,text}],
    totals:{cost,recover,net,crafts}, warnings:[str], unreachable_from: int|null}`
  unreachable_from = the first skill with no usable priced recipe (the plan stops there, with a warning).
  net < 0 means leveling makes a profit.
- `leveling.plan_to_shopping(plan)` returns `[(spell_id, crafts)]` so the UI can add the whole plan to the shopping list.

## API additions (ANALYTICS wires the routes)
- `GET /api/leveling/<prof_id>?start=&target=&mode=&recover=&sources=&known_only=` -> leveling.plan(...)
- `POST /api/leveling/<prof_id>/to-shopping` (same query) -> it adds every step's crafts to shopping_list (adds to existing
  qty) and returns `{added: n}`
- `GET /api/recipe/<spell_id>/trend`
- `GET /api/trending-crafts?limit=20&mine=0` -> recipes with the biggest positive profit_trend_pct (and a minimum avg profit)
  plus the biggest negative ones: `{up:[recipe], down:[recipe]}`

## UI (WEB)
- Profession detail and My Professions: a profit sparkline column (inline SVG from `spark`), a trend % (colour coded) and a
  confidence badge (high hidden, medium amber dot, low grey "low data" pill). A "Hide low-confidence" toggle defaults to ON.
  The expanded recipe row shows a profit/margin trend chart (Chart.js; profit as line, margin % on a secondary axis is OK here,
  or two small charts) from `/api/recipe/<id>/trend`.
- Item page: the history chart marks outlier days (hollow red points, excluded from the market-price line), "thin market"
  warning chip when price_flag is set, confidence badge. Deals: confidence badge, an "Include low-confidence" toggle, and a
  thin_low/thin_high chip.
- Overview: a "Trending crafts" card (up/down).
- **New `#/leveling` page "Leveling Guide":**
  - Inputs: profession picker, start skill (default my skill when the profession is mine, else 1), target (300),
    mode (market/current), recover (sell/vendor/none), sources (trainer only / include recipe drops), known only.
  - Summary tiles: total cost, recovered, net cost (or "profit!" in green), crafts, warnings.
  - A skill-bar visual 1->300 with coloured segments per step and milestone ticks.
  - A step table: range, recipe (item link with tooltip), crafts (expected), net cost, reagents expandable.
  - Aggregated shopping table, recipes to buy, and an "Add plan to Shopping List" button.
- Add Leveling Guide to the nav after My Professions. Mocks for all new endpoints in mock.js.

---
# v5: AH Tool Logger (addon/AHToolLogger + ahtool/market.py)
- SavedVariables `AHToolLoggerDB = {v=1, scans=[{t,realm,faction,char,src,total,n,d}], owned={"realm-char":{t,realm,faction,char,d}}, mail={key:{t,type,item,itemId?,player,price,buyout,deposit,fee,count,char,realm}}}`.
  Scan row: `itemId,suffixId,count,buyout,minBid,bid,timeLeft,owner` joined by `;` (owned rows add `,saleStatus`).
- Tables: ah_scans, ah_auctions (raw, newest N scans), ah_scan_items (per scan/item summary), ah_sales (kind sold|expired|relisted), my_mail, my_auctions.
- `market.ingest(conn,cfg,force)` (watcher + run.py + POST /api/ingest), `diff_scans(prev,cur,gap)`, `buy_cost(stacks,qty)`, `cost_for(conn,cfg,{item:qty})`.
- API: `GET /api/item/<id>/market?qty=&days=14`, `GET /api/market/activity?days=7&limit=100`, `GET /api/market/my?days=30`; `/api/status` gains `logger`;
  `/api/shopping` reagents gain `listing` (buy_cost or null), totals gain `listing_cost`, `listing_short_items`, plus `listing_scan`, `listing_scan_fresh`.
- Config: `logger_savedvariables` (default: next to Auctionator.lua), `logger_keep_raw_scans` 6, `logger_max_age_h` 24.
- UI: item page "Live market" (web/js/market.js), `#/market` Market Activity, `#/sales` My Sales, shopping "Real cost".

---
# v6: Daily Market Report (`ahtool/report.py`, `#/report`)
- `report.daily(conn, cfg, date=None, realm=None)`: window = 24 h ending at the last full scan of `date` (local ISO date;
  default the latest scan), "the day before" = the 24 h before that. Unknown date -> ValueError (API: 404).
- `GET /api/market/report?date=YYYY-MM-DD` -> `{installed, dates:[iso desc], date, realm, faction,
   window:{start,end,scans,prev_scans,first_ts,covered_s,prev_covered_s} | null (no scans),
   listings:[{ts,auctions}], peak/low/latest:{ts,auctions}, gone:{auctions,of,pct,from_ts,to_ts,sold,expired,relisted}|null,
   totals:{gold_day,gold_observed,units_day,units_observed,sold_auctions,items_sold,gold_change,sell_through},
   top_gold/top_units:[{item_id,name,quality,icon_url,sold,value,sold_day,value_day,avg_unit}],
   most_listed:[{item_id,...,qty,auctions,sellers,min_unit}], price_source:"scans"|"auctionator", price_items, cheaper, pricier,
   up/down:[{item_id,...,pct,before,now}], groups:[{key,label,items,pct,cheaper,pricier}],
   swing:{item_id,...,ratio,low,low_ts,high,high_ts}|null, deal:{item_id,...,price,baseline,discount}|null, generated_ts}`
- Sales come from scan pairs ending in the window, scaled to 24 h by the seconds those pairs cover. Prices: median of the
  per-scan median unit buyout per window (Auctionator daily price when the day before has no scans). Busy item = 10+
  listed in both periods; +-0.5 % = no change; a group needs 3+ busy items.

---
# v7: Web version (`run_web.py`, `ahtool/sync.py`, `ahtool/webstate.py`)
- `server.make_server(..., web=True)`: index.html gets `data-mode="web"`; `/healthz`; blocked: POST ingest,
  notify/test, alerts/evaluate, GET postings, market/my (404 "not available in the web version").
- **Per-browser state:** `POST /api/x {m, p, b, s}` runs API call m/p with body b. `s` (localStorage `ahtool-state-v1`:
  `{professions:{pid:skill}, known:{spell:0|1}, shopping:{spell:qty}, watch:{item:{max_price,enabled,note,created_at,
  last_alert_price,last_alert_ts}}, settings:{}, alerts:[..], alertstate:{kind:item:{price,ts}}, eval}`) is loaded into
  TEMP tables named like the real ones (also empty temp postings/my_mail/my_auctions and a temp copy of meta), so all
  code runs unchanged and nothing is written to the shared DB. Answer `{r, s?}`: s after writes, and from
  `/api/status` when `eval` != MAX(ingests.id) (then `alerts.evaluate(notify_user=False)` runs; first visit only sets
  eval). Frontend: `api.WEB`, writes serialised, GET state merged (alerts only).
- **Sync, protocol 2** (Bearer `AHTOOL_SYNC_TOKEN` = owner, or a key from `ahtool/keys.py` / `ahkey` in the image:
  table `sync_keys(id,name,hash sha256,role owner|contributor,created_at,revoked_at,last_push_ts,pushes)`; not reachable
  via /api/x). `GET /api/sync/state` -> `{protocol, epoch, you, static_hash, accept_static, accept_cfg, last_push_ts,
  scans:[[realm,faction,ts,raw_kept,diffed]]}`. `POST /api/sync/push` (gzip JSON, one atomic batch) with any of `cfg`
  and `static` (owner only, also on an empty server; `hash` must be a sha256 hex, tooltip `icon` must match
  `^[A-Za-z0-9_-]+$` or is stored NULL), `meta` (max of last_ingest_ts, logger_ingest_ts, clamped to now), `price_current`, `price_daily`,
  `snapshots` (insert or ignore, unique realm/item/ts), `scans:[{scan,items,auctions,sales}]` (at most
  `MAX_SCANS_PER_BATCH` = 50 new scans stored per batch, the rest counts as `applied.scans_deferred` and is resent by
  the PC next time; a scan with ts over now+`SCAN_TS_SLACK` (60 s) is skipped and counted in `scans_deferred` too, the
  client does not send such scans), `scan_updates:[{key,drop_raw?,sales?}]`. Unknown keys (top level, in a
  scan / scan update object, in meta) -> 400. **Per-key prices:** server tables `price_daily_src` /
  `price_current_src` hold each key's own rows (src_key, -1 = from before keys were tracked); `price_daily` /
  `price_current` are the merge, recomputed for every realm/item(/day) a push or purge touches: daily = min low, max
  high, max available over all keys; current = newest (last_day, updated_at) among rows not "from the future"
  (last_day > today UTC or updated_at > now; such a row only shows while there is nothing else; pushed updated_at is
  clamped to the receive time) - the same rule as `sync.current_newer` locally. **Scans** are unique per (realm,
  faction, ts, src_key) on the server (schema 3; the local app keeps UNIQUE(realm, faction, ts)), `state.scans` lists
  only the caller's own (and pre-key) scans, raw auctions are pruned to the newest N per realm/faction/key. A push
  that changes merged prices adds one server-side `ingests` row (cache + alert marker, column `src_key`; a contributor
  keeps only its newest); an owner push also sets meta `sync_owner_realm` and adds its realms to meta
  `sync_owner_realms` (the web version's realm list, `analytics.list_realms`). **Schema:** `sync.ensure_schema` (at
  start in run_web.py and per sync request) migrates once to meta `sync_schema`=3 (rebuild ah_scans without the
  global UNIQUE, fill the per-key tables with the merged rows as `LEGACY_SRC` - versions before round 3 filed them
  under the merged row's last-writer src_key, which cannot be undone -, `sweep_invalid`: delete stored rows that fail
  SPEC), then 4 (table `sync_scan_deletes(id, seq, origin, realm, faction, ts)`; scans sharing (realm, faction, ts)
  get a new seq). Every scan deletion (`_drop_scans`: purge, sweep) records a row there and bumps the seq of the
  remaining scans with that key.
  **Push handling (server.py):** body read + gunzip outside `_PUSH_LOCK` with a read slot (`MAX_PUSH_READS`=3, plus
  one owner-only slot), one push per key in flight (else 503), `sync.too_many_values` (contributors: commas + `[` +
  `{` <= `SERVER_MAX_ROWS`*10+10000, else 413), then `_PUSH_LOCK` (owners waiting go first) for parse + apply.
  Pulls: `PULL_SLOTS`=2 pages built at once (wait `PULL_WAIT` 10 s, then 503 + Retry-After), sent after the slot is
  released. Proxy: every TCP peer at most MAX_CONN_PER_IP connections (MAX_CONN_PER_PROXY=64 with a trusted proxy);
  `AHTOOL_TRUSTED_PROXIES` limits whose X-Forwarded-For is used.
  **Validation** (`sync.SPEC`, server `apply` and client `apply_pull`): every row column by column - ints are real ints
  (no bool/float/str/NaN) in range (ids 1..2^31-1, prices 1..2^31-1 copper in price tables / 0..2^31-1 in
  scans - the gold cap; keeps SUM(count*unit_price) far from 2^63, and aggregates use TOTAL() -, auction and sale
  count 1..10000), text <= 64 chars (realm 1..64), days <= today+1, timestamps 2001..now+5 min (pull: +1 h). One bad row
  -> the whole batch 400 (`BadPayload`; pull: `SyncError`, nothing of the page stored; the server leaves stored rows that
  fail SPEC out of pull pages and logs them). Pull cursor values must be ints 0..2^63-1 (else 400). At most `SERVER_MAX_ROWS`
  (2x MAX_BATCH_ROWS) rows per batch, static excluded. A stored price_current row from the future is always
  replaced. The client leaves out local rows that would be refused and splits price_current/snapshots/scan_updates
  into MAX_BATCH_ROWS batches. **Ownership:** server-only nullable column `src_key` on price_daily, price_current,
  snapshots, ah_scans = the key id that pushed / last changed the row (0 = AHTOOL_SYNC_TOKEN, NULL = before this
  column existed). `scan_updates` only apply to scans with the pusher's own src_key (owners: any); others count as
  `applied.scan_updates_refused`. `ahkey purge <name|id>` (`keys.purge` -> `sync.purge_key`) deletes the key's rows
  (scans with their items/auctions/sales, its per-key price rows - merged rows are recomputed from the other keys -,
  snapshots, ingests rows), refreshes `sync_owner_realms` and adds an `ingests` row; PCs that already pulled them keep
  them unless the local row is still exactly what was pulled (then the recomputed row replaces it on the next pull).
  **Latest scan** (`market.latest_scan(conn, realm, faction=None)`, report.daily's scans and faction): your own
  scans (web: owner's - src_key NULL/0/owner key) if the newest is at most `PREFER_OWN_S` (6 h) old, else anyone's.
  **Default realm (web):** `analytics.get_realm` uses meta `sync_owner_realm`, else the realm with the most
  price_current rows, never the newest ingest row (local app: unchanged). `apply_pull` writes its ingest row with the
  PC's current realm, so pulled data never switches the local default realm.
  **Client transport:** sync_url must be https (http only for localhost/127.0.0.1/::1), redirects are never followed
  (the key would go along), answers are capped at 256 MB, at most 300 pull pages per run. Clients keep their own watermarks in local meta `sync:<sha1(url)>`
  = `{epoch, daily:{realm:max_day}, snap_ts}`; another epoch (reset server DB) or `--full` -> send everything.
- **Pull:** server-only column `seq` on price_daily, price_current, ah_scans = the push (meta `sync_seq`) that last
  changed the row. `GET /api/sync/pull?cursor={pd:[seq,rowid],pc:[seq,rowid],sn:rowid,sc:[seq,id]}` -> `{protocol,
  epoch, cursor, more, price_daily, price_current, snapshots, scans:[{origin,scan,items,auctions|null,sales}]}`, <=
  PULL_LIMIT rows per table. Only one scan per (realm, faction, ts) is sent (`_SCAN_WINNER`: an owner's/pre-key scan
  before a contributor's, else the lowest id). `&v=3` (`PULL_VERSION`, sent by current clients) adds
  `pull_version: 3`, `scan_deletes:[[origin,realm,faction,ts]]` (cursor `sd`: id) and caps the scans part at
  ~PULL_LIMIT rows: a larger scan ends with `more: true` and continues in the next page(s) as
  `{origin, cont: true, items, auctions?, sales}` (cursor `sp:[id, seq, items_off, auctions_off, sales_off]`; if the
  scan changed meanwhile it is sent again from the start). Without `v` the old format: whole scans. Client: local
  nullable column `ah_scans.sync_origin` (= server scan id); a first entry replaces a pulled ('web') copy at that key
  completely (items, auctions, sales), `cont` entries append to the copy with that origin, a delete removes the
  pulled copy at that key whose origin matches (or is NULL); own scans are never touched. Pages without origin (older
  servers) keep the old merge. Client (`sync.pull` / `apply_pull`): same merges locally, snapshots insert-if-missing, scans
  inserted as source='web' (char '', diffed=1; own scans coming back are skipped; your own logger scan replaces a
  pulled one with the same key); what was pulled per row is kept in local tables `sync_pulled_current|daily|snap`: a
  row still equal to it takes the server's value as is, and push leaves such values out (daily: that column NULL),
  so other players' data is never pushed back as yours (created on first use; resets the pull cursor once); one local
  `ingests` row with
  file_mtime NULL when prices changed; cursor in meta `pull:<sha1(url)>` with epoch. Local rules: ingest.py merges
  price_daily (`MERGE_DAILY`), keeps a stored price_current row p instead of its own observation only if
  `not sync.current_is_future(p.last_day, p.updated_at) and (p.last_day, p.updated_at) > (last_day, mtime)`, and compares
  file_mtime only with non-NULL rows; market.py never diffs against or prunes own raw because of source='web' scans;
  push never sends source='web' scans.
- Local: config `sync_url`, `sync_token`, `sync_hide_sellers`, `sync_pull` (true), `sync_pull_minutes` (5); `run.py`
  runs `sync.Syncer` (push 8 s after an import, then pull; pull on a timer; alerts + page refresh after a pull).
