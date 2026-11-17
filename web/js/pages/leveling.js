// Leveling Guide: cheapest route from a start skill to a target skill for one profession.
// Inputs live in the hash query (#/leveling?prof=8&start=1&target=300...) so plans are linkable.
import { api } from '../api.js';
import {
  esc, money, num, itemLink, skillRange, sourceLabel, errorState, skeletonRows, skeletonTiles,
  emptyState, tile, isNum, confBadge, flagChip, debounce,
} from '../util.js';
import { DataTable } from '../table.js';
import { toast, emit } from '../ui.js';
import { PROF_ICONS, recipeName } from './professions.js';
import { openGuide } from '../levelprint.js';

const SKILL_MAX = 375;
const GATHERING = new Set([5, 11, 12]); // the planner returns 400 for these
const SEG_COLORS = 8;
// Trainer ranks (Classic). Texts from the plan's `milestones` override these when present.
const TRAINER = [
  { skill: 50, short: 'Journeyman', text: 'Journeyman – train at 50 (requires level 10)' },
  { skill: 75, short: 'Apprentice cap', text: 'Apprentice cap – Journeyman needed to go past 75' },
  { skill: 125, short: 'Expert', text: 'Expert – train at 125 (requires level 20)' },
  { skill: 150, short: 'Journeyman cap', text: 'Journeyman cap – Expert needed to go past 150' },
  { skill: 200, short: 'Artisan', text: 'Artisan – train at 200 (requires level 35)' },
  { skill: 225, short: 'Expert cap', text: 'Expert cap – Artisan needed to go past 225' },
];
const DEFAULTS = { target: 300, mode: 'baseline', recover: 'sell', sources: 'trainer', known_only: false };

function clampInt(v, lo, hi, dflt) {
  const n = Math.round(Number(v));
  if (!isFinite(n) || v === '' || v === null || v === undefined) return dflt;
  return Math.max(lo, Math.min(hi, n));
}

export async function render(ctx) {
  const { el, query } = ctx;
  el.innerHTML = `
    <header class="page-head">
      <div>
        <h1>Leveling Guide</h1>
        <p class="lead">The cheapest route to your target skill. Each skill point picks the recipe with the lowest expected cost – orange and yellow recipes always give a point, after yellow the chance falls until the recipe turns grey. Crafted items are sold back (or vendored) to recover part of the cost.</p>
      </div>
      <div class="head-actions">
        <button type="button" class="btn" id="lv-print" disabled title="Open a printer-friendly guide you can print, save as PDF, download or copy as text"><span aria-hidden="true">⎙</span> Print / Share</button>
        <button type="button" class="btn btn-gold" id="lv-shop" disabled><span aria-hidden="true">＋</span> Add plan to Shopping List</button>
      </div>
    </header>
    <section class="card filters" id="lv-form">${skeletonRows(2, 4)}</section>
    <div id="lv-out">${skeletonTiles(5)}${skeletonRows(8, 6)}</div>`;

  let profs = [];
  let mine = new Map();
  try {
    const [p, m] = await Promise.all([api.professions(), api.myProfessions().catch(() => [])]);
    if (!ctx.alive()) return;
    profs = (Array.isArray(p) ? p : []).filter((x) => x.recipe_count > 0 && !GATHERING.has(x.id)).sort((a, b) => a.name.localeCompare(b.name));
    mine = new Map((Array.isArray(m) ? m : []).map((x) => [x.profession_id, x.skill]));
  } catch (err) {
    if (ctx.alive()) {
      el.querySelector('#lv-form').outerHTML = '';
      el.querySelector('#lv-out').innerHTML = errorState(err);
    }
    return;
  }
  if (!profs.length) {
    el.querySelector('#lv-form').remove();
    el.querySelector('#lv-out').innerHTML = emptyState('No recipes loaded', 'Recipe data is built at startup from AtlasLootClassic.', '⚒');
    return;
  }

  // ---- state from the URL
  const pickDefaultProf = () => {
    const mineCraft = profs.filter((p) => mine.has(p.id));
    const below = mineCraft.find((p) => (mine.get(p.id) || 0) < DEFAULTS.target);
    return (below || mineCraft[0] || profs.find((p) => p.id === 8) || profs[0]).id;
  };
  const defaultStart = (id) => {
    const s = mine.get(id);
    return isNum(s) && s >= 1 ? Math.min(s, SKILL_MAX - 1) : 1;
  };
  const qProf = Number(query.prof);
  const st = {
    prof: profs.some((p) => p.id === qProf) ? qProf : pickDefaultProf(),
    start: null,
    target: clampInt(query.target, 2, SKILL_MAX, DEFAULTS.target),
    mode: query.mode === 'current' ? 'current' : 'baseline',
    recover: ['sell', 'vendor', 'none'].includes(query.recover) ? query.recover : DEFAULTS.recover,
    sources: query.sources === 'all' ? 'all' : 'trainer',
    known_only: query.known === '1' || query.known_only === '1',
  };
  st.start = clampInt(query.start, 1, SKILL_MAX - 1, defaultStart(st.prof));

  const writeHash = () => {
    const q = new URLSearchParams({ prof: String(st.prof), start: String(st.start), target: String(st.target) });
    if (st.mode !== DEFAULTS.mode) q.set('mode', st.mode);
    if (st.recover !== DEFAULTS.recover) q.set('recover', st.recover);
    if (st.sources !== DEFAULTS.sources) q.set('sources', st.sources);
    if (st.known_only) q.set('known', '1');
    const h = `#/leveling?${q.toString()}`;
    if (location.hash !== h) history.replaceState(null, '', h);
  };
  const apiOpts = () => ({ start: st.start, target: st.target, mode: st.mode, recover: st.recover, sources: st.sources, known_only: st.known_only });

  // ---- form
  const seg = (group, opts) => `<div class="seg seg-sm" role="radiogroup" data-group="${group}">${opts
    .map(([v, l, t]) => `<button type="button" class="seg-btn ${st[group] === v ? 'on' : ''}" data-v="${v}" role="radio" aria-checked="${st[group] === v}" ${t ? `title="${esc(t)}"` : ''}>${l}</button>`)
    .join('')}</div>`;
  const form = el.querySelector('#lv-form');
  form.innerHTML = `
    <div class="filter-row lv-row">
      <label class="field lv-prof">
        <span class="field-label">Profession</span>
        <select id="lv-prof" class="select-in">${profs
          .map((p) => `<option value="${p.id}" ${p.id === st.prof ? 'selected' : ''}>${PROF_ICONS[p.id] || ''} ${esc(p.name)}${mine.has(p.id) ? ` (yours: ${num(mine.get(p.id))})` : ''}</option>`)
          .join('')}</select>
      </label>
      <label class="field field-num">
        <span class="field-label">Start skill</span>
        <input type="number" id="lv-start" min="1" max="${SKILL_MAX - 1}" step="1" value="${st.start}" inputmode="numeric">
      </label>
      <label class="field field-num">
        <span class="field-label">Target</span>
        <input type="number" id="lv-target" min="2" max="${SKILL_MAX}" step="1" value="${st.target}" inputmode="numeric">
      </label>
    </div>
    <div class="filter-row lv-row lv-opts">
      <div class="field field-auto"><span class="field-label">Prices</span>${seg('mode', [['baseline', 'Market price', 'Median of the last 14 days – stable'], ['current', 'Current price', 'Lowest price in the latest scan']])}</div>
      <div class="field field-auto"><span class="field-label">Recover cost by</span>${seg('recover', [['sell', 'Selling on AH', 'Sell crafted items on the AH (after the 5% cut). Vendor price when sale data is thin.'], ['vendor', 'Vendor', 'Sell crafted items to an NPC'], ['none', 'Nothing', 'Ignore the value of crafted items']])}</div>
      <div class="field field-auto"><span class="field-label">Recipes</span>${seg('sources', [['trainer', 'Trainer only'], ['all', 'Incl. recipe drops', 'Also use recipes taught by items; their AH price is added once']])}</div>
      <label class="toggle" title="Only recipes you know (set under My Professions) or that the trainer teaches at your skill"><input type="checkbox" id="lv-known" ${st.known_only ? 'checked' : ''}><span class="toggle-ui"></span> Known recipes only</label>
    </div>`;

  // ---- plan
  let plan = null;
  let token = 0;
  const out = el.querySelector('#lv-out');
  const shopBtn = el.querySelector('#lv-shop');
  const printBtn = el.querySelector('#lv-print');

  const load = async () => {
    writeHash();
    const t = ++token;
    shopBtn.disabled = true;
    printBtn.disabled = true;
    const pname = (profs.find((p) => p.id === st.prof) || {}).name || 'Profession';
    document.title = `Leveling ${pname} – AH Tool`;
    if (st.start >= st.target) {
      plan = null;
      out.innerHTML = emptyState(`Already at ${num(st.target)}`, `Your start skill (${num(st.start)}) is not below the target. Lower the start skill or raise the target.`, '✓');
      return;
    }
    out.classList.add('is-loading');
    if (!plan) out.innerHTML = `${skeletonTiles(5)}<section class="card">${skeletonRows(8, 6)}</section>`;
    try {
      const res = await api.leveling(st.prof, apiOpts());
      if (!ctx.alive() || t !== token) return;
      plan = res;
      drawPlan(out, res, st);
      shopBtn.disabled = !(res && res.steps && res.steps.length);
      printBtn.disabled = shopBtn.disabled;
    } catch (err) {
      if (!ctx.alive() || t !== token) return;
      plan = null;
      out.innerHTML = errorState(err);
    } finally {
      if (t === token) out.classList.remove('is-loading');
    }
  };

  form.addEventListener('click', (e) => {
    const b = e.target.closest('.seg-btn');
    if (!b) return;
    const g = b.closest('[data-group]').dataset.group;
    if (st[g] === b.dataset.v) return;
    st[g] = b.dataset.v;
    b.parentElement.querySelectorAll('.seg-btn').forEach((x) => {
      x.classList.toggle('on', x === b);
      x.setAttribute('aria-checked', String(x === b));
    });
    load();
  });
  form.querySelector('#lv-prof').addEventListener('change', (e) => {
    st.prof = Number(e.target.value);
    st.start = defaultStart(st.prof);
    form.querySelector('#lv-start').value = st.start;
    plan = null;
    load();
  });
  form.querySelector('#lv-known').addEventListener('change', (e) => {
    st.known_only = e.target.checked;
    load();
  });
  const readNums = () => {
    const sEl = form.querySelector('#lv-start');
    const tEl = form.querySelector('#lv-target');
    const s = clampInt(sEl.value, 1, SKILL_MAX - 1, st.start);
    const tg = clampInt(tEl.value, 2, SKILL_MAX, st.target);
    if (s === st.start && tg === st.target) return;
    st.start = s;
    st.target = tg;
    load();
  };
  const readNumsSoon = debounce(readNums, 700);
  form.addEventListener('input', (e) => {
    if (e.target.matches('#lv-start, #lv-target')) readNumsSoon();
  });
  form.addEventListener('change', (e) => {
    if (!e.target.matches('#lv-start, #lv-target')) return;
    e.target.value = e.target.id === 'lv-start' ? clampInt(e.target.value, 1, SKILL_MAX - 1, st.start) : clampInt(e.target.value, 2, SKILL_MAX, st.target);
    readNums();
  });

  printBtn.addEventListener('click', () => {
    if (!plan || !plan.steps || !plan.steps.length) return;
    const pname = (profs.find((p) => p.id === st.prof) || {}).name || 'Profession';
    const d = new Date();
    const pad = (x) => String(x).padStart(2, '0');
    const ok = openGuide(plan, {
      profName: pname,
      start: st.start,
      target: st.target,
      sources: plan.sources || st.sources,
      milestones: milestonesFor(plan),
      dateText: `${d.getDate()} ${d.toLocaleString('en-GB', { month: 'short' })} ${d.getFullYear()}`,
      priceText: st.mode === 'current' ? 'cheapest price in the latest AH scan' : 'AH market price (14-day median)',
      recoverText: { sell: 'crafted items sold on the AH', vendor: 'crafted items vendored', none: 'crafted items not counted' }[plan.recover || st.recover] || '',
      fileName: `${pname.toLowerCase().replace(/[^a-z0-9]+/g, '-')}-${st.start}-${st.target}-${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}.html`,
    });
    if (!ok) toast('<strong>Pop-up blocked</strong> – allow pop-ups for this page to open the guide.', 'err');
  });

  shopBtn.addEventListener('click', async () => {
    if (!plan || !plan.steps || !plan.steps.length) return;
    shopBtn.disabled = true;
    try {
      const res = await api.levelingToShopping(st.prof, apiOpts());
      const n = res && isNum(res.added) ? res.added : plan.steps.length;
      const crafts = plan.totals && isNum(plan.totals.crafts) ? plan.totals.crafts : null;
      toast(`<strong>Plan added</strong> – ${num(n)} recipe${n === 1 ? '' : 's'}${crafts !== null ? ` (${num(crafts)} crafts)` : ''} put on the shopping list. <a href="#/shopping">Open list →</a>`, 'ok');
      emit('shopping', { leveling: st.prof });
    } catch (err) {
      toast(`<strong>Could not add the plan</strong><br>${esc(err.message)}`, 'err');
    } finally {
      if (ctx.alive()) shopBtn.disabled = !(plan && plan.steps && plan.steps.length);
    }
  });

  await load();
}

// ---------------------------------------------------------------- output
function colorMap(steps) {
  const m = new Map();
  for (const s of steps) if (!m.has(s.spell_id)) m.set(s.spell_id, m.size % SEG_COLORS);
  return m;
}

function drawPlan(out, plan, st) {
  const steps = (plan.steps || []).slice().sort((a, b) => a.from - b.from);
  const t = plan.totals || {};
  const warnings = plan.warnings || [];
  const colors = colorMap(steps);
  const start = isNum(plan.start) ? plan.start : st.start;
  const target = isNum(plan.target) ? plan.target : st.target;
  const unreach = isNum(plan.unreachable_from) ? plan.unreachable_from : null;

  if (!steps.length) {
    out.innerHTML = `${emptyState(
      unreach !== null ? `No route from skill ${num(unreach)}` : 'No plan',
      warnings.length ? warnings.map(esc).join('<br>') : 'There is no usable recipe with skill colours and prices in this range. Try “Incl. recipe drops” or another price basis.',
      '⛔'
    )}`;
    return;
  }

  const net = t.net;
  const profit = isNum(net) && net < 0;
  const recoverSub = { sell: 'selling crafted items on the AH', vendor: 'vendoring crafted items', none: 'not counted' }[plan.recover || st.recover] || '';
  const tiles = `<div class="tiles tiles-5 lv-tiles">
    ${tile('Total cost', money(t.cost), isNum(t.recipes) && t.recipes > 0 ? `incl. ${money(t.recipes)} for recipes` : 'reagents for every craft')}
    ${tile('Recovered', money(t.recover), recoverSub)}
    ${profit
      ? tile('Net result', `<span class="up lv-profit">Profit! ${money(-net, { signed: true })}</span>`, 'leveling pays for itself', 'tile-hot')
      : tile('Net cost', money(net), `${num(start)} → ${num(unreach !== null ? unreach : target)}`, 'tile-accent')}
    ${tile('Crafts', num(t.crafts), `${num(steps.length)} step${steps.length === 1 ? '' : 's'}${isNum(t.crafts_expected) ? ` · ≈ ${num(t.crafts_expected, 1)} expected` : ''}${isNum(t.subcrafts) && t.subcrafts > 0 ? ` · +${num(t.subcrafts)} sub-crafts` : ''}`)}
    ${tile('Warnings', num(warnings.length), warnings.length ? '<a href="#lv-warn" data-jump="lv-warn">see below</a>' : 'none', warnings.length ? 'tile-warn' : '')}
  </div>`;

  const stop = unreach !== null
    ? `<div class="callout callout-stop"><span aria-hidden="true">⛔</span> <span><strong>The plan stops at skill ${num(unreach)}</strong> – there is no usable recipe with a price from there to ${num(target)}. ${esc(warnings.find((w) => /from skill/i.test(w)) || '')}</span></div>`
    : '';

  out.innerHTML = `${tiles}${stop}
    <section class="card">
      <div class="card-head"><h2>Skill path</h2><span class="card-note">${num(1)} → ${num(target)} · colours match the steps below</span></div>
      ${skillBar(steps, colors, plan, start, target, unreach)}
    </section>
    <section class="card card-flush">
      <div class="card-head pad-x"><h2>Steps</h2><span class="card-note">Click a step for its reagents</span></div>
      <div id="lv-steps"></div>
    </section>
    <div class="grid-2 lv-bottom">
      <section class="card card-flush">
        <div class="card-head pad-x"><h2>Shopping</h2><span class="card-note">Everything the plan needs</span></div>
        <div id="lv-shopping">${shoppingTable(plan.shopping || [])}</div>
      </section>
      <div class="lv-side">
        ${subcraftsCard(plan.subcrafts || [])}
        ${toolsCard(plan.tools || [])}
        ${recipesCard(plan.recipes_to_buy || [], plan.sources || st.sources)}
        ${warnings.length ? `<section class="card" id="lv-warn"><div class="card-head"><h2><span class="warn-text" aria-hidden="true">⚠</span> Warnings</h2></div><ul class="warn-list">${warnings.map((w) => `<li>${esc(w)}</li>`).join('')}</ul></section>` : ''}
      </div>
    </div>`;

  out.querySelectorAll('[data-jump]').forEach((a) =>
    a.addEventListener('click', (e) => {
      e.preventDefault();
      const target = document.getElementById(a.dataset.jump);
      if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    })
  );

  new DataTable(out.querySelector('#lv-steps'), stepColumns(colors), {
    sortKey: 'from',
    sortDir: 'asc',
    rowKey: (r) => `${r.from}-${r.spell_id}`,
    expand: stepExpand,
    rowClass: (r) => (r.confidence === 'low' ? 'row-lowconf' : ''),
  }).setRows(steps);
}

function milestonesFor(plan) {
  const given = (plan.milestones || []).filter((m) => m && isNum(m.skill));
  return (given.length ? given.map((m) => ({ skill: m.skill, text: m.text || '' })) : TRAINER.slice()).sort((a, b) => a.skill - b.skill);
}

function skillBar(steps, colors, plan, start, target, unreach) {
  const lo = 1;
  const span = Math.max(1, target - lo);
  const pos = (s) => Math.max(0, Math.min(100, ((s - lo) / span) * 100));
  const parts = [];
  if (start > lo) parts.push(`<div class="sbar-done" style="left:0;width:${pos(start).toFixed(2)}%" title="Already reached: 1–${num(start)}"></div>`);
  for (const s of steps) {
    const l = pos(s.from);
    const w = Math.max(0.35, pos(s.to) - l);
    const name = s.name || `Spell #${s.spell_id}`;
    parts.push(`<div class="sbar-seg c${colors.get(s.spell_id)}" style="left:${l.toFixed(2)}%;width:${w.toFixed(2)}%" title="${esc(`${s.from}–${s.to} · ${name} · ${s.crafts} crafts`)}">${w >= 9 ? `<span>${esc(name)}</span>` : ''}</div>`);
  }
  if (unreach !== null && unreach < target) parts.push(`<div class="sbar-stop" style="left:${pos(unreach).toFixed(2)}%;width:${(100 - pos(unreach)).toFixed(2)}%" title="No usable priced recipe from ${num(unreach)}"></div>`);

  // Milestone texts vary by profession – use the plan's own list when it has one.
  const ms = milestonesFor(plan).filter((m) => m.skill > lo && m.skill < target);

  const ticks = ms.map((m) => `<span class="sbar-tick ${m.skill < start ? 'past' : ''}" style="left:${pos(m.skill).toFixed(2)}%" title="${esc(m.text)}"><i></i><b>${m.skill}</b></span>`).join('');
  const marks = [lo, ...(start > lo ? [start] : []), target];
  return `<div class="sbar" role="img" aria-label="${esc(`Skill path from ${start} to ${target} in ${steps.length} steps`)}">
      <div class="sbar-track">${parts.join('')}${ms.map((m) => `<span class="sbar-line" style="left:${pos(m.skill).toFixed(2)}%"></span>`).join('')}</div>
      <div class="sbar-ticks">${ticks}</div>
      <div class="sbar-ends">${marks.map((m, i) => `<span class="${i === 0 ? 'first' : m === target ? 'last' : 'mid'}" style="left:${pos(m).toFixed(2)}%">${m === start && start > lo ? `start ${m}` : m}</span>`).join('')}</div>
    </div>
    ${ms.length ? `<ul class="ms-list">${ms.map((m) => `<li class="${m.skill <= start ? 'past' : ''}"><b>${m.skill}</b> ${esc(m.text)}</li>`).join('')}</ul>` : ''}`;
}

function stepColumns(colors) {
  return [
    {
      key: 'from',
      label: 'Skill',
      sortable: false,
      render: (r) => `<span class="lv-range"><span class="sw c${colors.get(r.spell_id)}" aria-hidden="true"></span>${num(r.from)}–${num(r.to)}</span>`,
    },
    {
      key: 'name',
      label: 'Recipe',
      sortable: false,
      render: (r) => `${recipeName(r)}${isNum(r.recipe_item) ? ` <span class="tag-mini warn" title="This recipe is taught by an item you need to buy – see Recipes to buy">recipe to buy</span>` : ''}<div class="cell-sub">${skillRange(r.skill)}${chanceText(r.chance)}</div><div class="cell-sub show-sm"><strong>${num(r.crafts)}</strong> crafts · ≈ ${num(r.crafts_expected, 1)} expected</div>`,
    },
    {
      key: 'crafts',
      label: 'Crafts',
      align: 'right',
      sortable: false,
      hideSm: true,
      title: 'Crafts to make (rounded up) – expected value below',
      render: (r) => `<strong>${num(r.crafts)}</strong><div class="cell-sub">≈ ${num(r.crafts_expected, 1)} expected</div>${isNum(r.kept) && r.kept > 0 ? `<div class="cell-sub" title="Products kept as reagents for later steps">${num(r.kept)} kept for later</div>` : ''}`,
    },
    {
      key: 'cost_total',
      label: 'Cost',
      align: 'right',
      sortable: false,
      hideSm: true,
      render: (r) => `${money(r.cost_total)}<div class="cell-sub">${money(r.cost_each)} each</div>${isNum(r.reuse_saving) && r.reuse_saving > 0 ? `<div class="cell-sub up" title="Saved by using items crafted in earlier steps">−${money(r.reuse_saving)} reused</div>` : ''}`,
    },
    {
      key: 'recover_total',
      label: 'Recovered',
      align: 'right',
      sortable: false,
      hideSm: true,
      render: (r) => (isNum(r.recover_total) && r.recover_total > 0 ? `${money(r.recover_total)}<div class="cell-sub">${money(r.recover_each)} each${r.recover_source === 'vendor' ? ' · vendor' : r.recover_source === 'ah' ? ' · AH' : r.recover_source === 'mixed' ? ' · AH + vendor' : ''}</div>${depthText(r)}` : '<span class="muted">–</span>'),
    },
    {
      key: 'net_total',
      label: 'Net cost',
      align: 'right',
      sortable: false,
      render: (r) => netCell(r.net_total, r.net_each),
    },
    {
      key: 'confidence',
      label: 'Data',
      align: 'center',
      sortable: false,
      hideSm: true,
      title: 'Confidence of the prices behind this step',
      render: (r) => confBadge(r.confidence) || '<span class="muted" title="Good price data">✓</span>',
    },
  ];
}

function netCell(total, each) {
  if (!isNum(total)) return '<span class="muted">–</span>';
  const sub = isNum(each) ? `<div class="cell-sub">${each < 0 ? `${money(-each)} profit each` : `${money(each)} each`}</div>` : '';
  if (total < 0) return `<span class="lv-gain">${money(-total, { signed: true })}</span>${sub}`;
  return `${money(total)}${sub}`;
}

function stepExpand(s) {
  const regs = s.reagents || [];
  if (!regs.length) return '<div class="muted pad">No reagents recorded.</div>';
  return `<div class="reagents">
    <table class="mini">
      <thead><tr><th>Reagent</th><th class="r">Per craft</th><th class="r">Need</th><th class="r">Unit price</th><th>Source</th><th class="r">Total</th></tr></thead>
      <tbody>${regs
        .map((g) => {
          const need = isNum(g.count) && isNum(s.crafts) ? g.count * s.crafts : null;
          return `<tr class="${g.source === 'none' || !isNum(g.unit_price) ? 'row-incomplete' : ''}">
            <td>${itemLink(g.item_id, g.name, g.quality)}${confBadge(g.confidence)} ${flagChip(g.price_flag)}</td>
            <td class="r">${num(g.count)}</td>
            <td class="r">${num(need)}</td>
            <td class="r">${money(g.unit_price)}</td>
            <td>${sourceLabel(g.source)}${g.source === 'craft' && g.craft_name ? `<div class="cell-sub">${esc(g.craft_name)}</div>` : ''}${isNum(g.from_earlier_steps) && g.from_earlier_steps > 0 ? `<div class="cell-sub up">${num(g.from_earlier_steps)} from earlier steps</div>` : ''}</td>
            <td class="r">${isNum(need) && isNum(g.unit_price) ? money(need * g.unit_price) : '–'}</td>
          </tr>`;
        })
        .join('')}</tbody>
      <tfoot>
        <tr><td colspan="5">Cost for ${num(s.crafts)} crafts</td><td class="r">${money(s.cost_total)}</td></tr>
        ${isNum(s.recover_total) && s.recover_total > 0 ? `<tr><td colspan="5">Recovered from ${s.created_item_id ? itemLink(s.created_item_id, s.name, s.created_quality) : 'products'}</td><td class="r">${money(s.recover_total)}</td></tr>` : ''}
      </tfoot>
    </table>
  </div>`;
}

function shoppingTable(rows) {
  if (!rows.length) return emptyState('Nothing to buy', '', '☰');
  const buy = rows.filter((r) => r.source !== 'craft');
  const craft = rows.filter((r) => r.source === 'craft');
  const total = buy.reduce((s, r) => s + (isNum(r.total) ? r.total : 0), 0);
  const tr = (r) => `<tr class="${r.source === 'none' || !isNum(r.unit_price) ? 'row-incomplete' : ''}">
      <td>${itemLink(r.item_id, r.name, r.quality)}${confBadge(r.confidence)} ${flagChip(r.price_flag)}</td>
      <td class="r">${num(r.need)}</td>
      <td class="r hide-sm">${money(r.unit_price)}</td>
      <td>${sourceLabel(r.source)}</td>
      <td class="r">${money(r.total)}</td>
      <td class="r hide-sm">${r.source === 'ah' ? num(r.available) : '<span class="muted">–</span>'}</td>
    </tr>`;
  return `<div class="table-wrap"><table class="mini lv-shop">
    <thead><tr><th>Item</th><th class="r">Need</th><th class="r hide-sm">Unit</th><th>Source</th><th class="r">Total</th><th class="r hide-sm" title="Quantity on the AH now">On AH</th></tr></thead>
    <tbody>${buy.map(tr).join('')}</tbody>
    ${craft.length ? `<tbody class="lv-craft"><tr class="group-row"><td colspan="6">Craft yourself – cheaper than buying (materials are included above)</td></tr>${craft.map(tr).join('')}</tbody>` : ''}
    <tfoot><tr><td colspan="4">Total to buy</td><td class="r">${money(total)}</td><td class="hide-sm"></td></tr></tfoot>
  </table></div>`;
}

function recipesCard(list, sources) {
  if (!list.length && sources !== 'all') return '';
  return `<section class="card">
    <div class="card-head"><h2>Recipes to buy</h2>${list.length ? `<span class="card-note">${money(list.reduce((s, r) => s + (isNum(r.price) ? r.price : 0), 0))} total</span>` : ''}</div>
    ${list.length
      ? `<ul class="list">${list
          .slice()
          .sort((a, b) => (a.skill ?? 0) - (b.skill ?? 0))
          .map((r) => `<li class="list-row"><div class="list-main"><span>${itemLink(r.item_id, r.name, r.quality)}${confBadge(r.confidence)}</span><div class="list-meta">${r.recipe_name ? `teaches ${esc(r.recipe_name)}` : 'teaches a recipe used in the plan'}${isNum(r.skill) ? ` · skill ${num(r.skill)}` : ''}</div></div><div class="list-side">${money(r.price)}${r.source ? `<div class="list-sub">${sourceLabel(r.source)}</div>` : ''}</div></li>`)
          .join('')}</ul>`
      : '<div class="muted">No recipe purchases needed – trainer recipes are cheapest here.</div>'}
  </section>`;
}

function chanceText(c) {
  if (!c || !isNum(c.start)) return '';
  const p = (v) => `${Math.round(v * 100)}%`;
  const same = !isNum(c.end) || Math.round(c.start * 100) === Math.round(c.end * 100);
  return ` <span class="lv-chance" title="Chance of a skill-up per craft at the start and end of this step">· ${same ? p(c.start) : `${p(c.start)} → ${p(c.end)}`} chance</span>`;
}

/** Market-depth cap (when the backend provides it): how many products go to the AH vs. a vendor. */
function depthText(r) {
  if (!isNum(r.ah_units) && !isNum(r.vendor_units)) return '';
  if (!isNum(r.vendor_units) || r.vendor_units <= 0) return '';
  return `<div class="cell-sub warn-text" title="The AH can only absorb so many – the rest is vendored">AH ${num(r.ah_units || 0)} / vendor ${num(r.vendor_units)}</div>`;
}

function subcraftsCard(list) {
  if (!list.length) return '';
  return `<section class="card card-flush">
    <div class="card-head pad-x"><h2>Crafted along the way</h2><span class="card-note">Cheaper than buying</span></div>
    <div class="table-wrap"><table class="mini lv-shop">
      <thead><tr><th>Item</th><th class="r">Need</th><th class="r">Crafts</th><th class="r">Unit cost</th></tr></thead>
      <tbody>${list
        .map((x) => `<tr>
          <td>${x.item_id ? itemLink(x.item_id, x.name, x.quality) : `<span class="spell-name">${esc(x.name || '')}</span>`}${x.name && x.item_id ? '' : ''}</td>
          <td class="r">${num(x.need)}</td>
          <td class="r">${num(x.crafts)}${isNum(x.num_created) && x.num_created > 1 ? ` <span class="muted">×${num(x.num_created)}</span>` : ''}</td>
          <td class="r">${money(x.unit_cost)}</td>
        </tr>`)
        .join('')}</tbody>
    </table></div>
    <p class="card-foot-note pad-x">Their materials are included in the shopping list.</p>
  </section>`;
}

function toolsCard(list) {
  if (!list.length) return '';
  return `<section class="card">
    <div class="card-head"><h2>Tools</h2><span class="card-note">Not included in the totals</span></div>
    <ul class="list">${list
      .map((t) => `<li class="list-row"><div class="list-main">${t.item_id ? itemLink(t.item_id, t.name, 1) : esc(t.name || '')}<div class="list-meta">${isNum(t.skill) ? `make at skill ${num(t.skill)}` : ''}</div></div><div class="list-side">${money(t.cost)}</div></li>`)
      .join('')}</ul>
  </section>`;
}
