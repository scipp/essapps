# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A sciline pipeline behind a spec, in plain requests and through stages; it
makes no accumulators.
"""

from collections.abc import Iterator
from typing import NewType

import pytest
import sciline
from pydantic import BaseModel, Field

from ess.dispatch import Backend, Client, Status, SubmitError, Template, dataset
from ess.dispatch.testing import FakeDatasets
from ess.spec import Array, NexusFile, WorkflowSpec
from ess.spec.pipeline import PipelineBinding

Run = NewType('Run', float)
Loaded = NewType('Loaded', float)
Offset = NewType('Offset', float)
Note = NewType('Note', str)
Shifted = NewType('Shifted', float)
Offsets = NewType('Offsets', list)
Total = NewType('Total', float)


def shift(loaded: Loaded, offset: Offset) -> Shifted:
    return Shifted(loaded + offset)


def total(loaded: Loaded, offsets: Offsets) -> Total:
    return Total(sum(loaded + row['offset'] for row in offsets))


class Sample(BaseModel):
    file: NexusFile


class Shift(BaseModel):
    offset: float = 0.0


class Annotation(BaseModel):
    note: str = ''  # set on the pipeline, but no output depends on it


class ShiftParams(BaseModel):
    sample: Sample
    shift: Shift = Field(default_factory=Shift)
    annotation: Annotation = Field(default_factory=Annotation)


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


class OffsetRow(BaseModel):
    offset: float


class TotalParams(BaseModel):
    sample: Sample
    offsets: list[OffsetRow]


class TotalOutputs(BaseModel):
    total: Array()  # type: ignore[valid-type]


TOTAL = WorkflowSpec(
    name='total',
    version=1,
    title='total',
    description='total',
    params=TotalParams,
    outputs=TotalOutputs,
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

    return sciline.Pipeline([load, shift, total])


@pytest.fixture
def client(pipeline: sciline.Pipeline) -> Iterator[Client]:
    datasets = FakeDatasets(proposal='p1')
    datasets.measure(1, 1.0)
    binding = PipelineBinding(
        SHIFT,
        pipeline,
        params={'sample.file': Run, 'shift.offset': Offset, 'annotation.note': Note},
        outputs={'value': Shifted},
    )
    summing = PipelineBinding(
        TOTAL,
        pipeline,
        params={'sample.file': Run, 'offsets': Offsets},
        outputs={'total': Total},
    )
    backend = Backend(datasets, [binding, summing])
    yield Client(backend, proposal='p1', submitter='anna')
    backend.close()


def test_a_stage_loads_its_run_once(client: Client, loaded: list[float]) -> None:
    tune = client.stage(
        Template(SHIFT, params={'sample': {'file': dataset(run=1)}}, blanks=('shift',))
    )
    tuned = [client.compute(tune, {'shift': {'offset': x}}) for x in (0.5, 1.5, 2.5)]
    plain = client.compute(
        SHIFT, {'sample': {'file': dataset(run=1)}, 'shift': {'offset': 1.5}}
    )

    assert [client.output(r, 'value') for r in tuned] == [10.5, 11.5, 12.5]
    assert client.output(plain, 'value') == 11.5
    assert tuned[1].request == plain.request
    assert loaded == [1.0, 1.0]  # once for the stage, once for the plain request


def test_a_pipeline_takes_a_table_whole_and_makes_no_accumulators(
    client: Client,
) -> None:
    offsets = [{'offset': x} for x in (0.5, 1.5, 2.5)]
    plain = client.compute(
        TOTAL, {'sample': {'file': dataset(run=1)}, 'offsets': offsets}
    )

    assert client.output(plain, 'total') == 34.5
    with pytest.raises(SubmitError, match='its binding makes no accumulators'):
        client.accumulator(
            Template(
                TOTAL, params={'sample': {'file': dataset(run=1)}}, blanks=('offsets',)
            )
        )


def test_a_blank_no_output_depends_on_is_accepted(client: Client) -> None:
    annotate = client.stage(
        Template(
            SHIFT, params={'sample': {'file': dataset(run=1)}}, blanks=('annotation',)
        )
    )
    noted = client.compute(annotate, {'annotation': {'note': 'first try'}})

    assert client.output(noted, 'value') == 10.0


def test_a_parameter_without_a_sciline_key_fails_the_record(
    pipeline: sciline.Pipeline,
) -> None:
    datasets = FakeDatasets(proposal='p1')
    datasets.measure(1, 1.0)
    binding = PipelineBinding(
        SHIFT,
        pipeline,
        params={'sample.file': Run, 'shift.offset': Offset},
        outputs={'value': Shifted},
    )
    backend = Backend(datasets, [binding])
    client = Client(backend, proposal='p1', submitter='anna')

    record = client.compute(SHIFT, {'sample': {'file': dataset(run=1)}})
    backend.close()

    assert client.status(record) is Status.FAILED
    assert 'no sciline key' in client.failure(record)
    assert 'annotation' in client.failure(record)
