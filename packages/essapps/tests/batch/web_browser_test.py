# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The batch page in Chromium: what requests alone cannot show, such as editing
by a click and a selection that stays put while the page polls.

The tests need Playwright, a Chromium, and the page's libraries (see
``static/vendor.sh``). They use the Chromium that ``playwright install chromium``
downloads, or else ``chromium`` on the ``PATH``. Without one of the three they
skip, except where the environment variable ``CI`` is set (GitHub Actions sets
it): there they fail, so that a broken install step cannot turn them off.
"""

import os
import shutil
import socket
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, NoReturn

import pytest
import uvicorn

from ess.apps import Client
from ess.apps.batch import Batch
from ess.apps.batch.web import Shown, create_app
from ess.apps.batch.web.api import STATIC

from .toys import Gate, Measure


def unavailable(reason: str) -> NoReturn:
    if os.environ.get('CI'):
        pytest.fail(reason, pytrace=False)
    pytest.skip(reason, allow_module_level=True)


try:
    from playwright import sync_api
except ImportError:
    unavailable('needs Playwright')
if not (STATIC / 'vendor').is_dir():
    unavailable(f'needs the libraries of the page: sh {STATIC / "vendor.sh"}')


def draw(shown: Sequence[Shown]) -> dict[str, Any]:
    return {'data': [{'y': [1, 2], 'name': s.name} for s in shown], 'layout': {}}


@pytest.fixture(scope='module')
def browser() -> Iterator[Any]:
    with sync_api.sync_playwright() as playwright:
        bundled = playwright.chromium.executable_path
        chromium = bundled if Path(bundled).exists() else shutil.which('chromium')
        if chromium is None:
            unavailable('needs Chromium: playwright install chromium')
        browser = playwright.chromium.launch(executable_path=chromium)
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
    """
    The page, loaded; a test fails on any error in the browser's console that
    it does not take from ``page.errors``.
    """
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    errors: list[str] = []
    page.errors = errors
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


def test_typing_in_a_run_cell_replaces_what_it_shows(page: Any, batch: Batch) -> None:
    cell(page, 'water', 'transmission').click()
    page.keyboard.type('1002')
    page.keyboard.press('Enter')

    page.wait_for_selector('#rows .tabulator-cell.edited')
    assert (
        batch.rows['1'].values['transmission']
        == (batch.rows['2'].values['transmission'])
    )


def test_an_empty_run_cell_left_writes_nothing_and_tab_goes_on(
    page: Any, batch: Batch
) -> None:
    page.click('#add-row')
    page.keyboard.type('blank')
    page.keyboard.press('Enter')
    page.wait_for_selector('#rows .tabulator-row:has-text("blank")')

    cell(page, 'blank', 'transmission').click()
    page.click('#title')
    cell(page, 'blank', 'transmission').click()
    editor(page).press('Tab')

    focused = 'document.activeElement.closest(".tabulator-cell")'
    assert page.evaluate(f'{focused}.getAttribute("tabulator-field")') == (
        'cells.thickness.value'
    )
    editor(page).press('Escape')
    assert 'transmission' not in batch.rows['blank'].values
    assert batch.rows.edits.get('blank', {}) == {}


def test_a_rescan_asks_before_it_replaces_a_table_loaded(
    page: Any, tmp_path: Path
) -> None:
    asked: list[str] = []
    page.on('dialog', lambda d: (asked.append(d.message), d.accept()))
    table = tmp_path / 'rows.csv'
    table.write_bytes(page.request.get(f'{page.url}api/table.csv').body())
    page.set_input_files('#pick-table', table)
    page.wait_for_selector('#toast:has-text("Loaded 3")')

    page.click('#rescan')

    page.wait_for_selector('#toast:has-text("Found 3")')
    assert asked == [
        'The table you loaded, rows.csv, is replaced by what the runs give. '
        'Values set by hand stay.'
    ]


def test_the_zoom_stays_when_the_selection_changes(page: Any, client: Client) -> None:
    page.click('#reduce')
    client.wait(client.records())
    page.wait_for_selector('#plot .main-svg')
    page.evaluate("Plotly.relayout('plot', {'xaxis.range': [0.2, 0.8]})")

    page.locator('#rows .tabulator-row', has_text='sds').locator('input').click()

    page.wait_for_function("document.getElementById('plot').data.length === 1")
    assert page.evaluate("document.getElementById('plot').layout.xaxis.range") == [
        0.2,
        0.8,
    ]


def test_an_edit_the_server_does_not_get_is_taken_back_and_said(
    page: Any, batch: Batch
) -> None:
    page.route('**/api/cells', lambda route: route.abort())
    cell(page, 'sds', 'thickness').click()
    editor(page).fill('2.5')
    editor(page).press('Enter')

    page.wait_for_selector('#message:has-text("The server does not answer")')
    assert cell(page, 'sds', 'thickness').inner_text() == '1.0'
    assert batch.rows.edits == {}
    assert page.errors == ['Failed to load resource: net::ERR_FAILED']
    page.errors.clear()
