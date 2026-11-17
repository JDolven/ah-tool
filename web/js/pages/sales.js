// My Sales: own auctions vs. the latest full scan, and AH mail (sold / bought / expired) recorded by AH Tool Logger.
import { api } from '../api.js';
import { esc, money, num, pct, isNum, itemLink, relTime, tsFmt, tile, errorState, skeletonRows, skeletonTiles, emptyState } from '../util.js';
import { DataTable } from '../table.js';
import { INSTALL_TEXT, scanAge } from '../market.js';

let days = 30;

const KIND = {
  sold: ['Sold', 'status-sold'],
  bought: ['Bought', 'status-lowest'],
  expired: ['Expired', 'status-undercut'],
  cancelled: ['Cancelled', 'status-undercut'],
};

export async function render(ctx) {
  const { el } = ctx;
  el.innerHTML = `
    <header class="page-head">
      <div><h1>My Sales</h1><p class="lead">Your auctions compared with the latest full scan, and every sale, purchase and expired auction from your AH mail.</p></div>
      <div class="seg" id="ms-days">${[7, 30, 60].map((d) => `<button class="seg-btn ${d === days ? 'on' : ''}" data-days="${d}">${d}d</button>`).join('')}</div>
    </header>
    <div id="ms-tiles">${skeletonTiles(4)}</div>
    <div id="ms-body"><section class="card">${skeletonRows(8, 5)}</section></div>`;
  el.querySelectorAll('#ms-days .seg-btn').forEach((b) => b.addEventListener('click', () => {
    days = Number(b.dataset.days);
    render(ctx);
  }));

  let m;
  try {
    m = await api.mySales(days);
  } catch (err) {
    if (ctx.alive()) {
      el.querySelector('#ms-tiles').innerHTML = '';
      el.querySelector('#ms-body').innerHTML = errorState(err);
    }
    return;
  }
  if (!ctx.alive()) return;
  const t = m.totals;
  const posted = t.sold + t.expired + t.cancelled;
  el.querySelector('#ms-tiles').innerHTML = `<div class="tiles">
    ${tile('Earned (after AH cut)', money(t.sold_net), `${num(t.sold)} sale(s) · AH cut ${money(t.fees)}`, '')}
    ${tile('Spent on purchases', money(t.bought_value), `${num(t.bought)} won auction(s)`)}
    ${tile('Sell-through', posted ? pct(t.sold / posted, 0) : '–', `${num(t.expired)} expired · ${num(t.cancelled)} cancelled`)}
    ${tile('Active auctions', num(m.auctions.length), m.auctions_ts ? `seen ${relTime(m.auctions_ts)}` : 'open the Auctions tab at the AH')}
  </div>`;

  const nothing = !m.log.length && !m.auctions.length;
  el.querySelector('#ms-body').innerHTML = `
    ${nothing ? `<div class="callout">${m.installed
      ? 'Nothing recorded yet. Open your mailbox (AH mail) and the Auctions tab at the Auction House, then /reload.'
      : INSTALL_TEXT}</div>` : ''}
    <section class="card card-flush">
      <div class="card-head pad-x"><h2>My active auctions</h2><span class="card-note">${scanAge(m.scan)}</span></div>
      <div id="ms-auctions"></div>
    </section>
    <section class="card card-flush">
      <div class="card-head pad-x"><h2>Per item</h2><span class="card-note">last ${days} days</span></div>
      <div id="ms-items"></div>
    </section>
    <section class="card card-flush">
      <div class="card-head pad-x"><h2>AH mail log</h2></div>
      <div id="ms-log"></div>
    </section>`;

  new DataTable(el.querySelector('#ms-auctions'), [
    { key: 'name', label: 'Item', defaultDir: 'asc', sort: (r) => String(r.name || '').toLowerCase(), render: (r) => itemLink(r.item_id, r.name, r.quality) },
    { key: 'count', label: 'Qty', align: 'right', render: (r) => num(r.count) },
    { key: 'unit', label: 'My price', align: 'right', render: (r) => money(r.unit) },
    { key: 'lowest_other', label: 'Lowest other', align: 'right', render: (r) => money(r.lowest_other) },
    {
      key: 'status', label: 'Status', sort: (r) => ({ undercut: 0, lowest: 1, sold: 2 }[r.status] ?? 3),
      render: (r) => {
        if (r.status === 'sold') return '<span class="status-pill status-sold">Sold</span>';
        if (r.status === 'undercut') return `<span class="status-pill status-undercut" title="${num(r.cheaper_qty)} cheaper unit(s) listed">Undercut</span>`;
        if (r.status === 'lowest') return '<span class="status-pill status-lowest">Lowest</span>';
        return '<span class="muted">–</span>';
      },
    },
    { key: 'time_left', label: 'Time left', hideSm: true, render: (r) => esc(r.time_left_text || '–') },
    { key: 'char', label: 'Character', hideMd: true, render: (r) => esc(r.char || '') },
  ], { sortKey: 'status', sortDir: 'asc', rowKey: (r, i) => `${r.char}-${r.item_id}-${r.count}-${r.buyout}-${r.time_left}`, limit: 50, empty: emptyState('No active auctions recorded', 'Open the Auctions tab at the Auction House so the addon can see them.', '✉') }).setRows(m.auctions);

  new DataTable(el.querySelector('#ms-items'), [
    { key: 'name', label: 'Item', defaultDir: 'asc', sort: (r) => String(r.name || '').toLowerCase(), render: (r) => (r.item_id ? itemLink(r.item_id, r.name, r.quality) : esc(r.name || '?')) },
    { key: 'sold', label: 'Sold', align: 'right', render: (r) => num(r.sold) },
    { key: 'sold_net', label: 'Earned', align: 'right', render: (r) => money(r.sold_net) },
    { key: 'avg_sold_unit', label: 'Avg price', align: 'right', render: (r) => money(r.avg_sold_unit) },
    { key: 'expired', label: 'Expired', align: 'right', hideSm: true, render: (r) => num(r.expired + r.cancelled) },
    { key: 'sell_through', label: 'Sell-through', align: 'right', hideSm: true, render: (r) => (isNum(r.sell_through) ? pct(r.sell_through, 0) : '–') },
    { key: 'bought_value', label: 'Bought', align: 'right', hideMd: true, render: (r) => (r.bought ? `${num(r.bought)} · ${money(r.bought_value)}` : '<span class="muted">–</span>') },
  ], { sortKey: 'sold_net', sortDir: 'desc', rowKey: (r) => `${r.item_id}-${r.name}`, limit: 50, empty: emptyState('No AH mail recorded', 'Open your mailbox in game, then /reload.', '✉') }).setRows(m.items);

  new DataTable(el.querySelector('#ms-log'), [
    { key: 'ts', label: 'When', render: (r) => `<span title="${esc(relTime(r.ts))}">${tsFmt(r.ts)}</span>` },
    { key: 'kind', label: 'Type', render: (r) => { const k = KIND[r.kind] || [r.kind, '']; return `<span class="status-pill ${k[1]}">${esc(k[0])}</span>`; } },
    { key: 'name', label: 'Item', defaultDir: 'asc', sort: (r) => String(r.name || '').toLowerCase(), render: (r) => (r.item_id ? itemLink(r.item_id, r.name, r.quality) : esc(r.name || '?')) },
    { key: 'count', label: 'Qty', align: 'right', render: (r) => num(r.count) },
    { key: 'price', label: 'Price', align: 'right', render: (r) => (r.price ? money(r.price) : '<span class="muted">–</span>') },
    { key: 'unit', label: 'Each', align: 'right', hideSm: true, render: (r) => money(r.unit) },
    { key: 'player', label: 'Buyer / seller', hideMd: true, render: (r) => esc(r.player || '') },
  ], { sortKey: 'ts', sortDir: 'desc', rowKey: (r, i) => `${r.ts}-${r.kind}-${r.name}-${r.price}-${r.count}`, limit: 50 }).setRows(m.log);
}
