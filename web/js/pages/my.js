import { api, WEB, loadState, replaceState } from '../api.js';
import {
  esc, money, num, skillRange, debounce, errorState, skeletonRows, emptyState, isNum, confBadge,
} from '../util.js';
import { DataTable } from '../table.js';
import { toast, emit, addShopCtl } from '../ui.js';
import { PROF_ICONS, recipeColumns, recipeExpand, recipeName, mountRecipeTrends } from './professions.js';

const SKILL_MAX = 375;
const GATHERING = new Set([5, 7, 11, 12]);
const state = { mode: 'current', tab: null, show: 'all', text: '', hideLow: true };

export async function render(ctx) {
  const { el } = ctx;
  el.innerHTML = `
    <header class="page-head">
      <div>
        <h1>My Professions</h1>
        <p class="lead">Tell the tool what you can craft. Recipes count as craftable when your skill is at least the recipe's orange (trainer) level – override single recipes below if you have not learned them, or know a recipe from a drop.</p>
      </div>
    </header>
    ${WEB ? `<section class="card">
      <div class="card-head"><h2>Saved in this browser</h2></div>
      <p class="card-note">Your professions, known recipes, shopping list and watchlist are stored only in this browser – nothing about you is kept on the server. Export them to move to another browser or PC, or to keep a backup.</p>
      <div class="set-foot">
        <button type="button" class="btn btn-sm" id="st-export">Export setup</button>
        <label class="btn btn-sm">Import setup<input type="file" id="st-import" accept="application/json,.json" hidden></label>
      </div>
    </section>` : ''}
    <section class="card">
      <div class="card-head"><h2>Your professions</h2><span class="card-note" id="mp-saved" aria-live="polite"></span></div>
      <div id="mp-profs">${skeletonRows(4, 3)}</div>
    </section>
    <section class="card card-flush">
      <div class="card-head pad-x">
        <h2>Craftable now</h2>
        <div class="card-tools">
          <label class="toggle toggle-sm" title="Hide recipes whose prices rest on very little data or a thin market"><input type="checkbox" id="mc-hide-low" ${state.hideLow ? 'checked' : ''}><span class="toggle-ui"></span> Hide low-confidence</label>
          <div class="seg seg-sm" role="radiogroup" aria-label="Price basis" id="mc-mode">
            <button class="seg-btn ${state.mode === 'current' ? 'on' : ''}" data-mode="current" role="radio" aria-checked="${state.mode === 'current'}">Current price</button>
            <button class="seg-btn ${state.mode === 'baseline' ? 'on' : ''}" data-mode="baseline" role="radio" aria-checked="${state.mode === 'baseline'}">Market price</button>
          </div>
        </div>
      </div>
      <div class="pad-x result-note" id="mc-note"></div>
      <div id="mc-table">${skeletonRows(6, 6)}</div>
    </section>
    <section class="card card-flush" id="kr-sec">
      <div class="card-head pad-x"><h2>Recipes</h2><span class="card-note">Auto = decided by your skill</span></div>
      <div class="pad-x kr-bar" id="kr-bar"></div>
      <div id="kr-table">${skeletonRows(6, 5)}</div>
    </section>`;

  let profs = [];
  let mine = new Map(); // profession_id -> skill
  if (WEB) wireSetupTransfer(el);

  const craftTable = new DataTable(el.querySelector('#mc-table'), recipeColumns({ shop: true, showProf: true, noCraftTag: true, noSkill: true }), {
    sortKey: 'profit',
    sortDir: 'desc',
    rowKey: (r) => r.spell_id,
    expand: recipeExpand,
    onRender: mountRecipeTrends,
    rowClass: (r) => (!r.cost_complete ? 'row-incomplete' : ''),
    limit: 25,
    empty: emptyState('Nothing craftable yet', 'Pick a profession and set your skill above.', '⚒'),
  });

  const knownTable = new DataTable(el.querySelector('#kr-table'), knownColumns(), {
    sortKey: 'skill',
    sortDir: 'asc',
    rowKey: (r) => r.spell_id,
    expand: recipeExpand,
    onRender: mountRecipeTrends,
    rowClass: (r) => (r.can_craft ? '' : 'row-dim'),
    empty: emptyState('No recipes match', 'Change the filter or the search.'),
  });

  // ---- professions picker
  const drawProfs = () => {
    const box = el.querySelector('#mp-profs');
    const sorted = profs.slice().sort((a, b) => (b.recipe_count > 0) - (a.recipe_count > 0) || a.name.localeCompare(b.name));
    box.innerHTML = `<div class="myprof-grid">${sorted
      .map((p) => {
        const on = mine.has(p.id);
        const sk = on ? mine.get(p.id) : SKILL_MAX;
        return `<div class="myprof ${on ? 'on' : ''} ${p.recipe_count ? '' : 'gather'}" data-prof="${esc(p.id)}">
          <label class="myprof-head">
            <input type="checkbox" data-pick="${esc(p.id)}" ${on ? 'checked' : ''}>
            <span class="prof-icon" aria-hidden="true">${PROF_ICONS[p.id] || '◇'}</span>
            <span class="myprof-name">${esc(p.name)}<span class="prof-sub">${p.recipe_count ? `${num(p.recipe_count)} recipes` : (GATHERING.has(p.id) ? 'gathering – no recipes' : 'no recipes loaded')}</span></span>
            <span class="myprof-check" aria-hidden="true">✓</span>
          </label>
          <div class="myprof-skill">
            <input type="range" min="0" max="${SKILL_MAX}" step="1" value="${sk}" data-skill-range="${esc(p.id)}" ${on ? '' : 'disabled'} aria-label="${esc(p.name)} skill">
            <input type="number" min="0" max="${SKILL_MAX}" step="1" value="${sk}" data-skill-num="${esc(p.id)}" ${on ? '' : 'disabled'} aria-label="${esc(p.name)} skill value">
          </div>
        </div>`;
      })
      .join('')}</div>`;
  };

  const flash = (msg) => {
    const n = el.querySelector('#mp-saved');
    if (!n) return;
    n.textContent = msg;
    clearTimeout(n._t);
    n._t = setTimeout(() => (n.textContent = ''), 2200);
  };

  const saveProfs = debounce(async () => {
    flash('Saving…');
    try {
      await api.setMyProfessions([...mine.entries()].map(([profession_id, skill]) => ({ profession_id, skill })));
      emit('myprofs', {});
      if (!ctx.alive()) return;
      flash('Saved ✓');
      loadCrafts();
      loadKnown();
    } catch (err) {
      flash('');
      toast(`<strong>Could not save professions</strong><br>${esc(err.message)}`, 'err');
    }
  }, 600);

  const box = el.querySelector('#mp-profs');
  box.addEventListener('change', (e) => {
    const pick = e.target.closest('[data-pick]');
    if (pick) {
      const id = Number(pick.dataset.pick);
      const card = pick.closest('.myprof');
      const range = card.querySelector('[data-skill-range]');
      const numIn = card.querySelector('[data-skill-num]');
      if (pick.checked) mine.set(id, Number(range.value));
      else mine.delete(id);
      card.classList.toggle('on', pick.checked);
      range.disabled = numIn.disabled = !pick.checked;
      if (pick.checked && !state.tab) state.tab = id;
      saveProfs();
    }
  });
  box.addEventListener('input', (e) => {
    const t = e.target.closest('[data-skill-range], [data-skill-num]');
    if (!t) return;
    const card = t.closest('.myprof');
    const id = Number(card.dataset.prof);
    const v = Math.max(0, Math.min(SKILL_MAX, Math.round(Number(t.value) || 0)));
    if (t.matches('[data-skill-range]')) card.querySelector('[data-skill-num]').value = v;
    else if (t.value !== '') card.querySelector('[data-skill-range]').value = v;
    if (mine.has(id)) {
      mine.set(id, v);
      saveProfs();
    }
  });

  // ---- craftable now
  let crafts = [];
  const applyCrafts = () => {
    const rows = state.hideLow ? crafts.filter((r) => r.confidence !== 'low') : crafts;
    const hidden = crafts.length - rows.length;
    const note = el.querySelector('#mc-note');
    if (note) note.textContent = hidden ? `${hidden} low-confidence recipe${hidden === 1 ? '' : 's'} hidden` : '';
    craftTable.setRows(rows);
  };
  el.querySelector('#mc-hide-low').addEventListener('change', (e) => {
    state.hideLow = e.target.checked;
    applyCrafts();
  });
  const loadCrafts = async () => {
    if (!mine.size) {
      crafts = [];
      applyCrafts();
      return;
    }
    try {
      const res = await api.myCrafts(state.mode, 200);
      if (!ctx.alive()) return;
      crafts = Array.isArray(res) ? res : [];
      applyCrafts();
    } catch (err) {
      if (ctx.alive()) el.querySelector('#mc-table').innerHTML = errorState(err);
    }
  };
  el.querySelectorAll('#mc-mode .seg-btn').forEach((b) =>
    b.addEventListener('click', () => {
      if (state.mode === b.dataset.mode) return;
      state.mode = b.dataset.mode;
      el.querySelectorAll('#mc-mode .seg-btn').forEach((x) => {
        x.classList.toggle('on', x === b);
        x.setAttribute('aria-checked', String(x === b));
      });
      el.querySelector('#mc-table').innerHTML = skeletonRows(6, 6);
      loadCrafts();
    })
  );

  // ---- known recipes per profession
  let recipes = [];
  const craftingMine = () => profs.filter((p) => mine.has(p.id) && p.recipe_count > 0);
  const drawBar = () => {
    const list = craftingMine();
    const bar = el.querySelector('#kr-bar');
    if (!list.length) {
      bar.innerHTML = '';
      return;
    }
    if (!list.some((p) => p.id === state.tab)) state.tab = list[0].id;
    bar.innerHTML = `
      <div class="seg seg-wrap" role="tablist" aria-label="Profession">${list
        .map((p) => `<button class="seg-btn ${p.id === state.tab ? 'on' : ''}" data-tab="${esc(p.id)}" role="tab" aria-selected="${p.id === state.tab}"><span aria-hidden="true">${PROF_ICONS[p.id] || '◇'}</span> ${esc(p.name)} <span class="seg-count">${num(mine.get(p.id))}</span></button>`)
        .join('')}</div>
      <div class="kr-filters">
        <div class="seg seg-sm" role="radiogroup" aria-label="Show">
          ${[['all', 'All'], ['craft', 'Can craft'], ['no', 'Cannot craft']].map(([v, l]) => `<button class="seg-btn ${state.show === v ? 'on' : ''}" data-show="${v}" role="radio" aria-checked="${state.show === v}">${l}</button>`).join('')}
        </div>
        <input type="search" class="text-in" id="kr-txt" placeholder="Search recipes…" value="${esc(state.text)}" aria-label="Search recipes">
      </div>`;
  };
  const applyKnown = () => {
    const t = state.text.trim().toLowerCase();
    knownTable.setRows(recipes.filter((r) => {
      if (state.show === 'craft' && !r.can_craft) return false;
      if (state.show === 'no' && r.can_craft) return false;
      if (t && !String(r.name || '').toLowerCase().includes(t)) return false;
      return true;
    }));
  };
  const loadKnown = async () => {
    drawBar();
    if (!craftingMine().length) {
      el.querySelector('#kr-table').innerHTML = emptyState('No crafting profession selected', 'Pick one above to review its recipes.', '⚒');
      return;
    }
    const tab = state.tab;
    try {
      const res = await api.profession(tab, state.mode);
      if (!ctx.alive() || tab !== state.tab) return;
      recipes = Array.isArray(res) ? res : [];
      applyKnown();
    } catch (err) {
      if (ctx.alive()) el.querySelector('#kr-table').innerHTML = errorState(err);
    }
  };
  const krSec = el.querySelector('#kr-sec');
  krSec.addEventListener('click', async (e) => {
    const tab = e.target.closest('[data-tab]');
    if (tab) {
      state.tab = Number(tab.dataset.tab);
      el.querySelector('#kr-table').innerHTML = skeletonRows(6, 5);
      loadKnown();
      return;
    }
    const show = e.target.closest('[data-show]');
    if (show) {
      state.show = show.dataset.show;
      krSec.querySelectorAll('[data-show]').forEach((x) => {
        x.classList.toggle('on', x === show);
        x.setAttribute('aria-checked', String(x === show));
      });
      applyKnown();
      return;
    }
    const kb = e.target.closest('[data-known]');
    if (kb) {
      const id = Number(kb.closest('[data-spell]').dataset.spell);
      const val = kb.dataset.known === 'auto' ? null : Number(kb.dataset.known);
      const r = recipes.find((x) => x.spell_id === id);
      if (r && r.known === val) return;
      kb.closest('.seg').querySelectorAll('.seg-btn').forEach((x) => x.classList.toggle('on', x === kb));
      try {
        await api.setKnown(id, val);
        emit('known', { spell_id: id, known: val });
        if (!ctx.alive()) return;
        loadKnown();
        loadCrafts();
      } catch (err) {
        toast(`<strong>Could not save</strong><br>${esc(err.message)}`, 'err');
        if (ctx.alive()) applyKnown();
      }
    }
  });
  krSec.addEventListener('input', debounce((e) => {
    if (e.target.id === 'kr-txt') {
      state.text = e.target.value;
      applyKnown();
    }
  }, 150));

  // ---- initial load
  try {
    const [p, m] = await Promise.all([api.professions(), api.myProfessions()]);
    if (!ctx.alive()) return;
    profs = Array.isArray(p) ? p : [];
    mine = new Map((Array.isArray(m) ? m : []).map((x) => [x.profession_id, x.skill]));
  } catch (err) {
    if (!ctx.alive()) return;
    el.querySelector('#mp-profs').innerHTML = errorState(err);
    el.querySelector('#mc-table').innerHTML = '';
    el.querySelector('#kr-table').innerHTML = '';
    return;
  }
  drawProfs();
  await Promise.all([loadCrafts(), loadKnown()]);
}

function knownColumns() {
  return [
    { key: 'name', label: 'Recipe', defaultDir: 'asc', sort: (r) => String(r.name || '').toLowerCase(), render: (r) => recipeName(r) + confBadge(r.confidence) },
    { key: 'skill', label: 'Skill', defaultDir: 'asc', hideSm: true, sort: (r) => (r.skill ? r.skill.min : null), render: (r) => skillRange(r.skill) },
    {
      key: 'can_craft',
      label: 'Status',
      sort: (r) => (r.can_craft ? 1 : 0),
      render: (r) => {
        if (r.can_craft) return '<span class="badge b-hit">✓ Can craft</span>';
        if (r.known === 0) return '<span class="badge b-muted">Not known</span>';
        return '<span class="badge b-muted" title="Your skill is below the orange level">Skill too low</span>';
      },
    },
    {
      key: 'known',
      label: 'Known?',
      sortable: false,
      render: (r) => {
        const k = r.known === 1 || r.known === 0 ? r.known : null;
        return `<span class="seg seg-sm seg-known" data-spell="${esc(r.spell_id)}" role="radiogroup" aria-label="Known status for ${esc(r.name)}">
          <button type="button" class="seg-btn ${k === null ? 'on' : ''}" data-known="auto" title="Decide by skill">Auto</button>
          <button type="button" class="seg-btn ${k === 1 ? 'on' : ''}" data-known="1" title="I know this recipe">Known</button>
          <button type="button" class="seg-btn ${k === 0 ? 'on' : ''}" data-known="0" title="I do not know this recipe">Unknown</button>
        </span>`;
      },
    },
    {
      key: 'profit',
      label: 'Profit',
      align: 'right',
      hideSm: true,
      render: (r) => (isNum(r.profit) ? (r.cost_complete ? money(r.profit, { signed: true }) : `<span class="incomplete">≤ ${money(r.profit, { signed: true })}</span>`) : '<span class="muted">–</span>'),
    },
    { key: 'shop', label: '', sortable: false, align: 'right', hideSm: true, render: (r) => (r.can_craft ? addShopCtl(r.spell_id, r.name) : '') },
  ];
}


// Web version: move the browser-stored setup between browsers as a JSON file.
function wireSetupTransfer(el) {
  el.querySelector('#st-export').addEventListener('click', () => {
    const { eval: _e, alertstate: _a, ...s } = loadState();
    const blob = new Blob([JSON.stringify({ ahtool_setup: 1, ...s }, null, 1)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `ahtool-setup-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  });
  el.querySelector('#st-import').addEventListener('change', async (e) => {
    const f = e.target.files && e.target.files[0];
    if (!f) return;
    try {
      const s = JSON.parse(await f.text());
      if (!s || s.ahtool_setup !== 1) throw new Error('This is not an AH Tool setup file.');
      delete s.ahtool_setup;
      if (!confirm('Replace the setup in this browser with the imported one?')) return;
      replaceState(s);
      toast('<strong>Setup imported</strong>', 'ok');
      location.reload();
    } catch (err) {
      toast(`<strong>Import failed</strong><br>${esc(err.message)}`, 'err');
    } finally {
      e.target.value = '';
    }
  });
}
