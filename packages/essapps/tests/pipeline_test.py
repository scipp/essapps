# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""A sciline pipeline behind a spec, in plain requests and through stages."""

from collections.abc import Iterator
from typing import NewType

import pytest
import sciline
from ess.reduce.spec import Array, NexusFile, WorkflowSpec
from pydantic import BaseModel

from ess.apps import Backend, Client, Status, Template, dataset
from ess.apps.pipeline import PipelineBinding
from ess.apps.testing import FakeDatasets

Run = NewType('Run', float)
Loaded = NewType('Loaded', float)
Offset = NewType('Offset', float)
Note = NewType('Note', str)
Shifted = NewType('Shifted', float)


def shift(loaded: Loaded, offset: Offset) -> Shifted:
    return Shifted(loaded + offset)


class ShiftParams(BaseModel):
    run: NexusFile
    offset: float = 0.0
    note: str = ''  # set on the pipeline, but no output depends on it


class ShiftOutputs(BaseModel):
    value: Array()  # type: ignore[valid-type]


SHIFT = WorkflowSpec(
    name='shift',
    version=1,
    title='shift',
    description='shift',
    params=ShiftParams,
    outputs=ShiftOutputs,
)


@pytest.fixture
def loaded() -> list[float]:
    """The runs the pipeline has loaded."""
    return []


@pytest.fixture
def pipeline(loaded: list[float]) -> sciline.Pipeline:
    def load(run: Run) -> Loaded:
        loaded.append(run)
        return Loaded(10 * run)

    return sciline.Pipeline([load, shift])


@pytest.fixture
def client(pipeline: sciline.Pipeline) -> Iterator[Client]:
    datasets = FakeDatasets(proposal='p1')
    datasets.measure(1, 1.0)
    binding = PipelineBinding(
        pipeline,
        params={'run': Run, 'offset': Offset, 'note': Note},
        outputs={'value': Shifted},
    )
    backend = Backend(datasets, {SHIFT: binding})
    yield Client(backend, proposal='p1', submitter='anna')
    backend.close()


def test_a_stage_loads_its_run_once(client: Client, loaded: list[float]) -> None:
    with client.session() as session:
        tune = session.stage(
            Template(SHIFT, params={'run': dataset(run=1)}, blanks=('offset',))
        )
        tuned = [client.compute(tune, {'offset': x}) for x in (0.5, 1.5, 2.5)]
    plain = client.compute(SHIFT, {'run': dataset(run=1), 'offset': 1.5})

    assert [client.output(r, 'value') for r in tuned] == [10.5, 11.5, 12.5]
    assert client.output(plain, 'value') == 11.5
    assert tuned[1].request == plain.request
    assert loaded == [1.0, 1.0]  # once for the stage, once for the plain request


def test_a_blank_no_output_depends_on_is_accepted(client: Client) -> None:
    with client.session() as session:
        annotate = session.stage(
            Template(SHIFT, params={'run': dataset(run=1)}, blanks=('note',))
        )
        noted = client.compute(annotate, {'note': 'first try'})

    assert client.output(noted, 'value') == 10.0


def test_a_parameter_without_a_sciline_key_fails_the_record(
    pipeline: sciline.Pipeline,
) -> None:
    datasets = FakeDatasets(proposal='p1')
    datasets.measure(1, 1.0)
    binding = PipelineBinding(
        pipeline, params={'run': Run, 'offset': Offset}, outputs={'value': Shifted}
    )
    backend = Backend(datasets, {SHIFT: binding})
    client = Client(backend, proposal='p1', submitter='anna')

    record = client.compute(SHIFT, {'run': dataset(run=1)})
    backend.close()

    assert client.status(record) is Status.FAILED
    assert 'no sciline key' in client.failure(record)
    assert 'note' in client.failure(record)
