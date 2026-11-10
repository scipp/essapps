// A table of rows by `key`, on Tabulator. The boxes at the left select rows,
// and nothing else does. A click on a cell that can be edited edits it: the
// editor opens at what the cell shows, Enter or leaving writes, Escape or a
// value left as it was writes nothing, Tab writes and goes on. An edit of a
// row that is selected along with others writes that cell to every row
// selected.
import {TabulatorFull as Tabulator} from './vendor/tabulator_esm.min.js';
import {h} from './dom.js';

const sameSet = (a, b) => a.length === b.length && a.every((x) => b.includes(x));

// What an empty cell stands for, in words.
const empty = (column) => (column.default ? `the default, ${column.default}` : 'empty');

// An editor of text or of a number. `say` tells why a value is not taken.
function inputEditor(column, say) {
  const number = column.editor.type === 'number';
  return (cell, onRendered, success, cancel) => {
    const start = cell.getData().cells[column.name].text || column.default || '';
    const input = h('input', {
      type: 'text',
      class: 'cell-editor',
      value: start,
      inputmode: number ? 'decimal' : null,
      spellcheck: 'false',
      'aria-label': column.title,
    });
    onRendered(() => {
      input.focus();
      input.select();
    });
    let open = true;
    const close = (write) => {
      if (!open) return;
      open = false;
      const text = input.value.trim();
      if (!write || text === start) return cancel();
      if (text === '') return success(null);
      if (number && !Number.isFinite(Number(text))) {
        say(`${column.title}: ${text} is not a number, so nothing was written.`);
        return cancel();
      }
      return success(number ? Number(text) : text);
    };
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') close(true);
      if (event.key === 'Escape') close(false);
    });
    input.addEventListener('blur', () => close(true));
    return input;
  };
}

function editorOf(column, say) {
  switch (column.editor?.type) {
    case 'select':
      return {
        editor: 'list',
        editorParams: {
          values: column.editor.options,
          autocomplete: true,
          freetext: true,
          allowEmpty: true,
          listOnEmpty: true,
          clearable: true,
        },
      };
    case 'number':
    case 'text':
      return {editor: inputEditor(column, say)};
    default:
      return {editable: false};
  }
}

// The option that typed text stands for: itself, or the one option that
// holds it, ignoring case.
function option(column, text) {
  if (!text || column.editor.options.includes(text)) return text;
  const found = column.editor.options.filter((o) => o.toLowerCase().includes(text.toLowerCase()));
  return found.length === 1 ? found[0] : undefined;
}

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
  // column to its column as the server sends it.
  constructor(element, {columns, edits, edit, select, sorted, say, placeholder, sort = []}) {
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
    this.table.on('rowSelectionChanged', () => this.quiet || this.announce());
    this.table.on('dataSorted', (sorters) =>
      sorted?.(sorters.map((s) => ({column: s.field, dir: s.dir}))),
    );
    this.table.on('cellEdited', (cell) => {
      const column = edits[cell.getField()];
      const key = cell.getRow().getData().key;
      const selected = this.selected();
      const keys = selected.length > 1 && selected.includes(key) ? selected : [key];
      let value = cell.getValue();
      if (column.editor.type === 'select') {
        value = option(column, value);
        if (value === undefined) {
          say(`${column.title}: no option matches ${cell.getValue()}, so nothing was written.`);
          cell.restoreOldValue();
          return this.closed();
        }
      }
      edit(keys, column.name, value === '' ? null : value);
      this.closed();
    });
    this.table.on('cellEditCancelled', () => this.closed());
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
  async update(rows) {
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
