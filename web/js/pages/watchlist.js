import { api, WEB } from '../api.js';
import {
  esc, money, num, pct, itemLink, itemName, qClass, discountBar, relTime, tsFmt, debounce,
  errorState, skeletonRows, emptyState, isNum, dateLong, ttAttr,
} from '../util.js';
import { DataTable } from '../table.js';
import { toast, emit, moneyFields, readMoneyFields, openWatchEditor, alertKindBadge, closePopover } from '../ui.js';
import * as bn from '../notify.js';

export async function render(ctx) {
  const { el } = ctx;
  el.innerHTML = `
    <header class="page-head">
      <div>
        <h1>Watchlist &amp; Alerts</h1>
        <p class="lead">Watch items and get an alert when the price drops to your limit. ${WEB ? 'Alerts are checked when new prices arrive, while this page is open in this browser.' : 'Alerts are checked after every import – also while this page is closed, as long as the tool is running.'}</p>
      </div>
    </header>
    <section class="card filters">
      <div class="filter-row">
        <div class="field field-grow wl-add">
          <span class="field-label">Add an item</span>
          <div class="search wl-search">
            <span class="search-ico" aria-hidden="true">⌕</span>
            <input type="search" id="wl-q" placeholder="Search for item or ID…" autocomplete="off" spellcheck="false" aria-label="Find an item to watch" aria-controls="wl-results">
            <div class="search-results" id="wl-results" role="listbox" hidden></div>
          </div>
        </div>
      </div>
    </section>
    <div class="result-bar" id="wl-summary"></div>
    <section class="card card-flush" id="wl-table">${skeletonRows(5, 6)}</section>
    <div class="grid-2 grid-wide-left">
      <section class="card">
        <div class="card-head"><h2>Alert history</h2><button type="button" class="btn btn-sm" id="al-seen">Mark all seen</button></div>
        <div id="al-list">${skeletonRows(5, 3)}</div>
      </section>
      <section class="card" id="ns-card">
        <div class="card-head"><h2>Notifications</h2></div>
        <div id="ns-body">${skeletonRows(4, 2)}</div>
      </section>
    </div>`;

  const table = new DataTable(el.querySelector('#wl-table'), columns(), {
    sortKey: 'status',
    sortDir: 'desc',
    rowKey: (r) => r.item_id,
    rowClass: (r) => (r.triggered ? 'row-hit' : !r.enabled ? 'row-paused' : ''),
    empty: emptyState('You are not watching anything yet', 'Search above, or use the <strong>☆ Watch</strong> button on an item page or a deal.', '☆'),
  });

  let rows = [];
  const loadWatch = async () => {
    try {
      const res = await api.watchlist();
      if (!ctx.alive()) return;
      rows = Array.isArray(res) ? res : [];
      table.setRows(rows);
      const hit = rows.filter((r) => r.triggered).length;
      el.querySelector('#wl-summary').innerHTML = rows.length
        ? `<span><strong>${num(rows.length)}</strong> watched</span>${hit ? `<span class="up">★ ${num(hit)} at or below your price now</span>` : '<span class="muted">None at your price right now</span>'}`
        : '';
    } catch (err) {
      if (ctx.alive()) el.querySelector('#wl-table').innerHTML = errorState(err);
    }
  };

  const loadAlerts = async () => {
    try {
      const res = await api.alerts(50, true);
      if (!ctx.alive()) return;
      renderAlerts(el.querySelector('#al-list'), Array.isArray(res) ? res : []);
      el.querySelector('#al-seen').disabled = !(res || []).some((a) => !a.seen);
    } catch (err) {
      if (ctx.alive()) el.querySelector('#al-list').innerHTML = errorState(err);
    }
  };

  const loadSettings = async () => {
    try {
      const s = await api.settings();
      if (!ctx.alive()) return;
      renderSettings(el.querySelector('#ns-body'), s || {});
    } catch (err) {
      if (ctx.alive()) el.querySelector('#ns-body').innerHTML = errorState(err);
    }
  };

  // ---- row edits
  const saveRow = async (id, body, okMsg) => {
    try {
      await api.setWatch(id, body);
      emit('watch', { item_id: id });
      if (okMsg) toast(okMsg, 'ok');
    } catch (err) {
      toast(`<strong>Could not save</strong><br>${esc(err.message)}`, 'err');
    }
    if (ctx.alive()) loadWatch();
  };
  const pendingMax = new Map();
  el.querySelector('#wl-table').addEventListener('input', (e) => {
    const box = e.target.closest('.money-in[data-max-for]');
    if (!box) return;
    const id = box.dataset.maxFor;
    clearTimeout(pendingMax.get(id));
    pendingMax.set(id, setTimeout(() => {
      pendingMax.delete(id);
      const max = readMoneyFields(box);
      const r = rows.find((x) => String(x.item_id) === String(id));
      saveRow(id, { max_price: max }, `<strong>Saved</strong> ${esc(itemName(r && r.name, id))}: ${max === null ? 'any deal' : `≤ ${money(max)}`}`);
    }, 900));
  });
  el.querySelector('#wl-table').addEventListener('change', (e) => {
    const t = e.target.closest('input[data-enable]');
    if (t) saveRow(t.dataset.enable, { enabled: t.checked ? 1 : 0 });
  });
  el.querySelector('#wl-table').addEventListener('click', async (e) => {
    const rm = e.target.closest('[data-unwatch]');
    if (rm) {
      rm.disabled = true;
      const id = rm.dataset.unwatch;
      const r = rows.find((x) => String(x.item_id) === String(id));
      try {
        await api.removeWatch(id);
        emit('watch', { item_id: id, watch: null });
        toast(`<strong>Stopped watching</strong> ${esc(itemName(r && r.name, id))}`, 'ok');
      } catch (err) {
        toast(`<strong>Could not remove</strong><br>${esc(err.message)}`, 'err');
      }
      if (ctx.alive()) loadWatch();
    }
  });
  const onWatch = () => {
    if (ctx.alive()) loadWatch();
    else window.removeEventListener('ahtool:watch', onWatch);
  };
  window.addEventListener('ahtool:watch', onWatch);
  const onAlerts = () => {
    if (ctx.alive()) {
      loadAlerts();
      loadWatch();
    } else window.removeEventListener('ahtool:alerts', onAlerts);
  };
  window.addEventListener('ahtool:alerts', onAlerts);

  el.querySelector('#al-seen').addEventListener('click', async (e) => {
    e.currentTarget.disabled = true;
    try {
      await api.markAlertsSeen();
      emit('alerts-seen', {});
    } catch (err) {
      toast(`<strong>Failed</strong><br>${esc(err.message)}`, 'err');
    }
    loadAlerts();
  });

  wireSearch(el);
  wireSettings(el, ctx);

  await Promise.all([loadWatch(), loadAlerts(), loadSettings()]);
}

function statusRank(r) {
  if (!r.enabled) return 0;
  if (r.triggered) return 3;
  return isNum(r.price) ? 2 : 1;
}

function columns() {
  return [
    {
      key: 'name',
      label: 'Item',
      defaultDir: 'asc',
      sort: (r) => String(r.name || `Item #${r.item_id}`).toLowerCase(),
      render: (r) => `${itemLink(r.item_id, r.name, r.quality)}${r.note ? `<div class="cell-sub">${esc(r.note)}</div>` : ''}`,
    },
    {
      key: 'price',
      label: 'Current price',
      align: 'right',
      render: (r) => `${money(r.price)}${isNum(r.available) ? `<div class="cell-sub">${num(r.available)} listed</div>` : ''}`,
    },
    { key: 'baseline', label: 'Market price', align: 'right', hideSm: true, render: (r) => money(r.baseline) },
    { key: 'discount', label: 'Discount', hideSm: true, hideMd: true, render: (r) => discountBar(r.discount) },
    {
      key: 'max_price',
      label: 'Alert at ≤',
      sort: (r) => (isNum(r.max_price) ? r.max_price : null),
      render: (r) => `<div class="max-cell">${moneyFields(r.max_price, { label: `Max price for ${itemName(r.name, r.item_id)}`, idp: `mx${r.item_id}` }).replace('class="money-in"', `class="money-in money-in-sm" data-max-for="${esc(r.item_id)}"`)}
        ${!isNum(r.max_price) ? '<div class="cell-sub">empty = any deal</div>' : ''}</div>`,
    },
    {
      key: 'status',
      label: 'Status',
      sort: statusRank,
      render: (r) => {
        if (!r.enabled) return '<span class="badge b-muted">Paused</span>';
        if (r.triggered) return '<span class="badge b-hit">★ Triggered</span>';
        if (!isNum(r.price)) return '<span class="badge b-muted" title="Not on the AH in the latest scan">Not listed</span>';
        return '<span class="badge b-wait">Waiting</span>';
      },
    },
    {
      key: 'enabled',
      label: 'On',
      align: 'center',
      render: (r) => `<label class="toggle toggle-sm" title="${r.enabled ? 'Alerts on' : 'Alerts paused'}"><input type="checkbox" data-enable="${esc(r.item_id)}" ${r.enabled ? 'checked' : ''} aria-label="Alerts for ${esc(itemName(r.name, r.item_id))}"><span class="toggle-ui"></span></label>`,
    },
    {
      key: 'act',
      label: '',
      sortable: false,
      align: 'right',
      render: (r) => `<span class="row-acts"><button type="button" class="icon-btn icon-btn-sm" data-watch="${esc(r.item_id)}" data-name="${esc(r.name || '')}" data-quality="${isNum(r.quality) ? r.quality : ''}" data-price="${isNum(r.price) ? r.price : ''}" data-max="${isNum(r.max_price) ? r.max_price : ''}" data-note="${esc(r.note || '')}" data-watching="1" title="Edit watch" aria-label="Edit watch for ${esc(itemName(r.name, r.item_id))}"><span class="wb-ico" aria-hidden="true">✎</span><span class="wb-label sr-only">Edit</span></button><button type="button" class="icon-btn icon-btn-sm" data-unwatch="${esc(r.item_id)}" title="Stop watching" aria-label="Stop watching ${esc(itemName(r.name, r.item_id))}">×</button></span>`,
    },
  ];
}

/** threshold = max price (watch), minimum profit (vendor_flip) or market price/baseline (my_reagent). */
export function thresholdLabel(kind) {
  if (kind === 'vendor_flip') return 'min profit';
  if (kind === 'my_reagent') return 'market';
  return 'limit';
}

function renderAlerts(box, alerts) {
  if (!alerts.length) {
    box.innerHTML = emptyState('No alerts yet', 'When a watched item hits your price after an import, it shows up here.', '♪');
    return;
  }
  let lastDay = '';
  box.innerHTML = `<ul class="list alert-list">${alerts
    .map((a) => {
      const d = new Date((a.ts || 0) * 1000);
      const day = isNum(a.ts) ? `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}` : '';
      const sep = day !== lastDay ? `<li class="list-day">${esc(dateLong(day))}</li>` : '';
      lastDay = day;
      return `${sep}<li class="list-row alert-row ${a.seen ? '' : 'unseen'}">
        <div class="list-main">
          <div class="alert-line">${a.seen ? '' : '<span class="dot dot-new" title="New"></span>'}${itemLink(a.item_id, a.name, a.quality)} ${alertKindBadge(a.kind)}</div>
          <div class="list-meta">${esc(a.message || '')}</div>
        </div>
        <div class="list-side">
          <div>${money(a.price)}</div>
          <div class="list-sub" title="${esc(tsFmt(a.ts))}">${isNum(a.threshold) ? `${thresholdLabel(a.kind)} ${money(a.threshold)} · ` : ''}${relTime(a.ts)}</div>
        </div>
      </li>`;
    })
    .join('')}</ul>`;
}

function renderSettings(box, s) {
  const markup = isNum(s.shopping_max_markup) ? Math.round(s.shopping_max_markup * 100) : 15;
  const bs = bn.state();
  box.innerHTML = `
    <div class="set-list">
      ${WEB ? `<div class="set-row">
        <div class="set-text"><div class="set-title">Saved in this browser</div><div class="set-desc">Your watchlist, alerts and these settings are stored only in this browser, not on the server. Alerts are checked while this page is open, when new prices arrive.</div></div>
      </div>` : `<div class="set-row">
        <div class="set-text"><div class="set-title">Windows notifications</div><div class="set-desc">A Windows toast when an alert fires after an import – even with the browser closed.</div></div>
        <label class="toggle"><input type="checkbox" data-set="notify_windows" ${s.notify_windows !== false ? 'checked' : ''} aria-label="Windows notifications"><span class="toggle-ui"></span></label>
      </div>`}
      <div class="set-row">
        <div class="set-text"><div class="set-title">Browser notifications</div><div class="set-desc" id="bn-desc">${esc(bn.describe(bs))}</div></div>
        <button type="button" class="btn btn-sm" id="bn-btn" ${bs === 'unsupported' || bs === 'denied' ? 'disabled' : ''}>${bs === 'on' ? 'Turn off' : 'Enable'}</button>
      </div>
      <div class="set-row">
        <div class="set-text"><div class="set-title">Cheap reagents for my crafts</div><div class="set-desc">Alert when a reagent of a recipe you can craft is a deal (below market price). <a href="#/my">My Professions →</a></div></div>
        <label class="toggle"><input type="checkbox" data-set="notify_my_reagents" ${s.notify_my_reagents ? 'checked' : ''} aria-label="Cheap reagent alerts"><span class="toggle-ui"></span></label>
      </div>
      <div class="set-row set-row-wrap">
        <div class="set-text"><div class="set-title">Vendor flips</div><div class="set-desc">Alert on items listed under vendor price with at least this profit per unit. Empty or 0 = off.</div></div>
        ${moneyFields(s.notify_vendor_flip_min > 0 ? s.notify_vendor_flip_min : null, { label: 'Minimum vendor profit', idp: 'vf' }).replace('class="money-in"', 'class="money-in money-in-sm" id="vf-box"')}
      </div>
      <div class="set-row">
        <div class="set-text"><div class="set-title">Shopping list max price markup</div><div class="set-desc">Max price in the Auctionator export = the higher of current and market price, plus this.</div></div>
        <label class="pct-in"><input type="number" min="0" max="200" step="1" value="${markup}" data-markup aria-label="Markup in percent"><i>%</i></label>
      </div>
    </div>
    <div class="set-foot">
      <button type="button" class="btn" id="ns-test"><span aria-hidden="true">♪</span> Send test notification</button>
      <span class="card-note" id="ns-saved" aria-live="polite"></span>
    </div>`;
}

function wireSettings(el, ctx) {
  const body = el.querySelector('#ns-body');
  const saved = (msg = 'Saved ✓') => {
    const n = el.querySelector('#ns-saved');
    if (!n) return;
    n.textContent = msg;
    clearTimeout(n._t);
    n._t = setTimeout(() => (n.textContent = ''), 2200);
  };
  const save = async (partial) => {
    try {
      await api.setSettings(partial);
      saved();
    } catch (err) {
      toast(`<strong>Could not save setting</strong><br>${esc(err.message)}`, 'err');
    }
  };
  body.addEventListener('change', (e) => {
    const t = e.target.closest('input[data-set]');
    if (t) save({ [t.dataset.set]: t.checked });
    const mk = e.target.closest('input[data-markup]');
    if (mk) {
      const v = Math.max(0, Math.min(200, Number(mk.value) || 0));
      mk.value = v;
      save({ shopping_max_markup: Math.round(v) / 100 });
    }
  });
  body.addEventListener('input', debounce((e) => {
    if (e.target.closest('#vf-box')) save({ notify_vendor_flip_min: readMoneyFields(el.querySelector('#vf-box')) || 0 });
  }, 800));
  body.addEventListener('click', async (e) => {
    if (e.target.closest('#bn-btn')) {
      const before = bn.state();
      const after = before === 'on' ? bn.disable() : await bn.enable();
      if (!ctx.alive()) return;
      el.querySelector('#bn-desc').textContent = bn.describe(after);
      const b = el.querySelector('#bn-btn');
      b.textContent = after === 'on' ? 'Turn off' : 'Enable';
      b.disabled = after === 'unsupported' || after === 'denied';
      if (after === 'on' && before !== 'on') bn.test();
      emit('browser-notify', { state: after });
      return;
    }
    const test = e.target.closest('#ns-test');
    if (test) {
      test.disabled = true;
      try {
        if (WEB) {
          if (!bn.test()) throw new Error('Enable browser notifications first.');
          toast('<strong>Test notification shown</strong>', 'ok');
          return;
        }
        await api.notifyTest();
        const shown = bn.test();
        toast(`<strong>Test notification sent</strong> – check the Windows notification area${shown ? ' (and the browser notification)' : ''}.`, 'ok');
      } catch (err) {
        toast(`<strong>Test failed</strong><br>${esc(err.message)}`, 'err');
      } finally {
        test.disabled = false;
      }
    }
  });
}

function wireSearch(el) {
  const inp = el.querySelector('#wl-q');
  const box = el.querySelector('#wl-results');
  let items = [];
  let idx = -1;
  let tok = 0;
  const close = () => {
    box.hidden = true;
    idx = -1;
  };
  const draw = () => {
    box.innerHTML = items.length
      ? items
          .map((it, i) => `<button type="button" class="sr-row ${i === idx ? 'sel' : ''}" data-i="${i}" role="option" aria-selected="${i === idx}">
            <span class="sr-name ${qClass(it.quality)}" ${ttAttr(it.id)}>${esc(itemName(it.name, it.id))}</span>
            <span class="sr-meta">ID ${esc(it.id)}${isNum(it.available) ? ` · ${num(it.available)} listed` : ''}</span>
            <span class="sr-price">${money(it.price)}${isNum(it.discount) && it.discount >= 0.1 ? `<span class="sr-disc">−${pct(it.discount)}</span>` : ''}</span>
          </button>`)
          .join('')
      : '<div class="sr-empty">No results</div>';
    box.hidden = false;
  };
  const pick = (it) => {
    close();
    openWatchEditor(inp, { id: it.id, name: it.name, quality: it.quality, price: it.price, max: null, note: '', watching: false });
    inp.value = '';
  };
  inp.addEventListener('input', debounce(async () => {
    const q = inp.value.trim();
    if (!q) return close();
    const t = ++tok;
    try {
      const res = await api.search(q, 8);
      if (t !== tok) return;
      items = Array.isArray(res) ? res : [];
      idx = items.length ? 0 : -1;
      draw();
    } catch (err) {
      box.innerHTML = `<div class="sr-empty">Search failed: ${esc(err.message)}</div>`;
      box.hidden = false;
    }
  }, 180));
  inp.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown' && items.length) {
      e.preventDefault();
      idx = (idx + 1) % items.length;
      draw();
    } else if (e.key === 'ArrowUp' && items.length) {
      e.preventDefault();
      idx = (idx - 1 + items.length) % items.length;
      draw();
    } else if (e.key === 'Enter' && !box.hidden && items[idx >= 0 ? idx : 0]) {
      e.preventDefault();
      pick(items[idx >= 0 ? idx : 0]);
    } else if (e.key === 'Escape') close();
  });
  box.addEventListener('mousedown', (e) => e.preventDefault());
  box.addEventListener('click', (e) => {
    const b = e.target.closest('[data-i]');
    if (b) pick(items[Number(b.dataset.i)]);
  });
  inp.addEventListener('blur', () => setTimeout(close, 150));
  inp.addEventListener('focus', () => closePopover());
}
