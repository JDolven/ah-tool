import { api } from '../api.js';
import {
  esc, money, num, pctRaw, itemLink, tsFmt, relTime, debounce,
  errorState, skeletonRows, skeletonTiles, emptyState, tile, isNum, diffClass, searchText
} from '../util.js';
import { DataTable } from '../table.js';

export async function render(ctx) {
  const { el } = ctx;
  el.innerHTML = `
    <header class="page-head"><div><h1>My auctions</h1><p class="lead">What you have posted via Auctionator, compared with the lowest price on the AH now.</p></div></header>
    <div id="po-tiles">${skeletonTiles(3)}</div>
    <section class="card filters">
      <div class="filter-row">
        <label class="field field-grow"><span class="field-label">Filter</span><input type="search" id="po-txt" placeholder="Name or ID…"></label>
        <label class="toggle"><input type="checkbox" id="po-under"><span class="toggle-ui"></span> Only where I am undercut</label>
      </div>
    </section>
    <section class="card card-flush" id="po-table">${skeletonRows(10, 6)}</section>`;

  let rows;
  try {
    rows = await api.postings();
  } catch (err) {
    if (ctx.alive()) {
      el.querySelector('#po-table').innerHTML = errorState(err);
      el.querySelector('#po-tiles').innerHTML = '';
    }
    return;
  }
  if (!ctx.alive()) return;
  rows = Array.isArray(rows) ? rows : [];
  if (!rows.length) {
    el.querySelector('#po-tiles').innerHTML = '';
    el.querySelector('#po-table').innerHTML = emptyState('No auctions recorded', 'Auctionator records history when you post items from the Selling tab. Do a /reload afterwards.', '✉');
    return;
  }

  const total = rows.reduce((s, r) => s + (isNum(r.price) ? r.price * (r.quantity || 1) : 0), 0);
  const undercut = rows.filter((r) => isNum(r.diff_pct) && r.diff_pct > 0).length;
  const latest = rows.reduce((m, r) => Math.max(m, r.ts || 0), 0);
  el.querySelector('#po-tiles').innerHTML = `<div class="tiles tiles-3">
    ${tile('Postings', num(rows.length), `latest ${relTime(latest)}`)}
    ${tile('Total posted', money(total), 'price × quantity')}
    ${tile('Undercut', num(undercut), 'my price is above the current lowest price', undercut ? 'tile-warn' : '')}
  </div>`;

  const table = new DataTable(el.querySelector('#po-table'), [
    { key: 'name', label: 'Item', defaultDir: 'asc', sort: (r) => String(r.name || '').toLowerCase(), render: (r) => itemLink(r.item_id, r.name, r.quality) },
    { key: 'ts', label: 'Posted', render: (r) => `<span title="${esc(relTime(r.ts))}">${tsFmt(r.ts)}</span>` },
    { key: 'price', label: 'My price', align: 'right', render: (r) => money(r.price) },
    { key: 'quantity', label: 'Qty', align: 'right', render: (r) => num(r.quantity) },
    { key: 'current_price', label: 'Current price', align: 'right', render: (r) => money(r.current_price) },
    {
      key: 'diff_pct',
      label: 'Difference',
      align: 'right',
      title: 'My price compared with the current lowest price',
      render: (r) => {
        if (!isNum(r.diff_pct)) return '<span class="muted">–</span>';
        const label = r.diff_pct > 0.5 ? 'undercut' : r.diff_pct < -0.5 ? 'cheapest' : 'equal';
        return `<span class="pill ${r.diff_pct > 0.5 ? 'pill-down' : 'pill-up'}" title="${label}">${pctRaw(r.diff_pct, 0)}</span>`;
      },
    },
  ], { sortKey: 'ts', sortDir: 'desc', rowKey: (r) => `${r.item_id}-${r.ts}-${r.price}-${r.quantity}`, limit: 100, empty: emptyState('No results') });

  let text = '';
  let onlyUnder = false;
  const apply = () => {
    const t = searchText(text);
    table.setRows(rows.filter((r) => {
      if (onlyUnder && !(isNum(r.diff_pct) && r.diff_pct > 0.5)) return false;
      if (t && !(String(r.name || '').toLowerCase().includes(t) || String(r.item_id) === t)) return false;
      return true;
    }));
  };
  el.querySelector('#po-txt').addEventListener('input', debounce((e) => { text = e.target.value; apply(); }, 150));
  el.querySelector('#po-under').addEventListener('change', (e) => { onlyUnder = e.target.checked; apply(); });
  apply();
}

export { diffClass };
