// What the list of batches and the list of workspaces share: a row with the
// title as a link, the name and the description; what a click on it does; and
// the fields Title and Description of a form. A page adds its own columns.

import {h} from './dom.js';

// The handler of a click on a row of a list, or on a link that behaves as its
// title. A click on the title with a modifier key is left to the browser,
// which opens a new tab or window. So is a middle click, which fires no
// `click`. The name and the description are text to select, so a click on
// them, or one that ends a text selection, does nothing. Any other click calls
// `open(onTitle)`, with whether it was on the title, in place of following the
// link.
export function onChoose(open) {
  return (event) => {
    const title = event.target.closest('a');
    if (title && (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey)) return;
    if (event.target.closest('.text') || getSelection().toString()) return;
    event.preventDefault();
    open(Boolean(title));
  };
}

// The row of `item`, {name, title, description}, whose title links to `href`,
// followed by `cells`.
export function listRow(item, href, open, ...cells) {
  return h(
    'tr',
    {class: 'item', 'data-name': item.name, onclick: onChoose(open)},
    h(
      'td',
      {},
      h('a', {href, text: item.title}),
      ' ', // so that a double click selects the name alone
      h('small', {class: 'text', text: item.name}),
      item.description && h('div', {class: 'hint text description', text: item.description}),
    ),
    ...cells,
  );
}

// Fills the body of `table` with `row(item)` for each of `items`, or with the
// hint `empty`. Only when the items changed, so that the keyboard focus, a text
// selection and a click are not lost. Returns the rows.
export function fillList(table, items, row, empty) {
  const body = table.tBodies[0];
  const listed = JSON.stringify(items);
  if (listed !== body.listed) {
    body.listed = listed;
    const columns = table.tHead.rows[0].cells.length;
    const none = h('tr', {}, h('td', {class: 'hint', colspan: columns, text: empty}));
    body.replaceChildren(...(items.length ? items.map(row) : [none]));
  }
  return body.querySelectorAll('tr.item');
}

// The fields Title and Description of a form, with the ids `${prefix}-title`
// and `${prefix}-description`.
export function headerFields(prefix) {
  return [
    h('div', {class: 'field'}, h('label', {for: `${prefix}-title`, text: 'Title'}), h('input', {type: 'text', id: `${prefix}-title`, name: 'title'})),
    h(
      'div',
      {class: 'field'},
      h('label', {for: `${prefix}-description`, text: 'Description'}),
      h('textarea', {id: `${prefix}-description`, name: 'description', rows: 3}),
    ),
  ];
}

// The title and the description of a form with `headerFields`, trimmed.
export function headerOf(form) {
  const {title, description} = form.elements;
  return {title: title.value.trim(), description: description.value.trim()};
}
