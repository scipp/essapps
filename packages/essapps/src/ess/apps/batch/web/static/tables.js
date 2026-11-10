// A table of rows by `key`, on Tabulator. A click selects a row, Ctrl or
// Shift and a click add to the selection, the box at the left toggles a row.
// A double-click edits a cell: Enter writes it, Escape drops it, Tab writes it
// and goes on to the next cell. Editing a cell of a row that is part of a
// selection of several writes that cell to every row selected.
import {TabulatorFull as Tabulator} from './vendor/tabulator_esm.min.js';
import {h} from './dom.js';

// A column: {name, title, editor, default} as the server sends it, with
// `editor` one of {type: 'select', options}, {type: 'number'}, {type: 'text'}.
function editorOf(column) {
  switch (column.editor?.type) {
    case 'select':
      return {
        editor: 'list',
        editorParams: {values: column.editor.options, autocomplete: true, listOnEmpty: true, clearable: true},
      };
    case 'number':
      return {editor: 'number', editorParams: {step: column.editor.step, selectContents: true}};
    case 'text':
      return {editor: 'input', editorParams: {selectContents: true}};
    default:
      return {};
  }
}

// What a cell shows: its text, or the default in grey; marked where it was
// set by hand, with a button back to what the scan gave, or where it is the
// reason the row cannot be reduced.
function cellFormatter(column, revert) {
  return (cell) => {
    const row = cell.getData();
    const problem = row.problems?.[column.name];
    const edited = row.edited.includes(column.name);
    cell.getElement().classList.toggle('bad', Boolean(problem));
    cell.getElement().classList.toggle('edited', edited);
    const text = row[`${column.name}:text`];
    const shown = text
      ? h('span', {class: 'text', text})
      : h('span', {class: problem ? 'text' : 'text muted', text: problem || column.default || '—'});
    const scanned = row[`${column.name}:scanned`] || 'nothing';
    return h(
      'span',
      {class: 'cell'},
      shown,
      edited &&
        h('button', {
          type: 'button',
          class: 'revert',
          title: `Set by hand. Back to what the runs give: ${scanned}`,
          'aria-label': `Back to ${scanned}`,
          text: '↺',
          onclick: (event) => {
            event.stopPropagation();
            revert(row.key, column.name);
          },
        }),
    );
  };
}

function cellTooltip(column) {
  return (event, cell) => {
    const row = cell.getData();
    if (row.problems?.[column.name]) return `${column.title}: ${row.problems[column.name]}`;
    if (row.edited.includes(column.name)) return `Set by hand; the runs give ${row[`${column.name}:scanned`] || 'nothing'}`;
    return '';
  };
}

const STATUS_ORDER = ['failed', 'incomplete', 'outdated', 'cancelled', 'ready', 'running', 'done'];

export const statusColumn = {
  title: 'Status',
  field: 'status',
  width: 116,
  sorter: (a, b) => STATUS_ORDER.indexOf(a) - STATUS_ORDER.indexOf(b),
  formatter: (cell) => h('span', {class: `pill st-${cell.getValue()}`, text: cell.getValue()}),
  tooltip: (event, cell) => cell.getData().reason || '',
};

export class Grid {
  constructor(element, {columns, edit, revert, select, placeholder}) {
    this.editing = false;
    this.pending = null;
    this.select = select;
    // The selection before the first click of a double-click, which that
    // click replaces.
    this.before = {keys: [], at: 0};
    element.addEventListener(
      'mousedown',
      (event) => {
        if (event.detail < 2) this.before = {keys: this.selected(), at: 0};
        else this.before.at = Date.now();
      },
      true,
    );
    const box = (checked, onclick) =>
      h('input', {type: 'checkbox', checked, 'aria-label': 'Select', onclick});
    this.table = new Tabulator(element, {
      index: 'key',
      height: '100%',
      layout: 'fitColumns',
      selectableRows: true,
      selectableRowsRangeMode: 'click',
      editTriggerEvent: 'dblclick',
      placeholder: h('div', {class: 'empty', text: placeholder}),
      rowHeader: {
        headerSort: false,
        resizable: false,
        width: 36,
        hozAlign: 'center',
        formatter: (cell) =>
          box(cell.getRow().isSelected(), (event) => {
            event.stopPropagation();
            cell.getRow().toggleSelect();
          }),
        titleFormatter: () =>
          box(false, (event) => {
            event.stopPropagation();
            if (event.target.checked) this.table.selectRow('active');
            else this.table.deselectRow();
          }),
      },
      columns: columns.map((c) =>
        c.name
          ? {
              title: c.title,
              field: c.name,
              minWidth: 150,
              headerSort: false,
              formatter: cellFormatter(c, revert),
              tooltip: cellTooltip(c),
              ...editorOf(c),
            }
          : c,
      ),
    });
    this.built = new Promise((resolve) => this.table.on('tableBuilt', resolve));
    this.table.on('rowSelectionChanged', (data, rows, selected, deselected) => {
      for (const row of [...selected, ...deselected]) {
        const box = row.getElement().querySelector('.tabulator-row-header input');
        if (box) box.checked = row.isSelected();
      }
      this.select(this.selected());
    });
    this.table.on('cellEditing', (cell) => {
      this.editing = true;
      const key = cell.getRow().getData().key;
      const clicked = Date.now() - this.before.at < 1000;
      const keys = clicked ? this.before.keys : this.selected();
      this.editKeys = keys.length > 1 && keys.includes(key) ? keys : [key];
      if (this.editKeys.length > 1) this.table.selectRow(this.editKeys);
    });
    const done = () => {
      this.editing = false;
      if (this.pending) this.update(this.pending);
    };
    this.table.on('cellEditCancelled', done);
    this.table.on('cellEdited', (cell) => {
      const value = cell.getValue();
      edit(this.editKeys, cell.getField(), value === '' ? null : value);
      done();
    });
  }

  selected() {
    return this.table.getSelectedData().map((row) => row.key);
  }

  // Show `rows`, keeping the selection, but not while a cell is edited.
  async update(rows) {
    await this.built;
    if (this.editing) {
      this.pending = rows;
      return;
    }
    this.pending = null;
    const keys = this.selected();
    await this.table.replaceData(rows);
    const kept = keys.filter((key) => this.table.getRow(key));
    if (kept.length) this.table.selectRow(kept);
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
