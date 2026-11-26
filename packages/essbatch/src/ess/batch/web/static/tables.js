// A table of rows by `key`, on Tabulator. Only the boxes at the left select
// rows. A click on an editable cell opens its editor (see `cellEditor`). An
// edit of a row that is selected along with others writes the value to every
// selected row.
import {TabulatorFull as Tabulator, EditModule, TooltipModule} from './vendor/tabulator_esm.min.js';
import {h} from './dom.js';

// Tabulator's edit module (`focusScrollAdjust`) scrolls the whole row of an
// opened editor into view, even if nearly all of it shows. The editor scrolls
// itself instead, only if less than half of the row shows (see `cellEditor`).
class Edit extends EditModule {
  focusScrollAdjust() {}
}
Tabulator.registerModule(Edit);

// Tabulator inserts a tooltip string as HTML. Every tooltip, of a cell or of a
// header, passes through `loadTooltip`, so here a string becomes an element
// that holds it as text.
class Tooltip extends TooltipModule {
  loadTooltip(event, target, tip) {
    const make = typeof tip === 'function' ? tip : () => tip;
    super.loadTooltip(event, target, (e, component, onRendered) => {
      const text = make(e, component, onRendered);
      return typeof text === 'string' && text ? h('div', {text}) : text;
    });
  }
}
Tabulator.registerModule(Tooltip);

const sameSet = (a, b) => a.length === b.length && a.every((x) => b.includes(x));

const empty = (column) => (column.default ? `empty: ${column.default}` : 'empty');

const matching = (options, text) =>
  options.filter((o) => o.toLowerCase().includes(text.trim().toLowerCase()));

// The list of options under the editor of a pick. The editor writes the
// marked option. Typing filters the list and marks the first option left.
class Choices {
  constructor(options, at, pick) {
    this.options = options;
    this.shown = options;
    this.marked = -1;
    this.element = h('ul', {class: 'choices', role: 'listbox'});
    // Keep the focus in the editor: a blur would close it.
    this.element.addEventListener('mousedown', (event) => event.preventDefault());
    const box = at.getBoundingClientRect();
    const below = window.innerHeight - box.bottom > 200 || box.top < 200;
    Object.assign(this.element.style, {
      left: `${box.left}px`,
      minWidth: `${box.width}px`,
      ...(below ? {top: `${box.bottom}px`} : {bottom: `${window.innerHeight - box.top}px`}),
    });
    this.pick = pick;
    document.body.append(this.element);
  }

  // With `current`, shows all options and marks `current`. Without, shows
  // those that contain `text`.
  show(text, current) {
    this.shown = current === undefined ? matching(this.options, text) : this.options;
    this.marked = current === undefined ? (text.trim() && this.shown.length ? 0 : -1) : this.shown.indexOf(current);
    this.element.replaceChildren(
      ...this.shown.map((o, i) =>
        h('li', {role: 'option', text: o, 'aria-selected': i === this.marked ? 'true' : null, onclick: () => this.pick(o)}),
      ),
    );
    this.element.children[this.marked]?.scrollIntoView({block: 'nearest'});
  }

  move(by) {
    if (!this.shown.length) return;
    this.marked = Math.min(Math.max(this.marked + by, 0), this.shown.length - 1);
    this.element.querySelector('[aria-selected]')?.removeAttribute('aria-selected');
    this.element.children[this.marked].setAttribute('aria-selected', 'true');
    this.element.children[this.marked].scrollIntoView({block: 'nearest'});
  }

  get value() {
    return this.shown[this.marked];
  }

  remove() {
    this.element.remove();
  }
}

// The editor of a cell: a text input that opens at the value of the cell, all
// selected, so that typing replaces it. Enter, Tab or leaving writes. Escape,
// unchanged text, or an empty cell left empty writes nothing. A pick lists
// what `options()` returns when the editor opens. `say` shows why a value is
// not written.
function cellEditor(column, options, say) {
  const {type} = column.editor;
  return (cell, onRendered, success, cancel) => {
    const held = cell.getData().cells[column.name];
    const start = (type === 'select' ? held.value : held.text) || column.default || '';
    const input = h('input', {
      type: 'text',
      class: 'cell-editor',
      value: start,
      inputmode: type === 'number' ? 'decimal' : null,
      spellcheck: 'false',
      autocomplete: 'off',
      'aria-label': column.title,
    });
    let choices = null;
    let open = true;
    const close = (write, picked) => {
      if (!open) return;
      open = false;
      const text = input.value.trim();
      const value = picked ?? choices?.value;
      choices?.remove();
      if (!write) return cancel();
      if (text === '') return held.value == null ? cancel() : success(null);
      if (type === 'select') {
        if (value !== undefined) return value === held.value ? cancel() : success(value);
        say(`${column.title}: no option matches ${text}, so nothing was written.`);
        return cancel();
      }
      if (text === start) return cancel();
      if (type === 'number' && !Number.isFinite(Number(text))) {
        say(`${column.title}: ${text} is not a number, so nothing was written.`);
        return cancel();
      }
      return success(type === 'number' ? Number(text) : text);
    };
    onRendered(() => {
      if (type === 'select') {
        choices = new Choices(options(), cell.getElement(), (o) => close(true, o));
        choices.show('', held.value);
      }
      const row = cell.getRow().getElement().getBoundingClientRect();
      const view = cell.getTable().element.querySelector('.tabulator-tableholder').getBoundingClientRect();
      const shown = Math.min(row.bottom, view.bottom) - Math.max(row.top, view.top);
      input.focus({preventScroll: shown > row.height / 2});
      input.select();
    });
    input.addEventListener('input', () => choices?.show(input.value));
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') close(true);
      if (event.key === 'Escape') close(false);
      if (choices && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
        event.preventDefault();
        event.stopPropagation();
        choices.move(event.key === 'ArrowDown' ? 1 : -1);
      }
    });
    // Tab is left to the table: it opens the next cell, and the blur closes
    // this editor.
    input.addEventListener('blur', () => close(true));
    return input;
  };
}

const editorOf = (column, options, say) =>
  ['select', 'number', 'text'].includes(column.editor?.type)
    ? {editor: cellEditor(column, options, say)}
    : {editable: false};

// With `revert`, a cell set by hand gets a button that restores its earlier
// value.
function cellFormatter(column, revert) {
  return (cell) => {
    const data = cell.getData();
    // A row drawn before the rows of new columns arrive has no such cell.
    const c = data.cells[column.name] ?? {};
    const element = cell.getElement();
    element.classList.toggle('bad', Boolean(c.problem));
    element.classList.toggle('edited', Boolean(c.edited));
    const shown = c.text
      ? h('span', {class: 'text', text: c.text})
      : h('span', {class: 'text muted', text: c.problem ? 'missing' : column.default || '—'});
    const back = c.original || empty(column);
    return h(
      'span',
      {class: 'cell'},
      shown,
      revert &&
        c.edited &&
        h('button', {
          type: 'button',
          class: 'revert',
          title: `Set by hand. Back to ${back}`,
          'aria-label': `Back to ${back}`,
          text: '↺',
          onclick: (event) => {
            event.stopPropagation();
            revert(data.key, column.name);
          },
        }),
    );
  };
}

function cellTooltip(column) {
  return (event, cell) => {
    const c = cell.getData().cells[column.name] ?? {};
    if (c.problem) return c.problem;
    if (c.edited) return `${c.text || empty(column)}, set by hand; it was ${c.original || empty(column)}`;
    return c.text;
  };
}

// Tabulator columns for the columns {name, title, editor, default} that the
// server sends. `current()` returns the columns of the state shown, whose
// options an editor lists. `revert(key, name)`, if given, restores a cell set
// by hand. `menu(column)`, if given, returns the items of the header menu, in
// the form of Tabulator's `headerMenu`.
export function cellColumns(columns, {current, revert, say, menu}) {
  const options = (column) => () => current().find((c) => c.name === column.name).editor.options;
  return columns.map((column) => ({
    title: column.title,
    headerTooltip: column.title,
    field: `cells.${column.name}.value`,
    minWidth: 96,
    headerSort: false,
    cssClass: column.editor ? `editable ${column.editor.type}` : '',
    formatter: cellFormatter(column, revert),
    tooltip: cellTooltip(column),
    ...(menu?.(column).length > 0 && {headerMenu: menu(column)}),
    ...editorOf(column, options(column), say),
  }));
}

export class Grid {
  // `columns` are Tabulator columns. `edits` maps the field of each editable
  // column to the column as the server sends it. If `edit` returns false, or
  // a promise of false, the cell shows its earlier value again.
  constructor(element, {columns, edits, edit, select, sorted, placeholder}) {
    this.element = element;
    this.edits = edits;
    this.onSelect = select;
    this.shown = [];
    this.json = new Map(); // each row as last drawn, as JSON
    this.pending = null;
    this.quiet = false;
    this.empty = h('div', {class: 'empty', text: placeholder}); // a caller may change its text
    this.table = new Tabulator(element, {
      index: 'key',
      height: '100%',
      layout: 'fitColumns',
      selectableRows: 'highlight',
      editTriggerEvent: 'click',
      placeholder: this.empty,
      rowHeader: {
        formatter: 'rowSelection',
        titleFormatter: 'rowSelection',
        titleFormatterParams: {rowRange: 'active'},
        headerSort: false,
        resizable: false,
        width: 36,
        hozAlign: 'center',
        headerHozAlign: 'center',
      },
      columns,
    });
    this.built = new Promise((resolve) => this.table.on('tableBuilt', resolve));
    this.updated = this.built;
    this.table.on('renderComplete', () => this.fit());
    this.built.then(() => new ResizeObserver(() => this.fit()).observe(this.table.rowManager.element));
    this.table.on('rowSelectionChanged', () => this.quiet || this.announce());
    this.table.on('dataSorted', (sorters) =>
      sorted?.(sorters.map((s) => ({column: s.field, dir: s.dir}))),
    );
    this.table.on('cellEdited', (cell) => {
      const column = this.edits[cell.getField()];
      const key = cell.getRow().getData().key;
      const selected = this.selected();
      const keys = selected.length > 1 && selected.includes(key) ? selected : [key];
      Promise.resolve(edit(keys, column.name, cell.getValue())).then((done) => done === false && cell.restoreOldValue());
      this.closed();
    });
    this.table.on('cellEditCancelled', () => this.closed());
  }

  async setColumns(columns, edits) {
    await this.built;
    this.edits = edits;
    this.table.setColumns(columns);
  }

  // Space below the last row, so that the rows scroll by whole rows and the
  // top row shows whole at the end. A margin, since the renderer sets the
  // padding itself, to stand for the rows it does not draw.
  fit() {
    const holder = this.table.rowManager.element;
    const rows = holder.querySelector('.tabulator-table');
    const height = rows?.querySelector('.tabulator-row')?.offsetHeight;
    if (!height) return;
    const over = rows.offsetHeight - holder.clientHeight;
    rows.style.marginBottom = over > 0 ? `${(height - (over % height)) % height}px` : '0px';
  }

  selected() {
    return this.table.getSelectedData().map((row) => row.key);
  }

  announce() {
    const keys = this.selected();
    if (sameSet(keys, this.shown)) return;
    this.shown = keys;
    this.onSelect(keys);
  }

  editing() {
    return Boolean(this.element.querySelector('.tabulator-editing'));
  }

  closed() {
    setTimeout(() => this.pending && this.update(...this.pending));
  }

  // Shows `rows` and keeps the selection. While a cell is edited, the rows
  // wait until the editor closes. `start`, {selected, filter, sort}, is for
  // the rows of another table, such as another batch: they replace the rows
  // shown, from the top, with the selection, status filter and sort of
  // `start`.
  update(rows, start) {
    this.updated = this.draw(rows, start);
    return this.updated;
  }

  // Scrolls to the row of `key` once it is drawn, unless the filter hides it.
  async reveal(key) {
    await this.updated;
    await this.table.scrollToRow(key, 'nearest', false).catch(() => {});
  }

  // Removes the rows gone, updates those changed, and adds the new ones at
  // their place in `rows`. Other rows are not drawn again. These Tabulator
  // calls do not filter or sort, so the filter and sort are applied again
  // after them. The table is replaced instead if it has no rows yet, where
  // adding takes quadratic time, or if the remaining rows changed order. While
  // a cell is edited, the rows wait in `pending`, with the `start` of any rows
  // that waited before them.
  async draw(rows, start) {
    await this.built;
    this.pending = [rows, start ?? this.pending?.[1]];
    if (this.editing()) return;
    this.pending = null;
    const keys = new Set(rows.map((row) => row.key));
    const before = this.table.getData().map((row) => row.key);
    const had = new Set(before);
    const kept = rows.filter((row) => had.has(row.key));
    const stay = before.filter((key) => keys.has(key));
    const gone = before.filter((key) => !keys.has(key));
    const json = new Map(rows.map((row) => [row.key, JSON.stringify(row)]));
    this.quiet = true;
    try {
      await this.keepFocus(async () => {
        if (!start && before.length && kept.every((row, i) => row.key === stay[i])) {
          if (gone.length) await this.table.deleteRow(gone);
          // Each row changed is drawn again whole: a cell shows more than the
          // value Tabulator compares.
          const changed = kept.filter((row) => this.json.get(row.key) !== json.get(row.key));
          if (changed.length) await this.table.updateData(changed);
          for (const row of changed) this.table.getRow(row.key).reformat();
          // New rows next to each other are added at once, after the row
          // before them, or at the top.
          for (let i = 0, end; i < rows.length; i = end) {
            end = i + 1;
            if (had.has(rows[i].key)) continue;
            while (end < rows.length && !had.has(rows[end].key)) end++;
            await this.table.addData(rows.slice(i, end), i === 0, rows[i - 1]?.key);
          }
          if (gone.length || changed.length || kept.length < rows.length) this.table.refreshFilter();
        } else {
          const selected = (start?.selected ?? this.selected()).filter((key) => keys.has(key));
          if (start) {
            this.setFilter(start.filter);
            const fields = this.table.getColumns().map((c) => c.getField());
            this.table.setSort(start.sort.filter((s) => fields.includes(s.column)));
            this.table.deselectRow();
            await this.table.setData(rows);
          } else await this.table.replaceData(rows);
          if (selected.length) this.table.selectRow(selected);
        }
        this.deselectHidden();
      });
    } finally {
      this.quiet = false;
    }
    this.json = json;
    this.announce();
  }

  // Runs `work`, which may draw rows again, and returns the keyboard focus to
  // the control it was on in a cell: the first element of its tag in the cell
  // at the same position in the row of the same key.
  async keepFocus(work) {
    const active = document.activeElement;
    const cell = active.closest?.('.tabulator-cell');
    const row = cell && this.table.getRows('visible').find((r) => r.getElement().contains(cell));
    if (!row) return work();
    const key = row.getData().key;
    const at = [...row.getElement().querySelectorAll('.tabulator-cell')].indexOf(cell);
    await work();
    if (document.activeElement === active) return;
    const again = this.table.getRow(key)?.getElement().querySelectorAll('.tabulator-cell')[at];
    again?.querySelector(active.tagName)?.focus({preventScroll: true});
  }

  async filter(status) {
    await this.built;
    this.setFilter(status);
    this.deselectHidden();
  }

  setFilter(status) {
    if (status === 'all') this.table.clearFilter();
    else this.table.setFilter('status', '=', status);
  }

  deselectHidden() {
    const shown = new Set(this.table.getRows('active').map((row) => row.getData().key));
    const hidden = this.selected().filter((key) => !shown.has(key));
    if (hidden.length) this.table.deselectRow(hidden);
  }
}

// A status may contain a space, as in "settings changed".
export const statusClass = (status) => `st-${status.replaceAll(' ', '-')}`;

const STATUS_ORDER = ['failed', 'incomplete', 'outdated', 'cancelled', 'ready', 'running', 'settings changed', 'done'];

export const statusColumn = {
  title: 'Status',
  field: 'status',
  width: 260,
  minWidth: 260,
  resizable: false,
  sorter: (a, b) => STATUS_ORDER.indexOf(a) - STATUS_ORDER.indexOf(b),
  formatter: (cell) => {
    const row = cell.getData();
    return h(
      'span',
      {class: 'status'},
      h('span', {class: `pill ${statusClass(row.status)}`}, row.status),
      row.changed && h('span', {class: 'changed', text: row.changed}),
    );
  },
  tooltip: (event, cell) => cell.getData().reason,
};
