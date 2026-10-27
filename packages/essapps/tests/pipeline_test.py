# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A sciline pipeline behind a spec, in plain requests and through stages; it
makes no accumulators.
"""

from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any, NewType

import pytest
import sciline
from pydantic import BaseModel, Field

from ess.apps import Backend, Client, Status, SubmitError, Template, dataset
from ess.apps.testing import FakeDatasets, read_dataset
from ess.spec import DatasetModel, WorkflowSpec
from ess.spec.pipeline import PipelineBinding

Loaded = NewType('Loaded', float)
Shifted = NewType('Shifted', float)
Offsets = NewType('Offsets', list)
Total = NewType('Total', float)


class SampleRun(DatasetModel): ...


class Shift(BaseModel):
    offset: float = 0.0


class Annotation(BaseModel):
    note: str = ''  # set on the pipeline, but no output depends on it


class Offset(BaseModel):
    offset: float


def shift(loaded: Loaded, shift: Shift) -> Shifted:
    return Shifted(loaded + shift.offset)


def total(loaded: Loaded, offsets: Offsets) -> Total:
    return Total(sum(loaded + row.offset for row in offsets))


class ShiftParams(BaseModel):
    shift: Shift = Field(default_factory=Shift)
    annotation: Annotation = Field(default_factory=Annotation)
    sample: SampleRun


class ShiftOutputs(BaseModel):
    value: float


SHIFT = WorkflowSpec(
    name='shift',
    version=1,
    title='shift',
    description='shift',
    params=ShiftParams,
    outputs=ShiftOutputs,
)


class TotalParams(BaseModel):
    offsets: list[Offset] = Field(min_length=1)
    sample: SampleRun


class TotalOutputs(BaseModel):
    total: float


TOTAL = WorkflowSpec(
    name='total',
    version=1,
    title='total',
    description='total',
    params=TotalParams,
    outputs=TotalOutputs,
    tables=('offsets',),
)


@pytest.fixture
def loaded() -> list[float]:
    """The runs the pipeline has loaded."""
    return []


@pytest.fixture
def make_pipeline(
    loaded: list[float],
) -> Callable[[Mapping[str, Any]], sciline.Pipeline]:
    def load(sample: SampleRun) -> Loaded:
        loaded.append(read_dataset(sample.path))
        return Loaded(10 * read_dataset(sample.path))

    return lambda fixed: sciline.Pipeline([load, shift, total])


@pytest.fixture
def datasets(tmp_path: Path) -> FakeDatasets:
    datasets = FakeDatasets(proposal='p1', directory=tmp_path)
    datasets.measure(1, 1.0)
    return datasets


@pytest.fixture
def client(
    datasets: FakeDatasets,
    make_pipeline: Callable[[Mapping[str, Any]], sciline.Pipeline],
) -> Iterator[Client]:
    bind = [
        PipelineBinding(SHIFT, make_pipeline, keys={'value': (Shifted,)}),
        PipelineBinding(
            TOTAL, make_pipeline, keys={'offsets': (Offsets,), 'total': (Total,)}
        ),
    ]
    backend = Backend(datasets, bind)
    yield Client(backend, proposal='p1', submitter='anna')
    backend.close()


def test_a_stage_loads_its_run_once(client: Client, loaded: list[float]) -> None:
    sample = {'sample': dataset(run=1)}
    tune = client.stage(Template(SHIFT, params=sample, blanks=('shift',)))
    tuned = [
        client.compute(tune, params={'shift': Shift(offset=x)}) for x in (0.5, 1.5, 2.5)
    ]
    plain = client.compute(SHIFT, params={'shift': Shift(offset=1.5), **sample})

    assert [client.output(r, 'value') for r in tuned] == [10.5, 11.5, 12.5]
    assert client.output(plain, 'value') == 11.5
    assert tuned[1].request == plain.request
    assert loaded == [1.0, 1.0]  # once for the stage, once for the plain request


def test_a_pipeline_takes_a_table_whole_and_makes_no_accumulators(
    client: Client,
) -> None:
    offsets = [Offset(offset=x) for x in (0.5, 1.5, 2.5)]
    sample = {'sample': dataset(run=1)}
    plain = client.compute(TOTAL, params={'offsets': offsets, **sample})

    assert client.output(plain, 'total') == 34.5
    with pytest.raises(SubmitError, match='its binding makes no accumulators'):
        client.accumulator(Template(TOTAL, params=sample, blanks=('offsets',)))


def test_a_stage_takes_a_table_as_a_blank(client: Client) -> None:
    sample = {'sample': dataset(run=1)}
    tune = client.stage(Template(TOTAL, params=sample, blanks=('offsets',)))
    tuned = client.compute(tune, params={'offsets': [Offset(offset=1.0)]})

    assert client.output(tuned, 'total') == 11.0


def test_a_blank_no_output_depends_on_is_accepted(client: Client) -> None:
    annotate = client.stage(
        Template(SHIFT, params={'sample': dataset(run=1)}, blanks=('annotation',))
    )
    noted = client.compute(annotate, params={'annotation': {'note': 'first try'}})

    assert client.output(noted, 'value') == 10.0


def test_a_field_without_a_key_cannot_be_a_blank(
    datasets: FakeDatasets,
    make_pipeline: Callable[[Mapping[str, Any]], sciline.Pipeline],
) -> None:
    binding = PipelineBinding(
        SHIFT, make_pipeline, keys={'annotation': (), 'value': (Shifted,)}
    )
    backend = Backend(datasets, [binding])
    client = Client(backend, proposal='p1', submitter='anna')
    annotate = client.stage(
        Template(SHIFT, params={'sample': dataset(run=1)}, blanks=('annotation',))
    )

    record = client.compute(annotate, params={'annotation': {'note': 'x'}})
    backend.close()

    assert client.status(record) is Status.FAILED
    assert "['annotation']: without a key" in client.failure(record)
