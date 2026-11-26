# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The batch page's API over HTTP, on a real in-process workspace."""

import io
import itertools
import re
import tempfile
import zipfile
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import numpy as np
import pandas as pd
import pytest
import scipp as sc
import yaml
from ess.batch import (
    Batch,
    BatchManager,
    CorrectedFiles,
    DefinitionStore,
    FolderDefinitions,
    Header,
    Intake,
    RowSource,
)
from ess.batch.definition import (
    SETTINGS,
    TABLE,
    DefinitionWriteError,
    Key,
)
from ess.batch.web import Pages, Shown, create_app, demo, plots
from ess.batch.web.errors import NotFound, Rejected
from ess.batch.web.models import NewBatch
from ess.batch.writing import Writing
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from ess.apps import Client, local
from ess.apps.testing import CountingClient, FakeFiles
from ess.apps.writers import SCIPP_HDF5
from ess.spec import FileRef, SampleRunFile, WorkflowSpec, function_binding

from . import toys
from .toys import (
    PLAIN,
    SOURCES,
    STAGED,
    STAGED_NAMES,
    TEXT,
    Gate,
    Measure,
    Outputs,
    Refusing,
    pair,
)

BINS = ['binning', 'bins']
"""The path of the number of bins in the settings form."""


class Marks(BaseModel):
    role: str | None = None


def draw(shown: Sequence[Shown]) -> dict[str, Any]:
    return {
        'drawn': [s.key for s in shown],
        'colors': [s.color for s in shown],
        'outdated': [s.outdated for s in shown],
    }


@pytest.fixture
def batch(manager: BatchManager, label: str) -> Batch:
    """The batch of ``label``, as the manager holds it."""
    return manager.batch(label)


@pytest.fixture
def pages(intake: Intake, files: CorrectedFiles) -> Pages:
    return Pages(
        intake,
        draw={STAGED.name: draw},
        files=files,
        marks=Marks,
    )


def opened(pages: Pages, label: str) -> TestClient:
    """A client of the page of ``label``, which asks for ``api/...`` as it does."""
    return TestClient(create_app(pages), base_url=f'http://testserver/batches/{label}/')


def home(app: FastAPI) -> TestClient:
    """A client of the page that ``/`` of ``app`` redirects to."""
    page = TestClient(app).get('/')
    assert page.status_code == 200
    return TestClient(app, base_url=str(page.url))


def restarted(client: Client, definitions: DefinitionStore) -> Pages:
    """The pages of an intake with the sources of ``toys`` on ``definitions``."""
    return Pages(Intake(BatchManager(client, definitions=definitions), sources=SOURCES))


def started(client: Client, definitions: DefinitionStore) -> Pages:
    """The pages of an intake made on ``definitions``, as after a restart."""
    manager = BatchManager(client, definitions=definitions, staged=STAGED_NAMES)
    return Pages(Intake(manager, sources=SOURCES))


@pytest.fixture
def http(
    pages: Pages, label: str, batch: Batch, sample: Measure, client: Client
) -> TestClient:
    sample(1, 'water')
    sample(2, 'sds')
    sample(3, 'oil', partner=False)
    batch.set_table(toys.rows(client))
    return opened(pages, label)


def state(http: TestClient) -> dict[str, Any]:
    return http.get('/api/state').json()


def rows(http: TestClient) -> dict[str, dict[str, Any]]:
    return {row['key']: row for row in state(http)['rows']}


def statuses(http: TestClient) -> dict[str, str]:
    return {key: row['status'] for key, row in rows(http).items()}


def thickness(http: TestClient, keys: list[str], value: Any) -> dict[str, Any]:
    edit = {'keys': keys, 'column': 'thickness', 'value': value}
    return http.post('/api/cells', json=edit).json()


def draft(http: TestClient, value: Any) -> dict[str, Any]:
    """Set the number of bins in the form only."""
    edit = {'path': BINS, 'op': 'set', 'value': value}
    return http.post('/api/settings', json=edit).json()


def bins(http: TestClient, value: Any) -> dict[str, Any]:
    """Set the number of bins in the form, and apply it."""
    draft(http, value)
    applied = http.post('/api/settings/apply')
    assert applied.status_code == 200, applied.json()
    return applied.json()


def form_bins(shown: dict[str, Any]) -> Any:
    """The number of bins that the form of the settings shows."""
    (binning, *_) = shown['settings']
    return binning['child']['children'][0]['value']


def reduced(http: TestClient, client: Client, which: str = 'remaining') -> None:
    assert http.post('/api/reduce', json={'which': which}).status_code == 200
    client.wait(client.records())


def test_the_page_lists_the_rows_with_their_names_notes_and_why_one_is_incomplete(
    http: TestClient,
) -> None:
    shown = rows(http)

    assert {k: r['name'] for k, r in shown.items()} == {
        '1': 'water',
        '2': 'sds',
        '3': 'oil',
    }
    assert [c['name'] for c in state(http)['notes']] == ['run']
    assert shown['1']['notes'] == {'run': '1'}
    assert shown['1']['cells']['sample']['text'] == 'run 1'
    assert statuses(http) == {'1': 'ready', '2': 'ready', '3': 'incomplete'}
    assert shown['3']['cells']['transmission']['problem'] == 'Transmission is missing'
    assert state(http)['action']['label'] == 'Reduce 2 remaining'


def test_a_poll_at_the_same_version_answers_204_but_not_after_a_restart(
    http: TestClient,
) -> None:
    now = state(http)
    same = {'after': now['revision'], 'start': now['start']}

    assert http.get('/api/state', params=same).status_code == 204
    restarted = http.get('/api/state', params={**same, 'start': 'other'})
    assert restarted.json()['revision'] == now['revision']


def test_a_poll_sees_a_reduction_finish(
    http: TestClient, client: Client, gate: Gate
) -> None:
    gate.close()
    running = http.post('/api/reduce', json={'which': 'remaining'}).json()
    gate.open()
    client.wait(client.records())

    after = {'after': running['revision'], 'start': running['start']}
    finished = http.get('/api/state', params=after).json()

    assert [r['status'] for r in running['rows']][:2] == ['running', 'running']
    assert [r['status'] for r in finished['rows']][:2] == ['done', 'done']


def test_a_poll_sees_a_row_that_another_caller_put_without_a_record(
    http: TestClient, batch: Batch, sample: Measure, client: Client
) -> None:
    now = state(http)
    after = {'after': now['revision'], 'start': now['start']}
    batch.set_table(batch.table())
    assert http.get('/api/state', params=after).status_code == 204

    sample(4, 'salt', partner=False)
    batch.set_table(toys.rows(client))
    polled = http.get('/api/state', params=after).json()

    salt = {row['key']: row for row in polled['rows']}['4']
    assert salt['status'] == 'incomplete'
    assert salt['cells']['sample']['value'].startswith('run 4')


@pytest.mark.parametrize(
    'act',
    [
        lambda page: thickness(page, ['1'], 3),
        lambda page: draft(page, 3),
        lambda page: page.post('/api/rows/remove', json={'keys': ['1']}),
    ],
    ids=['a cell', 'the settings form', 'a row removed'],
)
def test_an_action_of_one_page_reaches_another_at_its_next_poll(
    http: TestClient, act: Callable[[TestClient], Any]
) -> None:
    now = state(http)
    after = {'after': now['revision'], 'start': now['start']}
    assert http.get('/api/state', params=after).status_code == 204

    act(http)

    polled = http.get('/api/state', params=after)
    assert polled.status_code == 200
    assert polled.json()['revision'] > now['revision']


def test_a_rescan_offers_the_runs_it_found_and_changes_no_row(
    http: TestClient, sample: Measure
) -> None:
    before = state(http)['runs']
    sample(4, 'salt')

    listed = http.post('/api/relist').json()

    assert listed['runs'] > before  # the Runs view asks for the runs again
    assert [row['key'] for row in listed['rows']] == ['1', '2', '3']
    (transmission,) = [c for c in listed['columns'] if c['name'] == 'transmission']
    assert any(o.startswith('run 1004') for o in transmission['editor']['options'])


def test_an_action_answers_with_the_state_after_it(http: TestClient) -> None:
    after = thickness(http, ['1'], 3)

    cell = rows(http)['1']['cells']['thickness']
    assert after['rows'][0]['cells']['thickness'] == cell
    assert (cell['value'], cell['text']) == (3.0, '3.0')


def test_an_edit_of_several_rows_writes_each_and_says_so(
    http: TestClient, batch: Batch
) -> None:
    notice = thickness(http, ['1', '2'], 5.0)['notice']

    assert list(batch.table().loc[['1', '2'], 'thickness']) == [5.0, 5.0]
    assert notice == 'Thickness set on 2 rows.'


def test_an_edit_of_several_rows_with_one_unknown_edits_none(
    http: TestClient, batch: Batch
) -> None:
    before = batch.table()

    refused = http.post(
        '/api/cells', json={'keys': ['1', '9'], 'column': 'thickness', 'value': 5}
    )

    assert 'no row 9' in refused.json()['message']
    pd.testing.assert_frame_equal(batch.table(), before)


def test_a_file_cell_is_set_by_the_label_of_its_option_and_kept_as_its_name(
    http: TestClient, batch: Batch
) -> None:
    column = next(c for c in state(http)['columns'] if c['name'] == 'transmission')
    label = next(o for o in column['editor']['options'] if o.startswith('run 1001'))

    edit = {'keys': ['3'], 'column': 'transmission', 'value': label}
    http.post('/api/cells', json=edit)

    assert statuses(http)['3'] == 'ready'
    assert rows(http)['3']['cells']['transmission']['text'] == 'run 1001'
    table = batch.table()
    assert table.at['3', 'transmission'] == table.at['1', 'transmission']


def test_a_cell_emptied_is_empty_in_the_table(http: TestClient, batch: Batch) -> None:
    http.post('/api/cells', json={'keys': ['1'], 'column': 'transmission'})

    assert statuses(http)['1'] == 'incomplete'
    assert pd.isna(batch.table().at['1', 'transmission'])


@pytest.mark.parametrize(
    ('request_', 'message'),
    [
        (('post', '/api/cells', {'keys': ['9'], 'column': 'thickness'}), 'no row 9'),
        (('post', '/api/clear', {'keys': ['1', '9']}), 'no row 9'),
        (('post', '/api/cells', {'keys': ['1'], 'column': 'colour'}), 'no column'),
        (('get', '/api/row?key=a/b', None), 'no row a/b'),
        (
            (
                'post',
                '/api/cells',
                {'keys': ['1'], 'column': 'thickness', 'value': 'x'},
            ),
            'Thickness: x is not a number',
        ),
    ],
)
def test_a_request_that_cannot_be_done_answers_422_with_a_message(
    http: TestClient, request_: tuple[str, str, Any], message: str
) -> None:
    method, url, body = request_

    response = http.request(method, url, json=body)

    assert response.status_code == 422
    assert message in response.json()['message']


def test_a_refused_reduction_answers_422_and_the_state_names_the_cell(
    http: TestClient, batch: Batch
) -> None:
    # The state of a batch does not read the records that a cell references,
    # so only the refusal of the submission, kept by the page, marks the cell.
    batch.set_table(batch.table().assign(offset='nope.x'))
    assert rows(http)['1']['cells']['offset']['problem'] is None

    response = http.post('/api/reduce', json={'which': 'selected', 'keys': ['1']})

    assert response.status_code == 422
    assert response.json() == {'message': 'Nothing was reduced: one value is rejected.'}
    shown = state(http)
    (refused,) = shown['submit_errors']
    assert refused['text'] == 'water: Offset: unknown record or accumulator nope'
    problem = shown['rows'][0]['cells']['offset']['problem']
    assert problem == 'Offset: unknown record or accumulator nope'


def test_a_refused_value_stays_marked_until_it_is_edited(http: TestClient) -> None:
    thickness(http, ['1'], -1)
    http.post('/api/reduce', json={'which': 'selected', 'keys': ['1']})

    thickness(http, ['2'], 3)
    assert rows(http)['1']['cells']['thickness']['problem']
    thickness(http, ['1'], 3)
    assert state(http)['submit_errors'] == []


def refuse_thickness_of_water(http: TestClient) -> None:
    thickness(http, ['1'], -1)
    refused = http.post('/api/reduce', json={'which': 'selected', 'keys': ['1']})
    assert refused.status_code == 422


def test_a_refusal_goes_when_a_reduction_succeeds(http: TestClient) -> None:
    refuse_thickness_of_water(http)
    assert state(http)['submit_errors'] != []

    reduce = http.post('/api/reduce', json={'which': 'selected', 'keys': ['2']})

    assert reduce.status_code == 200
    assert reduce.json()['submit_errors'] == []


def test_a_refusal_stays_when_another_cell_of_its_row_is_edited(
    http: TestClient,
) -> None:
    refuse_thickness_of_water(http)
    column = next(c for c in state(http)['columns'] if c['name'] == 'transmission')
    label = next(o for o in column['editor']['options'] if o.startswith('run 1002'))

    http.post(
        '/api/cells', json={'keys': ['1'], 'column': 'transmission', 'value': label}
    )

    assert [r['request_key'] for r in state(http)['submit_errors']] == ['1']


def test_a_refusal_of_a_cell_stays_when_other_settings_are_applied(
    http: TestClient,
) -> None:
    refuse_thickness_of_water(http)

    applied = bins(http, 3)

    assert [r['request_key'] for r in applied['submit_errors']] == ['1']


def test_a_refusal_of_a_setting_goes_when_settings_are_applied(
    http: TestClient,
) -> None:
    bins(http, 0)
    assert http.post('/api/reduce', json={'which': 'remaining'}).status_code == 422
    assert [r['request_key'] for r in state(http)['submit_errors']] == [None]

    applied = bins(http, 3)

    assert applied['submit_errors'] == []


def test_reduce_then_the_state_shows_the_rows_done(
    http: TestClient, client: Client
) -> None:
    reduced(http, client)

    assert statuses(http) == {'1': 'done', '2': 'done', '3': 'incomplete'}
    assert state(http)['action']['label'] == 'Nothing left to reduce'


def test_reduce_all_reduces_the_rows_done_again(
    http: TestClient, client: Client
) -> None:
    reduced(http, client)
    first = {k: r['record'] for k, r in rows(http).items()}

    reduced(http, client, which='all')

    again = {k: r['record'] for k, r in rows(http).items()}
    assert [k for k in first if first[k] != again[k]] == ['1', '2']


def test_rows_running_show_progress_and_cancel_shows_as_cancelled(
    http: TestClient, client: Client, gate: Gate
) -> None:
    gate.close()
    http.post('/api/reduce', json={'which': 'remaining'})
    assert state(http)['action']['progress'] == [0, 2]

    http.post('/api/cancel')
    gate.open()
    client.wait(client.records())

    assert statuses(http) == {'1': 'cancelled', '2': 'cancelled', '3': 'incomplete'}
    assert state(http)['action']['label'] == 'Retry 2 cancelled'


def test_clear_outputs_of_rows_shows_them_cleared_and_reduce_computes_them_again(
    http: TestClient, client: Client
) -> None:
    reduced(http, client)

    cleared = http.post('/api/clear', json={'keys': ['1']}).json()

    assert cleared['notice'] == (
        'Cleared the outputs of 1 row. Reduce computes them again.'
    )
    shown = {k: r for k, r in rows(http).items() if k in ('1', '2')}
    assert {k: (r['status'], r['result']) for k, r in shown.items()} == {
        '1': ('cleared', None),
        '2': ('done', 'current'),
    }
    assert shown['1']['reason'] == (
        'Its outputs were cleared. "Reduce" computes them again.'
    )
    reduced(http, client)
    assert statuses(http) == {'1': 'done', '2': 'done', '3': 'incomplete'}


def test_clear_outputs_of_the_batch_clears_every_row(
    http: TestClient, client: Client
) -> None:
    reduced(http, client)

    cleared = http.post('/api/clear', json={}).json()

    assert cleared['notice'] == (
        'Cleared the outputs of the batch. Reduce computes them again.'
    )
    assert statuses(http) == {'1': 'cleared', '2': 'cleared', '3': 'incomplete'}


def test_a_clear_of_a_batch_with_a_running_row_answers_422_and_names_the_row(
    http: TestClient, client: Client, gate: Gate
) -> None:
    gate.close()
    http.post('/api/reduce', json={'which': 'remaining'})

    answer = http.post('/api/clear', json={})

    gate.open()
    client.wait(client.records())
    assert answer.status_code == 422
    assert answer.json()['message'] == (
        'Nothing was cleared: row 1 of Scan is still running.'
    )


def test_clear_all_outputs_clears_the_outputs_of_every_batch(
    http: TestClient, client: Client
) -> None:
    reduced(http, client)

    cleared = http.post('/api/outputs/clear')

    assert cleared.json() == {
        'notice': 'Cleared the outputs of every batch. Reduce computes them again.'
    }
    assert statuses(http) == {'1': 'cleared', '2': 'cleared', '3': 'incomplete'}


def test_clear_all_outputs_with_a_running_row_answers_422_and_clears_nothing(
    http: TestClient, client: Client, gate: Gate
) -> None:
    reduced(http, client, 'all')
    http.post('/api/clear', json={'keys': ['2']})
    gate.close()
    http.post('/api/reduce', json={'which': 'remaining'})

    answer = http.post('/api/outputs/clear')

    gate.open()
    client.wait(client.records())
    assert answer.status_code == 422
    assert answer.json()['message'] == (
        'Nothing was cleared: row 2 of Scan is still running.'
    )
    assert statuses(http)['1'] == 'done'


def test_the_plot_and_the_results_leave_out_a_cleared_row(
    http: TestClient, client: Client
) -> None:
    reduced(http, client)

    http.post('/api/clear', json={'keys': ['1']})

    assert http.post('/api/plot', json={}).json()['figure']['drawn'] == ['2']
    content = http.post('/api/results.zip', json={'keys': []}).content
    names = zipfile.ZipFile(io.BytesIO(content)).namelist()
    assert not any(n.startswith('results/1/') for n in names)
    assert any(n.startswith('results/2/') for n in names)


def test_the_action_counts_the_rows_running_and_the_rows_that_can_be_reduced_again(
    http: TestClient, client: Client, gate: Gate
) -> None:
    def counts() -> dict[str, int]:
        action = state(http)['action']
        return {k: action[k] for k in ('remaining', 'again', 'running')}

    before = counts()
    gate.close()
    http.post('/api/reduce', json={'which': 'remaining'})
    running = counts()
    gate.open()
    client.wait(client.records())

    assert before == {'remaining': 2, 'again': 2, 'running': 0}
    assert running == {'remaining': 0, 'again': 0, 'running': 2}
    assert counts() == {'remaining': 0, 'again': 2, 'running': 0}


def test_a_row_says_whether_its_result_is_current_or_outdated(
    http: TestClient, client: Client
) -> None:
    assert rows(http)['1']['result'] is None
    reduced(http, client)
    thickness(http, ['1'], 3)

    shown = rows(http)

    assert (shown['1']['status'], shown['1']['result']) == ('outdated', 'outdated')
    assert (shown['2']['status'], shown['2']['result']) == ('done', 'current')
    assert shown['3']['result'] is None


def test_a_changed_setting_of_a_column_turns_the_rows_that_take_it_settings_changed(
    http: TestClient, client: Client
) -> None:
    thickness(http, ['1'], 3.0)
    reduced(http, client)

    http.post('/api/settings', json={'path': ['thickness'], 'op': 'set', 'value': 2.5})
    http.post('/api/settings/apply')

    assert statuses(http) == {'1': 'done', '2': 'settings changed', '3': 'incomplete'}
    assert rows(http)['2']['changed'] == 'Thickness changed'
    # The row tab shows the value that the row took, and that it changed.
    detail = http.get('/api/row', params={'key': '2'}).json()
    assert detail['differs'] == ['thickness']
    (took,) = [n for n in detail['reduced_with'] if n['path'] == ['thickness']]
    assert took['value'] == 1.0


@pytest.mark.parametrize(('key', 'shown'), [('1', 2.5), ('2', 1.0)])
def test_the_settings_of_a_row_put_in_the_form_are_those_it_took_at_a_column(
    http: TestClient, client: Client, key: str, shown: float
) -> None:
    thickness(http, ['1'], 3.0)
    reduced(http, client)
    http.post('/api/settings', json={'path': ['thickness'], 'op': 'set', 'value': 2.5})
    http.post('/api/settings/apply')

    put = http.post('/api/row/use-settings', json={'key': key}).json()

    # Row 1 gave its own value, so the setting stays; row 2 took the setting.
    (field,) = [n for n in put['settings'] if n['path'] == ['thickness']]
    assert field['value'] == shown
    assert put['settings_unapplied'] is (key == '2')


def test_a_column_that_the_source_fills_has_no_setting_on_the_page(
    http: TestClient, batch: Batch
) -> None:
    template = yaml.safe_load(http.get('/api/settings.yaml').content)
    template['params'] = {'transmission': 'run:1001', 'thickness': 2.0}
    loaded = http.post(
        '/api/settings.yaml',
        params={'name': 'f'},
        content=yaml.safe_dump(template).encode(),
    ).json()
    http.post('/api/settings/apply')
    made = http.post('/api/columns/setting', json={'name': 'transmission'})
    without = b'key,sample,name\na,run:1,first\n'
    table = http.post('/api/table', params={'name': 'rows.csv'}, content=without)

    form = [n['path'][0] for n in loaded['settings']]
    assert form == ['binning', 'thickness', 'offset']
    assert [c['from_source'] for c in loaded['columns']] == [True, True, False]
    assert batch.settings['thickness'] == 2.0
    assert 'transmission' not in batch.settings
    assert made.status_code == 422
    assert 'the source of the batch fills transmission' in made.json()['message']
    assert table.status_code == 422
    assert 'fills transmission' in table.json()['message']


def test_the_setting_of_a_column_without_a_default_can_be_reset_to_none(
    manager: BatchManager, pages: Pages
) -> None:
    table = pd.DataFrame(
        {'sample': [None], 'transmission': ['run:1001']}, index=pd.Index(['a'])
    )
    settings = {'sample': FileRef(file='run:1')}
    label = manager.create(STAGED.name, title='T', table=table, settings=settings).label
    page = opened(pages, label)
    shown = state(page)

    page.post('/api/settings', json={'path': ['sample'], 'op': 'reset'})
    applied = page.post('/api/settings/apply').json()

    (sample,) = [n for n in shown['settings'] if n['path'] == ['sample']]
    assert sample['value'] is not None
    assert 'sample' not in manager.batch(label).settings
    assert [r['status'] for r in applied['rows']] == ['incomplete']


def test_a_changed_setting_leaves_the_rows_reduced_reading_settings_changed(
    http: TestClient, client: Client
) -> None:
    reduced(http, client)

    bins(http, 3)

    shown = state(http)
    assert statuses(http) == {
        '1': 'settings changed',
        '2': 'settings changed',
        '3': 'incomplete',
    }
    assert rows(http)['1']['changed'] == 'Binning changed'
    assert rows(http)['1']['reason'].startswith(
        'The settings changed since the reduction: Binning.'
    )
    assert (shown['counts']['settings changed'], shown['counts']['done']) == (2, 0)
    assert shown['action']['label'] == 'Nothing left to reduce'
    detail = http.get('/api/row', params={'key': '1'}).json()
    assert detail['differs'] == ['binning']
    table = pd.read_csv(io.BytesIO(http.get('/api/table.csv').content), dtype=str)
    assert list(table['status']) == [
        'settings changed',
        'settings changed',
        'incomplete',
    ]


def test_the_rows_of_earlier_settings_have_results_to_plot_and_download(
    http: TestClient, client: Client
) -> None:
    reduced(http, client)
    bins(http, 3)

    plot = http.post('/api/plot', json={}).json()
    content = http.post('/api/results.zip', json={'keys': []}).content

    assert [r['result'] for r in state(http)['rows']] == ['current', 'current', None]
    assert (plot['figure']['drawn'], plot['figure']['outdated']) == (
        ['1', '2'],
        [False, False],
    )
    names = zipfile.ZipFile(io.BytesIO(content)).namelist()
    assert {n.split('/')[1] for n in names} == {'1', '2', 'results.csv'}


def test_reduce_leaves_the_rows_of_earlier_settings_and_apply_to_all_reduces_them(
    http: TestClient, client: Client
) -> None:
    reduced(http, client)
    bins(http, 3)
    thickness(http, ['2'], 4.0)

    reduced(http, client)
    assert (statuses(http)['1'], statuses(http)['2']) == ('settings changed', 'done')

    reduced(http, client, which='settings changed')
    assert statuses(http)['1'] == 'done'
    assert state(http)['counts']['settings changed'] == 0


def test_a_page_started_again_marks_the_rows_reduced_with_other_settings(
    http: TestClient, client: Client, definitions: DefinitionStore, label: str
) -> None:
    reduced(http, client)
    again = opened(started(client, definitions), label)

    bins(again, 3)

    assert statuses(again) == {
        '1': 'settings changed',
        '2': 'settings changed',
        '3': 'incomplete',
    }


class Count(BaseModel):
    count: int = Field(default=2, ge=1)


class ParamsV2(BaseModel):
    """The toy's params with ``binning.bins`` renamed and ``offset`` removed."""

    binning: Count = Field(default_factory=Count)
    sample: SampleRunFile
    transmission: SampleRunFile
    thickness: float = 1.0


def test_a_row_reduced_by_another_spec_version_is_outdated(
    files: CorrectedFiles, sample: Measure, definitions: DefinitionStore
) -> None:
    v2 = PLAIN.model_copy(update={'version': 2, 'params': ParamsV2})
    bind = [function_binding(s, lambda _: Outputs(total=1.0)) for s in (PLAIN, v2)]
    sample(1, 'water')
    with local(proposal='p1', files=files, bind=bind) as client:
        pages = started(client, definitions)
        label = toys.from_pairs(pages.intake, PLAIN.name, title='New')
        old = Batch(client, PLAIN, label=label, table=toys.rows(client))
        client.wait(old.reduce(['1']))
        page = opened(pages, label)

        assert statuses(page) == {'1': 'outdated'}
        assert rows(page)['1']['reason'] == (
            'Binning changed. It was reduced by another version of the workflow.'
        )
        assert state(page)['action']['label'] == 'Reduce 1 outdated'
        detail = page.get('/api/row', params={'key': '1'}).json()
        assert detail['reduced_with'] is None
        used = page.post('/api/row/use-settings', json={'key': '1'})
        assert used.status_code == 422
        assert 'another version' in used.json()['message']


def test_a_failed_row_stays_failed_when_the_settings_change(
    http: TestClient, client: Client
) -> None:
    bins(http, 1)
    reduced(http, client)

    bins(http, 2)

    assert statuses(http)['1'] == 'failed'
    assert rows(http)['1']['reason'] == 'one bin'
    assert state(http)['action']['label'] == 'Retry 2 failed'


def test_a_failed_row_reads_failed_in_the_table_and_the_counts_when_settings_change(
    http: TestClient, client: Client
) -> None:
    bins(http, 1)
    reduced(http, client)

    bins(http, 2)

    table = pd.read_csv(io.BytesIO(http.get('/api/table.csv').content), dtype=str)
    assert list(table['status']) == ['failed', 'failed', 'incomplete']
    counts = state(http)['counts']
    assert (counts['failed'], counts['outdated']) == (2, 0)


def test_an_edit_of_the_settings_changes_the_form_only(
    http: TestClient, client: Client, batch: Batch
) -> None:
    reduced(http, client)

    edited = draft(http, 3)

    assert form_bins(edited) == 3
    assert edited['settings_unapplied'] is True
    assert batch.settings == {}
    assert statuses(http) == {'1': 'done', '2': 'done', '3': 'incomplete'}


def test_apply_writes_the_form_to_the_batch(http: TestClient, batch: Batch) -> None:
    draft(http, 3)

    applied = http.post('/api/settings/apply').json()

    assert batch.settings['binning'] == {'bins': 3}
    assert applied['settings_unapplied'] is False
    assert applied['notice'] == 'Applied the settings.'


def test_discard_shows_the_batch_s_settings_again(
    http: TestClient, batch: Batch
) -> None:
    bins(http, 3)
    draft(http, 5)

    discarded = http.post('/api/settings/discard').json()

    assert form_bins(discarded) == 3
    assert discarded['settings_unapplied'] is False
    assert batch.settings['binning'] == {'bins': 3}


def test_apply_is_refused_while_the_form_misses_something(
    http: TestClient, client: Client, batch: Batch
) -> None:
    reduced(http, client)
    http.post(
        '/api/settings',
        json={'path': ['offset'], 'op': 'choose', 'value': 'From a record'},
    )

    applied = http.post('/api/settings/apply')

    assert applied.json() == {'message': 'Not applied: Offset: choose a result.'}
    shown = state(http)
    assert shown['settings_unapplied'] is True
    assert shown['settings_problem'] == 'Offset: choose a result'
    assert shown['action']['label'] == 'Fill in or discard the settings'
    assert shown['action']['blocked'] == 'Fill in or discard the settings'
    assert batch.settings == {}
    assert {r['status'] for r in shown['rows'][:2]} == {'done'}


def test_a_setting_refused_is_marked_at_its_field_before_reducing(
    http: TestClient, client: Client
) -> None:
    reduced(http, client)

    bins(http, 0)

    shown = state(http)
    (binning, *_) = shown['settings']
    assert binning['child']['children'][0]['path'] == BINS
    assert 'greater than' in binning['child']['children'][0]['problem']
    assert 'greater than' in shown['settings_problem']
    assert statuses(http) == {'1': 'outdated', '2': 'outdated', '3': 'incomplete'}
    assert http.post('/api/reduce', json={'which': 'remaining'}).status_code == 422


def test_a_refused_setting_is_marked_only_while_the_form_shows_it(
    http: TestClient,
) -> None:
    bins(http, 0)

    other = http.post(
        '/api/settings', json={'path': ['offset'], 'op': 'choose', 'value': 'Given'}
    ).json()
    edited = draft(http, 3)
    discarded = http.post('/api/settings/discard').json()

    def problem(shown: dict[str, Any]) -> str | None:
        return shown['settings'][0]['child']['children'][0]['problem']

    assert 'greater than' in (problem(other) or '')
    assert 'greater than' in (other['settings_problem'] or '')
    assert (problem(edited), edited['settings_problem']) == (None, None)
    assert 'greater than' in (problem(discarded) or '')


@pytest.mark.parametrize('suffix', ['csv', 'xlsx'])
def test_the_table_downloaded_and_uploaded_gives_the_same_table(
    http: TestClient, batch: Batch, manager: BatchManager, pages: Pages, suffix: str
) -> None:
    thickness(http, ['2'], 4)
    content = http.get(f'/api/table.{suffix}').content
    other = manager.create(STAGED.name, title='Other').label

    loaded = opened(pages, other).post(
        '/api/table', params={'name': f'rows.{suffix}'}, content=content
    )

    assert loaded.json()['notice'] == f'Loaded 3 rows from rows.{suffix}.'
    # A cell of .xlsx has a type; a note of .csv is text.
    expected = batch.table()
    if suffix == 'csv':
        expected = expected.astype({'run': 'string'})
    pd.testing.assert_frame_equal(manager.batch(other).table(), expected)


def test_a_table_loaded_replaces_the_rows_the_columns_and_so_the_forms(
    http: TestClient, batch: Batch
) -> None:
    content = b'key,sample,transmission,name\na,run:1,run:1001,first\n'

    loaded = http.post('/api/table', params={'name': 'rows.csv'}, content=content)

    assert loaded.status_code == 200, loaded.json()
    shown = loaded.json()
    assert [(r['key'], r['name']) for r in shown['rows']] == [('a', 'first')]
    assert [c['name'] for c in shown['columns']] == ['sample', 'transmission']
    assert batch.parameter_columns == ('sample', 'transmission')
    assert 'thickness' in [n['path'][0] for n in shown['settings']]
    assert thickness(http, ['a'], 3) == {'message': 'There is no column thickness.'}


def test_a_table_the_batch_refuses_is_not_loaded_and_the_rows_stay(
    http: TestClient,
) -> None:
    content = b'key,sample,thickness\na,run:1,thick\n'

    loaded = http.post('/api/table', params={'name': 'rows.csv'}, content=content)

    assert loaded.status_code == 422
    assert loaded.json()['message'].startswith('Cannot load rows.csv: ')
    assert list(rows(http)) == ['1', '2', '3']


def kept_settings(store: DefinitionStore, params: dict[str, Any]) -> None:
    """Keep ``params`` as the settings of the one batch in ``store``."""
    (label,) = store.labels()
    kept = store.load(SETTINGS.key(label), dict)
    store.save(SETTINGS.key(label), {**kept, 'params': params})


def test_a_page_on_settings_kept_for_another_version_starts_and_names_them(
    tmp_path: Path,
) -> None:
    client, files, store = demo.open_demo(tmp_path)

    with client:
        demo.build(client, files, store)
        kept_settings(store, {'gone': 1})
        shown = state(home(demo.build(client, files, store)))

    assert shown['settings_problem'] == (
        'Settings that this form cannot show: gone = 1: not a setting here; '
        'gone: not a parameter of demo-iofq'
    )


def test_settings_kept_that_the_form_cannot_show_are_named_and_kept_until_apply(
    tmp_path: Path,
) -> None:
    client, files, definitions = demo.open_demo(tmp_path)
    params = {'wavelength': 5.0, 'corrections': {'banks': 3, 'solid_angle': False}}

    with client:
        demo.build(client, files, definitions)
        kept_settings(definitions, params)
        page = home(demo.build(client, files, definitions))
        shown = state(page)
        (label,) = definitions.labels()
        still = definitions.load(SETTINGS.key(label), lambda d: d['params'])
        after = page.post('/api/settings/apply').json()

    assert shown['settings_unapplied'] is False
    assert shown['settings_problem'].startswith(
        'Settings that this form cannot show: wavelength = 5.0: 5.0 is not a group'
    )
    assert (
        'corrections = {"banks":3,"solid_angle":false}: ' in shown['settings_problem']
    )
    assert still == params
    assert after['settings_problem'] is None


class Flaky:
    """Definitions in memory, which fail to keep anything while ``fail`` is set."""

    def __init__(self) -> None:
        self.kept: dict[Key, Any] = {}
        self.saves = 0
        self.fail = False

    def load[T](self, key: Key, read: Callable[[Any], T]) -> T | None:
        return read(self.kept[key]) if key in self.kept else None

    def save(self, key: Key, data: Any) -> None:
        if self.fail:
            raise DefinitionWriteError('disk full')
        self.saves += 1
        self.kept[key] = data

    def delete(self, key: Key) -> None:
        self.kept.pop(key, None)

    def labels(self) -> list[str]:
        return sorted({key[1] for key in self.kept if key[0] == 'labels'})


@pytest.fixture
def flaky() -> Flaky:
    return Flaky()


@pytest.fixture
def kept(client: Client, fake: FakeFiles, sample: Measure, flaky: Flaky) -> TestClient:
    """A page on a batch and runs whose definitions are :class:`Flaky`."""
    sample(1, 'water')
    sample(2, 'sds')
    intake = Intake(BatchManager(client, definitions=flaky), sources=SOURCES)
    label = toys.from_pairs(intake, STAGED.name, title='Kept', label='kept')
    files = CorrectedFiles(fake, definitions=flaky)
    return opened(Pages(intake, files=files, marks=Marks), label)


def test_a_change_the_definitions_cannot_keep_answers_422_and_nothing_changes(
    kept: TestClient, flaky: Flaky
) -> None:
    before = dict(flaky.kept)
    flaky.fail = True

    draft(kept, 7)
    setting = kept.post('/api/settings/apply')
    cell = thickness(kept, ['1'], 3)
    kept.post('/api/settings/discard')  # a table is not loaded over a draft
    table = kept.post(
        '/api/table',
        params={'name': 'rows.csv'},
        content=b'key,sample,transmission,thickness\n1,1,1001,3\n',
    )

    assert setting.json() == {'message': 'The change was not stored: disk full'}
    assert cell == {'message': 'The change was not stored: disk full'}
    assert table.json() == {'message': 'The change was not stored: disk full'}
    assert flaky.kept == before
    assert rows(kept)['1']['cells']['thickness']['text'] == ''


def test_settings_not_kept_stay_in_the_form_to_apply_again(
    kept: TestClient, flaky: Flaky
) -> None:
    draft(kept, 7)
    flaky.fail = True
    kept.post('/api/settings/apply')
    shown = state(kept)
    flaky.fail = False

    kept.post('/api/settings/apply')

    assert (form_bins(shown), shown['settings_unapplied']) == (7, True)
    assert flaky.kept[SETTINGS.key('kept')]['params']['binning'] == {'bins': 7}


def test_a_correction_of_several_runs_is_one_write_and_all_or_none(
    kept: TestClient, flaky: Flaky
) -> None:
    keys = [r['key'] for r in kept.get('/api/runs').json()['rows'][:2]]
    edit = {'keys': keys, 'column': 'role', 'value': 'transmission'}
    saves = flaky.saves

    flaky.fail = True
    refused = kept.post('/api/runs/cells', json=edit)
    roles = [
        r['cells']['role']['edited'] for r in kept.get('/api/runs').json()['rows'][:2]
    ]
    flaky.fail = False
    kept.post('/api/runs/cells', json=edit)

    assert refused.status_code == 422
    assert roles == [False, False]
    assert flaky.saves == saves + 1


def test_what_a_person_set_on_the_page_is_there_when_it_is_served_again(
    tmp_path: Path,
) -> None:
    def served() -> tuple[Client, TestClient]:
        client, files, store = demo.open_demo(tmp_path)
        return client, home(demo.build(client, files, store))

    client, page = served()
    with client:
        run = page.get('/api/runs').json()['rows'][1]['key']
        page.post(
            '/api/cells', json={'keys': ['60302'], 'column': 'thickness', 'value': 2.5}
        )
        page.post(
            '/api/settings',
            json={'path': ['wavelength', 'start'], 'op': 'set', 'value': 3},
        )
        page.post('/api/settings/apply')
        page.post(
            '/api/runs/cells',
            json={'keys': [run], 'column': 'sample', 'value': 'moved'},
        )
        before = state(page)
        runs = page.get('/api/runs').json()

    client, page = served()
    with client:
        after = state(page)
        assert page.get('/api/runs').json()['rows'] == runs['rows']
    wavelength = {tuple(n['path']): n for n in after['settings']}['wavelength',]
    assert wavelength['child']['children'][0]['value'] == 3.0
    assert runs['rows'][1]['cells']['sample']['edited']
    assert after['rows'] == before['rows']
    assert after['settings'] == before['settings']
    shown = {r['key']: r for r in after['rows']}['60302']['cells']['thickness']
    assert shown['text'] == '2.5'


def test_a_settings_file_loaded_fills_the_form_until_apply(
    http: TestClient, batch: Batch
) -> None:
    content = http.get('/api/settings.yaml').content
    bins(http, 9)

    name = {'name': 'mine.yaml'}
    loaded = http.post('/api/settings.yaml', params=name, content=content).json()
    http.post('/api/settings/apply')

    assert loaded['notice'] == (
        'The form shows the settings of mine.yaml. Apply to use them.'
    )
    assert (form_bins(loaded), loaded['settings_unapplied']) == (2, True)
    assert batch.settings['binning'] == {'bins': 2}


def test_a_refused_settings_file_leaves_the_form_as_it_was(
    http: TestClient, batch: Batch
) -> None:
    template = yaml.safe_load(http.get('/api/settings.yaml').content)
    template['params'] = {'binning': {'bins': 5}, 'offset': 'nonsense'}
    before = draft(http, 3)

    loaded = http.post(
        '/api/settings.yaml',
        params={'name': 'f'},
        content=yaml.safe_dump(template).encode(),
    )

    assert loaded.status_code == 422
    assert state(http)['settings'] == before['settings']
    assert batch.settings == {}


def test_settings_the_batch_has_show_and_an_applied_edit_of_another_keeps_them(
    pages: Pages, label: str, batch: Batch
) -> None:
    batch.settings = {'binning': {'bins': 5}, 'offset': {'value': 1.5}}
    page = opened(pages, label)

    shown = state(page)
    page.post(
        '/api/settings', json={'path': ['offset', 'value'], 'op': 'set', 'value': 2}
    )
    page.post('/api/settings/apply')

    assert form_bins(shown) == 5
    # The field of the column thickness shows its default, which Apply writes.
    assert batch.settings == {
        'binning': {'bins': 5},
        'thickness': 1.0,
        'offset': {'value': 2.0},
    }


@pytest.mark.parametrize('which', ['remaining', 'settings changed', 'all', 'selected'])
def test_while_the_form_holds_a_change_nothing_is_reduced(
    http: TestClient, client: Client, which: str
) -> None:
    draft(http, 3)

    reduce = http.post('/api/reduce', json={'which': which, 'keys': ['1']})

    assert reduce.json() == {
        'message': 'Nothing was reduced: apply or discard the settings.'
    }
    assert state(http)['action']['blocked'] == 'Apply or discard the settings'
    assert client.records(label='scan') == []


def test_the_settings_file_holds_the_settings_at_parameter_columns(
    http: TestClient, batch: Batch
) -> None:
    batch.settings = {**batch.settings, 'thickness': 2.5}

    template = yaml.safe_load(http.get('/api/settings.yaml').content)

    assert 'thickness' in batch.parameter_columns
    assert template['params']['thickness'] == 2.5


def test_while_the_form_holds_a_change_the_settings_are_not_downloaded(
    http: TestClient,
) -> None:
    draft(http, 3)

    download = http.get('/api/settings.yaml')

    assert download.json() == {'message': 'Apply or discard the settings first.'}


@pytest.mark.parametrize(
    ('content', 'message'),
    [
        (b'just: [unclosed', 'it is not a settings file saved from this page.'),
        (b'a,b\n1,2\n', 'it is not a settings file saved from this page.'),
        (None, 'Bins: two is not a number.'),
    ],
)
def test_a_settings_file_that_cannot_be_loaded_says_why_in_a_sentence(
    http: TestClient, content: bytes | None, message: str
) -> None:
    if content is None:  # a settings file with a value of the wrong kind
        template = yaml.safe_load(http.get('/api/settings.yaml').content)
        template['params']['binning'] = {'bins': 'two'}
        content = yaml.safe_dump(template).encode()

    response = http.post('/api/settings.yaml', params={'name': 'f'}, content=content)

    assert response.json() == {'message': f'Cannot load f: {message}'}


def test_the_results_of_the_rows_done_download_as_a_zip(
    http: TestClient, client: Client
) -> None:
    reduced(http, client)

    content = http.post('/api/results.zip', json={'keys': ['1', '3']}).content

    names = zipfile.ZipFile(io.BytesIO(content)).namelist()
    assert sorted(names) == [
        'results/1/_record.json',
        'results/1/total.h5',
        'results/results.csv',
    ]


def test_the_plot_draws_the_results_of_the_rows_asked_for_in_their_colours(
    http: TestClient, client: Client
) -> None:
    reduced(http, client)

    plot = http.post('/api/plot', json={'keys': ['2', '3']}).json()

    assert plot['figure'] == {
        'drawn': ['2'],
        'colors': [plots.PALETTE[1]],
        'outdated': [False],
    }
    assert plot['colors'] == {'2': plots.PALETTE[1]}
    assert http.post('/api/plot', json={}).json()['figure']['drawn'] == ['1', '2']


def test_a_drawing_that_raises_says_so_in_place_of_the_figure(
    intake: Intake,
    label: str,
    client: Client,
    sample: Measure,
) -> None:
    def broken(_: Sequence[Shown]) -> Any:
        raise RuntimeError('released')

    sample(1, 'water')
    intake.manager.batch(label).set_table(toys.rows(client))
    pages = Pages(intake, draw={STAGED.name: broken})
    page = opened(pages, label)
    reduced(page, client)

    plot = page.post('/api/plot', json={}).json()

    assert plot['figure'] is None
    assert 'Cannot draw: RuntimeError: released' in plot['note']


def test_no_two_curves_of_a_figure_share_a_colour() -> None:
    n = len(plots.PALETTE)
    first, second, third = plots.PALETTE[:3]

    assert plots.colors([1, n + 1, 2]) == [second, first, third]
    assert len(set(plots.colors(range(3 * n)))) == 3 * n


def test_plotting_and_saving_chosen_rows_reads_their_records_only(
    client: Client, sample: Measure, definitions: DefinitionStore
) -> None:
    counting = CountingClient(client)
    manager = BatchManager(counting, definitions=definitions)  # type: ignore[arg-type]
    for run in (1, 2, 3):
        sample(run, f's{run}')
    label = manager.create(STAGED.name, title='Scan', table=toys.rows(client)).label
    pages = Pages(Intake(manager, sources=SOURCES), draw={STAGED.name: draw})
    page = opened(pages, label)
    reduced(page, client, 'all')
    chosen = {'keys': ['1']}

    def reads(call: Callable[[], object]) -> int:
        counting.reads.clear()
        call()
        return counting.reads['status']

    plot = reads(lambda: page.post('/api/plot', json=chosen))
    saved = reads(lambda: page.post('/api/results.zip', json=chosen))

    # the zip also asks the status of the record it exports
    assert (plot, saved) == (1, 2)


def test_a_row_shows_which_settings_its_result_was_reduced_with(
    http: TestClient, client: Client
) -> None:
    reduced(http, client)
    bins(http, 5)

    detail = http.get('/api/row', params={'key': '1'}).json()
    assert detail['differs'] == ['binning']


def test_the_settings_of_a_row_fill_the_form_until_apply(
    http: TestClient, client: Client, batch: Batch
) -> None:
    reduced(http, client)
    bins(http, 5)

    used = http.post('/api/row/use-settings', json={'key': '1'}).json()
    applied = http.post('/api/settings/apply').json()

    assert used['notice'] == (
        'The form shows the settings water was reduced with. Apply to use them.'
    )
    assert (form_bins(used), used['settings_unapplied']) == (2, True)
    assert used['rows'][0]['status'] == 'settings changed'
    assert batch.settings['binning'] == {'bins': 2}
    assert applied['rows'][0]['status'] == 'done'


@pytest.mark.parametrize('keep', [True, False], ids=['reduce them again', 'back'])
def test_settings_tried_on_one_row_reach_the_others_only_when_a_person_says_so(
    http: TestClient, client: Client, keep: bool
) -> None:
    reduced(http, client)
    bins(http, 4)
    http.post('/api/reduce', json={'which': 'selected', 'keys': ['1']})
    client.wait(client.records())

    def settings_changed() -> list[str]:
        return [k for k, r in rows(http).items() if r['status'] == 'settings changed']

    def binning() -> list[int]:
        return [
            client.latest('scan', k).request.params['binning']['bins'] for k in '12'
        ]

    assert (settings_changed(), binning()) == (['2'], [4, 2])
    assert http.get('/api/row', params={'key': '1'}).json()['differs'] == []
    if keep:
        reduced(http, client, which='settings changed')
        assert (settings_changed(), binning()) == ([], [4, 4])
    else:
        bins(http, 2)
        assert (settings_changed(), binning()) == (['1'], [4, 2])


def test_a_row_added_on_the_page_is_empty_and_rows_removed_are_gone(
    http: TestClient, batch: Batch
) -> None:
    added = http.post('/api/rows/add-empty', json={'key': 'extra'}).json()
    assert (added['rows'][-1]['name'], added['rows'][-1]['status']) == (
        'extra',
        'incomplete',
    )
    # A key may be the name of another row; the batch refuses a key it has.
    named = http.post('/api/rows/add-empty', json={'key': 'water'}).json()
    assert [r['key'] for r in named['rows']] == ['1', '2', '3', 'extra', 'water']
    same = http.post('/api/rows/add-empty', json={'key': 'water'})
    assert same.status_code == 422
    http.post('/api/rows/remove', json={'keys': ['water']})

    after = http.post('/api/rows/remove', json={'keys': ['extra', '2']}).json()

    assert after['notice'] == 'Removed 2 rows.'
    assert list(batch.table().index) == ['1', '3']


def available(http: TestClient) -> dict[str, str]:
    """The rows that "Add rows from the runs" lists, by key: the mark of each."""
    return {r['key']: r['mark'] for r in http.get('/api/rows/available').json()['rows']}


def test_the_available_rows_are_those_of_the_source_the_table_lacks(
    http: TestClient, sample: Measure
) -> None:
    sample(4, 'ethanol')
    sample(5, 'acetone', partner=False)

    listed = http.get('/api/rows/available').json()

    assert [(r['key'], r['name'], r['mark']) for r in listed['rows']] == [
        ('4', 'ethanol', ''),
        ('5', 'acetone', 'incomplete'),
    ]


def test_rows_are_added_as_the_source_makes_them(
    http: TestClient, sample: Measure
) -> None:
    sample(4, 'ethanol')

    added = http.post('/api/rows/add', json={'keys': ['4']}).json()

    assert added['notice'] == 'Added 1 row.'
    assert [r['key'] for r in added['rows']] == ['1', '2', '3', '4']
    assert added['rows'][-1]['cells']['sample']['text'] == 'run 4'
    assert available(http) == {}


def test_a_row_added_then_removed_is_available_again_and_follow_does_not_add_it(
    http: TestClient, pages: Pages, intake: Intake, sample: Measure
) -> None:
    label = toys.from_pairs(intake, STAGED.name, title='Later', follow=True)
    following = opened(pages, label)
    sample(4, 'ethanol')
    following.post('/api/rows/add', json={'keys': ['4']})
    following.post('/api/rows/remove', json={'keys': ['4']})
    sample(5, 'acetone')

    pages.tick()

    assert list(rows(following)) == ['1', '2', '5']
    assert available(following) == {'3': 'incomplete', '4': ''}


def test_rows_the_manager_does_not_take_answer_422_and_none_is_added(
    http: TestClient, batch: Batch
) -> None:
    answer = http.post('/api/rows/add', json={'keys': ['1']})

    assert answer.status_code == 422
    assert (
        answer.json()['message']
        == 'No row was added: rows [\'1\'] are in the batch already.'
    )
    assert list(batch.table().index) == ['1', '2', '3']


def failing(error: Exception) -> Callable[[Any], Any]:
    """A ``list`` of the file source that raises ``error``."""

    def listing(selector: Any) -> Any:
        raise error

    return listing


def test_a_file_source_that_cannot_list_is_said_on_each_call_that_runs_a_source(
    http: TestClient, fake: FakeFiles
) -> None:
    fake.list = failing(OSError('the mount went away'))

    answers = [
        http.get('/api/rows/available'),
        http.post('/api/rows/add', json={'keys': ['4']}),
        http.get('/api/sources/rows', params={'name': 'pairs'}),
        http.post('/api/batches', json=new_batch()),
    ]

    assert [(a.status_code, a.json()['message']) for a in answers] == [
        (422, 'The rows of the source cannot be listed: the mount went away.'),
        (422, 'No row was added: the mount went away.'),
        (422, 'The mount went away.'),
        (422, 'The batch was not made: the mount went away.'),
    ]


def test_a_defect_of_a_source_is_said_with_its_type_and_the_page_works_on(
    http: TestClient, fake: FakeFiles
) -> None:
    fake.list = failing(KeyError('run'))

    answers = [
        http.get('/api/rows/available'),
        http.post('/api/rows/add', json={'keys': ['4']}),
        http.get('/api/sources/rows', params={'name': 'pairs'}),
        http.post('/api/batches', json=new_batch()),
    ]

    assert [(a.status_code, a.json()['message']) for a in answers] == [
        (422, "The rows of the source cannot be listed: KeyError: 'run'."),
        (422, "No row was added: KeyError: 'run'."),
        (422, "KeyError: 'run'."),
        (422, "The batch was not made: KeyError: 'run'."),
    ]
    assert http.get('/api/state').status_code == 200


def test_metadata_that_the_code_of_the_application_refuses_is_said_at_its_fields(
    http: TestClient, fake: FakeFiles
) -> None:
    fake.list = failing(toys.refused_metadata())

    answers = [
        http.get('/api/rows/available'),
        http.get('/api/sources/rows', params={'name': 'pairs'}),
        http.post('/api/batches', json=new_batch()),
    ]

    said = toys.REFUSED_METADATA
    assert [(a.status_code, a.json()['message']) for a in answers] == [
        (422, f'The rows of the source cannot be listed: the source pairs: {said}.'),
        (422, f'{said[:1].upper()}{said[1:]}.'),
        (422, f'The batch was not made: {said}.'),
    ]


def test_a_source_that_no_longer_gives_a_column_of_the_batch_is_said(
    pages: Pages, label: str, client: Client, definitions: DefinitionStore
) -> None:
    sources = {'pairs': toys.SAMPLES}  # its code now fills sample only
    again = Pages(
        Intake(BatchManager(client, definitions=definitions), sources=sources)
    )

    answer = opened(again, label).get('/api/rows/available')

    assert (answer.status_code, answer.json()['message']) == (
        422,
        'The rows of the source cannot be listed: the source pairs: it no '
        'longer gives the column transmission that the batch stores as a source '
        'column.',
    )


# Columns and settings


def make_setting(http: TestClient, key: str | None = None) -> Any:
    """Make the column thickness a setting, of the value of row ``key``."""
    return http.post('/api/columns/setting', json={'name': 'thickness', 'key': key})


def test_a_column_whose_rows_agree_is_made_a_setting_of_their_value(
    http: TestClient, batch: Batch
) -> None:
    thickness(http, ['1', '2', '3'], 2.0)

    made = make_setting(http).json()

    assert made['notice'] == 'Thickness is a setting: one value for every row.'
    assert batch.parameter_columns == ('sample', 'transmission')
    assert batch.settings['thickness'] == 2.0
    assert [c['name'] for c in made['columns']] == ['sample', 'transmission']
    (shown,) = [n for n in made['settings'] if n['path'] == ['thickness']]
    assert shown['value'] == 2.0
    assert made['may_be_columns'] == ['thickness']


def test_the_values_of_a_column_are_listed_each_with_a_row_that_has_it(
    http: TestClient,
) -> None:
    thickness(http, ['2'], 3.0)

    listed = http.get('/api/columns/values', params={'name': 'thickness'}).json()

    # An empty cell takes the default.
    assert listed == [{'key': '1', 'text': '1.0'}, {'key': '2', 'text': '3.0'}]


def test_a_column_made_a_setting_whose_table_is_refused_keeps_its_cells(
    http: TestClient, client: Client, refusing: Refusing, definitions: DefinitionStore
) -> None:
    thickness(http, ['1', '2', '3'], 2.5)
    refusing.part = 'table'

    answer = make_setting(http)

    assert answer.status_code == 422
    batch = started(client, definitions).intake.manager.batch('scan')
    assert batch.parameter_columns == ('sample', 'transmission', 'thickness')
    assert batch.table()['thickness'].tolist() == [2.5, 2.5, 2.5]


def test_a_column_whose_rows_differ_is_made_a_setting_of_the_row_chosen(
    http: TestClient, batch: Batch
) -> None:
    thickness(http, ['1'], 2.0)
    thickness(http, ['2'], 3.0)

    refused = make_setting(http)
    make_setting(http, '2')

    assert refused.status_code == 422
    assert refused.json()['message'] == (
        'Nothing was changed: the rows differ at thickness: choose the value to keep.'
    )
    assert batch.settings['thickness'] == 3.0


def test_a_setting_made_a_column_is_shown_in_the_form_and_in_each_empty_cell(
    http: TestClient, batch: Batch
) -> None:
    before = column_of(state(http), 'thickness')
    make_setting(http)
    http.post('/api/settings', json={'path': ['thickness'], 'op': 'set', 'value': 2.5})
    http.post('/api/settings/apply')

    made = http.post('/api/settings/column', json={'name': 'thickness'}).json()

    assert made['notice'] == (
        'Thickness is a column: a row takes the setting until its cell is set.'
    )
    assert batch.parameter_columns == ('sample', 'transmission', 'thickness')
    assert batch.table()['thickness'].isna().all()
    assert batch.settings['thickness'] == 2.5
    assert [r['cells']['thickness']['text'] for r in made['rows']] == [''] * 3
    # The grid shows an empty cell in grey as the value it takes.
    assert (before['default'], column_of(made, 'thickness')['default']) == (
        '1.0',
        '2.5',
    )
    (shown,) = [n for n in made['settings'] if n['path'] == ['thickness']]
    assert shown['value'] == 2.5
    assert made['may_be_columns'] == []


def column_of(shown: dict[str, Any], name: str) -> dict[str, Any]:
    """The column ``name`` of the grid of the rows in the state ``shown``."""
    (column,) = [c for c in shown['columns'] if c['name'] == name]
    return column


def test_a_group_setting_made_a_column_and_a_setting_again_keeps_its_value(
    http: TestClient, batch: Batch
) -> None:
    bins(http, 5)

    column = http.post('/api/settings/column', json={'name': 'binning'}).json()
    cells = batch.table()['binning'].tolist()
    setting = http.post('/api/columns/setting', json={'name': 'binning'}).json()

    assert [c['name'] for c in column['columns']][-1] == 'binning'
    assert cells == [None] * 3
    assert 'binning' not in batch.parameter_columns
    assert form_bins(setting) == 5


@pytest.mark.parametrize('way', ['make a column', 'make a setting', 'load a table'])
def test_columns_do_not_change_while_the_form_holds_a_change(
    http: TestClient, batch: Batch, way: str
) -> None:
    draft(http, 9)
    table = b'key,sample,transmission,name\na,run:1,run:1001,first\n'

    if way == 'make a column':
        answer = http.post('/api/settings/column', json={'name': 'binning'})
    elif way == 'make a setting':
        answer = http.post('/api/columns/setting', json={'name': 'thickness'})
    else:
        answer = http.post('/api/table', params={'name': 'rows.csv'}, content=table)

    assert answer.status_code == 422
    assert answer.json()['message'] == (
        'Nothing was changed: apply or discard the settings first.'
    )
    assert batch.parameter_columns == ('sample', 'transmission', 'thickness')
    assert list(batch.table().index) == ['1', '2', '3']
    assert form_bins(state(http)) == 9


def test_a_correction_of_a_run_changes_no_row_and_can_be_taken_back(
    http: TestClient, fake: FakeFiles
) -> None:
    key = fake.measure(4, 1.0, role='sample', sample='oil').file
    http.post('/api/relist')
    runs = state(http)['runs']
    before = state(http)['rows']

    edit = {'keys': [key], 'column': 'role', 'value': 'transmission'}
    http.post('/api/runs/cells', json=edit)

    assert state(http)['rows'] == before
    (cell,) = [
        r['cells']['role']
        for r in http.get('/api/runs').json()['rows']
        if r['key'] == key
    ]
    assert (cell['text'], cell['edited'], cell['original']) == (
        'transmission',
        True,
        'sample',
    )
    assert state(http)['runs'] > runs
    reverted = http.post(
        '/api/runs/cells/revert', json={'keys': [key], 'column': 'role'}
    ).json()
    assert reverted == {'notice': 'Role set back on 1 run.'}


# Forms, on a spec with a list and an optional group


class Range(BaseModel):
    start: float = Field(default=1.0, title='Start (Å)')
    banks: list[int] = [1, 2]


class Settings(BaseModel):
    range: Range = Field(default_factory=Range)
    offset: Range | None = None
    sample: SampleRunFile


FORMS = WorkflowSpec(
    name='forms',
    version=1,
    title='Forms',
    description='A toy of forms.',
    params=Settings,
    outputs=BaseModel,
)


@pytest.fixture
def form(files: CorrectedFiles, definitions: DefinitionStore) -> Iterator[TestClient]:
    bind = [function_binding(FORMS, lambda _: BaseModel())]
    with local(proposal='p1', files=files, bind=bind) as client:
        pages = started(client, definitions)
        rows = pd.DataFrame(columns=['sample'])
        label = pages.intake.manager.create(FORMS.name, title='Forms', table=rows).label
        yield opened(pages, label)


def test_a_correction_whose_runs_cannot_be_listed_says_that_it_was_kept(
    http: TestClient, files: CorrectedFiles, fake: FakeFiles
) -> None:
    state(http)  # the page is made while the files can be listed
    (run, *_) = http.get('/api/runs').json()['rows']
    fake.list = lambda selector: (_ for _ in ()).throw(OSError('half written'))

    edit = {'keys': [run['key']], 'column': 'role', 'value': 'sample'}
    answer = http.post('/api/runs/cells', json=edit)

    assert dict(files.corrections(FileRef(file=run['key']))) == {'role': 'sample'}
    assert answer.status_code == 422
    assert answer.json()['message'] == (
        'Corrected, but the runs cannot be listed: half written'
    )


def settings(form: TestClient, edit: dict[str, Any]) -> dict[str, Any]:
    response = form.post('/api/settings', json=edit)
    assert response.status_code == 200, response.json()
    return {tuple(n['path']): n for n in response.json()['settings']}


def test_a_form_set_writes_an_input_at_the_path_of_its_errors(form: TestClient) -> None:
    nodes = settings(form, {'path': ['range', 'start'], 'op': 'set', 'value': 2})

    (start, _) = nodes['range',]['child']['children']
    assert (start['path'], start['value']) == (['range', 'start'], 2.0)


def test_a_form_choose_picks_an_alternative_of_a_choice(form: TestClient) -> None:
    nodes = settings(form, {'path': ['offset'], 'op': 'choose', 'value': 'Given'})

    offset = nodes['offset',]
    assert offset['chosen'] == 'Given'
    assert [w['label'] for w in offset['alternatives']] == ['Given', 'Not used']
    assert offset['child']['path'] == ['offset']


@pytest.mark.parametrize(('op', 'banks'), [('add', [1, 2, None]), ('remove', [1])])
def test_a_form_adds_and_removes_elements_of_a_list(
    form: TestClient, op: str, banks: list[int | None]
) -> None:
    nodes = settings(form, {'path': ['range', 'banks'], 'op': op})

    (_, elements) = nodes['range',]['child']['children']
    assert [e['value'] for e in elements['elements']] == banks


def test_a_form_edit_at_no_field_is_refused(form: TestClient) -> None:
    edit = {'path': ['range', 'nothing'], 'op': 'set', 'value': 1}

    response = form.post('/api/settings', json=edit)

    assert response.status_code == 422
    assert response.json() == {'message': "no setting at ['range', 'nothing']"}


# The plot


def test_a_figure_has_error_bars_axes_with_units_and_no_nan() -> None:
    curve = sc.DataArray(
        sc.array(dims=['Q'], values=[4.0, np.nan], variances=[4.0, 1.0], unit='1/cm'),
        coords={'Q': sc.array(dims=['Q'], values=[0.1, 0.2, 0.3], unit='1/angstrom')},
    )

    figure = plots.figure([plots.Curve('water', curve, '#000')], quantity='I(Q)')

    (trace,) = figure['data']
    assert trace['name'] == 'water'
    assert np.allclose(trace['x'], [0.15, 0.25])
    assert trace['y'] == [4.0, None]
    assert trace['error_y']['array'] == [2.0, 1.0]
    assert figure['layout']['xaxis']['title']['text'] == 'Q [1/Å]'
    assert figure['layout']['yaxis']['title']['text'] == 'I(Q) [1/cm]'


def test_the_demo_measures_its_samples_again_under_numbered_names() -> None:
    groups = demo.measurements(len(demo.SAMPLES) + 2)

    runs = [run for group in groups for run in group]
    samples = [sample for _, _, role, sample in runs if role == 'sample']
    assert samples[-2:] == ['H2O buffer #2', 'D2O buffer #2']
    assert len({run for run, *_ in runs}) == len(runs)


def test_the_demo_makes_a_batch_of_its_samples_and_follow_adds_those_that_arrive(
    tmp_path: Path,
) -> None:
    groups = demo.measurements()
    demo.measure(tmp_path, groups[0] + groups[1])
    client, files, definitions = demo.open_demo(tmp_path)

    with client:
        app = demo.build(client, files, definitions)
        page = home(app)
        first = state(page)
        columns = ['sample', 'transmission', 'thickness']
        made = page.post(
            '/api/batches',
            json={
                'title': 'Arriving',
                'source': 'sample-runs',
                'spec': demo.DEMO.name,
                'parameter_columns': columns,
                'follow': True,
            },
        ).json()
        demo.measure(tmp_path, groups[0] + groups[1] + groups[2])
        app.state.pages.tick()
        client.wait(client.records())
        after = state(opened(app.state.pages, made['label']))

    assert [row['name'] for row in first['rows']] == ['H2O buffer']
    assert first['follow']['on'] is False
    assert [row['name'] for row in after['rows']] == ['H2O buffer', 'D2O buffer']
    assert [c['name'] for c in after['columns']] == columns


def test_the_page_and_the_files_it_names_are_served_under_a_path_prefix(
    pages: Pages, label: str
) -> None:
    parent = FastAPI()
    parent.mount('/batch', create_app(pages))
    under = TestClient(parent)

    page = under.get('/batch/')
    names = re.findall(r'(?:src|href)="([^"]+)"', page.text)
    local = [n for n in names if not n.startswith('data:') and 'vendor' not in n]

    assert page.url == 'http://testserver/batch/batches/scan/'
    assert local
    for name in local:
        assert not name.startswith(('/', 'http')), name
        assert under.get(urljoin(str(page.url), name)).status_code == 200, name
    assert under.get(urljoin(str(page.url), 'api/state')).status_code == 200


def test_a_large_answer_is_compressed_for_a_client_that_accepts_gzip(
    http: TestClient, sample: Measure, batch: Batch, client: Client
) -> None:
    for run in range(10, 200):
        sample(run, f'sample {run}')
    batch.set_table(toys.rows(client))

    plain = http.get('/api/state', headers={'Accept-Encoding': 'identity'})
    packed = http.get('/api/state', headers={'Accept-Encoding': 'gzip'})

    assert 'content-encoding' not in plain.headers
    assert packed.headers['content-encoding'] == 'gzip'
    assert int(packed.headers['content-length']) < len(plain.content) / 3
    assert packed.json() == plain.json()


# Batches by URL


def test_each_batch_has_its_page_at_its_label(
    http: TestClient, manager: BatchManager, pages: Pages, client: Client
) -> None:
    other = manager.create(STAGED.name, title='Other', table=toys.rows(client)).label
    page = opened(pages, other)

    bins(page, 5)
    thickness(page, ['1'], 3)

    mine, theirs = state(http), state(page)
    assert (mine['label'], mine['title']) == ('scan', 'Scan')
    assert (theirs['label'], theirs['title']) == (other, 'Other')
    assert (form_bins(mine), form_bins(theirs)) == (2, 5)
    assert rows(http)['1']['cells']['thickness']['text'] == ''
    assert rows(page)['1']['cells']['thickness']['text'] == '3.0'


def test_the_root_redirects_to_the_batch_that_follows(
    pages: Pages, intake: Intake
) -> None:
    http = TestClient(create_app(pages), follow_redirects=False)
    for label in ('a', 'b'):
        intake.create(
            STAGED.name, title=label, source='pairs', label=label, follow=label == 'b'
        )

    page = http.get('/')

    assert (page.status_code, page.headers['location']) == (307, 'batches/b/')


def test_the_root_redirects_to_the_only_batch(pages: Pages, intake: Intake) -> None:
    http = TestClient(create_app(pages), follow_redirects=False)
    intake.create(STAGED.name, title='a', source='pairs', label='a')

    page = http.get('/')

    assert (page.status_code, page.headers['location']) == (307, 'batches/a/')


def test_the_root_lists_several_batches_if_none_follows(
    pages: Pages, intake: Intake
) -> None:
    http = TestClient(create_app(pages), follow_redirects=False)
    for label in ('a', 'b'):
        intake.create(STAGED.name, title=label, source='pairs', label=label)

    page = http.get('/')

    assert page.status_code == 200
    assert page.headers['content-type'].startswith('text/html')


def test_a_label_that_is_no_batch_answers_404_and_its_page_says_so(
    pages: Pages, manager: BatchManager, label: str
) -> None:
    http = TestClient(create_app(pages))
    page = opened(pages, label)
    state(page)

    unknown = http.get('/batches/none/api/state')
    action = http.post('/batches/none/api/relist')
    manager.remove(label)

    # The page is served, and shows the batches with the message of its state.
    assert http.get('/batches/none/').status_code == 200
    assert unknown.json() == {'message': 'There is no batch none.'}
    assert (action.status_code, action.json()) == (404, unknown.json())
    assert page.get('/api/state').status_code == 404


# The list of batches


def test_the_batches_view_lists_every_batch_and_the_one_that_follows(
    http: TestClient, intake: Intake
) -> None:
    intake.create(
        STAGED.name,
        title='Other',
        source='pairs',
        description='The second series',
        label='other',
        follow=True,
    )

    listed = http.get('/api/batches').json()

    assert listed['rows'] == [
        {
            'label': 'other',
            'title': 'Other',
            'description': 'The second series',
            'follows': True,
        },
        {'label': 'scan', 'title': 'Scan', 'description': '', 'follows': False},
    ]


def test_the_list_of_batches_is_the_same_from_every_page(
    http: TestClient, pages: Pages, intake: Intake
) -> None:
    other = intake.create(STAGED.name, title='Other', source='pairs').label

    listed = http.get('/api/batches').json()

    assert opened(pages, other).get('/api/batches').json() == listed
    assert TestClient(create_app(pages)).get('/api/batches').json() == listed


def test_a_page_learns_from_its_poll_that_another_batch_follows(
    http: TestClient,
) -> None:
    now = state(http)

    http.post('/api/batches', json=new_batch(title='Other', follow=True))

    polled = http.get(
        '/api/state', params={'after': now['revision'], 'start': now['start']}
    )
    assert polled.status_code == 200
    listed = polled.json()['batches']
    assert [(b['title'], b['follows']) for b in listed] == [
        ('Other', True),
        ('Scan', False),
    ]
    assert http.get('/api/batches').json()['rows'] == listed


def test_a_correction_on_one_page_shows_in_the_runs_view_of_another(
    http: TestClient, pages: Pages, intake: Intake, fake: FakeFiles
) -> None:
    key = fake.measure(4, 1.0, role='sample', sample='oil').file
    other = opened(
        pages, intake.create(STAGED.name, title='Other', source='pairs').label
    )
    runs = state(other)['runs']

    edit = {'keys': [key], 'column': 'role', 'value': 'transmission'}
    http.post('/api/runs/cells', json=edit)

    assert state(other)['runs'] > runs
    shown = {
        r['key']: r['cells']['role'] for r in other.get('/api/runs').json()['rows']
    }
    assert shown[key]['text'] == 'transmission'


def test_counts_are_given_in_the_words_of_the_page(
    http: TestClient, batch: Batch, client: Client
) -> None:
    client.wait(batch.reduce(['1']))
    bins(http, 3)  # row 1 has status settings changed

    counted = http.get('/api/batches/counts').json()

    assert counted['rows'] == {
        'scan': {'settings changed': 1, 'ready': 1, 'incomplete': 1}
    }
    assert counted['failed'] == {}


def counted(http: TestClient) -> dict[str, int]:
    """The counts of the batch ``scan``."""
    return http.get('/api/batches/counts').json()['rows']['scan']


def test_a_count_after_a_row_finished_gives_its_new_status(
    http: TestClient, batch: Batch, client: Client, gate: Gate
) -> None:
    gate.close()
    records = batch.reduce(['1'])
    running = counted(http)
    gate.open()
    client.wait(records)

    assert running == {'running': 1, 'ready': 1, 'incomplete': 1}
    assert counted(http) == {'done': 1, 'ready': 1, 'incomplete': 1}


def test_a_count_after_a_file_arrived_that_a_row_names_gives_its_new_status(
    http: TestClient, batch: Batch, fake: FakeFiles
) -> None:
    toys.edit(batch, '3', transmission='uuid:run-1003')
    before = counted(http)

    fake.measure(1003, 1.0, role='transmission', sample='oil')

    assert before == {'ready': 2, 'incomplete': 1}
    assert counted(http) == {'ready': 3}


def test_the_counts_give_the_rows_of_each_batch_by_status(
    pages: Pages, manager: BatchManager, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    sample(2, 'oil', partner=False)
    manager.create('plain', title='Morning', table=toys.rows(client), label='a')
    manager.create('plain', title='Afternoon', table=toys.rows(client), label='b')
    client.wait(manager.batch('a').reduce(['1']))

    counted = pages.counts()

    assert counted.rows == {
        'a': {'done': 1, 'incomplete': 1},
        'b': {'ready': 1, 'incomplete': 1},
    }
    assert counted.failed == {}


def test_a_batch_that_cannot_be_counted_has_its_reason(
    client: Client, definitions: FolderDefinitions, sample: Measure
) -> None:
    sample(1, 'water')
    first = BatchManager(client, definitions=definitions)
    first.create('plain', title='Fine', table=toys.rows(client), label='a')
    first.create('plain', title='Broken', table=toys.rows(client), label='b')
    definitions.save(TABLE.key('b'), {'nonsense': 1})

    counted = restarted(client, definitions).counts()

    assert list(counted.rows) == list(counted.files) == ['a']
    assert 'table.json: not a table' in counted.failed['b']


# The batches of each run


def named(http: TestClient) -> dict[str, list[str]]:
    """The identities of the files that each batch names, by label."""
    return http.get('/api/batches/counts').json()['files']


def test_the_counts_name_the_runs_of_each_batch_also_a_run_two_share(
    http: TestClient, manager: BatchManager, client: Client
) -> None:
    manager.create(
        'plain', title='Water', table=toys.rows(client).loc[['1']], label='w'
    )

    assert named(http) == {
        'scan': [
            'uuid:run-1',
            'uuid:run-1001',
            'uuid:run-1002',
            'uuid:run-2',
            'uuid:run-3',
        ],
        'w': ['uuid:run-1', 'uuid:run-1001'],
    }


def test_a_row_that_follow_adds_names_its_runs_in_the_next_count(
    pages: Pages, intake: Intake, sample: Measure
) -> None:
    label = toys.from_pairs(intake, STAGED.name, title='Scan', follow=True)
    page = TestClient(create_app(pages))
    before = named(page)[label]
    sample(1, 'water')

    pages.tick()

    assert (before, named(page)[label]) == ([], ['uuid:run-1', 'uuid:run-1001'])


def test_a_run_named_otherwise_than_the_runs_view_lists_it_is_matched(
    http: TestClient, batch: Batch, fake: FakeFiles
) -> None:
    fake.measure(7, 1.0, role='sample', sample='oil')
    toys.edit(batch, '3', transmission='run:7')

    listed = {r['key'] for r in http.get('/api/runs').json()['rows']}

    assert 'uuid:run-7' in listed
    assert 'uuid:run-7' in named(http)['scan']


# Making a batch

STAGED_PARAMS = [
    {'name': 'sample', 'title': 'Sample'},
    {'name': 'transmission', 'title': 'Transmission'},
    {'name': 'thickness', 'title': 'Thickness'},
]
"""The parameters of the spec offered as columns: binning and offset are groups."""


def new_batch(**given: Any) -> dict[str, Any]:
    """What the form "New batch" sends for a batch of the rows of ``pairs``."""
    return {
        'title': 'Afternoon',
        'source': 'pairs',
        'spec': STAGED.name,
        'parameter_columns': ['sample', 'transmission'],
    } | given


def test_the_sources_offer_the_specs_that_fit_and_the_parameters_a_cell_edits(
    pages: Pages,
) -> None:
    pairs, none = TestClient(create_app(pages)).get('/api/sources').json()

    assert pairs == {
        'name': 'pairs',
        'source_columns': ['sample', 'transmission'],
        'specs': [
            {'name': 'staged', 'title': 'Staged', 'params': STAGED_PARAMS},
            {'name': 'plain', 'title': 'Plain', 'params': STAGED_PARAMS},
        ],
    }
    assert (none['name'], none['source_columns']) == (None, [])
    assert [s['name'] for s in none['specs']] == [
        'staged',
        'plain',
        'find',
        'series',
        'repeated',
    ]
    assert [p['name'] for p in none['specs'][0]['params']] == [
        'sample',
        'transmission',
        'thickness',
    ]


def test_a_source_that_no_spec_fits_is_not_offered_and_no_source_always_is(
    client: Client, definitions: DefinitionStore
) -> None:
    odd = RowSource(pair, source_columns=('sample', 'background'))
    manager = BatchManager(client, definitions=definitions)

    pages = Pages(Intake(manager, sources={'odd': odd}))
    offered = TestClient(create_app(pages)).get('/api/sources').json()

    assert [s['name'] for s in offered] == [None]


def test_a_batch_made_from_a_source_has_every_complete_row_and_the_columns_sent(
    http: TestClient, pages: Pages, manager: BatchManager
) -> None:
    sent = new_batch(
        description='Thicker cells',
        parameter_columns=['sample', 'transmission', 'thickness'],
    )

    made = http.post('/api/batches', json=sent).json()

    assert manager.headers()[made['label']] == Header('Afternoon', 'Thicker cells')
    page = state(opened(pages, made['label']))
    assert [r['key'] for r in page['rows']] == ['1', '2']  # 3 has no transmission
    assert [c['name'] for c in page['columns']] == [
        'sample',
        'transmission',
        'thickness',
    ]
    listed = {b['label']: b['title'] for b in state(http)['batches']}
    assert listed == {'scan': 'Scan', made['label']: 'Afternoon'}


def test_a_batch_made_to_follow_follows_and_one_without_a_source_cannot(
    http: TestClient, intake: Intake
) -> None:
    made = http.post('/api/batches', json=new_batch(follow=True)).json()
    refused = http.post('/api/batches', json=new_batch(source=None, follow=True))

    assert intake.following == made['label']
    assert refused.status_code == 422
    [error] = refused.json()['detail']
    assert error['msg'].endswith('a batch follows its source, and none is given')


def test_stop_following_answers_the_page_of_a_batch_that_follows_no_more(
    pages: Pages, intake: Intake
) -> None:
    label = toys.from_pairs(intake, STAGED.name, title='F', follow=True)

    stopped = opened(pages, label).post('/api/follow/stop').json()

    assert (stopped['notice'], stopped['follow']['on']) == ('Stopped following.', False)
    assert intake.following is None


def test_a_batch_made_from_the_page_starts_with_the_settings_of_its_spec(
    client: Client, definitions: DefinitionStore
) -> None:
    settings = {STAGED.name: {'binning': {'bins': 5}}}
    manager = BatchManager(client, definitions=definitions, settings=settings)
    pages = Pages(Intake(manager, sources=SOURCES))

    made = TestClient(create_app(pages)).post('/api/batches', json=new_batch())

    assert form_bins(state(opened(pages, made.json()['label']))) == 5


def test_a_batch_without_a_source_is_made_with_the_columns_sent_and_no_rows(
    pages: Pages, manager: BatchManager, sample: Measure
) -> None:
    sample(1, 'water')
    sent = new_batch(source=None, spec='find', parameter_columns=['sample'])

    made = TestClient(create_app(pages)).post('/api/batches', json=sent).json()

    batch = manager.batch(made['label'])
    assert (batch.spec.name, batch.parameter_columns, len(batch.table())) == (
        'find',
        ('sample',),
        0,
    )


def source_rows(http: TestClient, **ends: str) -> Any:
    """The rows of the source ``pairs``, marked for a range of ``ends``."""
    return http.get('/api/sources/rows', params={'name': 'pairs', **ends})


def test_the_rows_of_a_source_are_marked_as_a_range_takes_them(
    http: TestClient, sample: Measure
) -> None:
    sample(4, 'ethanol')

    listed = source_rows(http, first='2').json()

    assert [(r['key'], r['name'], r['mark']) for r in listed['rows']] == [
        ('1', 'water', 'left out'),
        ('2', 'sds', 'taken'),
        ('3', 'oil', 'incomplete'),
        ('4', 'ethanol', 'taken'),
    ]
    assert [c['name'] for c in listed['columns']] == ['run']
    assert listed['rows'][0]['cells'] == {'run': '1'}


@pytest.mark.parametrize(
    ('ends', 'said'),
    [
        ({'first': '3'}, '3 is not a complete row'),
        ({'first': '2', 'last': '1'}, 'the row 2 comes after the row 1'),
    ],
    ids=['incomplete', 'reversed'],
)
def test_a_range_that_takes_no_row_of_its_ends_makes_no_batch(
    http: TestClient, manager: BatchManager, ends: dict[str, str], said: str
) -> None:
    answer = http.post('/api/batches', json=new_batch(**ends))
    listing = source_rows(http, **ends)

    assert answer.status_code == 422
    assert answer.json()['message'] == f'The batch was not made: {said}.'
    assert (listing.status_code, listing.json()['message']) == (
        422,
        f'{said[:1].upper()}{said[1:]}.',
    )
    assert list(manager.headers()) == ['scan']


def test_a_batch_that_follows_with_a_last_row_is_not_made(
    http: TestClient, manager: BatchManager, intake: Intake
) -> None:
    answer = http.post('/api/batches', json=new_batch(last='2', follow=True))

    assert (answer.status_code, answer.json()['message']) == (
        422,
        'The batch was not made: a batch that follows takes every row that '
        'arrives later, so it has no last row.',
    )
    assert (list(manager.headers()), intake.following) == (['scan'], None)


def test_a_batch_of_the_rows_that_arrive_later_takes_none_now_and_follow_adds_them(
    http: TestClient,
    pages: Pages,
    manager: BatchManager,
    sample: Measure,
    fake: FakeFiles,
) -> None:
    listed = source_rows(http, later='true').json()['rows']

    made = http.post('/api/batches', json=new_batch(later=True, follow=True)).json()
    empty = list(manager.batch(made['label']).table().index)
    sample(4, 'ethanol')
    fake.measure(1003, 1.0, role='transmission', sample='oil')  # 3 waited
    pages.tick()

    assert [(r['key'], r['mark']) for r in listed] == [
        ('1', 'left out'),
        ('2', 'left out'),
        ('3', 'incomplete'),
    ]
    assert empty == []
    assert list(manager.batch(made['label']).table().index) == ['3', '4']


class RefusingAt:
    """Definitions that refuse the ``at``-th save, and keep every other write."""

    def __init__(self, definitions: DefinitionStore, at: int) -> None:
        self._definitions = definitions
        self.at = at
        self.saves = 0

    def load(self, key: Key, read: Callable[[Any], Any]) -> Any:
        return self._definitions.load(key, read)

    def save(self, key: Key, data: Any) -> None:
        self.saves += 1
        if self.saves == self.at:
            raise DefinitionWriteError(f'{key}: refused')
        self._definitions.save(key, data)

    def delete(self, key: Key) -> None:
        self._definitions.delete(key)

    def labels(self) -> list[str]:
        return self._definitions.labels()


def test_a_batch_made_on_the_page_is_made_as_asked_or_not_at_all(
    client: Client, sample: Measure, tmp_path: Path
) -> None:
    sample(1, 'water')
    new = NewBatch(
        title='New',
        spec=STAGED.name,
        source='pairs',
        parameter_columns=['sample', 'transmission', 'thickness'],
        settings_from='old',
        follow=True,
    )
    for at in itertools.count(1):
        definitions = FolderDefinitions(tmp_path / str(at))
        Intake(BatchManager(client, definitions=definitions), sources=SOURCES).create(
            STAGED.name,
            title='Old',
            source='pairs',
            settings={'thickness': 2.0},
            label='old',
            follow=True,
        )
        refusing = RefusingAt(definitions, at)
        pages = Pages(
            Intake(BatchManager(client, definitions=refusing), sources=SOURCES)
        )
        try:
            made = pages.create(new)
        except Rejected:
            manager = pages.intake.manager
            assert (list(manager.headers()), pages.intake.following) == (
                ['old'],
                'old',
            )
        else:
            break

    batch = pages.intake.manager.batch(made.label)
    assert at > 4  # settings, rows, header, source, following
    assert batch.table()['thickness'].isna().tolist() == [True]
    assert batch.settings['thickness'] == 2.0
    assert pages.intake.following == made.label


def test_a_batch_that_starts_with_the_settings_of_no_batch_is_not_made(
    http: TestClient, manager: BatchManager
) -> None:
    answer = http.post('/api/batches', json=new_batch(settings_from='gone'))

    assert (answer.status_code, answer.json()['message']) == (
        422,
        'The batch was not made: there is no batch gone to start from.',
    )
    assert list(manager.headers()) == ['scan']


def test_a_batch_made_from_the_page_of_a_batch_starts_with_its_settings(
    http: TestClient, pages: Pages
) -> None:
    bins(http, 7)

    made = http.post('/api/batches', json=new_batch(settings_from='scan')).json()

    assert form_bins(state(opened(pages, made['label']))) == 7


def test_a_setting_of_the_batch_at_a_column_of_the_new_one_is_its_empty_cells(
    http: TestClient, manager: BatchManager
) -> None:
    make_setting(http)
    http.post('/api/settings', json={'path': ['thickness'], 'op': 'set', 'value': 2.5})
    http.post('/api/settings/apply')
    columns = ['sample', 'transmission', 'thickness']

    made = http.post(
        '/api/batches', json=new_batch(settings_from='scan', parameter_columns=columns)
    ).json()

    batch = manager.batch(made['label'])
    assert batch.parameter_columns == tuple(columns)
    assert batch.table()['thickness'].isna().tolist() == [True, True]
    assert [c['thickness'] for c in batch.cells().values()] == [2.5, 2.5]


def test_a_row_that_arrives_in_a_new_batch_that_follows_takes_a_setting_ticked(
    http: TestClient,
    pages: Pages,
    client: Client,
    sample: Measure,
) -> None:
    make_setting(http)
    http.post('/api/settings', json={'path': ['thickness'], 'op': 'set', 'value': 2.5})
    http.post('/api/settings/apply')
    columns = ['sample', 'transmission', 'thickness']
    sent = new_batch(
        settings_from='scan', parameter_columns=columns, later=True, follow=True
    )

    made = http.post('/api/batches', json=sent).json()
    sample(4, 'ethanol')
    pages.tick()
    client.wait(client.records())

    (record,) = client.records(label=made['label'])
    assert record.member == '4'
    assert record.request.params['thickness'] == 2.5


@pytest.mark.parametrize(('way', 'kept'), [('source', False), ('table file', True)])
def test_a_column_that_the_rows_fill_keeps_its_cells_over_a_setting_of_its_name(
    http: TestClient, manager: BatchManager, way: str, kept: bool
) -> None:
    manager.create(STAGED.name, title='X', settings={'sample': 'run:1'}, label='x')

    if way == 'source':
        sent = new_batch(
            settings_from='x', parameter_columns=['sample', 'transmission']
        )
        made = http.post('/api/batches', json=sent).json()
        given = ['uuid:run-1', 'uuid:run-2']
    else:
        text = 'key,sample,transmission\na,run:5,run:1001\nb,run:6,run:1002\n'
        made = from_table(http, text, settings_from='x').json()
        given = ['run:5', 'run:6']

    batch = manager.batch(made['label'])
    assert batch.table()['sample'].tolist() == given
    # A source fills the column, so an empty cell there waits for a run; a
    # table file has no source, and the setting is for the rows added later.
    assert ('sample' in batch.settings) is kept


def from_table(http: TestClient, text: str, **given: Any) -> Any:
    """Make a batch from a table file ``rows.csv`` that holds ``text``."""
    query = {'spec': STAGED.name, 'title': 'From a file', 'name': 'rows.csv'} | given
    return http.post('/api/batches/table', params=query, content=text.encode())


def test_a_batch_made_from_a_table_file_has_its_rows_and_columns_and_no_source(
    http: TestClient, manager: BatchManager, intake: Intake
) -> None:
    text = 'key,run,sample,transmission\na,1,run:1,run:1001\nb,2,run:2,run:1002\n'

    made = from_table(http, text, settings_from='scan').json()

    batch = manager.batch(made['label'])
    assert batch.parameter_columns == ('sample', 'transmission')
    assert list(batch.table().index) == ['a', 'b']
    assert intake.row_source(made['label']) is None
    assert manager.headers()[made['label']].title == 'From a file'


@pytest.mark.parametrize(
    ('text', 'said'),
    [('', 'the file is empty'), ('run,sample\n1,run:1\n', "a column 'key'")],
    ids=['empty', 'no-key'],
)
def test_a_table_file_refused_names_the_file_chosen(
    http: TestClient, manager: BatchManager, text: str, said: str
) -> None:
    made = from_table(http, text)
    loaded = http.post('/api/table', params={'name': 'rows.csv'}, content=text.encode())

    assert made.status_code == loaded.status_code == 422
    assert made.json()['message'].startswith('The batch was not made: rows.csv: ')
    assert loaded.json()['message'].startswith('Cannot load rows.csv: ')
    for message in (made.json()['message'], loaded.json()['message']):
        assert said in message
        assert tempfile.gettempdir() not in message
    assert list(manager.headers()) == ['scan']


def test_an_empty_settings_file_says_so_in_a_sentence(http: TestClient) -> None:
    answer = http.post('/api/settings.yaml', params={'name': 's.yaml'}, content=b'')

    assert (answer.status_code, answer.json()['message']) == (
        422,
        'Cannot load s.yaml: it is not a settings file saved from this page.',
    )


def test_a_misspelt_column_of_a_table_file_is_a_note_and_its_setting_is_asked_for(
    http: TestClient, pages: Pages
) -> None:
    made = from_table(http, 'key,samples,transmission\na,run:1,run:1001\n').json()

    shown = state(opened(pages, made['label']))

    assert [c['name'] for c in shown['notes']] == ['samples']
    assert [c['name'] for c in shown['columns']] == ['transmission']
    # The form asks for the setting; the row reads ready, and nothing is
    # reduced until the setting is given.
    assert shown['settings_problem'] == (
        'Sample: choose a run; Sample: a required parameter, not given'
    )
    assert shown['action']['label'] == 'Check the settings'
    assert [r['status'] for r in shown['rows']] == ['ready']


@pytest.mark.parametrize(
    ('given', 'said'),
    [
        ({'title': ''}, 'a batch has a title that is not empty'),
        ({'title': '  '}, 'a batch has a title that is not empty'),
        ({'source': 'other'}, 'the source other is not registered; registered: pairs'),
        ({'spec': 'other'}, 'the workspace binds no spec other'),
    ],
    ids=['title', 'blank', 'source', 'spec'],
)
def test_what_the_manager_refuses_answers_422_and_makes_no_batch(
    pages: Pages, manager: BatchManager, given: dict[str, Any], said: str
) -> None:
    answer = TestClient(create_app(pages)).post('/api/batches', json=new_batch(**given))

    assert answer.status_code == 422
    assert answer.json()['message'].startswith('The batch was not made: ')
    assert said in answer.json()['message']
    assert manager.headers() == {}


def test_the_root_without_a_batch_serves_the_page_and_the_routes_of_the_workspace(
    pages: Pages, sample: Measure
) -> None:
    sample(1, 'water')
    http = TestClient(create_app(pages))

    page = http.get('/')

    assert page.status_code == 200
    assert page.headers['content-type'].startswith('text/html')
    assert http.get('/static/app.js').status_code == 200
    assert http.get('/api/batches').json()['rows'] == []
    assert len(http.get('/api/runs').json()['rows']) == 2


def test_the_root_is_not_kept_by_the_browser_with_or_without_a_batch(
    pages: Pages, intake: Intake
) -> None:
    http = TestClient(create_app(pages), follow_redirects=False)

    page = http.get('/')
    intake.create(STAGED.name, title='First', source='pairs')
    redirect = http.get('/')

    assert (page.status_code, redirect.status_code) == (200, 307)
    assert page.headers['cache-control'] == 'no-store'
    assert redirect.headers['cache-control'] == 'no-store'


def test_runs_are_corrected_without_a_batch(pages: Pages, sample: Measure) -> None:
    sample(1, 'water')
    http = TestClient(create_app(pages))
    (run, *_) = http.get('/api/runs').json()['rows']
    edit = {'keys': [run['key']], 'column': 'role', 'value': 'background'}

    answer = http.post('/api/runs/cells', json=edit)

    assert answer.json() == {'notice': 'Role corrected on 1 run.'}
    (shown, *_) = http.get('/api/runs').json()['rows']
    assert (shown['cells']['role']['text'], shown['cells']['role']['edited']) == (
        'background',
        True,
    )


def test_a_batch_whose_rows_cannot_be_read_answers_422_and_the_list_stays(
    client: Client, definitions: DefinitionStore
) -> None:
    first = BatchManager(client, definitions=definitions)
    label = first.create(STAGED.name, title='Odd').label
    definitions.save(TABLE.key(label), {'nonsense': 1})
    pages = restarted(client, definitions)

    shown = opened(pages, label).get('/api/state')

    assert shown.status_code == 422
    assert shown.json()['message'].startswith(f'Cannot show {label}: ')
    listed = opened(pages, label).get('/api/batches').json()['rows']
    assert [b['title'] for b in listed] == ['Odd']


def test_a_batch_shows_while_the_files_cannot_be_listed(
    http: TestClient, batch: Batch, client: Client, fake: FakeFiles
) -> None:
    client.wait(batch.reduce(['1']))
    listed = fake.list
    fake.list = lambda selector: (_ for _ in ()).throw(OSError('half written'))

    shown = state(http)

    assert statuses(http) == {'1': 'done', '2': 'ready', '3': 'incomplete'}
    assert shown['files_problem'] == 'The files cannot be listed: half written'
    column = next(c for c in shown['columns'] if c['name'] == 'sample')
    assert column['editor']['options'] == []
    assert http.post('/api/plot', json={'keys': []}).json()['figure'] is not None
    runs = http.get('/api/runs').json()
    assert (runs['rows'], runs['problem']) == ([], shown['files_problem'])
    fake.list = listed
    assert http.post('/api/relist').json()['files_problem'] is None


def test_a_batch_shows_while_the_file_source_raises_and_each_row_says_why(
    http: TestClient, fake: FakeFiles
) -> None:
    with toys.unreachable(fake, 'resolve'), toys.unreachable(fake, 'list'):
        shown = http.get('/api/state')
        edit = {'keys': ['2'], 'column': 'thickness', 'value': 1.5}
        edited = http.post('/api/cells', json=edit)

    assert (shown.status_code, edited.status_code) == (200, 200)
    why = shown.json()['files_problem']
    assert why == 'The files cannot be listed: the mount is gone'
    assert {r['status'] for r in edited.json()['rows']} == {'incomplete'}
    (first, *_) = edited.json()['rows']
    assert first['cells']['sample']['problem'] == (
        'Sample: file uuid:run-1 cannot be read: the mount is gone'
    )
    assert statuses(http) == {'1': 'ready', '2': 'ready', '3': 'incomplete'}
    assert http.post('/api/relist').json()['files_problem'] is None


# The tab "Batch"


def save_header(http: TestClient, **given: Any) -> Any:
    """Save the header of the page's batch, as the tab "Batch" sends it."""
    sent = {'title': 'Scan', 'description': ''}
    return http.post('/api/batch', json=sent | given)


def test_the_state_shows_the_workflow_and_description_of_the_batch(
    http: TestClient,
) -> None:
    assert state(http)['header'] == {
        'workflow': 'Staged',
        'spec': 'staged',
        'description': '',
    }


def test_a_header_change_the_definitions_refuse_answers_422_and_changes_nothing(
    http: TestClient, manager: BatchManager, refusing: Refusing
) -> None:
    before = state(http)
    refusing.part = 'header'

    answer = save_header(http, title='Morning')

    assert (answer.status_code, answer.json()) == (
        422,
        {'message': "The change was not stored: ('labels', 'scan', 'header'): refused"},
    )
    assert manager.headers()['scan'].title == 'Scan'
    after = state(http)
    assert (after['title'], after['header'], after['rows']) == (
        before['title'],
        before['header'],
        before['rows'],
    )


def test_a_batch_to_follow_whose_follow_the_definitions_refuse_is_not_made(
    http: TestClient, manager: BatchManager, intake: Intake, refusing: Refusing
) -> None:
    refusing.part = 'source'

    answer = http.post('/api/batches', json=new_batch(follow=True))

    assert answer.status_code == 422
    assert re.fullmatch(
        r"The change was not stored: \('labels', '[\d-]+', 'source'\): refused",
        answer.json()['message'],
    )
    assert (list(manager.headers()), intake.following) == (['scan'], None)


def test_a_title_and_description_saved_on_the_page_are_kept(
    http: TestClient, manager: BatchManager
) -> None:
    saved = save_header(http, title='Morning', description='1 mm cells')

    assert manager.headers()['scan'] == Header('Morning', '1 mm cells')
    shown = saved.json()
    assert (shown['title'], shown['notice']) == ('Morning', 'Saved.')
    assert shown['header']['description'] == '1 mm cells'


def test_a_header_the_manager_refuses_answers_422_and_changes_nothing(
    http: TestClient, manager: BatchManager
) -> None:
    before = manager.headers()

    answer = save_header(http, title=' ')

    assert answer.status_code == 422
    assert answer.json()['message'] == (
        'The batch was not changed: a batch has a title that is not empty.'
    )
    assert manager.headers() == before


def test_a_batch_removed_from_its_page_is_gone_and_its_results_stay(
    http: TestClient, manager: BatchManager, batch: Batch, client: Client
) -> None:
    client.wait(batch.reduce(['1']))

    removed = http.post('/api/batch/remove')

    assert (removed.status_code, removed.json()) == (200, {'notice': 'Removed Scan.'})
    assert manager.headers() == {}
    assert client.records(label='scan') != []
    gone = http.get('/api/state')
    assert (gone.status_code, gone.json()) == (
        404,
        {'message': 'There is no batch scan.'},
    )


def test_a_batch_removed_with_clear_has_the_outputs_of_its_records_cleared(
    http: TestClient, manager: BatchManager, batch: Batch, client: Client
) -> None:
    client.wait(batch.reduce(['1']))

    removed = http.post('/api/batch/remove', json={'clear': True})

    assert removed.json() == {'notice': 'Removed Scan and cleared its outputs.'}
    assert manager.headers() == {}
    assert client.cleared(client.records(label='scan')) == [True]


def test_removing_the_batch_that_follows_turns_follow_off(
    pages: Pages, intake: Intake
) -> None:
    label = toys.from_pairs(intake, STAGED.name, title='F', follow=True)

    opened(pages, label).post('/api/batch/remove')

    assert pages.intake.following is None


def test_a_removal_the_definitions_refuse_answers_422_and_the_batch_stays(
    pages: Pages, manager: BatchManager, intake: Intake, refusing: Refusing
) -> None:
    label = toys.from_pairs(intake, STAGED.name, title='F', label='f', follow=True)
    refusing.part = 'header'

    answer = opened(pages, label).post('/api/batch/remove')

    assert answer.status_code == 422
    assert answer.json()['message'] == (
        "The change was not stored: ('labels', 'f', 'header'): refused"
    )
    # Nothing was removed: the header is kept, and the batch still follows.
    assert list(manager.headers()) == ['f']
    assert intake.following == 'f'


def test_another_page_of_a_removed_batch_answers_404_at_its_next_poll(
    http: TestClient, pages: Pages
) -> None:
    other = opened(pages, 'scan')
    now = state(other)

    http.post('/api/batch/remove')

    at = {'after': now['revision'], 'start': now['start']}
    polled = other.get('/api/state', params=at)
    assert (polled.status_code, polled.json()) == (
        404,
        {'message': 'There is no batch scan.'},
    )


def test_a_page_handed_out_before_its_batch_is_removed_answers_missing(
    http: TestClient, pages: Pages
) -> None:
    # As a request does that found the batch, then waited for the lock while
    # another removed it.
    page = pages.page('scan')

    pages.remove('scan')

    with pytest.raises(NotFound, match=r'There is no batch scan\.'):
        page.poll(0)
    with pytest.raises(NotFound):
        page.relist()


def test_a_batch_made_again_under_a_removed_label_gets_a_new_page(
    http: TestClient, pages: Pages, intake: Intake
) -> None:
    page = pages.page('scan')

    pages.remove('scan')
    intake.create(STAGED.name, title='Again', source='pairs', label='scan')

    assert pages.page('scan') is not page
    assert state(http)['title'] == 'Again'


# Output


def with_rows(pages: Pages, label: str, sample: Measure, client: Client) -> TestClient:
    """The page of ``label`` of ``pages``, rows 1 and 2 done, 3 incomplete."""
    http = opened(pages, label)
    sample(1, 'water')
    sample(2, 'sds')
    sample(3, 'oil', partner=False)
    pages.intake.manager.batch(label).set_table(toys.rows(client))
    reduced(http, client)
    return http


@pytest.fixture
def scicat() -> Any:
    from scitacean.testing.client import FakeClient
    from scitacean.testing.transfer import FakeFileTransfer

    return FakeClient.without_login(
        url='https://scicat.invalid/api/v3', file_transfer=FakeFileTransfer()
    )


@pytest.fixture
def publishing(
    intake: Intake, scicat: Any, label: str, sample: Measure, client: Client
) -> TestClient:
    """The page of ``label`` with a catalogue, rows 1 and 2 done, 3 incomplete."""
    from ess.apps.scicat import Catalogue

    catalogue = Catalogue(client=scicat, instrument='loki')
    return with_rows(Pages(intake, catalogue=catalogue), label, sample, client)


@pytest.fixture
def written(intake: Intake, label: str, sample: Measure, client: Client) -> TestClient:
    """The page of ``label`` with the format ``text``, rows 1 and 2 done."""
    pages = Pages(intake, writers={STAGED.name: [TEXT]})
    return with_rows(pages, label, sample, client)


FORM = {
    'format': 'scipp-hdf5',
    'outputs': ['total'],
    'owner': 'Anna',
    'contact_email': 'anna@example.org',
}


def zipped(http: TestClient, keys: list[str]) -> list[str]:
    """The names in the zip of the results of the rows ``keys``."""
    content = http.post('/api/results.zip', json={'keys': keys}).content
    return sorted(zipfile.ZipFile(io.BytesIO(content)).namelist())


def test_without_a_catalogue_the_output_tab_has_the_form_but_no_publishing(
    http: TestClient,
) -> None:
    output = state(http)['output']
    applied = http.post('/api/output/form', json=FORM).json()

    assert ([f['name'] for f in output['formats']], output['catalogue']) == (
        ['scipp-hdf5'],
        False,
    )
    assert output['outputs'] == [{'name': 'total', 'title': 'total'}]
    assert applied['notice'] == 'Applied the output options.'
    assert state(http)['output']['options']['owner'] == 'Anna'
    assert http.post('/api/publish', json={'keys': ['1']}).status_code == 422


def test_the_formats_are_those_given_and_scipp_hdf5_the_default_first(
    written: TestClient,
) -> None:
    output = state(written)['output']

    assert output['formats'] == [
        {
            'name': w.name,
            'title': w.title,
            'description': w.description,
            'outputs': None if w.outputs is None else list(w.outputs),
        }
        for w in (TEXT, SCIPP_HDF5)
    ]
    assert output['options']['format'] == 'text'


def test_a_kept_format_the_page_lacks_is_a_problem_until_one_it_has_is_applied(
    intake: Intake, label: str, sample: Measure, client: Client
) -> None:
    writing = Writing(intake.manager, {STAGED.name: [TEXT]})
    writing.set_options(label, writing.options(label))
    http = with_rows(Pages(intake), label, sample, client)
    kept = state(http)['output']
    refused = http.post('/api/results.zip', json={'keys': ['1']})

    http.post('/api/output/form', json=FORM)

    assert kept['options']['format'] == 'text'
    assert 'no writer of the format text' in kept['problem']
    assert refused.status_code == 422
    assert state(http)['output']['problem'] == ''


def test_the_results_download_as_the_form_says(written: TestClient) -> None:
    as_text = zipped(written, ['1', '3'])
    written.post('/api/output/form', json=FORM)
    as_hdf5 = zipped(written, ['1', '3'])

    assert as_text == [
        'results/1/_record.json',
        'results/1/total.txt',
        'results/results.csv',
    ]
    assert as_hdf5 == [
        'results/1/_record.json',
        'results/1/total.h5',
        'results/results.csv',
    ]


def test_a_published_row_shows_the_identifier_of_its_dataset(
    publishing: TestClient, scicat: Any
) -> None:
    assert state(publishing)['output']['catalogue']
    publishing.post('/api/output/form', json=FORM)

    done = publishing.post('/api/publish', json={'keys': ['2']}).json()

    [pid] = scicat.datasets
    assert scicat.get_dataset(pid).name == 'Scan sds'
    shown = {row['key']: row['published'] for row in done['rows']}
    assert shown == {'1': '', '2': f'pid:{pid}', '3': ''}
    assert done['notice'] == f'Published 1 row: pid:{pid}.'


@pytest.mark.parametrize(
    ('form', 'message'),
    [
        (FORM | {'outputs': ['nope']}, 'Not applied: staged has no output nope'),
        (FORM | {'name': '{sample}'}, 'Not applied: name: a name may hold {title}'),
    ],
)
def test_a_form_refused_says_why(
    http: TestClient, form: dict[str, Any], message: str
) -> None:
    refused = http.post('/api/output/form', json=form)

    assert refused.status_code == 422
    assert message in refused.json()['message']


@pytest.mark.parametrize(
    ('form', 'keys', 'message'),
    [
        (FORM, ['3'], 'Not published: no result to publish, done and not outdated: 3'),
        (FORM, [], 'Select the rows to publish'),
        (
            FORM | {'owner': ''},
            ['1'],
            'Not published: the output options name no owner',
        ),
    ],
)
def test_a_refused_publication_says_why_and_publishes_nothing(
    publishing: TestClient,
    scicat: Any,
    form: dict[str, Any],
    keys: list[str],
    message: str,
) -> None:
    publishing.post('/api/output/form', json=form)

    refused = publishing.post('/api/publish', json={'keys': keys})

    assert refused.status_code == 422
    assert message in refused.json()['message']
    assert scicat.datasets == {}
