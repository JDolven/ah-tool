import { api } from '../api.js';
import {
  esc, money, num, pct, itemLink, dateLong, relTime, tsFmt, reasonBadge, discountBar,
  noData, errorState, skeletonTiles, skeletonRows, tile, emptyState, isNum,
  sparkline, trendPctHtml, confBadge,
} from '../util.js';
import { mountChart, chartBox, plainAxis, alpha } from '../charts.js';
import { addShopCtl } from '../ui.js';

export async function render(ctx) {
  const { el } = ctx;
  el.innerHTML = `
    <header class="page-head">
      <div>
        <h1>Overview</h1>
        <p class="lead">The market at a glance – latest scan, best deals and profitable crafts.</p>
      </div>
    </header>
    <section id="ov-status" class="status-hero">${skeletonRows(2, 3)}</section>
    <section id="ov-tiles">${skeletonTiles(4)}</section>
    <section id="ov-mine"></section>
    <div class="grid-2">
      <section class="card">
        <div class="card-head"><h2>Best deals now</h2><a class="card-link" href="#/deals">See all →</a></div>
        <div id="ov-deals">${skeletonRows(8, 3)}</div>
      </section>
      <section class="card">
        <div class="card-head"><h2>Most profitable crafts</h2><a class="card-link" href="#/professions">All professions →</a></div>
        <div id="ov-crafts">${skeletonRows(8, 3)}</div>
      </section>
    </div>
    <section class="card" id="ov-trend-card">
      <div class="card-head"><h2>Trending crafts</h2><span class="card-note">Profit of the last 3 days vs. the week before</span></div>
      <div id="ov-trend">${skeletonRows(4, 3)}</div>
    </section>
    <section class="card">
      <div class="card-head"><h2>Scan activity</h2><span class="card-note">Items seen per day</span></div>
      <div id="ov-scan">${chartBox('ov-scan-chart', 140, 'Scan activity per day')}</div>
    </section>`;

  let status;
  try {
    status = await api.status();
  } catch (err) {
    if (!ctx.alive()) return;
    el.querySelector('#ov-status').innerHTML = errorState(err);
    el.querySelector('#ov-tiles').innerHTML = '';
    el.querySelector('#ov-deals').innerHTML = '';
    el.querySelector('#ov-crafts').innerHTML = '';
    el.querySelector('#ov-scan').innerHTML = '';
    return;
  }
  if (!ctx.alive()) return;
  renderStatus(el.querySelector('#ov-status'), status);

  if (!status || !status.items_priced) {
    el.querySelector('#ov-tiles').innerHTML = '';
    el.querySelector('#ov-deals').innerHTML = noData();
    el.querySelector('#ov-crafts').innerHTML = noData();
    el.querySelector('#ov-scan').innerHTML = emptyState('No scans recorded');
    return;
  }

  const [market, deals] = await Promise.all([
    api.market().catch((e) => ({ __err: e })),
    api.deals(0.25, 0, '').catch((e) => ({ __err: e })),
  ]);
  if (!ctx.alive()) return;

  renderTiles(el.querySelector('#ov-tiles'), market, deals);
  renderDeals(el.querySelector('#ov-deals'), deals);
  renderScan(el.querySelector('#ov-scan'), market);
  renderCrafts(el.querySelector('#ov-crafts'), ctx);
  renderMine(el.querySelector('#ov-mine'), ctx);
  renderTrending(el.querySelector('#ov-trend'), ctx);
}

async function renderTrending(box, ctx) {
  let res;
  let profs = [];
  try {
    [res, profs] = await Promise.all([api.trendingCrafts(6), api.professions().catch(() => [])]);
  } catch (err) {
    if (ctx.alive()) box.innerHTML = errorState(err);
    return;
  }
  if (!ctx.alive()) return;
  const profName = (r) => r.profession_name || ((profs || []).find((p) => p.id === r.profession_id) || {}).name || '';
  const up = (res && res.up) || [];
  const down = (res && res.down) || [];
  if (!up.length && !down.length) {
    box.innerHTML = emptyState('No trends yet', 'Trends need a few days of scans for both reagents and the crafted item.', '↗');
    return;
  }
  const row = (r) => `<li class="list-row">
      <div class="list-main">
        <span class="trend-name">${r.created_item_id ? itemLink(r.created_item_id, r.name, r.created_quality) : `<span class="spell-name">${esc(r.name)}</span>`}${confBadge(r.confidence)}</span>
        <div class="list-meta">${r.profession_id ? `<a href="#/professions/${encodeURIComponent(r.profession_id)}" class="muted-link">${esc(profName(r))}</a>` : esc(profName(r))} · profit ${money(r.profit, { signed: true })}</div>
      </div>
      <div class="list-side trend-side">
        ${sparkline(r.spark, { w: 72, h: 22 })}
        ${trendPctHtml(r.profit_trend_pct)}
      </div>
    </li>`;
  const col = (title, cls, list, empty) => `<div class="trend-col">
      <h3 class="trend-h ${cls}">${title}</h3>
      ${list.length ? `<ul class="list">${list.map(row).join('')}</ul>` : `<div class="muted pad">${empty}</div>`}
    </div>`;
  box.innerHTML = `<div class="trend-cols">
    ${col('<span aria-hidden="true">▲</span> Rising profit', 'up', up, 'Nothing is rising right now.')}
    ${col('<span aria-hidden="true">▼</span> Falling profit', 'down', down, 'Nothing is falling right now.')}
  </div>`;
}

async function renderMine(box, ctx) {
  box.innerHTML = `<div class="card"><div class="card-head"><h2>Best crafts for you</h2></div>${skeletonRows(3, 3)}</div>`;
  let mine;
  try {
    mine = await api.myProfessions();
  } catch (err) {
    if (ctx.alive()) box.innerHTML = '';
    return;
  }
  if (!ctx.alive()) return;
  if (!Array.isArray(mine) || !mine.length) {
    box.innerHTML = `<div class="cta">
      <span class="cta-icon" aria-hidden="true">✓</span>
      <div class="cta-text"><strong>Which professions do you have?</strong> Set them once and the tool shows the most profitable crafts <em>you</em> can make, flags cheap reagents for them and builds a shopping list.</div>
      <a class="btn btn-gold" href="#/my">Set my professions</a>
    </div>`;
    return;
  }
  let crafts;
  try {
    crafts = await api.myCrafts('current', 50);
  } catch (err) {
    if (ctx.alive()) box.innerHTML = `<div class="card"><div class="card-head"><h2>Best crafts for you</h2></div>${errorState(err)}</div>`;
    return;
  }
  if (!ctx.alive()) return;
  const best = (crafts || []).filter((r) => isNum(r.profit) && r.profit > 0 && r.cost_complete).slice(0, 6);
  const names = mine.map((m) => `${esc(m.name)} ${num(m.skill)}`).join(' · ');
  box.innerHTML = `<section class="card">
    <div class="card-head"><h2>Best crafts for you</h2><span class="card-note">${names} · <a href="#/my">edit</a></span></div>
    ${best.length
      ? `<ul class="list list-cols">${best
          .map(
            (r) => `<li class="list-row">
              <div class="list-main">
                ${r.created_item_id ? itemLink(r.created_item_id, r.name, r.created_quality) : `<span class="spell-name">${esc(r.name)}</span>`}
                <div class="list-meta">${esc(r.profession_name || '')} · cost ${money(r.cost)}</div>
              </div>
              <div class="list-side">
                <div>${money(r.profit, { signed: true })}</div>
                <div class="list-sub ${r.margin > 0 ? 'up' : 'down'}">${pct(r.margin, 0, true)} margin</div>
              </div>
              <div class="list-act">${addShopCtl(r.spell_id, r.name)}</div>
            </li>`
          )
          .join('')}</ul>`
      : emptyState('Nothing profitable to craft right now', 'None of your craftable recipes make a profit at current prices.', '⚒')}
  </section>`;
}

function renderStatus(box, s) {
  if (!s) return;
  const fresh = isNum(s.last_ingest_ts) && Date.now() / 1000 - s.last_ingest_ts < 3600;
  const warn = [];
  if (s.item_names_loaded === false) warn.push('Item names are not loaded yet');
  if (s.recipes_loaded === false) warn.push('Recipes are not loaded yet');
  box.innerHTML = `
    <div class="status-main">
      <div class="status-item">
        <span class="status-label">Realm</span>
        <span class="status-value realm">${esc(s.realm || '–')}</span>
      </div>
      <div class="status-item">
        <span class="status-label">Last scanned</span>
        <span class="status-value">${dateLong(s.latest_date)}</span>
      </div>
      <div class="status-item">
        <span class="status-label">Last imported</span>
        <span class="status-value"><span class="dot ${fresh ? 'dot-ok' : 'dot-idle'}"></span>${relTime(s.last_ingest_ts)}</span>
        <span class="status-sub">${tsFmt(s.last_ingest_ts)}</span>
      </div>
      <div class="status-item">
        <span class="status-label">Priced items</span>
        <span class="status-value">${num(s.items_priced)}</span>
      </div>
    </div>
    <div class="hint">
      <span class="hint-icon" aria-hidden="true">↻</span>
      <div><strong>New scan?</strong> At the Auction House, scan with Auctionator (prices) and click <em>Full Scan</em> on the AH Tool strip or type <code>/ahtl scan</code> (all listings and sales; once per 15 min). Then type <code>/reload</code> or log out – WoW then writes the data to disk and the tool updates itself within a few seconds.
      ${warn.length ? `<div class="hint-warn">⚠ ${warn.map(esc).join(' · ')}</div>` : ''}</div>
    </div>`;
}

function renderTiles(box, market, deals) {
  if (!market || market.__err) {
    box.innerHTML = '';
    return;
  }
  const t = market.totals || {};
  const dealCount = Array.isArray(deals) ? deals.length : null;
  const vendorCount = Array.isArray(deals) ? deals.filter((d) => (d.reasons || []).includes('under_vendor')).length : 0;
  box.innerHTML = `<div class="tiles">
    ${tile('Market value', money(t.market_value), 'Sum of lowest price × quantity listed', 'tile-accent')}
    ${tile('Unique items', num(t.items), 'priced in the latest scan')}
    ${tile('Units listed', num(t.listed_quantity), 'max quantity seen per item')}
    ${tile('Deals', num(dealCount), vendorCount ? `${vendorCount} under vendor price` : 'at least 25% below market price')}
  </div>`;
}

function renderDeals(box, deals) {
  if (!deals || deals.__err) {
    box.innerHTML = errorState(deals && deals.__err);
    return;
  }
  if (!deals.length) {
    box.innerHTML = emptyState('No deals right now', 'No items are 25% or more below market price.');
    return;
  }
  const top = deals.slice().sort((a, b) => (b.score || 0) - (a.score || 0)).slice(0, 8);
  box.innerHTML = `<ul class="list">${top
    .map(
      (d) => `<li class="list-row">
        <div class="list-main">
          ${itemLink(d.item_id, d.name, d.quality)}
          <div class="list-badges">${(d.reasons || []).map((r) => reasonBadge(r)).join('')}</div>
        </div>
        <div class="list-side">
          <div>${money(d.price)}</div>
          ${(d.reasons || []).includes('under_vendor') && isNum(d.vendor_profit)
            ? `<div class="list-sub">vendor profit ${money(d.vendor_profit)}</div>`
            : `<div class="list-sub">${discountBar(d.discount)}</div>`}
        </div>
      </li>`
    )
    .join('')}</ul>`;
}

async function renderCrafts(box, ctx) {
  let profs;
  try {
    profs = await api.professions();
  } catch (err) {
    if (ctx.alive()) box.innerHTML = errorState(err);
    return;
  }
  if (!ctx.alive()) return;
  const top = (profs || [])
    .map((p) => ({ ...p, _best: 'best_profit_reliable' in p ? p.best_profit_reliable : p.best_profit }))
    .filter((p) => p.recipe_count > 0 && isNum(p._best) && p._best > 0)
    .sort((a, b) => b._best - a._best)
    .slice(0, 4);
  if (!top.length) {
    box.innerHTML = emptyState('No profitable crafts found', 'No recipes are profitable at current prices.');
    return;
  }
  const lists = await Promise.all(top.map((p) => api.profession(p.id, 'current').then((r) => ({ p, r })).catch(() => ({ p, r: [] }))));
  if (!ctx.alive()) return;
  const all = [];
  for (const { p, r } of lists) {
    for (const rec of r || []) {
      if (isNum(rec.profit) && rec.profit > 0 && rec.cost_complete && rec.confidence !== 'low') all.push({ ...rec, _prof: p });
    }
  }
  all.sort((a, b) => b.profit - a.profit);
  const best = all.slice(0, 8);
  if (!best.length) {
    box.innerHTML = emptyState('No profitable crafts found');
    return;
  }
  box.innerHTML = `<ul class="list">${best
    .map(
      (r) => `<li class="list-row">
        <div class="list-main">
          ${r.created_item_id ? itemLink(r.created_item_id, r.name, r.created_quality) : `<span>${esc(r.name)}</span>`}
          <div class="list-meta"><a href="#/professions/${encodeURIComponent(r._prof.id)}" class="muted-link">${esc(r._prof.name)}</a> · cost ${money(r.cost)}</div>
        </div>
        <div class="list-side">
          <div>${money(r.profit, { signed: true })}</div>
          <div class="list-sub ${r.margin > 0 ? 'up' : 'down'}">${pct(r.margin, 0, true)} margin</div>
        </div>
      </li>`
    )
    .join('')}</ul>`;
}

function renderScan(box, market) {
  const days = market && !market.__err ? market.scan_days || [] : [];
  if (!days.length) {
    box.innerHTML = emptyState('No scans recorded');
    return;
  }
  const recent = days.slice(-60);
  mountChart(box.querySelector('canvas'), (c) => ({
    type: 'bar',
    data: {
      labels: recent.map((d) => d.date),
      datasets: [
        {
          label: 'Items',
          data: recent.map((d) => d.items),
          backgroundColor: alpha(c.accent, 0.75),
          hoverBackgroundColor: c.accent,
          borderRadius: 3,
          borderSkipped: 'start',
          maxBarThickness: 14,
        },
      ],
    },
    options: {
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { title: (it) => dateLong(it[0].label), label: (it) => `${num(it.parsed.y)} items` } },
      },
      scales: {
        x: plainAxis(c, { ticks: { color: c.muted, maxRotation: 0, autoSkipPadding: 18, callback(v) { return this.getLabelForValue(v).slice(5); } } }),
        y: { grid: { color: c.grid }, border: { display: false }, ticks: { color: c.muted, maxTicksLimit: 4 }, beginAtZero: true },
      },
    },
  }));
}
