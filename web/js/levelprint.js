// Printable / shareable leveling guide: a standalone HTML document (no app needed to view it) built from a plan.
// Opened in a new tab with buttons for Print / Save as PDF, Download HTML and Copy as text (Discord-friendly).
import { esc, isNum } from './util.js';

const nf = new Intl.NumberFormat('en-GB', { maximumFractionDigits: 0 });
// Quality colours readable on white paper.
const Q_PRINT = ['#6b6b6b', '#1a1a1a', '#1a8a00', '#0055aa', '#7b22b8', '#c05f00', '#8a7340', '#0088aa'];

/** Copper -> "12g 34s" (copper dropped above 1g, silver dropped above 1000g). */
export function gold(copper) {
  if (!isNum(copper)) return '–';
  const neg = copper < 0;
  const c = Math.round(Math.abs(copper));
  const g = Math.floor(c / 10000);
  const s = Math.floor((c % 10000) / 100);
  const cc = c % 100;
  let out;
  if (g >= 1000) out = `${nf.format(g)}g`;
  else if (g > 0) out = s ? `${nf.format(g)}g ${s}s` : `${nf.format(g)}g`;
  else if (s > 0) out = cc ? `${s}s ${cc}c` : `${s}s`;
  else out = `${cc}c`;
  return (neg ? '−' : '') + out;
}

const n = (v) => (isNum(v) ? nf.format(v) : '–');
const itemName = (id, name) => name || (id ? `Item #${id}` : '?');
const qStyle = (q) => `color:${Q_PRINT[isNum(q) ? q : 1] || Q_PRINT[1]}`;
const srcText = (s) => ({ ah: 'AH', vendor: 'Vendor', craft: 'Craft' }[s] || 'No price');

function stepName(s) {
  return s.name || s.created_name || `Spell #${s.spell_id}`;
}

function guideParts(plan, meta) {
  const steps = (plan.steps || []).slice().sort((a, b) => a.from - b.from);
  const start = isNum(plan.start) ? plan.start : meta.start;
  const target = isNum(plan.target) ? plan.target : meta.target;
  const unreach = isNum(plan.unreachable_from) ? plan.unreachable_from : null;
  const ms = (meta.milestones || []).filter((m) => m.skill > start && m.skill < (unreach ?? target));
  // Interleave steps and trainer milestones by skill.
  const rows = [];
  let mi = 0;
  for (const s of steps) {
    while (mi < ms.length && ms[mi].skill <= s.from) rows.push({ ms: ms[mi++] });
    rows.push({ step: s });
  }
  while (mi < ms.length) rows.push({ ms: ms[mi++] });
  const shop = (plan.shopping || []).filter((r) => r.source !== 'craft');
  return { steps, start, target, unreach, rows, shop, t: plan.totals || {} };
}

/** Plain text for chat (Discord markdown). */
export function guideText(plan, meta) {
  const { start, target, unreach, rows, shop, t } = guideParts(plan, meta);
  const L = [];
  L.push(`**${meta.profName} ${start}–${unreach ?? target} leveling guide** (${meta.dateText})`);
  const net = isNum(t.net) ? (t.net < 0 ? `profit ${gold(-t.net)}` : `net cost ≈ ${gold(t.net)}`) : '';
  L.push(`Cost ${gold(t.cost)} · recovered ${gold(t.recover)} · ${net} · ${n(t.crafts)} crafts`);
  L.push('');
  L.push('__Shopping list__');
  for (const r of shop) L.push(`- ${n(r.need)}× ${itemName(r.item_id, r.name)}${r.source === 'vendor' ? ' (vendor)' : ''}${isNum(r.total) ? ` – ${gold(r.total)}` : ''}`);
  if (plan.recipes_to_buy && plan.recipes_to_buy.length) {
    L.push('');
    L.push('__Recipes to buy__');
    for (const r of plan.recipes_to_buy) L.push(`- ${itemName(r.item_id, r.name)}${isNum(r.price) ? ` – ${gold(r.price)}` : ''}`);
  }
  const subs = plan.subcrafts || [];
  if (subs.length) {
    L.push('');
    L.push('__Craft along the way__ (materials are in the shopping list)');
    for (const x of subs) L.push(`- ${n(x.need)}× ${x.name || ''}`);
  }
  L.push('');
  L.push('__Steps__');
  for (const x of rows) {
    if (x.ms) { L.push(`> ${x.ms.skill}: ${x.ms.text}`); continue; }
    const s = x.step;
    const regs = (s.reagents || []).map((g) => `${n(g.count)} ${itemName(g.item_id, g.name)}`).join(', ');
    L.push(`${s.from}–${s.to}: ${n(s.crafts)}× ${stepName(s)}${regs ? ` (${regs})` : ''}`);
  }
  if (unreach !== null) L.push(`(No priced recipe found from ${unreach} to ${target}.)`);
  L.push('');
  L.push(`Prices: ${meta.priceText} – they change with the market.`);
  return L.join('\n');
}

const CSS = `
*{box-sizing:border-box}
body{margin:0;background:#f3f1ec;color:#1a1a1a;font:14px/1.45 "Segoe UI",system-ui,-apple-system,sans-serif}
.page{max-width:900px;margin:0 auto;padding:28px 32px 48px;background:#fff;min-height:100vh}
.bar{position:sticky;top:0;z-index:2;display:flex;flex-wrap:wrap;gap:8px;justify-content:center;padding:10px 16px;background:#2b2620;box-shadow:0 2px 8px rgba(0,0,0,.25)}
.bar button{font:inherit;font-weight:600;padding:7px 14px;border-radius:6px;border:1px solid #c9a94a;background:#3a3329;color:#f1dfa6;cursor:pointer}
.bar button.main{background:#c9a94a;color:#1d1a14}
.bar button:hover{filter:brightness(1.15)}
.bar .msg{color:#cfc6b4;align-self:center;font-size:13px;min-width:90px}
h1{font-size:26px;margin:0 0 2px}
h2{font-size:17px;margin:26px 0 8px;padding-bottom:4px;border-bottom:2px solid #c9a94a}
.sub{color:#5d574d;margin:0 0 16px}
.sum{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:14px 0 6px}
.sum div{border:1px solid #ddd6c6;border-radius:8px;padding:8px 10px}
.sum b{display:block;font-size:18px}
.sum span{font-size:12px;color:#5d574d;text-transform:uppercase;letter-spacing:.04em}
.profit{color:#1a7a00}
table{width:100%;border-collapse:collapse}
th,td{padding:5px 8px;border-bottom:1px solid #e6e0d3;text-align:left;vertical-align:top}
th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:#5d574d;border-bottom:1px solid #bfb6a3}
.r{text-align:right;white-space:nowrap}
.ck{width:22px;padding-right:0}
.ck i{display:inline-block;width:13px;height:13px;border:1.5px solid #8a8274;border-radius:3px;vertical-align:-2px}
.skill{white-space:nowrap;font-weight:700;font-variant-numeric:tabular-nums}
.regs{font-size:12.5px;color:#4a453d}
.ms td{background:#fbf5e3;font-weight:600;color:#6b5414}
.foot td{font-weight:700;border-bottom:0;border-top:2px solid #bfb6a3}
.muted{color:#8a8274}
.warn{color:#9a4b00}
ul{margin:4px 0;padding-left:20px}
.note{margin-top:28px;font-size:12px;color:#7a7364}
@media (max-width:640px){.page{padding:18px 16px 40px}.sum{grid-template-columns:repeat(2,1fr)}.hide-sm{display:none}}
@media print{
  body{background:#fff;font-size:11.5px}
  .bar{display:none}
  .page{max-width:none;padding:0}
  h2{break-after:avoid}
  tr{break-inside:avoid}
  @page{margin:14mm 12mm}
}`;

/** Full standalone HTML document. */
export function guideHtml(plan, meta) {
  const { start, target, unreach, rows, shop, t } = guideParts(plan, meta);
  const title = `${meta.profName} ${start}–${unreach ?? target} leveling guide`;
  const item = (id, name, q) => `<span style="${qStyle(q)}">${esc(itemName(id, name))}</span>`;
  const shopTotal = shop.reduce((s, r) => s + (isNum(r.total) ? r.total : 0), 0);
  const profit = isNum(t.net) && t.net < 0;

  const stepRows = rows.map((x) => {
    if (x.ms) return `<tr class="ms"><td class="ck"></td><td class="skill">${esc(x.ms.skill)}</td><td colspan="3">⚑ ${esc(x.ms.text)}</td></tr>`;
    const s = x.step;
    const regs = (s.reagents || []).map((g) => `${n(g.count)}× ${item(g.item_id, g.name, g.quality)}`).join(', ');
    const made = s.num_created > 1 ? ` <span class="muted">(makes ${n(s.num_created)})</span>` : '';
    const buyRecipe = isNum(s.recipe_item) ? ' <span class="warn">(recipe must be bought)</span>' : '';
    return `<tr><td class="ck"><i></i></td><td class="skill">${esc(s.from)}–${esc(s.to)}</td>
      <td><b>${n(s.crafts)}×</b> ${s.created_item_id ? item(s.created_item_id, stepName(s), s.created_quality) : esc(stepName(s))}${made}${buyRecipe}${regs ? `<div class="regs">each: ${regs}</div>` : ''}</td>
      <td class="r hide-sm">${gold(s.cost_total)}</td><td class="r">${isNum(s.net_total) ? (s.net_total < 0 ? `<span class="profit">+${gold(-s.net_total)}</span>` : gold(s.net_total)) : '–'}</td></tr>`;
  }).join('');

  const recipes = plan.recipes_to_buy || [];
  const subs = plan.subcrafts || [];
  const tools = plan.tools || [];
  const warnings = plan.warnings || [];
  const text = guideText(plan, meta);

  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${esc(title)}</title><style>${CSS}</style></head>
<body>
<div class="bar" id="bar">
  <button class="main" id="b-print">Print / Save as PDF</button>
  <button id="b-dl">Download HTML</button>
  <button id="b-copy">Copy as text</button>
  <span class="msg" id="msg"></span>
</div>
<div class="page">
  <h1>${esc(title)}</h1>
  <p class="sub">Generated ${esc(meta.dateText)} · prices: ${esc(meta.priceText)} · ${esc(meta.recoverText)}${meta.sources === 'all' ? ' · includes recipes bought from the AH' : ' · trainer recipes only'}</p>
  <div class="sum">
    <div><span>Total cost</span><b>${gold(t.cost)}</b></div>
    <div><span>Recovered</span><b>${gold(t.recover)}</b></div>
    <div><span>${profit ? 'Profit' : 'Net cost'}</span><b class="${profit ? 'profit' : ''}">${profit ? '+' + gold(-t.net) : gold(t.net)}</b></div>
    <div><span>Crafts</span><b>${n(t.crafts)}</b></div>
  </div>
  ${unreach !== null ? `<p class="warn"><b>The guide stops at skill ${unreach}</b> – no recipe with a known price from there to ${target}.</p>` : ''}

  <h2>Shopping list</h2>
  <table><thead><tr><th class="ck"></th><th>Item</th><th class="r">Need</th><th class="r hide-sm">Each</th><th>From</th><th class="r">Total</th></tr></thead>
  <tbody>${shop.map((r) => `<tr><td class="ck"><i></i></td><td>${item(r.item_id, r.name, r.quality)}</td><td class="r">${n(r.need)}</td><td class="r hide-sm">${gold(r.unit_price)}</td><td>${srcText(r.source)}</td><td class="r">${gold(r.total)}</td></tr>`).join('')}</tbody>
  <tfoot><tr class="foot"><td></td><td colspan="4">Total to buy</td><td class="r">${gold(shopTotal)}</td></tr></tfoot></table>

  ${recipes.length ? `<h2>Recipes to buy</h2><table><tbody>${recipes.slice().sort((a, b) => (a.skill ?? 0) - (b.skill ?? 0)).map((r) => `<tr><td class="ck"><i></i></td><td>${item(r.item_id, r.name, r.quality)}${r.recipe_name ? ` <span class="muted">– teaches ${esc(r.recipe_name)}</span>` : ''}</td><td class="r">${isNum(r.skill) ? `skill ${r.skill}` : ''}</td><td class="r">${gold(r.price)}</td></tr>`).join('')}</tbody></table>` : ''}

  <h2>Steps</h2>
  <table><thead><tr><th class="ck"></th><th>Skill</th><th>Craft</th><th class="r hide-sm">Cost</th><th class="r">Net</th></tr></thead>
  <tbody>${stepRows}</tbody></table>

  ${subs.length ? `<h2>Crafted along the way</h2><p class="muted" style="margin:0 0 6px">Make these yourself – cheaper than buying. Their materials are in the shopping list.</p><table><tbody>${subs.map((x) => `<tr><td class="ck"><i></i></td><td>${x.item_id ? item(x.item_id, x.name, x.quality) : esc(x.name || '')}</td><td class="r">${n(x.need)} needed</td><td class="r">${n(x.crafts)} crafts</td></tr>`).join('')}</tbody></table>` : ''}
  ${tools.length ? `<h2>Tools</h2><ul>${tools.map((x) => `<li>${esc(x.name || '')}${isNum(x.skill) ? ` <span class="muted">(make at ${x.skill})</span>` : ''}</li>`).join('')}</ul>` : ''}
  ${warnings.length ? `<h2>Notes</h2><ul class="warn">${warnings.map((w) => `<li>${esc(w)}</li>`).join('')}</ul>` : ''}

  <p class="note">Crafts are rounded up from the expected number – you may need a few more or fewer. After yellow, the skill-up chance drops until the recipe turns grey. Prices are a snapshot and change with the market.</p>
</div>
<script>
(function(){
  var TEXT = ${JSON.stringify(text).replace(/</g, '\\u003c')};
  var FILE = ${JSON.stringify(meta.fileName)};
  var msg = document.getElementById('msg');
  function say(t){ msg.textContent = t; setTimeout(function(){ if (msg.textContent === t) msg.textContent = ''; }, 2500); }
  document.getElementById('b-print').onclick = function(){ window.print(); };
  document.getElementById('b-dl').onclick = function(){
    var html = '<!doctype html>\\n' + document.documentElement.outerHTML;
    var a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([html], { type: 'text/html' }));
    a.download = FILE; document.body.appendChild(a); a.click(); a.remove();
    say('Saved');
  };
  document.getElementById('b-copy').onclick = function(){
    function fallback(){ var t = document.createElement('textarea'); t.value = TEXT; document.body.appendChild(t); t.select(); try { document.execCommand('copy'); say('Copied'); } catch (e) { say('Copy failed'); } t.remove(); }
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(TEXT).then(function(){ say('Copied'); }, fallback); else fallback();
  };
})();
</script>
</body></html>`;
}

/** Open the guide in a new tab. Call synchronously from a click handler (pop-up blockers). */
export function openGuide(plan, meta) {
  const html = guideHtml(plan, meta);
  const url = URL.createObjectURL(new Blob([html], { type: 'text/html' }));
  const w = window.open(url, '_blank');
  setTimeout(() => URL.revokeObjectURL(url), 60000);
  return !!w;
}
