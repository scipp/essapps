# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
F7: Tabulator's ``addData``, which ``Grid.draw`` uses for the first rows, costs
quadratic time in the rows; ``setData`` does not. Milliseconds per call.
"""

from harness import browser, new_page, serve_demo, show

JS = """async ([n, how]) => {
  const {TabulatorFull: T} = await import('/static/vendor/tabulator_esm.min.js');
  const div = document.createElement('div');
  div.style.height = '600px';
  document.body.append(div);
  const columns = [{title: 'Name', field: 'name'}];
  const t = new T(div, {index: 'key', height: '600px', columns});
  await new Promise((r) => t.on('tableBuilt', r));
  const rows = Array.from({length: n}, (_, i) => ({key: String(i), name: `row ${i}`}));
  const t0 = performance.now();
  if (how === 'top') await t.addData(rows, true);
  else if (how === 'bottom') await t.addData(rows, false);
  else await t.setData(rows);
  const ms = performance.now() - t0;
  t.destroy();
  div.remove();
  return Math.round(ms);
}"""

with browser() as b, serve_demo() as served:
    page = new_page(b, served['url'])
    for n in (1000, 3000, 6000):
        show(n, {how: page.evaluate(JS, [n, how]) for how in ('top', 'bottom', 'set')})
