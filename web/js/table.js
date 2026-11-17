// Minimal sortable / expandable table component.
import { esc, emptyState } from './util.js';

/**
 * columns: [{ key, label, align:'right'|'left'|'center', render(row)->html, sort(row)->value, cls, title, hideSm, hideMd }]
 * opts: { sortKey, sortDir:'asc'|'desc', empty, expand(row)->html, rowKey(row), rowClass(row), limit, onRender(el) }
 */
export class DataTable {
  constructor(container, columns, opts = {}) {
    this.el = container;
    this.columns = columns;
    this.opts = opts;
    this.rows = [];
    this.sortKey = opts.sortKey || null;
    this.sortDir = opts.sortDir || 'desc';
    this.expanded = new Set();
    this.limit = opts.limit || 0;
    this.showAll = false;
    this.el.addEventListener('click', (e) => this.onClick(e));
    this.el.addEventListener('keydown', (e) => {
      if ((e.key === 'Enter' || e.key === ' ') && e.target.matches('th[data-sort], tr.expandable')) {
        e.preventDefault();
        this.onClick(e);
      }
    });
  }

  setRows(rows) {
    this.rows = rows || [];
    this.render();
  }

  onClick(e) {
    const th = e.target.closest('th[data-sort]');
    if (th && this.el.contains(th)) {
      const key = th.dataset.sort;
      if (this.sortKey === key) this.sortDir = this.sortDir === 'asc' ? 'desc' : 'asc';
      else {
        this.sortKey = key;
        const col = this.columns.find((c) => c.key === key);
        this.sortDir = col && col.defaultDir ? col.defaultDir : 'desc';
      }
      this.render();
      return;
    }
    if (e.target.closest('[data-more]')) {
      this.showAll = true;
      this.render();
      return;
    }
    if (e.target.closest('a, button, input, label')) return;
    const tr = e.target.closest('tr.expandable');
    if (tr && this.el.contains(tr)) {
      const k = tr.dataset.key;
      if (this.expanded.has(k)) this.expanded.delete(k);
      else this.expanded.add(k);
      this.render();
    }
  }

  sorted() {
    const col = this.columns.find((c) => c.key === this.sortKey);
    if (!col) return this.rows.slice();
    const get = col.sort || ((r) => r[col.key]);
    const dir = this.sortDir === 'asc' ? 1 : -1;
    return this.rows.slice().sort((a, b) => {
      const va = get(a);
      const vb = get(b);
      const na = va === null || va === undefined || (typeof va === 'number' && !isFinite(va));
      const nb = vb === null || vb === undefined || (typeof vb === 'number' && !isFinite(vb));
      if (na && nb) return 0;
      if (na) return 1; // nulls last regardless of direction
      if (nb) return -1;
      if (typeof va === 'string' || typeof vb === 'string') return String(va).localeCompare(String(vb), 'en-GB') * dir;
      return (va - vb) * dir;
    });
  }

  render() {
    const { expand, rowKey, rowClass, empty } = this.opts;
    if (!this.rows.length) {
      this.el.innerHTML = empty || emptyState('No results', 'Try adjusting the filters.');
      if (this.opts.onRender) this.opts.onRender(this.el);
      return;
    }
    let rows = this.sorted();
    const total = rows.length;
    if (this.limit && !this.showAll) rows = rows.slice(0, this.limit);
    const colCount = this.columns.length + (expand ? 1 : 0);

    let html = '<div class="table-wrap"><table class="dt"><thead><tr>';
    if (expand) html += '<th class="exp-col" aria-label="Expand"></th>';
    for (const c of this.columns) {
      const sortable = c.sortable !== false;
      const active = this.sortKey === c.key;
      const arrow = active ? (this.sortDir === 'asc' ? '▲' : '▼') : '';
      const cls = [c.align === 'right' ? 'r' : c.align === 'center' ? 'c' : '', c.hideSm ? 'hide-sm' : '', c.hideMd ? 'hide-md' : '', sortable ? 'sortable' : '', active ? 'active' : ''].join(' ');
      html += `<th class="${cls}" ${sortable ? `data-sort="${esc(c.key)}" tabindex="0" aria-sort="${active ? (this.sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}"` : ''} ${c.title ? `title="${esc(c.title)}"` : ''}>${esc(c.label)}<span class="arrow">${arrow}</span></th>`;
    }
    html += '</tr></thead><tbody>';

    rows.forEach((r, i) => {
      const key = rowKey ? String(rowKey(r)) : String(i);
      const isOpen = expand && this.expanded.has(key);
      const rc = (rowClass ? rowClass(r) : '') + (expand ? ' expandable' : '') + (isOpen ? ' open' : '');
      html += `<tr class="${rc}" data-key="${esc(key)}" ${expand ? 'tabindex="0" aria-expanded="' + (isOpen ? 'true' : 'false') + '"' : ''}>`;
      if (expand) html += `<td class="exp-col"><span class="chev" aria-hidden="true">›</span></td>`;
      for (const c of this.columns) {
        const cls = [c.align === 'right' ? 'r' : c.align === 'center' ? 'c' : '', c.hideSm ? 'hide-sm' : '', c.hideMd ? 'hide-md' : '', c.cls || ''].join(' ');
        const val = c.render ? c.render(r) : esc(r[c.key]);
        html += `<td class="${cls}" data-label="${esc(c.label)}">${val}</td>`;
      }
      html += '</tr>';
      if (isOpen) html += `<tr class="exp-row"><td colspan="${colCount}">${expand(r)}</td></tr>`;
    });
    html += '</tbody></table></div>';
    if (this.limit && !this.showAll && total > this.limit) {
      html += `<div class="table-more"><button class="btn btn-ghost" data-more>Show all ${total} rows</button></div>`;
    }
    this.el.innerHTML = html;
    if (this.opts.onRender) this.opts.onRender(this.el);
  }
}
