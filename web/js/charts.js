// Chart.js wrapper: theme-aware, re-renders all live charts on theme change.
import { moneyText, goldTick, esc } from './util.js';

const live = []; // { canvas, factory, chart }

export function hasChart() {
  return typeof window.Chart === 'function';
}

export function themeColors() {
  const cs = getComputedStyle(document.documentElement);
  const v = (n) => cs.getPropertyValue(n).trim();
  return {
    text: v('--text'),
    text2: v('--text-2'),
    muted: v('--muted'),
    grid: v('--grid'),
    surface: v('--surface'),
    surface2: v('--surface-2'),
    border: v('--border'),
    accent: v('--accent'),
    up: v('--up'),
    down: v('--down'),
    series: [v('--s1'), v('--s2'), v('--s3'), v('--s4'), v('--s5'), v('--s6')],
    font: v('--font-sans') || 'system-ui, sans-serif',
  };
}

export function alpha(hex, a) {
  const h = hex.replace('#', '');
  if (h.length !== 6) return hex;
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${a})`;
}

function applyDefaults(c) {
  const C = window.Chart;
  C.defaults.font.family = c.font;
  C.defaults.font.size = 12;
  C.defaults.color = c.text2;
  C.defaults.borderColor = c.grid;
  C.defaults.animation.duration = 350;
  const tt = C.defaults.plugins.tooltip;
  tt.backgroundColor = c.surface2;
  tt.titleColor = c.text;
  tt.bodyColor = c.text;
  tt.borderColor = c.border;
  tt.borderWidth = 1;
  tt.padding = 10;
  tt.cornerRadius = 8;
  tt.boxPadding = 4;
  tt.usePointStyle = true;
  C.defaults.plugins.legend.labels.usePointStyle = true;
  C.defaults.plugins.legend.labels.boxWidth = 8;
  C.defaults.plugins.legend.labels.boxHeight = 8;
}

/** Mount a chart; factory(colors) must return a Chart.js config. */
export function mountChart(canvas, factory) {
  if (!canvas) return null;
  if (!hasChart()) {
    const box = canvas.parentElement;
    if (box) box.innerHTML = '<div class="chart-missing">Chart unavailable (Chart.js could not be loaded – check your connection).</div>';
    return null;
  }
  const c = themeColors();
  applyDefaults(c);
  const chart = new window.Chart(canvas, factory(c));
  live.push({ canvas, factory, chart });
  return chart;
}

export function destroyAll() {
  while (live.length) {
    const e = live.pop();
    try {
      e.chart.destroy();
    } catch (err) {
      /* ignore */
    }
  }
}

/** Destroy charts whose canvas has been removed from the page (e.g. a table re-render). */
export function pruneDetached() {
  for (let i = live.length - 1; i >= 0; i--) {
    if (document.body.contains(live[i].canvas)) continue;
    try {
      live[i].chart.destroy();
    } catch (err) {
      /* ignore */
    }
    live.splice(i, 1);
  }
}

export function rerenderAll() {
  if (!hasChart()) return;
  const c = themeColors();
  applyDefaults(c);
  for (const e of live) {
    if (!document.body.contains(e.canvas)) continue;
    try {
      e.chart.destroy();
    } catch (err) {
      /* ignore */
    }
    e.chart = new window.Chart(e.canvas, e.factory(c));
  }
}

// ---- Shared axis / tooltip helpers --------------------------------------

export function moneyAxis(c, extra = {}) {
  return Object.assign(
    {
      grid: { color: c.grid, drawTicks: false },
      border: { display: false },
      ticks: { color: c.muted, padding: 8, callback: (v) => goldTick(v), maxTicksLimit: 6 },
    },
    extra
  );
}

export function plainAxis(c, extra = {}) {
  return Object.assign(
    {
      grid: { display: false },
      border: { color: c.grid },
      ticks: { color: c.muted, maxRotation: 0, autoSkipPadding: 14 },
    },
    extra
  );
}

export function moneyTooltip(label) {
  return {
    callbacks: {
      label: (ctx) => `${label ? label(ctx) : ctx.dataset.label}: ${moneyText(ctx.parsed.y ?? ctx.parsed)}`,
    },
  };
}

/** Plain HTML legend/caption fallback text. */
export function chartBox(id, height = 260, label = '') {
  return `<div class="chart-box" style="height:${height}px"><canvas id="${esc(id)}" role="img" aria-label="${esc(label)}"></canvas></div>`;
}
