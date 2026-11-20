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
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NoReturn

import pandas as pd
import pytest
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

from ess.apps import Client
from ess.apps.batch import Batch, BatchManager, CorrectedFiles, Header
from ess.apps.batch.definition import rows_key
from ess.apps.batch.web import Pages, Shown, create_app
from ess.apps.batch.web.api import STATIC

from . import toys
from .toys import SOURCES, STAGED, Gate, Measure, Refusing


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


class Marks(BaseModel):
    role: str | None = None


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
def batch(manager: BatchManager, label: str) -> Batch:
    """The batch of ``label``, as the manager holds it."""
    return manager.batch(label)


@pytest.fixture
def pages(manager: BatchManager, files: CorrectedFiles) -> Pages:
    """The pages, with a step of Follow every 0.2 s while a batch follows."""
    return Pages(
        manager,
        interval=0.2,
        draw={STAGED.name: draw},
        files=files,
        marks=Marks,
    )


@pytest.fixture
def url(
    pages: Pages,
    label: str,
    batch: Batch,
    sample: Measure,
    gate: Gate,
    client: Client,
) -> Iterator[str]:
    """
    The URL of the page of ``label``. The gate opens at the end, so that a
    test that fails while it holds the gate closed does not stall the end of
    the server.
    """
    for run, name in enumerate(['water', 'sds', 'oil'], start=1):
        sample(run, name)
    batch.put(toys.rows(client))
    with serve(create_app(pages)) as root:
        try:
            yield f'{root}batches/{label}/'
        finally:
            gate.open()


START = 30
"""The seconds that a server may take to start."""


@contextmanager
def serve(app: FastAPI) -> Iterator[str]:
    """The URL of ``app``, served in a thread until the test ends."""
    with socket.socket() as s:
        s.bind(('localhost', 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host='localhost', port=port, log_level='warning')
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + START
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            server.should_exit = True
            pytest.fail(f'the server did not start within {START} s', pytrace=False)
        time.sleep(0.01)
    yield f'http://localhost:{port}/'
    server.should_exit = True
    thread.join()


def watched(browser: Any) -> Any:
    """
    A new page that collects each error in the browser's console, and each
    exception, in ``page.errors``.
    """
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    errors: list[str] = []
    page.errors = errors
    page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)
    page.on('pageerror', lambda e: errors.append(str(e)))
    return page


@pytest.fixture
def page(browser: Any, url: str) -> Iterator[Any]:
    """
    The page, loaded; a test fails on any error in the browser's console that
    it does not take from ``page.errors``.
    """
    page = watched(browser)
    page.goto(url)
    page.wait_for_selector('#rows .tabulator-row')
    yield page
    page.close()
    assert page.errors == []


def cell(page: Any, name: str, column: str) -> Any:
    row = page.locator('#rows .tabulator-row', has_text=name)
    return row.locator(f'.tabulator-cell[tabulator-field="cells.{column}.value"]')


TOP_ROW = """h => {
  const box = h.getBoundingClientRect();
  const at = document.elementFromPoint(box.left + 80, box.top + 1);
  const row = at.closest('.tabulator-row');
  const top = Math.round(row.getBoundingClientRect().top - box.top);
  return `${row.innerText} at ${top}`;
}"""
"""The row at the top of a table, and where its top is, given the table's holder."""


def editor(page: Any) -> Any:
    return page.locator('#rows .tabulator-cell.tabulator-editing input')


def test_a_click_on_a_cell_edits_it(page: Any, batch: Batch) -> None:
    cell(page, 'sds', 'thickness').click()
    editor(page).fill('2.5')
    editor(page).press('Enter')

    cell(page, 'sds', 'thickness').filter(has_text='2.5').wait_for()
    assert batch.frame().at['2', 'thickness'] == 2.5


def test_an_editor_left_unchanged_writes_nothing_and_the_table_updates_after(
    page: Any, batch: Batch
) -> None:
    before = batch.frame()
    cell(page, 'water', 'transmission').click()
    page.click('#title')
    cell(page, 'water', 'thickness').click()
    editor(page).press('Escape')
    unchanged = batch.frame()

    toys.edit(batch, '3', thickness=7.0)

    page.wait_for_selector('#rows .tabulator-cell:has-text("7.0")')
    pd.testing.assert_frame_equal(unchanged, before)


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
    thickness = batch.frame()['thickness']
    assert (list(thickness[['1', '3']]), pd.isna(thickness['2'])) == ([4.0, 4.0], True)


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
    assert 'blank' in batch.frame().index


def test_typing_in_a_run_cell_replaces_what_it_shows(page: Any, batch: Batch) -> None:
    cell(page, 'water', 'transmission').click()
    page.keyboard.type('1002')
    page.keyboard.press('Enter')

    cell(page, 'water', 'transmission').filter(has_text='run 1002').wait_for()
    table = batch.frame()
    assert table.at['1', 'transmission'] == table.at['2', 'transmission']


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
    assert batch.frame().loc['blank'].isna().all()


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
    assert pd.isna(batch.frame().at['2', 'thickness'])
    assert page.errors == ['Failed to load resource: net::ERR_FAILED']
    page.errors.clear()


HOLD = """part => {
  window.held = [];
  const fetched = window.fetch;
  window.fetch = async (url, init) => {
    const response = await fetched(url, init);
    if (!String(url).includes(part)) return response;
    const copy = new Response(await response.blob(), response);
    return new Promise((release) => window.held.push(() => release(copy)));
  };
}"""
RELEASE = """async i => {
  window.held[i]();
  await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
}"""


def hold(page: Any, part: str) -> None:
    """Keep the answers of the requests whose URL holds ``part`` until released."""
    page.evaluate(HOLD, part)


def held(page: Any, n: int) -> None:
    """Wait until ``n`` answers are held."""
    page.wait_for_function(f'window.held.length === {n}')


def release(page: Any, i: int) -> None:
    """Give the page the ``i``-th held answer, and wait until it has handled it."""
    page.evaluate(RELEASE, i)


def select(page: Any, name: str) -> None:
    page.locator('#rows .tabulator-row', has_text=name).locator('input').click()


def test_the_row_tab_does_not_show_a_row_after_a_second_is_selected(page: Any) -> None:
    page.click('[data-tab=row]')
    hold(page, 'api/row')
    select(page, 'water')
    held(page, 1)
    select(page, 'sds')

    release(page, 0)

    assert 'Select only one' in page.inner_text('#tab-row')


def test_an_older_answer_for_the_runs_does_not_replace_a_newer_one(
    page: Any, url: str
) -> None:
    page.click('[data-view=runs]')
    key = page.evaluate(
        "fetch('api/runs').then(r => r.json()).then(b => b.rows[0].key)"
    )
    hold(page, 'api/runs')
    for n, value in enumerate(['first', 'second']):
        edit = {'keys': [key], 'column': 'role', 'value': value}
        assert page.request.post(f'{url}api/runs/cells', data=edit).ok
        held(page, n + 1)

    release(page, 1)
    release(page, 0)

    assert 'second' in page.inner_text('#runs')
    assert 'first' not in page.inner_text('#runs')


def test_a_request_the_server_does_not_answer_says_so_where_no_action_asked(
    page: Any,
) -> None:
    page.route('**/api/plot*', lambda route: route.abort())

    select(page, 'water')

    page.wait_for_selector('#message:has-text("The server does not answer")')
    assert page.errors == ['Failed to load resource: net::ERR_FAILED']
    page.errors.clear()


def test_a_request_the_server_fails_says_its_status_where_no_action_asked(
    page: Any,
) -> None:
    page.route('**/api/plot*', lambda route: route.fulfill(status=500))

    select(page, 'water')

    page.wait_for_selector('#message:has-text("The server answered 500.")')
    assert len(page.errors) == 1
    assert 'status of 500' in page.errors[0]
    page.errors.clear()


def test_a_poll_the_server_refuses_says_the_message_of_the_server(
    page: Any,
) -> None:
    # A 404, a batch that is gone, shows the list instead (see the tab "Batch").
    message = {'message': 'Cannot show scan: the table is half written.'}
    page.route('**/api/state*', lambda route: route.fulfill(status=422, json=message))

    page.wait_for_selector('#offline:has-text("the table is half written.")')
    assert all('status of 422' in e for e in page.errors)
    page.errors.clear()


def test_the_page_loads_its_rows_under_a_path_prefix(
    browser: Any, pages: Pages, batch: Batch, sample: Measure, client: Client
) -> None:
    sample(1, 'water')
    batch.put(toys.rows(client))
    parent = FastAPI()
    parent.mount('/batch', create_app(pages))
    with serve(parent) as url:
        page = browser.new_page()
        errors: list[str] = []
        page.on(
            'console', lambda m: errors.append(m.text) if m.type == 'error' else None
        )
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(f'{url}batch/')

        page.wait_for_selector('#rows .tabulator-row:has-text("water")')
        page.close()
    assert errors == []


def test_a_plot_of_the_keys_of_ten_thousand_rows_is_answered(
    page: Any, url: str
) -> None:
    keys = [f'run-{n}' for n in range(10_000)]

    answer = page.request.post(f'{url}api/plot', data={'keys': keys})

    assert answer.ok


BINS = '#settings input[data-path=\'["binning","bins"]\']'
"""The input of the number of bins in the settings form."""


def test_a_setting_is_in_force_after_apply_and_discard_takes_a_change_back(
    page: Any, batch: Batch
) -> None:
    page.click('[data-tab=settings]')
    page.fill(BINS, '5')
    page.press(BINS, 'Tab')
    page.wait_for_selector('#draft.changed')
    said = page.inner_text('#draft-state')
    before = dict(batch.settings)

    page.click('#settings-apply')
    page.wait_for_selector('#draft:not(.changed)')
    applied = dict(batch.settings)
    page.fill(BINS, '7')
    page.press(BINS, 'Tab')
    page.wait_for_selector('#draft.changed')
    page.click('#settings-discard')
    page.wait_for_selector('#draft:not(.changed)')

    assert said == 'Changed, not applied yet.'
    assert before == {}
    assert applied['binning'] == {'bins': 5}
    assert page.input_value(BINS) == '5'
    assert batch.settings['binning'] == {'bins': 5}


def test_typing_in_the_settings_form_while_a_poll_arrives_sends_what_was_typed(
    page: Any, url: str
) -> None:
    sent: list[str] = []
    page.on(
        'request',
        lambda r: sent.append(r.post_data) if r.url.endswith('/api/settings') else None,
    )
    page.click('[data-tab=settings]')
    page.click(BINS)
    page.press(BINS, 'Control+a')
    page.keyboard.type('1')

    # Another page changes the form, which the next poll of this one brings.
    choose = {'path': ['offset'], 'op': 'choose', 'value': 'Given'}
    assert page.request.post(f'{url}api/settings', data=choose).ok
    page.wait_for_selector('#draft.changed')
    page.keyboard.type('2')
    with page.expect_response('**/api/settings'):
        page.keyboard.press('Tab')

    assert sent == ['{"path":["binning","bins"],"op":"set","value":12}']
    page.wait_for_selector('#settings input[data-path=\'["offset","value"]\']')
    assert page.input_value(BINS) == '12'


def test_a_value_typed_and_apply_clicked_at_once_are_applied_with_one_click(
    page: Any, batch: Batch
) -> None:
    page.click('[data-tab=settings]')
    page.click(BINS)
    page.keyboard.press('Control+a')
    page.keyboard.type('5')

    page.click('#settings-apply')

    page.wait_for_selector('#toast:has-text("Applied the settings.")')
    page.wait_for_selector('#draft:not(.changed)')
    assert batch.settings['binning'] == {'bins': 5}


def test_escape_after_a_value_sent_puts_that_value_back_and_sends_nothing(
    page: Any,
) -> None:
    sent: list[str] = []
    page.on(
        'request',
        lambda r: sent.append(r.post_data) if r.url.endswith('/api/settings') else None,
    )
    page.click('[data-tab=settings]')
    page.click(BINS)
    page.keyboard.press('Control+a')
    page.keyboard.type('7')
    with page.expect_response('**/api/settings'):
        page.keyboard.press('Enter')

    page.keyboard.press('Control+a')
    page.keyboard.type('6')
    page.keyboard.press('Escape')
    # Actions are sent in order, so an edit at the blur would come before this.
    with page.expect_response('**/api/relist'):
        page.click('#relist')

    assert sent == ['{"path":["binning","bins"],"op":"set","value":7}']
    assert page.input_value(BINS) == '7'


def test_a_form_that_arrives_while_the_focus_rests_in_an_input_is_drawn(
    page: Any, url: str
) -> None:
    page.click('[data-tab=settings]')
    page.click(BINS)

    edit = {'path': ['binning', 'bins'], 'op': 'set', 'value': 4}
    assert page.request.post(f'{url}api/settings', data=edit).ok

    page.wait_for_function(
        f'document.querySelector({BINS!r}).value === "4"', polling=100
    )
    assert page.evaluate('document.activeElement.dataset.path') == (
        '["binning","bins"]'
    )


@pytest.fixture
def holder(page: Any, batch: Batch, sample: Measure, client: Client) -> Any:
    """The holder of the rows of the page, with more rows than it shows."""
    for run in range(4, 60):
        sample(run, f'r{run}')
    batch.put(toys.rows(client))
    page.wait_for_selector('#n-rows:text-is("59")')
    return page.locator('#rows .tabulator-tableholder')


def test_the_scroll_position_stays_when_a_row_arrives(
    holder: Any, page: Any, batch: Batch, sample: Measure, client: Client
) -> None:
    holder.evaluate('h => { h.scrollTop = h.scrollHeight / 2; }')
    top = holder.evaluate(TOP_ROW)
    height = holder.evaluate('h => h.scrollHeight')

    sample(60, 'r60')
    batch.put(toys.rows(client))

    page.wait_for_function(
        f'h => h.scrollHeight > {height}', arg=holder.element_handle()
    )
    assert top.startswith('r')
    assert holder.evaluate(TOP_ROW) == top


def test_a_click_on_a_row_mostly_shown_at_the_bottom_does_not_scroll(
    holder: Any, page: Any
) -> None:
    name = holder.evaluate("""h => {
      const bottom = h.getBoundingClientRect().bottom;
      const row = [...h.querySelectorAll('.tabulator-row')]
        .find((r) => r.getBoundingClientRect().bottom > bottom);
      const hidden = row.getBoundingClientRect().bottom - bottom;
      h.scrollTop += hidden - 0.2 * row.offsetHeight;
      return row.querySelector('[tabulator-field=name]').textContent;
    }""")
    top = holder.evaluate(TOP_ROW)
    # A click by position: the click of a locator scrolls its cell into view.
    box = cell(page, name, 'thickness').bounding_box()
    page.mouse.click(box['x'] + 10, box['y'] + 5)

    editor(page).wait_for()
    assert holder.evaluate(TOP_ROW) == top


@pytest.fixture
def following(browser: Any, pages: Pages, label: str) -> Iterator[Any]:
    """The page of a batch with no rows yet, following its files."""
    pages.manager.follow(label)
    with serve(create_app(pages)) as url:
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        errors: list[str] = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(url)
        page.wait_for_selector('#follow input:checked')
        yield page
        page.close()
    assert errors == []


def test_with_follow_on_rows_appear_and_are_reduced_without_a_click(
    following: Any, sample: Measure
) -> None:
    for run, name in enumerate(['water', 'sds'], start=1):
        sample(run, name)

    following.wait_for_selector('#rows .tabulator-row:has-text("sds")')
    following.wait_for_selector('.chip.st-done .n:text-is("2")')
    assert following.inner_text('#follow-status') == 'Following, every 0.2 s'


def test_the_page_says_which_other_batch_follows(
    page: Any, pages: Pages, manager: BatchManager
) -> None:
    other = manager.create(STAGED.name, title='Other', source='pairs')

    pages.manager.follow(other)

    page.wait_for_selector('#follow-status:text-is("Other follows")')
    assert not page.is_checked('#follow input')


HTML = '<img src=x onerror="window.__xss = 1">'


def test_html_in_the_name_of_a_row_shows_as_text_in_its_tooltip(page: Any) -> None:
    page.click('#add-row')
    page.keyboard.type(HTML)
    page.keyboard.press('Enter')
    name = page.locator('#rows .tabulator-cell[tabulator-field=name]', has_text='<img')

    name.hover()

    page.wait_for_selector('.tabulator-tooltip')
    assert page.inner_text('.tabulator-tooltip') == HTML
    assert page.locator('.tabulator-tooltip img').count() == 0


def test_html_in_a_cell_shows_as_text_in_its_tooltip(page: Any) -> None:
    page.click('[data-view=runs]')
    role = page.locator('#runs .tabulator-cell[tabulator-field="cells.role.value"]')
    role.first.click()
    page.keyboard.type(HTML)
    page.keyboard.press('Enter')
    edited = page.locator('#runs .tabulator-cell.edited').first

    edited.hover()

    page.wait_for_selector('.tabulator-tooltip')
    assert HTML in page.inner_text('.tabulator-tooltip')
    assert page.locator('.tabulator-tooltip img').count() == 0


def test_a_run_that_arrives_can_be_chosen_in_a_cell(page: Any, sample: Measure) -> None:
    sample(4, 'ice')
    page.click('#relist')
    page.wait_for_selector('#toast:has-text("Listed the runs again.")')

    cell(page, 'water', 'transmission').click()

    page.wait_for_selector('.choices li:has-text("run 1004")')


def pills(page: Any) -> list[str]:
    return page.evaluate(PILLS)


PILLS = """[...document.querySelectorAll('#rows .tabulator-row')].map(
  (r) => r.querySelector('.pill').textContent
)"""
"""The status of each row shown, from the top."""


def reduce_rows(page: Any, url: str, client: Client, *keys: str) -> None:
    """Reduce the rows of ``keys`` and wait until the page shows them done."""
    answer = page.request.post(
        f'{url}api/reduce', data={'which': 'selected', 'keys': keys}
    )
    assert answer.ok
    client.wait(client.records())
    page.wait_for_selector(f'.chip.st-done .n:text-is("{len(keys)}")')


def test_a_row_that_changes_is_filtered_out_again(
    page: Any, url: str, client: Client
) -> None:
    page.click('.chip.st-ready')
    assert pills(page) == ['ready'] * 3

    reduce_rows(page, url, client, '2')

    page.wait_for_function(f'{PILLS}.length === 2')
    assert pills(page) == ['ready'] * 2


def test_a_row_that_changes_moves_to_where_the_sort_puts_it(
    page: Any, url: str, client: Client
) -> None:
    page.click('#rows .tabulator-col[tabulator-field=status]')
    assert pills(page) == ['ready'] * 3

    reduce_rows(page, url, client, '1')

    page.wait_for_function(f'{PILLS}[2] === "done"')
    assert pills(page) == ['ready', 'ready', 'done']


def test_the_runs_are_shown_when_the_view_is_first_opened(page: Any) -> None:
    page.click('[data-view=runs]')

    page.wait_for_selector('#runs .tabulator-row')
    assert page.locator('#runs .tabulator-row').count() == 6


def test_a_run_corrected_while_the_runs_are_hidden_shows_when_they_are_opened(
    page: Any, url: str
) -> None:
    key = page.evaluate(
        "fetch('api/runs').then(r => r.json()).then(b => b.rows[0].key)"
    )
    edit = {'keys': [key], 'column': 'role', 'value': 'corrected'}
    with page.expect_response('**/api/runs'):
        assert page.request.post(f'{url}api/runs/cells', data=edit).ok

    page.click('[data-view=runs]')

    page.wait_for_selector('#runs .tabulator-cell:has-text("corrected")')


def correct_first_sample(page: Any, role: str) -> None:
    """Correct the role of the first sample run in the Runs view."""
    page.click('[data-view=runs]')
    cells = '#runs .tabulator-cell[tabulator-field="cells.role.value"]'
    page.locator(f'{cells}:has-text("sample")').first.click()
    page.keyboard.type(role)
    page.keyboard.press('Enter')


def test_a_run_corrected_on_its_page_changes_no_row(page: Any) -> None:
    correct_first_sample(page, 'background')

    page.wait_for_selector('#toast:has-text("Role corrected on 1 run.")')
    page.click('[data-view=rows]')
    assert page.locator('#rows .tabulator-row').count() == 3


def active(page: Any) -> str:
    """The tag and the class of the element that has the keyboard focus."""
    return page.evaluate(
        '`${document.activeElement.tagName}.${document.activeElement.className}`'
    )


def test_a_chip_of_the_filter_keeps_the_focus_when_the_state_changes(
    page: Any, url: str, client: Client
) -> None:
    page.focus('.chip.st-ready')

    reduce_rows(page, url, client, '2')

    assert active(page) == 'BUTTON.chip st-ready'


def test_a_button_of_the_row_tab_keeps_the_focus_when_the_row_changes(
    page: Any, url: str, client: Client
) -> None:
    reduce_rows(page, url, client, '1')
    change = {'path': ['binning', 'bins'], 'op': 'set', 'value': 5}
    assert page.request.post(f'{url}api/settings', data=change).ok
    assert page.request.post(f'{url}api/settings/apply').ok
    page.click('[data-tab=row]')
    select(page, 'water')
    button = page.locator('#tab-row button', has_text='Put these settings')
    button.focus()
    assert active(page) == 'BUTTON.'

    edit = {'keys': ['1'], 'column': 'thickness', 'value': 9.0}
    assert page.request.post(f'{url}api/cells', data=edit).ok

    page.wait_for_function(
        "document.querySelector('#tab-row dl').innerText.includes('9.0')"
        " && document.activeElement.textContent.startsWith('Put these settings')"
    )


# The list of batches


def test_the_batches_view_counts_the_rows_every_two_seconds_while_shown(
    page: Any, batch: Batch, client: Client
) -> None:
    batch.put(batch.frame().drop(index=['3']))
    page.click('[data-view=batches]')
    page.wait_for_selector('#batches .pill.st-ready:text-is("2 ready")')

    client.wait(batch.reduce(['1']))

    page.wait_for_selector('#batches .pill.st-done:text-is("1 done")', timeout=5000)
    page.click('[data-view=rows]')
    assert asked(page, '/api/batches/counts', seconds=2.5) == 0


def test_a_count_that_fails_says_why_in_the_batches_view_and_shows_no_counts(
    page: Any,
) -> None:
    page.click('[data-view=batches]')
    page.wait_for_selector('#batches .pill')

    refused = {'message': 'Cannot count.'}
    page.route('**/api/batches/counts', lambda r: r.fulfill(status=422, json=refused))

    page.wait_for_selector('#message:has-text("Cannot count.")')
    assert page.is_visible('#message')
    assert page.locator('#batches .pill').count() == 0
    page.click('[data-view=rows]')  # no count after this one
    page.wait_for_timeout(300)
    assert all('status of 422' in e for e in page.errors)
    page.errors.clear()


def test_a_batch_that_could_not_be_counted_shows_why_in_its_row(page: Any) -> None:
    counted = {'rows': {}, 'files': {}, 'failed': {'scan': 'half written'}}
    page.route('**/api/batches/counts', lambda r: r.fulfill(json=counted))

    page.click('[data-view=batches]')

    page.wait_for_selector('#batches td.bad:text-is("half written")')


# A change of batch


def asked(page: Any, part: str, seconds: float) -> int:
    """The number of requests whose URL holds ``part`` in the next ``seconds``."""
    urls: list[str] = []

    def heard(request: Any) -> None:
        if part in request.url:
            urls.append(request.url)

    page.on('request', heard)
    page.wait_for_timeout(seconds * 1000)
    page.remove_listener('request', heard)
    return len(urls)


@pytest.fixture
def second(manager: BatchManager, url: str) -> str:
    """
    A second batch, ``other``, of the row ``2`` of the source, sds, with a
    thickness of 5; the batch ``scan`` has the same row, with no thickness.
    """
    return manager.create(
        STAGED.name,
        title='Other',
        description='The second series',
        source='pairs',
        choose=lambda given: given.loc[['2']].assign(thickness=5.0),
        label='other',
    )


KEEP = 'window.kept = true'
"""A mark that a load of the document forgets."""
KEPT = 'window.kept === true'


def to_batch(page: Any, title: str) -> None:
    """Show the batch of ``title`` by a click on its title in the Batches view."""
    page.click('[data-view=batches]')
    page.click(f'#batches a:text-is("{title}")')
    page.wait_for_selector(f'#title:has-text("{title}")')


def shows(page: Any, title: str) -> None:
    """Wait until the page has drawn the first state of the batch of ``title``."""
    page.wait_for_selector(f'#title:has-text("{title}")')
    page.wait_for_selector('body:not([inert])')


def test_a_click_on_a_title_shows_its_batch_in_place_in_the_rows_view(
    second: str, page: Any, pages: Pages
) -> None:
    pages.manager.follow(second)
    page.evaluate(KEEP)
    page.click('[data-tab=settings]')
    page.click('[data-view=batches]')
    listed = page.locator('#batches tbody tr', has_text='Other')
    listed.locator('.follows:text-is("follows")').wait_for()
    assert 'The second series' in listed.inner_text()
    assert page.inner_text('#batches tr[aria-current=page] a') == 'Scan'

    page.click('#batches a:text-is("Other")')

    shows(page, 'Other')
    assert page.url.endswith(f'/batches/{second}/')
    assert page.evaluate(KEPT)
    assert page.get_attribute('[data-view=rows]', 'aria-pressed') == 'true'
    assert page.get_attribute('[data-tab=settings]', 'aria-selected') == 'true'
    assert page.locator('#rows .tabulator-row').count() == 1
    assert cell(page, 'sds', 'thickness').inner_text() == '5.0'


def test_a_click_on_a_row_of_the_list_shows_its_batch_and_stays_in_the_list(
    second: str, page: Any
) -> None:
    page.evaluate(KEEP)
    page.click('[data-tab=batch]')
    page.click('[data-view=batches]')

    page.click('#batches tr:has-text("Other") td.n')

    page.wait_for_selector('#batch-fixed dd:text-is("other")')
    assert page.inner_text('#batches tr[aria-current=page] a') == 'Other'
    assert page.get_attribute('[data-view=batches]', 'aria-pressed') == 'true'
    assert page.url.endswith(f'/batches/{second}/')
    assert page.evaluate(KEPT)


def test_a_click_on_a_title_with_a_modifier_key_opens_the_batch_in_a_new_tab(
    second: str, page: Any
) -> None:
    page.click('[data-view=batches]')

    with page.context.expect_page() as opened:
        page.click('#batches a:text-is("Other")', modifiers=['Control'])

    opened.value.wait_for_selector('#title:has-text("Other")')
    assert opened.value.url.endswith(f'/batches/{second}/')
    assert page.url.endswith('/batches/scan/')
    assert 'Scan' in page.inner_text('#title')
    opened.value.close()


# The batches of each run

BATCHES = '.tabulator-cell[tabulator-field="batches"]'
"""The cells of the column Batches of the Runs view."""


def batches_of(page: Any, run: str) -> Any:
    """The cell Batches of the run labelled ``run``, such as ``run 2``."""
    label = page.locator(f'.tabulator-cell[tabulator-field="file"]:text-is("{run}")')
    return page.locator('#runs .tabulator-row', has=label).locator(BATCHES)


def test_the_runs_view_names_the_batches_of_each_run_and_sorts_those_in_none_first(
    second: str, page: Any, batch: Batch
) -> None:
    batch.put(batch.frame().drop(index=['3']))  # runs 3 and 1003 in no batch
    page.click('[data-view=runs]')

    page.wait_for_selector(f'#runs {BATCHES}:has-text("Other, Scan")')
    assert batches_of(page, 'run 1').inner_text() == 'Scan'
    assert batches_of(page, 'run 1002').inner_text() == 'Other, Scan'  # as listed
    page.wait_for_function(
        "[...document.querySelectorAll('#runs .tabulator-row')]"
        ".filter(r => r.innerText.includes('Scan')).length === 4"
    )

    page.click('#runs .tabulator-col[tabulator-field="batches"] .tabulator-col-title')

    first = page.locator('#runs .tabulator-row').nth(0)
    assert first.locator('[tabulator-field="file"]').inner_text() in (
        'run 3',
        'run 1003',
    )
    assert first.locator(BATCHES).inner_text() == ''


def test_a_click_on_a_title_in_the_runs_view_shows_its_batch(
    second: str, page: Any
) -> None:
    page.evaluate(KEEP)
    page.click('[data-view=runs]')

    page.click(f'#runs {BATCHES} a:text-is("Other")')

    shows(page, 'Other')
    assert page.url.endswith(f'/batches/{second}/')
    assert page.get_attribute('[data-view=rows]', 'aria-pressed') == 'true'
    assert page.evaluate(KEPT)


def test_a_correction_typed_in_the_runs_view_stays_while_the_batches_change(
    page: Any, batch: Batch
) -> None:
    page.click('[data-view=runs]')
    page.wait_for_selector(f'#runs {BATCHES}:has-text("Scan")')
    roles = '#runs .tabulator-cell[tabulator-field="cells.role.value"]'
    page.locator(f'{roles}:has-text("sample")').first.click()
    page.keyboard.type('backgr')

    batch.put(batch.frame().drop(index=['3']))
    for _ in range(2):
        with page.expect_response('**/api/batches/counts'):
            pass

    typing = page.locator('#runs .tabulator-cell.tabulator-editing input')
    assert typing.input_value() == 'backgr'
    assert typing.evaluate('e => e === document.activeElement')
    page.keyboard.press('Enter')
    page.wait_for_selector('#toast:has-text("Role corrected on 1 run.")')
    page.wait_for_function(
        "[...document.querySelectorAll('#runs .tabulator-row')]"
        ".filter(r => r.innerText.includes('Scan')).length === 4"
    )


def test_back_and_forward_change_the_batch_in_place(second: str, page: Any) -> None:
    page.evaluate(KEEP)
    to_batch(page, 'Other')

    page.go_back()
    shows(page, 'Scan')
    assert page.locator('#rows .tabulator-row').count() == 3
    page.go_forward()
    shows(page, 'Other')

    assert page.locator('#rows .tabulator-row').count() == 1
    assert page.evaluate(KEPT)


def test_each_batch_keeps_its_filter_selection_and_sort_and_the_page_its_tab(
    second: str, page: Any
) -> None:
    page.click('#rows .tabulator-col[tabulator-field=name]')
    select(page, 'water')
    page.click('.chip.st-ready')
    page.click('[data-tab=row]')

    to_batch(page, 'Other')
    page.wait_for_selector('.chip.st-all[aria-pressed=true]')
    assert page.inner_text('#selection') == ''
    assert page.get_attribute('[data-tab=row]', 'aria-selected') == 'true'
    page.go_back()

    shows(page, 'Scan')
    page.wait_for_selector('#tab-row h2:text-is("water")')
    assert page.inner_text('#selection') == '1 selected'
    assert page.get_attribute('.chip.st-ready', 'aria-pressed') == 'true'
    assert page.evaluate(PILLS) == ['ready'] * 3
    names = page.locator('#rows .tabulator-row [tabulator-field=name]')
    assert names.all_inner_texts() == ['oil', 'sds', 'water']


def test_nothing_of_the_batch_before_stays_after_a_change(
    second: str, page: Any, url: str, client: Client, manager: BatchManager
) -> None:
    sent: list[str] = []
    page.on('request', lambda r: sent.append(r.url) if r.method == 'POST' else None)
    to_batch(page, 'Other')
    reduce_rows(page, url.replace('/scan/', f'/{second}/'), client, '2')
    page.wait_for_selector('#plot .main-svg')
    select(page, 'sds')
    page.click('.chip.st-done')
    cell(page, 'sds', 'thickness').click()
    editor(page).fill('thick')
    editor(page).press('Enter')
    page.wait_for_selector('#message:has-text("thick is not a number")')
    page.click('[data-tab=settings]')
    page.click(BINS)
    page.keyboard.press('Control+a')
    page.keyboard.type('9')
    sent.clear()

    page.go_back()

    shows(page, 'Scan')
    page.wait_for_selector('#plot[hidden]', state='attached')  # in the tab Result
    assert page.evaluate(PILLS) == ['ready'] * 3
    assert page.inner_text('#selection') == ''
    assert page.is_hidden('#message')
    assert page.input_value(BINS) != '9'
    assert page.is_hidden('#draft.changed')
    page.click('[data-tab=row]')
    assert 'Select a row' in page.inner_text('#tab-row')
    # The value typed goes to the draft of the batch it was typed for.
    other = url.replace('/scan/', f'/{second}/')
    assert [u for u in sent if '/api/settings' in u] == [f'{other}api/settings']
    assert manager.batch(second).settings == {}


def test_a_change_closes_the_menus_dialogs_and_forms_of_the_batch_before(
    second: str, page: Any
) -> None:
    to_batch(page, 'Other')
    page.click('[data-tab=batch]')
    page.fill('#batch-title', 'Typed')
    page.click('#add-row')
    page.keyboard.type('typed')
    page.click('#files .menu-button')
    page.go_back()
    shows(page, 'Scan')
    assert page.input_value('#batch-title') == 'Scan'
    assert page.is_hidden('#add-form')
    assert page.is_hidden('#files .menu-items')
    page.click('#take-open')
    page.wait_for_selector('#take[open]')

    page.go_forward()

    shows(page, 'Other')
    assert page.is_hidden('#take')
    assert page.input_value('#batch-title') == 'Other'


def test_an_answer_for_the_batch_before_is_dropped(
    second: str, page: Any, batch: Batch, manager: BatchManager
) -> None:
    hold(page, 'api/cells')
    cell(page, 'water', 'thickness').click()
    editor(page).fill('2.5')
    editor(page).press('Enter')
    held(page, 1)

    to_batch(page, 'Other')
    release(page, 0)
    page.wait_for_timeout(300)

    assert page.locator('#rows .tabulator-row').count() == 1
    assert 'Other' in page.inner_text('#title')
    assert batch.frame().at['1', 'thickness'] == 2.5
    assert list(manager.batch(second).frame()['thickness']) == [5.0]


def test_a_value_typed_goes_to_its_batch_after_an_action_on_its_way(
    second: str, page: Any
) -> None:
    hold(page, 'api/relist')
    page.click('#relist')
    held(page, 1)
    page.click('[data-tab=settings]')
    page.click(BINS)
    page.keyboard.press('Control+a')
    page.keyboard.type('9')

    to_batch(page, 'Other')
    release(page, 0)
    page.wait_for_timeout(300)
    assert 'Other' in page.inner_text('#title')
    page.go_back()

    shows(page, 'Scan')
    page.wait_for_selector('#draft.changed')
    assert page.input_value(BINS) == '9'


def test_a_file_asked_for_before_a_change_is_saved(second: str, page: Any) -> None:
    hold(page, 'api/table.csv')
    page.click('#files .menu-button')
    page.click('[data-file="table.csv"]')
    held(page, 1)
    to_batch(page, 'Other')

    with page.expect_download() as saved:
        release(page, 0)

    assert saved.value.suggested_filename == 'rows.csv'
    assert 'water' in Path(saved.value.path()).read_text()


def test_the_focus_on_a_title_in_the_list_stays_while_it_counts(
    second: str, page: Any
) -> None:
    page.click('[data-view=batches]')
    page.wait_for_selector('#batches .pill')
    page.focus('#batches a:text-is("Other")')

    with page.expect_response('**/api/batches/counts'):
        pass
    page.wait_for_timeout(100)
    assert page.evaluate('document.activeElement.textContent') == 'Other'
    page.keyboard.press('Enter')

    shows(page, 'Other')


def test_selecting_the_text_of_a_batch_in_the_list_does_not_show_it(
    second: str, page: Any
) -> None:
    page.click('[data-view=batches]')

    page.dblclick(f'#batches small:text-is("{second}")')
    assert page.evaluate('getSelection().toString()') == second
    description = page.locator('#batches .hint:text-is("The second series")')
    description.click(position={'x': 2, 'y': 5})
    page.mouse.down()
    page.mouse.move(400, description.bounding_box()['y'] + 5)
    page.mouse.up()
    page.wait_for_timeout(300)

    assert page.url.endswith('/batches/scan/')
    assert page.evaluate('getSelection().toString()') != ''


def test_a_change_while_the_server_does_not_answer_says_so_and_shows_no_batch(
    second: str, page: Any
) -> None:
    page.route(f'**/{second}/api/state*', lambda route: route.abort())
    page.click('[data-view=batches]')
    page.click('#batches a:text-is("Other")')

    page.wait_for_selector('#offline:has-text("The server does not answer.")')
    page.wait_for_selector('body.no-batch:not([inert])')
    assert page.is_hidden('#view-rows')
    assert 'Scan' not in page.inner_text('#title')
    page.unroute(f'**/{second}/api/state*')
    shows(page, 'Other')
    page.wait_for_selector('#offline', state='hidden')
    assert page.is_visible('.right')
    assert all('ERR_FAILED' in e for e in page.errors)
    page.errors.clear()
    # The view chosen by the click on the title, also after a reload.
    assert page.is_visible('#view-rows')
    page.reload()
    page.wait_for_selector('#rows .tabulator-row')
    assert page.get_attribute('[data-view=rows]', 'aria-pressed') == 'true'


def test_a_refused_header_of_the_batch_before_is_the_message_of_the_page(
    second: str, page: Any
) -> None:
    hold(page, 'api/batch')  # also the list and its counts, which do not matter
    page.click('[data-tab=batch]')
    page.fill('#batch-title', ' ')
    page.click('#batch-form [type=submit]')
    held(page, 1)

    page.click('[data-view=batches]')
    page.click('#batches a:text-is("Other")')
    shows(page, 'Other')
    release(page, 0)

    page.wait_for_selector('#message:has-text("a title that is not empty")')
    assert page.inner_text('#message .message-text strong').startswith('Scan: ')
    assert page.inner_text('#batch-problem') == ''
    assert page.input_value('#batch-title') == 'Other'
    assert all('status of 422' in e for e in page.errors)
    page.errors.clear()


def test_a_refused_table_of_the_batch_before_is_the_message_of_the_page(
    second: str, page: Any, tmp_path: Path
) -> None:
    bad = tmp_path / 'bad.csv'
    bad.write_text('name,thickness\nwater,1\n')
    hold(page, 'api/table?')
    page.set_input_files('#pick-table', bad)
    held(page, 1)

    to_batch(page, 'Other')
    release(page, 0)

    page.wait_for_selector('#message:has-text("bad.csv")')
    assert page.inner_text('#message .message-text strong').startswith('Scan: ')
    assert page.locator('#rows .tabulator-row').count() == 1
    assert all('status of 422' in e for e in page.errors)
    page.errors.clear()


@pytest.mark.parametrize('start', ['', 'batches/gone/'])
def test_one_poll_runs_after_changes_from_the_root_or_a_batch_not_shown(
    browser: Any, second: str, url: str, start: str
) -> None:
    page = watched(browser)
    page.goto(url.removesuffix('batches/scan/') + start)
    page.wait_for_selector('#batches tbody tr:has-text("Other")')
    page.errors.clear()  # the batch gone answers 404
    page.evaluate(KEEP)

    for title in ['Scan', 'Other', 'Scan', 'Other']:
        to_batch(page, title)
        shows(page, title)
    page.go_back()
    shows(page, 'Scan')

    assert asked(page, '/api/state', seconds=3) <= 4
    assert page.evaluate(KEPT)
    page.close()
    assert page.errors == []


def test_a_change_while_rows_run_ends_with_the_counts_of_both_batches_right(
    second: str, page: Any, client: Client, gate: Gate
) -> None:
    gate.close()
    page.click('#reduce')
    page.wait_for_selector('#reduce.running')
    page.click('[data-view=batches]')
    page.wait_for_selector('#batches tr:has-text("Scan") .pill:text-is("3 running")')

    page.click('#batches a:text-is("Other")')
    shows(page, 'Other')
    gate.open()
    client.wait(client.records())
    page.click('[data-view=batches]')

    page.wait_for_selector('#batches tr:has-text("Scan") .pill:text-is("3 done")')
    assert page.inner_text('#batches tr:has-text("Other") td:nth-child(3)') == '1 ready'
    page.go_back()
    shows(page, 'Scan')
    page.click('[data-view=rows]')
    page.wait_for_selector('.chip.st-done .n:text-is("3")')
    assert page.locator('.chip.st-running').count() == 0


# Making a batch


def fill_new_batch(page: Any, title: str) -> None:
    """Fill the form "New batch", open, for a batch with the column Thickness."""
    page.fill('#new-title', title)
    page.check('#new-columns input[value=thickness]')


def test_a_person_makes_a_batch_and_lands_on_its_page(
    page: Any, manager: BatchManager
) -> None:
    page.click('[data-view=batches]')
    page.click('#new-batch-open')
    assert page.is_checked('#new-columns input[value=sample]')
    assert page.is_disabled('#new-columns input[value=sample]')

    fill_new_batch(page, 'Afternoon')
    page.click('#new-batch [type=submit]')

    page.wait_for_selector('#title:has-text("Afternoon")')
    (label,) = (k for k, h in manager.headers().items() if h.title == 'Afternoon')
    assert page.url.endswith(f'/batches/{label}/')
    page.wait_for_selector('.chip.st-all .n:text-is("3")')
    assert manager.batch(label).columns == ('sample', 'transmission', 'thickness')


def test_a_refusal_of_the_manager_shows_at_the_form_and_makes_nothing(
    page: Any, manager: BatchManager
) -> None:
    page.click('[data-view=batches]')
    page.click('#new-batch-open')

    fill_new_batch(page, ' ')
    page.click('#new-batch [type=submit]')

    page.wait_for_selector('#new-problem:has-text("a title that is not empty")')
    assert page.is_hidden('#message')
    assert page.is_enabled('#new-batch [type=submit]')
    assert [h.title for h in manager.headers().values()] == ['Scan']
    assert len(page.errors) == 1
    assert 'status of 422' in page.errors[0]
    page.errors.clear()


def test_a_double_click_on_make_makes_one_batch(
    page: Any, url: str, manager: BatchManager
) -> None:
    page.click('[data-view=batches]')
    page.click('#new-batch-open')
    sent: list[str] = []
    page.on('request', lambda r: sent.append(r.url) if r.method == 'POST' else None)

    fill_new_batch(page, 'Once')
    page.dblclick('#new-batch [type=submit]')

    page.wait_for_selector('#title:has-text("Once")')
    assert [u for u in sent if u.endswith('/api/batches')] == [f'{url}api/batches']
    assert sorted(h.title for h in manager.headers().values()) == ['Once', 'Scan']


def test_a_batch_is_made_of_the_range_of_rows_chosen_of_the_source(
    page: Any, manager: BatchManager
) -> None:
    page.click('[data-view=batches]')
    page.click('#new-batch-open')
    page.wait_for_selector(
        '#new-batch select[name=first] option[value="2"]', state='attached'
    )

    page.select_option('#new-batch select[name=first]', '2')
    page.wait_for_selector('#new-rows:text-is("The batch gets 2 rows.")')
    page.fill('#new-title', 'Later')
    page.click('#new-batch [type=submit]')

    page.wait_for_selector('#title:has-text("Later")')
    (label,) = (k for k, h in manager.headers().items() if h.title == 'Later')
    assert list(manager.batch(label).frame().index) == ['2', '3']


def test_rows_picked_in_the_dialog_are_added_from_the_runs(
    page: Any, batch: Batch, sample: Measure
) -> None:
    sample(4, 'ethanol')
    sample(5, 'acetone', partner=False)

    page.click('#take-open')
    page.wait_for_selector('#take-rows tr:has-text("acetone") .pill:text-is("waits")')
    page.check('#take-rows input[aria-label=ethanol]')
    page.click('#take [value=take]')

    page.wait_for_selector('#rows .tabulator-row:has-text("ethanol")')
    assert list(batch.frame().index) == ['1', '2', '3', '4']


def test_a_column_made_a_setting_and_a_column_again_keeps_its_cell_editors(
    page: Any, batch: Batch
) -> None:
    cell(page, 'sds', 'thickness').click()
    editor(page).fill('2.5')
    editor(page).press('Enter')
    cell(page, 'sds', 'thickness').filter(has_text='2.5').wait_for()

    header = page.locator('.tabulator-col[tabulator-field="cells.thickness.value"]')
    header.locator('.tabulator-header-popup-button').click()
    page.click('.tabulator-menu-item:has-text("Make this a setting")')
    page.check('#setting-values label:has-text("2.5") input')
    page.click('#make-setting [value=make]')
    page.click('[data-tab=settings]')
    page.click(
        '#settings h3:has-text("Thickness") button:text-is("Make this a column")'
    )

    cell(page, 'water', 'thickness').filter(has_text='2.5').wait_for()
    cell(page, 'water', 'thickness').click()
    editor(page).fill('3')
    editor(page).press('Enter')
    cell(page, 'water', 'thickness').filter(has_text='3').wait_for()
    assert batch.frame()['thickness'].tolist() == [3.0, 2.5, 2.5]


@pytest.fixture
def no_batch(browser: Any, pages: Pages, sample: Measure) -> Iterator[Any]:
    """The page at the root of a workspace without a batch."""
    sample(1, 'water')
    with serve(create_app(pages)) as url:
        page = watched(browser)
        page.goto(url)
        page.wait_for_selector('#new-batch:not([hidden])')
        yield page
        page.close()
    assert page.errors == []


def test_the_root_without_a_batch_shows_the_batches_and_runs_and_makes_one(
    no_batch: Any, manager: BatchManager
) -> None:
    assert no_batch.is_hidden('[data-view=rows]')
    assert no_batch.is_hidden('.right')
    no_batch.wait_for_selector('[data-view=runs] .n:text-is("2")')

    fill_new_batch(no_batch, 'First')
    no_batch.click('#new-batch [type=submit]')

    no_batch.wait_for_selector('#rows .tabulator-row:has-text("water")')
    (label,) = manager.headers()
    assert no_batch.url.endswith(f'/batches/{label}/')


def test_a_run_is_corrected_without_a_batch(no_batch: Any) -> None:
    correct_first_sample(no_batch, 'background')

    no_batch.wait_for_selector('#toast:has-text("Role corrected on 1 run.")')
    no_batch.wait_for_selector('#runs .tabulator-cell.edited:has-text("background")')


def test_a_batch_that_cannot_be_shown_says_why_and_shows_the_list(
    browser: Any,
    client: Client,
    refusing: Refusing,
    files: CorrectedFiles,
    sample: Measure,
) -> None:
    sample(1, 'water')
    first = BatchManager(client, sources=SOURCES, definitions=refusing)
    first.create(STAGED.name, title='Good', rows=toys.rows(client), label='good')
    first.create(STAGED.name, title='Broken', label='broken')
    refusing.save(rows_key('broken'), {'nonsense': 1})
    manager = BatchManager(client, sources=SOURCES, definitions=refusing)
    pages = Pages(manager, files=files, marks=Marks)
    with serve(create_app(pages)) as url:
        page = watched(browser)
        page.goto(f'{url}batches/broken/')

        page.wait_for_selector('#message:has-text("Cannot show broken: ")')
        page.wait_for_selector('#batches tbody tr:has-text("Good")')
        page.wait_for_selector('[data-view=runs] .n:text-is("2")')
        assert len(page.errors) == 1
        assert 'status of 422' in page.errors[0]
        page.errors.clear()
        page.click('#batches a:text-is("Good")')
        page.wait_for_selector('#rows .tabulator-row:has-text("water")')
        page.close()
    assert page.errors == []


# The tab "Batch"


def test_a_person_saves_a_title_and_a_description(
    page: Any, manager: BatchManager
) -> None:
    page.click('[data-tab=batch]')
    assert 'Staged' in page.inner_text('#batch-fixed')

    page.fill('#batch-title', 'Morning')
    page.fill('#batch-description', '1 mm cells')
    page.click('#batch-form [type=submit]')

    page.wait_for_selector('#title:has-text("Morning")')
    assert manager.headers()['scan'] == Header('Morning', '1 mm cells')


def test_a_refused_header_shows_at_the_form_and_changes_nothing(
    page: Any, manager: BatchManager
) -> None:
    page.click('[data-tab=batch]')

    page.fill('#batch-title', ' ')
    page.click('#batch-form [type=submit]')

    page.wait_for_selector('#batch-problem:has-text("a title that is not empty")')
    assert page.is_hidden('#message')
    assert manager.headers()['scan'].title == 'Scan'
    assert len(page.errors) == 1
    assert 'status of 422' in page.errors[0]
    page.errors.clear()


def test_remove_goes_to_another_batch_then_to_the_root_without_one(
    page: Any, manager: BatchManager
) -> None:
    manager.create(STAGED.name, title='Other', source='pairs', label='other')
    page.on('dialog', lambda dialog: dialog.accept())
    page.click('[data-tab=batch]')

    page.click('#remove-batch')
    page.wait_for_selector('#title:has-text("Other")')
    assert page.url.endswith('/batches/other/')
    page.click('[data-tab=batch]')
    page.click('#remove-batch')

    page.wait_for_selector('#new-batch:not([hidden])')
    assert manager.headers() == {}
    assert page.is_hidden('.right')
    # A poll on its way while the batch is removed answers 404.
    assert all('status of 404' in e for e in page.errors)
    page.errors.clear()


def test_another_page_of_a_removed_batch_shows_why_and_the_list(
    page: Any, pages: Pages, manager: BatchManager
) -> None:
    manager.create(STAGED.name, title='Other', source='pairs', label='other')

    pages.remove('scan')

    page.wait_for_selector('#message:has-text("There is no batch scan.")')
    page.wait_for_selector('#batches tbody tr:has-text("Other")')
    assert page.is_hidden('.right')
    assert page.is_hidden('#new-batch')
    assert all('status of 404' in e for e in page.errors)
    page.errors.clear()
