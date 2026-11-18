// The one element helper, and the calls of the API.

// h('button', {class: 'primary', onclick: f}, 'Reduce') makes an element.
// A prop that starts with "on" is an event handler, `class` and `text` set
// those, and any other prop is an attribute, left out if false or null.
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

// Each call returns {ok, status, body}; a refusal (422) is not thrown. A file
// is sent as it is, anything else as JSON. `path` is relative to the API, which
// is next to the page, so that the page works under any prefix.
async function call(method, path, body) {
  const init = {method, headers: {}};
  if (body !== undefined) {
    const raw = body instanceof Blob;
    init.headers['Content-Type'] = raw ? 'application/octet-stream' : 'application/json';
    init.body = raw ? body : JSON.stringify(body);
  }
  const response = await fetch(`api/${path}`, init);
  const isJson = response.headers.get('Content-Type')?.includes('json');
  return {
    ok: response.ok,
    status: response.status,
    body: isJson ? await response.json() : await response.blob(),
  };
}

export const get = (url) => call('GET', url);
export const post = (url, body = {}) => call('POST', url, body);

export function query(params) {
  const search = new URLSearchParams();
  for (const [name, values] of Object.entries(params)) {
    for (const value of [].concat(values)) search.append(name, value);
  }
  return search.toString();
}

// Save a blob as a file of the browser's downloads.
export function save(blob, name) {
  const url = URL.createObjectURL(blob);
  h('a', {href: url, download: name}).click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
