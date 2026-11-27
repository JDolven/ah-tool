// Shared helpers: escaping, formatting, small UI components.

// Text typed or pasted into a filter box, as matched against item names: a chat item link becomes its item id and
// the brackets of a copied [Item Name] are dropped (same rule as analytics.search_text on the server).
export function searchText(v) {
  const s = String(v ?? '');
  const m = s.match(/\|Hitem:(\d{1,10})[:|]/);
  if (m) return m[1];
  return s.replace(/[[\]]/g, ' ').split(/\s+/).filter(Boolean).join(' ').toLowerCase();
}

export function esc(v) {
  if (v === null || v === undefined) return '';
  return String(v)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

export function isNum(v) {
  return typeof v === 'number' && isFinite(v);
}

const nf0 = new Intl.NumberFormat('en-GB', { maximumFractionDigits: 0 });
const nf1 = new Intl.NumberFormat('en-GB', { maximumFractionDigits: 1 });

export function num(v, digits = 0) {
  if (!isNum(v)) return '–';
  return digits ? nf1.format(v) : nf0.format(v);
}

/** Fraction (0.25) -> "25%" */
export function pct(v, digits = 0, signed = false) {
  if (!isNum(v)) return '–';
  const n = v * 100;
  const s = n.toFixed(digits).replace('-', '−');
  return (signed && n > 0 ? '+' : '') + s + '%';
}

/** Already-percent value (12.5) -> "+12.5%" */
export function pctRaw(v, digits = 1, signed = true) {
  if (!isNum(v)) return '–';
  return pct(v / 100, digits, signed);
}

/** Copper -> HTML with colored g/s/c units. Leading zero units are omitted. */
export function money(copper, opts = {}) {
  if (!isNum(copper)) return '<span class="money muted">–</span>';
  const neg = copper < 0;
  let c = Math.round(Math.abs(copper));
  const g = Math.floor(c / 10000);
  const s = Math.floor((c % 10000) / 100);
  c = c % 100;
  const parts = [];
  if (g > 0) parts.push(`<span class="mg">${nf0.format(g)}<i>g</i></span>`);
  if (g > 0 || s > 0) {
    if (!(opts.compact && g >= 100)) parts.push(`<span class="ms">${g > 0 ? String(s).padStart(2, '0') : s}<i>s</i></span>`);
  }
  if (!(opts.compact && (g > 0))) {
    parts.push(`<span class="mc">${(g > 0 || s > 0) ? String(c).padStart(2, '0') : c}<i>c</i></span>`);
  }
  const cls = 'money' + (neg ? ' neg' : '') + (opts.signed && !neg && copper > 0 ? ' pos' : '');
  const sign = neg ? '−' : (opts.signed && copper > 0 ? '+' : '');
  return `<span class="${cls}">${sign}${parts.join(' ')}</span>`;
}

/** Plain-text money for chart tooltips etc. */
export function moneyText(copper) {
  if (!isNum(copper)) return '–';
  const neg = copper < 0;
  let c = Math.round(Math.abs(copper));
  const g = Math.floor(c / 10000);
  const s = Math.floor((c % 10000) / 100);
  c = c % 100;
  const out = [];
  if (g) out.push(nf0.format(g) + 'g');
  if (g || s) out.push(s + 's');
  out.push(c + 'c');
  return (neg ? '−' : '') + out.join(' ');
}

/** Compact gold for axis ticks: 1234567c -> "123g" */
export function goldTick(copper) {
  if (!isNum(copper)) return '';
  const g = copper / 10000;
  if (Math.abs(g) >= 1000) return nf1.format(g / 1000) + 'k g';
  if (Math.abs(g) >= 1) return nf1.format(g) + 'g';
  const s = copper / 100;
  if (Math.abs(s) >= 1) return nf1.format(s) + 's';
  return Math.round(copper) + 'c';
}

export const QUALITY_NAMES = ['Poor', 'Common', 'Uncommon', 'Rare', 'Epic', 'Legendary', 'Artifact', 'Heirloom'];
export const QUALITY_COLORS = ['#9d9d9d', '#ffffff', '#1eff00', '#0070dd', '#a335ee', '#ff8000', '#e6cc80', '#00ccff'];

export function itemName(name, id) {
  return name ? name : `Item #${id}`;
}

export function qClass(q) {
  return isNum(q) ? `q${q}` : 'q1';
}

/** Attribute that makes any element show the item tooltip on hover (see js/tooltip.js). */
export function ttAttr(id) {
  return id === null || id === undefined || id === '' ? '' : `data-item-id="${esc(id)}"`;
}

/** Icon URL from the server, or '' unless it is a plain https URL (no quotes/parens/spaces). */
export function safeIconUrl(url) {
  return typeof url === 'string' && /^https:\/\/[A-Za-z0-9.-]+\/[A-Za-z0-9_\-./%]+$/.test(url) ? url : '';
}

export function itemLink(id, name, quality, extra = '') {
  return `<a class="item ${qClass(quality)}" href="#/item/${encodeURIComponent(id)}" ${ttAttr(id)} ${extra}>${esc(itemName(name, id))}</a>`;
}

export function wowheadLink(id, label = 'Wowhead') {
  return `<a class="ext" href="https://www.wowhead.com/classic/item=${encodeURIComponent(id)}" target="_blank" rel="noopener">${esc(label)} ↗</a>`;
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** "2026-11-16" -> "16 Nov" */
export function dateShort(iso) {
  if (!iso) return '–';
  const [y, m, d] = String(iso).split('-').map(Number);
  if (!m) return esc(iso);
  return `${d} ${MONTHS[m - 1]}`;
}

export function dateLong(iso) {
  if (!iso) return '–';
  const [y, m, d] = String(iso).split('-').map(Number);
  if (!m) return esc(iso);
  return `${d} ${MONTHS[m - 1]} ${y}`;
}

/** unix seconds -> "16.11.2026 14:32" */
export function tsFmt(ts) {
  if (!isNum(ts)) return '–';
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getDate())}.${p(d.getMonth() + 1)}.${d.getFullYear()} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function relTime(ts) {
  if (!isNum(ts)) return 'never';
  const diff = Date.now() / 1000 - ts;
  if (diff < 0) return 'now';
  if (diff < 60) return 'just now';
  if (diff < 3600) return `${Math.round(diff / 60)} min ago`;
  if (diff < 86400) {
    const h = Math.round(diff / 3600);
    return `${h} ${h === 1 ? 'hour' : 'hours'} ago`;
  }
  const d = Math.round(diff / 86400);
  return `${d} ${d === 1 ? 'day' : 'days'} ago`;
}

export function debounce(fn, ms) {
  let t = null;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

export const REASONS = {
  under_vendor: { label: 'Under vendor price', cls: 'b-vendor', icon: '◆' },
  below_baseline: { label: 'Below market', cls: 'b-base', icon: '▼' },
  cheap_reagent: { label: 'Cheap reagent', cls: 'b-reagent', icon: '⚗' },
};

export function reasonBadge(r, title = '') {
  const def = REASONS[r] || { label: r, cls: '', icon: '' };
  return `<span class="badge ${def.cls}" ${title ? `title="${esc(title)}"` : ''}><span aria-hidden="true">${def.icon}</span> ${esc(def.label)}</span>`;
}

/** Horizontal discount bar (0..1). */
export function discountBar(d) {
  if (!isNum(d)) return '<span class="muted">–</span>';
  const w = Math.max(0, Math.min(1, d)) * 100;
  const cls = d >= 0.5 ? 'hot' : d >= 0.3 ? 'warm' : '';
  return `<div class="dbar ${cls}" title="${pct(d, 1)} below market"><div class="dbar-track"><div class="dbar-fill" style="width:${w.toFixed(1)}%"></div></div><span class="dbar-val">${pct(d)}</span></div>`;
}

export function diffClass(v) {
  if (!isNum(v)) return 'muted';
  return v > 0 ? 'up' : v < 0 ? 'down' : 'muted';
}

export function emptyState(title, text = '', icon = '◇') {
  return `<div class="empty"><div class="empty-icon" aria-hidden="true">${icon}</div><div class="empty-title">${esc(title)}</div>${text ? `<div class="empty-text">${text}</div>` : ''}</div>`;
}

export const NO_DATA_TEXT = 'No data yet – run a scan in Auctionator and /reload';

export function noData() {
  return emptyState('No data yet', 'Run a scan in Auctionator and type <code>/reload</code> in game (or log out). The tool reads the file automatically.', '⌛');
}

export function errorState(err) {
  const msg = err && err.message ? err.message : String(err);
  return `<div class="empty error"><div class="empty-icon" aria-hidden="true">⚠</div><div class="empty-title">Could not load data</div><div class="empty-text">${esc(msg)}</div><button class="btn" data-action="retry">Try again</button></div>`;
}

export function skeletonRows(n = 6, cols = 5) {
  let out = '<div class="skel-table">';
  for (let i = 0; i < n; i++) {
    out += '<div class="skel-row">';
    for (let j = 0; j < cols; j++) out += `<span class="skel" style="width:${40 + ((i * 7 + j * 13) % 50)}%"></span>`;
    out += '</div>';
  }
  return out + '</div>';
}

export function skeletonTiles(n = 4) {
  let out = '<div class="tiles">';
  for (let i = 0; i < n; i++) out += '<div class="tile"><span class="skel" style="width:50%"></span><span class="skel skel-lg" style="width:70%"></span></div>';
  return out + '</div>';
}

export function tile(label, valueHtml, sub = '', cls = '') {
  return `<div class="tile ${cls}"><div class="tile-label">${esc(label)}</div><div class="tile-value">${valueHtml}</div>${sub ? `<div class="tile-sub">${sub}</div>` : ''}</div>`;
}

/** Skill range: orange (min), yellow (low), green (mid), grey (high). */
export function skillRange(skill) {
  if (!skill) return '<span class="muted">–</span>';
  const { min, low, high } = skill;
  const green = isNum(low) && isNum(high) ? Math.round((low + high) / 2) : null;
  const parts = [
    [min, 'sk-o', 'Orange (trainer)'],
    [low, 'sk-y', 'Yellow'],
    [green, 'sk-g', 'Green'],
    [high, 'sk-x', 'Grey'],
  ];
  return `<span class="skill">${parts.map(([v, c, t]) => `<span class="${c}" title="${t}: ${isNum(v) ? v : '–'}">${isNum(v) ? v : '–'}</span>`).join('')}</span>`;
}

export function sourceLabel(src) {
  if (src === 'ah') return '<span class="src src-ah">AH</span>';
  if (src === 'vendor') return '<span class="src src-vendor">Vendor</span>';
  if (src === 'craft') return '<span class="src src-craft" title="Cheaper to craft yourself than to buy">Craft</span>';
  return '<span class="src src-none">No price</span>';
}

// ---------------------------------------------------------------- v4: price confidence, thin markets, profit trend

export const CONF_TEXT = {
  high: 'High confidence – plenty of history and volume',
  medium: 'Medium confidence – limited history or few listings',
  low: 'Low confidence – very little data or a thin market',
};

/** Confidence badge: high = nothing (unless showHigh), medium = amber dot, low = grey "low data" pill. */
export function confBadge(conf, { showHigh = false } = {}) {
  if (conf === 'medium') return `&#8288;<span class="conf conf-med" title="${CONF_TEXT.medium}" role="img" aria-label="Medium confidence"></span>`;
  if (conf === 'low') return `<span class="conf conf-low" title="${CONF_TEXT.low}">low data</span>`;
  if (conf === 'high' && showHigh) return `<span class="conf conf-high" title="${CONF_TEXT.high}">✓ reliable</span>`;
  return '';
}

export const PRICE_FLAGS = {
  thin_high: 'Single overpriced listing – market price used',
  thin_low: 'Very few listed – may be a mistake or a snipe',
};

/** "Thin market" chip for price_flag thin_high / thin_low. long=true shows the explanation inline. */
export function flagChip(flag, { long = false } = {}) {
  if (!flag || !PRICE_FLAGS[flag]) return '';
  const icon = flag === 'thin_high' ? '▲' : '▼';
  return `<span class="thin-chip thin-${flag === 'thin_high' ? 'high' : 'low'}" title="${esc(PRICE_FLAGS[flag])}"><span aria-hidden="true">${icon}</span> Thin market${long ? `<span class="thin-text"> – ${esc(PRICE_FLAGS[flag])}</span>` : ''}</span>`;
}

let sparkSeq = 0;

/** Inline SVG sparkline with a zero line; above zero green, below zero red. values may contain nulls. */
export function sparkline(values, { w = 84, h = 24, label = 'Profit, last 14 days' } = {}) {
  const vals = Array.isArray(values) ? values : [];
  const nums = vals.filter(isNum);
  if (nums.length < 2) return `<span class="spark spark-empty" style="width:${w}px" title="Not enough price history">–</span>`;
  let min = Math.min(0, ...nums);
  let max = Math.max(0, ...nums);
  if (max === min) max = min + 1;
  const pad = 2;
  const n = vals.length;
  const x = (i) => pad + (i * (w - pad * 2)) / Math.max(1, n - 1);
  const y = (v) => pad + ((max - v) * (h - pad * 2)) / (max - min);
  const zy = y(0).toFixed(1);
  let d = '';
  let pen = false;
  let last = null;
  vals.forEach((v, i) => {
    if (!isNum(v)) {
      pen = false;
      return;
    }
    d += `${pen ? 'L' : 'M'}${x(i).toFixed(1)} ${y(v).toFixed(1)}`;
    pen = true;
    last = [x(i), y(v), v];
  });
  const id = `spk${++sparkSeq}`;
  const first = nums[0];
  const lastV = nums[nums.length - 1];
  const aria = `${label}: from ${moneyText(first)} to ${moneyText(lastV)}`;
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(aria)}"><title>${esc(aria)}</title>
    <defs><clipPath id="${id}a"><rect x="0" y="0" width="${w}" height="${zy}"/></clipPath><clipPath id="${id}b"><rect x="0" y="${zy}" width="${w}" height="${h}"/></clipPath></defs>
    <line class="spark-zero" x1="0" x2="${w}" y1="${zy}" y2="${zy}"/>
    <path class="spark-up" d="${d}" clip-path="url(#${id}a)"/>
    <path class="spark-down" d="${d}" clip-path="url(#${id}b)"/>
    ${last ? `<circle class="${last[2] >= 0 ? 'spark-dot-up' : 'spark-dot-down'}" cx="${last[0].toFixed(1)}" cy="${last[1].toFixed(1)}" r="2.2"/>` : ''}
  </svg>`;
}

/** profit_trend_pct (already percent) -> coloured "▲ 12%". */
export function trendPctHtml(v) {
  if (!isNum(v)) return '<span class="trend-pct muted">–</span>';
  const cls = v > 0 ? 'up' : v < 0 ? 'down' : 'muted';
  const arrow = v > 0 ? '▲' : v < 0 ? '▼' : '•';
  const shown = Math.abs(v) >= 999 ? '>999%' : `${Math.abs(v) >= 10 ? Math.round(Math.abs(v)) : Math.abs(v).toFixed(1)}%`;
  return `<span class="trend-pct ${cls}" title="Profit last 3 days vs. the week before: ${pctRaw(v)}"><span aria-hidden="true">${arrow}</span> ${shown}</span>`;
}
