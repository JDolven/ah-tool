import { api } from '../api.js';
import {
  esc, money, num, pct, itemLink, reasonBadge, discountBar, REASONS, debounce,
  noData, errorState, skeletonRows, emptyState, isNum, confBadge, flagChip, tile, relTime,
} from '../util.js';
import { DataTable } from '../table.js';
import { watchBtn } from '../ui.js';

const state = {
  threshold: 25,
  reasons: new Set(),
  minGold: 0,
  text: '',
  includeLow: false,
};

export async function render(ctx) {
  const { el, query } = ctx;
  if (query.threshold) state.threshold = Math.max(10, Math.min(60, Number(query.threshold) || 25));

  el.innerHTML = `
    <header class="page-head">
      <div>
        <h1>Deals</h1>
        <p class="lead">Items priced below <strong>market price</strong> – the median of the daily lowest price over the 14 days before the latest scan – or below what a vendor pays.</p>
      </div>
    </header>
    <div id="vflip"></div>
    <section class="card filters">
      <div class="filter-row">
        <label class="field field-slider">
          <span class="field-label">Minimum discount <strong id="th-val">${state.threshold} %</strong></span>
          <input type="range" id="th" min="10" max="60" step="5" value="${state.threshold}" aria-label="Minimum discount in percent">
          <span class="slider-scale"><span>10 %</span><span>35 %</span><span>60 %</span></span>
        </label>
        <label class="field">
          <span class="field-label">Min. market price (gold)</span>
          <input type="number" id="minv" min="0" step="1" value="${state.minGold}" inputmode="decimal">
        </label>
        <label class="field field-grow">
          <span class="field-label">Filter</span>
          <input type="search" id="txt" placeholder="Name or ID…" value="${esc(state.text)}">
        </label>
        <label class="toggle" title="Also show below-market deals whose market price rests on very little data"><input type="checkbox" id="inc-low" ${state.includeLow ? 'checked' : ''}><span class="toggle-ui"></span> Include low-confidence</label>
      </div>
      <div class="chips" role="group" aria-label="Filter by reason">
        ${Object.entries(REASONS)
          .map(([k, r]) => `<button class="chip ${r.cls} ${state.reasons.has(k) ? 'on' : ''}" data-reason="${k}" aria-pressed="${state.reasons.has(k)}"><span aria-hidden="true">${r.icon}</span> ${esc(r.label)}</button>`)
          .join('')}
        <span class="chips-note">None selected = all</span>
      </div>
    </section>
    <div class="result-bar" id="deal-summary"></div>
    <section class="card card-flush" id="deal-table">${skeletonRows(10, 6)}</section>`;

  let watched = new Map(); // item_id -> {max_price, note}
  const table = new DataTable(el.querySelector('#deal-table'), columns(() => watched), {
    sortKey: 'score',
    sortDir: 'desc',
    rowKey: (r) => r.item_id,
    rowClass: (r) => ((r.reasons || []).includes('under_vendor') ? 'row-vendor' : '') + (r.confidence === 'low' ? ' row-lowconf' : ''),
    empty: emptyState('No deals match the filters', 'Lower the discount threshold or remove some filters.'),
  });

  const onWatch = (e) => {
    if (!ctx.alive()) return window.removeEventListener('ahtool:watch', onWatch);
    const { item_id, watch } = e.detail || {};
    if (watch) watched.set(String(item_id), watch);
    else watched.delete(String(item_id));
  };
  window.addEventListener('ahtool:watch', onWatch);

  let data = [];
  const apply = () => {
    const minC = (Number(state.minGold) || 0) * 10000;
    const t = state.text.trim().toLowerCase();
    const rows = data.filter((d) => {
      if (state.reasons.size && !(d.reasons || []).some((r) => state.reasons.has(r))) return false;
      const ref = isNum(d.baseline) ? d.baseline : d.price;
      if (minC && !(isNum(ref) && ref >= minC)) return false;
      if (t && !(String(d.name || '').toLowerCase().includes(t) || String(d.item_id) === t)) return false;
      return true;
    });
    table.setRows(rows);
    const potential = rows.reduce((s, d) => s + (isNum(d.baseline) && isNum(d.price) ? Math.max(0, d.baseline - d.price) : 0), 0);
    el.querySelector('#deal-summary').innerHTML = `<span><strong>${num(rows.length)}</strong> of ${num(data.length)} deals</span>
      <span class="muted">Total discount per unit: ${money(potential)}</span>`;
  };

  const load = async () => {
    try {
      const [res, wl] = await Promise.all([
        api.deals(state.threshold / 100, 0, '', state.includeLow),
        api.watchlist().catch(() => []),
      ]);
      if (!ctx.alive()) return;
      watched = new Map((Array.isArray(wl) ? wl : []).map((w) => [String(w.item_id), w]));
      data = Array.isArray(res) ? res : [];
      if (!data.length) {
        const st = await api.status().catch(() => null);
        if (!ctx.alive()) return;
        if (st && !st.items_priced) {
          el.querySelector('#deal-table').innerHTML = noData();
          el.querySelector('#deal-summary').innerHTML = '';
          return;
        }
      }
      apply();
    } catch (err) {
      if (ctx.alive()) el.querySelector('#deal-table').innerHTML = errorState(err);
    }
  };

  const reload = debounce(() => {
    el.querySelector('#deal-table').innerHTML = skeletonRows(10, 6);
    load();
  }, 250);

  el.querySelector('#th').addEventListener('input', (e) => {
    state.threshold = Number(e.target.value);
    el.querySelector('#th-val').textContent = `${state.threshold} %`;
    reload();
  });
  el.querySelector('#inc-low').addEventListener('change', (e) => {
    state.includeLow = e.target.checked;
    el.querySelector('#deal-table').innerHTML = skeletonRows(10, 6);
    load();
  });
  el.querySelector('#minv').addEventListener('input', debounce((e) => {
    state.minGold = Math.max(0, Number(e.target.value) || 0);
    apply();
  }, 200));
  el.querySelector('#txt').addEventListener('input', debounce((e) => {
    state.text = e.target.value;
    apply();
  }, 150));
  el.querySelectorAll('.chip[data-reason]').forEach((b) =>
    b.addEventListener('click', () => {
      const k = b.dataset.reason;
      if (state.reasons.has(k)) state.reasons.delete(k);
      else state.reasons.add(k);
      b.classList.toggle('on', state.reasons.has(k));
      b.setAttribute('aria-pressed', String(state.reasons.has(k)));
      apply();
    })
  );

  await Promise.all([load(), loadFlips(el, ctx)]);
}

// ---- buy everything below vendor price (AH Tool Logger full scan: every listing, not just the cheapest)
async function loadFlips(el, ctx) {
  const box = el.querySelector('#vflip');
  let r;
  try {
    r = await api.vendorFlips();
  } catch (err) {
    if (ctx.alive()) box.innerHTML = '';
    return;
  }
  if (!ctx.alive()) return;
  if (!r || !r.scan) {
    box.innerHTML = '';
    return;
  }
  const t = r.totals || {};
  const unverified = (t.profit || 0) - (t.profit_game_price || 0);
  box.innerHTML = `
    <section class="card vflip-card">
      <div class="card-head"><h2>Buy everything below vendor price</h2>
        <span class="card-note">Every listing in the full scan of ${esc(relTime(r.scan.ts))} whose buyout is under what a vendor pays – bought and sold to a vendor (no AH cut)</span></div>
      <div class="tiles tiles-compact">
        ${tile('Profit', `<span class="${t.profit > 0 ? 'up' : ''}">${money(t.profit)}</span>`, unverified > 0 ? `${money(unverified)} of it uses Classic vendor prices` : 'all vendor prices from the game')}
        ${tile('Cost', money(t.cost), `vendor pays ${money(t.revenue)}`)}
        ${tile('Units', num(t.qty), `${num(t.listings)} listings`)}
        ${tile('Items', num(t.items))}
      </div>
      <div id="vflip-table"></div>
    </section>`;
  if (!(r.items || []).length) {
    box.querySelector('#vflip-table').innerHTML = emptyState('Nothing below vendor price', 'No listing in the latest full scan sells for less than a vendor pays.');
    return;
  }
  new DataTable(box.querySelector('#vflip-table'), [
    { key: 'name', label: 'Item', sort: (x) => String(x.name || '').toLowerCase(),
      render: (x) => `${itemLink(x.item_id, x.name, x.quality)}${x.game_price ? '' : ' <span class="tag-mini warn" title="Vendor price from the Classic Era tables – Forever may pay less. A full scan with AH Tool Logger 1.5 or seeing the item in your bags fetches the real price.">Classic price</span>'}` },
    { key: 'qty', label: 'Units', align: 'right', render: (x) => `${num(x.qty)}<div class="cell-sub">${num(x.listings)} listing${x.listings === 1 ? '' : 's'}</div>` },
    { key: 'max_unit', label: 'Pay up to', align: 'right', hideSm: true, render: (x) => money(x.max_unit) },
    { key: 'vendor_sell', label: 'Vendor pays', align: 'right', hideSm: true, render: (x) => money(x.vendor_sell) },
    { key: 'cost', label: 'Cost', align: 'right', hideSm: true, render: (x) => money(x.cost) },
    { key: 'profit', label: 'Profit', align: 'right', defaultDir: 'desc', render: (x) => `<span class="up">${money(x.profit, { signed: true })}</span>` },
  ], { sortKey: 'profit', sortDir: 'desc', rowKey: (x) => x.item_id, limit: 25 }).setRows(r.items);
}

function columns(getWatched) {
  return [
    {
      key: 'name',
      label: 'Item',
      defaultDir: 'asc',
      sort: (r) => (r.name || `Item #${r.item_id}`).toLowerCase(),
      render: (r) => `${itemLink(r.item_id, r.name, r.quality)}${confBadge(r.confidence)}${r.price_flag ? ' ' + flagChip(r.price_flag) : ''}${r.days_of_data != null && r.days_of_data < 5 ? ' <span class="tag-mini" title="Little history">few days</span>' : ''}${
        r.for_my_crafts ? ' <span class="badge b-mine" title="Reagent for a recipe you can craft">✓ My crafts</span>' : ''}`,
    },
    { key: 'price', label: 'Current price', align: 'right', render: (r) => money(r.price) },
    { key: 'baseline', label: 'Market price', align: 'right', title: 'Median of last 14 days', render: (r) => money(r.baseline) },
    { key: 'discount', label: 'Discount', render: (r) => discountBar(r.discount) },
    {
      key: 'reasons',
      label: 'Reason',
      sortable: false,
      render: (r) =>
        `<div class="badges">${(r.reasons || [])
          .map((x) => reasonBadge(x, x === 'cheap_reagent' && r.professions && r.professions.length ? 'Used in: ' + r.professions.join(', ') : ''))
          .join('')}</div>${
          (r.reasons || []).includes('cheap_reagent') && r.professions && r.professions.length
            ? `<div class="cell-sub">${esc(r.professions.join(', '))}</div>`
            : ''
        }`,
    },
    { key: 'vendor_profit', label: 'Vendor profit', align: 'right', hideSm: true, render: (r) => (isNum(r.vendor_profit) && r.vendor_profit > 0 ? money(r.vendor_profit, { signed: true }) : '<span class="muted">–</span>') },
    { key: 'available', label: 'Qty', align: 'right', hideSm: true, render: (r) => num(r.available) },
    { key: 'score', label: 'Score', align: 'right', hideSm: true, render: (r) => (isNum(r.score) ? r.score.toFixed(2) : '–') },
    {
      key: 'watch',
      label: '',
      sortable: false,
      align: 'right',
      render: (r) => watchBtn({ id: r.item_id, name: r.name, quality: r.quality, price: r.price, watch: getWatched().get(String(r.item_id)) || null }, { iconOnlySm: true }),
    },
  ];
}
