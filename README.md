# AH Tool

Reads your Auctionator scans, keeps the price history forever (SQLite) and shows everything in your browser.

Built for the Forever realm (Classic Era based) with Auctionator, but should work with Classic Era Auctionator data in
general.

## Requirements

| What | Needed? | Used for |
|---|---|---|
| Python 3.12+ (Windows) | **Required** | Runs the app – standard library only, no `pip install` |
| [Auctionator](https://www.curseforge.com/wow/addons/auctionator) addon | **One of the two** | Prices and price history from Auctionator's scans (`SavedVariables/Auctionator.lua`) |
| [AtlasLootClassic](https://www.curseforge.com/wow/addons/atlaslootclassic) addon | **Required for Professions, Leveling Guide and Shopping List** | Recipe data (`AtlasLootClassic/Data/Profession.lua`). Without it the price pages still work |
| AH Tool Logger addon (in this repo, `addon/AHToolLogger`) | **One of the two** | Full scans of every auction: current and daily prices too (see below), Market Activity, My Sales, listings, real cost, sales estimates |
| Polestar_ItemDB addon | Optional | Better item tooltips for custom Forever items |
| Internet on first start | Required once | Downloads item and spell names from wago.tools; tooltips load icons from wow.zamimg.com |

Note: this is for **Auctionator**, not Auctioneer – Auctioneer data is not read.

## Setup
The easiest way: download `ahtool-windows.zip` from the *Run it yourself* page of a site running the web version
(below). It contains the app, the AH Tool Logger addon and Python, so nothing has to be installed: unzip it and
double-click `start.bat`. From the source code:

1. Install Python 3.12 or newer and the addons above in WoW, and log in once with Auctionator enabled.
2. Start the app once: it looks for your WoW folder and writes `config.json` itself. If it cannot find it, or to
   change it, copy `config.example.json` to `config.json` and set:
   - `savedvariables` – your `...\WTF\Account\<ACCOUNT>\SavedVariables\Auctionator.lua`
   - `addons_dir` – your `...\Interface\AddOns` folder (recipes are read from AtlasLootClassic there).
3. Optional: copy `addon/AHToolLogger` into `Interface/AddOns` for full scans and sales estimates (see below).

## Start
Double-click `start.bat` (or run `python run.py`). Your browser opens http://localhost:8642/.
The first start downloads item and recipe data and may take a few minutes.

## Getting new data
1. At the Auction House, scan:
   - **Auctionator** (its *Full Scan* or a normal Auctionator scan) – updates prices and price history.
   - **AH Tool Logger** – click *Full Scan* on the strip under the AH window, or type `/ahtl scan`. Stores every
     auction for Market Activity, listings, real cost and sales estimates. The server allows one full scan every
     15 minutes; the button counts down. An Auctionator *Full Scan* is also picked up by the logger.
   A logger full scan is enough on its own: the app turns each one into current and daily prices (lowest unit
   buyout and quantity per item), so prices stay up to date without Auctionator. Where Auctionator has a newer price
   for an item (you looked at it after the scan), that one is kept. Prices go under Auctionator's realm key
   (e.g. `PvP Alliance`); the neutral houses (Booty Bay, Gadgetzan, Everlook) get their own (`PvP Neutral`).
   Set `"prices_from_logger": false` in config.json to keep prices Auctionator-only.
2. Type `/reload` (or log out). WoW only writes the files to disk then. The logger keeps the last 3 scans, so
   `/reload` at least every third scan.
3. The tool detects the change within ~5 seconds and the page refreshes itself.

In-game commands: `/ahtl` shows the logger status, `/ahtl scan` starts a full scan (Auction House must be open).

## Pages
- **Overview** – status, best deals now, most profitable crafts.
- **Deals** – items below market price (median of the last 14 days), under vendor price, and cheap reagents.
- **Browse** – every item with an AH price in the Auction House categories (Weapon, Armor → Cloth → Chest, Trade
  Goods, Recipe …), with search, required level and quality filters, price, market price, quantity and vendor price.
  Custom Forever items get their category from AH Tool Logger (1.6), which records it with the sell price.
- **Professions** – all recipes per profession with cost, sell value, profit (after the 5% AH cut) and margin.
- **Item** – price chart, history, recipes, your own auctions.
- **Statistics** – the market broken down by quality/class, biggest movers.
- **My auctions** – what you have posted, compared with the current price.
- **Market Report** (`#/report`) – one day at the auction house on a single page: listings through the day, estimated
  gold traded and units sold, most listed, biggest price moves and group moves vs the day before, and highlights.
  Needs AH Tool Logger full scans (several a day give the best report). Pick earlier days with the date picker; the
  *Print* button prints it or saves it as PDF.

## Item tooltips
Hover any item name to see an in-game style tooltip (stats, effects, sell price, icon) plus the AH price, market
price, discount and recipe info. Stats come from the Classic Era client tables (wago.tools), with Forever changes from
`Polestar_ItemDB` (`Forever.lua` wins) and the game's hotfix cache. Custom Forever items without data show a short
tooltip. Icons are loaded from wow.zamimg.com.

## Files
- `config.json` – your local settings (not in git; start from `config.example.json`): path to `Auctionator.lua`, port, deal threshold (`deal_threshold`), AH cut.
- `data/ah.db` – all history. Back this up if you want to keep your data.
- `python -m ahtool.ingest --force` – re-import manually. `--rebuild-static` re-fetches item names/recipes.

## Limitations
- Auctionator stores one lowest price per item per scan and daily min/max – not every individual auction.
- Some custom Forever items/recipes have no name and show up as "Item #id" / "Spell #id".
- Vendor reagents (vials, thread, etc.) are priced from what you have actually paid at vendors (Auctionator's cache).

## v2: My professions, shopping list, watchlist and alerts

- **My professions** (`#/my`): choose professions and skill. A recipe is "craftable" if its profession is yours and
  (you marked it known, or it has no override and its min skill <= your skill). Known/unknown overrides per recipe.
  `/api/mycrafts` lists craftable recipes by profit; deals are flagged `for_my_crafts`.
- **Shopping list** (`#/shopping`): add recipes with quantities; reagents are aggregated (AH vs vendor) with totals.
  `auctionator_import` is a string for Auctionator (Shopping -> Import -> paste), e.g.
  `AH Tool^"Linen Cloth";;0;0;0;0;0;0;0;0.0023;;;;20`. Items whose names contain `"` or `^`, or that have no name, are
  listed in `unexportable`. Max price = max(AH price, baseline) x (1 + `shopping_max_markup`).
- **Watchlist and alerts** (`#/watchlist`): watch items with a max price (or any deal when no max price is set).
  After every Auctionator import (file watcher and `POST /api/ingest`) alerts are evaluated: an alert fires when the
  condition becomes true, when the price drops below the last alerted price, or after 24 h; it resets when the
  condition turns false. Optional rules: vendor flips (`notify_vendor_flip_min`) and deals on reagents for your
  crafts (`notify_my_reagents`). One Windows toast per ingest (PowerShell/WinRT; disable with `notify_windows`).
  `POST /api/notify/test` sends a test toast; `POST /api/alerts/evaluate` forces an evaluation.
- User data lives in the same SQLite DB (new tables are created automatically; existing data is kept).

## Outlier handling and confidence (v4)

Auctionator stores only one minimum price per scan, so a thin market (one item listed at an absurd price) can
distort a day. The AH Tool cleans this before computing anything:

- **Daily outliers**: each day is compared to the median of the other days within +-7 days (Hampel filter, needs 5+
  points). A day is dropped when it is far outside the usual spread (3.5 x scaled MAD), differs by more than 50 %,
  and also differs by more than 50 % from both neighbouring days (so real level shifts are kept). Dropped days are
  excluded from baseline, averages, min/max, trends and profit history; the item chart shows them as hollow red points.
- **Thin current price**: with <= 2 listed, the current price is `thin_high` when it is above 2x baseline (treated as
  the baseline for crafting cost and sell value) or `thin_low` when it is below 0.5x baseline (a valid deal, but flagged).
  Crafted output is also never valued above 1.25x its baseline.
- **Confidence** (`high`/`medium`/`low`): high needs 7+ days of data, an average of 5+ listed over 14 days and
  volatility < 0.5; low means < 3 days, an average of < 2 listed, or a thin_high price. A recipe's confidence is the
  worst of its reagents and its product. Low-confidence `below_baseline` deals are hidden unless `?include_low=1`.
- Thresholds live in `config.json` (`outlier_mad_k`, `outlier_min_rel`, `thin_qty`, `thin_high_factor`,
  `conf_min_days`, `conf_min_avg_qty`, `trend_min_profit`).

## AH Tool Logger addon (v5): full scans, real market depth and estimated sales

Auctionator only stores the lowest price and the max quantity per day. The small addon in `addon/AHToolLogger`
records the raw data instead:

- **Every full scan** – Auctionator's *Full Scan* button is picked up automatically, or type `/ahtl scan` at the
  Auction House (the server allows one full scan every 15 minutes). Each auction is stored with stack size, buyout,
  bid, time left and seller. `/ahtl` shows what is stored.
- **Your own auctions** – whenever the Auctions tab is opened.
- **AH mail** – sold, bought, expired and cancelled auctions, when the mailbox is opened.
- **Vendor sell prices** – the price the game itself reports (`GetItemInfo`) for every item in a full scan, every
  product and reagent in an open profession window and everything in your bags; you don't need to own the items.
  Many custom Forever items are not loaded yet while a scan is read; the logger asks again 10 s, 1 min and 3 min after
  the scan, and `/ahtl` shows how many items of the last scan have a price - wait a minute after a scan before `/reload`. The app uses them instead of the
  Classic Era client tables, which Forever has changed for some items (e.g. Lesser Magic Wand).

Install: copy `addon/AHToolLogger` into `Interface/AddOns` and enable it on the
character screen. Data reaches the app on `/reload` or logout, same as Auctionator (`SavedVariables/AHToolLogger.lua`).

What the app does with it:

- **Item page → Live market:** all listings, market-depth chart, "cost to buy N" (buys whole stacks from the cheapest
  up), estimated sold per day, average sold price and sell-through.
- **Market Activity:** best-selling items, days of supply, the biggest sellers on the AH and the list of full scans.
- **My Sales:** your active auctions (undercut / lowest / sold), earnings after the AH cut, purchases, sell-through and
  the full AH mail log.
- **Shopping List:** a "Real cost" column that walks the listings of the latest full scan (only when it is less than
  `logger_max_age_h` = 24 hours old) and warns when not enough is listed.

**How sales are estimated:** two full scans less than 48 hours apart are compared. An auction that is gone in the
newer scan counts as *sold* only if it could not have run out: the least time left of its category (short 0, medium
30 min, long 2 h, very long 12 h) is longer than the gap between the scans. Otherwise it counts as *expired*, and if the
same seller posted that item again it counts as *relisted*. Most auctions run 8 hours, so an evening and a morning scan
show mostly expired auctions; cancelled auctions look like sales. The numbers are estimates – several scans a day, a
few hours apart, give the best results.

A scan counts as incomplete (kept, but not used for prices, sales or listings) when the logger read fewer auctions
than the game reported, or when it lists far fewer different items than the previous scan. The number of auctions
alone is not used: it halves overnight as auctions run out.
Raw auctions are kept for the newest `logger_keep_raw_scans` (6) scans; per-item summaries and sales are kept forever.

## Web version (share prices with other players)

The same app can run as a shared website (e.g. in Docker on a home server) that everyone can open in a browser.
Your local AH Tool pushes the market data to it after every import; the website never reads WoW files itself.

**What leaves your PC** – only market data: item/recipe/tooltip data, Auctionator prices and the AH Tool Logger full
scans. **What never leaves it** – your posting history, AH mail (sales, purchases, buyers), your own auctions, your
professions, shopping list, watchlist, alerts, settings and the paths in config.json. In the full scans the name of
the scanning character is removed, and every auction or sale posted by one of *your* characters gets a blank seller,
so nobody can tell which auctions are yours. Your characters are found from your scans, your own auctions and your
AH mail; add alts the logger has never seen to `sync_hide_sellers`. `python -m ahtool.sync --dry-run` shows the
batches it would send and which names it masks. What is pushed is decided by explicit column lists in
`ahtool/sync.py`, so new local data stays local until someone adds it there on purpose.

**Personal setup lives in each visitor's browser.** On the website, My Professions, known recipes, the shopping
list, the watchlist, alert history and settings are stored in the browser (localStorage) and sent along with each
request, so the server computes "craftable for you", leveling plans and alerts without storing anything about anyone.
*My Professions → Export setup / Import setup* moves it to another browser. Alerts are checked while the page is
open, when new prices arrive, and show as browser notifications. My Sales and My auctions do not exist on the website.

**Run the server** (Docker): `docker build -t ahtool-web .` then
`docker run -v /path/to/data:/data -e AHTOOL_SYNC_TOKEN=<random, 32+ chars> -e AHTOOL_PUBLIC_URL=https://ah.example.net -p 127.0.0.1:8642:8642 ahtool-web`
(`127.0.0.1:` publishes the port on this host only, for a reverse proxy running here; leave it out only when the
container is the public endpoint itself).
Generate a token with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. That token is the owner's.

**Behind a reverse proxy** (recommended: it terminates HTTPS). The site refuses cross-site POSTs, so it must be able
to tell that a request comes from its own page. It accepts a POST when the browser says `Sec-Fetch-Site:
same-origin`, when the `Origin` matches `Host` or `X-Forwarded-Host` (default ports 80/443 are normalised, so
`Host: ah.example.net:443` matches `https://ah.example.net`), or when it equals `AHTOOL_PUBLIC_URL`. So: set
`AHTOOL_PUBLIC_URL` to the address people open, and have the proxy pass the original host. If it also appends the
client address to `X-Forwarded-For`, set `AHTOOL_TRUST_PROXY=1` (the number of proxies in front, e.g. `2` for
Cloudflare Tunnel → nginx) so the per-address limit (`AHTOOL_MAX_PER_IP`, default 16 concurrent requests) counts
visitors instead of the proxy. Never set it when the port is reachable directly, or anyone could pick an address:
publish the port as `-p 127.0.0.1:8642:8642` (or only on the Docker network the proxy uses), and/or set
`AHTOOL_TRUSTED_PROXIES` to the proxy's address(es) - comma-separated IPs or networks, e.g. `172.17.0.1` or
`10.0.0.0/8` - so `X-Forwarded-For` is only believed from them (it implies `AHTOOL_TRUST_PROXY=1` if that is unset).
Every TCP peer, the proxy included, may hold at most `AHTOOL_MAX_PER_PROXY` (default 64) connections at once.

- nginx: `proxy_set_header Host $host; proxy_set_header X-Forwarded-Host $host;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for; proxy_set_header X-Forwarded-Proto $scheme;`
  and `client_max_body_size 32m;` (sync batches), `proxy_read_timeout 180s;` (a large push takes a while).
- Caddy: `reverse_proxy 127.0.0.1:8642` passes Host and sets X-Forwarded-For/-Host/-Proto by default.
- Cloudflare Tunnel: point the public hostname at `http://ahtool:8642` and leave *HTTP Host Header* empty (the
  public host is passed on); Cloudflare sets X-Forwarded-For. Allow `/api/sync/*` past any Access login.

The server itself drops connections that do not deliver their headers within 10 s or their body at a reasonable
rate, caps one sync batch at 24 MB unpacked (`AHTOOL_MAX_SYNC_MB`) and parses one push at a time (a second one
waits up to 15 s, then gets 503 and is retried). A push body is received before it waits for that, so a slow upload
blocks nobody: at most 3 bodies (plus 1 for the owner) are held at once and each key has one push in flight. A
contributor's batch that would build more JSON values than any valid batch can is refused before it is parsed. At
most 2 pull pages are built at once (the PC retries a 503), and a page holds about 50,000 scan rows at most - a large
scan is split over several pages. Give the container at least 1 GB of memory.

**Downloads (`#/download`, *Run it yourself*)**: the image also builds `ahtool-windows.zip` (the app, the addon and
the official Windows embeddable Python, downloaded from python.org during `docker build`) and
`ahtool-logger-addon.zip`, served at `/download/`. Choose the Python version with
`--build-arg PYTHON_EMBED_VERSION=3.13.5` and pin it with `--build-arg PYTHON_EMBED_SHA256=<sha256 from python.org>`
(recommended: the build then fails on any other file). Without access to python.org the package is built without
Python and the page asks for an installed one. The zips are built from explicit file lists in `ahtool/downloads.py`,
so config.json, keys and data never end up in them. By hand: `python -m ahtool.downloads build downloads
--python-version 3.13.5`.

**Push from your PC**: add to config.json

```json
"sync_url": "https://ah.example.net",
"sync_token": "<the same token>",
"sync_hide_sellers": ["MyBankAlt"]
```

and restart `start.bat`. It pushes at start and a few seconds after each import (only what is new). By hand:
`python -m ahtool.sync` (`--full` resends everything, `--push-only` / `--pull-only`).

**Two-way:** the app also pulls what the other players pushed – at start, after each push and every
`sync_pull_minutes` (default 5; `"sync_pull": false` turns it off). Their prices are merged into your local
database with the same rules as on the website (lowest low, highest high, newest current price), so your pages,
deals, shopping list and alerts use everyone's scans, and the page refreshes itself when something arrives. Their
full scans are stored with source `web`: they are never pushed back, and your own sales estimates only compare your
own scans, so a sale is not counted twice. Your own Auctionator import merges the same way, so a `/reload` never
replaces a newer price someone else saw with an older one of yours. Nothing personal is pulled – the website has
none.

**More people pushing** – give everyone who runs the local AH Tool their own key, made on the server:

```
docker exec ahtool ahkey create "Ola"       # prints the key once + the config.json lines to send Ola
docker exec ahtool ahkey list               # who, last push, number of pushes
docker exec ahtool ahkey revoke "Ola"       # refused from the next request
docker exec ahtool ahkey purge "Ola"        # delete every price and scan row Ola's key pushed
```

Only a hash of each key is stored, so a lost key is replaced, not recovered. Everyone's data is merged: daily
prices keep the lowest low and the highest high, the current price keeps the newest observation, and scans are added
side by side. Each PC masks its own characters and never sends its own trades, exactly as above. Analysis settings
and the item/recipe data come only from the owner (the token, or a key made with `--owner`), so differing addon
versions on other PCs cannot flip them back and forth. Note that one player's characters are ordinary sellers in
another player's scans, just as in game; ask them to list each other in `sync_hide_sellers` if that matters. When
several people scan the same hours, Market Activity counts the sales each of them estimated, so totals run high.

The website checks every pushed row (and each PC every pulled one): numbers must be numbers in a sane range, text
short, and nothing may be dated in the future (more than 5 minutes ahead; a scan more than 1 minute ahead is sent
with a later push instead), so keep the PC clock right. A batch with one
bad row is refused as a whole. The site remembers which key pushed each scan and price row: only that key (or the
owner) can later change a scan, and if someone sends bad prices, `ahkey revoke` + `ahkey purge` removes them (rows
pushed before this was added are not tracked; PCs that already pulled prices keep their copy, purged scans are
deleted on the PCs too). When two keys pushed a scan for the same realm, faction and second, PCs get only one of
them - the owner's, else the one pushed first - so nobody can overwrite the owner's scans on other PCs.
*Purging a key on a site upgraded from before per-key prices (sync schema 3):* that upgrade filed each old merged
price row under the key that last changed it, and those rows cannot be told apart from the key's own pushes, so a
purge of that key also removes such old values (which may include the owner's). Newer upgrades file them as
untracked. Afterwards run `python -m ahtool.sync --full` on the owner's PC to send its own prices again. The website's default
realm follows the owner's pushes. `sync_url` must be `https://` (plain `http://` only for localhost), and the app
never follows a redirect with your key.

## License
AH Tool (the app and the AH Tool Logger addon) is free software under the
[GNU Affero General Public License v3.0](LICENSE). You may use, change and share it, but any copy or modified
version you distribute – or run as a service for others over a network – must also be released under the AGPL-3.0
with its source code.
