// The launch page: the workspaces of the root folder, and the form "New
// workspace". A click on a workspace opens it and loads its batch page, at
// workspaces/<name>/. An open workspace can be closed, and any workspace
// removed. Every URL is relative to the page's own.

import {h, get, post, toast} from './dom.js';
import {fillList, headerFields, headerOf, listRow} from './list.js';

const $ = (id) => document.getElementById(id);
const workspaceUrl = (name) => new URL(`workspaces/${encodeURIComponent(name)}/`, location.href).href;
const action = (name, verb) => `workspaces/${encodeURIComponent(name)}/${verb}`;

let listed = null; // the workspaces, as last loaded

function showMessage(text) {
  $('message').querySelector('.message-text').textContent = text;
  $('message').hidden = !text;
}

// The name of a file source as the form and the list show it; null if the
// workspace.json of a workspace cannot be read.
const sourceName = (files) => (files === 'local' ? 'files.csv of the folder' : (files ?? '').replace(/^scicat:/, 'SciCat '));

async function load() {
  const {ok, body} = await get('workspaces', location.href);
  if (!ok) return showMessage(body.message);
  listed = body;
  if (body.application) document.title = $('title').textContent = `${body.application}: workspaces`;
  draw();
  if (!body.rows.length) openForm();
}

function draw() {
  const button = (text, title, onclick) =>
    h('button', {type: 'button', class: `quiet ${text.toLowerCase().split(' ')[0]}`, text, title, onclick});
  const rowOf = (workspace) =>
    listRow(
      workspace,
      workspaceUrl(workspace.name),
      () => openWorkspace(workspace),
      h('td', {text: workspace.proposal ?? ''}),
      h('td', {text: sourceName(workspace.files)}),
      h('td', {class: workspace.problem ? 'bad' : '', text: workspace.problem ?? (workspace.open ? 'open' : '')}),
      h(
        'td',
        {},
        !workspace.problem &&
          button('Clear outputs', `Delete the stored outputs, to free disk space. ${KEPT}`, (event) => clearWorkspace(event, workspace)),
        button('Remove', 'Delete the folder of the workspace, with its batches, history and outputs.', (event) => removeWorkspace(event, workspace)),
      ),
    );
  fillList($('workspaces'), listed.rows, rowOf, 'No workspace yet.');
}

// Opens the workspace on the server, then loads its batch page. The server
// would open it at the first request of that page too, but here a refusal
// shows as a message.
async function openWorkspace({name}) {
  const {ok, body} = await post(action(name, 'open'), {}, location.href);
  if (ok) return location.assign(workspaceUrl(name));
  showMessage(body.message);
  await load();
}

// Posts `verb` of the workspace, then shows what the server said and loads
// the list again.
async function act(name, verb) {
  const {ok, body} = await post(action(name, verb), {}, location.href);
  if (ok) toast(body.notice);
  showMessage(ok ? '' : body.message);
  await load();
}

// What a clear of the outputs keeps, as against Remove.
const KEPT = 'The batches, their settings and the history stay, and a reduction of the same rows computes the outputs again.';

async function clearWorkspace(event, {name, title}) {
  event.stopPropagation(); // not a click on the row
  if (confirm(`Clear the outputs of the workspace "${title}", to free disk space? ${KEPT}`)) await act(name, 'clear');
}

async function removeWorkspace(event, {name, title}) {
  event.stopPropagation(); // not a click on the row
  const question =
    `Remove the workspace "${title}"? Its folder is deleted: the batches, the history and the stored outputs, ` +
    'including the history of the datasets published to SciCat. The datasets stay in SciCat.';
  if (confirm(question)) await act(name, 'remove');
}

function openForm() {
  const form = $('new-workspace');
  form.reset();
  form.elements.files.replaceChildren(...listed.allowed.map((files) => h('option', {value: files, text: sourceName(files)})));
  $('new-files-field').hidden = listed.allowed.length < 2;
  $('new-problem').textContent = '';
  form.hidden = false;
  $('new-workspace-open').hidden = true;
  form.elements.name.focus();
}

function closeForm() {
  $('new-workspace').hidden = true;
  $('new-workspace-open').hidden = false;
}

// Makes the workspace, then loads its batch page.
async function make(event) {
  event.preventDefault();
  const form = $('new-workspace');
  const {elements} = form;
  const name = elements.name.value.trim();
  const body = {name, ...headerOf(form), proposal: elements.proposal.value.trim(), files: elements.files.value};
  const done = await post('workspaces', body, location.href);
  if (!done.ok) return ($('new-problem').textContent = done.body.message);
  await openWorkspace({name});
}

function wire() {
  const form = $('new-workspace');
  form.elements.name.closest('.field').after(...headerFields('new'));
  form.onsubmit = make;
  form.querySelector('[data-cancel]').onclick = closeForm;
  form.addEventListener('keydown', (event) => event.key === 'Escape' && closeForm());
  $('new-workspace-open').onclick = openForm;
  document.querySelector('#message .close').onclick = () => showMessage('');
}

wire();
load();
