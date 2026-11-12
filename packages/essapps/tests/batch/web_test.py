# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The batch page's API over HTTP, on a real in-process workspace."""

import io
import zipfile
from collections.abc import Sequence
from typing import Any

import numpy as np
import pytest
import scipp as sc
import yaml
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from ess.apps import Client
from ess.apps.batch import Batch, CorrectedFiles
from ess.apps.batch.web import Shown, create_app, plots
from ess.apps.testing import FakeFiles
from ess.spec import SampleRunFile, WorkflowSpec

from .toys import Gate, Measure

BINS = ['binning', 'bins']
"""The path of the number of bins in the settings form."""


class Marks(BaseModel):
    role: str | None = None


def draw(shown: Sequence[Shown]) -> dict[str, Any]:
    return {'drawn': [s.key for s in shown], 'colors': [s.color for s in shown]}


@pytest.fixture
def http(batch: Batch, files: CorrectedFiles, sample: Measure) -> TestClient:
    sample(1, 'water')
    sample(2, 'sds')
    sample(3, 'oil', partner=False)
    batch.scan()
    app = create_app(batch, name='name', draw=draw, files=files, marks=Marks)
    return TestClient(app)


def state(http: TestClient) -> dict[str, Any]:
    return http.get('/api/state').json()


def rows(http: TestClient) -> dict[str, dict[str, Any]]:
    return {row['key']: row for row in state(http)['rows']}


def statuses(http: TestClient) -> dict[str, str]:
    return {key: row['status'] for key, row in rows(http).items()}


def thickness(http: TestClient, keys: list[str], value: Any) -> dict[str, Any]:
    edit = {'keys': keys, 'column': 'thickness', 'value': value}
    return http.post('/api/cells', json=edit).json()


def reduced(http: TestClient, client: Client, which: str = 'remaining') -> None:
    assert http.post('/api/reduce', json={'which': which}).status_code == 200
    client.wait(client.records())


def test_a_scan_lists_the_rows_with_their_names_and_why_one_is_incomplete(
    http: TestClient,
) -> None:
    shown = rows(http)

    assert {k: r['name'] for k, r in shown.items()} == {
        '1': 'water',
        '2': 'sds',
        '3': 'oil',
    }
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


def test_an_action_answers_with_the_state_after_it(http: TestClient) -> None:
    after = thickness(http, ['1'], 3)

    cell = rows(http)['1']['cells']['thickness']
    assert after['rows'][0]['cells']['thickness'] == cell
    assert (cell['text'], cell['edited'], cell['scanned']) == ('3.0', True, '')


def test_an_edit_of_several_rows_writes_each_and_says_so(
    http: TestClient, batch: Batch
) -> None:
    notice = thickness(http, ['1', '2'], 5.0)['notice']

    assert [batch.rows[k].values['thickness'] for k in ('1', '2')] == [5.0, 5.0]
    assert notice == 'Thickness set on 2 rows.'


def test_a_file_cell_is_set_by_the_label_of_its_option(http: TestClient) -> None:
    column = next(c for c in state(http)['columns'] if c['name'] == 'transmission')
    label = next(o for o in column['editor']['options'] if o.startswith('run 1001'))

    edit = {'keys': ['3'], 'column': 'transmission', 'value': label}
    http.post('/api/cells', json=edit)

    assert statuses(http)['3'] == 'ready'
    assert rows(http)['3']['cells']['transmission']['text'] == 'run 1001'


def test_a_cell_set_back_takes_what_the_scan_gave_and_the_others_stay(
    http: TestClient, batch: Batch
) -> None:
    scanned = batch.rows['1'].values['transmission']
    thickness(http, ['1'], 2)
    http.post('/api/cells', json={'keys': ['1'], 'column': 'transmission'})
    assert statuses(http)['1'] == 'incomplete'

    http.post('/api/cells/revert', json={'keys': ['1'], 'column': 'transmission'})

    assert batch.rows['1'].values['transmission'] == scanned
    assert batch.rows['1'].values['thickness'] == 2.0


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
    http: TestClient,
) -> None:
    thickness(http, ['1'], -1)

    response = http.post('/api/reduce', json={'which': 'selected', 'keys': ['1']})

    assert response.status_code == 422
    assert response.json() == {'message': 'Nothing was reduced: one value is refused.'}
    shown = state(http)
    (refused,) = shown['refused']
    assert refused['text'] == 'water: Thickness must be greater than or equal to 0'
    assert refused['placed']
    problem = shown['rows'][0]['cells']['thickness']['problem']
    assert problem == 'Thickness must be greater than or equal to 0'


def test_a_refused_value_stays_marked_until_it_is_edited(http: TestClient) -> None:
    thickness(http, ['1'], -1)
    http.post('/api/reduce', json={'which': 'selected', 'keys': ['1']})

    thickness(http, ['2'], 3)
    assert rows(http)['1']['cells']['thickness']['problem']
    thickness(http, ['1'], 3)
    assert state(http)['refused'] == []


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


def test_a_changed_setting_makes_the_rows_done_outdated(
    http: TestClient, client: Client
) -> None:
    reduced(http, client)

    http.post('/api/settings', json={'path': BINS, 'op': 'set', 'value': 3})

    assert statuses(http) == {'1': 'outdated', '2': 'outdated', '3': 'incomplete'}
    assert state(http)['action']['label'] == 'Reduce 2 outdated'
    assert rows(http)['1']['reason'] == 'Binning changed.'


def test_a_failed_row_stays_failed_when_the_settings_change(
    http: TestClient, client: Client
) -> None:
    http.post('/api/settings', json={'path': BINS, 'op': 'set', 'value': 1})
    reduced(http, client)

    http.post('/api/settings', json={'path': BINS, 'op': 'set', 'value': 2})

    assert statuses(http)['1'] == 'failed'
    assert rows(http)['1']['reason'] == 'one bin'
    assert state(http)['action']['label'] == 'Retry 2 failed'


def test_incomplete_settings_block_reducing_and_no_result_is_current(
    http: TestClient, client: Client
) -> None:
    reduced(http, client)

    http.post(
        '/api/settings',
        json={'path': ['offset'], 'op': 'choose', 'value': 'From a record'},
    )

    shown = state(http)
    assert shown['settings_problem'] == 'Offset: choose a result'
    assert shown['action']['blocked'] == 'Offset: choose a result'
    assert {r['status'] for r in shown['rows'][:2]} == {'outdated'}


def test_a_setting_refused_is_marked_at_its_field(http: TestClient) -> None:
    http.post('/api/settings', json={'path': BINS, 'op': 'set', 'value': 0})

    assert http.post('/api/reduce', json={'which': 'remaining'}).status_code == 422

    (binning, *_) = state(http)['settings']
    assert binning['child']['children'][0]['path'] == BINS
    assert 'greater than' in binning['child']['children'][0]['problem']


@pytest.mark.parametrize('suffix', ['csv', 'xlsx'])
def test_the_table_downloaded_and_uploaded_gives_the_same_rows(
    http: TestClient, batch: Batch, client: Client, suffix: str
) -> None:
    thickness(http, ['2'], 4)
    content = http.get(f'/api/table.{suffix}').content
    other = Batch(client, batch.spec, columns=batch.columns, label='other')

    TestClient(create_app(other)).post(
        '/api/table', params={'name': f'rows.{suffix}'}, content=content
    )

    assert [(r.key, r.values) for r in other.rows] == [
        (r.key, r.values) for r in batch.rows
    ]


def test_a_table_loaded_replaces_what_was_set_by_hand_until_a_rescan(
    http: TestClient,
) -> None:
    thickness(http, ['1'], 4)
    content = http.get('/api/table.csv').content

    loaded = http.post('/api/table', params={'name': 'rows.csv'}, content=content)

    assert loaded.json()['loaded'] == 'rows.csv'
    assert rows(http)['1']['cells']['thickness'] == {
        'value': 4.0,
        'text': '4.0',
        'edited': False,
        'scanned': '4.0',
        'problem': None,
    }
    scanned = http.post('/api/scan').json()
    assert scanned['loaded'] is None
    assert scanned['notice'] == 'Found 3 rows. 1 row changed.'
    assert rows(http)['1']['cells']['thickness']['text'] == ''


def test_the_settings_downloaded_and_uploaded_are_the_same(
    http: TestClient,
) -> None:
    content = http.get('/api/settings.yaml').content
    http.post('/api/settings', json={'path': BINS, 'op': 'set', 'value': 9})

    http.post('/api/settings.yaml', params={'name': 'mine.yaml'}, content=content)

    (binning, *_) = state(http)['settings']
    assert binning['child']['children'][0]['value'] == 2


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
        template['params']['binning']['bins'] = 'two'
        content = yaml.safe_dump(template).encode()

    response = http.post('/api/settings.yaml', params={'name': 'f'}, content=content)

    assert response.json() == {'message': f'Cannot load f: {message}'}


def test_the_results_of_the_rows_done_download_as_a_zip(
    http: TestClient, client: Client
) -> None:
    reduced(http, client)

    content = http.get('/api/results.zip', params={'keys': ['1', '3']}).content

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

    plot = http.get('/api/plot', params={'keys': ['2', '3']}).json()

    assert plot['figure'] == {'drawn': ['2'], 'colors': [plots.PALETTE[1]]}
    assert plot['colors'] == {'2': plots.PALETTE[1]}
    assert http.get('/api/plot').json()['figure']['drawn'] == ['1', '2']


def test_no_two_curves_of_a_figure_share_a_colour() -> None:
    n = len(plots.PALETTE)
    first, second, third = plots.PALETTE[:3]

    assert plots.colors([1, n + 1, 2]) == [second, first, third]
    assert len(set(plots.colors(range(3 * n)))) == 3 * n


def test_a_row_shows_which_settings_its_result_was_reduced_with(
    http: TestClient, client: Client
) -> None:
    reduced(http, client)
    http.post('/api/settings', json={'path': BINS, 'op': 'set', 'value': 5})

    detail = http.get('/api/row', params={'key': '1'}).json()
    assert detail['differs'] == ['binning']
    http.post('/api/row/use-settings', json={'key': '1'})

    assert statuses(http)['1'] == 'done'


def test_a_reduction_of_one_row_alone_becomes_its_result(
    http: TestClient, client: Client
) -> None:
    reduced(http, client)
    edit = {'key': '1', 'path': ['thickness'], 'op': 'set', 'value': 4.0}

    detail = http.post('/api/row/tweak', json=edit).json()
    http.post('/api/row/tweak/submit', json={'key': '1'})
    client.wait(client.records())

    (field,) = [n for n in detail['tweak'] if n['path'] == ['thickness']]
    assert field['value'] == 4.0
    assert (rows(http)['1']['status'], rows(http)['1']['alone']) == ('done', True)
    assert client.latest('scan', '1').request.params['thickness'] == 4.0


def test_a_row_added_by_hand_is_gone_for_good_when_removed(
    http: TestClient,
) -> None:
    http.post('/api/rows/add', json={'key': 'extra'})
    assert rows(http)['extra']['added']
    same = http.post('/api/rows/add', json={'key': 'water'})
    assert same.json() == {'message': 'There is a row named water already.'}

    after = http.post('/api/rows/remove', json={'keys': ['extra', '2']}).json()

    assert after['notice'] == 'Removed 2 rows. 1 added by hand cannot be brought back.'
    assert after['removed'] == 1
    restored = http.post('/api/rows/restore').json()
    assert [r['key'] for r in restored['rows']] == ['1', '2', '3']


def test_a_correction_of_a_run_reaches_the_rows_and_can_be_taken_back(
    http: TestClient, fake: FakeFiles
) -> None:
    key = fake.measure(4, 1.0, role='sample', sample='oil').file
    http.post('/api/scan')
    runs = state(http)['runs']

    edit = {'keys': [key], 'column': 'role', 'value': 'transmission'}
    http.post('/api/runs/cells', json=edit)

    assert statuses(http)['3'] == 'ready'
    (cell,) = [
        r['cells']['role']
        for r in http.get('/api/runs').json()['rows']
        if r['key'] == key
    ]
    assert (cell['text'], cell['edited'], cell['scanned']) == (
        'transmission',
        True,
        'sample',
    )
    assert state(http)['runs'] > runs
    reverted = http.post(
        '/api/runs/cells/revert', json={'keys': [key], 'column': 'role'}
    ).json()
    assert statuses(http)['3'] == 'incomplete'
    # Run 4 is a sample again: of a row of its own, also named oil.
    assert reverted['notice'] == '2 rows changed. oil and oil are now incomplete.'


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
def form(client: Client) -> TestClient:
    batch = Batch(client, FORMS, columns=('sample',), label='forms')
    return TestClient(create_app(batch))


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
