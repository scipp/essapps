// The batch page. The server holds the samples, the settings, and the
// results; this file only shows what GET /api/state gives, and sends what a
// person does. It keeps what is shown: the selection, the filter, the tab.
import {h, get, post, upload, query, save} from './dom.js';
import {Grid, statusColumn} from './tables.js';
import {renderForm, summary} from './form.js';

const $ = (id) => document.getElementById(id);

const view = {
  state: null,
  version: 0,
  filter: 'all',
  tab: 'result',
  selected: [],
  plotted: null, // what the plot shows, to fetch it again only on a change
  tweakOpen: false,
};
let rows; // the Grid of the samples
let runs; // the Grid of the runs

// Talking to the server

// Send an action; show its notice or its refusal, then the new state.
async function act(url, body) {
  report(await post(url, body));
  await refresh();
}

function report(response) {
  if (response.ok) {
    hideMessage();
    if (response.body?.notice) toast(response.body.notice);
    return;
  }
  const {message = 'Something went wrong.', errors = []} = response.body ?? {};
  const placed = errors.filter((e) => e.placed).length;
  const where = placed ? `${placed === 1 ? 'The value refused is' : `${placed} values refused are`} marked in red.` : '';
  showMessage(message, where, errors.filter((e) => !e.placed).map((e) => e.text));
}

async function download(url, name) {
  const response = await get(url);
  if (response.ok) save(response.body, name);
  else report(response);
}

async function refresh() {
  const response = await get(`/api/state?after=${view.version}`);
  if (response.ok && response.body.rows) render(response.body);
}

async function poll() {
  try {
    await refresh();
    $('message').classList.remove('offline');
  } catch {
    showMessage('The server does not answer.', 'The page tries again every second.', []);
    $('message').classList.add('offline');
  }
  setTimeout(poll, 1000);
}

// Messages

function showMessage(title, detail, items) {
  const box = $('message');
  box.querySelector('.message-text').replaceChildren(
    h(
      'div',
      {},
      h('strong', {text: title}),
      detail && ` ${detail}`,
      items.length > 0 && h('ul', {}, items.map((text) => h('li', {text}))),
    ),
  );
  box.hidden = false;
}

function hideMessage() {
  $('message').hidden = true;
}

let toastTimer;
function toast(text) {
  const box = $('toast');
  box.textContent = text;
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (box.hidden = true), 2500);
}

// Showing the state

function render(state) {
  const first = view.state === null;
  view.state = state;
  view.version = state.version;
  if (first) build(state);
  document.title = `${state.title} · batch`;
  $('title').textContent = state.title;
  $('folder').textContent = state.folder;
  $('n-samples').textContent = state.counts.all;
  $('n-runs').textContent = state.runs ? state.runs.rows.length : '';
  renderAction(state.action);
  renderChips(state.counts);
  rows.update(state.rows);
  if (runs) runs.update(state.runs.rows);
  $('removed').hidden = state.removed === 0;
  $('restore').textContent = `${state.removed} removed: bring back`;
  if (state.unplaced.length) showMessage('Nothing was reduced.', '', state.unplaced);
  renderSettings(state.settings);
  renderSelection();
  if (view.tab === 'row') loadRow();
}

function build(state) {
  const name = state.info.map((info) => ({
    title: info.title,
    field: info.name,
    minWidth: 180,
    widthGrow: 1.5,
    formatter: (cell) => cell.getValue() || cell.getData().key,
  }));
  rows = new Grid($('rows'), {
    columns: [...name, ...state.columns, statusColumn],
    placeholder: 'No samples. Rescan the runs, or load a table from the Files menu.',
    edit: (keys, column, value) => act('/api/cells', {keys, column, value}),
    revert: (key, column) => act('/api/cells/revert', {keys: [key], column}),
    select: (keys) => {
      view.selected = keys;
      renderSelection();
      if (view.tab === 'row') loadRow();
    },
  });
  if (state.runs) {
    runs = new Grid($('runs'), {
      columns: [{title: 'Run', field: 'file', width: 120}, ...state.runs.columns],
      placeholder: 'No runs in this folder.',
      edit: (keys, column, value) => act('/api/runs/cells', {keys, column, value}),
      revert: (key, column) => act('/api/runs/cells/revert', {keys: [key], column}),
      select: () => {},
    });
  } else {
    document.querySelector('[data-view="runs"]').hidden = true;
  }
}

function renderAction(action) {
  const button = $('reduce');
  const running = action.progress !== null;
  button.querySelector('.label').textContent = running
    ? `Reducing ${action.progress[0]} of ${action.progress[1]}`
    : action.label;
  button.querySelector('.bar').style.width = running
    ? `${(100 * action.progress[0]) / action.progress[1]}%`
    : '0';
  button.classList.toggle('running', running);
  button.disabled = running || action.remaining === 0;
  $('cancel').disabled = action.running === 0;
  $('cancel').classList.toggle('danger', action.running > 0);
  $('n-ready').textContent = action.ready;
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

let shownSettings = '';
function renderSettings(nodes) {
  const json = JSON.stringify(nodes);
  if (json === shownSettings) return;
  shownSettings = json;
  renderForm($('settings'), nodes, (edit) => act('/api/settings', edit));
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
  document.querySelector('[data-reduce="selected"]').disabled = n === 0;
  const doneSelected = selectedRows().filter((row) => row.result).length;
  $('n-results-selected').textContent = doneSelected;
  $('n-results-all').textContent = done.length;
  document.querySelector('[data-file="results-selected"]').disabled = doneSelected === 0;
  document.querySelector('[data-file="results-all"]').disabled = done.length === 0;
  renderPlot();
}

// The result

const PLOT_LAYOUT = {
  margin: {l: 64, r: 12, t: 8, b: 44},
  paper_bgcolor: 'rgba(0,0,0,0)',
  plot_bgcolor: 'rgba(0,0,0,0)',
  font: {family: 'system-ui, -apple-system, "Segoe UI", sans-serif', size: 12, color: '#2b2f36'},
  colorway: ['#2f6fb3', '#d1495b', '#2e9d6c', '#e0902f', '#7a5bb5', '#1f9aa8', '#9c6b3f', '#c25a9c', '#5f6b7a', '#8aa832', '#b33f3f', '#3f5fb3'],
  // Below the axes, so that a dozen curves leave the data visible.
  legend: {orientation: 'h', x: 0, y: -0.14, xanchor: 'left', yanchor: 'top', font: {size: 11}},
  hovermode: 'closest',
};
const AXIS = {gridcolor: '#eceef1', linecolor: '#8a919c', zeroline: false, tickfont: {size: 11}};

async function renderPlot() {
  const chosen = view.selected.length ? selectedRows() : view.state.rows;
  const done = chosen.filter((row) => row.result).map((row) => row.record);
  const what = JSON.stringify([view.selected, done]);
  $('plot-title').textContent = view.selected.length
    ? `I(Q) of the ${view.selected.length === 1 ? 'sample' : `${view.selected.length} samples`} selected`
    : 'I(Q) of the samples done';
  renderNotDone();
  if (what === view.plotted) return;
  view.plotted = what;
  const response = await get(`/api/plot?${query({keys: view.selected})}`);
  const {figure, note} = response.body;
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
    xaxis: {...figure.layout.xaxis, ...AXIS},
    yaxis: {...figure.layout.yaxis, ...AXIS},
  };
  Plotly.react($('plot'), figure.data, layout, {displaylogo: false, responsive: true});
}

// The rows selected that the plot leaves out, and why.
function renderNotDone() {
  const missing = selectedRows().filter((row) => row.status !== 'done');
  $('not-done').replaceChildren(
    ...missing.map((row) =>
      h(
        'li',
        {},
        h('span', {class: `pill st-${row.status}`, text: row.status}),
        h('span', {class: 'name', text: row.name || row.key}),
        row.reason && h('span', {class: 'why', text: row.reason}),
      ),
    ),
  );
}

// One sample

async function loadRow() {
  const pane = $('tab-row');
  if (view.selected.length !== 1) {
    pane.replaceChildren(
      h('div', {class: 'empty'}, view.selected.length ? 'Select only one sample to see its details.' : 'Select a sample to see what it was reduced with, or why it failed.'),
    );
    return;
  }
  const key = view.selected[0];
  const response = await get(`/api/rows/${encodeURIComponent(key)}`);
  if (response.ok && view.selected[0] === key) renderRow(response.body);
}

function renderRow(detail) {
  const row = view.state.rows.find((r) => r.key === detail.key);
  if (!row) return;
  const url = `/api/rows/${encodeURIComponent(row.key)}`;
  const editTweak = async (edit) => {
    const response = await post(`${url}/tweak`, edit);
    if (response.ok) renderRow(response.body);
    else report(response);
  };
  const tweak = h('div', {class: 'form'});
  renderForm(tweak, detail.tweak, editTweak);
  const reasonClass = {failed: 'bad', cancelled: 'bad', incomplete: 'warn', outdated: 'info'}[row.status];
  const details = h(
    'details',
    {class: 'tweak', open: view.tweakOpen, ontoggle: (event) => (view.tweakOpen = event.target.open)},
    h('summary', {text: 'Reduce this sample alone with other parameters'}),
    h('p', {class: 'hint', text: 'The result becomes this sample’s result. The shared settings stay as they are.'}),
    tweak,
    h('button', {type: 'button', class: 'primary', text: 'Reduce this sample alone', onclick: () => act(`${url}/tweak/submit`)}),
  );
  $('tab-row').replaceChildren(
    h(
      'div',
      {class: 'row-head'},
      h('h2', {text: row.name || row.key}),
      h('span', {class: `pill st-${row.status}`, text: row.status}),
    ),
    h(
      'dl',
      {class: 'summary'},
      view.state.columns.map((c) => [h('dt', {text: c.title}), h('dd', {text: row[`${c.name}:text`] || c.default || '—'})]),
    ),
    row.reason && h('div', {class: `reason ${reasonClass ?? ''}`, text: row.reason}),
    detail.reduced_with
      ? h(
          'section',
          {class: 'reduced-with'},
          h('h3', {text: `Reduced at ${detail.created} with`}),
          detail.differs.length > 0 &&
            h('p', {class: 'hint differs', text: `Differs from the current settings in ${detail.differs.join(', ')}.`}),
          summary(detail.reduced_with, detail.differs),
          detail.differs.length > 0 &&
            h('button', {type: 'button', text: 'Use these settings for all samples', onclick: () => act(`${url}/use-settings`)}),
        )
      : h('p', {class: 'hint', text: 'Not reduced yet.'}),
    details,
  );
}

// Wiring

function openMenu(menu) {
  const items = menu.querySelector('.menu-items');
  const open = items.hidden;
  document.querySelectorAll('.menu-items').forEach((m) => (m.hidden = true));
  items.hidden = !open;
}

function wire() {
  $('reduce').onclick = () => act('/api/reduce', {which: 'remaining'});
  $('reduce-more').onclick = () => openMenu($('reduce-split'));
  document.querySelectorAll('[data-reduce]').forEach((item) => {
    item.onclick = () => {
      $('reduce-split').querySelector('.menu-items').hidden = true;
      act('/api/reduce', {which: item.dataset.reduce, keys: view.selected});
    };
  });
  $('cancel').onclick = () => act('/api/cancel');
  $('rescan').onclick = () => act('/api/scan');
  $('remove').onclick = () => act('/api/rows/remove', {keys: view.selected});
  $('restore').onclick = () => act('/api/rows/restore');
  document.querySelector('#message .close').onclick = hideMessage;

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
    act('/api/rows/add', {key: addForm.key.value});
    closeAdd();
  };
  addForm.querySelector('[data-cancel]').onclick = closeAdd;
  addForm.key.onkeydown = (event) => event.key === 'Escape' && closeAdd();

  document.querySelectorAll('[data-view]').forEach((button) => {
    button.onclick = () => {
      document.querySelectorAll('[data-view]').forEach((b) => b.setAttribute('aria-pressed', String(b === button)));
      $('view-samples').hidden = button.dataset.view !== 'samples';
      $('view-runs').hidden = button.dataset.view !== 'runs';
      (button.dataset.view === 'runs' ? runs : rows)?.table.redraw();
    };
  });
  document.querySelectorAll('[data-tab]').forEach((button) => {
    button.onclick = () => {
      view.tab = button.dataset.tab;
      document.querySelectorAll('[data-tab]').forEach((b) => b.setAttribute('aria-selected', String(b === button)));
      for (const tab of ['result', 'settings', 'row']) $(`tab-${tab}`).hidden = tab !== view.tab;
      if (view.tab === 'row') loadRow();
      if (view.tab === 'result') Plotly.Plots.resize($('plot'));
    };
  });

  const files = $('files');
  files.querySelector('.menu-button').onclick = () => openMenu(files);
  const actions = {
    'load-table': () => $('pick-table').click(),
    'load-settings': () => $('pick-settings').click(),
    'table.csv': () => download('/api/table.csv', 'samples.csv'),
    'table.xlsx': () => download('/api/table.xlsx', 'samples.xlsx'),
    'settings.yaml': () => download('/api/settings.yaml', 'settings.yaml'),
    'results-selected': () => download(`/api/results.zip?${query({keys: view.selected})}`, 'results.zip'),
    'results-all': () => download('/api/results.zip', 'results.zip'),
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
      report(await upload(url, input.files[0]));
      input.value = '';
      await refresh();
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
