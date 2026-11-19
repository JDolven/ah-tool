// Market Report: one day at the auction house, summarised from AH Tool Logger full scans.
import { api } from '../api.js';
import { esc, money, moneyText, num, pct, isNum, itemName, qClass, ttAttr, dateLong, errorState, debounce } from '../util.js';
import { noScans } from '../market.js';

const FLAT = 0.005;
let offResize = null;

const p2 = (n) => String(n).padStart(2, '0');
const hm = (ts) => {
  const d = new Date(ts * 1000);
  return `${p2(d.getHours())}:${p2(d.getMinutes())}`;
};
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const dayHm = (ts) => {
  const d = new Date(ts * 1000);
  return `${d.getDate()} ${MONTHS[d.getMonth()]} ${hm(ts)}`;
};
const goldOnly = (c) => (isNum(c) ? `${num(Math.round(c / 10000))}g` : '–');
const plural = (n, one, many) => `${num(n)} ${n === 1 ? one : many}`;

function icon(r, size = '') {
  const img = r.icon_url ? `<img src="${esc(r.icon_url)}" alt="" loading="lazy" onerror="this.remove()">` : '';
  return `<span class="rp-ico ${size} ${qClass(r.quality)}" aria-hidden="true">${img}</span>`;
}

function itemCell(r, sub = '') {
  return `<div class="rp-item">${icon(r)}<div class="rp-item-text">
    <a class="item ${qClass(r.quality)}" href="#/item/${encodeURIComponent(r.item_id)}" ${ttAttr(r.item_id)}>${esc(itemName(r.name, r.item_id))}</a>
    ${sub ? `<div class="rp-sub">${sub}</div>` : ''}</div></div>`;
}

function changeHtml(v, { big = false } = {}) {
  if (!isNum(v)) return '<span class="muted">–</span>';
  if (Math.abs(v) < FLAT) return `<span class="rp-flat">no change</span>`;
  const up = v > 0;
  return `<span class="rp-chg ${up ? 'up' : 'down'} ${big ? 'big' : ''}"><span class="rp-arrow" aria-hidden="true">${up ? '▲' : '▼'}</span>${pct(Math.abs(v), Math.abs(v) < 0.1 ? 1 : 0)}</span>`;
}

// ---------------------------------------------------------------- listings chart (inline SVG, sized to its box)
function niceStep(range, target = 3) {
  const raw = range / target;
  const mag = 10 ** Math.floor(Math.log10(raw || 1));
  const n = raw / mag;
  return (n < 1.5 ? 1 : n < 3.5 ? 2 : n < 7.5 ? 5 : 10) * mag;
}

function listingsSvg(rep, width) {
  const pts = rep.listings;
  const W = Math.max(300, Math.round(width));
  const H = W < 560 ? 200 : 230;
  const padL = 44;
  const padR = 14;
  const padT = 34;
  const padB = 26;
  const x0 = rep.window.start;
  const x1 = rep.window.end;
  const vals = pts.map((p) => p.auctions);
  const lo = Math.min(...vals);
  const hi = Math.max(...vals);
  const step = niceStep(Math.max(hi - lo, hi * 0.1));
  const yMin = Math.max(0, Math.floor((lo - step * 0.5) / step) * step);
  const yMax = Math.ceil((hi + step * 0.3) / step) * step;
  const X = (ts) => padL + ((ts - x0) / (x1 - x0)) * (W - padL - padR);
  const Y = (v) => padT + (1 - (v - yMin) / (yMax - yMin || 1)) * (H - padT - padB);
  const yk = (v) => (v >= 1000 ? `${num(v / 1000, v % 1000 ? 1 : 0)}k` : num(v));

  let grid = '';
  for (let v = yMin; v <= yMax + 1e-9; v += step) {
    grid += `<line class="rp-grid" x1="${padL}" x2="${W - padR}" y1="${Y(v)}" y2="${Y(v)}"/><text class="rp-axis" x="${padL - 8}" y="${Y(v) + 4}" text-anchor="end">${yk(v)}</text>`;
  }
  // x ticks on whole hours, every 3 h (6 h on narrow screens)
  const every = (W < 560 ? 6 : 3) * 3600;
  const first = new Date(x0 * 1000);
  first.setMinutes(0, 0, 0);
  let t = first.getTime() / 1000 + 3600;
  while (new Date(t * 1000).getHours() % (every / 3600)) t += 3600;
  let xt = '';
  for (; t <= x1; t += every) {
    xt += `<text class="rp-axis" x="${X(t)}" y="${H - 6}" text-anchor="middle">${hm(t)}</text>`;
  }

  const line = pts.map((p, i) => `${i ? 'L' : 'M'}${X(p.ts).toFixed(1)},${Y(p.auctions).toFixed(1)}`).join(' ');
  const area = pts.length > 1 ? `${line} L${X(pts[pts.length - 1].ts).toFixed(1)},${Y(yMin)} L${X(pts[0].ts).toFixed(1)},${Y(yMin)} Z` : '';
  const dots = pts.map((p) => `<circle class="rp-dot" cx="${X(p.ts)}" cy="${Y(p.auctions)}" r="3"><title>${num(p.auctions)} listings at ${dayHm(p.ts)}</title></circle>`).join('');

  const label = (p, text, cls, below) => {
    const x = X(p.ts);
    const anchor = x < W * 0.22 ? 'start' : x > W * 0.78 ? 'end' : 'middle';
    const dx = anchor === 'start' ? -6 : anchor === 'end' ? 6 : 0;
    const y = below ? Y(p.auctions) + 20 : Y(p.auctions) - 11;
    return `<circle class="rp-mark ${cls}" cx="${x}" cy="${Y(p.auctions)}" r="5.5"/>
      <text class="rp-label" x="${x + dx}" y="${y}" text-anchor="${anchor}">${esc(text)}</text>`;
  };
  let marks = '';
  if (pts.length > 1) {
    marks += label(rep.peak, `${num(rep.peak.auctions)} at ${hm(rep.peak.ts)}, the day's high`, 'peak', false);
    if (rep.low.ts !== rep.peak.ts) marks += label(rep.low, `${num(rep.low.auctions)} at ${hm(rep.low.ts)}`, 'low', true);
    if (rep.latest.ts !== rep.peak.ts && rep.latest.ts !== rep.low.ts) {
      // below the line when it would collide with the peak label (or the top edge)
      const nearPeak = Math.abs(X(rep.latest.ts) - X(rep.peak.ts)) < 230 && Math.abs(Y(rep.latest.auctions) - Y(rep.peak.auctions)) < 30;
      const below = nearPeak || Y(rep.latest.auctions) < padT + 30;
      marks += label(rep.latest, `${num(rep.latest.auctions)} at ${hm(rep.latest.ts)}`, 'last', below);
    }
  }
  return `<svg class="rp-chart" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img"
      aria-label="Listings per full scan from ${dayHm(x0)} to ${dayHm(x1)}: high ${num(hi)}, low ${num(lo)}">
    <defs><linearGradient id="rp-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" class="rp-fill-a"/><stop offset="1" class="rp-fill-b"/></linearGradient></defs>
    ${grid}${xt}
    ${area ? `<path d="${area}" fill="url(#rp-fill)"/>` : ''}
    <path class="rp-line" d="${line}"/>${dots}${marks}
  </svg>`;
}

// ---------------------------------------------------------------- sections
function barList(rows, valueOf, mainHtml, subHtml, cls) {
  if (!rows.length) return '<p class="muted rp-none">No sales seen yet – needs two full scans less than 48 hours apart.</p>';
  const max = Math.max(...rows.map(valueOf), 1);
  return `<ol class="rp-bars ${cls}">${rows.map((r) => `<li>
      ${itemCell(r)}
      <span class="rp-bar" aria-hidden="true"><span style="width:${Math.max(3, (valueOf(r) / max) * 100).toFixed(1)}%"></span></span>
      <span class="rp-val"><strong>${mainHtml(r)}</strong><small>${subHtml(r)}</small></span>
    </li>`).join('')}</ol>`;
}

function moverList(rows, empty) {
  if (!rows.length) return `<p class="muted rp-none">${empty}</p>`;
  return `<ul class="rp-movers">${rows.map((r) => `<li>${itemCell(r, `${moneyText(r.before)} → ${moneyText(r.now)}`)}${changeHtml(r.pct, { big: true })}</li>`).join('')}</ul>`;
}

function summary(rep) {
  const parts = [];
  const g = rep.gone;
  if (g) parts.push(`${pct(g.pct)} of the listings were gone between ${hm(g.from_ts)} and ${hm(g.to_ts)}.`);
  const t = rep.totals;
  if (isNum(t.gold_day)) {
    let s = `An estimated <strong>${goldOnly(t.gold_day)}</strong> changed hands`;
    if (isNum(t.gold_change) && Math.abs(t.gold_change) >= FLAT) s += `, ${pct(Math.abs(t.gold_change))} ${t.gold_change > 0 ? 'more' : 'less'} than the day before`;
    parts.push(s + '.');
  }
  if (rep.price_items) parts.push(`Prices split: ${plural(rep.cheaper, 'item', 'items')} got cheaper, ${num(rep.pricier)} pricier.`);
  if (!parts.length) parts.push(`${num(rep.latest.auctions)} listings in the latest full scan.`);
  return parts.join(' ');
}

function facts(rep) {
  const out = [];
  const g = rep.gone;
  if (g) {
    const split = [g.sold && `${num(g.sold)} bought`, g.expired && `${num(g.expired)} expired`, g.relisted && `${num(g.relisted)} relisted`].filter(Boolean).join(', ');
    out.push(`<div class="rp-fact"><div class="rp-fact-big">${num(g.auctions)}</div><p>listings gone from ${hm(g.from_ts)} to ${hm(g.to_ts)}: ${pct(g.pct)} of the house${split ? ` (${split})` : ''}.</p></div>`);
  }
  const s = rep.swing;
  if (s) {
    out.push(`<div class="rp-fact"><div class="rp-fact-big">${icon(s, 'sm')}${num(s.ratio, 1)}×</div><p><a class="item ${qClass(s.quality)}" href="#/item/${s.item_id}" ${ttAttr(s.item_id)}>${esc(itemName(s.name, s.item_id))}</a> swung within the day: ${esc(moneyText(s.low))} at ${hm(s.low_ts)}, ${esc(moneyText(s.high))} at ${hm(s.high_ts)} (typical listing price).</p></div>`);
  }
  const d = rep.deal;
  if (d) {
    out.push(`<div class="rp-fact"><div class="rp-fact-big">${icon(d, 'sm')}${pct(d.discount)}</div><p>under market for <a class="item ${qClass(d.quality)}" href="#/item/${d.item_id}" ${ttAttr(d.item_id)}>${esc(itemName(d.name, d.item_id))}</a>: ${esc(moneyText(d.price))} against ${esc(moneyText(d.baseline))} (14-day median). <a href="#/deals">More deals</a></p></div>`);
  }
  const st = rep.totals.sell_through;
  if (isNum(st)) {
    out.push(`<div class="rp-fact"><div class="rp-fact-big">${pct(st)}</div><p>of the auctions that left the house before they could run out were bought; the rest expired.</p></div>`);
  }
  return out.length ? `<section class="rp-box rp-facts">${out.slice(0, 4).join('')}</section>` : '';
}

function footer(rep) {
  const w = rep.window;
  const hours = w.covered_s / 3600;
  const priceText = rep.price_source === 'scans'
    ? `Prices are listing prices: per item the typical (median) buyout per unit in each full scan, this day against the 24 hours before (${plural(w.prev_scans, 'scan', 'scans')}).`
    : 'There were no full scans the day before, so price changes use Auctionator’s daily lowest price for this day against the day before.';
  return `<footer class="rp-foot">
    <p>${plural(w.scans, 'full scan', 'full scans')} of the ${esc(rep.realm || '')} ${esc(rep.faction || '')} auction house, ${dayHm(w.first_ts)} to ${dayHm(w.end)}, taken with the AH Tool Logger addon.
    Sales and gold traded are estimates, not recorded sales: an auction that is gone before it could run out counts as bought (a cancel looks the same), and the totals are scaled from the ${num(hours, 1)} hours the scan pairs cover to a full day.
    ${priceText} Only items with 10+ listed in both periods count towards price moves and groups.</p>
    <p class="rp-gen">AH Tool · generated ${dayHm(rep.generated_ts)}</p>
  </footer>`;
}

function reportHtml(rep) {
  const t = rep.totals;
  const g = rep.gone;
  const tiles = [
    [num(rep.peak.auctions), `listings at the day’s peak, ${hm(rep.peak.ts)}`],
    g ? [pct(g.pct), `of listings gone between ${hm(g.from_ts)} and ${hm(g.to_ts)}`] : [num(rep.latest.auctions), `listings in the latest scan, ${hm(rep.latest.ts)}`],
    [goldOnly(t.gold_day), `changing hands a day in ${plural(t.items_sold, 'item', 'items')}, estimated`],
    [num(t.units_day), 'units sold a day, estimated'],
  ];
  return `<article class="rp">
    <div class="rp-top">
      <div><div class="rp-kicker">AH Tool · Daily</div><h2 class="rp-title">Market Report</h2></div>
      <div class="rp-meta">${esc([rep.realm, rep.faction].filter(Boolean).join(' · '))}<br>${dateLong(rep.date)}</div>
    </div>
    <p class="rp-summary">${summary(rep)}</p>
    ${rep.window.scans < 3 ? `<div class="callout rp-callout">Only ${plural(rep.window.scans, 'full scan', 'full scans')} in these 24 hours. Several full scans a day (one every 15 minutes is allowed) give a much better report.</div>` : ''}
    <div class="rp-tiles">${tiles.map(([v, l]) => `<div class="rp-tile"><div class="rp-tile-v">${v}</div><div class="rp-tile-l">${l}</div></div>`).join('')}</div>

    <section><h3 class="rp-h">Listings through the day</h3><div class="rp-chart-box" id="rp-chart"></div></section>

    <div class="rp-cols">
      <section><h3 class="rp-h">Total gold traded, 24h (est.)</h3><p class="rp-note">All gold spent on each item, not the price of one</p>
        ${barList(rep.top_gold, (r) => r.value_day ?? r.value, (r) => goldOnly(r.value_day ?? r.value), (r) => `${num(r.sold_day ?? r.sold)} sold, ${esc(moneyText(r.avg_unit))} each`, 'gold')}</section>
      <section><h3 class="rp-h">Most traded</h3><p class="rp-note">Units sold a day, estimated</p>
        ${barList(rep.top_units, (r) => r.sold_day ?? r.sold, (r) => `est. ${num(r.sold_day ?? r.sold)}`, (r) => `seen: ${num(r.sold)}`, 'units')}</section>
    </div>

    ${rep.most_listed.length ? `<section class="rp-box"><h3 class="rp-h">Most listed</h3><div class="rp-listed">${rep.most_listed.map((r) => itemCell(r, `<strong>${num(r.qty)}</strong> in ${plural(r.auctions, 'auction', 'auctions')}`)).join('')}</div></section>` : ''}

    <div class="rp-cols">
      <section><h3 class="rp-h">Up vs the day before</h3>${moverList(rep.up, 'Nothing got noticeably pricier.')}</section>
      <section><h3 class="rp-h">Down vs the day before</h3>${moverList(rep.down, 'Nothing got noticeably cheaper.')}</section>
    </div>

    ${rep.groups.length ? `<section><h3 class="rp-h">Group moves vs the day before</h3><div class="rp-box rp-groups">${rep.groups.map((x) => `<div class="rp-group">
      <div class="rp-group-l">${esc(x.label)}</div><div class="rp-group-v">${changeHtml(x.pct, { big: true })}</div>
      <div class="rp-sub">${num(x.cheaper)} cheaper, ${num(x.pricier)} pricier</div></div>`).join('')}</div></section>` : ''}

    ${facts(rep)}
    ${footer(rep)}
  </article>`;
}

// ---------------------------------------------------------------- page
export async function render(ctx) {
  const { el, query } = ctx;
  if (offResize) offResize();
  offResize = null;
  el.innerHTML = `
    <header class="page-head rp-head">
      <div><h1>Market Report</h1><p class="lead">A day at the auction house, summarised from your AH Tool Logger full scans.</p></div>
      <div class="rp-nav" id="rp-nav"></div>
    </header>
    <div id="rp-body"><section class="card"><span class="skel skel-lg" style="width:40%"></span><span class="skel" style="width:80%"></span><span class="skel" style="width:65%"></span></section></div>`;

  let rep;
  try {
    rep = await api.report(query.date || '');
  } catch (err) {
    if (ctx.alive()) el.querySelector('#rp-body').innerHTML = errorState(err);
    return;
  }
  if (!ctx.alive()) return;
  const body = el.querySelector('#rp-body');
  if (!rep.window) {
    body.innerHTML = `<section class="card">${noScans(rep.installed)}</section>`;
    return;
  }

  const dates = rep.dates || [];
  const i = dates.indexOf(rep.date);
  const older = i >= 0 ? dates[i + 1] : null;
  const newer = i > 0 ? dates[i - 1] : null;
  const go = (d) => { location.hash = d && d !== dates[0] ? `#/report?date=${d}` : '#/report'; };
  el.querySelector('#rp-nav').innerHTML = `
    <button class="btn btn-ghost" data-go="${esc(older || '')}" ${older ? '' : 'disabled'} aria-label="Previous day">‹</button>
    <label class="sr-only" for="rp-date">Report date</label>
    <select id="rp-date" class="rp-select">${dates.map((d) => `<option value="${esc(d)}" ${d === rep.date ? 'selected' : ''}>${dateLong(d)}</option>`).join('')}</select>
    <button class="btn btn-ghost" data-go="${esc(newer || '')}" ${newer ? '' : 'disabled'} aria-label="Next day">›</button>
    <button class="btn" id="rp-print" title="Print or save as PDF">Print</button>`;
  el.querySelectorAll('#rp-nav [data-go]').forEach((b) => b.addEventListener('click', () => b.dataset.go && go(b.dataset.go)));
  el.querySelector('#rp-date').addEventListener('change', (e) => go(e.target.value));
  el.querySelector('#rp-print').addEventListener('click', () => window.print());

  body.innerHTML = reportHtml(rep);
  const box = body.querySelector('#rp-chart');
  const draw = () => {
    if (!document.body.contains(box)) return;
    box.innerHTML = listingsSvg(rep, box.clientWidth || 800);
  };
  draw();
  const onResize = debounce(draw, 150);
  window.addEventListener('resize', onResize);
  window.addEventListener('beforeprint', draw);
  offResize = () => {
    window.removeEventListener('resize', onResize);
    window.removeEventListener('beforeprint', draw);
  };
}
