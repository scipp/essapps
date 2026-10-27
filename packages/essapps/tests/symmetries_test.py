# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The symmetries of docs/developer/README.md on a toy package that offers a
single-run spec and a multi-run spec with tables of its sample and can runs,
as docs/developer/three-ways-to-run-a-spec.html shows them: a sciline pipeline of
one sample run and one can run behind the single-run spec, and a driver over
the same pipeline behind the multi-run spec.

A run holds counts per pixel. A run's I(Q) is its counts summed into ``bins``
bins over the pixels (the numerator) divided by its total counts (the
denominator); the can's I(Q) is subtracted from the sample's. Over several
runs of a kind, the numerators and the denominators are summed before
dividing.
"""

from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, NewType

import numpy as np
import pytest
import sciline
import scipp as sc
import scipp.testing
from pydantic import BaseModel, ConfigDict, Field

from ess.apps import Client, SubmitError, Template, dataset, local
from ess.apps.testing import FakeDatasets, read_dataset
from ess.spec import DatasetModel, DriverBinding, WorkflowSpec
from ess.spec.pipeline import PipelineBinding

SampleNumerator = NewType('SampleNumerator', np.ndarray)
SampleDenominator = NewType('SampleDenominator', float)
CanNumerator = NewType('CanNumerator', np.ndarray)
CanDenominator = NewType('CanDenominator', float)
IofQ = NewType('IofQ', sc.Variable)


class SampleRun(DatasetModel): ...


class CanRun(DatasetModel): ...


class Reduction(BaseModel):
    bins: int = 2


class IofQParams(BaseModel):
    reduction: Reduction = Field(default_factory=Reduction)
    sample: SampleRun
    can: CanRun


class IofQMultiParams(BaseModel):
    reduction: Reduction = Field(default_factory=Reduction)
    sample: list[SampleRun] = Field(min_length=1)
    can: list[CanRun] = Field(min_length=1)


class IofQOutputs(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    iofq: sc.Variable


IOFQ = WorkflowSpec(
    name='iofq',
    version=1,
    title='iofq',
    description='toy spec iofq',
    params=IofQParams,
    outputs=IofQOutputs,
)
IOFQ_MULTI = WorkflowSpec(
    name='iofq-multi',
    version=1,
    title='iofq-multi',
    description='toy spec iofq-multi',
    params=IofQMultiParams,
    outputs=IofQOutputs,
    tables=('sample', 'can'),
)


def _counts(run: SampleRun | CanRun) -> np.ndarray:
    """A run's counts per pixel."""
    return np.asarray(read_dataset(run.path), dtype=float)


def sample_numerator(run: SampleRun, reduction: Reduction) -> SampleNumerator:
    return SampleNumerator(_counts(run).reshape(reduction.bins, -1).sum(axis=1))


def sample_denominator(run: SampleRun) -> SampleDenominator:
    return SampleDenominator(_counts(run).sum())


def can_numerator(run: CanRun, reduction: Reduction) -> CanNumerator:
    return CanNumerator(_counts(run).reshape(reduction.bins, -1).sum(axis=1))


def can_denominator(run: CanRun) -> CanDenominator:
    return CanDenominator(_counts(run).sum())


def iofq(
    sample_num: SampleNumerator,
    sample_den: SampleDenominator,
    can_num: CanNumerator,
    can_den: CanDenominator,
) -> IofQ:
    values = sample_num / sample_den - can_num / can_den
    return IofQ(sc.array(dims=['q'], values=values))


SANS = sciline.Pipeline(
    [sample_numerator, sample_denominator, can_numerator, can_denominator, iofq]
)
SINGLE = PipelineBinding(IOFQ, lambda fixed: SANS.copy(), keys={'iofq': (IofQ,)})


class IofQDriver:
    """
    Sums the numerator and the denominator of each table's runs, and divides
    once, at a read; refuses a state without a sample run or a can run.
    """

    def __init__(self, fixed: Mapping[str, Any]) -> None:
        wf = SANS.copy()
        wf[Reduction] = fixed['reduction']
        parts = {
            'sample': (SampleRun, (SampleNumerator, SampleDenominator)),
            'can': (CanRun, (CanNumerator, CanDenominator)),
        }
        self._contribute = {
            table: (key, sciline.Stage(wf, inputs=(key,), outputs=summed))
            for table, (key, summed) in parts.items()
        }
        summed = [key for _, keys in parts.values() for key in keys]
        self._finalize = sciline.Stage(wf, inputs=summed, outputs=(IofQ,))
        self._sums: dict[Any, Any] = {}

    def push(self, table: str, rows: Sequence[Any]) -> None:
        key, stage = self._contribute[table]
        for run in rows:
            for k, part in stage.compute({key: run}).items():
                self._sums[k] = self._sums.get(k, 0) + part

    def outputs(self) -> IofQOutputs:
        return IofQOutputs(iofq=self._finalize.compute(self._sums)[IofQ])


MULTI = DriverBinding(IOFQ_MULTI, IofQDriver)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[Client]:
    datasets = FakeDatasets(proposal='p1', directory=tmp_path)
    datasets.measure(611, [1.0, 3.0, 2.0, 6.0])
    datasets.measure(612, [2.0, 6.0, 4.0, 12.0])  # twice 611: the same I(Q)
    datasets.measure(614, [1.0, 1.0, 1.0, 1.0])
    with local(proposal='p1', datasets=datasets, bind=[SINGLE, MULTI]) as client:
        yield client


SAMPLE, CAN = dataset(run=611), dataset(run=614)
SAMPLES = [SAMPLE, dataset(run=612)]


def _single(client: Client, **params: Any) -> sc.Variable:
    return client.output(client.compute(IOFQ, params=params), 'iofq')


def _one_row(client: Client) -> sc.Variable:
    params = {'sample': [SAMPLE], 'can': [CAN]}
    return client.output(client.compute(IOFQ_MULTI, params=params), 'iofq')


def _through_a_stage(client: Client) -> sc.Variable:
    stage = client.stage(
        Template(IOFQ, params={'sample': SAMPLE, 'can': CAN}, blanks=('reduction',))
    )
    client.compute(stage, params={'reduction': Reduction(bins=1)})
    record = client.compute(stage, params={'reduction': Reduction(bins=2)})
    return client.output(record, 'iofq')


def _pushed(*pushes: Mapping[str, list[Any]]) -> Callable[[Client], sc.Variable]:
    def read(client: Client) -> sc.Variable:
        acc = client.accumulator(Template(IOFQ_MULTI, blanks=('sample', 'can')))
        for rows in pushes:
            acc.push(**rows)
        return client.output(acc, 'iofq')

    return read


WAYS = {
    'one-row': _one_row,
    'caching': _through_a_stage,
    'arrival': _pushed({'sample': SAMPLES}, {'can': [CAN]}),
    'arrival, one row per push': _pushed(
        {'sample': SAMPLES[:1]}, {'sample': SAMPLES[1:]}, {'can': [CAN]}
    ),
    'order, can first': _pushed({'can': [CAN]}, {'sample': SAMPLES}),
}


def test_the_single_run_spec_computes_the_toy_iofq(client: Client) -> None:
    # sample: [4, 8] / 12, can: [2, 2] / 4
    iofq = _single(client, sample=SAMPLE, can=CAN)
    assert iofq.values.tolist() == pytest.approx([-1 / 6, 1 / 6])


@pytest.mark.parametrize('way', WAYS.values(), ids=WAYS.keys())
def test_every_way_gives_the_iofq_of_the_single_run_spec(
    client: Client, way: Callable[[Client], sc.Variable]
) -> None:
    scipp.testing.assert_allclose(way(client), _single(client, sample=SAMPLE, can=CAN))


def test_a_request_that_leaves_out_a_table_is_refused(client: Client) -> None:
    with pytest.raises(SubmitError, match='can: a required parameter'):
        client.compute(IOFQ_MULTI, params={'sample': [SAMPLE]})


def test_a_call_through_a_stage_makes_the_record_of_the_plain_request(
    client: Client,
) -> None:
    stage = client.stage(Template(IOFQ, params={'sample': SAMPLE}, blanks=('can',)))
    staged = client.compute(stage, params={'can': CAN})
    plain = client.compute(IOFQ, params={'sample': SAMPLE, 'can': CAN})

    assert staged.request == plain.request
    assert staged.id != plain.id


def test_pushes_and_reads_of_an_accumulator_make_no_record(client: Client) -> None:
    _pushed({'sample': [SAMPLE]}, {'can': [CAN]})(client)
    assert client.records() == []


def test_a_pipeline_makes_no_accumulators_and_a_driver_no_stages(
    client: Client,
) -> None:
    with pytest.raises(SubmitError, match='its binding makes no accumulators'):
        client.accumulator(Template(IOFQ, params={'can': CAN}, blanks=('sample',)))
    tables = {'sample': [SAMPLE], 'can': [CAN]}
    with pytest.raises(SubmitError, match='its binding makes no stages'):
        client.stage(Template(IOFQ_MULTI, params=tables, blanks=('reduction',)))
