// The page of a batch, at batches/<label>/. The server holds the rows, the
// settings and the results. The page draws the state the server sends and
// posts what a person does. Each action returns the state after it, and a
// poll every second shows results as they arrive. The page stores only view
// choices (see `remember`). The document loads once: a change of batch, by
// the list of batches, Back or Forward, happens in place (see `show`). The
// root without a batch, and the page of a batch that cannot be shown, show
// the workspace instead (see `showWorkspace`).
import {h, get, post, query, save} from './dom.js';
import {Grid, cellColumns, statusClass, statusColumn} from './tables.js';
import {renderForm, summary} from './form.js';

const $ = (id) => document.getElementById(id);

// The label of the batch whose page is shown; undefined at the root.
function shownLabel() {
  const found = location.pathname.match(/\/batches\/([^/]+)\/$/);
  return found ? decodeURIComponent(found[1]) : undefined;
}
const root = () => new URL(shownLabel() ? '../../' : './', location.href).href;
const batchUrl = (label) => new URL(`batches/${encodeURIComponent(label)}/`, root()).href;

// What the page stores in `sessionStorage`, so that a reload, or a return to
// a batch, shows the same: for the workspace, the view, the tab of the right
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
  dismissed: '', // the submit errors whose message was closed
  runs: 0, // the revision of the runs shown
  runData: null, // the runs last loaded, with their columns
  batchData: null, // the list of batches last shown
  counts: null, // the counts of the rows of each batch, once counted
  files: {}, // the files that each batch refers to, from the last count
  columns: '', // the notes and the columns that the grid of the rows shows
  publishing: false, // whether a publish request is in flight
};
let rows; // the Grid of the rows
let runs; // the Grid of the runs

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
    // Without storage, the view resets at a reload; nothing else fails.
  }
}

const count = (n) => `${n} ${n === 1 ? 'row' : 'rows'}`;

// Talking to the server

// The page shown, whose API the requests go to, and the count of changes of
// batch (see `show`). A state that arrives after a change belongs to the
// batch before and is not drawn.
let here = location.href;
let changes = 0;

// Runs `task` after the actions sent before it, so that the server receives
// them in the order a person did them, such as an edit at the blur of an
// input and the click on Apply that moved the focus.
let queue = Promise.resolve();
function queued(task) {
  const done = queue.then(task);
  queue = done.catch(() => {});
  return done;
}

// Requests `url` of the page at `page`, a POST if `body` is given. Returns the
// parsed body, or null after `say` showed what went wrong. The response is
// also dropped, with no message, if a newer request of its `kind` was sent
// meanwhile or the batch changed (see `show`), or if `fresh()` returns false.
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

// Posts an action to the batch shown now, even if the page shows another
// batch when the queue reaches it. Draws the state returned, or has `say`
// show what went wrong. Returns whether it was done, or null if the page
// shows another batch since.
function act(url, body = {}, say = showMessage) {
  const [page, at, title] = [here, changes, view.state?.title];
  // An error that arrives after a change of batch belongs to the batch
  // before: the page's message shows it, with the title of that batch.
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

// Saves a file of the batch shown. The request waits in the queue, so that
// the file includes the actions sent before, such as an edit at the blur
// that the click caused.
async function download(url, name, body) {
  const page = here;
  const file = await queued(() => request(url, {body, page}));
  if (file) save(file, name);
}

class Offline extends Error {}
class Unshown extends Error {}

// Fetches and draws the state of the batch shown. A state that arrives after
// a change of batch is dropped.
async function refresh() {
  const at = changes;
  const after = view.state ? {after: view.state.revision, start: view.state.start} : {};
  const response = await get(`state?${query(after)}`, here).catch(() => null);
  if (at !== changes) return;
  if (!response) throw new Offline('The server does not answer.');
  if (response.status === 204) return;
  if (!response.ok) {
    const message = response.body?.message ?? `The server answered ${response.status}.`;
    // 404: the batch was removed, such as on another page.
    throw view.state && response.status !== 404 ? new Offline(message) : new Unshown(message);
  }
  render(response.body);
}

// Polls the state every second, with a banner while the server does not
// respond. If the server rejects the first state, or the batch is gone, the
// page shows why, and the batches and the runs. A change of batch starts a
// poll of its own and ends this one, so that one poll runs at a time.
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
      // No first state yet: show the Batches and Runs views, and nothing of
      // the batch before, until it arrives. `view.page`, the view chosen,
      // stays.
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
  if (old && state.revision < old.revision) return;
  view.state = state;
  const built = !rows;
  if (built) build(state);
  if (!old) {
    // Show the view chosen, also after the page showed no batch while the
    // server did not respond.
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
  renderAction(state.action, state.counts['settings changed']);
  renderChips(state.counts);
  if (old?.action.running && !state.action.running) {
    const ended = ['done', 'settings changed', 'failed', 'cancelled'].filter((s) => state.counts[s]).map((s) => `${state.counts[s]} ${s}`);
    toast(`Reducing ended: ${ended.join(', ')}.`);
  }
  showColumns(state);
  // The first state of a batch replaces the rows of the batch before.
  rows.update(state.rows, old ? undefined : {selected: view.selected, filter: view.filter, sort: view.sort});
  if (state.runs !== null && state.runs !== view.runs) loadRuns();
  showBatches({application: state.application, runs: state.runs, rows: state.batches});
  renderSubmitErrors(state.submit_errors);
  renderFollow(state.follow);
  $('files-problem').hidden = !state.files_problem;
  $('files-problem').textContent = $('files-problem').title = state.files_problem ?? '';
  renderSettings(state);
  renderBatch(state);
  renderOutput(state);
  renderSelection();
  if (view.tab === 'row') loadRow();
}

// The columns of the grid of the rows, and the editable ones by field.
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
    // A column that the source fills stays a column: its rows are incomplete until their runs arrive.
    menu: (column) => (column.from_source ? [] : [{label: 'Make this a setting', action: () => makeSetting(column)}]),
  });
  return {
    columns: [named, ...notes, ...cells, statusColumn, ...(state.output.catalogue ? [publishedColumn] : [])],
    edits: Object.fromEntries(state.columns.map((c) => [`cells.${c.name}.value`, c])),
  };
}

// Sets the columns of the grid if they changed, such as after a table file is
// loaded. The first call only records them, since `build` sets them.
function showColumns(state) {
  // Options are not compared: an editor reads them when it opens (`current`).
  const shown = JSON.stringify([state.notes, state.columns.map((c) => [c.name, c.title, c.editor?.type, c.default, c.from_source])]);
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

// Downloads the results of `rows` as a zip, after a confirmation if some are
// outdated.
function downloadResults(rows, name) {
  const done = rows.filter((row) => row.result);
  const old = done.filter((row) => row.result === 'outdated').length;
  const question = `${old} of the ${done.length} results are outdated: they may not match the settings, the cells or the workflow now. Download them anyway?`;
  if (old && !confirm(question)) return;
  download('results.zip', name, {keys: done.map((row) => row.key)});
}

async function loadRuns() {
  const shown = await request('runs', {kind: 'runs'});
  if (!shown) return;
  view.runs = shown.revision;
  view.runData = shown;
  $('n-runs').textContent = shown.rows.length;
  if (view.page === 'runs') drawRuns();
}

// The batches that refer to each run, by run key, in the order of the list of
// batches. Built from `view.files`, which a failed count does not change.
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

// The batches of a run, as links that behave as the titles in the list of
// batches (see `choose`).
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
// Two batches may share a title, so the tooltip shows the label too.
const batchNames = (batches) => batches.map((b) => `${b.title} (${b.label})`).join(', ');

// Draws the runs last loaded, each with the batches that refer to it. Called
// when the Runs view is shown, and after each change while it is, such as a
// count.
function drawRuns() {
  const {columns, rows: shown, problem} = view.runData ?? {};
  if (!shown) return;
  const fields = columns.map((c) => c.title.toLowerCase()).join(' or ');
  $('runs-hint').textContent = problem ?? `Correct the ${fields} of a run. It changes no row of a batch; Follow adds the rows that become complete.`;
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

// Posts a correction of runs, an action of the workspace, and returns whether
// it was done. The page of a batch then refreshes its state, whose revision of
// the runs has changed, and so loads the runs. The page without a batch loads
// the runs itself.
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

// `listed` comes with each state, or from api/batches on the page without a
// batch.
function showBatches(listed) {
  view.batchData = listed;
  $('n-batches').textContent = listed.rows.length;
  drawBatches();
  drawFollowChoice();
}

// While the Batches or the Runs view is shown, counts the rows of each batch
// and fetches the files that each refers to. The next count starts 2 s after
// the response to the one before, since the server reads the state of every
// batch for a count, and the pages and Follow wait meanwhile. The Runs view
// counts too and does not wait for a change of the runs, since Follow adds
// rows that refer to runs without changing them. A failed count shows no
// counts, and the Runs view still shows the batches of the last count. A page that
// shows no batch also loads the list with each count. Nothing counts while
// the browser tab is hidden.
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

// The list of batches, with their counts or why a count failed. The rows are
// rebuilt only when the list changes. A count updates the cells in place, so
// that the keyboard focus, a text selection and a click are not lost.
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
      ...(n ? CHIPS.filter((s) => s !== 'all' && n[s]).map((s) => h('span', {class: `pill ${statusClass(s)}`, text: `${n[s]} ${s}`})) : []),
    );
  }
}

// A click on a batch in the list shows that batch: a click on its title in
// the Rows view, a click elsewhere in its row in the view shown. A click on
// the title with a modifier key is left to the browser, which opens a new tab
// or window. So is a middle click, which fires no `click`. The label and the
// description are text to select, so a click on them, or one that ends a text
// selection, does nothing. The titles in the Batches column of the Runs view
// behave the same.
function choose(event, label) {
  const title = event.target.closest('a');
  if (title && (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey)) return;
  if (event.target.closest('.text') || getSelection().toString()) return;
  event.preventDefault();
  const page = title ? 'rows' : view.page;
  if (label === shownLabel()) showPage(page);
  else show(batchUrl(label), page, 'pushState');
}

// Shows the page at `url`, of a batch or the root, in place of the page
// shown, in the view `page`, or the view shown if none. `how` is the
// `history` method that records it; none for Back and Forward. The root
// shows the batch that the server redirects it to, else the page without a
// batch. The grids stay and get the rows of the batch, as `render` draws a
// first state. An action sent before goes to the batch it was made for,
// including a typed value that the blur sends, and a requested file is still
// saved. Nothing of the batch before stays: the selection, the filter and the
// sort are those stored for the batch; forms, menus, dialogs and messages
// close; a response that would draw the batch before is dropped (`changes`,
// and `sent` for each `kind`). The page ignores clicks (`inert`) until the
// first state is drawn.
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
  $('output-form').shown = null;
  $('output-problem').textContent = '';
  if (page) showPage(page);
  drawBatches();
  if (shownLabel()) poll();
  else showWorkspace();
}

// The form "New batch". api/sources lists what a batch can be made from. The
// server validates the batch, and an error shows at the form. Opened on the
// page of a batch, the form starts with that batch's source, workflow,
// columns and settings, so that the next batch needs only a range that starts
// later. Follow is off unless ticked, since it stops the batch that follows
// now, which the page cannot make follow again, and it reduces rows at once.
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
    const at = choices.findIndex((c) => c.name === view.state.follow.row_source);
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

// Lists the workflows of the source chosen, and shows the fields of the way
// chosen.
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

// The text beside the Follow box.
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

// The range chosen: its first and last keys, null where open. `later` means
// no row now: the batch gets only the rows added later.
function rangeChosen() {
  const {first, last} = $('new-batch').elements;
  const later = first.selectedOptions[0]?.hasAttribute('data-later') ?? false;
  return later ? {first: null, last: null, later} : {first: first.value || null, last: last.value || null, later};
}

// With Follow ticked, or a range of no row now, the range has no last row,
// and "to" is cleared and disabled. Returns whether "to" was cleared, so that
// the rows are marked again.
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

// The parameter boxes of the workflow chosen. A parameter that the source
// fills is always a column.
function chooseSpec() {
  const {name, source_columns: sourceColumns} = chosenSource();
  const spec = chosenSpec();
  const batch = view.state;
  const ticked = batch ? batch.columns.map((c) => c.name) : [];
  $('new-columns').replaceChildren(
    ...(spec?.params ?? []).map((p) => {
      const fixed = sourceColumns.includes(p.name);
      const box = h('input', {type: 'checkbox', name: 'column', value: p.name, checked: fixed || ticked.includes(p.name), disabled: fixed});
      return h('label', {title: fixed ? `Filled by ${name}` : null}, box, p.title);
    }),
  );
  $('new-settings').hidden = !batch;
  $('new-like').textContent = batch ? `Start with the settings of ${batch.title}` : '';
}

// Loads the rows of the source for the range. Only a complete row can be the
// first or the last.
async function loadSourceRows(name) {
  const {elements} = $('new-batch');
  sourceRows = null;
  for (const end of [elements.first, elements.last]) end.replaceChildren();
  drawRange();
  const loaded = await markRange(name);
  if (!loaded) return;
  const complete = loaded.rows.filter((row) => row.mark !== 'incomplete');
  const option = (row) => h('option', {value: row.key, text: row.name === row.key ? row.key : `${row.name} (${row.key})`});
  elements.first.replaceChildren(
    h('option', {value: '', text: 'the first'}),
    ...complete.map(option),
    h('option', {value: '', 'data-later': true, text: 'no row now'}),
  );
  elements.last.replaceChildren(h('option', {value: '', text: 'the last'}), ...complete.map(option));
  chooseFollow();
}

// Fetches how the range chosen marks each row of the source, and draws it.
// If the server rejects the range, the error shows, and the rows show
// without marks.
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

function drawRange(marked = true) {
  const box = $('new-source-rows');
  if (!sourceRows) {
    $('new-rows').textContent = '';
    return box.replaceChildren(h('div', {class: 'empty', text: 'Listing the rows…'}));
  }
  box.replaceChildren(sourceTable(sourceRows, (row) => (marked ? row.mark : '')));
  if (!marked) return;
  const n = (mark) => sourceRows.rows.filter((row) => row.mark === mark).length;
  const incomplete = n('incomplete');
  const later = incomplete ? `; ${count(incomplete)} incomplete, which Follow adds once complete if the batch follows` : '';
  $('new-rows').textContent = `The batch gets ${count(n('taken'))}${later}.`;
  drawFollowChoice();
}

// A table of the rows of a source, with `status(row)` as a pill. With
// `picked`, each row gets a box, and each change calls `picked` with the
// keys checked.
function sourceTable({columns, rows: listed}, status, picked) {
  if (!listed.length) return h('div', {class: 'empty', text: 'No rows.'});
  const boxes = [];
  const pills = {incomplete: 'st-incomplete', taken: 'st-done'};
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

// Posts the form, one request at a time, through the queue, so that the batch
// starts with the settings of an Apply sent before.
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
          parameter_columns: [...form.querySelectorAll('[name=column]:checked')].map((box) => box.value),
          follow: way() === 'source' && elements.follow.checked,
        },
      ];
  const page = here;
  const made = await queued(() => request(url, {body, say, page}));
  if (made) show(batchUrl(made.label), 'rows', 'pushState');
  else button.disabled = false;
}

// The dialog "Add rows from the runs": the rows of the source that the batch
// lacks.
async function openAddRows() {
  const listed = await request('rows/available', {kind: 'available-rows'});
  if (!listed) return;
  const dialog = $('add-rows');
  const add = dialog.querySelector('[value=add]');
  let keys = [];
  const picked = (chosen) => {
    keys = chosen;
    add.disabled = keys.length === 0;
    add.textContent = keys.length ? `Add ${count(keys.length)}` : 'Add';
  };
  picked([]);
  $('add-rows-list').replaceChildren(
    listed.rows.length
      ? sourceTable(listed, (row) => row.mark, picked)
      : h('div', {class: 'empty', text: 'Every row of the source is in the batch.'}),
  );
  dialog.returnValue = '';
  dialog.onclose = () => dialog.returnValue === 'add' && act('rows/add', {keys});
  dialog.showModal();
}

// "Make this a setting" in the header menu of a column. If the rows differ,
// the person chooses a value, which is sent as the key of a row that holds it.
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

// The reduce button, its menu, and the badges of the Settings tab.
function renderAction(action, settingsChanged) {
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
  $('n-settings-changed').textContent = settingsChanged;
  const counted = {remaining: action.remaining, 'settings changed': settingsChanged, all: action.again};
  for (const [which, n] of Object.entries(counted)) {
    document.querySelector(`[data-reduce="${which}"]`).disabled = Boolean(action.blocked) || n === 0;
  }
  $('reduce-more').disabled = Boolean(action.blocked);
  for (const id of ['reduce-more', 'reduce-settings-changed']) $(id).title = action.blocked ?? '';
  $('settings-badge').hidden = !action.blocked;
  $('settings-badge').title = action.blocked ?? '';
  $('settings-changed-badge').hidden = settingsChanged === 0;
  $('settings-changed-badge').textContent = settingsChanged;
  $('settings-changed-badge').title = `${count(settingsChanged)} with changed settings`;
}

const CHIPS = ['all', 'ready', 'running', 'done', 'settings changed', 'outdated', 'failed', 'incomplete', 'cancelled'];

// The chip of each status is made once and updated in place, so that a chip
// with the keyboard focus does not lose it.
const chips = new Map();

function chip(status) {
  if (!chips.has(status)) {
    chips.set(
      status,
      h(
        'button',
        {
          type: 'button',
          class: `chip ${statusClass(status)}`,
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

// Shows why nothing was reduced, until the message is closed or the errors
// change.
function renderSubmitErrors(errors) {
  const key = JSON.stringify(errors);
  if (!errors.length) {
    if (shownMessage) hideMessage();
    return;
  }
  if (key === view.dismissed || key === shownMessage) return;
  showMessage('Nothing was reduced.', errors.map((e) => e.text), key);
}

function renderFollow(follow) {
  $('add-rows-open').hidden = follow.row_source === null;
  $('follow-status').hidden = $('stop-follow').hidden = !follow.on;
  const status = $('follow-status');
  status.classList.toggle('bad', Boolean(follow.failure));
  const incomplete = follow.incomplete ? `; ${count(follow.incomplete)} of the source incomplete` : '';
  status.textContent = follow.failure ?? `Following, every ${follow.interval} s${incomplete}`;
  status.title = status.textContent;
  rows.empty.textContent = follow.on ? 'No rows yet. Follow adds the rows of the runs that arrive.' : NO_ROWS;
}

function stopFollowing() {
  const question = `Stop following? This page cannot make "${view.state.title}" follow again. Rows that arrive later are added with "Add rows from the runs", or go to a new batch.`;
  if (confirm(question)) act('follow/stop');
}

// Apply and Discard are never disabled, since a click on them may first send
// a typed value, at the blur.
function renderSettings(state) {
  const {action, counts, settings_unapplied: changed, settings_problem: problem} = state;
  $('settings-changed').hidden = counts['settings changed'] === 0;
  $('settings-changed-state').textContent = `${count(counts['settings changed'])} with changed settings.`;
  $('reduce-settings-changed').disabled = Boolean(action.blocked);
  renderDraft($('draft'), changed, problem ? `${problem}.` : '', 'These settings are in force.');
  // The file holds the settings in force, so it waits for Apply or Discard.
  const download = document.querySelector('[data-file="settings.yaml"]');
  download.disabled = changed;
  download.title = changed ? `${action.blocked} first.` : '';
  const offer = (node) =>
    node.column
      ? h(
          'span',
          {class: 'column-note'},
          h('span', {class: 'hint', text: 'A column: a row may give its own', title: 'Each row whose cell is empty takes this value, also a row added later.'}),
          h('button', {
            type: 'button',
            class: 'quiet small',
            text: 'Default',
            title: 'Back to the default of the parameter, or to no setting if it has none: then a row whose cell is empty is incomplete.',
            onclick: () => act('settings', {path: node.path, op: 'reset'}),
          }),
        )
      : state.may_be_columns.includes(node.path[0]) &&
        h('button', {
          type: 'button',
          class: 'quiet small',
          text: 'Make this a column',
          title: 'Each row may then give its own value in its cell. A row whose cell is empty, also one added later, takes this setting.',
          onclick: () => act('settings/column', {name: node.path[0]}),
        });
  renderForm($('settings'), state.settings, (edit) => act('settings', edit), offer);
}

// The tab "Batch". The inputs are filled again only when the header changes,
// so that a typed value stays while the page polls.
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

function saveBatch(event) {
  event.preventDefault();
  const {elements} = $('batch-form');
  const say = (text) => ($('batch-problem').textContent = text);
  say('');
  act('batch', {title: elements.title.value.trim(), description: elements.description.value.trim()}, say);
}

// Removes this batch, then shows the root in its place in the history.
async function removeBatch() {
  const question = `Remove the batch "${view.state.title}"? Its title, description, settings and table are removed. Its results stay in the workspace.`;
  if (!confirm(question)) return;
  const [page, at] = [here, changes];
  const done = await queued(() => request('batch/remove', {body: {}, page}));
  if (!done || at !== changes) return;
  await show(root(), null, 'replaceState');
  toast(done.notice);
}

function replaceChanges() {
  return !view.state.settings_unapplied || confirm('The settings form holds changes that are not applied. Replace them?');
}

function selectedRows() {
  return view.state.rows.filter((row) => view.selected.includes(row.key));
}

function renderSelection() {
  const n = view.selected.length;
  $('selection').textContent = n ? `${n} selected` : '';
  $('remove').hidden = n === 0;
  $('n-selected').textContent = n;
  document.querySelector('[data-reduce="selected"]').disabled = n === 0 || Boolean(view.state.action.blocked);
  renderResultFiles();
  renderPublishButton();
  renderPlot();
}

// The downloads of results use the output options in force, so they are
// disabled while the Output form holds a change.
function renderResultFiles() {
  const done = view.state.rows.filter((row) => row.result);
  const doneSelected = selectedRows().filter((row) => row.result);
  const said = (rows) => {
    const old = rows.filter((row) => row.result === 'outdated').length;
    return old ? `${rows.length}, ${old} outdated` : String(rows.length);
  };
  $('n-results-selected').textContent = said(doneSelected);
  $('n-results-all').textContent = said(done);
  const held = outputChanged() ? 'Apply or discard the changes of the output form first.' : view.state.output.problem;
  for (const [name, results] of [['results-selected', doneSelected], ['results-all', done]]) {
    const item = document.querySelector(`[data-file="${name}"]`);
    item.disabled = Boolean(held) || results.length === 0;
    item.title = held;
  }
}

// The output

const publishedColumn = {
  title: 'Published',
  field: 'published',
  minWidth: 120,
  headerSort: false,
  tooltip: (event, cell) => cell.getValue(),
};

// The tab "Output": the options by which results are downloaded and, with a
// catalogue, published. The form holds changes until Apply, as the settings
// form does, but in the browser: the server holds one page per batch for
// every browser, so a draft on the server would be shared. New options from the server refill
// the inputs only if they hold no change, so that typed values stay.
function renderOutput(state) {
  $('publishing').hidden = !state.output.catalogue;
  const form = $('output-form');
  const shown = JSON.stringify(state.output);
  if (shown === form.shown) {
    renderOutputDraft();
    return;
  }
  const held = form.shown != null && outputChanged(JSON.parse(form.shown).options);
  form.shown = shown;
  if (!held) fillOutput(state.output);
  else renderOutputDraft();
}

function fillOutput({options: given, formats, outputs}) {
  $('output-format').replaceChildren(...formats.map((f) => h('option', {value: f.name}, f.title)));
  $('output-outputs').replaceChildren(
    ...outputs.map((o) => h('label', {}, h('input', {type: 'checkbox', name: 'outputs', value: o.name, checked: given.outputs.includes(o.name)}), ` ${o.title}`)),
  );
  const {elements} = $('output-form');
  elements.format.value = given.format;
  elements.name.value = given.name;
  elements.description.value = given.description;
  elements.keywords.value = given.keywords.join(', ');
  elements.owner.value = given.owner;
  elements.contact_email.value = given.contact_email;
  renderOutputDraft();
}

// The inputs of the Output form, as the body of output/form.
function outputForm() {
  const {elements} = $('output-form');
  return {
    format: elements.format.value,
    outputs: [...document.querySelectorAll('#output-outputs input:checked')].map((i) => i.value),
    name: elements.name.value,
    description: elements.description.value,
    keywords: elements.keywords.value.split(','),
    owner: elements.owner.value,
    contact_email: elements.contact_email.value,
  };
}

// A form in a canonical text, the same from the server and from the inputs:
// without the spaces around each text, the line ends of a textarea, empty
// keywords, or the order of the outputs.
function normal(form) {
  const text = (value) => value.replace(/\r\n/g, '\n').trim();
  return JSON.stringify([
    form.format,
    [...form.outputs].sort(),
    text(form.name),
    text(form.description),
    form.keywords.map(text).filter(Boolean),
    text(form.owner),
    text(form.contact_email),
  ]);
}

// Whether the inputs differ from `form`, by default the output options in
// force. A format missing from the select compares as none, as the select
// shows it.
function outputChanged(form = view.state?.output.options) {
  if (!form) return false;
  const known = view.state.output.formats.some((f) => f.name === form.format);
  return normal(outputForm()) !== normal(known ? form : {...form, format: ''});
}

// A format with outputs of its own (`format.outputs`) ignores the outputs
// chosen, so the choice is hidden.
function renderOutputDraft() {
  renderDraft($('output-draft'), outputChanged(), view.state.output.problem, 'These output options are in force.');
  const format = view.state.output.formats.find((f) => f.name === $('output-format').value);
  $('output-outputs-field').hidden = format?.outputs != null;
  $('output-format-description').textContent = format?.description ?? '';
  renderResultFiles();
  renderPublishButton();
}

// The bar above the Settings and the Output form: whether the form holds a
// change not applied, and `said`, such as why the form is invalid, else
// `idle`.
function renderDraft(bar, changed, said, idle) {
  bar.classList.toggle('changed', changed);
  bar.querySelector('.settings-bar-state').textContent = changed ? `Changed, not applied yet. ${said}`.trim() : said || idle;
}

function toPublish() {
  return selectedRows().filter((row) => row.publishable);
}

// Why Publish is disabled, or '' if it is enabled.
function publishBlocked() {
  const {problem, unpublishable} = view.state.output;
  if (view.publishing) return 'Publishing…';
  if (outputChanged()) return 'Apply or discard the changes of the form first.';
  if (problem) return problem;
  if (unpublishable) return unpublishable;
  if (!toPublish().length) return 'Select rows that are done or settings changed and not published.';
  return '';
}

function renderPublishButton() {
  if (!view.state.output.catalogue) return;
  const n = toPublish().length;
  const blocked = publishBlocked();
  $('publish').disabled = Boolean(blocked);
  $('publish').textContent = n ? `Publish ${n} selected` : 'Publish';
  $('publish-hint').textContent = blocked || 'Publishes the result of each selected row that is done or settings changed and not published, with the output options in force.';
}

function sayOutput(text) {
  $('output-problem').textContent = text;
}

function applyOutput(event) {
  event.preventDefault();
  sayOutput('');
  act('output/form', outputForm(), sayOutput);
}

function discardOutput() {
  sayOutput('');
  fillOutput(view.state.output);
}

// The button stays disabled until the response, so that a second click does
// not publish a result twice.
async function publish() {
  const keys = toPublish().map((row) => row.key);
  if (!confirm(`Publish ${keys.length} result${keys.length === 1 ? '' : 's'} to the catalogue? A dataset cannot be removed from this page.`)) return;
  view.publishing = true;
  renderPublishButton();
  try {
    await act('publish', {keys});
  } finally {
    view.publishing = false;
    if (view.state) renderPublishButton();
  }
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

// The legend: a line per row, with the colour of its curve, dashed if
// outdated, and its status unless done. Above it, a count per status, with
// the reason if all its rows share it. For `settings changed`, that is the
// settings that changed, since the full reason is long.
function renderLegend() {
  const shown = view.selected.length ? selectedRows() : view.state.rows.filter((row) => row.key in view.colors);
  const groups = Map.groupBy(shown.filter((row) => row.status !== 'done'), (row) => row.status);
  $('legend-summary').replaceChildren(
    ...[...groups].map(([status, group]) => {
      const reasons = new Set(group.map((row) => (status === 'settings changed' ? row.changed : row.reason)));
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
        row.status !== 'done' && h('span', {class: `pill ${statusClass(row.status)}`, text: row.status}),
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
  const reasonClass = {failed: 'bad', cancelled: 'bad', incomplete: 'warn', outdated: 'info', 'settings changed': 'settings-changed'}[row.status];
  const useSettings = () => replaceChanges() && act('row/use-settings', {key: row.key}).then((done) => done && showTab('settings'));
  const settingsNames = Object.fromEntries([...view.state.columns.map((c) => [c.name, c.title]), ...view.state.settings.map((n) => [n.path[0], n.label])]);
  const parts = [
    h('div', {class: 'row-head'}, h('h2', {text: row.name}), h('span', {class: `pill ${statusClass(row.status)}`, text: row.status})),
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
            h('p', {class: 'hint differs', text: `Differs from what the row takes now in ${detail.differs.map((n) => settingsNames[n] ?? n).join(', ')}.`}),
          summary(detail.reduced_with, detail.differs),
          detail.differs.length > 0 &&
            h('button', {type: 'button', text: 'Put these settings in the form', onclick: useSettings}),
        )
      : !row.record && h('p', {class: 'hint', text: 'Not reduced yet.'}),
  ];
  const pane = $('tab-row');
  const focused = pane.contains(document.activeElement) && document.activeElement.textContent;
  pane.replaceChildren(...parts.filter(Boolean));
  // The buttons are rebuilt; the focus returns to the one with the same text.
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

// Shows the view `page`. A `chosen` view becomes `view.page` and is stored.
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
  for (const name of ['result', 'settings', 'row', 'batch', 'output']) $(`tab-${name}`).hidden = name !== tab;
  if (tab === 'row' && view.state) loadRow();
  if (tab === 'result' && !$('plot').hidden) Plotly.Plots.resize($('plot'));
}

function reduceSettingsChanged() {
  const n = view.state.counts['settings changed'];
  const question = `Reduce the ${count(n)} with changed settings again, with the settings now?`;
  if (confirm(question)) act('reduce', {which: 'settings changed'});
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
      if (item.dataset.reduce === 'settings changed') reduceSettingsChanged();
      else act('reduce', {which: item.dataset.reduce, keys: view.selected});
    };
  });
  $('cancel').onclick = () => act('cancel');
  $('stop-follow').onclick = stopFollowing;
  $('settings-apply').onclick = () => act('settings/apply');
  $('settings-discard').onclick = () => act('settings/discard');
  $('reduce-settings-changed').onclick = reduceSettingsChanged;
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
  $('add-rows-open').onclick = openAddRows;
  newBatch.querySelector('[data-cancel]').onclick = closeNewBatch;
  newBatch.addEventListener('keydown', (event) => event.key === 'Escape' && closeNewBatch());
  $('batch-form').onsubmit = saveBatch;
  $('output-form').onsubmit = applyOutput;
  $('output-form').oninput = () => {
    sayOutput('');
    renderOutputDraft();
  };
  $('output-discard').onclick = discardOutput;
  $('publish').onclick = publish;
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
    act('rows/add-empty', {key}).then((done) => done && rows.reveal(key));
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
    'table.csv': () => download('table.csv', 'table.csv'),
    'table.xlsx': () => download('table.xlsx', 'table.xlsx'),
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

// The page without a batch: at the root when the server redirects to no
// batch, or at a batch that cannot be shown, with `why`. It shows the Batches
// view, with "New batch" open if there is no batch, and the Runs view, loaded
// once. No state is polled; the list of batches reloads with each count.
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
