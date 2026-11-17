# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""F10: where the keyboard focus goes when a new state arrives."""

from harness import browser, new_page, post, serve_demo, show

ACTIVE = """(() => {
  const a = document.activeElement;
  if (!a) return null;
  const text = a.tagName === 'BODY' ? '' : (a.textContent || '').slice(0, 20);
  return `${a.tagName}.${a.className} ${text}`;
})()"""
H2O = '#rows .tabulator-row:has-text("H2O buffer")'

with browser() as b, serve_demo() as served:
    api = served['url'] + 'api/'
    page = new_page(b, served['url'], errors := [])
    page.focus('.chip.st-ready')
    show('focused:', page.evaluate(ACTIVE))
    post(api + 'scan', {})
    page.wait_for_timeout(1500)
    show('after a new state:', page.evaluate(ACTIVE))
    # The revert button of an edited cell.
    post(api + 'cells', {'keys': ['60302'], 'column': 'thickness', 'value': 2.0})
    page.wait_for_timeout(1500)
    page.locator(f'{H2O} .revert').focus()
    show('focus asked for the revert button, focused:', page.evaluate(ACTIVE))
    post(api + 'cells', {'keys': ['60302'], 'column': 'transmission', 'value': None})
    page.wait_for_timeout(1500)
    show('after the row changed:', page.evaluate(ACTIVE))
    show('errors:', errors)
