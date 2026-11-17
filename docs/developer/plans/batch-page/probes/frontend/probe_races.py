# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
F3, F4, F5, F6, F9: the page's handling of states and of fetches outside its
actions, each scenario on a demo server of its own.

Usage: probe_races.py [F3 F4-filter F4-sort F5 F6 F9]  (all if none given)
"""

import sys
from typing import Any

from harness import browser, serve_demo, show

from ess.apps.batch.web import demo

# Delivers the answers of fetches whose URL ends with `match` `ms` late. The
# body is that of the time of the request, so answers arrive out of order.
DELAY = """
(() => {
  const fetch0 = window.fetch;
  window.__delays = [];
  window.fetch = async (url, init) => {
    const r = await fetch0(url, init);
    const d = window.__delays.find(
      (d) => String(url).split("?")[0].endsWith(d.match) && d.n > 0
    );
    if (d) { d.n -= 1; await new Promise((res) => setTimeout(res, d.ms)); }
    return r;
  };
})();
"""
CORRECT = """([file, value]) => fetch('/api/runs/cells', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({keys: [file], column: 'sample', value}),
})"""
PILLS = """[...document.querySelectorAll('#rows .tabulator-row')].map(
  (r) => r.querySelector('.pill')?.textContent
)"""


def delay(page: Any, match: str, ms: int, n: int = 1) -> None:
    page.evaluate(f'window.__delays.push({{match: {match!r}, ms: {ms}, n: {n}}})')


def page_of(b: Any, url: str, errors: list[str]) -> Any:
    context = b.new_context(viewport={'width': 1440, 'height': 900})
    context.add_init_script(DELAY)
    page = context.new_page()
    page.on(
        'console',
        lambda m: errors.append(f'console: {m.text}') if m.type == 'error' else None,
    )
    page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
    page.goto(url)
    page.wait_for_selector('#rows .tabulator-row')
    return page


def check(name: str) -> str:
    """The selector of the box that selects the row named ``name``."""
    return f'#rows .tabulator-row:has-text("{name}") .tabulator-row-header input'


def reduce_one(page: Any, served: dict[str, Any], name: str) -> None:
    """Reduce the row named ``name`` from the menu of Reduce, and wait for it."""
    page.click(check(name))
    page.click('#reduce-more')
    page.click('[data-reduce=selected]')
    served['client'].wait(served['client'].records())
    page.wait_for_selector('.chip.st-done')
    page.wait_for_timeout(1200)


def row_tab_two_selected(b: Any, served: dict[str, Any]) -> None:
    """F5: a slow /api/row answer for one row lands after a second is selected."""
    errors: list[str] = []
    page = page_of(b, served['url'], errors)
    page.click('[data-tab=row]')
    delay(page, '/api/row', 1500)
    page.click(check('CTAB micelles'))
    page.wait_for_timeout(200)
    page.click(check('Apoferritin'))
    page.wait_for_timeout(300)
    before = page.inner_text('#tab-row')[:60]
    page.wait_for_timeout(2000)
    after = page.inner_text('#tab-row')[:60]
    show('F5 selected:', page.inner_text('#selection'))
    show('F5 row tab while waiting:', repr(before))
    show('F5 row tab after the slow answer:', repr(after))
    show('F5 errors:', errors)


def runs_out_of_order(b: Any, served: dict[str, Any]) -> None:
    """F6: an older /api/runs answer that arrives last is shown."""
    errors: list[str] = []
    page = page_of(b, served['url'], errors)
    page.click('[data-view=runs]')
    page.wait_for_selector('#runs .tabulator-row')
    file = page.evaluate(
        "fetch('/api/runs').then(r => r.json()).then(b => b.rows[1].key)"
    )
    # Correct a run twice; the answer of the first /api/runs after it is slow.
    delay(page, '/api/runs', 2500)
    page.evaluate(CORRECT, [file, 'first'])
    page.wait_for_timeout(1300)  # a poll sees it and asks /api/runs (slow)
    page.evaluate(CORRECT, [file, 'second'])
    page.wait_for_timeout(1300)  # a poll sees it and asks /api/runs (fast)
    text = page.inner_text('#runs')
    middle = text.count('second'), text.count('first')
    page.wait_for_timeout(2500)
    text = page.inner_text('#runs')
    end = text.count('second'), text.count('first')
    show('F6 (second, first) shown before the slow answer:', middle, 'after:', end)
    show('F6 errors:', errors)


def stale_options(b: Any, served: dict[str, Any]) -> None:
    """F3: the options of a column of runs are those of the first state."""
    errors: list[str] = []
    page = page_of(b, served['url'], errors)
    runs = [r for group in demo.measurements() for r in group]
    runs.append((70000, {'transmission': 0.5}, 'transmission', 'H2O buffer'))
    demo.measure(served['directory'], runs)
    page.click('#rescan')
    page.wait_for_timeout(1000)
    state = page.evaluate("fetch('/api/state').then(r => r.json())")
    column = next(c for c in state['columns'] if c['name'] == 'transmission')
    offered = [o for o in column['editor']['options'] if '70000' in o]
    show('F3 the server offers the new run:', offered)
    row = page.locator('#rows .tabulator-row', has_text='D2O buffer')
    row.locator('[tabulator-field="cells.transmission.value"]').click()
    page.wait_for_selector('.choices')
    shown = page.evaluate(
        "[...document.querySelectorAll('.choices li')].map(l => l.textContent)"
    )
    show(
        'F3 editor options:',
        len(shown),
        'holds the new run:',
        any('70000' in o for o in shown),
    )
    page.keyboard.press('Escape')
    show('F3 errors:', errors)


def filter_stale(b: Any, served: dict[str, Any]) -> None:
    """F4: with the chip 'ready' pressed, a row that becomes done stays shown."""
    errors: list[str] = []
    page = page_of(b, served['url'], errors)
    page.click('.chip.st-ready')
    before = page.locator('#rows .tabulator-row').count()
    reduce_one(page, served, 'CTAB micelles')
    shown = page.evaluate(PILLS)
    show(
        'F4 rows shown under "ready" before:',
        before,
        'after:',
        len(shown),
        'statuses shown:',
        sorted(set(shown)),
    )
    show('F4 errors:', errors)


def sort_stale(b: Any, served: dict[str, Any]) -> None:
    """F4: sorted by status, a row whose status changes keeps its place."""
    errors: list[str] = []
    page = page_of(b, served['url'], errors)
    page.click('.tabulator-col[tabulator-field=status] .tabulator-col-title')
    page.wait_for_timeout(300)
    reduce_one(page, served, 'CTAB micelles')
    show('F4 order of statuses shown, sorted by status:', page.evaluate(PILLS)[:8])
    show('F4 errors:', errors)


def failed_fetch(b: Any, served: dict[str, Any]) -> None:
    """F9: a fetch outside an action fails; what does the person see?"""
    errors: list[str] = []
    page = page_of(b, served['url'], errors)
    page.route('**/api/plot*', lambda route: route.abort())
    page.click(check('CTAB micelles'))
    page.wait_for_timeout(800)
    show(
        'F9 message shown:',
        page.is_visible('#message'),
        'offline shown:',
        page.is_visible('#offline'),
    )
    show('F9 errors:', errors)


SCENARIOS = {
    'F3': stale_options,
    'F4-filter': filter_stale,
    'F4-sort': sort_stale,
    'F5': row_tab_two_selected,
    'F6': runs_out_of_order,
    'F9': failed_fetch,
}

if __name__ == '__main__':
    with browser() as b:
        for name in sys.argv[1:] or SCENARIOS:
            with serve_demo() as served:
                SCENARIOS[name](b, served)
