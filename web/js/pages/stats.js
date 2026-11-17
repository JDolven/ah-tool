import { api } from '../api.js';
import {
  esc, money, num, pctRaw, itemLink, QUALITY_NAMES, QUALITY_COLORS, dateLong, moneyText, goldTick,
  noData, errorState, skeletonTiles, skeletonRows, emptyState, tile, isNum, diffClass,
} from '../util.js';
import { mountChart, chartBox, plainAxis, alpha } from '../charts.js';
import { DataTable } from '../table.js';

export async function render(ctx) {
  const { el } = ctx;
  el.innerHTML = `
    <header class="page-head"><div><h1>Statistics</h1><p class="lead">Market overview based on the latest scan and history.</p></div></header>
    <div id="st-body">${skeletonTiles(3)}<section class="card">${skeletonRows(8, 4)}</section></div>`;

  let m;
  try {
    m = await api.market();
  } catch (err) {
    if (ctx.alive()) el.querySelector('#st-body').innerHTML = errorState(err);
    return;
  }
  if (!ctx.alive()) return;
  const t = (m && m.totals) || {};
  if (!t.items) {
    el.querySelector('#st-body').innerHTML = noData();
    return;
  }

  el.querySelector('#st-body').innerHTML = `
    <div class="tiles tiles-3">
      ${tile('Market value', money(t.market_value), 'lowest price × quantity listed', 'tile-accent')}
      ${tile('Unique items', num(t.items))}
      ${tile('Units listed', num(t.listed_quantity))}
    </div>
    <div class="grid-2">
      <section class="card">
        <div class="card-head"><h2>Value by quality</h2></div>
        <div class="donut-wrap">
          ${chartBox('q-chart', 220, 'Market value by quality')}
          <ul class="donut-legend" id="q-legend"></ul>
        </div>
      </section>
      <section class="card">
        <div class="card-head"><h2>Value by item class</h2></div>
        <div id="class-box">${chartBox('class-chart', 300, 'Market value by item class')}</div>
      </section>
    </div>
    <div class="grid-2">
      <section class="card card-flush">
        <div class="card-head pad-x"><h2><span class="up">▲</span> Biggest gainers</h2><span class="card-note">7 days</span></div>
        <div id="mv-up"></div>
      </section>
      <section class="card card-flush">
        <div class="card-head pad-x"><h2><span class="down">▼</span> Biggest losers</h2><span class="card-note">7 days</span></div>
        <div id="mv-down"></div>
      </section>
    </div>
    <div class="grid-2">
      <section class="card card-flush">
        <div class="card-head pad-x"><h2>Most listed</h2></div>
        <div id="most-listed"></div>
      </section>
      <section class="card card-flush">
        <div class="card-head pad-x"><h2>Most valuable</h2></div>
        <div id="most-valuable"></div>
      </section>
    </div>
    <section class="card">
      <div class="card-head"><h2>Scan activity per day</h2><span class="card-note">Number of priced items</span></div>
      <div id="scan-box">${chartBox('scan-chart', 200, 'Scan activity per day')}</div>
    </section>`;

  renderQuality(el, m.by_quality || []);
  renderClass(el, m.by_class || []);
  itemTable(el.querySelector('#mv-up'), m.movers_up || [], 'trend7_pct', 'desc');
  itemTable(el.querySelector('#mv-down'), m.movers_down || [], 'trend7_pct', 'asc');
  itemTable(el.querySelector('#most-listed'), m.most_listed || [], 'available', 'desc');
  itemTable(el.querySelector('#most-valuable'), m.most_valuable || [], 'price', 'desc');
  renderScan(el, m.scan_days || []);
}

function rid(r) {
  return r.id ?? r.item_id;
}

function itemTable(box, rows, sortKey, dir) {
  if (!rows.length) {
    box.innerHTML = emptyState('No data', 'Needs more days of history.');
    return;
  }
  new DataTable(box, [
    { key: 'name', label: 'Item', defaultDir: 'asc', sort: (r) => String(r.name || '').toLowerCase(), render: (r) => itemLink(rid(r), r.name, r.quality) },
    { key: 'price', label: 'Price', align: 'right', render: (r) => money(r.price) },
    { key: 'baseline', label: 'Market', align: 'right', hideSm: true, render: (r) => money(r.baseline) },
    { key: 'available', label: 'Qty', align: 'right', render: (r) => num(r.available) },
    { key: 'trend7_pct', label: '7d', align: 'right', render: (r) => `<span class="${diffClass(r.trend7_pct)}">${pctRaw(r.trend7_pct, 0)}</span>` },
  ], { sortKey, sortDir: dir, rowKey: rid, limit: 10 }).setRows(rows);
}

function renderQuality(el, rows) {
  const data = rows.filter((r) => isNum(r.value) && r.value > 0).sort((a, b) => a.quality - b.quality);
  const legend = el.querySelector('#q-legend');
  if (!data.length) {
    el.querySelector('.donut-wrap').innerHTML = emptyState('No data');
    return;
  }
  const total = data.reduce((s, r) => s + r.value, 0);
  legend.innerHTML = data
    .map((r) => `<li><i style="background:var(--q${Number(r.quality) || 0})"></i><span class="dl-name">${esc(QUALITY_NAMES[r.quality] || `Quality ${r.quality}`)}</span><span class="dl-val">${money(r.value)}</span><span class="dl-sub">${num(r.count)} items · ${((r.value / total) * 100).toFixed(0)}%</span></li>`)
    .join('');
  mountChart(el.querySelector('#q-chart'), (c) => ({
    type: 'doughnut',
    data: {
      labels: data.map((r) => QUALITY_NAMES[r.quality] || `Quality ${r.quality}`),
      datasets: [{
        data: data.map((r) => r.value),
        backgroundColor: data.map((r) => qColorThemed(r.quality, c)),
        borderColor: c.surface,
        borderWidth: 2,
        hoverOffset: 6,
      }],
    },
    options: {
      maintainAspectRatio: false,
      cutout: '64%',
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: (ctx) => `${ctx.label}: ${moneyText(ctx.parsed)}` } },
      },
    },
  }));
}

function qColor(q) {
  return QUALITY_COLORS[q] || '#888888';
}

function qColorThemed(q, c) {
  // "Common" is white – on light theme use text color so it stays visible.
  if (q === 1) return c.text2;
  const v = getComputedStyle(document.documentElement).getPropertyValue(`--q${q}`).trim();
  return v || qColor(q);
}

function renderClass(el, rows) {
  const data = rows.filter((r) => isNum(r.value) && r.value > 0).sort((a, b) => b.value - a.value);
  if (!data.length) {
    el.querySelector('#class-box').innerHTML = emptyState('No data');
    return;
  }
  // Fold tail into "Other" so the chart stays readable.
  let shown = data.slice(0, 9);
  if (data.length > 9) {
    const rest = data.slice(9);
    shown.push({ class_name: 'Other', value: rest.reduce((s, r) => s + r.value, 0), count: rest.reduce((s, r) => s + (r.count || 0), 0) });
  }
  const box = el.querySelector('#class-box .chart-box');
  if (box) box.style.height = `${Math.max(160, shown.length * 30 + 30)}px`;
  mountChart(el.querySelector('#class-chart'), (c) => ({
    type: 'bar',
    data: {
      labels: shown.map((r) => r.class_name || `Class ${r.class_id}`),
      datasets: [{
        label: 'Market value',
        data: shown.map((r) => r.value),
        backgroundColor: alpha(c.accent, 0.8),
        hoverBackgroundColor: c.accent,
        borderRadius: 4,
        borderSkipped: 'start',
        maxBarThickness: 18,
      }],
    },
    options: {
      indexAxis: 'y',
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (ctx) => `${moneyText(ctx.parsed.x)}`,
            afterLabel: (ctx) => `${num(shown[ctx.dataIndex].count)} items`,
          },
        },
      },
      scales: {
        x: { grid: { color: c.grid }, border: { display: false }, ticks: { color: c.muted, callback: (v) => goldTick(v), maxTicksLimit: 5 } },
        y: { grid: { display: false }, border: { color: c.grid }, ticks: { color: c.text2 } },
      },
    },
  }));
}

function renderScan(el, days) {
  if (!days.length) {
    el.querySelector('#scan-box').innerHTML = emptyState('No scans recorded');
    return;
  }
  mountChart(el.querySelector('#scan-chart'), (c) => ({
    type: 'bar',
    data: {
      labels: days.map((d) => d.date),
      datasets: [{
        label: 'Items',
        data: days.map((d) => d.items),
        backgroundColor: alpha(c.series[0], 0.7),
        hoverBackgroundColor: c.series[0],
        borderRadius: 3,
        borderSkipped: 'start',
        maxBarThickness: 16,
      }],
    },
    options: {
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { title: (it) => dateLong(it[0].label), label: (it) => `${num(it.parsed.y)} items` } },
      },
      scales: {
        x: plainAxis(c, { ticks: { color: c.muted, maxRotation: 0, autoSkipPadding: 18, callback(v) { return this.getLabelForValue(v).slice(5).split('-').reverse().join('.'); } } }),
        y: { beginAtZero: true, grid: { color: c.grid }, border: { display: false }, ticks: { color: c.muted, maxTicksLimit: 5 } },
      },
    },
  }));
}
