# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Known defects of the batch page: each test states the behaviour wanted.
Each is marked as a strict ``xfail`` that says what happens instead.
A fix moves its test next to the tests of the same behaviour and removes the marker.
"""

from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import BaseModel

from ess.apps import Client
from ess.apps.batch import Batch, CorrectedFiles
from ess.apps.batch.web import create_app
from ess.apps.testing import FakeFiles
from ess.spec import FileRef

from .toys import Gate, Measure

AT = frozenset({'sample', 'transmission'})


class Marks(BaseModel):
    role: str | None = None
    sample: str | None = None


@pytest.fixture
def http(batch: Batch, files: CorrectedFiles, sample: Measure) -> TestClient:
    sample(1, 'water')
    batch.scan()
    return TestClient(create_app(batch, name='name', files=files, marks=Marks, at=AT))


def half_written(selector: Any) -> Any:
    """A file source's ``list`` while its list of files is being written."""
    raise OSError('half written')


@pytest.mark.xfail(
    strict=True,
    reason='D2: the scan after the correction fails with an OSError, which the '
    'page reports as "The change was not kept" although the correction was kept.',
)
def test_a_correction_whose_scan_fails_says_that_it_was_kept(
    http: TestClient, files: CorrectedFiles, fake: FakeFiles
) -> None:
    (run, *_) = http.get('/api/runs').json()['rows']
    fake.list = half_written

    edit = {'keys': [run['key']], 'column': 'sample', 'value': 'oil'}
    answer = http.post('/api/runs/cells', json=edit)

    assert dict(files.corrections(FileRef(file=run['key']))) == {'sample': 'oil'}
    assert answer.status_code == 422
    assert answer.json()['message'].startswith('Corrected, but the scan failed')


@pytest.mark.xfail(
    strict=True,
    reason='D3: the values of the file before the refused one stay in the form, '
    'and the next edit of any setting writes them to the batch.',
)
def test_a_refused_settings_file_leaves_the_settings_as_they_were(
    http: TestClient, batch: Batch
) -> None:
    before = dict(batch.settings)
    template = yaml.safe_load(http.get('/api/settings.yaml').content)
    template['params'] = {'binning': {'bins': 5}, 'offset': 'nonsense'}

    loaded = http.post(
        '/api/settings.yaml',
        params={'name': 'f'},
        content=yaml.safe_dump(template).encode(),
    )
    edit = {'path': ['offset', 'value'], 'op': 'set', 'value': 2}
    http.post('/api/settings', json=edit)

    assert loaded.status_code == 422
    assert batch.settings == {**before, 'offset': {'value': 2.0}}


@pytest.mark.xfail(
    strict=True,
    reason='D6: "still running" stays in the refusals after the row is done, '
    'until a reduction is accepted.',
)
def test_a_refusal_of_a_running_row_goes_once_the_row_is_done(
    http: TestClient, client: Client, gate: Gate
) -> None:
    gate.close()
    on = http.post('/api/follow', json={'on': True}).json()
    refused = http.post('/api/reduce', json={'which': 'selected', 'keys': ['1']})
    gate.open()
    client.wait(client.records())

    after = http.get('/api/state').json()

    assert [row['status'] for row in on['rows']] == ['running']
    assert refused.status_code == 422
    assert [row['status'] for row in after['rows']] == ['done']
    assert after['refused'] == []
