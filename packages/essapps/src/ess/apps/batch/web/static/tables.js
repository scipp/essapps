// A table of rows by `key`, on Tabulator. The boxes at the left select rows,
// and nothing else does. A click on a cell that can be edited edits it: the
// editor opens at what the cell holds, Enter or leaving writes, Escape, a
// value left as it was or an empty cell left empty writes nothing, Tab writes
// and goes on. An edit of a
// row that is selected along with others writes that cell to every row
// selected.
import {TabulatorFull as Tabulator} from './vendor/tabulator_esm.min.js';
import {h} from './dom.js';

const sameSet = (a, b) => a.length === b.length && a.every((x) => b.includes(x));

// What an empty cell stands for, in words.
const empty = (column) => (column.default ? `the default, ${column.default}` : 'empty');

// The options of a pick that hold `text`, ignoring case; all for no text.
const matching = (options, text) =>
  options.filter((o) => o.toLowerCase().includes(text.trim().toLowerCase()));

// A list of options under an editor of a pick: what is marked is what the
// editor writes. Typing filters it and marks the first option left.
class Choices {
  constructor(options, at, pick) {
    this.options = options;
    this.shown = options;
    this.marked = -1;
    this.element = h('ul', {class: 'choices', role: 'listbox'});
    // A press in the list must not take the focus from the editor.
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

  // Show the options that hold `text`, or all marking `current`.
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

// The editor of a cell, as an input that opens at what the cell holds, all
// of it selected so that typing replaces it. Enter, Tab or leaving writes;
// Escape, text left as it was, or an empty cell left empty writes nothing.
// A pick shows its options below. `say` tells why a value is not taken.
function cellEditor(column, say) {
  const {type, options} = column.editor;
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
        choices = new Choices(options, cell.getElement(), (o) => close(true, o));
        choices.show('', held.value);
      }
      // A row edited scrolls into view only if less than half of it shows.
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
    // Tab is left to the table, which opens the next cell and so ends this.
    input.addEventListener('blur', () => close(true));
    return input;
  };
}

const editorOf = (column, say) =>
  ['select', 'number', 'text'].includes(column.editor?.type)
    ? {editor: cellEditor(column, say)}
    : {editable: false};

// What a cell shows: its text, or what empty stands for in grey; marked where
// it was set by hand, with a button back to what it was, or where it is why
// the row cannot be reduced.
function cellFormatter(column, revert) {
  return (cell) => {
    const data = cell.getData();
    const c = data.cells[column.name];
    const element = cell.getElement();
    element.classList.toggle('bad', Boolean(c.problem));
    element.classList.toggle('edited', c.edited);
    const shown = c.text
      ? h('span', {class: 'text', text: c.text})
      : h('span', {class: 'text muted', text: c.problem ? 'missing' : column.default || '—'});
    const back = c.scanned || empty(column);
    return h(
      'span',
      {class: 'cell'},
      shown,
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
    const c = cell.getData().cells[column.name];
    if (c.problem) return c.problem;
    if (c.edited) return `${c.text || empty(column)}, set by hand; it was ${c.scanned || empty(column)}`;
    return c.text;
  };
}

// A column for each cell, given {name, title, editor, default} as the server
// sends them.
export function cellColumns(columns, {revert, say}) {
  return columns.map((column) => ({
    title: column.title,
    headerTooltip: column.title,
    field: `cells.${column.name}.value`,
    minWidth: 96,
    headerSort: false,
    cssClass: column.editor ? `editable ${column.editor.type}` : '',
    formatter: cellFormatter(column, revert),
    tooltip: cellTooltip(column),
    ...editorOf(column, say),
  }));
}

export class Grid {
  // `columns` are Tabulator's; `edits` maps the field of each editable
  // column to its column as the server sends it. Where `edit` answers false,
  // or a promise of false, the cell shows its value before again.
  constructor(element, {columns, edits, edit, select, sorted, placeholder, sort = []}) {
    this.element = element;
    this.onSelect = select;
    this.shown = [];
    this.json = new Map(); // each row as last shown
    this.pending = null;
    this.quiet = false;
    this.table = new Tabulator(element, {
      index: 'key',
      height: '100%',
      layout: 'fitColumns',
      // Rows drawn all at once: a few hundred are cheap, and the virtual
      // renderer scrolls a row clicked into view even if it shows nearly all.
      renderVertical: 'basic',
      selectableRows: 'highlight',
      editTriggerEvent: 'click',
      initialSort: sort,
      placeholder: h('div', {class: 'empty', text: placeholder}),
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
      const column = edits[cell.getField()];
      const key = cell.getRow().getData().key;
      const selected = this.selected();
      const keys = selected.length > 1 && selected.includes(key) ? selected : [key];
      Promise.resolve(edit(keys, column.name, cell.getValue())).then((done) => done === false && cell.restoreOldValue());
      this.closed();
    });
    this.table.on('cellEditCancelled', () => this.closed());
  }

  // Space below the last row, so that the rows scroll by whole rows to
  // the end, where the top row then shows whole.
  fit() {
    const holder = this.table.rowManager.element;
    const rows = holder.querySelector('.tabulator-table');
    const height = rows?.querySelector('.tabulator-row')?.offsetHeight;
    if (!height) return;
    rows.style.paddingBottom = '0px';
    const over = rows.offsetHeight - holder.clientHeight;
    rows.style.paddingBottom = over > 0 ? `${(height - (over % height)) % height}px` : '0px';
  }

  selected() {
    return this.table.getSelectedData().map((row) => row.key);
  }

  // Tell `select` the keys selected, if they changed.
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
    setTimeout(() => this.pending && this.update(this.pending));
  }

  // Show `rows`, keeping the selection, but not while a cell is edited.
  update(rows) {
    this.updated = this.draw(rows);
    return this.updated;
  }

  // Scroll to the row of `key`, once shown, if the filter shows it.
  async reveal(key) {
    await this.updated;
    await this.table.scrollToRow(key, 'nearest', false).catch(() => {});
  }

  async draw(rows) {
    await this.built;
    this.pending = rows;
    if (this.editing()) return;
    this.pending = null;
    const keys = rows.map((row) => row.key);
    const before = this.table.getData().map((row) => row.key);
    const json = new Map(rows.map((row) => [row.key, JSON.stringify(row)]));
    this.quiet = true;
    try {
      if (keys.length === before.length && keys.every((k, i) => k === before[i])) {
        // Draw again only the rows that changed, each whole: a cell shows
        // more than the value Tabulator compares.
        const changed = rows.filter((row) => this.json.get(row.key) !== json.get(row.key));
        if (changed.length) await this.table.updateData(changed);
        for (const row of changed) this.table.getRow(row.key).reformat();
      } else {
        const kept = this.selected().filter((key) => keys.includes(key));
        await this.table.replaceData(rows);
        if (kept.length) this.table.selectRow(kept);
      }
    } finally {
      this.quiet = false;
    }
    this.json = json;
    this.announce();
  }

  async select(keys) {
    await this.built;
    const known = new Set(this.table.getData().map((row) => row.key));
    this.table.selectRow(keys.filter((key) => known.has(key)));
  }

  // Show only the rows of `status`, and select none that is hidden.
  async filter(status) {
    await this.built;
    if (status === 'all') this.table.clearFilter();
    else this.table.setFilter('status', '=', status);
    const shown = new Set(this.table.getRows('active').map((row) => row.getData().key));
    const hidden = this.selected().filter((key) => !shown.has(key));
    if (hidden.length) this.table.deselectRow(hidden);
  }
}

const STATUS_ORDER = ['failed', 'incomplete', 'outdated', 'cancelled', 'ready', 'running', 'done'];

// The status of a row as a pill, with a star if its result was reduced alone
// with its own parameters; the tooltip says why.
export const statusColumn = {
  title: 'Status',
  field: 'status',
  width: 112,
  minWidth: 112,
  resizable: false,
  sorter: (a, b) => STATUS_ORDER.indexOf(a) - STATUS_ORDER.indexOf(b),
  formatter: (cell) => {
    const row = cell.getData();
    return h('span', {class: `pill st-${row.status}`}, row.status, row.alone && h('sup', {text: '*'}));
  },
  tooltip: (event, cell) => {
    const row = cell.getData();
    const alone = row.alone ? 'Reduced alone, with its own parameters.' : '';
    return [row.reason, alone].filter(Boolean).join(' ');
  },
};
