// Wowhead-style item tooltips. One delegated hover handler: any element with data-item-id gets a tooltip.
// Data: GET /api/tooltip/<id> (structured lines + AH section), cached client-side.
// Debug: ?tt=<id> pins a tooltip open (used for screenshots).
import { api } from './api.js';
import { esc, isNum, pct, itemName } from './util.js';

const SHOW_DELAY = 150;
const CACHE_MS = 5 * 60 * 1000;
const OFFSET = 16;

const cache = new Map(); // id -> { t, promise }
let el = null;
let timer = null;
let current = null; // { id, target }
let lastX = 0;
let lastY = 0;
let pinned = false;

function fetchTooltip(id) {
  const now = Date.now();
  const hit = cache.get(id);
  if (hit && now - hit.t < CACHE_MS) return hit.promise;
  const promise = api.tooltip(id).catch((err) => {
    cache.delete(id);
    throw err;
  });
  cache.set(id, { t: now, promise });
  return promise;
}

export function clearTooltipCache() {
  cache.clear();
}

function ensureEl() {
  if (el) return el;
  el = document.createElement('div');
  el.className = 'wtt';
  el.setAttribute('role', 'tooltip');
  el.hidden = true;
  document.body.appendChild(el);
  return el;
}

function coins(c) {
  if (!isNum(c)) return '<span class="wtt-dim">–</span>';
  c = Math.round(Math.abs(c));
  const g = Math.floor(c / 10000);
  const s = Math.floor((c % 10000) / 100);
  const cc = c % 100;
  const out = [];
  if (g) out.push(`<span class="wtt-g">${g.toLocaleString('en-GB')}</span>`);
  if (s) out.push(`<span class="wtt-s">${s}</span>`);
  if (cc || !out.length) out.push(`<span class="wtt-c">${cc}</span>`);
  return `<span class="wtt-money">${out.join(' ')}</span>`;
}

function qcls(q) {
  return `wtt-q${isNum(q) && q >= 0 && q <= 7 ? q : 1}`;
}

function reagentList(regs) {
  if (!regs || !regs.length) return '';
  return `<ul class="wtt-reagents">${regs
    .map((g) => `<li><span class="${qcls(g.quality)}">${esc(itemName(g.name, g.item_id))}</span> <span class="wtt-dim">×${esc(g.count)}</span>${isNum(g.unit_price) ? ` <span class="wtt-right">${coins(g.unit_price * g.count)}</span>` : ''}</li>`)
    .join('')}</ul>`;
}

function render(d) {
  const lines = (d.lines || [])
    .map((l) => {
      const style = l.style && /^(white|green|yellow|gray|red|q\d)$/.test(l.style) ? l.style : 'white';
      const left = isNum(l.money) ? `${esc(l.text)} ${coins(l.money)}` : esc(l.text);
      const right = l.right ? `<span class="wtt-r">${esc(l.right)}</span>` : '';
      return `<div class="wtt-line wtt-${style}${right ? ' wtt-split' : ''}"><span>${left || '&nbsp;'}</span>${right}</div>`;
    })
    .join('');
  const a = d.ah || {};
  const rows = [];
  rows.push(['Current price', isNum(a.price) ? coins(a.price) + (a.stale ? ' <span class="wtt-dim">(old)</span>' : '') : '<span class="wtt-dim">not listed</span>']);
  rows.push(['Market price', isNum(a.baseline) ? coins(a.baseline) : '<span class="wtt-dim">too little data</span>']);
  if (isNum(a.discount)) {
    const cls = a.discount >= 0.25 ? 'wtt-green' : a.discount < -0.05 ? 'wtt-red' : 'wtt-white';
    rows.push(['Discount', `<span class="${cls}">${a.discount >= 0 ? '−' : '+'}${pct(Math.abs(a.discount))}</span>`]);
  }
  if (isNum(a.available)) rows.push(['Available', `${a.available.toLocaleString('en-GB')}`]);
  let ahHtml = `<table class="wtt-ah">${rows.map(([k, v]) => `<tr><th>${k}</th><td>${v}</td></tr>`).join('')}</table>`;
  const used = a.used_in || {};
  if (used.count) {
    ahHtml += `<div class="wtt-line wtt-gold">Used in ${used.count} recipe${used.count === 1 ? '' : 's'}${used.professions && used.professions.length ? ` <span class="wtt-dim">(${esc(used.professions.join(', '))})</span>` : ''}</div>`;
  }
  const crafted = a.crafted_by || [];
  crafted.slice(0, 2).forEach((r, i) => {
    ahHtml += `<div class="wtt-line wtt-gold">Crafted by ${esc(r.profession)}${isNum(r.skill) && r.skill > 0 ? ` <span class="wtt-dim">(${r.skill})</span>` : ''}${isNum(r.cost) && r.cost_complete ? ` <span class="wtt-right wtt-dim">cost ${coins(r.cost / (r.num_created || 1))}</span>` : ''}</div>`;
    if (i === 0) ahHtml += reagentList(r.reagents);
  });
  if (crafted.length > 2) ahHtml += `<div class="wtt-line wtt-dim">+${crafted.length - 2} more recipe${crafted.length - 2 === 1 ? '' : 's'}</div>`;
  if (a.teaches) {
    const t = a.teaches;
    ahHtml += `<div class="wtt-line wtt-gold">Teaches ${t.created_item_id ? `<span class="${qcls(t.created_quality)}">${esc(itemName(t.created_name || t.name, t.created_item_id))}</span>` : esc(t.name)} <span class="wtt-dim">(${esc(t.profession)})</span></div>`;
    ahHtml += reagentList(t.reagents);
    if (isNum(t.profit)) ahHtml += `<div class="wtt-line ${t.profit >= 0 ? 'wtt-green' : 'wtt-red'}">Profit per craft ${t.profit < 0 ? '−' : ''}${coins(t.profit)}</div>`;
  }
  const custom = d.custom ? '<div class="wtt-line wtt-dim wtt-note">Custom Forever item – limited data</div>' : '';
  return `
    <div class="wtt-icon" style="background-image:url('${esc(d.icon_url || '')}')"></div>
    <div class="wtt-box">
      <div class="wtt-name ${qcls(d.quality)}">${esc(itemName(d.name, d.id))}</div>
      ${lines}
      ${custom}
      <div class="wtt-sep"></div>
      <div class="wtt-ahsec">${ahHtml}</div>
    </div>`;
}

function position(x, y) {
  if (!el || el.hidden) return;
  const vw = document.documentElement.clientWidth;
  const vh = document.documentElement.clientHeight;
  const r = el.getBoundingClientRect();
  let left = x + OFFSET;
  let top = y + OFFSET;
  if (left + r.width > vw - 8) left = Math.max(8, x - OFFSET - r.width);
  if (top + r.height > vh - 8) top = Math.max(8, Math.min(y - OFFSET - r.height, vh - 8 - r.height));
  el.style.transform = `translate(${Math.round(left)}px, ${Math.round(top)}px)`;
}

function hide() {
  clearTimeout(timer);
  timer = null;
  current = null;
  pinned = false;
  if (el) el.hidden = true;
}

async function show(id, target) {
  const box = ensureEl();
  const mine = (current = { id, target });
  let data;
  try {
    data = await fetchTooltip(id);
  } catch (e) {
    if (current !== mine) return;
    box.innerHTML = `<div class="wtt-box"><div class="wtt-line wtt-dim">No tooltip data for item #${esc(id)}</div></div>`;
    box.hidden = false;
    position(lastX, lastY);
    return;
  }
  if (current !== mine || !data) return;
  box.innerHTML = render(data);
  box.classList.toggle('wtt-noicon', !data.icon_url);
  box.hidden = false;
  position(lastX, lastY);
}

function idOf(node) {
  const t = node && node.closest ? node.closest('[data-item-id]') : null;
  if (!t) return null;
  const id = t.getAttribute('data-item-id');
  return /^\d+$/.test(id || '') ? { id, target: t } : null;
}

export function installTooltips() {
  const noHover = window.matchMedia && window.matchMedia('(hover: none)').matches;
  document.addEventListener('pointerover', (e) => {
    if (e.pointerType === 'touch' || noHover) return;
    const hit = idOf(e.target);
    if (!hit) return;
    if (current && current.target === hit.target) return;
    if (pinned) hide();
    clearTimeout(timer);
    lastX = e.clientX;
    lastY = e.clientY;
    if (el && !el.hidden) {
      // moving from one item to another: switch right away
      show(hit.id, hit.target);
      return;
    }
    current = { id: hit.id, target: hit.target, pending: true };
    fetchTooltip(hit.id).catch(() => {}); // prefetch during the delay
    timer = setTimeout(() => show(hit.id, hit.target), SHOW_DELAY);
  });
  document.addEventListener('pointerout', (e) => {
    if (!current || pinned) return;
    const t = current.target;
    if (t.contains(e.target) && !(e.relatedTarget && t.contains(e.relatedTarget))) hide();
  });
  document.addEventListener('pointermove', (e) => {
    if (pinned || !current || e.pointerType === 'touch') return;
    lastX = e.clientX;
    lastY = e.clientY;
    position(lastX, lastY);
  }, { passive: true });
  window.addEventListener('scroll', () => { if (!pinned) hide(); }, { passive: true, capture: true });
  window.addEventListener('hashchange', hide);
  document.addEventListener('pointerdown', () => { if (!pinned) hide(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') hide(); });
  window.addEventListener('blur', () => { if (!pinned) hide(); });

  // Debug: ?tt=<id> pins a tooltip (screenshots can't hover).
  const pin = new URLSearchParams(location.search).get('tt');
  if (pin && /^\d+$/.test(pin)) {
    setTimeout(() => {
      const target = document.querySelector(`[data-item-id="${pin}"]`);
      const r = target ? target.getBoundingClientRect() : null;
      lastX = r ? r.left + Math.min(r.width, 60) : 260;
      lastY = r ? r.top + r.height / 2 : 120;
      show(pin, target || document.body).then(() => {
        pinned = true;
        position(lastX, lastY);
      });
    }, 1200);
  }
}
