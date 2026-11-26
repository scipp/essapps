// The element helper `h`, the calls of the API, and the toast.

// h('button', {class: 'primary', onclick: f}, 'Reduce') returns a button.
// A prop that starts with "on" adds an event handler. `class` and `text` set
// the class and the text. Any other prop sets an attribute, omitted if false
// or null.
export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [name, value] of Object.entries(props)) {
    if (name.startsWith('on')) el.addEventListener(name.slice(2), value);
    else if (name === 'class') el.className = value;
    else if (name === 'text') el.textContent = value;
    else if (value === true) el.setAttribute(name, '');
    else if (value !== false && value != null) el.setAttribute(name, value);
  }
  el.append(...children.flat(Infinity).filter((c) => c != null && c !== false));
  return el;
}

// Each call returns {ok, status, body} and does not throw on an error status.
// A file is sent as raw bytes, anything else as JSON. `path` is relative to
// the API next to the page at `page`, so that the page works under any URL
// prefix.
async function call(method, path, body, page) {
  const init = {method, headers: {}};
  if (body !== undefined) {
    const raw = body instanceof Blob;
    init.headers['Content-Type'] = raw ? 'application/octet-stream' : 'application/json';
    init.body = raw ? body : JSON.stringify(body);
  }
  const response = await fetch(new URL(`api/${path}`, page), init);
  const isJson = response.headers.get('Content-Type')?.includes('json');
  return {
    ok: response.ok,
    status: response.status,
    body: isJson ? await response.json() : await response.blob(),
  };
}

export const get = (url, page) => call('GET', url, undefined, page);
export const post = (url, body, page) => call('POST', url, body, page);

export function query(params) {
  const search = new URLSearchParams();
  for (const [name, values] of Object.entries(params)) {
    for (const value of [].concat(values)) search.append(name, value);
  }
  return search.toString();
}

export function save(blob, name) {
  const url = URL.createObjectURL(blob);
  h('a', {href: url, download: name}).click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// Shows `text` in the element #toast for a few seconds.
let toastTimer;
export function toast(text) {
  const box = document.getElementById('toast');
  box.textContent = text;
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (box.hidden = true), 3500);
}
