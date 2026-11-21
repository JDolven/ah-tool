import { api, WEB } from '../api.js';
import {
  esc, money, num, pct, pctRaw, itemName, qClass, QUALITY_NAMES, wowheadLink, dateLong, dateShort,
  tsFmt, discountBar, errorState, skeletonTiles, skeletonRows, emptyState, tile, isNum, itemLink,
  moneyText, diffClass, ttAttr, confBadge, flagChip, PRICE_FLAGS, CONF_TEXT,
} from '../util.js';
import { mountChart, chartBox, moneyAxis, plainAxis, alpha, destroyAll } from '../charts.js';
import { DataTable } from '../table.js';
import { recipeColumns, recipeExpand, mountRecipeTrends } from './professions.js';
import { watchBtn } from '../ui.js';
import { renderItemMarket } from '../market.js';

let range = 30;

export async function render(ctx) {
  const { el, params } = ctx;
  const id = params[0];
  el.innerHTML = `
    <nav class="crumbs"><a href="#/">Overview</a> <span>/</span> <span>Item #${esc(id)}</span></nav>
    <header class="item-head"><span class="skel skel-lg" style="width:280px"></span></header>
    ${skeletonTiles(5)}
    <section class="card">${skeletonRows(6, 1)}</section>`;

  let it;
  try {
    it = await api.item(id);
  } catch (err) {
    if (!ctx.alive()) return;
    el.innerHTML = `<nav class="crumbs"><a href="#/">Overview</a></nav>` +
      (err.status === 404
        ? emptyState(`Item #${id} not found`, 'The item is not in the database, or has never been scanned.', '?')
        : errorState(err));
    return;
  }
  if (!ctx.alive()) return;

  const name = itemName(it.name, it.id);
  document.title = `${name} – AH Tool`;
  const cur = it.current || {};
  const st = it.stats || {};
  const disc = isNum(cur.price) && isNum(st.baseline) && st.baseline > 0 ? 1 - cur.price / st.baseline : null;
  const vendorDeal = isNum(cur.price) && isNum(it.vendor_sell) && it.vendor_sell > 0 && cur.price < it.vendor_sell;

  el.innerHTML = `
    <nav class="crumbs"><a href="#/">Overview</a> <span>/</span> <span>${esc(name)}</span></nav>
    <header class="item-head">
      <div>
        <h1 class="item-title ${qClass(it.quality)}" ${ttAttr(it.id)}>${esc(name)}</h1>
        <div class="item-meta">
          <span class="qual-tag ${qClass(it.quality)}">${esc(QUALITY_NAMES[it.quality] || 'Unknown quality')}</span>
          ${isNum(it.item_level) ? `<span>Item level ${it.item_level}</span>` : ''}
          <span>ID ${esc(it.id)}</span>
          ${wowheadLink(it.id)}
        </div>
      </div>
      <div class="item-price">
        <div class="item-price-label">Current price${cur.stale ? ' <span class="tag-mini warn" title="Not seen in the latest scans">stale</span>' : ''}${confBadge(cur.confidence, { showHigh: true })}</div>
        <div class="item-price-val">${money(cur.price)}</div>
        ${cur.price_flag ? `<div class="item-price-flag">${flagChip(cur.price_flag)}</div>` : ''}
        <div class="item-price-sub">${isNum(cur.available) ? `${num(cur.available)} pcs` : ''}${cur.last_date ? ` · seen ${dateLong(cur.last_date)}` : ''}</div>
        <div class="item-actions">
          ${watchBtn({ id: it.id, name: it.name, quality: it.quality, price: cur.price, watch: it.watch || null }, { small: false })}
          ${it.watch ? `<span class="item-watch-note">${isNum(it.watch.max_price) ? `Alert at ≤ ${money(it.watch.max_price)}` : 'Alert on any deal'}${it.watch.enabled === 0 || it.watch.enabled === false ? ' · paused' : ''}</span>` : ''}
        </div>
      </div>
    </header>

    <div class="tiles tiles-5">
      ${tile('Market price', money(st.baseline), 'median of last 14 days')}
      ${tile('Discount now', isNum(disc) ? `<span class="${disc > 0 ? 'up' : 'down'}">${pct(disc, 0)}</span>` : '–', isNum(disc) ? (disc > 0 ? 'below market price' : 'above market price') : 'not enough history')}
      ${tile('7-day trend', `<span class="${diffClass(st.trend7_pct)}">${pctRaw(st.trend7_pct)}</span>`, `7d avg ${money(st.avg7)}`)}
      ${tile('Vendor', money(it.vendor_sell), isNum(it.vendor_buy) && it.vendor_buy > 0 ? `buy from vendor ${money(it.vendor_buy)}` : 'sell price to NPC', vendorDeal ? 'tile-hot' : '')}
      ${tile('30 days', `${money(st.min30)} <span class="muted">–</span> ${money(st.max30)}`, `median ${money(st.median30)} · ${num(st.days_of_data)} days of data`)}
    </div>
    ${cur.price_flag && PRICE_FLAGS[cur.price_flag] ? `<div class="callout callout-thin">${flagChip(cur.price_flag)} <span>${esc(PRICE_FLAGS[cur.price_flag])}.${cur.price_flag === 'thin_high' ? ' Crafting and shopping calculations use the market price instead.' : ' It still counts as a deal, but check the listing before buying.'}</span></div>` : ''}
    ${cur.confidence === 'low' && !cur.price_flag ? `<div class="callout callout-low">${confBadge('low')} <span>${esc(CONF_TEXT.low)} – treat these numbers as rough.</span></div>` : ''}
    ${vendorDeal ? `<div class="callout callout-vendor">◆ Under vendor price! Buy and sell to an NPC for ${money(it.vendor_sell - cur.price, { signed: true })} per unit.</div>` : ''}

    <section class="card">
      <div class="card-head">
        <h2>Price history</h2>
        <div class="seg seg-sm" id="range">
          ${[[30, '30d'], [90, '90d'], [0, 'All']].map(([v, l]) => `<button class="seg-btn ${range === v ? 'on' : ''}" data-range="${v}">${l}</button>`).join('')}
        </div>
      </div>
      <div id="hist-box"></div>
      <div class="stat-strip">
        <span>30d avg <strong>${money(st.avg30)}</strong></span>
        <span>Volatility <strong>${isNum(st.volatility) ? pct(st.volatility, 0) : '–'}</strong></span>
        <span>Days of data <strong>${num(st.days_of_data)}</strong></span>
        ${outlierCount(it) ? `<span title="Odd days (e.g. a single extreme listing) are ignored for market price, averages and trend">Outlier days ignored <strong>${num(outlierCount(it))}</strong></span>` : ''}
      </div>
    </section>

    <section class="card" id="mkt-sec"></section>

    <div class="grid-2">
      <section class="card">
        <div class="card-head"><h2>Price changes</h2><span class="card-note">Recorded at each import</span></div>
        <div id="snaps"></div>
      </section>
      ${WEB ? '' : `<section class="card">
        <div class="card-head"><h2>My auctions</h2></div>
        <div id="my-posts"></div>
      </section>`}
    </div>

    <section class="card card-flush" id="crafted-sec">
      <div class="card-head pad-x"><h2>Crafted by</h2></div>
      <div id="crafted"></div>
    </section>
    <section class="card card-flush" id="used-sec">
      <div class="card-head pad-x"><h2>Used in</h2></div>
      <div id="used"></div>
    </section>`;

  const onWatch = (e) => {
    if (!ctx.alive()) return window.removeEventListener('ahtool:watch', onWatch);
    const d = e.detail || {};
    if (String(d.item_id) !== String(it.id)) return;
    const box = el.querySelector('.item-actions');
    if (!box) return;
    box.querySelector('.item-watch-note')?.remove();
    if (d.watch) {
      box.insertAdjacentHTML('beforeend', `<span class="item-watch-note">${isNum(d.watch.max_price) ? `Alert at ≤ ${money(d.watch.max_price)}` : 'Alert on any deal'}</span>`);
    }
  };
  window.addEventListener('ahtool:watch', onWatch);

  renderHistory(el, it);
  renderItemMarket(el.querySelector('#mkt-sec'), it.id, ctx.alive);
  el.querySelectorAll('#range .seg-btn').forEach((b) =>
    b.addEventListener('click', () => {
      range = Number(b.dataset.range);
      el.querySelectorAll('#range .seg-btn').forEach((x) => x.classList.toggle('on', x === b));
      destroyAll();
      renderHistory(el, it);
    })
  );

  // Snapshots
  const snaps = (it.snapshots || []).slice().sort((a, b) => b.ts - a.ts);
  el.querySelector('#snaps').innerHTML = snaps.length
    ? `<div class="scroll-list"><table class="mini"><thead><tr><th>Time</th><th class="r">Price</th><th class="r">Change</th><th class="r">Qty</th></tr></thead><tbody>${snaps
        .slice(0, 60)
        .map((s, i) => {
          const prev = snaps[i + 1];
          const ch = prev && isNum(prev.price) && prev.price > 0 && isNum(s.price) ? (s.price - prev.price) / prev.price : null;
          return `<tr><td>${tsFmt(s.ts)}</td><td class="r">${money(s.price)}</td><td class="r ${diffClass(ch)}">${isNum(ch) ? pct(ch, 0, true) : ''}</td><td class="r">${num(s.available)}</td></tr>`;
        })
        .join('')}</tbody></table></div>`
    : emptyState('No changes recorded yet', 'Snapshots are saved when the price changes between imports.');

  // My postings
  const posts = (it.postings || []).slice().sort((a, b) => b.ts - a.ts);
  if (el.querySelector('#my-posts')) el.querySelector('#my-posts').innerHTML = posts.length
    ? `<div class="scroll-list"><table class="mini"><thead><tr><th>Posted</th><th class="r">Price</th><th class="r">Qty</th><th class="r">vs current</th></tr></thead><tbody>${posts
        .map((p) => {
          const d = isNum(cur.price) && cur.price > 0 && isNum(p.price) ? ((p.price - cur.price) / cur.price) * 100 : null;
          return `<tr><td>${tsFmt(p.ts)}</td><td class="r">${money(p.price)}</td><td class="r">${num(p.quantity)}</td><td class="r ${diffClass(d)}">${pctRaw(d, 0)}</td></tr>`;
        })
        .join('')}</tbody></table></div>`
    : emptyState('You have not posted this item', '', '—');

  // Crafted by
  const crafted = it.crafted_by || [];
  if (crafted.length) {
    new DataTable(el.querySelector('#crafted'), recipeColumns({ shop: true }), {
      sortKey: 'profit', sortDir: 'desc', rowKey: (r) => r.spell_id, expand: recipeExpand, onRender: mountRecipeTrends,
    }).setRows(crafted);
  } else {
    el.querySelector('#crafted-sec').remove();
  }

  // Used in
  const used = it.used_in || [];
  if (used.length) {
    new DataTable(el.querySelector('#used'), [
      { key: 'name', label: 'Recipe', defaultDir: 'asc', render: (u) => esc(u.name || `Spell #${u.spell_id}`) },
      { key: 'profession', label: 'Profession', defaultDir: 'asc', render: (u) => (u.profession_id ? `<a href="#/professions/${encodeURIComponent(u.profession_id)}">${esc(u.profession || '')}</a>` : esc(u.profession || '–')) },
      { key: 'count', label: 'Count', align: 'right', render: (u) => num(u.count) },
    ], { sortKey: 'profession', sortDir: 'asc', limit: 15 }).setRows(used);
  } else {
    el.querySelector('#used-sec').remove();
  }
}

function outlierCount(it) {
  const st = it.stats || {};
  if (isNum(st.outlier_days)) return st.outlier_days;
  return (it.history || []).filter((h) => h.outlier).length;
}

function renderHistory(el, it) {
  const box = el.querySelector('#hist-box');
  let hist = (it.history || []).slice().sort((a, b) => (a.date < b.date ? -1 : 1));
  if (range) hist = hist.slice(-range);
  if (!hist.length) {
    box.innerHTML = emptyState('No price history yet', 'History builds up for each day you scan.');
    return;
  }
  box.innerHTML = `
    <div class="legend">
      <span class="lg"><i class="lg-line s1"></i>Lowest price</span>
      <span class="lg"><i class="lg-line s2 dashed"></i>Highest lowest-price</span>
      ${isNum((it.stats || {}).baseline) ? '<span class="lg"><i class="lg-line base"></i>Market price</span>' : ''}
      ${hist.some((h) => h.outlier) ? '<span class="lg" title="Odd days are left out of the price line, market price and averages"><i class="lg-ring"></i>Outlier (ignored)</span>' : ''}
    </div>
    ${chartBox('hist-chart', 280, 'Daily lowest and highest price')}
    <div class="sub-chart-label">Available quantity</div>
    ${chartBox('avail-chart', 90, 'Available quantity per day')}`;

  const labels = hist.map((h) => h.date);
  const baseline = (it.stats || {}).baseline;
  const pointR = hist.length > 45 ? 0 : 2.5;
  const hasOut = hist.some((h) => h.outlier);
  const clean = (v, h) => (h.outlier ? null : v);

  mountChart(el.querySelector('#hist-chart'), (c) => ({
    type: 'line',
    data: {
      labels,
      datasets: [
        {
          label: 'Lowest',
          data: hist.map((h) => clean(isNum(h.low) ? h.low : h.high, h)),
          borderColor: c.series[0],
          backgroundColor: alpha(c.series[0], 0.12),
          fill: 'origin',
          borderWidth: 2,
          tension: 0.25,
          pointRadius: pointR,
          pointHoverRadius: 5,
          pointBackgroundColor: c.series[0],
          pointBorderColor: c.surface,
          spanGaps: true,
        },
        {
          label: 'Highest',
          data: hist.map((h) => clean(h.high, h)),
          borderColor: c.series[1],
          borderDash: [5, 4],
          borderWidth: 2,
          tension: 0.25,
          pointRadius: 0,
          pointHoverRadius: 5,
          fill: false,
          spanGaps: true,
        },
        ...(isNum(baseline)
          ? [{
              label: 'Market price',
              data: hist.map(() => baseline),
              borderColor: c.muted,
              borderWidth: 1,
              borderDash: [2, 3],
              pointRadius: 0,
              pointHoverRadius: 0,
              fill: false,
            }]
          : []),
        ...(hasOut
          ? [{
              label: 'Outlier (ignored)',
              data: hist.map((h) => (h.outlier ? (isNum(h.low) ? h.low : h.high) : null)),
              showLine: false,
              pointStyle: 'circle',
              pointRadius: 5,
              pointHoverRadius: 7,
              pointBorderWidth: 2,
              pointBorderColor: c.down,
              pointBackgroundColor: 'transparent',
              borderColor: c.down,
              backgroundColor: 'transparent',
            }]
          : []),
      ],
    },
    options: {
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: (items) => dateLong(items[0].label),
            label: (ctx) => `${ctx.dataset.label}: ${moneyText(ctx.parsed.y)}`,
            afterBody: (items) => {
              const h = hist[items[0].dataIndex];
              return h && isNum(h.available) ? `Available: ${num(h.available)}` : '';
            },
          },
        },
      },
      scales: {
        x: plainAxis(c, { ticks: { color: c.muted, maxRotation: 0, autoSkipPadding: 20, callback(v) { return dateShort(this.getLabelForValue(v)); } } }),
        y: moneyAxis(c, { beginAtZero: false }),
      },
    },
  }));

  mountChart(el.querySelector('#avail-chart'), (c) => ({
    type: 'bar',
    data: {
      labels,
      datasets: [{
        label: 'Available',
        data: hist.map((h) => h.available),
        backgroundColor: alpha(c.series[2], 0.55),
        hoverBackgroundColor: c.series[2],
        borderRadius: 2,
        borderSkipped: 'start',
        maxBarThickness: 12,
      }],
    },
    options: {
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { title: (items) => dateLong(items[0].label), label: (ctx) => `Available: ${num(ctx.parsed.y)}` } },
      },
      scales: {
        x: { display: false },
        y: { beginAtZero: true, grid: { color: c.grid }, border: { display: false }, ticks: { color: c.muted, maxTicksLimit: 3 } },
      },
    },
  }));
}

export { itemLink };
