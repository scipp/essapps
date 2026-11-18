# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Known defects of the batch page: each test states the behaviour wanted.
Each is marked as a strict ``xfail`` that says what happens instead.
A fix moves its test next to the tests of the same behaviour and removes the marker.
"""

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import BaseModel

from ess.apps.batch import Batch, CorrectedFiles
from ess.apps.batch.web import create_app

from .toys import Measure

AT = frozenset({'sample', 'transmission'})


class Marks(BaseModel):
    role: str | None = None
    sample: str | None = None


@pytest.fixture
def http(batch: Batch, files: CorrectedFiles, sample: Measure) -> TestClient:
    sample(1, 'water')
    batch.scan()
    return TestClient(create_app(batch, name='name', files=files, marks=Marks, at=AT))


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
