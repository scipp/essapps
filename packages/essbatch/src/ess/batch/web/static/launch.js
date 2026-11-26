// The launch page: the workspaces of the root folder, and the form "New
// workspace". A click on a workspace opens it and loads its batch page, at
// workspaces/<name>/. Every URL is relative to the page's own.

import {h, get, post} from './dom.js';
import {fillList, headerFields, headerOf, listRow} from './list.js';

const $ = (id) => document.getElementById(id);
const workspaceUrl = (name) => new URL(`workspaces/${encodeURIComponent(name)}/`, location.href).href;
const action = (name, verb) => `workspaces/${encodeURIComponent(name)}/${verb}`;

let listed = null; // the workspaces, as last loaded

function showMessage(text) {
  $('message').querySelector('.message-text').textContent = text;
  $('message').hidden = !text;
}

let toastTimer;
function toast(text) {
  const box = $('toast');
  box.textContent = text;
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (box.hidden = true), 3500);
}

// The name of a file source as the form and the list show it.
const sourceName = (files) => (files === 'local' ? 'files.csv of the folder' : files.replace(/^scicat:/, 'SciCat '));

async function load() {
  const {ok, body} = await get('workspaces', location.href);
  if (!ok) return showMessage(body.message);
  listed = body;
  if (body.application) document.title = $('title').textContent = `${body.application}: workspaces`;
  draw();
  if (!body.rows.length) openForm();
}

function draw() {
  const rowOf = (workspace) =>
    listRow(
      workspace,
      workspaceUrl(workspace.name),
      () => open(workspace),
      h('td', {text: workspace.proposal}),
      h('td', {text: sourceName(workspace.files)}),
      h('td', {class: workspace.problem ? 'bad' : '', text: workspace.problem ?? (workspace.open ? 'open' : '')}),
      h('td', {}, h('button', {type: 'button', class: 'quiet remove', text: 'Remove', onclick: (event) => remove(event, workspace)})),
    );
  fillList($('workspaces'), listed.rows, rowOf, 'No workspace yet.');
}

// Opens the workspace on the server, then loads its batch page. The server
// would open it at the first request of that page too, but here a refusal
// shows as a message.
async function open({name}) {
  const {ok, body} = await post(action(name, 'open'), {}, location.href);
  if (ok) return location.assign(workspaceUrl(name));
  showMessage(body.message);
  await load();
}

async function remove(event, {name, title}) {
  event.stopPropagation(); // not a click on the row
  const question =
    `Remove the workspace "${title}"? Its folder is deleted: the batches, the history and the stored outputs, ` +
    'including the history of the datasets published to SciCat. The datasets stay in SciCat.';
  if (!confirm(question)) return;
  const {ok, body} = await post(action(name, 'remove'), {}, location.href);
  if (ok) toast(body.notice);
  showMessage(ok ? '' : body.message);
  await load();
}

function openForm() {
  const form = $('new-workspace');
  form.reset();
  form.elements.files.replaceChildren(...listed.sources.map((files) => h('option', {value: files, text: sourceName(files)})));
  $('new-files-field').hidden = listed.sources.length < 2;
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
  await open({name});
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
