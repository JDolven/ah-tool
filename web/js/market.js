// Shared "live market" widgets (AH Tool Logger full-scan data): item page section, scan age chip, install hint.
import { api } from './api.js';
import { esc, money, moneyText, num, pct, isNum, relTime, tile, emptyState, errorState, skeletonRows, dateShort } from './util.js';
import { mountChart, chartBox, moneyAxis, plainAxis, alpha } from './charts.js';
import { DataTable } from './table.js';

export const INSTALL_TEXT =
  'Install the AH Tool Logger addon (copy <code>addon/AHToolLogger</code> into <code>Interface/AddOns</code>), then do a Full Scan in Auctionator or type <code>/ahtl scan</code> at the Auction House, and /reload.';

export function scanAge(scan) {
  if (!scan) return '<span class="muted">no full scan</span>';
  const old = scan.age_s > 24 * 3600;
  return `<span class="${old ? 'warn-text' : 'muted'}" title="${esc(new Date(scan.ts * 1000).toLocaleString())}">full scan ${esc(relTime(scan.ts))} · ${num(scan.auctions)} auctions</span>`;
}

export function noScans(installed) {
  return emptyState(
    'No full scans yet',
    installed ? 'The addon is installed. Do a Full Scan in Auctionator (or <code>/ahtl scan</code>) at the Auction House, then /reload.' : INSTALL_TEXT,
    '⌛'
  );
}

/** Walk the cheapest whole stacks until qty is covered (same as the server's buy_cost). */
export function buyCost(listings, qty) {
  const stacks = [];
  for (const l of listings || []) if (isNum(l.unit) && l.unit > 0) for (let i = 0; i < l.auctions; i++) stacks.push([l.unit, l.count]);
  stacks.sort((a, b) => a[0] - b[0]);
  let total = 0;
  let bought = 0;
  let maxUnit = null;
  for (const [u, c] of stacks) {
    if (bought >= qty) break;
    total += u * c;
    bought += c;
    maxUnit = u;
  }
  return { qty, bought, total, avg: bought ? total / bought : null, maxUnit, short: Math.max(0, qty - bought) };
}

function costHtml(c) {
  if (!c.qty) return '';
  if (!c.bought) return '<span class="muted">Nothing listed with a buyout.</span>';
  return `Buying <strong>${num(c.bought)}</strong>${c.bought > c.qty ? ` <span class="muted">(whole stacks, ${num(c.bought - c.qty)} extra)</span>` : ''} costs
    <strong>${money(c.total)}</strong> · avg ${money(c.avg)} each · most expensive ${money(c.maxUnit)}
    ${c.short ? `<div class="warn-text">Only ${num(c.bought)} listed – ${num(c.short)} short.</div>` : ''}`;
}

/** Item page: live listings, depth, cost-to-buy and estimated sales. */
export async function renderItemMarket(box, itemId, alive) {
  box.innerHTML = `<div class="card-head"><h2>Live market</h2></div>${skeletonRows(4, 4)}`;
  let m;
  try {
    m = await api.itemMarket(itemId);
  } catch (err) {
    if (alive()) box.innerHTML = `<div class="card-head"><h2>Live market</h2></div>${errorState(err)}`;
    return;
  }
  if (!alive()) return;
  const head = `<div class="card-head"><h2>Live market</h2><span class="card-note">${scanAge(m.scan)}</span></div>`;
  if (!m.scan) {
    box.innerHTML = head + noScans(m.installed);
    return;
  }
  const s = m.summary;
  const sales = m.sales || { totals: {}, series: [], coverage: {} };
  const t = sales.totals || {};
  const cov = sales.coverage || {};
  box.innerHTML = `${head}
    <div class="tiles tiles-6 tiles-compact">
      ${tile('Listed now', s ? num(s.qty) : '0', s ? `${num(s.auctions)} auctions` : 'none in the latest scan')}
      ${tile('Sellers', s ? num(s.sellers) : '–')}
      ${tile('Cheapest', s ? money(s.min_unit) : '–', s && isNum(s.median_unit) ? `median ${money(s.median_unit)}` : '')}
      ${tile('Sold / day', isNum(t.per_day) ? num(t.per_day, 1) : '–', `${num(t.sold || 0)} sold in ${esc(sales.days)}d`)}
      ${tile('Avg sold price', money(t.avg_unit))}
      ${tile('Sell-through', isNum(t.sell_through) ? pct(t.sell_through, 0) : '–', 'sold vs. expired')}
    </div>
    <p class="fine">Sales are estimated from ${num(cov.scan_pairs || 0)} pair(s) of full scans over ${num(cov.days_observed || 0)} day(s): an auction that disappears while it still has time left counts as sold. More scans per day = better estimates.</p>
    ${s ? `
    <div class="grid-2">
      <div>
        <h3 class="sub-head">Market depth</h3>
        ${chartBox('depth-chart', 220, 'Cumulative quantity by unit price')}
        <div class="buy-calc">
          <label class="field"><span class="field-label">Cost to buy</span><input type="number" id="buy-qty" min="1" step="1" value="${Math.min(20, s.qty)}"></label>
          <div id="buy-res" class="buy-res"></div>
        </div>
      </div>
      <div>
        <h3 class="sub-head">Estimated sales per day</h3>
        ${sales.series.length ? chartBox('sales-chart', 220, 'Units sold and expired per day') : emptyState('No sales observed yet', 'Needs at least two full scans less than 48 hours apart.', '⇄')}
      </div>
    </div>
    <h3 class="sub-head">Listings</h3>
    <div id="lst-table" class="card-flush"></div>` : emptyState('Not listed right now', 'This item was not in the latest full scan.', '∅')}`;
  if (!s) return;

  const calc = () => {
    const q = Math.max(0, Math.floor(Number(box.querySelector('#buy-qty').value) || 0));
    box.querySelector('#buy-res').innerHTML = costHtml(buyCost(m.listings, q));
  };
  box.querySelector('#buy-qty').addEventListener('input', calc);
  calc();

  const pts = m.depth.map((d) => ({ x: d.cum_qty, y: d.unit }));
  mountChart(box.querySelector('#depth-chart'), (c) => ({
    type: 'line',
    data: { datasets: [{ label: 'Unit price', data: [{ x: 0, y: pts.length ? pts[0].y : 0 }, ...pts], stepped: 'after', borderColor: c.accent, backgroundColor: alpha(c.accent, 0.15), fill: true, pointRadius: 0, borderWidth: 2 }] },
    options: {
      maintainAspectRatio: false,
      parsing: false,
      scales: { x: plainAxis(c, { type: 'linear', title: { display: true, text: 'Quantity (cheapest first)', color: c.muted } }), y: moneyAxis(c) },
      plugins: { legend: { display: false }, tooltip: { callbacks: { title: (it) => `${num(it[0].parsed.x)} units`, label: (it) => `up to ${moneyText(it.parsed.y)} each` } } },
    },
  }));
  if (sales.series.length) {
    mountChart(box.querySelector('#sales-chart'), (c) => ({
      type: 'bar',
      data: {
        labels: sales.series.map((d) => dateShort(d.date)),
        datasets: [
          { label: 'Sold', data: sales.series.map((d) => d.sold), backgroundColor: c.up, borderRadius: 3, stack: 'a' },
          { label: 'Expired', data: sales.series.map((d) => d.expired), backgroundColor: alpha(c.muted, 0.45), borderRadius: 3, stack: 'a' },
        ],
      },
      options: { maintainAspectRatio: false, scales: { x: plainAxis(c, { stacked: true }), y: plainAxis(c, { stacked: true, grid: { color: c.grid }, beginAtZero: true }) } },
    }));
  }

  const table = new DataTable(box.querySelector('#lst-table'), [
    { key: 'unit', label: 'Unit price', align: 'right', defaultDir: 'asc', render: (r) => money(r.unit) },
    { key: 'count', label: 'Stack', align: 'right', render: (r) => num(r.count) },
    { key: 'auctions', label: 'Auctions', align: 'right', render: (r) => num(r.auctions) },
    { key: 'buyout', label: 'Stack buyout', align: 'right', hideSm: true, render: (r) => money(r.buyout) },
    { key: 'owner', label: 'Seller', defaultDir: 'asc', sort: (r) => String(r.owner || '~').toLowerCase(), render: (r) => (r.owner ? esc(r.owner) : '<span class="muted">unknown</span>') },
    { key: 'time_left', label: 'Time left', hideSm: true, render: (r) => esc(r.time_left_text || '–') + (r.has_bid ? ' <span class="tag-mini">bid</span>' : '') },
  ], { sortKey: 'unit', sortDir: 'asc', rowKey: (r) => `${r.unit}-${r.count}-${r.owner}-${r.time_left}`, limit: 25, empty: emptyState('No listings') });
  table.setRows(m.listings);
}
