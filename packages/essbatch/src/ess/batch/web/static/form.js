// A form from the nodes that the server sends (see forms.py). Each change
// goes to `edit` as {path, op, value}, and the server returns the whole form,
// which replaces this one.
import {h} from './dom.js';

// A new form replaces the one shown, and the focus returns to the input of the
// same path. The new form waits while the focused input holds a typed value
// not yet sent, so that the caret stays and no half-typed value is sent. It
// also waits while an edit is in flight, since the response to that edit is
// newer. A waiting form is drawn once neither holds; it is never dropped.
// `edit` returns a promise that settles once its response is drawn.
// `action(node)` returns an element for the heading of a parameter, or null.
export function renderForm(container, nodes, edit, action = () => null) {
  container.onfocusout ??= (event) => drawWaiting(container, event.relatedTarget);
  const json = JSON.stringify(nodes);
  if (json !== container.given) {
    container.given = json;
    container.sending ??= 0;
    const send = (change) => {
      container.given = null; // draw the response even if it equals this form
      container.sending += 1;
      Promise.resolve(edit(change)).finally(() => {
        container.sending -= 1;
        drawWaiting(container);
      });
    };
    container.waiting = () =>
      container.replaceChildren(
        ...nodes.map((node) =>
          h('section', {class: 'param'}, h('h3', {}, h('span', {text: node.label}), action(node)), ...body(node, send)),
        ),
      );
  }
  drawWaiting(container);
}

// Draws the waiting form, if there is one and nothing holds it, and returns
// the focus to `focused`.
function drawWaiting(container, focused = document.activeElement) {
  const active = document.activeElement;
  const typed = container.contains(active) && active.drawn !== undefined && active.value !== active.drawn;
  if (!container.waiting || container.sending || typed) return;
  const draw = container.waiting;
  container.waiting = null;
  const path = container.contains(focused) ? focused.dataset.path : null;
  draw();
  if (path) container.querySelector(`[data-path='${CSS.escape(path)}']`)?.focus();
}

function body(node, edit) {
  switch (node.kind) {
    case 'group':
      return node.children.map((child) => field(child, edit));
    case 'choice':
      return [node.selector && alternatives(node, edit), node.child && body(node.child, edit)].flat();
    case 'input':
      return [problem(node, control(node, edit))];
    case 'scalars':
      return [problem(node, scalars(node, edit))];
    case 'rows':
      return [h('p', {class: 'hint', text: `${rowCount(node)} rows. Edit them in the settings file.`})];
    default:
      return [];
  }
}

function field(node, edit) {
  if (node.kind === 'input' || node.kind === 'scalars') {
    const input = node.kind === 'input' ? control(node, edit) : scalars(node, edit);
    const target = input.matches('input, select') ? input : input.querySelector('input, select');
    return h(
      'div',
      {class: `field${node.problem ? ' bad' : ''}`},
      h('label', {text: node.label, title: node.description, for: target?.id}),
      input,
      node.problem && h('span', {class: 'problem', text: node.problem}),
    );
  }
  return h('fieldset', {}, h('legend', {text: node.label}), ...body(node, edit));
}

function problem(node, element) {
  if (!node.problem) return element;
  return h('div', {class: 'field bad'}, element, h('span', {class: 'problem', text: node.problem}));
}

function alternatives(node, edit) {
  return h(
    'div',
    {class: 'alternatives', role: 'radiogroup', 'aria-label': node.label},
    node.alternatives.map((alternative) =>
      h('button', {
        type: 'button',
        role: 'radio',
        'aria-checked': String(alternative.value === node.chosen),
        'data-path': JSON.stringify([...node.path, `alternative:${alternative.value}`]),
        text: alternative.label,
        onclick: () => edit({path: node.path, op: 'choose', value: alternative.value}),
      }),
    ),
  );
}

const isSwitch = (node) => node.options.length === 2 && node.options.join() === 'True,False';

// A text or number input sends its value on `change` (Enter or leaving), if
// it differs from `drawn`, the value drawn or sent last. Escape restores
// `drawn`.
function control(node, edit) {
  const path = JSON.stringify(node.path);
  const props = {id: `f-${path}`, 'data-path': path, 'aria-invalid': node.problem ? 'true' : null};
  const send = (value) => edit({path: node.path, op: 'set', value});
  let input;
  if (node.input === 'pick' && isSwitch(node)) {
    input = h('input', {...props, type: 'checkbox', checked: node.value === 'True'});
    input.addEventListener('change', () => send(input.checked ? 'True' : 'False'));
    return h('span', {class: 'switch'}, input);
  }
  if (node.input === 'pick') {
    input = h(
      'select',
      props,
      node.value == null && h('option', {value: '', text: 'Choose…'}),
      node.options.map((o) => h('option', {value: o, text: o, selected: o === node.value})),
    );
    input.addEventListener('change', () => send(input.value || null));
    return input;
  }
  const number = node.input === 'float' || node.input === 'int';
  input = h('input', {
    ...props,
    type: number ? 'number' : node.input === 'time' ? 'datetime-local' : 'text',
    step: node.input === 'int' ? '1' : number ? 'any' : null,
    value: node.value ?? '',
  });
  input.drawn = input.value;
  input.addEventListener('change', () => {
    if (input.value === input.drawn) return;
    input.drawn = input.value;
    if (number) send(input.value === '' ? null : Number(input.value));
    else send(input.value);
  });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      input.value = input.drawn;
      input.blur();
    }
  });
  return input;
}

function scalars(node, edit) {
  const step = (op) => () => edit({path: node.path, op});
  return h(
    'span',
    {class: 'scalars', id: `f-${JSON.stringify(node.path)}`},
    node.elements.map((element) => control(element, edit)),
    h('button', {type: 'button', class: 'quiet small', title: 'Add one', onclick: step('add')}, '+'),
    node.elements.length > 0 &&
      h('button', {type: 'button', class: 'quiet small', title: 'Remove the last', onclick: step('remove')}, '−'),
  );
}

function rowCount(node) {
  const first = Object.values(node.columns)[0];
  return first ? first.length : 0;
}

// The form as text, a line per parameter, such as what a result was reduced
// with. `differs` lists the parameters to mark.
export function summary(nodes, differs = []) {
  return h(
    'dl',
    {class: 'summary'},
    nodes.map((node) => {
      const mark = differs.includes(node.path[0]) ? 'differs' : null;
      return [h('dt', {class: mark, text: node.label}), h('dd', {class: mark, text: text(node) || '—'})];
    }),
  );
}

// "From 2 Å" for the label "From (Å)" and the value 2.
function labelled(node) {
  const [, label, unit] = node.label.match(/^(.*) \((.+)\)$/) ?? [null, node.label, ''];
  return [label, text(node), unit].filter(Boolean).join(' ');
}

function text(node) {
  switch (node.kind) {
    case 'group':
      return node.children.map(labelled).join(', ');
    case 'choice': {
      const alternative = node.alternatives.find((a) => a.value === node.chosen);
      const inner = node.child ? text(node.child) : '';
      return node.selector ? [alternative?.label, inner].filter(Boolean).join(': ') : inner;
    }
    case 'input':
      return {True: 'yes', False: 'no'}[node.value] ?? String(node.value ?? '—');
    case 'scalars':
      return node.elements.map(text).join(', ') || 'none';
    case 'rows':
      return `${rowCount(node)} rows`;
    default:
      return '';
  }
}
