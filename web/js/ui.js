// Shared interactive UI: toasts, gold/silver/copper inputs, Watch popover, "Add to shopping list" control.
// Buttons are wired by delegation (installGlobalActions), so pages only render HTML.
import { api } from './api.js';
import { esc, money, isNum, itemName, qClass, ttAttr } from './util.js';

// ---------------------------------------------------------------- toasts
export function toast(html, kind = '', ms = 4200) {
  const box = document.getElementById('toasts');
  if (!box) return;
  const t = document.createElement('div');
  t.className = `toast ${kind}`;
  t.innerHTML = html;
  box.appendChild(t);
  setTimeout(() => t.classList.add('out'), ms);
  setTimeout(() => t.remove(), ms + 500);
}

// ---------------------------------------------------------------- money input (g / s / c)
export function moneyFields(copper, { label = 'Price', idp = 'mf' } = {}) {
  const has = isNum(copper);
  const c = has ? Math.round(copper) : 0;
  const g = has ? Math.floor(c / 10000) : '';
  const s = has ? Math.floor((c % 10000) / 100) : '';
  const cc = has ? c % 100 : '';
  const f = (unit, v, max, w) =>
    `<label class="mi mi-${unit}"><input type="number" inputmode="numeric" min="0" ${max ? `max="${max}"` : ''} step="1" data-unit="${unit}" value="${v}" placeholder="0" aria-label="${esc(label)} – ${unit === 'g' ? 'gold' : unit === 's' ? 'silver' : 'copper'}" id="${idp}-${unit}" style="width:${w}"><i>${unit}</i></label>`;
  return `<span class="money-in" role="group" aria-label="${esc(label)}">${f('g', g, 0, '5.2em')}${f('s', s, 99, '3.4em')}${f('c', cc, 99, '3.4em')}</span>`;
}

/** Copper from a .money-in group, or null when every field is empty. */
export function readMoneyFields(root) {
  const box = root.matches && root.matches('.money-in') ? root : root.querySelector('.money-in');
  if (!box) return null;
  const vals = {};
  let any = false;
  for (const inp of box.querySelectorAll('input[data-unit]')) {
    const raw = inp.value.trim();
    if (raw !== '') any = true;
    vals[inp.dataset.unit] = Math.max(0, Math.floor(Number(raw) || 0));
  }
  if (!any) return null;
  return (vals.g || 0) * 10000 + Math.min(99, vals.s || 0) * 100 + Math.min(99, vals.c || 0);
}

export function setMoneyFields(root, copper) {
  const box = root.matches && root.matches('.money-in') ? root : root.querySelector('.money-in');
  if (!box) return;
  const c = isNum(copper) ? Math.round(copper) : null;
  const vals = c === null ? { g: '', s: '', c: '' } : { g: Math.floor(c / 10000), s: Math.floor((c % 10000) / 100), c: c % 100 };
  box.querySelectorAll('input[data-unit]').forEach((i) => (i.value = vals[i.dataset.unit]));
}

// ---------------------------------------------------------------- watch button
export function watchBtn({ id, name, quality, price, watch }, { small = true, iconOnlySm = false } = {}) {
  const on = !!watch;
  return `<button type="button" class="btn ${small ? 'btn-sm' : ''} watch-btn ${on ? 'on' : ''}" data-watch="${esc(id)}"
    data-name="${esc(name || '')}" data-quality="${isNum(quality) ? quality : ''}" data-price="${isNum(price) ? price : ''}"
    data-max="${on && isNum(watch.max_price) ? watch.max_price : ''}" data-note="${on ? esc(watch.note || '') : ''}" data-watching="${on ? '1' : ''}"
    title="${on ? 'Watching – edit alert' : 'Watch this item and get an alert'}" aria-label="${on ? 'Edit watch for' : 'Watch'} ${esc(itemName(name, id))}">
    <span class="wb-ico" aria-hidden="true">${on ? '★' : '☆'}</span><span class="wb-label ${iconOnlySm ? 'hide-sm' : ''}">${on ? 'Watching' : 'Watch'}</span></button>`;
}

function syncWatchButtons(id, watch) {
  document.querySelectorAll(`[data-watch="${CSS.escape(String(id))}"]`).forEach((b) => {
    const on = !!watch;
    b.classList.toggle('on', on);
    b.dataset.watching = on ? '1' : '';
    b.dataset.max = on && isNum(watch.max_price) ? watch.max_price : '';
    b.dataset.note = on ? watch.note || '' : '';
    b.querySelector('.wb-ico').textContent = on ? '★' : '☆';
    b.querySelector('.wb-label').textContent = on ? 'Watching' : 'Watch';
    b.title = on ? 'Watching – edit alert' : 'Watch this item and get an alert';
  });
}

// ---------------------------------------------------------------- popover
let pop = null;
let popAnchor = null;

export function closePopover() {
  if (pop) {
    pop.remove();
    pop = null;
    if (popAnchor && document.body.contains(popAnchor)) popAnchor.setAttribute('aria-expanded', 'false');
    popAnchor = null;
  }
}

function placePopover(el, anchor) {
  if (window.matchMedia('(max-width: 640px)').matches) {
    el.classList.add('sheet');
    return;
  }
  const r = anchor.getBoundingClientRect();
  const w = el.offsetWidth;
  const h = el.offsetHeight;
  let left = r.right - w;
  if (left < 12) left = Math.min(r.left, window.innerWidth - w - 12);
  left = Math.max(12, left);
  let top = r.bottom + 8;
  if (top + h > window.innerHeight - 12 && r.top - h - 8 > 12) top = r.top - h - 8;
  el.style.left = `${left}px`;
  el.style.top = `${Math.max(12, top)}px`;
}

export function openPopover(anchor, html, onMount) {
  closePopover();
  pop = document.createElement('div');
  pop.className = 'pop';
  pop.setAttribute('role', 'dialog');
  pop.innerHTML = html;
  document.body.appendChild(pop);
  popAnchor = anchor;
  anchor.setAttribute('aria-expanded', 'true');
  placePopover(pop, anchor);
  if (onMount) onMount(pop);
  const first = pop.querySelector('input, button');
  if (first) first.focus({ preventScroll: true });
  return pop;
}

document.addEventListener('mousedown', (e) => {
  if (pop && !pop.contains(e.target) && !(popAnchor && popAnchor.contains(e.target))) closePopover();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && pop) {
    const a = popAnchor;
    closePopover();
    if (a) a.focus();
  }
});
window.addEventListener('hashchange', closePopover);
// Bottom sheets survive resizes (the mobile keyboard resizes the viewport).
window.addEventListener('resize', () => {
  if (pop && !pop.classList.contains('sheet')) closePopover();
});

/** Opens the Watch editor for an item. data = {id,name,quality,price,max,note,watching} */
export function openWatchEditor(anchor, data) {
  const id = data.id;
  const watching = !!data.watching;
  const price = isNum(data.price) ? data.price : null;
  const html = `
    <div class="pop-head">
      <div class="pop-title">${watching ? 'Edit watch' : 'Watch item'}</div>
      <button type="button" class="pop-x" data-pop-close aria-label="Close">×</button>
    </div>
    <div class="pop-item ${qClass(data.quality)}" ${ttAttr(id)}>${esc(itemName(data.name, id))}</div>
    <div class="pop-sub">Current price ${money(price)}</div>
    <div class="pop-field">
      <span class="field-label">Alert when price ≤</span>
      ${moneyFields(isNum(data.max) ? data.max : null, { label: 'Max price', idp: 'wp' })}
      <div class="pop-quick">
        ${price !== null ? `<button type="button" class="chip chip-sm" data-fill="${price}">Current</button>
        <button type="button" class="chip chip-sm" data-fill="${Math.round(price * 0.9)}">−10%</button>
        <button type="button" class="chip chip-sm" data-fill="${Math.round(price * 0.75)}">−25%</button>` : ''}
        <button type="button" class="chip chip-sm" data-fill="">Any deal</button>
      </div>
      <p class="pop-hint">Leave empty to be alerted on any deal for this item (below market price or under vendor price).</p>
    </div>
    <label class="pop-field">
      <span class="field-label">Note <span class="muted">(optional)</span></span>
      <input type="text" class="text-in" data-note maxlength="120" value="${esc(data.note || '')}" placeholder="e.g. mats for Flask of the Titans">
    </label>
    <div class="pop-actions">
      ${watching ? '<button type="button" class="btn btn-sm btn-danger" data-pop-remove>Stop watching</button>' : ''}
      <span class="spacer"></span>
      <button type="button" class="btn btn-sm" data-pop-close>Cancel</button>
      <button type="button" class="btn btn-sm btn-gold" data-pop-save>${watching ? 'Save' : 'Watch'}</button>
    </div>`;

  openPopover(anchor, html, (el) => {
    el.addEventListener('click', async (e) => {
      const fill = e.target.closest('[data-fill]');
      if (fill) {
        setMoneyFields(el, fill.dataset.fill === '' ? null : Number(fill.dataset.fill));
        return;
      }
      if (e.target.closest('[data-pop-close]')) {
        closePopover();
        return;
      }
      const save = e.target.closest('[data-pop-save]');
      const remove = e.target.closest('[data-pop-remove]');
      if (!save && !remove) return;
      const btn = save || remove;
      btn.disabled = true;
      try {
        if (remove) {
          await api.removeWatch(id);
          syncWatchButtons(id, null);
          toast(`<strong>Stopped watching</strong> ${esc(itemName(data.name, id))}`, 'ok');
          emit('watch', { item_id: id, watch: null });
        } else {
          const max = readMoneyFields(el);
          const note = el.querySelector('[data-note]').value.trim();
          const body = { max_price: max, enabled: 1, note };
          await api.setWatch(id, body);
          syncWatchButtons(id, body);
          toast(`<strong>Watching</strong> ${esc(itemName(data.name, id))} – ${max === null ? 'alert on any deal' : `alert at ≤ ${money(max)}`}. <a href="#/watchlist">Watchlist →</a>`, 'ok');
          emit('watch', { item_id: id, watch: body });
        }
        closePopover();
      } catch (err) {
        btn.disabled = false;
        toast(`<strong>Could not save</strong><br>${esc(err.message)}`, 'err');
      }
    });
    el.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && e.target.matches('input')) {
        e.preventDefault();
        el.querySelector('[data-pop-save]').click();
      }
    });
  });
}

// ---------------------------------------------------------------- add to shopping list
export function addShopCtl(spellId, name) {
  return `<span class="addshop"><input class="qty-in" type="number" min="1" max="9999" step="1" value="1" inputmode="numeric" aria-label="Quantity of ${esc(name || 'recipe')}"><button type="button" class="btn btn-sm" data-addshop="${esc(spellId)}" data-name="${esc(name || '')}" title="Add to shopping list"><span aria-hidden="true">＋</span><span class="hide-sm">List</span></button></span>`;
}

async function addToShopping(btn) {
  const id = btn.dataset.addshop;
  const wrap = btn.closest('.addshop');
  const inp = wrap && wrap.querySelector('.qty-in');
  const add = Math.max(1, Math.round(Number(inp && inp.value) || 1));
  btn.disabled = true;
  try {
    const cur = await api.shopping(true);
    const row = ((cur && cur.recipes) || []).find((r) => String(r.spell_id) === String(id));
    const qty = (row ? row.qty : 0) + add;
    await api.setShopping(id, qty);
    btn.classList.add('done');
    setTimeout(() => btn.classList.remove('done'), 1200);
    toast(`<strong>Added ${add}×</strong> ${esc(btn.dataset.name || 'recipe')} to the shopping list${row ? ` (now ${qty})` : ''}. <a href="#/shopping">Open list →</a>`, 'ok');
    emit('shopping', { spell_id: id, qty });
  } catch (err) {
    toast(`<strong>Could not add</strong><br>${esc(err.message)}`, 'err');
  } finally {
    btn.disabled = false;
  }
}

// ---------------------------------------------------------------- events
export function emit(name, detail) {
  window.dispatchEvent(new CustomEvent(`ahtool:${name}`, { detail }));
}

export function installGlobalActions() {
  document.addEventListener('click', (e) => {
    const w = e.target.closest('[data-watch]');
    if (w) {
      e.preventDefault();
      if (pop && popAnchor === w) {
        closePopover();
        return;
      }
      openWatchEditor(w, {
        id: w.dataset.watch,
        name: w.dataset.name || null,
        quality: w.dataset.quality === '' ? null : Number(w.dataset.quality),
        price: w.dataset.price === '' ? null : Number(w.dataset.price),
        max: w.dataset.max === '' ? null : Number(w.dataset.max),
        note: w.dataset.note || '',
        watching: w.dataset.watching === '1',
      });
      return;
    }
    const a = e.target.closest('[data-addshop]');
    if (a) {
      e.preventDefault();
      addToShopping(a);
    }
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && e.target.matches('.addshop .qty-in')) {
      e.preventDefault();
      const b = e.target.closest('.addshop').querySelector('[data-addshop]');
      if (b) addToShopping(b);
    }
  });
}

// ---------------------------------------------------------------- alerts
export const ALERT_KINDS = {
  watch: { label: 'Watch', cls: 'b-watch', icon: '★' },
  vendor_flip: { label: 'Vendor flip', cls: 'b-vendor', icon: '◆' },
  my_reagent: { label: 'My reagent', cls: 'b-reagent', icon: '⚗' },
};

export function alertKindBadge(kind) {
  const d = ALERT_KINDS[kind] || { label: kind || 'Alert', cls: 'b-base', icon: '•' };
  return `<span class="badge ${d.cls}"><span aria-hidden="true">${d.icon}</span> ${esc(d.label)}</span>`;
}
