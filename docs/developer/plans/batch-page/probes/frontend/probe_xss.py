# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""F1: HTML in a row name runs as HTML in the tooltip of the Name cell."""

from harness import browser, new_page, serve_demo, show

NAME = '<img src=x onerror="window.__xss=(window.__xss||0)+1">'
TOOLTIP = 'document.querySelector(".tabulator-tooltip")?.innerHTML'

with serve_demo() as served, browser() as b:
    errors: list[str] = []
    page = new_page(b, served['url'], errors)
    page.click('#add-row')
    page.fill('#add-form input[name=key]', NAME)
    page.press('#add-form input[name=key]', 'Enter')
    page.wait_for_function(
        'document.querySelectorAll("#rows .tabulator-row").length > 24'
    )
    page.wait_for_timeout(500)
    cell = page.locator('#rows .tabulator-cell[tabulator-field=name]', has_text='<img')
    show('name cell text:', cell.inner_text())
    cell.hover()
    page.wait_for_timeout(1500)
    show('tooltip html:', page.evaluate(TOOLTIP))
    show(
        'window.__xss after hover (1 means the script ran):',
        page.evaluate('window.__xss'),
    )
    show('errors:', errors)
