# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The batch page's API over HTTP, on a real in-process workspace."""

import io
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
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from ess.apps import Client, local
from ess.apps.batch import (
    Batch,
    BatchManager,
    CorrectedFiles,
    DefinitionStore,
    FolderDefinitions,
    Header,
    Source,
)
from ess.apps.batch.definition import Key, NotKept, rows_key, settings_key
from ess.apps.batch.web import Pages, Shown, create_app, demo, plots
from ess.apps.batch.web.page import Missing
from ess.apps.testing import CountingClient, FakeFiles
from ess.spec import FileRef, SampleRunFile, WorkflowSpec, function_binding

from . import toys
from .toys import (
    PLAIN,
    SOURCES,
    STAGED,
    STAGED_NAMES,
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
def pages(manager: BatchManager, files: CorrectedFiles) -> Pages:
    return Pages(
        manager,
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


def started(client: Client, definitions: DefinitionStore) -> Pages:
    """The pages of a manager made on ``definitions``, as after a restart."""
    manager = BatchManager(
        client, sources=SOURCES, definitions=definitions, staged=STAGED_NAMES
    )
    return Pages(manager)


@pytest.fixture
def http(
    pages: Pages, label: str, batch: Batch, sample: Measure, client: Client
) -> TestClient:
    sample(1, 'water')
    sample(2, 'sds')
    sample(3, 'oil', partner=False)
    batch.put(toys.rows(client))
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
    same = {'after': now['version'], 'start': now['start']}

    assert http.get('/api/state', params=same).status_code == 204
    restarted = http.get('/api/state', params={**same, 'start': 'other'})
    assert restarted.json()['version'] == now['version']


def test_a_poll_sees_a_reduction_finish(
    http: TestClient, client: Client, gate: Gate
) -> None:
    gate.close()
    running = http.post('/api/reduce', json={'which': 'remaining'}).json()
    gate.open()
    client.wait(client.records())

    after = {'after': running['version'], 'start': running['start']}
    finished = http.get('/api/state', params=after).json()

    assert [r['status'] for r in running['rows']][:2] == ['running', 'running']
    assert [r['status'] for r in finished['rows']][:2] == ['done', 'done']


def test_a_poll_sees_a_row_that_another_caller_put_without_a_record(
    http: TestClient, batch: Batch, sample: Measure, client: Client
) -> None:
    now = state(http)
    after = {'after': now['version'], 'start': now['start']}
    batch.put(batch.frame())
    assert http.get('/api/state', params=after).status_code == 204

    sample(4, 'salt', partner=False)
    batch.put(toys.rows(client))
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
    after = {'after': now['version'], 'start': now['start']}
    assert http.get('/api/state', params=after).status_code == 204

    act(http)

    polled = http.get('/api/state', params=after)
    assert polled.status_code == 200
    assert polled.json()['version'] > now['version']


def test_a_rescan_offers_the_runs_it_found_and_changes_no_row(
    http: TestClient, sample: Measure
) -> None:
    sample(4, 'salt')

    listed = http.post('/api/relist').json()

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

    assert list(batch.frame().loc[['1', '2'], 'thickness']) == [5.0, 5.0]
    assert notice == 'Thickness set on 2 rows.'


def test_an_edit_of_several_rows_with_one_unknown_edits_none(
    http: TestClient, batch: Batch
) -> None:
    before = batch.frame()

    refused = http.post(
        '/api/cells', json={'keys': ['1', '9'], 'column': 'thickness', 'value': 5}
    )

    assert 'no row 9' in refused.json()['message']
    pd.testing.assert_frame_equal(batch.frame(), before)


def test_a_file_cell_is_set_by_the_label_of_its_option_and_kept_as_its_name(
    http: TestClient, batch: Batch
) -> None:
    column = next(c for c in state(http)['columns'] if c['name'] == 'transmission')
    label = next(o for o in column['editor']['options'] if o.startswith('run 1001'))

    edit = {'keys': ['3'], 'column': 'transmission', 'value': label}
    http.post('/api/cells', json=edit)

    assert statuses(http)['3'] == 'ready'
    assert rows(http)['3']['cells']['transmission']['text'] == 'run 1001'
    table = batch.frame()
    assert table.at['3', 'transmission'] == table.at['1', 'transmission']


def test_a_cell_emptied_is_empty_in_the_table(http: TestClient, batch: Batch) -> None:
    http.post('/api/cells', json={'keys': ['1'], 'column': 'transmission'})

    assert statuses(http)['1'] == 'incomplete'
    assert pd.isna(batch.frame().at['1', 'transmission'])


@pytest.mark.parametrize(
    ('request_', 'message'),
    [
        (('post', '/api/cells', {'keys': ['9'], 'column': 'thickness'}), 'no row 9'),
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
    batch.put(batch.frame().assign(offset='nope.x'))
    assert rows(http)['1']['cells']['offset']['problem'] is None

    response = http.post('/api/reduce', json={'which': 'selected', 'keys': ['1']})

    assert response.status_code == 422
    assert response.json() == {'message': 'Nothing was reduced: one value is refused.'}
    shown = state(http)
    (refused,) = shown['refused']
    assert refused['text'] == 'water: Offset: unknown record or accumulator nope'
    problem = shown['rows'][0]['cells']['offset']['problem']
    assert problem == 'Offset: unknown record or accumulator nope'


def test_a_refused_value_stays_marked_until_it_is_edited(http: TestClient) -> None:
    thickness(http, ['1'], -1)
    http.post('/api/reduce', json={'which': 'selected', 'keys': ['1']})

    thickness(http, ['2'], 3)
    assert rows(http)['1']['cells']['thickness']['problem']
    thickness(http, ['1'], 3)
    assert state(http)['refused'] == []


def refuse_thickness_of_water(http: TestClient) -> None:
    thickness(http, ['1'], -1)
    refused = http.post('/api/reduce', json={'which': 'selected', 'keys': ['1']})
    assert refused.status_code == 422


def test_a_refusal_goes_when_a_reduction_succeeds(http: TestClient) -> None:
    refuse_thickness_of_water(http)
    assert state(http)['refused'] != []

    reduce = http.post('/api/reduce', json={'which': 'selected', 'keys': ['2']})

    assert reduce.status_code == 200
    assert reduce.json()['refused'] == []


def test_a_refusal_stays_when_another_cell_of_its_row_is_edited(
    http: TestClient,
) -> None:
    refuse_thickness_of_water(http)
    column = next(c for c in state(http)['columns'] if c['name'] == 'transmission')
    label = next(o for o in column['editor']['options'] if o.startswith('run 1002'))

    http.post(
        '/api/cells', json={'keys': ['1'], 'column': 'transmission', 'value': label}
    )

    assert [r['entry'] for r in state(http)['refused']] == ['1']


def test_a_refusal_of_a_cell_stays_when_other_settings_are_applied(
    http: TestClient,
) -> None:
    refuse_thickness_of_water(http)

    applied = bins(http, 3)

    assert [r['entry'] for r in applied['refused']] == ['1']


def test_a_refusal_of_a_setting_goes_when_settings_are_applied(
    http: TestClient,
) -> None:
    bins(http, 0)
    assert http.post('/api/reduce', json={'which': 'remaining'}).status_code == 422
    assert [r['entry'] for r in state(http)['refused']] == [None]

    applied = bins(http, 3)

    assert applied['refused'] == []


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


def test_a_changed_setting_leaves_the_rows_done_marked_with_earlier_settings(
    http: TestClient, client: Client
) -> None:
    reduced(http, client)

    bins(http, 3)

    shown = rows(http)
    assert statuses(http) == {'1': 'done', '2': 'done', '3': 'incomplete'}
    assert [shown[k]['earlier'] for k in ('1', '2')] == [True, True]
    assert (
        shown['1']['reason'] == 'Reduced with earlier settings: Binning changed since.'
    )
    assert state(http)['action']['label'] == 'Nothing left to reduce'
    assert state(http)['action']['earlier'] == 2
    detail = http.get('/api/row', params={'key': '1'}).json()
    assert detail['differs'] == ['binning']
    plot = http.post('/api/plot', json={'keys': ['1']}).json()
    assert plot['figure']['outdated'] == [False]
    table = pd.read_csv(io.BytesIO(http.get('/api/table.csv').content), dtype=str)
    assert list(table['status']) == ['done', 'done', 'incomplete']
    assert list(table['earlier']) == ['True', 'True', 'False']


def test_reduce_leaves_the_rows_of_earlier_settings_and_apply_to_all_reduces_them(
    http: TestClient, client: Client
) -> None:
    reduced(http, client)
    bins(http, 3)
    thickness(http, ['2'], 4.0)

    reduced(http, client)
    assert statuses(http)['2'] == 'done'
    assert rows(http)['1']['earlier']
    assert not rows(http)['2']['earlier']

    reduced(http, client, which='earlier')
    assert not rows(http)['1']['earlier']
    assert state(http)['action']['earlier'] == 0


def test_a_page_started_again_marks_the_rows_reduced_with_other_settings(
    http: TestClient, client: Client, definitions: DefinitionStore, label: str
) -> None:
    reduced(http, client)
    again = opened(started(client, definitions), label)

    bins(again, 3)

    assert statuses(again) == {'1': 'done', '2': 'done', '3': 'incomplete'}
    assert rows(again)['1']['earlier']


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
        label = toys.from_pairs(pages.manager, PLAIN.name, title='New')
        old = Batch(client, PLAIN, label=label, rows=toys.rows(client))
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
    assert edited['settings_changed'] is True
    assert batch.settings == {}
    assert statuses(http) == {'1': 'done', '2': 'done', '3': 'incomplete'}
    assert not rows(http)['1']['earlier']


def test_apply_writes_the_form_to_the_batch(http: TestClient, batch: Batch) -> None:
    draft(http, 3)

    applied = http.post('/api/settings/apply').json()

    assert batch.settings['binning'] == {'bins': 3}
    assert applied['settings_changed'] is False
    assert applied['notice'] == 'Applied the settings.'


def test_discard_shows_the_batch_s_settings_again(
    http: TestClient, batch: Batch
) -> None:
    bins(http, 3)
    draft(http, 5)

    discarded = http.post('/api/settings/discard').json()

    assert form_bins(discarded) == 3
    assert discarded['settings_changed'] is False
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
    assert shown['settings_changed'] is True
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
    other = manager.create(STAGED.name, title='Other')

    loaded = opened(pages, other).post(
        '/api/table', params={'name': f'rows.{suffix}'}, content=content
    )

    assert loaded.json()['notice'] == f'Loaded 3 rows from rows.{suffix}.'
    # A cell of .xlsx has a type; a note of .csv is text.
    expected = batch.frame()
    if suffix == 'csv':
        expected = expected.astype({'run': 'string'})
    pd.testing.assert_frame_equal(manager.batch(other).frame(), expected)


def test_a_table_loaded_replaces_the_rows_the_columns_and_so_the_forms(
    http: TestClient, batch: Batch
) -> None:
    content = b'key,sample,name\na,run:1,first\n'

    loaded = http.post('/api/table', params={'name': 'rows.csv'}, content=content)

    assert loaded.status_code == 200, loaded.json()
    shown = loaded.json()
    assert [(r['key'], r['name']) for r in shown['rows']] == [('a', 'first')]
    assert [c['name'] for c in shown['columns']] == ['sample']
    assert batch.columns == ('sample',)
    settings = [n['path'][0] for n in shown['settings']]
    assert {'transmission', 'thickness'} <= set(settings)
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
    kept = store.load(settings_key(label), dict)
    store.save(settings_key(label), {**kept, 'params': params})


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
        still = definitions.load(settings_key(label), lambda d: d['params'])
        after = page.post('/api/settings/apply').json()

    assert shown['settings_changed'] is False
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
            raise NotKept('disk full')
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
    manager = BatchManager(client, sources=SOURCES, definitions=flaky)
    label = toys.from_pairs(manager, STAGED.name, title='Kept', label='kept')
    files = CorrectedFiles(fake, definitions=flaky)
    return opened(Pages(manager, files=files, marks=Marks), label)


def test_a_change_the_definitions_cannot_keep_answers_422_and_nothing_changes(
    kept: TestClient, flaky: Flaky
) -> None:
    before = dict(flaky.kept)
    flaky.fail = True

    draft(kept, 7)
    setting = kept.post('/api/settings/apply')
    cell = thickness(kept, ['1'], 3)
    table = kept.post(
        '/api/table',
        params={'name': 'rows.csv'},
        content=b'key,sample,transmission,thickness\n1,1,1001,3\n',
    )

    assert setting.json() == {'message': 'The change was not kept: disk full'}
    assert cell == {'message': 'The change was not kept: disk full'}
    assert table.json() == {'message': 'The change was not kept: disk full'}
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

    assert (form_bins(shown), shown['settings_changed']) == (7, True)
    assert flaky.kept[settings_key('kept')]['params']['binning'] == {'bins': 7}


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
    assert (form_bins(loaded), loaded['settings_changed']) == (2, True)
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
    assert batch.settings == {'binning': {'bins': 5}, 'offset': {'value': 2.0}}


@pytest.mark.parametrize('which', ['remaining', 'earlier', 'all', 'selected'])
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
    manager: BatchManager,
    label: str,
    client: Client,
    sample: Measure,
) -> None:
    def broken(_: Sequence[Shown]) -> Any:
        raise RuntimeError('released')

    sample(1, 'water')
    manager.batch(label).put(toys.rows(client))
    pages = Pages(manager, draw={STAGED.name: broken})
    page = opened(pages, label)
    reduced(page, client)

    plot = page.post('/api/plot', json={}).json()

    assert plot['figure'] is None
    assert 'Cannot draw: released' in plot['note']


def test_no_two_curves_of_a_figure_share_a_colour() -> None:
    n = len(plots.PALETTE)
    first, second, third = plots.PALETTE[:3]

    assert plots.colors([1, n + 1, 2]) == [second, first, third]
    assert len(set(plots.colors(range(3 * n)))) == 3 * n


def test_plotting_and_saving_chosen_rows_reads_their_records_only(
    client: Client, sample: Measure, definitions: DefinitionStore
) -> None:
    counting = CountingClient(client)
    manager = BatchManager(counting, sources=SOURCES, definitions=definitions)  # type: ignore[arg-type]
    for run in (1, 2, 3):
        sample(run, f's{run}')
    label = manager.create(STAGED.name, title='Scan', rows=toys.rows(client))
    pages = Pages(manager, draw={STAGED.name: draw})
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
    assert (form_bins(used), used['settings_changed']) == (2, True)
    assert used['rows'][0]['earlier']
    assert batch.settings['binning'] == {'bins': 2}
    assert not applied['rows'][0]['earlier']


@pytest.mark.parametrize('keep', [True, False], ids=['reduce them again', 'back'])
def test_settings_tried_on_one_row_reach_the_others_only_when_a_person_says_so(
    http: TestClient, client: Client, keep: bool
) -> None:
    reduced(http, client)
    bins(http, 4)
    http.post('/api/reduce', json={'which': 'selected', 'keys': ['1']})
    client.wait(client.records())

    def earlier() -> list[str]:
        return [k for k, r in rows(http).items() if r['earlier']]

    def binning() -> list[int]:
        return [
            client.latest('scan', k).request.params['binning']['bins'] for k in '12'
        ]

    assert (earlier(), binning()) == (['2'], [4, 2])
    assert http.get('/api/row', params={'key': '1'}).json()['differs'] == []
    if keep:
        reduced(http, client, which='earlier')
        assert (earlier(), binning()) == ([], [4, 4])
    else:
        bins(http, 2)
        assert (earlier(), binning()) == (['1'], [4, 2])


def test_a_row_added_on_the_page_is_empty_and_rows_removed_are_gone(
    http: TestClient, batch: Batch
) -> None:
    added = http.post('/api/rows/add', json={'key': 'extra'}).json()
    assert (added['rows'][-1]['name'], added['rows'][-1]['status']) == (
        'extra',
        'incomplete',
    )
    # A key may be the name of another row; the batch refuses a key it has.
    named = http.post('/api/rows/add', json={'key': 'water'}).json()
    assert [r['key'] for r in named['rows']] == ['1', '2', '3', 'extra', 'water']
    same = http.post('/api/rows/add', json={'key': 'water'})
    assert same.status_code == 422
    http.post('/api/rows/remove', json={'keys': ['water']})

    after = http.post('/api/rows/remove', json={'keys': ['extra', '2']}).json()

    assert after['notice'] == 'Removed 2 rows.'
    assert list(batch.frame().index) == ['1', '3']


def offered(http: TestClient) -> dict[str, str]:
    """The rows that "Add rows from the runs" lists, by key: the mark of each."""
    return {r['key']: r['mark'] for r in http.get('/api/rows/offered').json()['rows']}


def test_the_rows_offered_are_those_of_the_source_the_table_lacks(
    http: TestClient, sample: Measure
) -> None:
    sample(4, 'ethanol')
    sample(5, 'acetone', partner=False)

    listed = http.get('/api/rows/offered').json()

    assert [(r['key'], r['name'], r['mark']) for r in listed['rows']] == [
        ('4', 'ethanol', ''),
        ('5', 'acetone', 'waits'),
    ]


def test_rows_taken_are_added_as_the_source_makes_them(
    http: TestClient, sample: Measure
) -> None:
    sample(4, 'ethanol')

    taken = http.post('/api/rows/take', json={'keys': ['4']}).json()

    assert taken['notice'] == 'Added 1 row.'
    assert [r['key'] for r in taken['rows']] == ['1', '2', '3', '4']
    assert taken['rows'][-1]['cells']['sample']['text'] == 'run 4'
    assert offered(http) == {}


def test_a_row_taken_then_removed_is_offered_again_and_follow_does_not_add_it(
    http: TestClient, sample: Measure
) -> None:
    sample(4, 'ethanol')
    http.post('/api/rows/take', json={'keys': ['4']})
    http.post('/api/rows/remove', json={'keys': ['4']})
    sample(5, 'acetone')

    following = http.post('/api/follow', json={'on': True}).json()

    assert [r['key'] for r in following['rows']] == ['1', '2', '3', '5']
    assert offered(http) == {'4': ''}


def test_rows_the_manager_does_not_take_answer_422_and_none_is_added(
    http: TestClient, batch: Batch
) -> None:
    answer = http.post('/api/rows/take', json={'keys': ['1']})

    assert answer.status_code == 422
    assert (
        answer.json()['message']
        == 'No row was added: rows [\'1\'] are in the batch already.'
    )
    assert list(batch.frame().index) == ['1', '2', '3']


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
    assert batch.columns == ('sample', 'transmission')
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

    assert listed == [{'key': '1', 'text': ''}, {'key': '2', 'text': '3.0'}]


def test_a_column_made_a_setting_whose_table_is_refused_keeps_its_cells(
    http: TestClient, client: Client, refusing: Refusing, definitions: DefinitionStore
) -> None:
    thickness(http, ['1', '2', '3'], 2.5)
    refusing.part = 'rows'

    answer = make_setting(http)

    assert answer.status_code == 422
    batch = started(client, definitions).manager.batch('scan')
    assert batch.columns == ('sample', 'transmission', 'thickness')
    assert batch.frame()['thickness'].tolist() == [2.5, 2.5, 2.5]


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


def test_a_setting_made_a_column_has_its_value_in_every_row(
    http: TestClient, batch: Batch
) -> None:
    make_setting(http)
    http.post('/api/settings', json={'path': ['thickness'], 'op': 'set', 'value': 2.5})
    http.post('/api/settings/apply')

    made = http.post('/api/settings/column', json={'name': 'thickness'}).json()

    assert (
        made['notice'] == 'Thickness is a column: each row has the value in its cell.'
    )
    assert batch.columns == ('sample', 'transmission', 'thickness')
    assert batch.frame()['thickness'].tolist() == [2.5, 2.5, 2.5]
    assert 'thickness' not in batch.settings
    assert [r['cells']['thickness']['text'] for r in made['rows']] == ['2.5'] * 3
    assert made['may_be_columns'] == []


def test_a_group_setting_made_a_column_and_a_setting_again_keeps_its_value(
    http: TestClient, batch: Batch
) -> None:
    bins(http, 5)

    column = http.post('/api/settings/column', json={'name': 'binning'}).json()
    cells = batch.frame()['binning'].tolist()
    setting = http.post('/api/columns/setting', json={'name': 'binning'}).json()

    assert [c['name'] for c in column['columns']][-1] == 'binning'
    assert cells == [{'bins': 5}] * 3
    assert 'binning' not in batch.columns
    assert form_bins(setting) == 5


def test_columns_do_not_change_while_the_form_holds_a_change(
    http: TestClient, batch: Batch
) -> None:
    draft(http, 9)

    answer = http.post('/api/settings/column', json={'name': 'binning'})

    assert answer.status_code == 422
    assert answer.json()['message'] == (
        'Nothing was changed: apply or discard the settings first.'
    )
    assert batch.columns == ('sample', 'transmission', 'thickness')


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
        label = pages.manager.create(FORMS.name, title='Forms', rows=rows)
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


def test_a_form_choose_picks_a_way_of_a_choice(form: TestClient) -> None:
    nodes = settings(form, {'path': ['offset'], 'op': 'choose', 'value': 'Given'})

    offset = nodes['offset',]
    assert offset['chosen'] == 'Given'
    assert [w['label'] for w in offset['ways']] == ['Given', 'Not used']
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
        page = home(demo.build(client, files, definitions))
        before = rows(page)
        demo.measure(tmp_path, groups[0] + groups[1] + groups[2])
        after = page.post('/api/follow', json={'on': True}).json()
        page.post('/api/follow', json={'on': False})
        client.wait(client.records())

    assert [row['name'] for row in before.values()] == ['H2O buffer']
    assert [row['name'] for row in after['rows']] == ['H2O buffer', 'D2O buffer']
    assert [c['name'] for c in after['columns']] == [
        'sample',
        'transmission',
        'thickness',
    ]


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
    batch.put(toys.rows(client))

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
    other = manager.create(STAGED.name, title='Other', rows=toys.rows(client))
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
    pages: Pages, manager: BatchManager
) -> None:
    http = TestClient(create_app(pages), follow_redirects=False)
    for label in ('a', 'b'):
        manager.create(STAGED.name, title=label, source='pairs', label=label)
    pages.manager.follow('b')

    page = http.get('/')

    assert (page.status_code, page.headers['location']) == (307, 'batches/b/')


def test_the_root_redirects_to_the_only_batch(
    pages: Pages, manager: BatchManager
) -> None:
    http = TestClient(create_app(pages), follow_redirects=False)
    manager.create(STAGED.name, title='a', source='pairs', label='a')

    page = http.get('/')

    assert (page.status_code, page.headers['location']) == (307, 'batches/a/')


def test_the_root_lists_several_batches_if_none_follows(
    pages: Pages, manager: BatchManager
) -> None:
    http = TestClient(create_app(pages), follow_redirects=False)
    for label in ('a', 'b'):
        manager.create(STAGED.name, title=label, source='pairs', label=label)

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
    http: TestClient, pages: Pages, manager: BatchManager
) -> None:
    other = manager.create(
        STAGED.name,
        title='Other',
        source='pairs',
        description='The second series',
        label='other',
    )
    pages.manager.follow(other)

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
    http: TestClient, pages: Pages, manager: BatchManager
) -> None:
    other = manager.create(STAGED.name, title='Other', source='pairs')

    listed = http.get('/api/batches').json()

    assert opened(pages, other).get('/api/batches').json() == listed
    assert TestClient(create_app(pages)).get('/api/batches').json() == listed


def test_a_page_learns_from_its_poll_that_another_batch_follows(
    http: TestClient, pages: Pages, manager: BatchManager
) -> None:
    other = opened(pages, manager.create(STAGED.name, title='Other', source='pairs'))
    now = state(http)

    other.post('/api/follow', json={'on': True})

    polled = http.get(
        '/api/state', params={'after': now['version'], 'start': now['start']}
    )
    assert polled.status_code == 200
    listed = polled.json()['batches']
    assert [(b['title'], b['follows']) for b in listed] == [
        ('Other', True),
        ('Scan', False),
    ]
    assert http.get('/api/batches').json()['rows'] == listed


def test_a_correction_on_one_page_shows_in_the_runs_view_of_another(
    http: TestClient, pages: Pages, manager: BatchManager, fake: FakeFiles
) -> None:
    key = fake.measure(4, 1.0, role='sample', sample='oil').file
    other = opened(pages, manager.create(STAGED.name, title='Other', source='pairs'))
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
    bins(http, 3)  # row 1 is done with earlier settings

    counted = http.get('/api/batches/counts').json()

    assert counted == {
        'rows': {'scan': {'done': 1, 'ready': 1, 'incomplete': 1}},
        'failed': {},
    }


def test_the_counts_give_the_rows_of_each_batch_by_status(
    pages: Pages, manager: BatchManager, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    sample(2, 'oil', partner=False)
    manager.create('plain', title='Morning', rows=toys.rows(client), label='a')
    manager.create('plain', title='Afternoon', rows=toys.rows(client), label='b')
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
    first = BatchManager(client, sources=SOURCES, definitions=definitions)
    first.create('plain', title='Fine', rows=toys.rows(client), label='a')
    first.create('plain', title='Broken', rows=toys.rows(client), label='b')
    definitions.save(rows_key('b'), {'nonsense': 1})
    manager = BatchManager(client, sources=SOURCES, definitions=definitions)

    counted = Pages(manager).counts()

    assert list(counted.rows) == ['a']
    assert 'rows.json: not a table' in counted.failed['b']


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
        'columns': ['sample', 'transmission'],
    } | given


def test_the_sources_offer_the_specs_that_fit_and_the_parameters_a_cell_edits(
    pages: Pages,
) -> None:
    pairs, none = TestClient(create_app(pages)).get('/api/sources').json()

    assert pairs == {
        'name': 'pairs',
        'fills': ['sample', 'transmission'],
        'specs': [
            {'name': 'staged', 'title': 'Staged', 'params': STAGED_PARAMS},
            {'name': 'plain', 'title': 'Plain', 'params': STAGED_PARAMS},
        ],
    }
    assert (none['name'], none['fills']) == (None, [])
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
    odd = Source(pair, fills=('sample', 'background'))
    manager = BatchManager(client, sources={'odd': odd}, definitions=definitions)

    pages = Pages(manager)
    offered = TestClient(create_app(pages)).get('/api/sources').json()

    assert [s['name'] for s in offered] == [None]


def test_a_batch_made_from_a_source_has_every_row_it_filled_and_the_columns_sent(
    http: TestClient, pages: Pages, manager: BatchManager
) -> None:
    sent = new_batch(
        description='Thicker cells', columns=['sample', 'transmission', 'thickness']
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


def test_a_batch_made_from_the_page_starts_with_the_settings_of_its_spec(
    client: Client, definitions: DefinitionStore
) -> None:
    settings = {STAGED.name: {'binning': {'bins': 5}}}
    manager = BatchManager(
        client, sources=SOURCES, definitions=definitions, settings=settings
    )
    pages = Pages(manager)

    made = TestClient(create_app(pages)).post('/api/batches', json=new_batch())

    assert form_bins(state(opened(pages, made.json()['label']))) == 5


def test_a_batch_without_a_source_is_made_with_the_columns_sent_and_no_rows(
    pages: Pages, manager: BatchManager, sample: Measure
) -> None:
    sample(1, 'water')
    sent = new_batch(source=None, spec='find', columns=['sample'])

    made = TestClient(create_app(pages)).post('/api/batches', json=sent).json()

    batch = manager.batch(made['label'])
    assert (batch.spec.name, batch.columns, len(batch.frame())) == (
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
        ('3', 'oil', 'waits'),
        ('4', 'ethanol', 'taken'),
    ]
    assert [c['name'] for c in listed['columns']] == ['run']
    assert listed['rows'][0]['cells'] == {'run': '1'}


@pytest.mark.parametrize(
    ('first', 'last', 'taken'),
    [('2', None, ['2', '4']), (None, '2', ['1', '2']), ('2', '2', ['2'])],
    ids=['from', 'to', 'one'],
)
def test_a_batch_made_from_a_range_of_a_source_has_its_rows_filled_between_the_ends(
    http: TestClient,
    manager: BatchManager,
    sample: Measure,
    first: str | None,
    last: str | None,
    taken: list[str],
) -> None:
    sample(4, 'ethanol')

    made = http.post('/api/batches', json=new_batch(first=first, last=last)).json()

    assert list(manager.batch(made['label']).frame().index) == taken


@pytest.mark.parametrize(
    ('ends', 'said'),
    [
        ({'first': '3'}, '3 is not a filled row'),
        ({'first': '2', 'last': '1'}, 'the row 2 comes after the row 1'),
    ],
    ids=['waits', 'reversed'],
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


def test_a_batch_made_from_the_page_of_a_batch_starts_with_its_settings(
    http: TestClient, pages: Pages
) -> None:
    bins(http, 7)

    made = http.post('/api/batches', json=new_batch(settings_from='scan')).json()

    assert form_bins(state(opened(pages, made['label']))) == 7


def test_a_setting_of_the_batch_that_is_a_column_of_the_new_one_fills_its_cells(
    http: TestClient, manager: BatchManager
) -> None:
    make_setting(http)
    http.post('/api/settings', json={'path': ['thickness'], 'op': 'set', 'value': 2.5})
    http.post('/api/settings/apply')
    columns = ['sample', 'transmission', 'thickness']

    made = http.post(
        '/api/batches', json=new_batch(settings_from='scan', columns=columns)
    ).json()

    batch = manager.batch(made['label'])
    assert batch.columns == tuple(columns)
    assert batch.frame()['thickness'].tolist() == [2.5, 2.5]


def from_table(http: TestClient, text: str, **given: Any) -> Any:
    """Make a batch from a table file ``rows.csv`` that holds ``text``."""
    query = {'spec': STAGED.name, 'title': 'From a file', 'name': 'rows.csv'} | given
    return http.post('/api/batches/table', params=query, content=text.encode())


def test_a_batch_made_from_a_table_file_has_its_rows_and_columns_and_no_source(
    http: TestClient, manager: BatchManager
) -> None:
    text = 'key,run,sample,transmission\na,1,run:1,run:1001\nb,2,run:2,run:1002\n'

    made = from_table(http, text, settings_from='scan').json()

    batch = manager.batch(made['label'])
    assert batch.columns == ('sample', 'transmission')
    assert list(batch.frame().index) == ['a', 'b']
    assert manager.source(made['label']) is None
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
        ({'source': 'other'}, 'the source other is not registered'),
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
    pages: Pages, manager: BatchManager
) -> None:
    http = TestClient(create_app(pages), follow_redirects=False)

    page = http.get('/')
    manager.create(STAGED.name, title='First', source='pairs')
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
    first = BatchManager(client, sources=SOURCES, definitions=definitions)
    label = first.create(STAGED.name, title='Odd')
    definitions.save(rows_key(label), {'nonsense': 1})
    manager = BatchManager(client, sources=SOURCES, definitions=definitions)
    pages = Pages(manager)

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
        {'message': "The change was not kept: ('labels', 'scan', 'header'): refused"},
    )
    assert manager.headers()['scan'].title == 'Scan'
    after = state(http)
    assert (after['title'], after['header'], after['rows']) == (
        before['title'],
        before['header'],
        before['rows'],
    )


def test_follow_the_definitions_refuse_answers_422_and_follow_stays_off(
    http: TestClient, manager: BatchManager, refusing: Refusing
) -> None:
    refusing.part = 'following'

    answer = http.post('/api/follow', json={'on': True})

    assert (answer.status_code, answer.json()) == (
        422,
        {'message': "The change was not kept: ('following',): refused"},
    )
    assert manager.following is None
    assert state(http)['follow']['on'] is False


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

    assert removed.status_code == 200
    assert manager.headers() == {}
    assert client.records(label='scan') != []
    gone = http.get('/api/state')
    assert (gone.status_code, gone.json()) == (
        404,
        {'message': 'There is no batch scan.'},
    )


def test_removing_the_batch_that_follows_turns_follow_off(
    http: TestClient, pages: Pages
) -> None:
    pages.manager.follow('scan')

    http.post('/api/batch/remove')

    assert pages.manager.following is None


def test_a_removal_the_definitions_refuse_answers_422_and_the_batch_stays(
    http: TestClient, manager: BatchManager, refusing: Refusing
) -> None:
    manager.follow('scan')
    refusing.part = 'header'

    answer = http.post('/api/batch/remove')

    assert answer.status_code == 422
    assert answer.json()['message'] == (
        "The change was not kept: ('labels', 'scan', 'header'): refused"
    )
    # It follows no longer, so that no label without a header follows.
    assert list(manager.headers()) == ['scan']
    assert manager.following is None


def test_another_page_of_a_removed_batch_answers_404_at_its_next_poll(
    http: TestClient, pages: Pages
) -> None:
    other = opened(pages, 'scan')
    now = state(other)

    http.post('/api/batch/remove')

    at = {'after': now['version'], 'start': now['start']}
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

    with pytest.raises(Missing, match=r'There is no batch scan\.'):
        page.poll(0)
    with pytest.raises(Missing):
        page.relist()


def test_a_batch_made_again_under_a_removed_label_gets_a_new_page(
    http: TestClient, pages: Pages, manager: BatchManager
) -> None:
    page = pages.page('scan')

    pages.remove('scan')
    manager.create(STAGED.name, title='Again', source='pairs', label='scan')

    assert pages.page('scan') is not page
    assert state(http)['title'] == 'Again'
