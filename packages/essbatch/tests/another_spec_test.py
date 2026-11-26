# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""A batch on a folder whose records are of another spec than the one bound."""

from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pydantic
import pytest
from ess.batch import Batch

from ess.apps import Client, Listing
from ess.apps.folder import open_folder
from ess.spec import Binding, WorkflowSpec, function_binding


class Centre(pydantic.BaseModel):
    x: float


class Found(pydantic.BaseModel):
    centre: Centre


class Renamed(pydantic.BaseModel):
    """``Centre`` after ``x`` was renamed, with a default, under the same version."""

    x_mm: float = 0.0


class FoundRenamed(pydantic.BaseModel):
    centre: Renamed


class Bins(pydantic.BaseModel):
    bins: int = 4


def _centre(
    outputs: type[pydantic.BaseModel] = Found, version: int = 1
) -> WorkflowSpec:
    return WorkflowSpec(
        name='centre',
        version=version,
        title='Centre',
        description='A toy.',
        params=Bins,
        outputs=outputs,
    )


def _find(params: Bins) -> Found:
    return Found(centre=Centre(x=0.5))


def _find_renamed(params: Bins) -> FoundRenamed:
    return FoundRenamed(centre=Renamed(x_mm=500.0))


CENTRE = _centre()
WRITTEN = [function_binding(CENTRE, _find)]
OTHERS: dict[str, list[Binding]] = {
    'renamed': [function_binding(_centre(FoundRenamed), _find_renamed)],
    'bumped': [function_binding(_centre(version=2), _find)],
}
"""The bindings of another centre spec than the one ``WRITTEN`` binds."""


@pytest.fixture
def batch_of(tmp_path: Path) -> Callable[[list[Binding]], Batch]:
    """A batch of row ``5`` of the folder reopened with the bindings given."""
    (tmp_path / 'files.csv').write_text('file,run\n')

    def make(bindings: list[Binding]) -> Batch:
        client: Client = open_folder(tmp_path, bindings)
        rows = pd.DataFrame({'bins': [5]}, index=['5'])
        return Batch(client, bindings[0].spec, label='c', rows=rows)

    with open_folder(tmp_path, WRITTEN) as client:
        client.submit(CENTRE, params={'bins': 5}, label='c', member='5', persist=True)
    return make


@pytest.mark.parametrize('another', sorted(OTHERS))
def test_a_batch_shows_a_row_whose_result_is_of_another_spec_as_outdated(
    batch_of: Callable[[list[Binding]], Batch], another: str
) -> None:
    batch = batch_of(OTHERS[another])
    try:
        Listing(batch.client)  # what the batch's views list
        state = batch.state()['5']
        assert state.record is not None
        assert state.spec_changed
        assert state.status == 'outdated'
    finally:
        batch.client.close()
