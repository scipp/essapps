// A form from the nodes that the server sends (see forms.py): one section per
// parameter, the fields of a group in rows, a choice as a row of buttons.
// Each change is sent as {path, op, value} through `edit`; the server answers
// with the form again, which replaces this one.
import {h} from './dom.js';

export function renderForm(container, nodes, edit) {
  const focused = document.activeElement?.dataset?.path;
  container.replaceChildren(
    ...nodes.map((node) =>
      h('section', {class: 'param'}, h('h3', {text: node.label}), ...body(node, edit)),
    ),
  );
  if (focused) container.querySelector(`[data-path='${CSS.escape(focused)}']`)?.focus();
}

function body(node, edit) {
  switch (node.kind) {
    case 'group':
      return node.children.map((child) => field(child, edit));
    case 'choice':
      return [node.selector && ways(node, edit), node.child && body(node.child, edit)].flat();
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

// A field of a group: a labelled row, or a box of its own fields.
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

function ways(node, edit) {
  return h(
    'div',
    {class: 'ways', role: 'radiogroup', 'aria-label': node.label},
    node.ways.map((way) =>
      h('button', {
        type: 'button',
        role: 'radio',
        'aria-checked': String(way.value === node.chosen),
        'data-path': JSON.stringify([...node.path, `way:${way.value}`]),
        text: way.label,
        onclick: () => edit({path: node.path, op: 'choose', value: way.value}),
      }),
    ),
  );
}

const isSwitch = (node) => node.options.length === 2 && node.options.join() === 'True,False';

// The input of one value. Enter or leaving the input sends it; Escape puts
// back what the server holds.
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
  input.addEventListener('change', () => {
    if (number) send(input.value === '' ? null : Number(input.value));
    else send(input.value);
  });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      input.value = node.value ?? '';
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

// The form as text, for what a result was reduced with: a line per parameter.
// `differs` names the parameters to mark.
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

// A field with its label: "From 2 Å" for a label "From (Å)" and 2.
function labelled(node) {
  const [, label, unit] = node.label.match(/^(.*) \((.+)\)$/) ?? [null, node.label, ''];
  return [label, text(node), unit].filter(Boolean).join(' ');
}

function text(node) {
  switch (node.kind) {
    case 'group':
      return node.children.map(labelled).join(', ');
    case 'choice': {
      const way = node.ways.find((w) => w.value === node.chosen);
      const inner = node.child ? text(node.child) : '';
      return node.selector ? [way?.label, inner].filter(Boolean).join(': ') : inner;
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
