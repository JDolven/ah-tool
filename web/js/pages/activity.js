// Market Activity: estimated sales across the AH, sellers, scans (AH Tool Logger data).
import { api } from '../api.js';
import { esc, money, num, pct, isNum, itemLink, relTime, tsFmt, tile, errorState, skeletonRows, skeletonTiles, emptyState, debounce } from '../util.js';
import { DataTable } from '../table.js';
import { noScans, scanAge } from '../market.js';

let days = 7;

export async function render(ctx) {
  const { el } = ctx;
  el.innerHTML = `
    <header class="page-head">
      <div><h1>Market Activity</h1><p class="lead">What actually sells, estimated by comparing full scans from the AH Tool Logger addon.</p></div>
      <div class="seg" id="ma-days">${[3, 7, 14, 30].map((d) => `<button class="seg-btn ${d === days ? 'on' : ''}" data-days="${d}">${d}d</button>`).join('')}</div>
    </header>
    <div id="ma-tiles">${skeletonTiles(4)}</div>
    <div id="ma-body"><section class="card">${skeletonRows(10, 6)}</section></div>`;
  el.querySelectorAll('#ma-days .seg-btn').forEach((b) => b.addEventListener('click', () => {
    days = Number(b.dataset.days);
    render(ctx);
  }));

  let a;
  try {
    a = await api.activity(days);
  } catch (err) {
    if (ctx.alive()) {
      el.querySelector('#ma-tiles').innerHTML = '';
      el.querySelector('#ma-body').innerHTML = errorState(err);
    }
    return;
  }
  if (!ctx.alive()) return;
  if (!a.scan) {
    el.querySelector('#ma-tiles').innerHTML = '';
    el.querySelector('#ma-body').innerHTML = `<section class="card">${noScans(a.installed)}</section>`;
    return;
  }
  const t = a.totals;
  const cov = a.coverage;
  el.querySelector('#ma-tiles').innerHTML = `<div class="tiles">
    ${tile('Full scans', num(cov.scans), scanAge(a.scan))}
    ${tile('Estimated sales', money(t.value), `${num(t.sold_auctions)} auctions in ${num(cov.days_observed)} observed day(s)`)}
    ${tile('Expired', num(t.expired_auctions), 'could have run out between scans')}
    ${tile('Relisted', num(t.relisted_auctions), 'seller cancelled and posted again')}
  </div>`;

  el.querySelector('#ma-body').innerHTML = `
    ${cov.scan_pairs ? '' : `<div class="callout">Only one full scan so far – sales show up after the next full scan (less than 48 hours later). Several scans a day give the best estimates.</div>`}
    <section class="card filters">
      <div class="filter-row">
        <label class="field field-grow"><span class="field-label">Filter</span><input type="search" id="ma-txt" placeholder="Name or ID…"></label>
      </div>
    </section>
    <section class="card card-flush">
      <div class="card-head pad-x"><h2>Best sellers</h2><span class="card-note">Sorted by estimated sales value</span></div>
      <div id="ma-items"></div>
    </section>
    <div class="grid-2">
      <section class="card card-flush">
        <div class="card-head pad-x"><h2>Biggest sellers on the AH now</h2><span class="card-note">by listed buyout value</span></div>
        <div id="ma-sellers"></div>
      </section>
      <section class="card card-flush">
        <div class="card-head pad-x"><h2>Full scans</h2></div>
        <div id="ma-scans"></div>
      </section>
    </div>`;

  const items = new DataTable(el.querySelector('#ma-items'), [
    { key: 'name', label: 'Item', defaultDir: 'asc', sort: (r) => String(r.name || '').toLowerCase(), render: (r) => itemLink(r.item_id, r.name, r.quality) },
    { key: 'sold', label: 'Sold', align: 'right', render: (r) => num(r.sold) },
    { key: 'per_day', label: '/ day', align: 'right', render: (r) => num(r.per_day, 1) },
    { key: 'value', label: 'Value', align: 'right', render: (r) => money(r.value, { compact: true }) },
    { key: 'avg_unit', label: 'Avg price', align: 'right', render: (r) => money(r.avg_unit) },
    { key: 'sell_through', label: 'Sell-through', align: 'right', hideSm: true, title: 'Sold vs. expired', render: (r) => (isNum(r.sell_through) ? pct(r.sell_through, 0) : '–') },
    { key: 'listed_now', label: 'Listed now', align: 'right', hideSm: true, render: (r) => num(r.listed_now) },
    { key: 'days_of_supply', label: 'Days of supply', align: 'right', hideMd: true, title: 'Listed now ÷ sold per day', render: (r) => (isNum(r.days_of_supply) ? num(r.days_of_supply, 1) : '–') },
    { key: 'min_unit_now', label: 'Cheapest now', align: 'right', hideSm: true, render: (r) => money(r.min_unit_now) },
  ], { sortKey: 'value', sortDir: 'desc', rowKey: (r) => r.item_id, limit: 50, empty: emptyState('No sales observed yet', 'Needs two full scans less than 48 hours apart.', '⇄') });

  let text = '';
  const apply = () => {
    const q = text.trim().toLowerCase();
    items.setRows(a.items.filter((r) => !q || String(r.name || '').toLowerCase().includes(q) || String(r.item_id) === q));
  };
  el.querySelector('#ma-txt').addEventListener('input', debounce((e) => { text = e.target.value; apply(); }, 150));
  apply();

  new DataTable(el.querySelector('#ma-sellers'), [
    { key: 'owner', label: 'Seller', defaultDir: 'asc', sort: (r) => r.owner.toLowerCase(), render: (r) => esc(r.owner) },
    { key: 'auctions', label: 'Auctions', align: 'right', render: (r) => num(r.auctions) },
    { key: 'items', label: 'Items', align: 'right', render: (r) => num(r.items) },
    { key: 'value', label: 'Value', align: 'right', render: (r) => money(r.value, { compact: true }) },
  ], { sortKey: 'value', sortDir: 'desc', rowKey: (r) => r.owner, limit: 15, empty: emptyState('No seller names in the scan') }).setRows(a.sellers);

  new DataTable(el.querySelector('#ma-scans'), [
    { key: 'ts', label: 'When', render: (r) => `<span title="${esc(relTime(r.ts))}">${tsFmt(r.ts)}</span>` },
    { key: 'auctions', label: 'Auctions', align: 'right', render: (r) => num(r.auctions) + (r.partial ? ' <span class="tag-mini warn" title="Much smaller than the previous scan – not used for sales">partial</span>' : '') },
    { key: 'char', label: 'By', render: (r) => esc(r.char || '–') },
  ], { sortKey: 'ts', sortDir: 'desc', rowKey: (r) => r.ts, limit: 10 }).setRows(a.scans);
}
