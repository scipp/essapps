# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The batch page's API over HTTP, on a real in-process workspace."""

import io
import zipfile
from collections.abc import Mapping
from typing import Any

import numpy as np
import pytest
import scipp as sc
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from ess.apps import Client, Listing, Record
from ess.apps.batch import Batch, CorrectedFiles
from ess.apps.batch.web import create_app, forms, plots
from ess.apps.fields import Fields
from ess.apps.testing import FakeFiles

from .toys import Gate, Measure

BINS = ['binning', 'Given', 'bins']
"""The path of the field of the number of bins in the settings form."""


class Marks(BaseModel):
    role: str | None = None


@pytest.fixture
def http(batch: Batch, files: CorrectedFiles, sample: Measure) -> TestClient:
    sample(1, 'water')
    sample(2, 'sds')
    sample(3, 'oil', partner=False)
    batch.scan()

    def draw(records: Mapping[str, Record]) -> dict[str, Any]:
        return {'drawn': sorted(records)}

    app = create_app(batch, info=('name',), draw=draw, files=files, marks=Marks)
    return TestClient(app)


def state(http: TestClient) -> dict[str, Any]:
    return http.get('/api/state').json()


def rows(http: TestClient) -> dict[str, dict[str, Any]]:
    return {row['key']: row for row in state(http)['rows']}


def statuses(http: TestClient) -> dict[str, str]:
    return {key: row['status'] for key, row in rows(http).items()}


def reduced(http: TestClient, client: Client) -> None:
    assert http.post('/api/reduce', json={'which': 'remaining'}).status_code == 200
    client.wait(client.records())


def test_a_scan_lists_the_rows_with_their_info_and_why_one_is_incomplete(
    http: TestClient,
) -> None:
    shown = rows(http)

    assert {k: r['name'] for k, r in shown.items()} == {
        '1': 'water',
        '2': 'sds',
        '3': 'oil',
    }
    assert shown['1']['sample:text'] == 'run 1'
    assert statuses(http) == {'1': 'ready', '2': 'ready', '3': 'incomplete'}
    assert shown['3']['problems'] == {'transmission': 'missing'}
    assert state(http)['action']['label'] == 'Reduce 2 remaining'


def test_a_poll_at_the_same_version_answers_with_the_version_alone(
    http: TestClient,
) -> None:
    version = state(http)['version']

    assert http.get('/api/state', params={'after': version}).json() == {
        'version': version
    }


def test_an_edit_writes_a_cell_and_marks_it_as_set_by_hand(
    http: TestClient, batch: Batch
) -> None:
    edit = {'keys': ['1'], 'column': 'thickness', 'value': 3}

    assert http.post('/api/cells', json=edit).json() == {'notice': ''}
    assert batch.rows['1'].values['thickness'] == 3.0
    assert rows(http)['1']['edited'] == ['thickness']


def test_an_edit_of_several_rows_writes_each_and_says_so(
    http: TestClient, batch: Batch
) -> None:
    edit = {'keys': ['1', '2'], 'column': 'thickness', 'value': 5.0}

    notice = http.post('/api/cells', json=edit).json()['notice']

    assert [batch.rows[k].values['thickness'] for k in ('1', '2')] == [5.0, 5.0]
    assert notice == 'Thickness set on 2 samples.'


def test_a_file_cell_is_set_by_the_label_of_its_option(
    http: TestClient, fake: FakeFiles
) -> None:
    column = next(c for c in state(http)['columns'] if c['name'] == 'transmission')
    label = next(o for o in column['editor']['options'] if o.startswith('run 1001'))

    http.post(
        '/api/cells', json={'keys': ['3'], 'column': 'transmission', 'value': label}
    )

    assert statuses(http)['3'] == 'ready'
    assert rows(http)['3']['transmission:text'] == 'run 1001'


def test_a_cell_set_back_takes_what_the_scan_gave(
    http: TestClient, batch: Batch
) -> None:
    scanned = batch.rows['1'].values['transmission']
    http.post('/api/cells', json={'keys': ['1'], 'column': 'thickness', 'value': 2})
    http.post('/api/cells', json={'keys': ['1'], 'column': 'transmission'})
    assert statuses(http)['1'] == 'incomplete'

    http.post('/api/cells/revert', json={'keys': ['1'], 'column': 'transmission'})

    assert batch.rows['1'].values['transmission'] == scanned
    assert batch.rows['1'].values['thickness'] == 2.0


def test_a_refused_reduce_names_the_cell_with_422(http: TestClient) -> None:
    http.post('/api/cells', json={'keys': ['1'], 'column': 'thickness', 'value': -1})

    response = http.post('/api/reduce', json={'which': 'selected', 'keys': ['1']})

    assert response.status_code == 422
    (error,) = response.json()['errors']
    assert (error['entry'], error['loc'], error['placed']) == ('1', ['thickness'], True)
    assert 'greater than' in rows(http)['1']['problems']['thickness']


def test_reduce_then_the_state_shows_the_rows_done(
    http: TestClient, client: Client
) -> None:
    reduced(http, client)

    assert statuses(http) == {'1': 'done', '2': 'done', '3': 'incomplete'}
    assert state(http)['action']['label'] == 'Nothing left to reduce'


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


def test_a_changed_setting_makes_the_rows_done_outdated(
    http: TestClient, client: Client
) -> None:
    reduced(http, client)

    http.post('/api/settings', json={'path': BINS, 'op': 'set', 'value': 3})

    assert statuses(http) == {'1': 'outdated', '2': 'outdated', '3': 'incomplete'}
    assert state(http)['action']['label'] == 'Reduce 2 outdated'


def test_a_setting_refused_is_marked_at_its_field(http: TestClient) -> None:
    http.post('/api/settings', json={'path': BINS, 'op': 'set', 'value': 0})

    response = http.post('/api/reduce', json={'which': 'remaining'})

    assert response.status_code == 422
    (binning, *_) = state(http)['settings']
    assert 'greater than' in binning['child']['children'][0]['problem']


def test_the_table_downloaded_and_uploaded_gives_the_same_rows(
    http: TestClient, batch: Batch, client: Client
) -> None:
    http.post('/api/cells', json={'keys': ['2'], 'column': 'thickness', 'value': 4})
    content = http.get('/api/table.csv').content
    other = Batch(client, batch.spec, columns=batch.columns, label='other')

    TestClient(create_app(other)).post(
        '/api/table', params={'name': 'samples.csv'}, content=content
    )

    assert [(r.key, r.values) for r in other.rows] == [
        (r.key, r.values) for r in batch.rows
    ]


def test_the_settings_downloaded_and_uploaded_are_the_same(
    http: TestClient,
) -> None:
    content = http.get('/api/settings.yaml').content
    http.post('/api/settings', json={'path': BINS, 'op': 'set', 'value': 9})

    http.post('/api/settings.yaml', params={'name': 'mine.yaml'}, content=content)

    (binning, *_) = state(http)['settings']
    assert binning['child']['children'][0]['value'] == 2


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


def test_the_plot_draws_the_results_of_the_rows_asked_for(
    http: TestClient, client: Client
) -> None:
    reduced(http, client)

    assert http.get('/api/plot', params={'keys': ['2', '3']}).json() == {
        'figure': {'drawn': ['2']},
        'note': '',
    }
    assert http.get('/api/plot').json()['figure'] == {'drawn': ['1', '2']}


def test_a_row_shows_what_its_result_was_reduced_with_and_why_it_failed(
    http: TestClient, client: Client
) -> None:
    http.post('/api/settings', json={'path': BINS, 'op': 'set', 'value': 1})
    reduced(http, client)
    http.post('/api/settings', json={'path': BINS, 'op': 'set', 'value': 2})

    detail = http.get('/api/rows/1').json()

    assert detail['differs'] == ['Binning']
    assert rows(http)['1']['status'] == 'outdated'
    http.post('/api/rows/1/use-settings')
    assert rows(http)['1']['status'] == 'failed'
    assert 'one bin' in rows(http)['1']['reason']


def test_a_tweak_becomes_the_result_of_its_row(
    http: TestClient, client: Client
) -> None:
    reduced(http, client)
    edit = {'path': ['thickness'], 'op': 'set', 'value': 4.0}

    detail = http.post('/api/rows/1/tweak', json=edit).json()
    http.post('/api/rows/1/tweak/submit')
    client.wait(client.records())

    thickness = next(n for n in detail['tweak'] if n['path'] == ['thickness'])
    assert thickness['value'] == 4.0
    assert statuses(http)['1'] == 'done'
    assert client.latest('scan', '1').request.params['thickness'] == 4.0


def test_a_correction_of_a_run_reaches_the_rows(
    http: TestClient, fake: FakeFiles
) -> None:
    run = fake.measure(4, 1.0, role='sample', sample='oil')
    http.post('/api/scan')
    key = run.file

    http.post(
        '/api/runs/cells',
        json={'keys': [key], 'column': 'role', 'value': 'transmission'},
    )

    assert statuses(http)['3'] == 'ready'
    (corrected,) = [r for r in state(http)['runs']['rows'] if r['key'] == key]
    assert corrected['edited'] == ['role']


# The forms without HTTP


class Range(BaseModel):
    start: float = Field(default=1.0, title='Start (Å)')
    banks: list[int] = [1, 2]


class Settings(BaseModel):
    range: Range = Field(default_factory=Range)
    offset: Range | None = None


@pytest.fixture
def fields(client: Client) -> Fields:
    return Fields(Settings, Listing(client))


def test_a_form_set_writes_an_input(fields: Fields) -> None:
    forms.apply(fields, forms.Edit(path=['range', 'Given', 'start'], op='set', value=2))

    assert fields.params['range']['start'] == 2.0


def test_a_form_choose_picks_a_way(fields: Fields) -> None:
    forms.apply(fields, forms.Edit(path=['offset'], op='choose', value='Given'))

    (_, offset) = forms.tree(fields)
    assert offset['chosen'] == 'Given'
    assert [w['label'] for w in offset['ways']] == ['Given', 'Not used']
    assert offset['child']['kind'] == 'group'


@pytest.mark.parametrize(('op', 'banks'), [('add', [1, 2, None]), ('remove', [1])])
def test_a_form_adds_and_removes_elements_of_a_list(
    fields: Fields, op: str, banks: list[int | None]
) -> None:
    forms.apply(fields, forms.Edit(path=['range', 'Given', 'banks'], op=op))

    (range_, _) = forms.tree(fields)
    elements = range_['child']['children'][1]['elements']
    assert [e['value'] for e in elements] == banks


def test_a_form_edit_at_no_field_is_refused(fields: Fields) -> None:
    with pytest.raises(KeyError):
        forms.apply(fields, forms.Edit(path=['range', 'nothing'], op='set', value=1))


# The plot


def test_a_figure_has_error_bars_from_variances_and_axes_with_units() -> None:
    curve = sc.DataArray(
        sc.array(dims=['Q'], values=[4.0, 1.0], variances=[4.0, 1.0], unit='1/cm'),
        coords={'Q': sc.array(dims=['Q'], values=[0.1, 0.2, 0.3], unit='1/angstrom')},
    )

    figure = plots.figure({'water': curve}, y_name='I(Q)')

    (trace,) = figure['data']
    assert trace['name'] == 'water'
    assert np.allclose(trace['x'], [0.15, 0.25])
    assert trace['error_y']['array'] == [2.0, 1.0]
    assert figure['layout']['xaxis']['title']['text'] == 'Q [1/Å]'
    assert figure['layout']['yaxis']['title']['text'] == 'I(Q) [1/cm]'
    assert figure['layout']['xaxis']['type'] == 'log'
