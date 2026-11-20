// The page of a batch, at batches/<label>/. The server holds the rows, the
// settings, and the results; this file shows the state it sends and sends
// what a person does. Each action answers with the state after it; a poll
// every second sees results arrive. The page keeps only what it shows (see
// `remember`). The document is loaded once: a change of batch, by the list of
// batches, Back or Forward, happens in place (see `show`). A workspace without
// a batch serves the page at its root without one, and the page of a batch
// that cannot be shown is shown so too (see `showWorkspace`).
import {h, get, post, query, save} from './dom.js';
import {Grid, cellColumns, statusColumn} from './tables.js';
import {renderForm, summary} from './form.js';

const $ = (id) => document.getElementById(id);

// The label of the batch whose page is shown; none at the root.
function shownLabel() {
  const found = location.pathname.match(/\/batches\/([^/]+)\/$/);
  return found ? decodeURIComponent(found[1]) : undefined;
}
// The URL of the root of the workspace, and of the page of a batch.
const root = () => new URL(shownLabel() ? '../../' : './', location.href).href;
const batchUrl = (label) => new URL(`batches/${encodeURIComponent(label)}/`, root()).href;

// What the page remembers in the session, so that a reload, or a return to a
// batch, shows the same: for the workspace, the view, the tab of the right
// pane and the axes; for each batch, the filter, the selection and the sort.
const WORKSPACE = ['page', 'tab', 'log'];
const BATCH = ['filter', 'selected', 'sort'];
const batchView = () => ({filter: 'all', selected: [], sort: [], ...remembered(location.pathname, BATCH)});

const view = {
  state: null,
  page: 'rows',
  tab: 'result',
  log: {x: true, y: true},
  ...remembered(new URL(root()).pathname, WORKSPACE),
  ...batchView(),
  plotted: null, // what the plot shows, to fetch it again only on a change
  zoom: 1, // a new value forgets the zoom of the plot; Plotly ignores 0
  colors: {}, // the colour of each curve drawn, by key
  detail: '', // what the row tab shows, to draw it again only on a change
  dismissed: '', // the refusal whose message was closed
  runs: 0, // the version of the runs shown
  runData: null, // the runs last loaded, with their columns
  batchData: null, // the list of batches last shown
  counts: null, // the counts of the rows of each batch, once counted
  files: {}, // the files that each batch names, from the last count answered
  columns: '', // the notes and the columns that the grid of the rows shows
};
let rows; // the Grid of the rows
let runs; // the Grid of the runs

// What is remembered of `names` for the page at `path`.
function remembered(path, names) {
  try {
    const kept = JSON.parse(sessionStorage.getItem(`batch-view ${path}`)) ?? {};
    return Object.fromEntries(names.filter((name) => name in kept).map((name) => [name, kept[name]]));
  } catch {
    return {};
  }
}

function remember() {
  const pick = (names) => JSON.stringify(Object.fromEntries(names.map((name) => [name, view[name]])));
  try {
    sessionStorage.setItem(`batch-view ${new URL(root()).pathname}`, pick(WORKSPACE));
    if (shownLabel()) sessionStorage.setItem(`batch-view ${location.pathname}`, pick(BATCH));
  } catch {
    // A page that cannot store forgets at a reload, nothing more.
  }
}

const count = (n) => `${n} ${n === 1 ? 'row' : 'rows'}`;

// Talking to the server

// The page shown, whose API the requests ask, and the number of its changes
// (see `show`): a state of the batch before, which an action or a poll
// answers after a change, is not drawn.
let here = location.href;
let changes = 0;

// Run `task` after the actions sent before it, so that the server gets them
// in the order a person did them, such as an edit at the blur of an input and
// the click of Apply that moved the focus.
let queue = Promise.resolve();
function queued(task) {
  const done = queue.then(task);
  queue = done.catch(() => {});
  return done;
}

// Ask the server of the page at `page`, with a body for a POST. The answer is
// the parsed body, or null after `say` showed the message of what went wrong.
// The answer of a request is also dropped, without a message, if a newer
// request of its `kind` was sent meanwhile, or a change of batch (see
// `show`), or if `fresh()` says that what it was for has changed.
const sent = {};
async function request(url, {body, kind, fresh = () => true, say = showMessage, page = here} = {}) {
  const asked = kind && (sent[kind] = (sent[kind] ?? 0) + 1);
  let response;
  try {
    response = body === undefined ? await get(url, page) : await post(url, body, page);
  } catch {
    say('The server does not answer, so this was not done. Try again when it is back.');
    return null;
  }
  if ((kind && sent[kind] !== asked) || !fresh()) return null;
  if (response.ok) return response.body;
  say(response.body?.message ?? `The server answered ${response.status}.`);
  return null;
}

// Send an action to the batch shown now, also if the page shows another one
// when its turn comes, and show the state it answers with, or have `say` show
// what went wrong; whether it was done, or null if the page shows another
// batch since.
function act(url, body = {}, say = showMessage) {
  const [page, at, title] = [here, changes, view.state?.title];
  // A refusal answered after a change of batch is no message of the batch
  // shown: the page's message says it, with the title of its batch.
  const said = (text) => (at === changes ? say(text) : showMessage(`${title}: ${text}`));
  return queued(() => perform(url, body, said, page, at));
}

async function perform(url, body, say, page, at) {
  const state = await request(url, {body, say, page});
  if (at !== changes) return null;
  if (!state) {
    await refresh().catch(() => {});
    return false;
  }
  hideMessage();
  render(state);
  if (state.notice) toast(state.notice);
  return true;
}

// Save a file of the batch shown, asked for in the queue so that it holds the
// actions sent before, such as an edit at the blur that the click caused.
async function download(url, name, body) {
  const page = here;
  const file = await queued(() => request(url, {body, page}));
  if (file) save(file, name);
}

class Offline extends Error {}
class Unshown extends Error {}

// Ask for the state of the batch shown and show it; a state of the batch
// shown before is dropped.
async function refresh() {
  const at = changes;
  const after = view.state ? {after: view.state.version, start: view.state.start} : {};
  const response = await get(`state?${query(after)}`, here).catch(() => null);
  if (at !== changes) return;
  if (!response) throw new Offline('The server does not answer.');
  if (response.status === 204) return;
  if (!response.ok) {
    const message = response.body?.message ?? `The server answered ${response.status}.`;
    // A batch removed, such as on another page, is shown no more.
    throw view.state && response.status !== 404 ? new Offline(message) : new Unshown(message);
  }
  render(response.body);
}

// Ask for the state every second; say so while the server does not give it.
// A batch whose first state the server refuses, or that is gone, cannot be
// shown: the page says why, and shows the batches and the runs. A change of
// batch starts a poll of its own, and ends this one, so one runs at a time.
async function poll(at = changes) {
  if (at !== changes) return;
  let again = true;
  try {
    await refresh();
    $('offline').hidden = true;
  } catch (error) {
    if (error instanceof Unshown) {
      again = false;
      showWorkspace(error.message);
    } else if (error instanceof Offline) {
      $('offline').textContent = `${error.message} The page tries again every second; what it shows may be old.`;
      $('offline').hidden = false;
      // A batch whose first state does not come: the page shows the Batches
      // and Runs views, and nothing of the batch before, until it comes; the
      // view chosen stays the view of the page.
      if (!view.state) {
        document.body.inert = false;
        document.body.classList.add('no-batch');
        showPage('batches', false);
        document.title = $('title').textContent = view.batchData?.application ?? '';
      }
    } else throw error;
  } finally {
    if (again) setTimeout(() => poll(at), 1000);
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
  const built = !rows;
  if (built) build(state);
  if (!old) {
    // The view chosen, also after the page showed no batch while the server
    // did not answer.
    if (built || document.body.classList.contains('no-batch')) {
      showPage(state.runs === null && view.page === 'runs' ? 'rows' : view.page);
    }
    document.body.classList.remove('no-batch');
    document.body.inert = false;
  }
  const named = [state.title, state.application].filter(Boolean);
  document.title = named.join(' · ');
  $('title').textContent = named.reverse().join(' · ');
  $('folder').textContent = state.folder;
  $('n-rows').textContent = state.counts.all;
  renderAction(state.action, state.counts.earlier);
  renderChips(state.counts);
  if (old?.action.running && !state.action.running) {
    const ended = ['done', 'earlier', 'failed', 'cancelled'].filter((s) => state.counts[s]).map((s) => `${state.counts[s]} ${s}`);
    toast(`Reducing ended: ${ended.join(', ')}.`);
  }
  showColumns(state);
  // The first state of a batch replaces the rows of the batch before.
  rows.update(state.rows, old ? undefined : {selected: view.selected, filter: view.filter, sort: view.sort});
  if (state.runs !== null && state.runs !== view.runs) loadRuns();
  showBatches({application: state.application, runs: state.runs, rows: state.batches});
  renderRefused(state.refused);
  renderFollow(state.follow);
  $('files-problem').hidden = !state.files_problem;
  $('files-problem').textContent = $('files-problem').title = state.files_problem ?? '';
  renderSettings(state);
  renderBatch(state);
  renderSelection();
  if (view.tab === 'row') loadRow();
}

// The columns of the grid of the rows: the name, each note, each parameter
// column, and the status; and the columns that can be edited, by field.
function gridColumns(state) {
  const named = {
    title: 'Name',
    field: 'name',
    minWidth: 120,
    widthGrow: 2,
    tooltip: (event, cell) => cell.getValue(),
  };
  const notes = state.notes.map((c) => ({title: c.title, field: `notes.${c.name}`, minWidth: 96, headerSort: false}));
  const cells = cellColumns(state.columns, {
    current: () => view.state.columns,
    say: showMessage,
    menu: (column) => [{label: 'Make this a setting', action: () => makeSetting(column)}],
  });
  return {
    columns: [named, ...notes, ...cells, statusColumn],
    edits: Object.fromEntries(state.columns.map((c) => [`cells.${c.name}.value`, c])),
  };
}

// Show the columns of the table of `state` if they changed, as a table
// loaded changes them.
function showColumns(state) {
  // The options of a pick are read when its editor opens (`current`).
  const shown = JSON.stringify([state.notes, state.columns.map((c) => [c.name, c.title, c.editor?.type, c.default])]);
  if (shown === view.columns) return;
  const first = view.columns === '';
  view.columns = shown;
  if (first) return;
  const {columns, edits} = gridColumns(state);
  rows.setColumns(columns, edits);
}

const NO_ROWS = 'No rows. Add a row, or load a table from the Files menu.';

function build(state) {
  rows = new Grid($('rows'), {
    ...gridColumns(state),
    placeholder: NO_ROWS,
    edit: (keys, column, value) => act('cells', {keys, column, value}),
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
  });
  if (state.runs === null) document.querySelector('[data-view="runs"]').hidden = true;
  showTab(view.tab);
}

// The results of `rows` as a zip, if the person wants outdated ones.
function downloadResults(rows, name) {
  const done = rows.filter((row) => row.result);
  const old = done.filter((row) => row.result === 'outdated').length;
  const question = `${old} of the ${done.length} results are outdated: the settings or their cells changed since. Download them anyway?`;
  if (old && !confirm(question)) return;
  download('results.zip', name, {keys: done.map((row) => row.key)});
}

async function loadRuns() {
  const shown = await request('runs', {kind: 'runs'});
  if (!shown) return;
  view.runs = shown.version;
  view.runData = shown;
  $('n-runs').textContent = shown.rows.length;
  if (view.page === 'runs') drawRuns();
}

// The batches that name each run, by the key of the run, in the order of the
// list of batches: the files of each batch, from the last count answered,
// joined to the runs by identity, so that a count that fails changes none. A
// batch that could not be counted names none.
function batchesOfRuns() {
  const of = new Map();
  for (const {label, title} of view.batchData?.rows ?? []) {
    for (const file of view.files[label] ?? []) {
      if (!of.has(file)) of.set(file, []);
      of.get(file).push({label, title});
    }
  }
  return of;
}

// The batches of a run, each a link that shows its batch as its title in the
// list of batches does (see `choose`).
const batchLinks = (cell) =>
  h(
    'span',
    {class: 'batch-links'},
    cell.getValue().map(({label, title}, i) => [
      i ? ', ' : '',
      h('a', {href: batchUrl(label), text: title, onclick: (event) => choose(event, label)}),
    ]),
  );
const batchTitles = (batches) => batches.map((b) => b.title).join(', ');
// Two batches may share a title, so the tooltip names the label too.
const batchNames = (batches) => batches.map((b) => `${b.title} (${b.label})`).join(', ');

// Show the runs last loaded, each with the batches that name it. The grid is
// built and filled when the Runs view is shown, and again after each change
// while it is, such as a count. If the files cannot be listed, the hint says
// why.
function drawRuns() {
  const {columns, rows: shown, problem} = view.runData ?? {};
  if (!shown) return;
  const fields = columns.map((c) => c.title.toLowerCase()).join(' or ');
  $('runs-hint').textContent = problem ?? `Correct the ${fields} of a run. It changes no row of a batch; Follow adds the rows it fills.`;
  $('runs-hint').classList.toggle('bad', Boolean(problem));
  if (!runs) {
    runs = new Grid($('runs'), {
      columns: [
        {title: 'Run', field: 'file', minWidth: 160, widthGrow: 2},
        ...cellColumns(columns, {
          current: () => view.runData.columns,
          revert: (key, column) => correctRuns('runs/cells/revert', {keys: [key], column}),
          say: showMessage,
        }),
        {
          title: 'Batches',
          field: 'batches',
          minWidth: 120,
          widthGrow: 2,
          formatter: batchLinks,
          tooltip: (event, cell) => batchNames(cell.getValue()),
          // A run in no batch sorts first.
          sorter: (a, b) => batchTitles(a).localeCompare(batchTitles(b)),
        },
      ],
      edits: Object.fromEntries(columns.map((c) => [`cells.${c.name}.value`, c])),
      placeholder: 'No runs in this folder.',
      edit: (keys, column, value) => correctRuns('runs/cells', {keys, column, value}),
      select: () => {},
    });
  }
  const of = batchesOfRuns();
  runs.update(shown.map((row) => ({...row, batches: of.get(row.key) ?? []})));
}

// Correct runs, an action of the workspace; whether it was done. The page of
// a batch then asks for its state at once, whose version of the runs has
// moved, so that it loads the runs; a page without a batch asks for the runs.
function correctRuns(url, body) {
  return queued(async () => {
    const done = await request(url, {body});
    if (!done) return false;
    toast(done.notice);
    if (view.state) await refresh().catch(() => {});
    else await loadRuns();
    return true;
  });
}

// Show the list of batches: from each state, and on the page without a batch
// from api/batches.
function showBatches(listed) {
  view.batchData = listed;
  $('n-batches').textContent = listed.rows.length;
  drawBatches();
  drawFollowChoice();
}

// Count the rows of each batch, and ask for the files that each names, while
// the Batches view or the Runs view is shown, the next count 2 s after the
// answer of the one before: the server reads the state of every batch for a
// count, and the pages and Follow wait meanwhile. The Runs view counts too,
// and does not wait for a change of the runs, since Follow adds rows that name
// runs without one. The counts shown stay until the next answer; a count that
// fails shows none, and the Runs view keeps the batches of the last answer. A page that shows no batch, such as the page without
// a batch, loads the list with each count. Nothing counts while the tab of the
// browser is hidden.
let counting = false; // whether a count is on its way
let nextCount; // the timer of the next count
async function countBatches() {
  clearTimeout(nextCount);
  if (counting || !['batches', 'runs'].includes(view.page) || document.hidden) return;
  counting = true;
  const listed = document.body.classList.contains('no-batch') && (await request('batches'));
  if (listed) showBatches(listed);
  view.counts = await request('batches/counts');
  if (view.counts) view.files = view.counts.files;
  counting = false;
  drawBatches();
  if (view.page === 'runs') drawRuns();
  nextCount = setTimeout(countBatches, 2000);
}

// The list of batches, each a link to its page, with its counts once counted,
// or why it could not be counted; the batch shown is marked. The rows are made
// again only when the list changes; a count updates the cells of its rows, so
// that the keyboard focus, a text selected and a click stay.
function drawBatches() {
  if (!view.batchData) return;
  const body = $('batches').tBodies[0];
  const listed = JSON.stringify(view.batchData.rows);
  if (listed !== body.listed) {
    body.listed = listed;
    const none = h('tr', {}, h('td', {class: 'hint', colspan: 4, text: 'No batch yet.'}));
    body.replaceChildren(
      ...(view.batchData.rows.length ? [] : [none]),
      ...view.batchData.rows.map((batch) =>
        h(
          'tr',
          {class: 'batch', 'data-label': batch.label, onclick: (event) => choose(event, batch.label)},
          h(
            'td',
            {},
            h('a', {href: batchUrl(batch.label), text: batch.title}),
            ' ', // so that a double click selects the label alone
            h('small', {class: 'text', text: batch.label}),
            batch.description && h('div', {class: 'hint text description', text: batch.description}),
          ),
          h('td', {class: 'n'}),
          h('td'),
          h('td', {class: 'follows', text: batch.follows ? 'follows' : ''}),
        ),
      ),
    );
  }
  for (const row of body.querySelectorAll('tr.batch')) {
    const {label} = row.dataset;
    if (label === shownLabel()) row.setAttribute('aria-current', 'page');
    else row.removeAttribute('aria-current');
    const n = view.counts?.rows[label];
    const failed = view.counts?.failed[label];
    const counted = JSON.stringify([n, failed]);
    if (counted === row.counted) continue;
    row.counted = counted;
    const [, number, status] = row.cells;
    number.textContent = n ? count(Object.values(n).reduce((a, b) => a + b, 0)) : '';
    status.className = failed ? 'bad' : '';
    status.replaceChildren(
      failed ?? '',
      ...(n ? CHIPS.filter((s) => s !== 'all' && n[s]).map((s) => h('span', {class: `pill st-${s}`, text: `${n[s]} ${s}`})) : []),
    );
  }
}

// A click on a batch of the list shows that batch: a click on its title in the
// Rows view, elsewhere in its row in the view shown, as a click on a row of
// the grid shows that row in the row tab. A click on the title with a modifier
// key is left to the browser, which opens the page in a tab or window of its
// own; so is a click of the middle button, which is no `click`. The label and
// the description are text to select, so a click on them, or one that ends a
// selection, shows nothing. A title in the column Batches of the Runs view is
// such a title.
function choose(event, label) {
  const title = event.target.closest('a');
  if (title && (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey)) return;
  if (event.target.closest('.text') || getSelection().toString()) return;
  event.preventDefault();
  const page = title ? 'rows' : view.page;
  if (label === shownLabel()) showPage(page);
  else show(batchUrl(label), page, 'pushState');
}

// Show the page at `url`, of a batch or the root, in place of the page shown,
// in the view `page`, or the view shown if none; `how` is the call of
// `history` that records it, none for Back and Forward. The root shows the
// page of the batch that the server redirects it to, else the page without a
// batch. The grids stay and get the rows of the batch, as `render` draws a
// first state. An action goes to the batch it was made for, also a value
// typed that the blur sends, and a file asked for is saved. Nothing of the
// batch before stays: the selection, the filter and the sort are those
// remembered for the batch; forms, menus, dialogs and messages close; and an
// answer that would draw something of the batch before is dropped, a state
// (`changes`) or the answer of a request of a `kind`. The page takes no click
// until the first state is drawn.
async function show(url, page, how) {
  document.activeElement?.blur();
  const at = ++changes;
  for (const kind in sent) sent[kind] += 1;
  document.body.inert = true;
  if (how) history[how](null, '', url);
  if (!shownLabel()) {
    const home = await fetch(location.href).catch(() => null);
    if (at !== changes) return;
    if (home?.redirected) history.replaceState(null, '', home.url);
  }
  here = location.href;
  Object.assign(view, batchView(), {state: null, plotted: null, zoom: view.zoom + 1, colors: {}, detail: '', dismissed: ''});
  document.querySelectorAll('.menu-items').forEach((m) => (m.hidden = true));
  document.querySelectorAll('dialog[open]').forEach((d) => d.close());
  closeAdd();
  closeNewBatch();
  hideMessage();
  $('toast').hidden = true;
  $('batch-form').shown = null;
  $('batch-problem').textContent = '';
  if (page) showPage(page);
  drawBatches();
  if (shownLabel()) poll();
  else showWorkspace();
}

// The form "New batch". The server says what a batch can be made with
// (api/sources) and decides what can be made: a refusal shows at the form.
// The rows come from a source, a range of those that it has filled now; from
// a table file; or none. Opened on the page of a batch, the form starts at
// that batch's source, workflow, columns and settings, so that the next batch
// is this form with a range that starts later. A batch from a source may
// follow it; Follow is off unless ticked, since it stops the batch that
// follows, for good, and reduces rows at once with the settings chosen.
let choices = []; // what a new batch can be made with, as last loaded
let none = 0; // the index of the choice without a source
let sourceRows = null; // the rows of the source chosen, as last loaded
const FILE = 'file'; // the value of the choice "from a table file"

async function openNewBatch() {
  const form = $('new-batch');
  const loaded = await request('sources', {kind: 'sources'});
  if (!loaded) return;
  choices = loaded;
  none = choices.findIndex((c) => c.name === null);
  form.reset();
  form.elements.source.replaceChildren(
    ...choices.flatMap((c, i) => (c.name === null ? [] : [h('option', {value: i, text: `from ${c.name}`})])),
    h('option', {value: FILE, text: 'from a table file'}),
    h('option', {value: none, text: 'none: no rows'}),
  );
  if (view.state) {
    const at = choices.findIndex((c) => c.name === view.state.follow.source);
    form.elements.source.value = at >= 0 ? at : none;
  }
  chooseSource();
  drawFollowChoice();
  $('new-problem').textContent = '';
  form.querySelector('[type=submit]').disabled = false;
  form.hidden = false;
  $('new-batch-open').hidden = true;
  form.elements.title.focus();
}

function closeNewBatch() {
  $('new-batch').hidden = true;
  $('new-batch-open').hidden = false;
}

const way = () => {
  const chosen = $('new-batch').elements.source.value;
  return chosen === FILE ? FILE : choices[chosen].name === null ? 'none' : 'source';
};
const chosenSource = () => choices[way() === FILE ? none : $('new-batch').elements.source.value];
const chosenSpec = () => chosenSource().specs[$('new-batch').elements.spec.value];

// The workflows that fit the way chosen, and its fields.
function chooseSource() {
  const source = chosenSource();
  const kind = way();
  const {elements} = $('new-batch');
  elements.spec.replaceChildren(...source.specs.map((s, i) => h('option', {value: i, text: s.title})));
  const same = view.state ? source.specs.findIndex((s) => s.name === view.state.header.spec) : -1;
  if (same >= 0) elements.spec.value = same;
  $('new-rows').textContent = {
    none: 'The batch starts without rows: add them, or load a table.',
    [FILE]: 'The batch gets the rows and the columns of the file.',
    source: '',
  }[kind];
  $('new-range').hidden = kind !== 'source';
  $('new-follow').hidden = kind !== 'source';
  $('new-file').hidden = kind !== FILE;
  $('new-columns-field').hidden = kind === FILE;
  chooseSpec();
  if (kind === 'source') loadSourceRows(source.name);
}

// Beside Follow: if ticked, how many rows taken are reduced at once, or that
// the batch gets only the rows that arrive later; and which batch stops
// following.
function drawFollowChoice() {
  const other = view.batchData?.rows.find((batch) => batch.follows);
  const ticked = $('new-batch').elements.follow.checked;
  const n = ticked && sourceRows ? sourceRows.rows.filter((row) => row.mark === 'taken').length : 0;
  const none = ticked && rangeChosen().later;
  $('new-follow-other').textContent = [
    n ? `${count(n)} taken ${n === 1 ? 'is' : 'are'} reduced at once.` : '',
    none ? 'The batch gets only the rows that arrive later.' : '',
    other ? `${other.title} ${ticked ? 'stops following' : 'follows'}.` : '',
  ].join(' ').trim();
}

// The range chosen: its first and last keys, null where open, or `later`, no
// row now, so that the batch gets only the rows added later.
function rangeChosen() {
  const {first, last} = $('new-batch').elements;
  const later = first.selectedOptions[0]?.hasAttribute('data-later') ?? false;
  return later ? {first: null, last: null, later} : {first: first.value || null, last: last.value || null, later};
}

// Follow ticked: the range has no last row, since Follow adds every row that
// arrives later; nor has a range of no row now. "to" is then open, disabled,
// and says so. Whether "to" was set and is open now, so that the rows are to
// be marked again.
function chooseFollow() {
  const {follow, last} = $('new-batch').elements;
  const open = follow.checked || rangeChosen().later;
  const cleared = open && last.value !== '';
  if (open) last.value = '';
  last.disabled = open;
  if (last.options[0]) last.options[0].textContent = follow.checked ? 'open: Follow adds every later row' : open ? 'no row now' : 'the last';
  drawFollowChoice();
  return cleared;
}

// The parameters of the workflow chosen: those that the source fills are
// columns, the others a person may make columns. On the page of a batch, its
// columns are ticked, and its settings offered.
function chooseSpec() {
  const {name, fills} = chosenSource();
  const spec = chosenSpec();
  const batch = view.state;
  const ticked = batch ? batch.columns.map((c) => c.name) : [];
  $('new-columns').replaceChildren(
    ...(spec?.params ?? []).map((p) => {
      const fixed = fills.includes(p.name);
      const box = h('input', {type: 'checkbox', name: 'column', value: p.name, checked: fixed || ticked.includes(p.name), disabled: fixed});
      return h('label', {title: fixed ? `Filled by ${name}` : null}, box, p.title);
    }),
  );
  $('new-settings').hidden = !batch;
  $('new-like').textContent = batch ? `Start with the settings of ${batch.title}` : '';
}

// The rows that the source makes now, for the range; the first and the last
// row are chosen among those that are filled.
async function loadSourceRows(name) {
  const {elements} = $('new-batch');
  sourceRows = null;
  for (const end of [elements.first, elements.last]) end.replaceChildren();
  drawRange();
  const loaded = await markRange(name);
  if (!loaded) return;
  const filled = loaded.rows.filter((row) => row.mark !== 'waits');
  const option = (row) => h('option', {value: row.key, text: row.name === row.key ? row.key : `${row.name} (${row.key})`});
  elements.first.replaceChildren(
    h('option', {value: '', text: 'the first'}),
    ...filled.map(option),
    h('option', {value: '', 'data-later': true, text: 'no row now'}),
  );
  elements.last.replaceChildren(h('option', {value: '', text: 'the last'}), ...filled.map(option));
  chooseFollow();
}

// Ask the server how the range chosen marks each row of the source, and show
// it; a range it refuses is said, and its rows are shown without marks.
async function markRange(name = chosenSource().name) {
  const {first, last, later} = rangeChosen();
  const ends = {name, ...(first && {first}), ...(last && {last}), ...(later && {later})};
  const said = (text) => {
    $('new-rows').textContent = text;
    if (sourceRows) drawRange(false);
  };
  const loaded = await request(`sources/rows?${query(ends)}`, {kind: 'source-rows', say: said});
  if (!loaded || way() !== 'source' || chosenSource().name !== name) return null;
  sourceRows = loaded;
  drawRange();
  return loaded;
}

// The rows of the source, each with the mark the server gave it, or none.
function drawRange(marked = true) {
  const box = $('new-source-rows');
  if (!sourceRows) {
    $('new-rows').textContent = '';
    return box.replaceChildren(h('div', {class: 'empty', text: 'Listing the rows…'}));
  }
  box.replaceChildren(sourceTable(sourceRows, (row) => (marked ? row.mark : '')));
  if (!marked) return;
  const n = (mark) => sourceRows.rows.filter((row) => row.mark === mark).length;
  const waits = n('waits');
  const waiting = waits ? `; ${count(waits)} ${waits === 1 ? 'waits' : 'wait'} for a run, which Follow adds once filled if the batch follows` : '';
  $('new-rows').textContent = `The batch gets ${count(n('taken'))}${waiting}.`;
  drawFollowChoice();
}

// A table of the rows of a source: the name, each column, and what
// `status(row)` says; with a box to pick each row if given `picked`, which is
// told the keys picked at each change.
function sourceTable({columns, rows: listed}, status, picked) {
  if (!listed.length) return h('div', {class: 'empty', text: 'No rows.'});
  const boxes = [];
  const pills = {waits: 'st-incomplete', taken: 'st-done'};
  const body = listed.map((row) => {
    const said = status(row);
    const box = picked && h('input', {type: 'checkbox', value: row.key, 'aria-label': row.name});
    if (box) {
      box.onchange = () => picked(boxes.filter((b) => b.checked).map((b) => b.value));
      boxes.push(box);
    }
    return h(
      'tr',
      {class: said === 'left out' ? 'out' : null},
      box && h('td', {}, box),
      h('td', {text: row.name}),
      columns.map((c) => h('td', {text: row.cells[c.name]})),
      h('td', {}, said && h('span', {class: `pill ${pills[said] ?? 'st-cancelled'}`, text: said})),
    );
  });
  const head = h('tr', {}, picked && h('th'), h('th', {text: 'Name'}), columns.map((c) => h('th', {text: c.title})), h('th'));
  return h('table', {}, h('thead', {}, head), h('tbody', {}, body));
}

// Make the batch of the form, once at a time, in the queue, so that it starts
// with the settings that an Apply sent before has put in force.
async function makeBatch(event) {
  event.preventDefault();
  const form = $('new-batch');
  const button = form.querySelector('[type=submit]');
  if (button.disabled) return;
  const {elements} = form;
  const say = (text) => ($('new-problem').textContent = text);
  const header = {
    title: elements.title.value.trim(),
    description: elements.description.value.trim(),
    spec: chosenSpec()?.name ?? '',
    ...(!$('new-settings').hidden && elements.like.checked && {settings_from: view.state.label}),
  };
  const [file] = elements.table.files;
  if (way() === FILE && !file) return say('Choose a table file.');
  button.disabled = true;
  const [url, body] = way() === FILE
    ? [`batches/table?${query({...header, name: file.name})}`, file]
    : [
        'batches',
        {
          ...header,
          source: chosenSource().name,
          ...rangeChosen(),
          columns: [...form.querySelectorAll('[name=column]:checked')].map((box) => box.value),
          follow: way() === 'source' && elements.follow.checked,
        },
      ];
  const page = here;
  const made = await queued(() => request(url, {body, say, page}));
  if (made) show(batchUrl(made.label), 'rows', 'pushState');
  else button.disabled = false;
}

// The dialog "Add rows from the runs": the rows of the source that the table
// lacks, those that wait marked. The rows picked are added as the source makes
// them now.
async function openTake() {
  const listed = await request('rows/offered', {kind: 'offered'});
  if (!listed) return;
  const dialog = $('take');
  const add = dialog.querySelector('[value=take]');
  let keys = [];
  const picked = (chosen) => {
    keys = chosen;
    add.disabled = keys.length === 0;
    add.textContent = keys.length ? `Add ${count(keys.length)}` : 'Add';
  };
  picked([]);
  $('take-rows').replaceChildren(
    listed.rows.length
      ? sourceTable(listed, (row) => row.mark, picked)
      : h('div', {class: 'empty', text: 'Every row of the source is in the batch.'}),
  );
  dialog.returnValue = '';
  dialog.onclose = () => dialog.returnValue === 'take' && act('rows/take', {keys});
  dialog.showModal();
}

// "Make this a setting" on the header of a column: of the value that every
// row has, or, where the rows differ, of the value a person chooses, sent as
// the key of a row that has it. The server says which values there are.
async function makeSetting(column) {
  const values = await request(`columns/values?${query({name: column.name})}`, {kind: 'values'});
  if (!values) return;
  if (values.length < 2) return act('columns/setting', {name: column.name});
  const dialog = $('make-setting');
  dialog.querySelector('h3').textContent = `Make ${column.title} a setting`;
  const empty = column.default ? `empty: the default, ${column.default}` : 'empty';
  $('setting-values').replaceChildren(
    ...values.map(({key, text}, i) =>
      h('label', {}, h('input', {type: 'radio', name: 'value', value: key, checked: i === 0}), text || empty),
    ),
  );
  dialog.returnValue = '';
  dialog.onclose = () => {
    if (dialog.returnValue !== 'make') return;
    act('columns/setting', {name: column.name, key: dialog.querySelector('[name=value]:checked').value});
  };
  dialog.showModal();
}

// The reduce button and its menu, and the marks of the Settings tab; `earlier`
// counts the rows reduced with earlier settings.
function renderAction(action, earlier) {
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
  $('n-earlier').textContent = earlier;
  const counted = {remaining: action.remaining, earlier, all: action.again};
  for (const [which, n] of Object.entries(counted)) {
    document.querySelector(`[data-reduce="${which}"]`).disabled = Boolean(action.blocked) || n === 0;
  }
  $('reduce-more').disabled = Boolean(action.blocked);
  for (const id of ['reduce-more', 'reduce-earlier']) $(id).title = action.blocked ?? '';
  $('settings-badge').hidden = !action.blocked;
  $('settings-badge').title = action.blocked ?? '';
  $('earlier-badge').hidden = earlier === 0;
  $('earlier-badge').textContent = earlier;
  $('earlier-badge').title = `${count(earlier)} reduced with earlier settings`;
}

const CHIPS = ['all', 'ready', 'running', 'done', 'earlier', 'outdated', 'failed', 'incomplete', 'cancelled'];

// The chip of each status is made once and updated in place, so that a chip
// that has the keyboard focus keeps it.
const chips = new Map();

function chip(status) {
  if (!chips.has(status)) {
    chips.set(
      status,
      h(
        'button',
        {
          type: 'button',
          class: `chip st-${status}`,
          onclick: () => {
            view.filter = view.filter === status ? 'all' : status;
            remember();
            rows.filter(view.filter);
            renderChips(view.state.counts);
          },
        },
        h('span', {class: 'dot'}),
        h('span', {text: status}),
        h('span', {class: 'n'}),
      ),
    );
  }
  return chips.get(status);
}

function renderChips(counts) {
  const box = $('chips');
  const shown = CHIPS.filter((s) => s === 'all' || counts[s] > 0 || s === view.filter);
  shown.forEach((status, i) => {
    const element = chip(status);
    element.setAttribute('aria-pressed', String(view.filter === status));
    element.querySelector('.n').textContent = counts[status];
    if (box.children[i] !== element) box.insertBefore(element, box.children[i] ?? null);
  });
  [...box.children].slice(shown.length).forEach((element) => element.remove());
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

// "Add rows from the runs", unless the batch has no source; and while the
// batch follows, "Stop following" and why the last step failed, if it did,
// else how many rows wait. A batch that does not follow has no Follow control.
function renderFollow(follow) {
  $('take-open').hidden = follow.source === null;
  $('follow-status').hidden = $('stop-follow').hidden = !follow.on;
  const status = $('follow-status');
  status.classList.toggle('bad', Boolean(follow.failure));
  const waiting = follow.waiting ? `; ${count(follow.waiting)} of the source wait to be filled` : '';
  status.textContent = follow.failure ?? `Following, every ${follow.interval} s${waiting}`;
  status.title = status.textContent;
  rows.empty.textContent = follow.on ? 'No rows yet. Follow adds the rows of the runs that arrive.' : NO_ROWS;
}

// Stop Follow, once the person agrees that it cannot be turned on again.
function stopFollowing() {
  const question = `Stop following? "${view.state.title}" cannot follow again. Rows that arrive later are added with "Add rows from the runs", or go to a new batch.`;
  if (confirm(question)) act('follow/stop');
}

// The settings form, which holds changes until Apply; and the rows reduced
// with earlier settings, which a person may reduce again. Apply and Discard
// are never disabled: a click on them may send the value typed first.
function renderSettings(state) {
  const {action, counts, settings_changed: changed, settings_problem: problem} = state;
  $('earlier').hidden = counts.earlier === 0;
  $('earlier-state').textContent = `${count(counts.earlier)} reduced with earlier settings.`;
  $('reduce-earlier').disabled = Boolean(action.blocked);
  $('draft').classList.toggle('changed', changed);
  const said = problem ? ` ${problem}.` : '';
  $('draft-state').textContent = changed ? `Changed, not applied yet.${said}` : said.trim() || 'These settings are in force.';
  // The file holds the batch's settings, so it waits for Apply or Discard.
  const download = document.querySelector('[data-file="settings.yaml"]');
  download.disabled = changed;
  download.title = changed ? `${action.blocked} first.` : '';
  // A setting whose cell the page edits may be a column.
  const offer = (node) =>
    state.may_be_columns.includes(node.path[0]) &&
    h('button', {
      type: 'button',
      class: 'quiet small',
      text: 'Make this a column',
      title: 'Each row of the table gets this value in its cell. A row added later has an empty cell: the default, or an incomplete row if there is none.',
      onclick: () => act('settings/column', {name: node.path[0]}),
    });
  renderForm($('settings'), state.settings, (edit) => act('settings', edit), offer);
}

// The tab "Batch": its workflow, columns and label, and a form of its title
// and description. The inputs are filled again only when the header changes,
// so that a value typed stays while the page polls.
function renderBatch(state) {
  const {header, label} = state;
  const fixed = {
    Workflow: header.workflow,
    Columns: state.columns.map((c) => c.title).join(', '),
    Label: label,
  };
  $('batch-fixed').replaceChildren(...Object.entries(fixed).flatMap(([name, text]) => [h('dt', {text: name}), h('dd', {text})]));
  const form = $('batch-form');
  const shown = JSON.stringify([state.title, header]);
  if (shown === form.shown) return;
  form.shown = shown;
  form.elements.title.value = state.title;
  form.elements.description.value = header.description;
}

// Save the form of the tab "Batch"; a refusal shows at the form.
function saveBatch(event) {
  event.preventDefault();
  const {elements} = $('batch-form');
  const say = (text) => ($('batch-problem').textContent = text);
  say('');
  act('batch', {title: elements.title.value.trim(), description: elements.description.value.trim()}, say);
}

// Remove this batch, then show the root in its place in the history: the
// server redirects it to the batch that follows or the only one left, else it
// is the page without a batch.
async function removeBatch() {
  const question = `Remove the batch "${view.state.title}"? Its title, description, settings and table are removed. Its results stay in the workspace.`;
  if (!confirm(question)) return;
  const [page, at] = [here, changes];
  const done = await queued(() => request('batch/remove', {body: {}, page}));
  if (done && at === changes) show(root(), null, 'replaceState');
}

// Whether to replace the changes of the settings form that are not applied.
function replaceChanges() {
  return !view.state.settings_changed || confirm('The settings form holds changes that are not applied. Replace them?');
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
  const answer = await request('plot', {body: {keys: view.selected}, kind: 'plot'});
  if (!answer) return;
  const {figure, note, colors} = answer;
  view.colors = colors;
  renderLegend();
  const quantity = figure?.layout?.meta?.quantity ?? 'Result';
  const n = view.selected.length;
  $('plot-title').textContent = n ? `${quantity} of the ${count(n)} selected` : `${quantity} of the rows done`;
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
// outdated, and its status where not done; above it, a count per status, with
// the reason if all its rows share it: for rows of earlier settings, the
// settings that changed, since the tooltip that is their reason is long.
function renderLegend() {
  const shown = view.selected.length ? selectedRows() : view.state.rows.filter((row) => row.key in view.colors);
  const groups = Map.groupBy(shown.filter((row) => row.status !== 'done'), (row) => row.status);
  $('legend-summary').replaceChildren(
    ...[...groups].map(([status, group]) => {
      const reasons = new Set(group.map((row) => (status === 'earlier' ? row.changed : row.reason)));
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
      ? 'Select only one row to see its details.'
      : 'Select a row to see what it was reduced with, or why it failed.';
    pane.replaceChildren(h('div', {class: 'empty', text}));
    return;
  }
  const only = () => view.selected.length === 1 && view.selected[0] === row.key;
  const detail = await request(`row?${query({key: row.key})}`, {kind: 'row', fresh: only});
  if (detail) renderRow(detail);
}

function renderRow(detail) {
  const row = view.state.rows.find((r) => r.key === detail.key);
  if (!row) return;
  const json = JSON.stringify([detail, row, view.state.columns]);
  if (json === view.detail) return;
  view.detail = json;
  const tried = row.status === 'failed' || row.status === 'cancelled';
  const reasonClass = {failed: 'bad', cancelled: 'bad', incomplete: 'warn', outdated: 'info', earlier: 'earlier'}[row.status];
  const useSettings = () => replaceChanges() && act('row/use-settings', {key: row.key}).then((done) => done && showTab('settings'));
  const settingsNames = Object.fromEntries(view.state.settings.map((n) => [n.path[0], n.label]));
  const parts = [
    h('div', {class: 'row-head'}, h('h2', {text: row.name}), h('span', {class: `pill st-${row.status}`, text: row.status})),
    h(
      'dl',
      {class: 'summary'},
      view.state.columns.map((c) => [h('dt', {text: c.title}), h('dd', {text: row.cells[c.name].text || c.default || '—'})]),
    ),
    row.reason && h('div', {class: `reason ${reasonClass ?? ''}`, text: row.reason}),
    detail.reduced_with
      ? h(
          'section',
          {class: 'reduced-with'},
          h('h3', {text: `${tried ? 'Tried' : 'Reduced'} on ${when(detail.created)} with`}),
          detail.differs.length > 0 &&
            h('p', {class: 'hint differs', text: `Differs from the settings now in ${detail.differs.map((n) => settingsNames[n] ?? n).join(', ')}.`}),
          summary(detail.reduced_with, detail.differs),
          detail.differs.length > 0 &&
            h('button', {type: 'button', text: 'Put these settings in the form', onclick: useSettings}),
        )
      : !row.record && h('p', {class: 'hint', text: 'Not reduced yet.'}),
  ];
  const pane = $('tab-row');
  const focused = pane.contains(document.activeElement) && document.activeElement.textContent;
  pane.replaceChildren(...parts.filter(Boolean));
  // The buttons are made again; the focus goes to the one with the same text.
  if (focused) [...pane.querySelectorAll('button')].find((b) => b.textContent === focused)?.focus();
}

// A time as 2026-11-10 13:11, in the time zone of the browser.
function when(iso) {
  const t = new Date(iso);
  const two = (n) => String(n).padStart(2, '0');
  return `${t.getFullYear()}-${two(t.getMonth() + 1)}-${two(t.getDate())} ${two(t.getHours())}:${two(t.getMinutes())}`;
}

// Wiring

function closeAdd() {
  $('add-form').hidden = true;
  $('add-row').hidden = false;
}

function openMenu(menu) {
  const items = menu.querySelector('.menu-items');
  const open = items.hidden;
  document.querySelectorAll('.menu-items').forEach((m) => (m.hidden = true));
  items.hidden = !open;
}

// Show the view `page`; a `chosen` one is the view of the page from then on,
// and is remembered.
function showPage(page, chosen = true) {
  if (chosen) {
    view.page = page;
    remember();
  }
  document.querySelectorAll('[data-view]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.view === page)));
  $('view-rows').hidden = page !== 'rows';
  $('view-runs').hidden = page !== 'runs';
  $('view-batches').hidden = page !== 'batches';
  document.querySelector('main').classList.toggle('wide', page === 'runs');
  if (page === 'runs') drawRuns();
  if (page === 'batches' || page === 'runs') countBatches();
  const grid = {rows, runs}[page];
  grid?.built.then(() => grid.table.redraw());
}

function showTab(tab) {
  view.tab = tab;
  remember();
  document.querySelectorAll('[data-tab]').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === tab)));
  for (const name of ['result', 'settings', 'row', 'batch']) $(`tab-${name}`).hidden = name !== tab;
  if (tab === 'row' && view.state) loadRow();
  if (tab === 'result' && !$('plot').hidden) Plotly.Plots.resize($('plot'));
}

// Reduce the rows of earlier settings, from the reduce menu or the Settings
// tab, once the person confirms.
function reduceEarlier() {
  const n = view.state.counts.earlier;
  const question = `Reduce the ${count(n)} reduced with earlier settings again, with the settings now?`;
  if (confirm(question)) act('reduce', {which: 'earlier'});
}

function wire() {
  // Plotly fits the plot to its box only when the window changes size; the
  // box also changes with the legend below it.
  const plot = $('plot');
  new ResizeObserver(() => plot.data && !plot.hidden && Plotly.Plots.resize(plot)).observe(plot);
  $('reduce').onclick = () => act('reduce', {which: 'remaining'});
  $('reduce-more').onclick = () => openMenu($('reduce-split'));
  document.querySelectorAll('[data-reduce]').forEach((item) => {
    item.onclick = () => {
      $('reduce-split').querySelector('.menu-items').hidden = true;
      if (item.dataset.reduce === 'earlier') reduceEarlier();
      else act('reduce', {which: item.dataset.reduce, keys: view.selected});
    };
  });
  $('cancel').onclick = () => act('cancel');
  $('stop-follow').onclick = stopFollowing;
  $('settings-apply').onclick = () => act('settings/apply');
  $('settings-discard').onclick = () => act('settings/discard');
  $('reduce-earlier').onclick = reduceEarlier;
  $('relist').onclick = () => act('relist');
  const newBatch = $('new-batch');
  $('new-batch-open').onclick = openNewBatch;
  newBatch.onsubmit = makeBatch;
  newBatch.elements.source.onchange = chooseSource;
  newBatch.elements.spec.onchange = chooseSpec;
  newBatch.elements.first.onchange = () => {
    chooseFollow();
    markRange();
  };
  newBatch.elements.last.onchange = () => markRange();
  newBatch.elements.follow.onchange = () => chooseFollow() && markRange();
  $('take-open').onclick = openTake;
  newBatch.querySelector('[data-cancel]').onclick = closeNewBatch;
  newBatch.addEventListener('keydown', (event) => event.key === 'Escape' && closeNewBatch());
  $('batch-form').onsubmit = saveBatch;
  $('remove-batch').onclick = removeBatch;
  $('remove').onclick = () => act('rows/remove', {keys: view.selected});
  document.querySelector('#message .close').onclick = () => {
    view.dismissed = shownMessage;
    hideMessage();
  };

  const addForm = $('add-form');
  $('add-row').onclick = () => {
    addForm.hidden = false;
    $('add-row').hidden = true;
    addForm.key.value = '';
    addForm.key.focus();
  };
  addForm.onsubmit = (event) => {
    event.preventDefault();
    const key = addForm.key.value.trim();
    act('rows/add', {key}).then((done) => done && rows.reveal(key));
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
    const question = 'Load a table file? It replaces the table of this batch: its rows and its columns.';
    if (confirm(question)) $('pick-table').click();
  };
  const actions = {
    'load-table': loadTable,
    'load-settings': () => replaceChanges() && $('pick-settings').click(),
    'table.csv': () => download('table.csv', 'rows.csv'),
    'table.xlsx': () => download('table.xlsx', 'rows.xlsx'),
    'settings.yaml': () => download('settings.yaml', 'settings.yaml'),
    'results-selected': () => downloadResults(selectedRows(), 'results-selected.zip'),
    'results-all': () => downloadResults(view.state.rows, 'results-all.zip'),
  };
  files.querySelectorAll('[data-file]').forEach((item) => {
    item.onclick = () => {
      files.querySelector('.menu-items').hidden = true;
      actions[item.dataset.file]();
    };
  });
  for (const [input, url] of [[$('pick-table'), 'table'], [$('pick-settings'), 'settings.yaml']]) {
    input.onchange = async () => {
      if (!input.files.length) return;
      const file = input.files[0];
      input.value = '';
      const done = await act(`${url}?${query({name: file.name})}`, file);
      if (done && url === 'settings.yaml') showTab('settings');
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
  window.addEventListener('popstate', () => show(location.href));
  document.addEventListener('visibilitychange', countBatches);
}

// The page at the root, when it goes to no batch, or of a batch that cannot
// be shown, saying why: the Batches view, with the form "New batch" open if
// there is no batch, and the Runs view, loaded once. No state is polled; the
// list of batches is loaded again with each count. Making a batch, or a click
// on one, shows it (see `show`).
async function showWorkspace(why = '') {
  document.body.classList.add('no-batch');
  document.body.inert = false;
  showPage('batches');
  if (why) showMessage(why);
  const listed = await request('batches', {kind: 'batches'});
  if (!listed) return;
  showBatches(listed);
  if (!view.batchData.rows.length) openNewBatch();
  const {application, runs} = view.batchData;
  if (application) document.title = $('title').textContent = application;
  document.querySelector('[data-view="runs"]').hidden = runs === null;
  if (runs !== null) loadRuns();
}

wire();
if (shownLabel()) poll();
else showWorkspace();
