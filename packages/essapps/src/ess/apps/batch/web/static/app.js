// The batch page. The server holds the rows, the settings, and the results;
// this file shows the state it sends and sends what a person does. Each
// action answers with the state after it; a poll every second sees results
// arrive. The page keeps only what it shows: the selection, the filter, the
// tab, the sort, and the axes, which survive a reload.
import {h, get, post, upload, query, save} from './dom.js';
import {Grid, cellColumns, statusColumn} from './tables.js';
import {renderForm, summary} from './form.js';

const $ = (id) => document.getElementById(id);

const KEPT = 'batch-view';
const view = {
  state: null,
  filter: 'all',
  tab: 'result',
  page: 'samples',
  selected: [],
  sort: [],
  log: {x: true, y: true},
  ...remembered(),
  plotted: null, // what the plot shows, to fetch it again only on a change
  plots: 0, // the number of the last plot asked for, to drop older answers
  zoom: 1, // a new value forgets the zoom of the plot; Plotly ignores 0
  colors: {}, // the colour of each curve drawn, by key
  detail: '', // what the row tab shows, to draw it again only on a change
  dismissed: '', // the refusal whose message was closed
  runs: 0, // the version of the runs shown
  tweakOpen: false,
};
let rows; // the Grid of the rows
let runs; // the Grid of the runs

function remembered() {
  try {
    return JSON.parse(sessionStorage.getItem(KEPT)) ?? {};
  } catch {
    return {};
  }
}

function remember() {
  const {filter, tab, page, selected, sort, log} = view;
  try {
    sessionStorage.setItem(KEPT, JSON.stringify({filter, tab, page, selected, sort, log}));
  } catch {
    // A page that cannot store forgets at a reload, nothing more.
  }
}

const words = () => view.state?.words ?? {one: 'row', many: 'rows'};
const title = (word) => word[0].toUpperCase() + word.slice(1);
const count = (n) => `${n} ${n === 1 ? words().one : words().many}`;

// Talking to the server

// Send an action and show the state it answers with, or what went wrong;
// whether it was done.
async function act(url, body) {
  let response;
  try {
    response = await post(url, body);
  } catch {
    showMessage('The server does not answer, so nothing was done. Try again when it is back.');
    return false;
  }
  if (response.ok) {
    hideMessage();
    render(response.body);
    if (response.body.notice) toast(response.body.notice);
    return true;
  }
  showMessage(response.body?.message ?? `The server answered ${response.status}.`);
  await refresh().catch(() => {});
  return false;
}

async function download(url, name) {
  const response = await get(url);
  if (response.ok) save(response.body, name);
  else showMessage(response.body?.message ?? `The server answered ${response.status}.`);
}

class Offline extends Error {}

async function refresh() {
  const at = view.state ? {after: view.state.version, start: view.state.start} : {};
  let response;
  try {
    response = await get(`/api/state?${query(at)}`);
  } catch {
    throw new Offline('The server does not answer.');
  }
  if (response.status === 204) return;
  if (!response.ok) throw new Offline(`The server answered ${response.status}.`);
  render(response.body);
}

// Ask for the state every second; say so while the server does not give it.
async function poll() {
  try {
    await refresh();
    $('offline').hidden = true;
  } catch (error) {
    if (!(error instanceof Offline)) throw error;
    $('offline').textContent = `${error.message} The page tries again every second; what it shows may be old.`;
    $('offline').hidden = false;
  } finally {
    setTimeout(poll, 1000);
  }
}

// Messages

let shownMessage = '';
function showMessage(title, items = [], key = '') {
  $('message').querySelector('.message-text').replaceChildren(
    h('div', {}, h('strong', {text: title}), items.length > 0 && h('ul', {}, items.map((text) => h('li', {text})))),
  );
  shownMessage = key;
  $('message').hidden = false;
}

function hideMessage() {
  $('message').hidden = true;
  shownMessage = '';
}

let toastTimer;
function toast(text) {
  const box = $('toast');
  box.textContent = text;
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (box.hidden = true), 3500);
}

// Showing the state

function render(state) {
  const old = view.state;
  if (old && state.start !== old.start) return location.reload();
  if (old && state.version < old.version) return;
  view.state = state;
  if (!old) build(state);
  document.title = `${state.title} · batch`;
  $('title').textContent = state.title;
  $('folder').textContent = state.folder;
  $('n-samples').textContent = state.counts.all;
  renderAction(state.action);
  renderChips(state.counts);
  if (old?.action.running && !state.action.running) {
    const ended = ['done', 'failed', 'cancelled'].filter((s) => state.counts[s]).map((s) => `${state.counts[s]} ${s}`);
    toast(`Reducing ended: ${ended.join(', ')}.`);
  }
  const updated = rows.update(state.rows);
  if (!old) updated.then(() => rows.select(view.selected));
  if (state.runs !== null && state.runs !== view.runs) loadRuns();
  const many = state.words.many;
  $('rescan').title = state.loaded
    ? `Make the ${many} from the runs in place of the table you loaded, ${state.loaded}. Values set by hand stay.`
    : `Make the ${many} from the runs again. Values set by hand stay.`;
  $('restore').hidden = state.removed === 0;
  $('restore').textContent = `${state.removed} removed: bring back`;
  renderRefused(state.refused);
  renderSettings(state);
  renderSelection();
  if (view.tab === 'row') loadRow();
}

function build(state) {
  const {one, many} = state.words;
  $('word-many').textContent = title(many);
  $('tab-row-label').textContent = title(one);
  $('add-row').textContent = `+ Add ${one}`;
  $('settings-hint').textContent = `Shared by every ${one}. A change marks the results reduced with other settings as outdated.`;
  const named = {
    title: 'Name',
    field: 'name',
    minWidth: 120,
    widthGrow: 2,
    tooltip: (event, cell) => cell.getValue(),
  };
  const info = state.info.map((c) => ({title: c.title, field: `info.${c.name}`, minWidth: 96, headerSort: false}));
  const cells = state.columns;
  rows = new Grid($('rows'), {
    columns: [named, ...info, ...cellColumns(cells, {revert: (key, column) => act('/api/cells/revert', {keys: [key], column}), say: showMessage}), statusColumn],
    edits: Object.fromEntries(cells.map((c) => [`cells.${c.name}.value`, c])),
    placeholder: `No ${many}. Rescan the runs, or load a table from the Files menu.`,
    edit: (keys, column, value) => act('/api/cells', {keys, column, value}),
    select: (keys) => {
      view.selected = keys;
      remember();
      renderSelection();
      if (view.tab === 'row') loadRow();
    },
    sorted: (sort) => {
      view.sort = sort;
      remember();
    },
    sort: view.sort,
  });
  rows.filter(view.filter);
  if (state.runs === null) document.querySelector('[data-view="runs"]').hidden = true;
  showPage(state.runs === null ? 'samples' : view.page);
  showTab(view.tab);
}

// The results of `rows` as a zip, if the person wants outdated ones.
function downloadResults(rows, name) {
  const done = rows.filter((row) => row.result);
  const old = done.filter((row) => row.result === 'outdated').length;
  const question = `${old} of the ${done.length} results are outdated: the settings or their cells changed since. Download them anyway?`;
  if (old && !confirm(question)) return;
  download(`/api/results.zip?${query({keys: done.map((row) => row.key)})}`, name);
}

// Whether a rescan may go on: it replaces a table loaded, if asked.
function replacesTable() {
  const {loaded} = view.state;
  return !loaded || confirm(`The table you loaded, ${loaded}, is replaced by what the runs give. Values set by hand stay.`);
}

async function loadRuns() {
  const response = await get('/api/runs');
  if (!response.ok) return;
  const shown = response.body;
  view.runs = shown.version;
  $('n-runs').textContent = shown.rows.length;
  if (!runs) {
    const fields = shown.columns.map((c) => c.title.toLowerCase()).join(' or ');
    $('runs-hint').textContent = `Correct the ${fields} of a run. The ${words().one} table follows at once.`;
    runs = new Grid($('runs'), {
      columns: [
        {title: 'Run', field: 'file', minWidth: 160, widthGrow: 2},
        ...cellColumns(shown.columns, {
          revert: (key, column) => replacesTable() && act('/api/runs/cells/revert', {keys: [key], column}),
          say: showMessage,
        }),
      ],
      edits: Object.fromEntries(shown.columns.map((c) => [`cells.${c.name}.value`, c])),
      placeholder: 'No runs in this folder.',
      // A correction rescans, so it may replace a table loaded.
      edit: (keys, column, value) => replacesTable() && act('/api/runs/cells', {keys, column, value}),
      select: () => {},
    });
  }
  runs.update(shown.rows);
}

function renderAction(action) {
  const button = $('reduce');
  const running = action.progress !== null;
  const [finished, started] = action.progress ?? [0, 0];
  button.querySelector('.label').textContent = running
    ? `Reducing: ${finished} of ${started} done`
    : action.label;
  button.querySelector('.bar').style.width = running ? `${(100 * finished) / started}%` : '0';
  button.classList.toggle('running', running);
  button.disabled = running || action.remaining === 0 || Boolean(action.blocked);
  button.title = action.blocked ? `${action.blocked}. See the Settings tab.` : '';
  $('cancel').disabled = action.running === 0;
  $('cancel').classList.toggle('danger', action.running > 0);
  $('n-remaining').textContent = action.remaining;
  $('n-again').textContent = action.again;
  for (const which of ['remaining', 'all']) {
    document.querySelector(`[data-reduce="${which}"]`).disabled =
      Boolean(action.blocked) || (which === 'remaining' ? action.remaining : action.again) === 0;
  }
  $('reduce-more').disabled = Boolean(action.blocked);
  $('settings-badge').hidden = !action.blocked;
}

const CHIPS = ['all', 'ready', 'running', 'done', 'outdated', 'failed', 'incomplete', 'cancelled'];

function renderChips(counts) {
  const shown = CHIPS.filter((s) => s === 'all' || counts[s] > 0 || s === view.filter);
  $('chips').replaceChildren(
    ...shown.map((status) =>
      h(
        'button',
        {
          type: 'button',
          class: `chip st-${status}`,
          'aria-pressed': String(view.filter === status),
          onclick: () => {
            view.filter = view.filter === status ? 'all' : status;
            remember();
            rows.filter(view.filter);
            renderChips(view.state.counts);
          },
        },
        h('span', {class: 'dot'}),
        h('span', {text: status}),
        h('span', {class: 'n', text: counts[status]}),
      ),
    ),
  );
}

// The reasons of a refused reduction, until closed or the next change.
function renderRefused(refused) {
  const key = JSON.stringify(refused);
  if (!refused.length) {
    if (shownMessage) hideMessage();
    return;
  }
  if (key === view.dismissed || key === shownMessage) return;
  showMessage('Nothing was reduced.', refused.map((e) => e.text), key);
}

let shownSettings = '';
function renderSettings(state) {
  const json = JSON.stringify(state.settings);
  if (json === shownSettings) return;
  shownSettings = json;
  renderForm($('settings'), state.settings, (edit) => act('/api/settings', edit));
}

function selectedRows() {
  return view.state.rows.filter((row) => view.selected.includes(row.key));
}

function renderSelection() {
  const n = view.selected.length;
  const done = view.state.rows.filter((row) => row.result);
  $('selection').textContent = n ? `${n} selected` : '';
  $('remove').hidden = n === 0;
  $('n-selected').textContent = n;
  document.querySelector('[data-reduce="selected"]').disabled = n === 0 || Boolean(view.state.action.blocked);
  const doneSelected = selectedRows().filter((row) => row.result);
  const said = (rows) => {
    const old = rows.filter((row) => row.result === 'outdated').length;
    return old ? `${rows.length}, ${old} outdated` : String(rows.length);
  };
  $('n-results-selected').textContent = said(doneSelected);
  $('n-results-all').textContent = said(done);
  document.querySelector('[data-file="results-selected"]').disabled = doneSelected.length === 0;
  document.querySelector('[data-file="results-all"]').disabled = done.length === 0;
  renderPlot();
}

// The result

const PLOT_LAYOUT = {
  margin: {l: 64, r: 12, t: 8, b: 44},
  paper_bgcolor: 'rgba(0,0,0,0)',
  plot_bgcolor: 'rgba(0,0,0,0)',
  font: {family: 'system-ui, -apple-system, "Segoe UI", sans-serif', size: 12, color: '#2b2f36'},
  hovermode: 'closest',
  hoverlabel: {font: {size: 12}},
};
const AXIS = {gridcolor: '#eceef1', linecolor: '#8a919c', zeroline: false, tickfont: {size: 11}};
const PLOT_CONFIG = {
  displaylogo: false,
  responsive: true,
  modeBarButtons: [['zoom2d', 'pan2d', 'resetScale2d', 'toImage']],
};

function axis(given, log) {
  if (log) return {...given, ...AXIS};
  const {dtick, minor, ...rest} = given;
  return {...rest, ...AXIS, type: 'linear', exponentformat: 'e'};
}

async function renderPlot() {
  const chosen = view.selected.length ? selectedRows() : view.state.rows;
  const drawn = chosen.filter((row) => row.result).map((row) => [row.key, row.record, row.result, row.name]);
  const what = JSON.stringify([view.selected, drawn, view.log, view.zoom]);
  renderLegend();
  if (what === view.plotted) return;
  view.plotted = what;
  const asked = ++view.plots;
  const response = await get(`/api/plot?${query({keys: view.selected})}`);
  if (asked !== view.plots || !response.ok) return;
  const {figure, note, colors} = response.body;
  view.colors = colors;
  renderLegend();
  const quantity = figure?.layout?.meta?.quantity ?? 'Result';
  const n = view.selected.length;
  $('plot-title').textContent = n ? `${quantity} of the ${count(n)} selected` : `${quantity} of the ${words().many} done`;
  if (!figure) $('plot-title').textContent = 'Results';
  document.querySelector('.axes').hidden = !figure;
  $('plot-note').textContent = figure ? note : '';
  $('plot-empty').hidden = Boolean(figure);
  $('plot').hidden = !figure;
  if (!figure) {
    $('plot-empty').textContent = note;
    return;
  }
  const layout = {
    ...figure.layout,
    ...PLOT_LAYOUT,
    showlegend: false,
    uirevision: view.zoom,
    xaxis: axis(figure.layout.xaxis, view.log.x),
    yaxis: axis(figure.layout.yaxis, view.log.y),
  };
  Plotly.react($('plot'), figure.data, layout, PLOT_CONFIG);
}

// The legend of the plot, which also says why rows have no curve or an
// outdated one: a line per row, with the colour of its curve, dashed if
// outdated, and its status where not done; above it, a count per status.
function renderLegend() {
  const shown = view.selected.length ? selectedRows() : view.state.rows.filter((row) => row.key in view.colors);
  const groups = Map.groupBy(shown.filter((row) => row.status !== 'done'), (row) => row.status);
  $('legend-summary').replaceChildren(
    ...[...groups].map(([status, group]) => {
      const reasons = new Set(group.map((row) => row.reason));
      const [reason] = reasons;
      const text = reasons.size === 1 && reason ? `${group.length} ${status}: ${reason}` : `${group.length} ${status}`;
      return h('span', {text});
    }),
  );
  $('legend').replaceChildren(
    ...shown.map((row) => {
      const color = view.colors[row.key];
      const dashed = row.result === 'outdated' ? ' dashed' : '';
      return h(
        'li',
        {title: [row.name, row.reason].filter(Boolean).join(': ')},
        h('span', {class: `swatch${color ? dashed : ' none'}`, style: color ? `--color: ${color}` : null}),
        h('span', {class: 'name', text: row.name}),
        row.status !== 'done' && h('span', {class: `pill st-${row.status}`, text: row.status}),
      );
    }),
  );
}

// One row

async function loadRow() {
  const pane = $('tab-row');
  const row = view.selected.length === 1 && view.state.rows.find((r) => r.key === view.selected[0]);
  if (!row) {
    view.detail = '';
    const text = view.selected.length > 1
      ? `Select only one ${words().one} to see its details.`
      : `Select a ${words().one} to see what it was reduced with, or why it failed.`;
    pane.replaceChildren(h('div', {class: 'empty', text}));
    return;
  }
  const response = await get(`/api/row?${query({key: row.key})}`);
  if (response.ok && view.selected[0] === row.key) renderRow(response.body);
}

function renderRow(detail) {
  const row = view.state.rows.find((r) => r.key === detail.key);
  if (!row) return;
  const json = JSON.stringify([detail, row, view.state.columns]);
  if (json === view.detail) return;
  view.detail = json;
  const tried = row.status === 'failed' || row.status === 'cancelled';
  const reasonClass = {failed: 'bad', cancelled: 'bad', incomplete: 'warn', outdated: 'info'}[row.status];
  const useSettings = () => {
    const question = `Replace the settings of every ${words().one} with those that ${row.name} was reduced with? The settings you have now are lost unless saved (Files, Save settings).`;
    if (confirm(question)) act('/api/row/use-settings', {key: row.key});
  };
  const settingsNames = Object.fromEntries(view.state.settings.map((n) => [n.path[0], n.label]));
  const parts = [
    h('div', {class: 'row-head'}, h('h2', {text: row.name}), h('span', {class: `pill st-${row.status}`, text: row.status})),
    h(
      'dl',
      {class: 'summary'},
      view.state.columns.map((c) => [h('dt', {text: c.title}), h('dd', {text: row.cells[c.name].text || c.default || '—'})]),
    ),
    row.reason && h('div', {class: `reason ${reasonClass ?? ''}`, text: row.reason}),
    row.alone && h('p', {class: 'hint', text: 'Its result was reduced alone, with its own parameters.'}),
    detail.reduced_with
      ? h(
          'section',
          {class: 'reduced-with'},
          h('h3', {text: `${tried ? 'Tried' : 'Reduced'} on ${when(detail.created)} with`}),
          detail.differs.length > 0 &&
            h('p', {class: 'hint differs', text: `Differs from the settings now in ${detail.differs.map((n) => settingsNames[n] ?? n).join(', ')}.`}),
          summary(detail.reduced_with, detail.differs),
          detail.differs.length > 0 &&
            h('button', {type: 'button', text: `Use these settings for every ${words().one}`, onclick: useSettings}),
        )
      : h('p', {class: 'hint', text: 'Not reduced yet.'}),
  ];
  // The form to reduce the row alone is kept while the row stays, and drawn
  // again in place only when it changed, so that typing in it goes on.
  const pane = $('tab-row');
  let info = pane.querySelector('.row-info');
  let tweak = pane.querySelector('details.tweak');
  if (!info || tweak?.dataset.key !== row.key) {
    info = h('div', {class: 'row-info'});
    tweak = tweakBox(row);
    pane.replaceChildren(info, tweak);
  }
  info.replaceChildren(...parts.filter(Boolean));
  const form = tweak.querySelector('.form');
  const tweakJson = JSON.stringify(detail.tweak);
  if (form.dataset.shown !== tweakJson) {
    form.dataset.shown = tweakJson;
    renderForm(form, detail.tweak, async (edit) => {
      const response = await post('/api/row/tweak', {key: row.key, ...edit});
      if (response.ok) renderRow(response.body);
      else showMessage(response.body?.message ?? `The server answered ${response.status}.`);
    });
  }
}

// A time as 2026-11-10 13:11, in the time zone of the browser.
function when(iso) {
  const t = new Date(iso);
  const two = (n) => String(n).padStart(2, '0');
  return `${t.getFullYear()}-${two(t.getMonth() + 1)}-${two(t.getDate())} ${two(t.getHours())}:${two(t.getMinutes())}`;
}

function tweakBox(row) {
  return h(
    'details',
    {class: 'tweak', 'data-key': row.key, open: view.tweakOpen, ontoggle: (event) => (view.tweakOpen = event.target.open)},
    h('summary', {text: `Reduce this ${words().one} alone with other parameters`}),
    h('p', {class: 'hint', text: `The result becomes the result of ${row.name}. The shared settings stay as they are.`}),
    h('div', {class: 'form'}),
    h('button', {type: 'button', class: 'primary', text: `Reduce ${row.name} alone`, onclick: () => act('/api/row/tweak/submit', {key: row.key})}),
  );
}

// Wiring

function openMenu(menu) {
  const items = menu.querySelector('.menu-items');
  const open = items.hidden;
  document.querySelectorAll('.menu-items').forEach((m) => (m.hidden = true));
  items.hidden = !open;
}

function showPage(page) {
  view.page = page;
  remember();
  document.querySelectorAll('[data-view]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.view === page)));
  $('view-samples').hidden = page !== 'samples';
  $('view-runs').hidden = page !== 'runs';
  document.querySelector('main').classList.toggle('wide', page === 'runs');
  const grid = page === 'runs' ? runs : rows;
  grid?.built.then(() => grid.table.redraw());
}

function showTab(tab) {
  view.tab = tab;
  remember();
  document.querySelectorAll('[data-tab]').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === tab)));
  for (const name of ['result', 'settings', 'row']) $(`tab-${name}`).hidden = name !== tab;
  if (tab === 'row' && view.state) loadRow();
  if (tab === 'result' && !$('plot').hidden) Plotly.Plots.resize($('plot'));
}

function wire() {
  // Plotly fits the plot to its box only when the window changes size; the
  // box also changes with the legend below it.
  const plot = $('plot');
  new ResizeObserver(() => plot.data && !plot.hidden && Plotly.Plots.resize(plot)).observe(plot);
  $('reduce').onclick = () => act('/api/reduce', {which: 'remaining'});
  $('reduce-more').onclick = () => openMenu($('reduce-split'));
  document.querySelectorAll('[data-reduce]').forEach((item) => {
    item.onclick = () => {
      $('reduce-split').querySelector('.menu-items').hidden = true;
      act('/api/reduce', {which: item.dataset.reduce, keys: view.selected});
    };
  });
  $('cancel').onclick = () => act('/api/cancel');
  $('rescan').onclick = () => replacesTable() && act('/api/scan');
  $('remove').onclick = () => {
    const added = selectedRows().filter((row) => row.added).length;
    const question = `${added} of the ${count(view.selected.length)} selected ${added === 1 ? 'was' : 'were'} added by hand and cannot be brought back. Remove?`;
    if (!added || confirm(question)) act('/api/rows/remove', {keys: view.selected});
  };
  $('restore').onclick = () => act('/api/rows/restore');
  document.querySelector('#message .close').onclick = () => {
    view.dismissed = shownMessage;
    hideMessage();
  };

  const addForm = $('add-form');
  const closeAdd = () => {
    addForm.hidden = true;
    $('add-row').hidden = false;
  };
  $('add-row').onclick = () => {
    addForm.hidden = false;
    $('add-row').hidden = true;
    addForm.key.value = '';
    addForm.key.focus();
  };
  addForm.onsubmit = (event) => {
    event.preventDefault();
    const key = addForm.key.value.trim();
    act('/api/rows/add', {key}).then((done) => done && rows.reveal(key));
    closeAdd();
  };
  addForm.querySelector('[data-cancel]').onclick = closeAdd;
  addForm.key.addEventListener('keydown', (event) => event.key === 'Escape' && closeAdd());

  document.querySelectorAll('[data-view]').forEach((button) => (button.onclick = () => showPage(button.dataset.view)));
  document.querySelectorAll('[data-tab]').forEach((button) => (button.onclick = () => showTab(button.dataset.tab)));
  document.querySelectorAll('[data-axis]').forEach((button) => {
    button.setAttribute('aria-pressed', String(view.log[button.dataset.axis]));
    button.onclick = () => {
      view.log[button.dataset.axis] = !view.log[button.dataset.axis];
      button.setAttribute('aria-pressed', String(view.log[button.dataset.axis]));
      view.zoom += 1;
      remember();
      renderPlot();
    };
  });

  const files = $('files');
  files.querySelector('.menu-button').onclick = () => openMenu(files);
  const loadTable = () => {
    const {many} = words();
    const question = `Load a table file? Its ${many} take the place of those the runs give, values set by hand included. ${title(many)} you added by hand stay. Rescan runs to go back.`;
    if (confirm(question)) $('pick-table').click();
  };
  const actions = {
    'load-table': loadTable,
    'load-settings': () => $('pick-settings').click(),
    'table.csv': () => download('/api/table.csv', `${words().many}.csv`),
    'table.xlsx': () => download('/api/table.xlsx', `${words().many}.xlsx`),
    'settings.yaml': () => download('/api/settings.yaml', 'settings.yaml'),
    'results-selected': () => downloadResults(selectedRows(), 'results-selected.zip'),
    'results-all': () => downloadResults(view.state.rows, 'results-all.zip'),
  };
  files.querySelectorAll('[data-file]').forEach((item) => {
    item.onclick = () => {
      files.querySelector('.menu-items').hidden = true;
      actions[item.dataset.file]();
    };
  });
  for (const [input, url] of [[$('pick-table'), '/api/table'], [$('pick-settings'), '/api/settings.yaml']]) {
    input.onchange = async () => {
      if (!input.files.length) return;
      const file = input.files[0];
      input.value = '';
      const response = await upload(url, file);
      if (response.ok) {
        hideMessage();
        render(response.body);
        toast(response.body.notice);
      } else {
        showMessage(response.body?.message ?? `The server answered ${response.status}.`);
      }
    };
  }

  document.addEventListener('click', (event) => {
    if (!event.target.closest('.menu, .split')) {
      document.querySelectorAll('.menu-items').forEach((m) => (m.hidden = true));
    }
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') document.querySelectorAll('.menu-items').forEach((m) => (m.hidden = true));
  });
}

wire();
poll();
