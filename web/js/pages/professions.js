import { api } from '../api.js';
import {
  esc, money, num, pct, itemLink, skillRange, sourceLabel, reasonBadge, debounce,
  noData, errorState, skeletonRows, emptyState, isNum, qClass,
  confBadge, flagChip, sparkline, trendPctHtml, moneyText, dateLong, dateShort,
} from '../util.js';
import { DataTable } from '../table.js';
import { addShopCtl } from '../ui.js';
import { mountChart, moneyAxis, plainAxis, alpha, pruneDetached } from '../charts.js';

export const PROF_ICONS = {
  1: '✚', 2: '⚒', 3: '◈', 4: '⚗', 5: '❦', 6: '♨', 7: '⛏', 8: '✂', 9: '⚙', 10: '✦', 11: '≈', 12: '⌇', 13: '☠',
};

const detailState = { mode: 'current', onlyProfit: false, text: '', mineOnly: false, hideLow: true };

export async function renderList(ctx) {
  const { el } = ctx;
  el.innerHTML = `
    <header class="page-head">
      <div>
        <h1>Professions</h1>
        <p class="lead">What does it cost to craft, and what do you get back on the AH? Cost uses the cheapest source (AH or vendor); sales deduct the 5% AH cut.</p>
      </div>
      <a class="btn" href="#/my"><span aria-hidden="true">✓</span> Set my professions</a>
    </header>
    <div class="prof-grid" id="prof-grid">${Array.from({ length: 8 }, () => '<div class="prof-card skel-card"><span class="skel" style="width:60%"></span><span class="skel" style="width:40%"></span><span class="skel skel-lg" style="width:70%"></span></div>').join('')}</div>`;

  let profs;
  try {
    profs = await api.professions();
  } catch (err) {
    if (ctx.alive()) el.querySelector('#prof-grid').outerHTML = errorState(err);
    return;
  }
  if (!ctx.alive()) return;
  if (!profs || !profs.length) {
    el.querySelector('#prof-grid').outerHTML = emptyState('No recipes loaded', 'Recipe data is built at startup from AtlasLootClassic. Check that the addon folder in config.json is correct.');
    return;
  }
  const sorted = profs.slice().sort((a, b) => {
    if (!!a.mine !== !!b.mine) return a.mine ? -1 : 1;
    const ra = a.recipe_count > 0 ? 1 : 0;
    const rb = b.recipe_count > 0 ? 1 : 0;
    if (ra !== rb) return rb - ra;
    return (bestOf(b) ?? -Infinity) - (bestOf(a) ?? -Infinity);
  });
  el.querySelector('#prof-grid').innerHTML = sorted
    .map((p) => {
      const empty = !p.recipe_count;
      const pricedPct = p.recipe_count ? p.priced_count / p.recipe_count : 0;
      return `<a class="prof-card ${empty ? 'is-empty' : ''} ${p.mine ? 'is-mine' : ''}" href="#/professions/${encodeURIComponent(p.id)}">
        <div class="prof-top">
          <span class="prof-icon" aria-hidden="true">${PROF_ICONS[p.id] || '◇'}</span>
          <div class="prof-top-text">
            <div class="prof-name">${esc(p.name)}</div>
            ${p.mine ? `<div class="prof-sub">Your skill ${isNum(p.my_skill) ? num(p.my_skill) : '–'}</div>` : ''}
          </div>
          ${p.mine ? '<span class="badge b-mine" title="One of your professions">✓ Mine</span>' : ''}
        </div>
        <div class="prof-stats">
          <div><span class="ps-val">${num(p.recipe_count)}</span><span class="ps-lbl">recipes</span></div>
          <div><span class="ps-val">${num(hasReliable(p) && isNum(p.profitable_reliable_count) ? p.profitable_reliable_count : p.profitable_count)}</span><span class="ps-lbl" ${hasReliable(p) ? 'title="Profitable recipes whose prices are not low-confidence"' : ''}>profitable</span></div>
        </div>
        <div class="prof-best">
          <span class="ps-lbl" title="Best profit among recipes whose prices are not low-confidence">Best profit${hasReliable(p) ? ' (reliable)' : ''}</span>
          <span>${isNum(bestOf(p)) ? money(bestOf(p), { signed: true }) : '<span class="muted">–</span>'}</span>
        </div>
        ${hasReliable(p) && isNum(p.best_profit) && p.best_profit !== p.best_profit_reliable
          ? `<div class="prof-best-raw" title="Includes recipes with thin or little price data">incl. low-confidence ${money(p.best_profit, { signed: true })}</div>`
          : ''}
        <div class="meter" title="${num(p.priced_count)} of ${num(p.recipe_count)} have complete pricing"><div class="meter-fill" style="width:${(pricedPct * 100).toFixed(0)}%"></div></div>
      </a>`;
    })
    .join('');
}

/** v4: prefer the reliable (non-low-confidence) best profit when the backend provides it. */
function hasReliable(p) {
  return !!p && 'best_profit_reliable' in p;
}
function bestOf(p) {
  return hasReliable(p) ? p.best_profit_reliable : p.best_profit;
}

export async function renderDetail(ctx) {
  const { el, params, query } = ctx;
  const id = params[0];
  if (query.mode === 'baseline' || query.mode === 'current') detailState.mode = query.mode;

  el.innerHTML = `
    <nav class="crumbs"><a href="#/professions">Professions</a> <span>/</span> <span id="pd-crumb">…</span></nav>
    <header class="page-head">
      <div>
        <h1 id="pd-title"><span class="skel" style="width:200px;display:inline-block"></span></h1>
        <p class="lead" id="pd-lead"></p>
      </div>
    </header>
    <section class="card filters">
      <div class="filter-row">
        <div class="seg" role="radiogroup" aria-label="Price basis">
          <button class="seg-btn ${detailState.mode === 'current' ? 'on' : ''}" data-mode="current" role="radio" aria-checked="${detailState.mode === 'current'}">Current price</button>
          <button class="seg-btn ${detailState.mode === 'baseline' ? 'on' : ''}" data-mode="baseline" role="radio" aria-checked="${detailState.mode === 'baseline'}">Market price</button>
        </div>
        <label class="toggle"><input type="checkbox" id="only-profit" ${detailState.onlyProfit ? 'checked' : ''}><span class="toggle-ui"></span> Only profitable</label>
        <label class="toggle" title="Only recipes you can craft (set under My Professions)"><input type="checkbox" id="mine-only" ${detailState.mineOnly ? 'checked' : ''}><span class="toggle-ui"></span> Mine only</label>
        <label class="toggle" title="Hide recipes whose prices rest on very little data or a thin market"><input type="checkbox" id="hide-low" ${detailState.hideLow ? 'checked' : ''}><span class="toggle-ui"></span> Hide low-confidence</label>
        <label class="field field-grow">
          <span class="field-label sr-only">Search recipes</span>
          <input type="search" id="pd-txt" placeholder="Search recipes…" value="${esc(detailState.text)}">
        </label>
      </div>
      <p class="filter-note" id="mode-note"></p>
    </section>
    <div class="result-bar" id="pd-summary"></div>
    <section class="card card-flush" id="pd-table">${skeletonRows(12, 6)}</section>`;

  const table = new DataTable(el.querySelector('#pd-table'), recipeColumns({ shop: true }), {
    sortKey: 'profit',
    sortDir: 'desc',
    rowKey: (r) => r.spell_id,
    expand: recipeExpand,
    onRender: mountRecipeTrends,
    rowClass: (r) => (!r.cost_complete ? 'row-incomplete' : isNum(r.profit) && r.profit > 0 ? 'row-profit' : ''),
    empty: emptyState('No recipes match', 'Try turning off “Only profitable” or changing the search.'),
  });

  const setNote = () => {
    el.querySelector('#mode-note').textContent =
      detailState.mode === 'current'
        ? 'Current price: lowest price in the latest scan. Shows what you earn if you buy and sell now.'
        : 'Market price: median of the last 14 days. A more stable picture – less affected by individual listings.';
  };
  setNote();

  api.professions().then((profs) => {
    if (!ctx.alive()) return;
    const p = (profs || []).find((x) => String(x.id) === String(id));
    const name = p ? p.name : `Profession #${id}`;
    el.querySelector('#pd-title').innerHTML = `<span class="prof-icon lg" aria-hidden="true">${PROF_ICONS[id] || '◇'}</span> ${esc(name)}`;
    el.querySelector('#pd-crumb').textContent = name;
    el.querySelector('#pd-lead').innerHTML = p
      ? `${num(p.recipe_count)} recipes · ${num(p.priced_count)} with complete pricing · ${num(p.profitable_count)} profitable${p.mine ? ` · <span class="badge b-mine">✓ Mine – skill ${isNum(p.my_skill) ? num(p.my_skill) : '–'}</span>` : ''}`
      : '';
    document.title = `${name} – AH Tool`;
  }).catch(() => {
    if (ctx.alive()) el.querySelector('#pd-title').textContent = `Profession #${id}`;
  });

  let data = [];
  const apply = () => {
    const t = detailState.text.trim().toLowerCase();
    let lowHidden = 0;
    const rows = data.filter((r) => {
      if (detailState.hideLow && r.confidence === 'low') {
        lowHidden++;
        return false;
      }
      if (detailState.onlyProfit && !(isNum(r.profit) && r.profit > 0)) return false;
      if (t) {
        const hay = [r.name, r.created_name, ...(r.reagents || []).map((g) => g.name)].join(' ').toLowerCase();
        if (!hay.includes(t)) return false;
      }
      return true;
    });
    table.setRows(rows);
    const prof = rows.filter((r) => isNum(r.profit) && r.profit > 0).length;
    const inc = rows.filter((r) => !r.cost_complete).length;
    el.querySelector('#pd-summary').innerHTML = `<span><strong>${num(rows.length)}</strong> recipes</span>
      <span class="up">${num(prof)} profitable</span>
      ${inc ? `<span class="warn-text">⚠ ${num(inc)} missing a reagent price</span>` : ''}
      ${lowHidden ? `<span class="muted">${num(lowHidden)} low-confidence hidden</span>` : ''}`;
  };

  const load = async () => {
    el.querySelector('#pd-table').innerHTML = skeletonRows(12, 6);
    try {
      const res = await api.profession(id, detailState.mode, detailState.mineOnly);
      if (!ctx.alive()) return;
      data = Array.isArray(res) ? res : [];
      if (!data.length && detailState.mineOnly) {
        el.querySelector('#pd-table').innerHTML = emptyState('No recipes you can craft here', 'Add this profession and your skill under <a href="#/my">My Professions</a>, or turn off “Mine only”.', '✓');
        el.querySelector('#pd-summary').innerHTML = '';
        return;
      }
      if (!data.length) {
        el.querySelector('#pd-table').innerHTML = emptyState('No recipes for this profession', 'Gathering professions (Herbalism, Mining, Skinning, Fishing) have no recipes to calculate.');
        el.querySelector('#pd-summary').innerHTML = '';
        return;
      }
      apply();
    } catch (err) {
      if (ctx.alive()) el.querySelector('#pd-table').innerHTML = errorState(err);
    }
  };

  el.querySelectorAll('.seg-btn').forEach((b) =>
    b.addEventListener('click', () => {
      if (detailState.mode === b.dataset.mode) return;
      detailState.mode = b.dataset.mode;
      el.querySelectorAll('.seg-btn').forEach((x) => {
        x.classList.toggle('on', x === b);
        x.setAttribute('aria-checked', String(x === b));
      });
      setNote();
      load();
    })
  );
  el.querySelector('#only-profit').addEventListener('change', (e) => {
    detailState.onlyProfit = e.target.checked;
    apply();
  });
  el.querySelector('#hide-low').addEventListener('change', (e) => {
    detailState.hideLow = e.target.checked;
    apply();
  });
  el.querySelector('#mine-only').addEventListener('change', (e) => {
    detailState.mineOnly = e.target.checked;
    load();
  });
  el.querySelector('#pd-txt').addEventListener('input', debounce((e) => {
    detailState.text = e.target.value;
    apply();
  }, 150));

  await load();
}

export function recipeName(r) {
  const n = r.num_created > 1 ? ` <span class="muted">×${num(r.num_created, 1)}</span>` : '';
  if (r.created_item_id) return itemLink(r.created_item_id, r.name || r.created_name, r.created_quality) + n;
  return `<span class="spell-name">${esc(r.name || `Spell #${r.spell_id}`)}</span>`;
}

export function recipeColumns(opts = {}) {
  const cols = [
    {
      key: 'name',
      label: 'Recipe',
      defaultDir: 'asc',
      sort: (r) => String(r.name || '').toLowerCase(),
      render: (r) => `${recipeName(r)}${confBadge(r.confidence)}${r.sell_price_flag ? ' ' + flagChip(r.sell_price_flag) : ''}${!r.cost_complete ? ' <span class="tag-mini warn" title="Missing a price for at least one reagent">incomplete</span>' : ''}${
        r.can_craft && !opts.noCraftTag ? ' <span class="tag-mini ok" title="You can craft this">✓ can craft</span>' : ''}${
        opts.showProf && r.profession_name ? `<div class="cell-sub">${esc(r.profession_name)}</div>` : ''}`,
    },
  ];
  if (!opts.noSkill) {
    cols.push({ key: 'skill', label: 'Skill', defaultDir: 'asc', hideSm: true, hideMd: !!opts.shop, sort: (r) => (r.skill ? r.skill.min : null), render: (r) => skillRange(r.skill) });
  }
  cols.push(
    { key: 'cost', label: 'Cost', align: 'right', hideSm: !!opts.shop, render: (r) => `${money(r.cost)}${!r.cost_complete ? '<span class="warn-mark" title="Incomplete cost">*</span>' : ''}` },
    {
      key: 'sell',
      label: 'Sell value',
      align: 'right',
      hideSm: !!opts.shop,
      sort: (r) => (r.sell_source === 'vendor' ? r.vendor_value : r.sell),
      render: (r) => {
        if (!r.created_item_id) return '<span class="muted" title="Does not create an item (e.g. an enchant)">–</span>';
        if (r.sell_source === 'vendor') {
          const why = isNum(r.sell) ? 'Selling to a vendor pays more than the AH after the cut' : 'No AH price – profit uses the vendor price';
          return `<span title="${why}">${money(r.vendor_value)} <span class="src src-vendor">Vendor</span></span>`;
        }
        return money(r.sell);
      },
    },
    {
      key: 'profit',
      label: 'Profit',
      align: 'right',
      render: (r) => {
        if (!isNum(r.profit)) return '<span class="muted">–</span>';
        return r.cost_complete
          ? money(r.profit, { signed: true })
          : `<span class="incomplete" title="Uncertain: missing a price for at least one reagent">≤ ${money(r.profit, { signed: true })}</span>`;
      },
    },
    {
      key: 'margin',
      label: 'Margin',
      align: 'right',
      sort: (r) => (r.cost_complete ? r.margin : null),
      render: (r) => {
        if (!isNum(r.margin)) return '<span class="muted">–</span>';
        if (!r.cost_complete) return '<span class="muted" title="Cannot be calculated – incomplete cost">?</span>';
        return `<span class="pill ${r.margin > 0 ? 'pill-up' : 'pill-down'}">${pct(r.margin, 0, true)}</span>`;
      },
    },
    {
      key: 'trend',
      label: 'Trend 14d',
      align: 'right',
      hideSm: true,
      title: 'Daily profit over the last 14 days, and the change of the last 3 days vs. the week before',
      sort: (r) => r.profit_trend_pct,
      render: (r) => `<span class="trend-cell">${sparkline(r.spark)}${trendPctHtml(r.profit_trend_pct)}</span>`,
    },
    { key: 'sell_available', label: 'On AH', align: 'right', hideSm: true, hideMd: !!opts.shop, title: 'Quantity of the finished item on the AH', render: (r) => num(r.sell_available) }
  );
  if (opts.shop) {
    cols.push({ key: 'shop', label: 'Shopping list', sortable: false, align: 'right', render: (r) => addShopCtl(r.spell_id, r.name || r.created_name) });
  }
  return cols;
}

export function recipeExpand(r) {
  const regs = r.reagents || [];
  const trend = `<div class="rtrend" data-trend="${esc(r.spell_id)}">${trendBody(r.spell_id)}</div>`;
  if (!regs.length) return `<div class="rexp"><div class="muted pad">No reagents recorded.</div>${trend}</div>`;
  return `<div class="rexp"><div class="reagents">
    <table class="mini">
      <thead><tr><th>Reagent</th><th class="r">Qty</th><th class="r">Unit price</th><th>Source</th><th class="r">Total</th></tr></thead>
      <tbody>
      ${regs
        .map(
          (g) => `<tr class="${g.source === 'none' || !isNum(g.unit_price) ? 'row-incomplete' : ''}">
            <td>${itemLink(g.item_id, g.name, g.quality)}${confBadge(g.confidence)} ${flagChip(g.price_flag)} ${g.is_deal ? reasonBadge('cheap_reagent', 'Below market price now – good buy') : ''}</td>
            <td class="r">${num(g.count)}</td>
            <td class="r">${money(g.unit_price)}</td>
            <td>${sourceLabel(g.source)}</td>
            <td class="r">${money(g.total)}</td>
          </tr>`
        )
        .join('')}
      </tbody>
      <tfoot>
        <tr><td colspan="4">Total cost${!r.cost_complete ? ' <span class="warn-text">(incomplete)</span>' : ''}</td><td class="r">${money(r.cost)}</td></tr>
        ${r.created_item_id ? `<tr class="${r.sell_source === 'ah' ? '' : 'muted'}"><td colspan="4">Sale after 5% AH cut ${confBadge(r.sell_confidence)} ${flagChip(r.sell_price_flag)}${r.sell_source === 'ah' ? ' <span class="tag-mini ok">used</span>' : ''}</td><td class="r">${isNum(r.sell) ? money(Math.round(r.sell * 0.95)) : '–'}</td></tr>` : ''}
        ${r.created_item_id && r.vendor_value ? `<tr class="${r.sell_source === 'vendor' ? '' : 'muted'}"><td colspan="4">Sell to vendor${r.sell_source === 'vendor' ? ' <span class="tag-mini ok">used</span>' : ''}</td><td class="r">${money(r.vendor_value)}</td></tr>` : ''}
      </tfoot>
    </table>
  </div>${trend}</div>`;
}

// ---------------------------------------------------------------- v4: profit/margin trend chart in the expanded row
const trendCache = new Map(); // spell_id -> trend payload (or {__err})

function trendBody(id) {
  const t = trendCache.get(String(id));
  const head = (extra = '') => `<div class="rtrend-head"><span class="rtrend-title">Profit trend · 30 days</span>${extra}</div>`;
  if (!t) return `${head()}<div class="rtrend-empty"><span class="spinner"></span> Loading trend…</div>`;
  if (t.__err) return `${head()}<div class="rtrend-empty muted">Trend unavailable: ${esc(t.__err)}</div>`;
  const pts = (t.points || []).filter((p) => isNum(p.profit));
  if (pts.length < 2) return `${head()}<div class="rtrend-empty muted">Not enough daily prices yet to draw a trend.</div>`;
  const stats = `<span class="rtrend-stats">
      <span>7d avg ${money(t.avg_profit_7, { signed: true })}</span>
      <span>30d avg ${money(t.avg_profit_30, { signed: true })}</span>
      ${trendPctHtml(t.profit_trend_pct)}
    </span>`;
  return `${head(stats)}<div class="legend legend-sm"><span class="lg"><i class="lg-line s1"></i>Profit</span><span class="lg"><i class="lg-line s3 dashed"></i>Margin %</span></div>
    <div class="chart-box" style="height:170px"><canvas role="img" aria-label="Daily profit and margin, last 30 days"></canvas></div>`;
}

function drawTrend(box, t) {
  const canvas = box.querySelector('canvas');
  if (!canvas) return;
  // Trim leading days without data so short histories fill the chart.
  let pts = t.points || [];
  const first = pts.findIndex((p) => isNum(p.profit));
  if (first > 0) pts = pts.slice(first);
  mountChart(canvas, (c) => ({
    type: 'line',
    data: {
      labels: pts.map((p) => p.date),
      datasets: [
        {
          label: 'Profit',
          data: pts.map((p) => (isNum(p.profit) ? p.profit : null)),
          borderColor: c.series[0],
          backgroundColor: alpha(c.series[0], 0.1),
          fill: 'origin',
          borderWidth: 2,
          tension: 0.25,
          pointRadius: pts.length > 20 ? 0 : 2.5,
          pointHoverRadius: 4,
          spanGaps: true,
          yAxisID: 'y',
          segment: { borderColor: (s) => (s.p1.parsed.y < 0 || s.p0.parsed.y < 0 ? c.down : c.series[0]) },
        },
        {
          label: 'Margin',
          data: pts.map((p) => (isNum(p.margin) ? Math.round(p.margin * 1000) / 10 : null)),
          borderColor: c.series[2],
          borderDash: [5, 4],
          borderWidth: 1.5,
          tension: 0.25,
          pointRadius: 0,
          pointHoverRadius: 4,
          fill: false,
          spanGaps: true,
          yAxisID: 'y2',
        },
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
            label: (ctx) => (ctx.dataset.yAxisID === 'y2' ? `Margin: ${ctx.parsed.y.toFixed(1)}%` : `Profit: ${moneyText(ctx.parsed.y)}`),
          },
        },
      },
      scales: {
        x: plainAxis(c, { ticks: { color: c.muted, maxRotation: 0, autoSkipPadding: 18, callback(v) { return dateShort(this.getLabelForValue(v)); } } }),
        y: moneyAxis(c, { grid: { color: (g) => (g.tick && g.tick.value === 0 ? c.muted : c.grid), drawTicks: false } }),
        y2: { position: 'right', grid: { display: false }, border: { display: false }, ticks: { color: c.muted, maxTicksLimit: 5, callback: (v) => `${v}%` } },
      },
    },
  }));
}

/** DataTable onRender hook: fill every expanded row's trend slot (fetch once, then draw). */
export function mountRecipeTrends(root) {
  pruneDetached();
  root.querySelectorAll('.rtrend[data-trend]').forEach((box) => {
    const id = box.dataset.trend;
    const cached = trendCache.get(id);
    if (cached && Date.now() - cached.__ts < 60000) {
      if (!cached.__err) drawTrend(box, cached);
      return;
    }
    api.recipeTrend(id, 30)
      .then((t) => trendCache.set(id, { ...(t || { points: [] }), __ts: Date.now() }))
      .catch((err) => trendCache.set(id, { __err: err.message || 'error', __ts: Date.now() }))
      .then(() => {
        // The table may have re-rendered meanwhile – fill whatever slot exists now.
        root.querySelectorAll(`.rtrend[data-trend="${CSS.escape(id)}"]`).forEach((b) => {
          b.innerHTML = trendBody(id);
          const t = trendCache.get(id);
          if (t && !t.__err) drawTrend(b, t);
        });
      });
  });
}

/** Forget cached trends (after a new ingest the prices change). */
export function clearTrendCache() {
  trendCache.clear();
}


export { qClass };
