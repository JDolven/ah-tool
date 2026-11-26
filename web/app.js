// AH Tool – single-page app entry (ES module).
import { api, clearCache, MOCK, WEB, getRealm, setRealm } from './js/api.js';
import { esc, money, pct, itemName, qClass, relTime, dateLong, debounce, isNum, ttAttr } from './js/util.js';
import { installTooltips, clearTooltipCache } from './js/tooltip.js';
import { destroyAll, rerenderAll } from './js/charts.js';
import * as overview from './js/pages/overview.js';
import * as deals from './js/pages/deals.js';
import * as browse from './js/pages/browse.js';
import * as professions from './js/pages/professions.js';
import * as item from './js/pages/item.js';
import * as stats from './js/pages/stats.js';
import * as postings from './js/pages/postings.js';
import * as my from './js/pages/my.js';
import * as shopping from './js/pages/shopping.js';
import * as watchlist from './js/pages/watchlist.js';
import * as leveling from './js/pages/leveling.js';
import * as activity from './js/pages/activity.js';
import * as sales from './js/pages/sales.js';
import * as report from './js/pages/report.js';
import * as download from './js/pages/download.js';
import { toast, installGlobalActions, alertKindBadge, closePopover } from './js/ui.js';
import * as browserNotify from './js/notify.js';

const view = document.getElementById('view');

// ---------------------------------------------------------------- routing
const ROUTES = [
  { re: /^\/?$/, nav: 'overview', title: 'Overview', fn: overview.render },
  { re: /^\/deals\/?$/, nav: 'deals', title: 'Deals', fn: deals.render },
  { re: /^\/browse\/?$/, nav: 'browse', title: 'Browse', fn: browse.render },
  { re: /^\/professions\/?$/, nav: 'professions', title: 'Professions', fn: professions.renderList },
  { re: /^\/professions\/(\d+)\/?$/, nav: 'professions', title: 'Profession', fn: professions.renderDetail },
  { re: /^\/my\/?$/, nav: 'my', title: 'My Professions', fn: my.render },
  { re: /^\/leveling\/?$/, nav: 'leveling', title: 'Leveling Guide', fn: leveling.render },
  { re: /^\/shopping\/?$/, nav: 'shopping', title: 'Shopping List', fn: shopping.render },
  { re: /^\/watchlist\/?$/, nav: 'watchlist', title: 'Watchlist & Alerts', fn: watchlist.render },
  { re: /^\/item\/(\d+)\/?$/, nav: null, title: 'Item', fn: item.render },
  { re: /^\/market\/?$/, nav: 'market', title: 'Market Activity', fn: activity.render },
  { re: /^\/report\/?$/, nav: 'report', title: 'Market Report', fn: report.render },
  { re: /^\/sales\/?$/, nav: 'sales', title: 'My Sales', fn: sales.render },
  { re: /^\/stats\/?$/, nav: 'stats', title: 'Statistics', fn: stats.render },
  { re: /^\/postings\/?$/, nav: 'postings', title: 'My auctions', fn: postings.render },
  { re: /^\/download\/?$/, nav: 'download', title: 'Run it yourself', fn: download.render },
];

// Web version: pages built on your own trades do not exist there (that data never leaves your PC).
if (WEB) {
  for (const nav of ['sales', 'postings']) {
    document.querySelector(`.nav [data-nav="${nav}"]`)?.remove();
    const i = ROUTES.findIndex((r) => r.nav === nav);
    if (i >= 0) ROUTES.splice(i, 1);
  }
  const rb = document.getElementById('refresh-btn');
  if (rb) rb.title = 'Reload the latest prices from the server';
  const foot = document.querySelector('.foot');
  if (foot) foot.insertAdjacentHTML('beforeend', ' · Your professions, lists and alerts are saved in this browser only · <a href="#/download">Run it yourself</a>');
} else {
  // Downloads exist only on the web version (the local app is what they contain).
  document.querySelector('.nav [data-nav="download"]')?.remove();
  const i = ROUTES.findIndex((r) => r.nav === 'download');
  if (i >= 0) ROUTES.splice(i, 1);
}

let routeToken = 0;

// Page statistics (web version only, when the server added an Umami tracker with auto-track off). Hash routes are
// reported as paths – #/deals -> /deals – since the tracker drops the #part. The deferred script may still be
// loading on the first page, so wait for it briefly.
function trackPage(path) {
  if (!WEB) return;
  const url = path === '/' ? '/' : `/${path.replace(/^\/+|\/+$/g, '')}`;
  const send = (tries) => {
    const u = window.umami;
    if (u && typeof u.track === 'function') {
      try {
        u.track((p) => ({ ...p, url, title: document.title }));
      } catch (e) {
        /* statistics must never break the page */
      }
    } else if (tries > 0) {
      setTimeout(() => send(tries - 1), 500);
    }
  };
  send(10);
}

function parseHash() {
  let h = location.hash.replace(/^#/, '');
  if (!h) h = '/';
  const [path, qs] = h.split('?');
  const query = Object.fromEntries(new URLSearchParams(qs || ''));
  return { path, query };
}

async function route() {
  const { path, query } = parseHash();
  const token = ++routeToken;
  destroyAll();
  closeSearch();
  closeNav();
  closeBell();
  closePopover();

  const r = ROUTES.find((x) => x.re.test(path));
  document.querySelectorAll('[data-nav]').forEach((a) => a.classList.toggle('active', !!r && a.dataset.nav === r.nav));

  if (!r) {
    document.title = 'Page not found – AH Tool';
    view.innerHTML = `<div class="empty"><div class="empty-icon">?</div><div class="empty-title">Page not found</div><div class="empty-text"><a href="#/">Back to overview</a></div></div>`;
    return;
  }
  document.title = `${r.title} – AH Tool`;
  trackPage(path);
  const params = path.match(r.re).slice(1);
  view.classList.remove('fade-in');
  void view.offsetWidth;
  view.classList.add('fade-in');
  const ctx = { el: view, params, query, alive: () => token === routeToken };
  try {
    await r.fn(ctx);
  } catch (err) {
    console.error(err);
    if (ctx.alive()) {
      view.innerHTML = `<div class="empty error"><div class="empty-icon">⚠</div><div class="empty-title">Something went wrong</div><div class="empty-text">${esc(err && err.message)}</div><button class="btn" data-action="retry">Try again</button></div>`;
    }
  }
}

function rerender() {
  const y = window.scrollY;
  route().then(() => window.scrollTo(0, y));
}

window.addEventListener('hashchange', () => {
  route();
  window.scrollTo(0, 0);
});

view.addEventListener('click', (e) => {
  if (e.target.closest('[data-action="retry"]')) {
    clearCache();
    rerender();
  }
});

// ---------------------------------------------------------------- theme
const THEME_KEY = 'ahtool-theme';

function getStoredTheme() {
  try {
    return localStorage.getItem(THEME_KEY);
  } catch (e) {
    return null;
  }
}

function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  const btn = document.getElementById('theme-btn');
  if (btn) {
    btn.setAttribute('aria-label', t === 'dark' ? 'Switch to light theme' : 'Switch to dark theme');
    btn.title = btn.getAttribute('aria-label');
    btn.querySelector('.theme-icon').textContent = t === 'dark' ? '☀' : '☾';
  }
}

// ?theme=light|dark overrides the stored theme for this page load (handy for screenshots).
const themeParam = new URLSearchParams(location.search).get('theme');
applyTheme(themeParam === 'light' || themeParam === 'dark' ? themeParam : getStoredTheme() === 'light' ? 'light' : 'dark');

document.getElementById('theme-btn').addEventListener('click', () => {
  const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
  applyTheme(next);
  try {
    localStorage.setItem(THEME_KEY, next);
  } catch (e) {
    /* ignore */
  }
  // Let CSS variables apply before reading them for charts.
  requestAnimationFrame(() => rerenderAll());
});

// ---------------------------------------------------------------- mobile nav
const shell = document.querySelector('.shell');
function closeNav() {
  shell.classList.remove('nav-open');
}
document.getElementById('menu-btn').addEventListener('click', () => shell.classList.toggle('nav-open'));
document.getElementById('scrim').addEventListener('click', closeNav);

// ---------------------------------------------------------------- search
const sInput = document.getElementById('search');
const sBox = document.getElementById('search-results');
let sItems = [];
let sIndex = -1;
let sToken = 0;

function closeSearch() {
  sBox.hidden = true;
  sInput.setAttribute('aria-expanded', 'false');
  sIndex = -1;
}

function renderSearch() {
  if (!sItems.length) {
    sBox.innerHTML = '<div class="sr-empty">No results</div>';
  } else {
    sBox.innerHTML = sItems
      .map((it, i) => {
        const disc = isNum(it.discount) && it.discount >= 0.1 ? `<span class="sr-disc">−${pct(it.discount)}</span>` : '';
        return `<a class="sr-row ${i === sIndex ? 'sel' : ''}" href="#/item/${encodeURIComponent(it.id)}" ${ttAttr(it.id)} role="option" id="sr-${i}" aria-selected="${i === sIndex}">
          <span class="sr-name ${qClass(it.quality)}">${esc(itemName(it.name, it.id))}</span>
          <span class="sr-meta">${it.last_date ? dateLong(it.last_date) : 'never seen'}${isNum(it.available) ? ` · ${it.available} pcs` : ''}</span>
          <span class="sr-price">${money(it.price)} ${disc}</span>
        </a>`;
      })
      .join('');
  }
  sBox.hidden = false;
  sInput.setAttribute('aria-expanded', 'true');
  if (sIndex >= 0) sInput.setAttribute('aria-activedescendant', `sr-${sIndex}`);
  else sInput.removeAttribute('aria-activedescendant');
}

const doSearch = debounce(async () => {
  const q = sInput.value.trim();
  if (!q) {
    closeSearch();
    return;
  }
  const t = ++sToken;
  sBox.innerHTML = '<div class="sr-empty"><span class="spinner"></span> Searching…</div>';
  sBox.hidden = false;
  try {
    const res = await api.search(q, 12);
    if (t !== sToken) return;
    sItems = Array.isArray(res) ? res : [];
    sIndex = sItems.length ? 0 : -1;
    renderSearch();
  } catch (err) {
    if (t !== sToken) return;
    sBox.innerHTML = `<div class="sr-empty">Search failed: ${esc(err.message)}</div>`;
  }
}, 180);

sInput.addEventListener('input', doSearch);
sInput.addEventListener('focus', () => {
  if (sInput.value.trim() && sItems.length) renderSearch();
});
sInput.addEventListener('keydown', (e) => {
  if (e.key === 'ArrowDown' && sItems.length) {
    e.preventDefault();
    sIndex = (sIndex + 1) % sItems.length;
    renderSearch();
  } else if (e.key === 'ArrowUp' && sItems.length) {
    e.preventDefault();
    sIndex = (sIndex - 1 + sItems.length) % sItems.length;
    renderSearch();
  } else if (e.key === 'Enter') {
    const it = sItems[sIndex >= 0 ? sIndex : 0];
    if (it && !sBox.hidden) {
      location.hash = `#/item/${encodeURIComponent(it.id)}`;
      sInput.value = '';
      sInput.blur();
      closeSearch();
    } else if (/^\d+$/.test(sInput.value.trim())) {
      location.hash = `#/item/${sInput.value.trim()}`;
    }
  } else if (e.key === 'Escape') {
    closeSearch();
    sInput.blur();
  }
});
sBox.addEventListener('mousedown', (e) => {
  // keep focus so click registers before blur closes the box
  if (e.target.closest('.sr-row')) e.preventDefault();
});
sBox.addEventListener('click', (e) => {
  if (e.target.closest('.sr-row')) {
    sInput.value = '';
    closeSearch();
  }
});
document.addEventListener('click', (e) => {
  if (!e.target.closest('.search')) closeSearch();
});
document.addEventListener('keydown', (e) => {
  const tag = (document.activeElement && document.activeElement.tagName) || '';
  if (e.key === '/' && !/INPUT|TEXTAREA|SELECT/.test(tag)) {
    e.preventDefault();
    sInput.focus();
  }
});

// ---------------------------------------------------------------- status + refresh
let lastIngestTs = null;


function renderSideStatus(s) {
  const el = document.getElementById('side-status');
  if (!s) {
    el.innerHTML = '<span class="dot dot-err"></span> Cannot reach the server';
    return;
  }
  const realms = Array.isArray(s.realms) ? s.realms.filter((r) => typeof r === 'string' && r) : [];
  // a remembered realm the server no longer lists (renamed key, owner data gone): back to the default
  if (getRealm() && !realms.includes(getRealm())) setRealm('');
  const dot = `<span class="dot ${s.items_priced ? 'dot-ok' : 'dot-idle'}"></span>`;
  const realmHtml = realms.length > 1
    ? `<label class="ss-realm">${dot}<span class="sr-only">Realm</span><select id="realm-sel" class="ss-realm-sel" title="Show prices for this realm">${
      (realms.includes(s.realm) ? realms : [s.realm, ...realms]).filter(Boolean).map((r) => `<option value="${esc(r)}" ${r === s.realm ? 'selected' : ''}>${esc(r)}</option>`).join('')}</select></label>`
    : `<div class="ss-realm">${dot}${esc(s.realm || 'No realm')}</div>`;
  el.innerHTML = `
    ${realmHtml}
    <div class="ss-line">Scanned ${dateLong(s.latest_date)}</div>
    <div class="ss-line muted">${WEB ? `Updated ${relTime(s.last_push_ts)}` : `Imported ${relTime(s.last_ingest_ts)}`}</div>`;
}

document.getElementById('side-status').addEventListener('change', (e) => {
  if (e.target.id !== 'realm-sel') return;
  setRealm(e.target.value);
  clearTooltipCache(); // tooltips show the AH price of the realm they were fetched for
  lastIngestTs = null; // another realm has another last import: no "new data" toast
  pollStatus(true);
  rerender();
});

async function pollStatus(initial = false) {
  try {
    const s = await api.status(true);
    renderSideStatus(s);
    handleAlertCount(s, initial);
    if (!initial && lastIngestTs !== null && s.last_ingest_ts !== lastIngestTs) {
      toast('<strong>New data imported</strong> – refreshing the view', 'ok');
      clearCache();
      rerender();
    }
    lastIngestTs = s.last_ingest_ts;
  } catch (e) {
    renderSideStatus(null);
  }
}

const refreshBtn = document.getElementById('refresh-btn');
refreshBtn.addEventListener('click', async () => {
  if (refreshBtn.disabled) return;
  refreshBtn.disabled = true;
  refreshBtn.classList.add('busy');
  try {
    if (WEB) {
      clearCache();
      await pollStatus();
      toast('<strong>Reloaded</strong> – prices arrive from the AH Tool on a player\'s PC after each scan.', 'ok');
      rerender();
      return;
    }
    const res = await api.ingest();
    clearCache();
    const s = await api.status(true).catch(() => null);
    if (s) {
      lastIngestTs = s.last_ingest_ts;
      renderSideStatus(s);
      handleAlertCount(s, false);
    }
    const r = res || {};
    toast(
      r.skipped
        ? '<strong>No changes</strong> – the file has not changed since last time.'
        : `<strong>Updated</strong> – ${isNum(r.items) ? r.items : '?'} items, ${isNum(r.changed) ? r.changed : '?'} changed.`,
      'ok'
    );
    rerender();
  } catch (err) {
    toast(`<strong>Refresh failed</strong><br>${esc(err.message)}`, 'err');
  } finally {
    refreshBtn.disabled = false;
    refreshBtn.classList.remove('busy');
  }
});

// ---------------------------------------------------------------- alerts: bell, toasts, browser notifications
const bellBtn = document.getElementById('bell-btn');
const bellPanel = document.getElementById('bell-panel');
const bellCount = document.getElementById('bell-count');
let unseen = null; // last known unseen_alerts

function setUnseen(n) {
  unseen = n;
  const show = isNum(n) && n > 0;
  bellCount.hidden = !show;
  bellCount.textContent = n > 99 ? '99+' : String(n);
  bellBtn.classList.toggle('has-new', show);
  bellBtn.setAttribute('aria-label', show ? `Alerts – ${n} new` : 'Alerts');
  const nav = document.getElementById('nav-alerts');
  nav.hidden = !show;
  nav.textContent = n > 99 ? '99+' : String(n);
}

async function handleAlertCount(s, initial) {
  if (!s || !isNum(s.unseen_alerts)) return;
  const prev = unseen;
  setUnseen(s.unseen_alerts);
  if (initial || prev === null || s.unseen_alerts <= prev) return;
  const delta = s.unseen_alerts - prev;
  let fresh = [];
  try {
    fresh = ((await api.alerts(Math.min(20, Math.max(delta, 1)), true)) || []).filter((a) => !a.seen).slice(0, delta);
  } catch (e) {
    fresh = [];
  }
  const list = fresh.slice(0, 3).map((a) => `<div class="toast-alert">${esc(a.message || itemName(a.name, a.item_id))}</div>`).join('');
  toast(
    `<strong>★ ${delta} new alert${delta === 1 ? '' : 's'}</strong>${list}${delta > 3 ? `<div class="muted">+${delta - 3} more</div>` : ''}<a href="#/watchlist">View alerts →</a>`,
    'alert',
    8000
  );
  browserNotify.show(fresh.length ? fresh : [{ id: Date.now(), message: `${delta} new alert${delta === 1 ? '' : 's'}`, item_id: null }]);
  if (!bellPanel.hidden) openBell();
  window.dispatchEvent(new CustomEvent('ahtool:alerts', { detail: { count: s.unseen_alerts } }));
}

function closeBell() {
  bellPanel.hidden = true;
  bellBtn.setAttribute('aria-expanded', 'false');
}

async function openBell() {
  bellPanel.hidden = false;
  bellBtn.setAttribute('aria-expanded', 'true');
  bellPanel.innerHTML = '<div class="bell-head"><strong>Alerts</strong></div><div class="sr-empty"><span class="spinner"></span> Loading…</div>';
  let alerts = [];
  try {
    alerts = (await api.alerts(8, true)) || [];
  } catch (err) {
    bellPanel.innerHTML = `<div class="sr-empty">Could not load alerts: ${esc(err.message)}</div>`;
    return;
  }
  if (bellPanel.hidden) return;
  const anyNew = alerts.some((a) => !a.seen);
  bellPanel.innerHTML = `
    <div class="bell-head"><strong>Alerts</strong>${anyNew ? '<button type="button" class="btn btn-sm btn-ghost" id="bell-seen">Mark all seen</button>' : ''}</div>
    ${alerts.length
      ? `<div class="bell-list">${alerts
          .map((a) => `<a class="bell-row ${a.seen ? '' : 'unseen'}" href="#/item/${encodeURIComponent(a.item_id)}">
            <span class="bell-line"><span class="item ${qClass(a.quality)}" ${ttAttr(a.item_id)}>${esc(itemName(a.name, a.item_id))}</span>${alertKindBadge(a.kind)}</span>
            <span class="bell-msg">${esc(a.message || '')}</span>
            <span class="bell-meta">${money(a.price)} · ${relTime(a.ts)}</span>
          </a>`)
          .join('')}</div>`
      : '<div class="sr-empty">No alerts yet. Watch an item to get one.</div>'}
    <a class="bell-foot" href="#/watchlist">Watchlist &amp; all alerts →</a>`;
}

bellBtn.addEventListener('click', (e) => {
  e.stopPropagation();
  if (bellPanel.hidden) openBell();
  else closeBell();
});
bellPanel.addEventListener('click', async (e) => {
  if (e.target.closest('#bell-seen')) {
    e.preventDefault();
    try {
      await api.markAlertsSeen();
      setUnseen(0);
      window.dispatchEvent(new CustomEvent('ahtool:alerts', { detail: { count: 0 } }));
      openBell();
    } catch (err) {
      toast(`<strong>Failed</strong><br>${esc(err.message)}`, 'err');
    }
  } else if (e.target.closest('a')) closeBell();
});
document.addEventListener('click', (e) => {
  if (!bellPanel.hidden && !e.target.closest('.bell-wrap')) closeBell();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !bellPanel.hidden) {
    closeBell();
    bellBtn.focus();
  }
});
window.addEventListener('ahtool:alerts-seen', () => setUnseen(0));

// ---------------------------------------------------------------- shopping list count in nav
async function updateShopCount() {
  try {
    const sh = await api.shopping(true);
    const n = ((sh && sh.recipes) || []).length;
    const el = document.getElementById('nav-shop');
    el.hidden = !n;
    el.textContent = String(n);
  } catch (e) {
    /* ignore */
  }
}
window.addEventListener('ahtool:shopping', updateShopCount);

installGlobalActions();
installTooltips();
if (MOCK) document.getElementById('mock-flag').hidden = false;

pollStatus(true);
updateShopCount();
setInterval(() => pollStatus(false), 20000);
route();

