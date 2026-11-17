import { api } from '../api.js';
import {
  esc, money, num, itemLink, reasonBadge, sourceLabel, errorState, skeletonRows, skeletonTiles, emptyState, tile, isNum, qClass, ttAttr, confBadge, flagChip,
} from '../util.js';
import { DataTable } from '../table.js';
import { toast, emit } from '../ui.js';
import { scanAge } from '../market.js';

export async function render(ctx) {
  const { el } = ctx;
  el.innerHTML = `
    <header class="page-head">
      <div>
        <h1>Shopping List</h1>
        <p class="lead">Plan a crafting session: pick recipes and quantities, see every reagent you need, what it costs and what you earn – then send the list straight to Auctionator.</p>
      </div>
      <div class="head-actions" id="sh-actions"></div>
    </header>
    <div id="sh-tiles">${skeletonTiles(4)}</div>
    <section class="card export-card" id="sh-export"><span class="skel" style="width:40%"></span><span class="skel" style="width:70%"></span></section>
    <section class="card card-flush">
      <div class="card-head pad-x"><h2>Recipes</h2><span class="card-note" id="sh-rnote"></span></div>
      <div id="sh-recipes">${skeletonRows(4, 6)}</div>
    </section>
    <div class="grid-2" id="sh-regs">
      <section class="card"><div class="card-head"><h2>Buy on the AH</h2></div>${skeletonRows(5, 4)}</section>
      <section class="card"><div class="card-head"><h2>Buy from a vendor</h2></div>${skeletonRows(3, 4)}</section>
    </div>`;

  const recipeTable = new DataTable(el.querySelector('#sh-recipes'), recipeCols(), {
    sortKey: 'total_profit',
    sortDir: 'desc',
    rowKey: (r) => r.spell_id,
    rowClass: (r) => (!r.cost_complete ? 'row-incomplete' : ''),
  });

  let data = null;
  const pending = new Map(); // spell_id -> timer

  const draw = () => {
    const d = data || {};
    const recipes = d.recipes || [];
    if (!recipes.length) {
      el.querySelector('#sh-actions').innerHTML = '';
      el.querySelector('#sh-tiles').innerHTML = '';
      el.querySelector('#sh-export').hidden = true;
      el.querySelector('#sh-regs').hidden = true;
      el.querySelector('#sh-rnote').textContent = '';
      el.querySelector('#sh-recipes').innerHTML = emptyState(
        'Your shopping list is empty',
        'Add recipes with the <strong>＋ List</strong> button on a <a href="#/professions">profession page</a> or under <a href="#/my">My Professions</a> → Craftable now.',
        '☰'
      );
      return;
    }
    el.querySelector('#sh-export').hidden = false;
    el.querySelector('#sh-regs').hidden = false;
    el.querySelector('#sh-actions').innerHTML = '<button type="button" class="btn btn-danger-ghost" id="sh-clear">Clear all</button>';
    const t = d.totals || {};
    // `reagents` holds every reagent; split by source (vendor_reagents is only a subset of it).
    const all = d.reagents || [];
    const ah = all.filter((g) => g.source === 'ah');
    const vend = all.filter((g) => g.source === 'vendor');
    const unexp = d.unexportable || [];
    const incomplete = recipes.some((r) => !r.cost_complete);
    const units = recipes.reduce((s, r) => s + (r.qty || 0), 0);
    el.querySelector('#sh-tiles').innerHTML = `<div class="tiles">
      ${tile('Total cost', money(t.cost), incomplete ? '<span class="warn-text">⚠ some reagents have no price</span>' : `${num(all.length)} different reagents`, 'tile-accent')}
      ${tile('Sell value', money(t.sell), `${num(units)} crafts at current price`)}
      ${tile('Expected profit', money(t.profit, { signed: true }), recipes.some((r) => !isNum(r.profit_each)) ? 'after AH cut · recipes without a sell price skipped' : 'after the 5% AH cut', isNum(t.profit) && t.profit > 0 ? 'tile-hot' : '')}
      ${isNum(t.listing_cost)
        ? tile('AH reagents at real listings', money(t.listing_cost), t.listing_short_items ? `<span class="warn-text">⚠ ${num(t.listing_short_items)} reagent(s) not enough listed</span>` : `buying whole stacks · ${scanAge(d.listing_scan)}`)
        : tile('Reagent deals', num(ah.filter((g) => g.is_deal).length), 'reagents below market price now')}
    </div>`;
    el.querySelector('#sh-rnote').textContent = `${num(recipes.length)} recipes · ${num(units)} crafts`;
    recipeTable.setRows(recipes);
    renderExport(el.querySelector('#sh-export'), d);
    el.querySelector('#sh-regs').innerHTML = `
      <section class="card">
        <div class="card-head"><h2>Buy on the AH</h2><span class="card-note">${num(ah.length)} reagents${d.listing_scan_fresh ? ' · “Real cost” walks the listings of the latest full scan' : ''}</span></div>
        ${ah.length ? regTable(ah, true) : emptyState('Nothing to buy on the AH', '', '✓')}
      </section>
      <section class="card">
        <div class="card-head"><h2>Buy from a vendor</h2><span class="card-note">cheaper than the AH</span></div>
        ${vend.length ? regTable(vend, false) : emptyState('No vendor reagents', '', '✓')}
        ${unexp.length ? `<div class="sub-head">Not exportable to Auctionator</div>${regTable(unexp, false, true)}` : ''}
      </section>`;
  };

  const load = async () => {
    try {
      const res = await api.shopping(true);
      if (!ctx.alive()) return;
      data = res || {};
      const focused = document.activeElement && document.activeElement.closest && document.activeElement.closest('[data-qty]');
      const fid = focused ? focused.dataset.qty : null;
      draw();
      if (fid) {
        const inp = el.querySelector(`input[data-qty="${CSS.escape(fid)}"]`);
        if (inp) {
          inp.focus();
          inp.select();
        }
      }
    } catch (err) {
      if (!ctx.alive()) return;
      el.querySelector('#sh-recipes').innerHTML = errorState(err);
      el.querySelector('#sh-tiles').innerHTML = '';
      el.querySelector('#sh-export').hidden = true;
      el.querySelector('#sh-regs').hidden = true;
    }
  };

  const saveQty = (id, qty, delay = 0) => {
    clearTimeout(pending.get(id));
    pending.set(id, setTimeout(async () => {
      pending.delete(id);
      try {
        await api.setShopping(id, qty);
        emit('shopping', { spell_id: id, qty });
        if (qty <= 0) {
          const r = ((data && data.recipes) || []).find((x) => String(x.spell_id) === String(id));
          toast(`<strong>Removed</strong> ${esc(r ? r.name : 'recipe')} from the list`, 'ok');
        }
      } catch (err) {
        toast(`<strong>Could not update</strong><br>${esc(err.message)}`, 'err');
      }
      if (ctx.alive()) load();
    }, delay));
  };

  el.addEventListener('click', async (e) => {
    const step = e.target.closest('[data-step]');
    if (step) {
      const id = step.dataset.spell;
      const inp = el.querySelector(`input[data-qty="${CSS.escape(id)}"]`);
      const next = Math.max(0, (Math.round(Number(inp.value)) || 0) + Number(step.dataset.step));
      if (next === 0) {
        saveQty(id, 0);
        return;
      }
      inp.value = next;
      saveQty(id, next, 350);
      return;
    }
    const rm = e.target.closest('[data-remove]');
    if (rm) {
      rm.disabled = true;
      saveQty(rm.dataset.remove, 0);
      return;
    }
    const clear = e.target.closest('#sh-clear');
    if (clear) {
      if (!clear.classList.contains('confirm')) {
        clear.classList.add('confirm');
        clear.textContent = 'Click again to clear';
        setTimeout(() => {
          if (clear.isConnected) {
            clear.classList.remove('confirm');
            clear.textContent = 'Clear all';
          }
        }, 3000);
        return;
      }
      clear.disabled = true;
      try {
        await api.clearShopping();
        emit('shopping', { cleared: true });
        toast('<strong>Shopping list cleared</strong>', 'ok');
      } catch (err) {
        toast(`<strong>Could not clear</strong><br>${esc(err.message)}`, 'err');
      }
      if (ctx.alive()) load();
      return;
    }
    const copy = e.target.closest('#sh-copy');
    if (copy) copyImport(el, copy, data && data.auctionator_import);
  });
  el.addEventListener('input', (e) => {
    const inp = e.target.closest('input[data-qty]');
    if (!inp) return;
    const v = Math.round(Number(inp.value));
    if (inp.value.trim() === '' || !isFinite(v) || v < 1) return; // wait for a valid number; remove via × or −
    saveQty(inp.dataset.qty, Math.min(9999, v), 700);
  });

  await load();
}

function recipeCols() {
  return [
    {
      key: 'name',
      label: 'Recipe',
      defaultDir: 'asc',
      sort: (r) => String(r.name || '').toLowerCase(),
      render: (r) => `${r.created_item_id ? itemLink(r.created_item_id, r.name, r.created_quality) : `<span class="spell-name">${esc(r.name || `Spell #${r.spell_id}`)}</span>`}${confBadge(r.confidence)}
        ${!r.cost_complete ? ' <span class="tag-mini warn" title="Missing a price for at least one reagent">incomplete</span>' : ''}
        <div class="cell-sub">${esc(r.profession_name || '')}</div>`,
    },
    {
      key: 'qty',
      label: 'Qty',
      align: 'center',
      render: (r) => `<span class="stepper">
        <button type="button" data-step="-1" data-spell="${esc(r.spell_id)}" aria-label="One less">−</button>
        <input type="number" min="1" max="9999" step="1" inputmode="numeric" value="${esc(r.qty)}" data-qty="${esc(r.spell_id)}" aria-label="Quantity of ${esc(r.name)}">
        <button type="button" data-step="1" data-spell="${esc(r.spell_id)}" aria-label="One more">+</button>
      </span>`,
    },
    { key: 'cost_each', label: 'Cost each', align: 'right', hideSm: true, render: (r) => money(r.cost_each) },
    { key: 'profit_each', label: 'Profit each', align: 'right', hideSm: true, render: (r) => (isNum(r.profit_each) ? money(r.profit_each, { signed: true }) : '<span class="muted">–</span>') },
    { key: 'total_cost', label: 'Total cost', align: 'right', hideSm: true, render: (r) => money(r.total_cost) },
    {
      key: 'total_profit',
      label: 'Total profit',
      align: 'right',
      render: (r) => (isNum(r.total_profit) ? (r.cost_complete ? money(r.total_profit, { signed: true }) : `<span class="incomplete">≤ ${money(r.total_profit, { signed: true })}</span>`) : '<span class="muted" title="Does not create an item">–</span>'),
    },
    { key: 'rm', label: '', sortable: false, align: 'right', render: (r) => `<button type="button" class="icon-btn icon-btn-sm" data-remove="${esc(r.spell_id)}" title="Remove from list" aria-label="Remove ${esc(r.name)}">×</button>` },
  ];
}

function regTable(rows, isAh, withReason = false) {
  const total = rows.reduce((s, g) => s + (isNum(g.total) ? g.total : 0), 0);
  const real = isAh && rows.some((g) => g.listing);
  const realTotal = rows.reduce((s, g) => s + (g.listing ? g.listing.total : 0), 0);
  return `<div class="table-wrap"><table class="mini">
    <thead><tr><th>Reagent</th><th class="r">Need</th><th class="r">Unit</th><th class="r">Total</th>${real ? '<th class="r" title="Buying whole stacks from the cheapest up, in the latest full scan">Real cost</th>' : ''}${isAh ? '<th class="r hide-sm">On AH</th>' : '<th class="hide-sm">Source</th>'}</tr></thead>
    <tbody>${rows
      .map((g) => {
        const short = isAh && isNum(g.available) && g.available < g.need;
        return `<tr class="${g.source === 'none' || !isNum(g.unit_price) ? 'row-incomplete' : ''}">
          <td>${itemLink(g.item_id, g.name, g.quality)}${confBadge(g.confidence)}${g.price_flag ? " " + flagChip(g.price_flag) : ""}${withReason && g.reason ? `<div class="cell-sub warn-text">${esc(g.reason)}</div>` : ''}${g.is_deal ? ` ${reasonBadge('cheap_reagent', isNum(g.baseline) ? 'Below market price now – good time to buy' : '')}` : ''}
            ${isAh && g.is_deal && isNum(g.baseline) ? `<div class="cell-sub">market ${money(g.baseline)}</div>` : ''}</td>
          <td class="r"><strong>${num(g.need)}</strong></td>
          <td class="r">${money(g.unit_price)}</td>
          <td class="r">${money(g.total)}</td>
          ${real ? `<td class="r">${g.listing ? `${money(g.listing.total)}${g.listing.short ? `<div class="cell-sub warn-text">${num(g.listing.short)} short</div>` : (g.listing.avg_unit ? `<div class="cell-sub">avg ${money(g.listing.avg_unit)}</div>` : '')}` : '<span class="muted">–</span>'}</td>` : ''}
          ${isAh ? `<td class="r hide-sm ${short ? 'warn-text' : ''}" ${short ? 'title="Fewer listed than you need"' : ''}>${num(g.available)}${short ? ' ⚠' : ''}</td>` : `<td class="hide-sm">${sourceLabel(g.source)}</td>`}
        </tr>`;
      })
      .join('')}</tbody>
    <tfoot><tr><td colspan="3">Total</td><td class="r">${money(total)}</td>${real ? `<td class="r">${money(realTotal)}</td>` : ''}<td class="hide-sm"></td></tr></tfoot>
  </table></div>`;
}

function renderExport(box, d) {
  const txt = d.auctionator_import || '';
  const all = d.reagents || [];
  const unexp = d.unexportable || [];
  const vend = all.filter((g) => g.source === 'vendor');
  const n = all.filter((g) => g.source === 'ah' && g.name).length;
  box.innerHTML = `
    <div class="export-grid">
      <div>
        <h2 class="export-title"><span class="export-ico" aria-hidden="true">⇪</span> Send to Auctionator</h2>
        <ol class="howto">
          <li>Click <strong>Copy Auctionator import</strong>.</li>
          <li>In game, open the Auction House → <strong>Auctionator</strong> → <strong>Shopping</strong> tab.</li>
          <li>Click <strong>Import</strong>, paste (<kbd>Ctrl</kbd>+<kbd>V</kbd>) and confirm.</li>
          <li>The list <strong>“AH Tool”</strong> appears – select it and press <strong>Search</strong>.</li>
        </ol>
        <p class="export-note">${n ? `${num(n)} AH reagents with quantity and a max price (current or market price + markup, whichever is higher).` : 'No AH reagents to export.'}
          ${vend.length ? ` ${num(vend.length)} vendor reagent${vend.length === 1 ? ' is' : 's are'} left out – buy ${vend.length === 1 ? 'it' : 'them'} from an NPC.` : ''}</p>
        ${unexp.length ? `<p class="export-warn">⚠ Not included: ${unexp.map((g) => `<span class="${qClass(g.quality)}" ${ttAttr(g.item_id)} ${g.reason ? `title="${esc(g.reason)}"` : ''}>${esc(g.name || `Item #${g.item_id}`)}</span>`).join(', ')} – add ${unexp.length === 1 ? 'it' : 'them'} manually.</p>` : ''}
      </div>
      <div class="export-side">
        <button type="button" class="btn btn-gold btn-lg" id="sh-copy" ${txt ? '' : 'disabled'}><span aria-hidden="true">⧉</span> Copy Auctionator import</button>
        <details class="export-raw">
          <summary>Show import text</summary>
          <textarea readonly rows="4" id="sh-raw" aria-label="Auctionator import text">${esc(txt)}</textarea>
        </details>
      </div>
    </div>`;
}

async function copyImport(el, btn, txt) {
  if (!txt) return;
  let ok = false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(txt);
      ok = true;
    }
  } catch (e) {
    ok = false;
  }
  if (!ok) {
    const ta = document.createElement('textarea');
    ta.value = txt;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try {
      ok = document.execCommand('copy');
    } catch (e) {
      ok = false;
    }
    ta.remove();
  }
  if (ok) {
    const label = btn.innerHTML;
    btn.classList.add('copied');
    btn.innerHTML = '<span aria-hidden="true">✓</span> Copied – paste it in Auctionator';
    toast('<strong>Copied!</strong> In game: Auctionator → Shopping → Import → paste.', 'ok');
    setTimeout(() => {
      if (btn.isConnected) {
        btn.classList.remove('copied');
        btn.innerHTML = label;
      }
    }, 2600);
  } else {
    const det = el.querySelector('.export-raw');
    det.open = true;
    const ta = el.querySelector('#sh-raw');
    ta.focus();
    ta.select();
    toast('<strong>Copy blocked by the browser</strong> – the text is selected, press Ctrl+C.', 'err');
  }
}
