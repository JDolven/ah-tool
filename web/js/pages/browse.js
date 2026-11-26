// Browse: every item with an AH price, by Auction House category (class -> subclass -> slot), like the game.
import { api } from '../api.js';
import {
  esc, money, num, itemLink, discountBar, debounce, errorState, skeletonRows, emptyState, isNum, dateLong,
  confBadge, QUALITY_NAMES, QUALITY_COLORS,
} from '../util.js';
import { DataTable } from '../table.js';

const LIMIT = 1000;

function readState(query) {
  const int = (v) => (v !== undefined && v !== '' && Number.isFinite(Number(v)) ? Number(v) : null);
  return {
    cls: int(query.class),
    sub: int(query.sub),
    slot: query.slot || '',
    q: query.q || '',
    min: int(query.min),
    max: int(query.max),
    quality: int(query.quality),
    all: query.all === '1',
  };
}

function writeHash(st) {
  const q = new URLSearchParams();
  if (st.cls !== null) q.set('class', String(st.cls));
  if (st.sub !== null) q.set('sub', String(st.sub));
  if (st.slot) q.set('slot', st.slot);
  if (st.q) q.set('q', st.q);
  if (st.min !== null) q.set('min', String(st.min));
  if (st.max !== null) q.set('max', String(st.max));
  if (st.quality !== null) q.set('quality', String(st.quality));
  if (st.all) q.set('all', '1');
  const h = `#/browse${q.toString() ? `?${q}` : ''}`;
  if (location.hash !== h) history.replaceState(null, '', h);
}

function treeHtml(cats, st) {
  const on = (c, s, sl) => st.cls === c && st.sub === s && (st.slot || '') === (sl || '');
  const btn = (label, count, c, s, sl, depth) => `<button type="button" class="br-node d${depth} ${on(c, s, sl) ? 'on' : ''}"
      data-cls="${c ?? ''}" data-sub="${s ?? ''}" data-slot="${esc(sl || '')}" aria-pressed="${on(c, s, sl)}">
      <span class="br-label">${esc(label)}</span><span class="br-count">${num(count)}</span></button>`;
  const total = cats.reduce((a, c) => a + c.count, 0);
  let html = btn('All items', total, null, null, '', 0);
  for (const c of cats) {
    html += btn(c.name, c.count, c.id, null, '', 0);
    if (st.cls !== c.id) continue;
    for (const s of c.subclasses) {
      html += btn(s.name, s.count, c.id, s.id, '', 1);
      if (st.sub !== s.id || s.slots.length < 2) continue;
      for (const sl of s.slots) html += btn(sl.name, sl.count, c.id, s.id, sl.name, 2);
    }
  }
  return html;
}

export async function render(ctx) {
  const { el, query } = ctx;
  const st = readState(query);
  const qualityOpts = QUALITY_NAMES.slice(0, 6)
    .map((n, i) => `<option value="${i}" ${st.quality === i ? 'selected' : ''} style="color:${QUALITY_COLORS[i]}">${esc(n)}+</option>`)
    .join('');
  el.innerHTML = `
    <header class="page-head"><div><h1>Browse</h1>
      <p class="lead">Every item with an AH price, sorted into the Auction House categories. Pick a category on the left.</p></div></header>
    <div class="br-layout">
      <nav class="card br-tree" id="br-tree" aria-label="Categories">${skeletonRows(10, 1)}</nav>
      <div class="br-main">
        <section class="card filters">
          <div class="filter-row">
            <label class="field field-grow"><span class="field-label">Search</span>
              <input type="search" id="br-q" placeholder="Name or ID…" value="${esc(st.q)}"></label>
            <label class="field br-lvl"><span class="field-label">Req. level</span>
              <span class="br-range"><input type="number" id="br-min" min="0" max="60" inputmode="numeric" placeholder="min" value="${st.min ?? ''}">
              <span aria-hidden="true">–</span>
              <input type="number" id="br-max" min="0" max="60" inputmode="numeric" placeholder="max" value="${st.max ?? ''}"></span></label>
            <label class="field"><span class="field-label">Quality</span>
              <select id="br-quality" class="select-in"><option value="">Any</option>${qualityOpts}</select></label>
            <label class="toggle" title="Also show items that were not on the AH in the latest scans (last known price)"><input type="checkbox" id="br-all" ${st.all ? 'checked' : ''}><span class="toggle-ui"></span> Include not listed now</label>
          </div>
        </section>
        <div class="result-bar" id="br-summary"></div>
        <section class="card card-flush" id="br-table">${skeletonRows(12, 6)}</section>
      </div>
    </div>`;

  const table = new DataTable(el.querySelector('#br-table'), [
    {
      key: 'name', label: 'Item', sort: (r) => String(r.name || '').toLowerCase(),
      render: (r) => `${itemLink(r.item_id, r.name, r.quality)}<div class="cell-sub">${esc([r.slot, r.stale && r.last_date ? `last seen ${dateLong(r.last_date)}` : ''].filter(Boolean).join(' · '))}</div>`,
    },
    { key: 'required_level', label: 'Lvl', align: 'right', title: 'Required level', render: (r) => (isNum(r.required_level) && r.required_level > 0 ? num(r.required_level) : '<span class="muted">–</span>') },
    { key: 'item_level', label: 'iLvl', align: 'right', hideSm: true, render: (r) => (isNum(r.item_level) ? num(r.item_level) : '–') },
    { key: 'price', label: 'Price', align: 'right', title: 'Lowest unit buyout in the latest scan', render: (r) => money(r.price) },
    { key: 'baseline', label: 'Market price', align: 'right', hideSm: true, title: 'Median of the last 14 days', render: (r) => `${money(r.baseline)}${r.confidence === 'low' ? ` ${confBadge('low')}` : ''}` },
    { key: 'discount', label: 'Discount', hideMd: true, render: (r) => discountBar(r.discount) },
    { key: 'available', label: 'Qty', align: 'right', defaultDir: 'desc', render: (r) => num(r.available) },
    { key: 'vendor_sell', label: 'Vendor', align: 'right', hideSm: true, render: (r) => (r.vendor_sell ? money(r.vendor_sell) : '<span class="muted">–</span>') },
  ], {
    sortKey: 'available', sortDir: 'desc', rowKey: (r) => r.item_id, limit: 100,
    rowClass: (r) => (r.stale ? 'row-stale' : ''),
    empty: emptyState('No items here', 'Try another category or loosen the filters.'),
  });

  let token = 0;
  const load = async () => {
    const my = ++token;
    writeHash(st);
    let r;
    try {
      r = await api.browse({ class: st.cls, sub: st.sub, slot: st.slot, q: st.q, min_level: st.min, max_level: st.max, quality: st.quality, listed: !st.all, limit: LIMIT });
    } catch (err) {
      if (ctx.alive() && my === token) el.querySelector('#br-table').innerHTML = errorState(err);
      return;
    }
    if (!ctx.alive() || my !== token) return;
    el.querySelector('#br-tree').innerHTML = treeHtml(r.categories || [], st);
    table.setRows(r.items || []);
    el.querySelector('#br-summary').innerHTML = `<span><strong>${num(r.total)}</strong> item${r.total === 1 ? '' : 's'}</span>${r.total > r.items.length ? `<span class="muted">showing the ${num(r.items.length)} with the most listed – narrow the search for the rest</span>` : ''}`;
  };
  const loadSoon = debounce(load, 250);

  el.querySelector('#br-tree').addEventListener('click', (e) => {
    const b = e.target.closest('.br-node');
    if (!b) return;
    const v = (x) => (x === '' ? null : Number(x));
    st.cls = v(b.dataset.cls);
    st.sub = v(b.dataset.sub);
    st.slot = b.dataset.slot || '';
    load();
  });
  el.querySelector('#br-q').addEventListener('input', (e) => { st.q = e.target.value.trim(); loadSoon(); });
  const lvl = (id, key) => el.querySelector(id).addEventListener('input', (e) => {
    const n = e.target.value === '' ? null : Math.max(0, Math.min(60, Math.round(Number(e.target.value) || 0)));
    st[key] = n;
    loadSoon();
  });
  lvl('#br-min', 'min');
  lvl('#br-max', 'max');
  el.querySelector('#br-quality').addEventListener('change', (e) => { st.quality = e.target.value === '' ? null : Number(e.target.value); load(); });
  el.querySelector('#br-all').addEventListener('change', (e) => { st.all = e.target.checked; load(); });

  await load();
}
