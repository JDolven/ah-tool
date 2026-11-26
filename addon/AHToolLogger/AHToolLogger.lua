-- AH Tool Logger
-- Records raw Auction House data for the AH Tool desktop app (read from SavedVariables on /reload or logout).
--   * every full scan, no matter which addon starts it (Auctionator's Full Scan, or /ahtl scan)
--   * your own auctions (Auctions tab)
--   * AH mail: sold / bought / expired / cancelled
-- Works with the modern Auction House API (C_AuctionHouse.ReplicateItems, used by the Forever client) and with the
-- legacy one (QueryAuctionItems getAll).
-- Data format of a scan (compact string, one auction per row, rows separated by ";"):
--   [N*]itemId,suffixId,count,buyout,minBid,bid,timeLeft,owner   (N* = N identical auctions)
-- timeLeft is the band 1..4 (short, medium, long, very long).
-- A scan also carries nm = "itemId:quality:name;..." - one name per item, so the app can name custom realm items
-- that are missing from its static data.
-- sell = { [itemId] = copper }: the vendor sell price the game reports (C_Item.GetItemInfo) for every item seen in
-- a full scan, in a profession window or in your bags - no need to own the item. The app prefers it over the Classic
-- Era client tables, which Forever has changed for some items.
-- info = { [itemId] = "classID:subclassID:equipLoc" }: the item's Auction House category from the same call, so the
-- app can sort custom Forever items (missing from its static data) into the right category.

local VERSION = 1
local KEEP_SCANS = 3          -- full scans kept in the file (the app imports them on /reload and keeps the history)
local BATCH = 2000            -- auctions read per frame
local INVOICE_DAYS = 60       -- mail records kept in the file
local MAIL_DAYS = 30          -- lifetime of AH mail

local MODERN = type(C_AuctionHouse) == "table" and type(C_AuctionHouse.ReplicateItems) == "function"
local LEGACY = not MODERN and type(QueryAuctionItems) == "function"

local frame = CreateFrame("Frame")
local state = { pending = false, reading = false, ownScan = false, ahOpen = false, others = nil, houseFaction = nil }

local function msg(s)
  DEFAULT_CHAT_FRAME:AddMessage("|cff33ff99AH Tool Logger|r: " .. s)
end

local function db()
  if type(AHToolLoggerDB) ~= "table" or AHToolLoggerDB.v ~= VERSION then
    AHToolLoggerDB = { v = VERSION, scans = {}, owned = {}, mail = {} }
  end
  local d = AHToolLoggerDB
  d.scans = d.scans or {}
  d.owned = d.owned or {}
  d.mail = d.mail or {}
  d.sell = d.sell or {}
  d.info = d.info or {}
  return d
end

-- Vendor sell prices ------------------------------------------------------------------------------------------------

local sellWait, sellWaiting = {}, 0   -- items the client is still loading (GET_ITEM_INFO_RECEIVED)

-- Newer clients (Forever) have only C_Item.GetItemInfo; the global GetItemInfo is gone there.
local itemInfo = (C_Item and C_Item.GetItemInfo) or GetItemInfo

local function infoOf(itemId)
  local _, _, _, _, _, _, _, _, equipLoc, _, price, classID, subclassID = itemInfo(itemId)
  return price, classID, subclassID, equipLoc
end

local function noteSell(itemId)
  itemId = tonumber(itemId)
  if not itemId or itemId <= 0 then return end
  local ok, price, classID, subclassID, equipLoc = pcall(infoOf, itemId)
  if ok and type(price) == "number" and price >= 0 then
    db().sell[itemId] = price
    if type(classID) == "number" then
      db().info[itemId] = string.format("%d:%d:%s", classID, tonumber(subclassID) or 0, equipLoc or "")
    end
    if sellWait[itemId] then sellWait[itemId] = nil; sellWaiting = sellWaiting - 1 end
  elseif ok and not sellWait[itemId] and sellWaiting < 50000 then
    sellWait[itemId] = true  -- GetItemInfo asked the server; the answer arrives as GET_ITEM_INFO_RECEIVED
    sellWaiting = sellWaiting + 1
    if sellWaiting == 1 then frame:RegisterEvent("GET_ITEM_INFO_RECEIVED") end
  end
end

local function onItemInfo(itemId, success)
  itemId = tonumber(itemId)
  if not itemId or not sellWait[itemId] then return end
  sellWait[itemId] = nil
  sellWaiting = sellWaiting - 1
  if success then noteSell(itemId) end
  if sellWaiting <= 0 then
    sellWaiting = 0
    frame:UnregisterEvent("GET_ITEM_INFO_RECEIVED")
  end
end

-- Items of the last full scan, retried a few times after the scan: many custom Forever items are not loaded yet
-- while the scan is read, and GET_ITEM_INFO_RECEIVED does not always arrive for every one of them.
local lastScanItems = {}

local function scanPriceCount()
  local have, total = 0, 0
  local sell = db().sell
  for _, id in ipairs(lastScanItems) do
    total = total + 1
    if sell[id] then have = have + 1 end
  end
  return have, total
end

local function retryScanPrices()
  local sell = db().sell
  for _, id in ipairs(lastScanItems) do
    if not sell[id] then
      if sellWait[id] then  -- ask again even if an earlier request never answered
        sellWait[id] = nil
        sellWaiting = sellWaiting - 1
      end
      noteSell(id)
    end
  end
end

local function linkId(link)
  return type(link) == "string" and tonumber(link:match("item:(%d+)")) or nil
end

-- Profession window: every product and reagent listed (TradeSkill API; Enchanting uses the Craft API in Classic).
local function readProfession()
  if GetNumTradeSkills and GetTradeSkillItemLink then
    for i = 1, GetNumTradeSkills() or 0 do
      noteSell(linkId(GetTradeSkillItemLink(i)))
      for j = 1, (GetTradeSkillNumReagents and GetTradeSkillNumReagents(i)) or 0 do
        noteSell(linkId(GetTradeSkillReagentItemLink(i, j)))
      end
    end
  end
  if GetNumCrafts and GetCraftItemLink then
    for i = 1, GetNumCrafts() or 0 do
      noteSell(linkId(GetCraftItemLink(i)))
      for j = 1, (GetCraftNumReagents and GetCraftNumReagents(i)) or 0 do
        noteSell(linkId(GetCraftReagentItemLink and GetCraftReagentItemLink(i, j)))
      end
    end
  end
end

local function clean(s)
  if s == nil then return "" end
  return (tostring(s):gsub("[,;|\"\\]", ""))
end

local function suffixOf(link)
  if type(link) ~= "string" then return 0 end
  local s = link:match("item:%-?%d+:%-?%d*:%-?%d*:%-?%d*:%-?%d*:%-?%d*:(%-?%d*)")
  return tonumber(s) or 0
end

-- Neutral auction houses (goblin towns): their auctioneers have no faction. English names, used only when the
-- auctioneer itself can't be asked.
local NEUTRAL_TOWNS = { ["Booty Bay"] = true, ["Gadgetzan"] = true, ["Everlook"] = true }

-- The auction house you are at, not your own faction: Booty Bay, Gadgetzan and Everlook share one neutral house
-- that must not be mixed with the Alliance or Horde one (diffs and sales estimates would be wrong).
local function houseFaction()
  if UnitExists and UnitExists("npc") then
    local f = UnitFactionGroup("npc")
    if f == "Alliance" or f == "Horde" then return f end
    return "Neutral"
  end
  local sub = (GetMinimapZoneText and GetMinimapZoneText()) or (GetSubZoneText and GetSubZoneText()) or ""
  if NEUTRAL_TOWNS[sub] then return "Neutral" end
  return UnitFactionGroup("player") or ""
end

-- GetRealmName() has returned "" on some servers: fall back to the normalized name, then to the last one seen.
local function realmName()
  local r = GetRealmName and GetRealmName() or ""
  if r == "" and GetNormalizedRealmName then r = GetNormalizedRealmName() or "" end
  local d = db()
  if r ~= "" then
    d.realm = r
  else
    r = d.realm or ""
  end
  return r
end

-- atHouse: data read at the Auction House (scans, own auctions) carries the house's faction
local function who(atHouse)
  local faction = UnitFactionGroup("player") or ""
  if atHouse then faction = state.houseFaction or houseFaction() end
  return realmName(), faction, UnitName("player") or ""
end

local function makeRow(itemId, link, count, minBid, buyout, bid, tl, owner)
  if not itemId or itemId <= 0 or not count or count <= 0 then return nil end
  return string.format("%d,%d,%d,%d,%d,%d,%d,%s", itemId, suffixOf(link), count,
    buyout or 0, minBid or 0, bid or 0, tl or 0, clean(owner))
end

-- Full scan: API wrappers --------------------------------------------------------------------------------------------

local api = {}
if MODERN then
  api.event = "REPLICATE_ITEM_LIST_UPDATE"
  api.first = 0
  function api.count() return C_AuctionHouse.GetNumReplicateItems() or 0 end
  function api.row(i)
    local name, _, count, quality, _, _, _, minBid, _, buyout, bid, _, _, owner, ownerFull, _, itemId =
      C_AuctionHouse.GetReplicateItemInfo(i)
    local tl = C_AuctionHouse.GetReplicateItemTimeLeft and C_AuctionHouse.GetReplicateItemTimeLeft(i) or 0
    local link = C_AuctionHouse.GetReplicateItemLink(i)
    return makeRow(itemId, link, count, minBid, buyout, bid, tl, ownerFull or owner), itemId, name, quality, link
  end
elseif LEGACY then
  api.event = "AUCTION_ITEM_LIST_UPDATE"
  api.first = 1
  function api.count() return (GetNumAuctionItems("list")) or 0 end
  function api.row(i)
    local name, _, count, quality, _, _, _, minBid, _, buyout, bid, _, _, owner, ownerFull, _, itemId =
      GetAuctionItemInfo("list", i)
    local link = GetAuctionItemLink("list", i)
    return makeRow(itemId, link, count, minBid, buyout, bid, GetAuctionItemTimeLeft("list", i), ownerFull or owner),
      itemId, name, quality, link
  end
end

local function restoreOthers()
  if state.others then
    for _, f in ipairs(state.others) do f:RegisterEvent(api.event) end
    state.others = nil
  end
end

local function readFullScan()
  local total = api.count()
  if total == 0 then
    msg("full scan returned no auctions.")
    restoreOthers()
    return
  end
  state.reading = true
  local source = state.ownScan and "ahtool" or "addon"
  state.ownScan = false
  local rows, seen, t = {}, {}, time()
  local names, nameList, priced = {}, {}, {}
  local i, last_i = api.first, api.first + total - 1
  local function step()
    if api.count() ~= total then
      state.reading = false
      restoreOthers()
      msg(string.format("the auction list changed while reading (%d/%d) - scan discarded.", i - api.first, total))
      return
    end
    state.progress = (i - api.first) / total
    local stop = math.min(i + BATCH - 1, last_i)
    for idx = i, stop do
      local ok, row, itemId, name, quality, link = pcall(api.row, idx)
      if ok and itemId and not priced[itemId] then
        priced[itemId] = true
        noteSell(itemId)
      end
      if ok and itemId and not names[itemId] then
        if (not name or name == "") and type(link) == "string" then name = link:match("|h%[(.-)%]|h") end
        name = name and clean(name):gsub(":", "") or ""
        if name ~= "" then
          names[itemId] = true
          nameList[#nameList + 1] = string.format("%d:%d:%s", itemId, tonumber(quality) or -1, name)
        end
      end
      if ok and row then
        local k = seen[row]
        if k then
          rows[k][2] = rows[k][2] + 1
        else
          rows[#rows + 1] = { row, 1 }
          seen[row] = #rows
        end
      end
    end
    i = stop + 1
    if i <= last_i then
      C_Timer.After(0, step)
      return
    end
    state.reading = false
    restoreOthers()
    local out, n = {}, 0
    for j = 1, #rows do
      local r = rows[j]
      out[j] = r[2] > 1 and (r[2] .. "*" .. r[1]) or r[1]
      n = n + r[2]
    end
    seen = nil
    local realm, faction, char = who(true)
    local scans = db().scans
    scans[#scans + 1] = { t = t, realm = realm, faction = faction, char = char, src = source, api = MODERN and "modern" or "legacy",
                          total = total, n = n, d = table.concat(out, ";"), nm = table.concat(nameList, ";") }
    while #scans > KEEP_SCANS do table.remove(scans, 1) end
    lastScanItems = {}
    for id in pairs(priced) do lastScanItems[#lastScanItems + 1] = id end
    for _, delay in ipairs({ 10, 60, 180 }) do
      C_Timer.After(delay, function() pcall(retryScanPrices) end)
    end
    msg(string.format("saved full scan with %d auctions. /reload or log out to send it to AH Tool.", n))
  end
  step()
end

local function lastRequest()
  local t = db().lastRequest or 0
  local a = Auctionator and Auctionator.SavedState
  if a then t = math.max(t, a.TimeOfLastReplicateScan or 0, a.TimeOfLastGetAllScan or 0) end
  return t
end

local function onScanRequested()
  if state.reading then return end
  state.pending = true
  state.pendingSince = time()
  db().lastRequest = time()
  frame:RegisterEvent(api.event)
  msg("full scan detected - waiting for data from the server ...")
  local since = state.pendingSince
  C_Timer.After(120, function()
    if state.pending and state.pendingSince == since then
      state.pending = false
      frame:UnregisterEvent(api.event)
      restoreOthers()
      msg("no scan data arrived within 2 minutes - the server may have refused the scan (15 minute limit).")
    end
  end)
end

if MODERN then
  hooksecurefunc(C_AuctionHouse, "ReplicateItems", onScanRequested)
elseif LEGACY then
  hooksecurefunc("QueryAuctionItems", function(_, _, _, _, _, _, getAll)
    if getAll then onScanRequested() end
  end)
end

local function startOwnScan()
  if not (MODERN or LEGACY) then
    msg("no supported Auction House API found on this client.")
    return
  end
  if not state.ahOpen then
    msg("open the Auction House first.")
    return
  end
  if state.reading or state.pending then
    msg("a full scan is already running.")
    return
  end
  if LEGACY then
    local _, canAll = CanSendAuctionQuery()
    if not canAll then
      msg("a full scan is not available yet (the server allows one every 15 minutes).")
      return
    end
    if not ITEM_QUALITY_COLORS[-1] then ITEM_QUALITY_COLORS[-1] = { r = 0, g = 0, b = 0 } end
    -- Keep the default UI and other addons from processing the huge result (same as Auctionator does).
    state.others = { GetFramesRegisteredForEvent(api.event) }
    for _, f in ipairs(state.others) do f:UnregisterEvent(api.event) end
    state.ownScan = true
    QueryAuctionItems("", nil, nil, 0, nil, nil, true, false, nil)
  else
    local wait = 15 * 60 - (time() - lastRequest())
    if wait > 0 then
      msg(string.format("the server allows one full scan every 15 minutes - try again in %d min.", math.ceil(wait / 60)))
      return
    end
    state.ownScan = true
    C_AuctionHouse.ReplicateItems()
  end
end

-- Status strip under the Auction House window ------------------------------------------------------------------------

local ui
local function ago(t)
  local s = time() - t
  if s < 90 then return "just now" end
  if s < 5400 then return math.floor(s / 60 + 0.5) .. " min ago" end
  if s < 172800 then return math.floor(s / 3600 + 0.5) .. " h ago" end
  return math.floor(s / 86400 + 0.5) .. " days ago"
end

local function updateUI()
  if not ui or not ui:IsShown() then return end
  local d = db()
  local last = d.scans[#d.scans]
  local txt
  if state.reading then
    txt = string.format("|cffffd100Reading full scan ... %d%%|r", math.floor((state.progress or 0) * 100))
  elseif state.pending then
    txt = "|cffffd100Waiting for data from the server ...|r"
  elseif last then
    txt = string.format("Last full scan %s - %d auctions. /reload to send to AH Tool.", ago(last.t), last.n)
  else
    txt = "No full scan stored yet."
  end
  ui.text:SetText(txt)
  local wait = 15 * 60 - (time() - lastRequest())
  if state.reading or state.pending then
    ui.button:SetText("Scanning ...")
    ui.button:Disable()
  elseif wait > 0 and not LEGACY then
    ui.button:SetText(string.format("Full Scan (%d:%02d)", math.floor(wait / 60), wait % 60))
    ui.button:Disable()
  else
    ui.button:SetText("Full Scan")
    ui.button:Enable()
  end
end

local function buildUI()
  if ui then return end
  local parent = AuctionHouseFrame or AuctionFrame
  if not parent then return end
  ui = CreateFrame("Frame", "AHToolLoggerStrip", parent, BackdropTemplateMixin and "BackdropTemplate" or nil)
  ui:SetSize(470, 30)
  -- below the tab row at the bottom of the AH window (the tabs hang ~32px under the frame)
  ui:SetPoint("TOPRIGHT", parent, "BOTTOMRIGHT", 0, -36)
  ui:SetFrameStrata("HIGH")
  if ui.SetBackdrop then
    ui:SetBackdrop({ bgFile = "Interface/Tooltips/UI-Tooltip-Background",
                     edgeFile = "Interface/Tooltips/UI-Tooltip-Border", edgeSize = 12,
                     insets = { left = 3, right = 3, top = 3, bottom = 3 } })
    ui:SetBackdropColor(0, 0, 0, 0.85)
  end
  ui.button = CreateFrame("Button", nil, ui, "UIPanelButtonTemplate")
  ui.button:SetSize(130, 22)
  ui.button:SetPoint("RIGHT", ui, "RIGHT", -4, 0)
  ui.button:SetScript("OnClick", function() startOwnScan() end)
  ui.label = ui:CreateFontString(nil, "OVERLAY", "GameFontNormalSmall")
  ui.label:SetPoint("LEFT", ui, "LEFT", 8, 0)
  ui.label:SetText("AH Tool")
  ui.text = ui:CreateFontString(nil, "OVERLAY", "GameFontHighlightSmall")
  ui.text:SetPoint("LEFT", ui.label, "RIGHT", 8, 0)
  ui.text:SetPoint("RIGHT", ui.button, "LEFT", -6, 0)
  ui.text:SetJustifyH("LEFT")
  ui.text:SetWordWrap(false)
  local acc = 0
  ui:SetScript("OnUpdate", function(_, el)
    acc = acc + el
    if acc >= 0.5 then acc = 0; updateUI() end
  end)
  updateUI()
end

-- Own auctions -------------------------------------------------------------------------------------------------------

local function bandFromSeconds(s)
  if not s then return 0 end
  if s <= 1800 then return 1 elseif s <= 7200 then return 2 elseif s <= 43200 then return 3 end
  return 4
end

local function readOwned()
  local rows = {}
  if MODERN and C_AuctionHouse.GetOwnedAuctions then
    for _, a in ipairs(C_AuctionHouse.GetOwnedAuctions() or {}) do
      local itemId = a.itemKey and a.itemKey.itemID
      local row = makeRow(itemId, a.itemLink, a.quantity, 0, a.buyoutAmount, a.bidAmount,
        bandFromSeconds(a.timeLeftSeconds), "")
      if row then rows[#rows + 1] = row .. "," .. ((a.status == 1) and 1 or 0) end
    end
  elseif LEGACY then
    for i = 1, (GetNumAuctionItems("owner")) or 0 do
      local _, _, count, _, _, _, _, minBid, _, buyout, bid, _, _, _, _, saleStatus, itemId = GetAuctionItemInfo("owner", i)
      local row = makeRow(itemId, GetAuctionItemLink("owner", i), count, minBid, buyout, bid,
        GetAuctionItemTimeLeft("owner", i), "")
      if row then rows[#rows + 1] = row .. "," .. (saleStatus or 0) end
    end
  else
    return
  end
  local realm, faction, char = who(true)
  db().owned[realm .. "-" .. char] = { t = time(), realm = realm, faction = faction, char = char, n = #rows,
                                       d = table.concat(rows, ";") }
end

-- AH mail ------------------------------------------------------------------------------------------------------------

local function subjectPattern(fmt)
  if type(fmt) ~= "string" then return nil end
  local p = fmt:gsub("([%(%)%.%+%-%*%?%[%]%^%$])", "%%%1")
  p = p:gsub("%%s", "(.+)")
  return "^" .. p .. "$"
end

local expiredPat = subjectPattern(AUCTION_EXPIRED_MAIL_SUBJECT)
local cancelledPat = subjectPattern(AUCTION_REMOVED_MAIL_SUBJECT)

local function addMail(rec)
  local mail = db().mail
  local bucket = math.floor(rec.t / 900)
  local base = table.concat({ rec.type, rec.item or "", rec.player or "", rec.price or 0, rec.count or 0 }, "|")
  for b = bucket - 1, bucket + 1 do
    if mail[base .. "|" .. b] then return end
  end
  mail[base .. "|" .. bucket] = rec
end

local function readMail()
  if not GetInboxNumItems then return end
  local n = GetInboxNumItems() or 0
  local now = time()
  local realm, faction, char = who()
  for i = 1, n do
    local _, _, _, subject, _, _, daysLeft = GetInboxHeaderInfo(i)
    if daysLeft then
      local sent = now - math.floor((MAIL_DAYS - daysLeft) * 86400)
      local invoiceType, itemName, playerName, bid, buyout, deposit, consignment, _, _, _, count
      if GetInboxInvoiceInfo then
        invoiceType, itemName, playerName, bid, buyout, deposit, consignment, _, _, _, count = GetInboxInvoiceInfo(i)
      end
      if (invoiceType == "seller" or invoiceType == "buyer") and itemName then
        addMail({ t = sent, type = invoiceType == "seller" and "sold" or "bought", item = itemName,
                  player = playerName, price = bid or 0, buyout = buyout or 0, deposit = deposit or 0,
                  fee = consignment or 0, count = count or 1, realm = realm, faction = faction, char = char })
      elseif subject then
        local kind, item = nil, nil
        if expiredPat then item = subject:match(expiredPat); if item then kind = "expired" end end
        if not kind and cancelledPat then item = subject:match(cancelledPat); if item then kind = "cancelled" end end
        if kind then
          local _, itemId, _, count = GetInboxItem(i, 1)
          addMail({ t = sent, type = kind, item = item, itemId = itemId, count = count or 1, price = 0,
                    realm = realm, faction = faction, char = char })
        end
      end
    end
  end
  local cutoff = now - INVOICE_DAYS * 86400
  local mail = db().mail
  for k, rec in pairs(mail) do
    if (rec.t or 0) < cutoff then mail[k] = nil end
  end
end

-- Events -------------------------------------------------------------------------------------------------------------

local mailTimer = false

-- Bags: everything you carry (crafted items that never reach the AH, e.g. wands).
local function readBags()
  local numSlots = (C_Container and C_Container.GetContainerNumSlots) or GetContainerNumSlots
  local itemId = (C_Container and C_Container.GetContainerItemID) or GetContainerItemID
  if not numSlots or not itemId then return end
  for bag = 0, (NUM_BAG_SLOTS or 4) do
    for slot = 1, numSlots(bag) or 0 do
      noteSell(itemId(bag, slot))
    end
  end
end

local profTimer, bagTimer = false, false

local function bagsSoon(delay)
  if not bagTimer then
    bagTimer = true
    C_Timer.After(delay, function() bagTimer = false; pcall(readBags) end)
  end
end

frame:SetScript("OnEvent", function(_, event, ...)
  if event == "GET_ITEM_INFO_RECEIVED" then
    onItemInfo(...)
  elseif event == "TRADE_SKILL_SHOW" or event == "TRADE_SKILL_UPDATE" or event == "CRAFT_SHOW"
      or event == "CRAFT_UPDATE" then
    if not profTimer then
      profTimer = true
      C_Timer.After(1, function() profTimer = false; pcall(readProfession) end)
    end
  elseif event == api.event then
    if state.pending then
      state.pending = false
      frame:UnregisterEvent(api.event)
      readFullScan()
    end
  elseif event == "OWNED_AUCTIONS_UPDATED" or event == "AUCTION_OWNED_LIST_UPDATE" then
    pcall(readOwned)
  elseif event == "MAIL_INBOX_UPDATE" then
    if not mailTimer then
      mailTimer = true
      C_Timer.After(1, function() mailTimer = false; pcall(readMail) end)
    end
  elseif event == "AUCTION_HOUSE_SHOW" then
    state.ahOpen = true
    state.houseFaction = houseFaction()  -- the auctioneer is the "npc" unit only while the house is open
    C_Timer.After(0, function() pcall(buildUI) end)
  elseif event == "AUCTION_HOUSE_CLOSED" then
    state.ahOpen = false
    state.houseFaction = nil
    if state.pending then
      state.pending = false
      if api.event then frame:UnregisterEvent(api.event) end
    end
    restoreOthers()
  elseif event == "BAG_UPDATE_DELAYED" then
    bagsSoon(2)
  elseif event == "PLAYER_LOGIN" then
    db()
    bagsSoon(5)
  end
end)

for _, ev in ipairs({ "PLAYER_LOGIN", "MAIL_INBOX_UPDATE", "AUCTION_HOUSE_SHOW", "AUCTION_HOUSE_CLOSED",
                      MODERN and "OWNED_AUCTIONS_UPDATED" or "AUCTION_OWNED_LIST_UPDATE",
                      "TRADE_SKILL_SHOW", "TRADE_SKILL_UPDATE", "CRAFT_SHOW", "CRAFT_UPDATE", "BAG_UPDATE_DELAYED" }) do
  pcall(frame.RegisterEvent, frame, ev)
end

SLASH_AHTOOLLOGGER1 = "/ahtl"
SlashCmdList.AHTOOLLOGGER = function(arg)
  arg = (arg or ""):lower():match("^%s*(.-)%s*$")
  if arg == "scan" then
    startOwnScan()
  else
    local d = db()
    local last = d.scans[#d.scans]
    local nm, ns = 0, 0
    for _ in pairs(d.mail) do nm = nm + 1 end
    for _ in pairs(d.sell) do ns = ns + 1 end
    local have, total = scanPriceCount()
    if total > 0 then
      msg(string.format("vendor sell prices for %d of the %d items in this session's last scan%s.", have, total,
        have < total and " - more arrive as the game loads them; wait a minute before /reload" or ""))
    end
    msg(string.format("%d full scan(s) stored%s, %d mail record(s), %d vendor sell price(s). Auction House API: %s.",
      #d.scans, last and (", last " .. date("%Y-%m-%d %H:%M", last.t) .. " (" .. last.n .. " auctions)") or "", nm,
      ns, MODERN and "modern" or (LEGACY and "legacy" or "none")))
    msg("/ahtl scan - start a full scan (Auction House must be open). Auctionator's Full Scan is recorded too.")
  end
end
