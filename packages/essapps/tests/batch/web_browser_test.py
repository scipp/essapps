# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The batch page in Chromium: what requests alone cannot show, such as editing
by a click and a selection that stays put while the page polls. Skipped without
Playwright, Chromium at ``/usr/bin/chromium``, or the page's libraries (see
``static/vendor.sh``).
"""

import socket
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from ess.apps import Client
from ess.apps.batch import Batch
from ess.apps.batch.web import Shown, create_app
from ess.apps.batch.web.api import STATIC

from .toys import Gate, Measure

sync_api = pytest.importorskip('playwright.sync_api')
uvicorn = pytest.importorskip('uvicorn')
CHROMIUM = Path('/usr/bin/chromium')
if not CHROMIUM.exists() or not (STATIC / 'vendor').is_dir():
    pytest.skip('needs Chromium and the libraries of the page', allow_module_level=True)


def draw(shown: Sequence[Shown]) -> dict[str, Any]:
    return {'data': [{'y': [1, 2], 'name': s.name} for s in shown], 'layout': {}}


@pytest.fixture(scope='module')
def browser() -> Iterator[Any]:
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(CHROMIUM))
        yield browser
        browser.close()


@pytest.fixture
def url(batch: Batch, sample: Measure) -> Iterator[str]:
    for run, name in enumerate(['water', 'sds', 'oil'], start=1):
        sample(run, name)
    batch.scan()
    app = create_app(batch, name='name', draw=draw)
    with socket.socket() as s:
        s.bind(('localhost', 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host='localhost', port=port, log_level='warning')
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    yield f'http://localhost:{port}/'
    server.should_exit = True
    thread.join()


@pytest.fixture
def page(browser: Any, url: str) -> Iterator[Any]:
    """The page, loaded; a test fails on any error in the browser's console."""
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    errors: list[str] = []
    page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(url)
    page.wait_for_selector('#rows .tabulator-row')
    yield page
    page.close()
    assert errors == []


def cell(page: Any, name: str, column: str) -> Any:
    row = page.locator('#rows .tabulator-row', has_text=name)
    return row.locator(f'.tabulator-cell[tabulator-field="cells.{column}.value"]')


def editor(page: Any) -> Any:
    return page.locator('#rows .tabulator-cell.tabulator-editing input')


def test_a_click_on_a_cell_edits_it(page: Any, batch: Batch) -> None:
    cell(page, 'sds', 'thickness').click()
    editor(page).fill('2.5')
    editor(page).press('Enter')

    page.wait_for_selector('#rows .tabulator-cell.edited')
    assert batch.rows['2'].values['thickness'] == 2.5


def test_an_editor_left_unchanged_writes_nothing_and_the_table_updates_after(
    page: Any, batch: Batch
) -> None:
    cell(page, 'water', 'transmission').click()
    page.click('#title')
    cell(page, 'water', 'thickness').click()
    editor(page).press('Escape')

    batch.rows = batch.rows.edit('3', {'thickness': 7.0})
    page.click('#rescan')

    page.wait_for_selector('#rows .tabulator-cell:has-text("7.0")')
    assert batch.rows.edits == {'3': {'thickness': 7.0}}


def test_an_edit_in_a_row_checked_with_others_writes_each(
    page: Any, batch: Batch
) -> None:
    for name in ['water', 'oil']:
        row = page.locator('#rows .tabulator-row', has_text=name)
        row.locator('.tabulator-row-header input').click()

    cell(page, 'oil', 'thickness').click()
    editor(page).fill('4')
    editor(page).press('Enter')

    page.wait_for_selector('#toast:has-text("set on 2 rows")')
    assert {k: dict(v) for k, v in batch.rows.edits.items()} == {
        '1': {'thickness': 4.0},
        '3': {'thickness': 4.0},
    }


def test_the_selection_and_the_plot_stay_put_while_rows_run(
    page: Any, client: Client, gate: Gate
) -> None:
    plots: list[str] = []
    page.on('request', lambda r: plots.append(r.url) if '/api/plot' in r.url else None)
    page.locator('#rows .tabulator-row', has_text='sds').locator('input').click()
    page.wait_for_timeout(300)
    gate.close()
    page.click('#reduce')
    page.wait_for_selector('#reduce.running')
    asked = len(plots)

    page.wait_for_timeout(2500)

    assert len(plots) == asked
    assert page.inner_text('#selection') == '1 selected'
    gate.open()
    client.wait(client.records())
    page.wait_for_selector('.chip.st-done')


def test_a_row_is_added_by_typing_its_name(page: Any, batch: Batch) -> None:
    page.click('#add-row')
    page.keyboard.type('blank')
    page.keyboard.press('Enter')

    page.wait_for_selector('#rows .tabulator-row:has-text("blank")')
    assert 'blank' in batch.rows
