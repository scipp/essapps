# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The symmetries of docs/developer/README.md on a toy package that offers a
single-run and a multi-run spec of one reduction, as
docs/developer/three-ways-to-run-a-spec.html shows them: two bindings of one
sciline pipeline of one sample run and one can run.

A run holds counts per pixel. A run's I(Q) is its counts summed into ``bins``
bins over the pixels (the numerator) divided by its total counts (the
denominator); the can's I(Q) is subtracted if there is a can run. Over
several runs of a kind, the numerators and the denominators are summed before
dividing.
"""

from collections.abc import Callable, Iterator, Mapping
from typing import Any, NewType

import numpy as np
import pytest
import sciline
import scipp as sc
import scipp.testing
from pydantic import BaseModel, Field

from ess.dispatch import Client, SubmitError, Template, dataset, local
from ess.dispatch.testing import FakeDatasets
from ess.spec import Array, NexusFile, WorkflowSpec
from ess.spec.pipeline import AccumulatingPipelineBinding, PipelineBinding


class SampleRow(BaseModel):
    """A group of the single-run spec, and a row of the multi-run spec."""

    file: NexusFile


class CanRow(BaseModel):
    file: NexusFile


class Reduction(BaseModel):
    bins: int = 2


class IofQParams(BaseModel):
    sample: SampleRow
    can: CanRow | None = None
    reduction: Reduction = Field(default_factory=Reduction)


class MultiIofQParams(BaseModel):
    sample_runs: list[SampleRow]
    can_runs: list[CanRow] = []
    reduction: Reduction = Field(default_factory=Reduction)


class IofQOutputs(BaseModel):
    iofq: Array()  # type: ignore[valid-type]


def _spec(name: str, params: type[BaseModel]) -> WorkflowSpec:
    return WorkflowSpec(
        name=name,
        version=1,
        title=name,
        description=f'toy spec {name}',
        params=params,
        outputs=IofQOutputs,
    )


IOFQ = _spec('iofq', IofQParams)
IOFQ_MULTI = _spec('iofq-multi', MultiIofQParams)


SampleFile = NewType('SampleFile', list)
CanFile = NewType('CanFile', list)
Bins = NewType('Bins', int)
SampleNumerator = NewType('SampleNumerator', np.ndarray)
SampleDenominator = NewType('SampleDenominator', float)
CanNumerator = NewType('CanNumerator', np.ndarray)
CanDenominator = NewType('CanDenominator', float)
IofQ = NewType('IofQ', sc.Variable)


def _counts(run: list | None) -> np.ndarray:
    """A run's counts per pixel; no run counts nothing."""
    return np.asarray([] if run is None else run, dtype=float)


def sample_numerator(run: SampleFile, bins: Bins) -> SampleNumerator:
    return SampleNumerator(_counts(run).reshape(bins, -1).sum(axis=1))


def sample_denominator(run: SampleFile) -> SampleDenominator:
    return SampleDenominator(_counts(run).sum())


def can_numerator(run: CanFile, bins: Bins) -> CanNumerator:
    return CanNumerator(_counts(run).reshape(bins, -1).sum(axis=1))


def can_denominator(run: CanFile) -> CanDenominator:
    return CanDenominator(_counts(run).sum())


def iofq(
    sample_num: SampleNumerator,
    sample_den: SampleDenominator,
    can_num: CanNumerator,
    can_den: CanDenominator,
) -> IofQ:
    can = np.divide(can_num, can_den, out=np.zeros_like(can_num), where=can_den > 0)
    return IofQ(sc.array(dims=['q'], values=sample_num / sample_den - can))


SANS = sciline.Pipeline(
    [sample_numerator, sample_denominator, can_numerator, can_denominator, iofq]
)
SINGLE = PipelineBinding(
    SANS,
    params={'sample.file': SampleFile, 'can.file': CanFile, 'reduction.bins': Bins},
    outputs={'iofq': IofQ},
)
MULTI = AccumulatingPipelineBinding(
    SANS,
    params={
        'sample_runs.file': SampleFile,
        'can_runs.file': CanFile,
        'reduction.bins': Bins,
    },
    outputs={'iofq': IofQ},
    accumulate={
        'sample_runs': (SampleNumerator, SampleDenominator),
        'can_runs': (CanNumerator, CanDenominator),
    },
)


def _rows_kept_by_the_framework(**values: Any) -> Mapping[str, Any]:
    return MULTI.stage(values, ())()


@pytest.fixture(
    params=[MULTI, _rows_kept_by_the_framework],
    ids=['accumulating binding', 'plain function'],
)
def client(request: pytest.FixtureRequest) -> Iterator[Client]:
    """
    A client whose multi-run binding accumulates, or is a plain function whose
    rows the framework keeps.
    """
    datasets = FakeDatasets(proposal='p1')
    datasets.measure(611, [1.0, 3.0, 2.0, 6.0])
    datasets.measure(614, [1.0, 1.0, 1.0, 1.0])
    with local(
        proposal='p1', datasets=datasets, bind={IOFQ: SINGLE, IOFQ_MULTI: request.param}
    ) as client:
        yield client


SAMPLE, CAN = dataset(run=611), dataset(run=614)


def _single(client: Client) -> sc.Variable:
    return client.output(
        client.compute(IOFQ, {'sample': {'file': SAMPLE}, 'can': {'file': CAN}}),
        'iofq',
    )


def _one_row(client: Client) -> sc.Variable:
    tables = {'sample_runs': [{'file': SAMPLE}], 'can_runs': [{'file': CAN}]}
    return client.output(client.compute(IOFQ_MULTI, tables), 'iofq')


def _through_a_stage(client: Client) -> sc.Variable:
    stage = client.stage(
        Template(
            IOFQ,
            params={'sample': {'file': SAMPLE}, 'can': {'file': CAN}},
            blanks=('reduction',),
        )
    )
    client.compute(stage, {'reduction': {'bins': 1}})
    return client.output(client.compute(stage, {'reduction': {'bins': 2}}), 'iofq')


def _pushed(*pushes: Mapping[str, Any]) -> Callable[[Client], sc.Variable]:
    def read(client: Client) -> sc.Variable:
        acc = client.accumulator(
            Template(IOFQ_MULTI, blanks=('sample_runs', 'can_runs'))
        )
        for push in pushes:
            acc.push(push)
        return client.output(acc, 'iofq')

    return read


WAYS = {
    'one-row': _one_row,
    'caching': _through_a_stage,
    'arrival': _pushed({'sample_runs': {'file': SAMPLE}, 'can_runs': {'file': CAN}}),
    'order, sample first': _pushed(
        {'sample_runs': {'file': SAMPLE}}, {'can_runs': {'file': CAN}}
    ),
    'order, can first': _pushed(
        {'can_runs': {'file': CAN}}, {'sample_runs': {'file': SAMPLE}}
    ),
}


def test_the_single_run_spec_computes_the_toy_iofq(client: Client) -> None:
    # sample: [4, 8] / 12, can: [2, 2] / 4
    assert _single(client).values.tolist() == pytest.approx([-1 / 6, 1 / 6])


@pytest.mark.parametrize('way', WAYS.values(), ids=WAYS.keys())
def test_every_way_gives_the_iofq_of_the_single_run_spec(
    client: Client, way: Callable[[Client], sc.Variable]
) -> None:
    scipp.testing.assert_allclose(way(client), _single(client))


def test_a_can_left_out_is_an_empty_table_of_cans(client: Client) -> None:
    expected = client.output(client.compute(IOFQ, {'sample': {'file': SAMPLE}}), 'iofq')
    multi = client.compute(IOFQ_MULTI, {'sample_runs': [{'file': SAMPLE}]})
    pushed = _pushed({'sample_runs': {'file': SAMPLE}})(client)

    scipp.testing.assert_allclose(client.output(multi, 'iofq'), expected)
    scipp.testing.assert_allclose(pushed, expected)


def test_a_call_through_a_stage_makes_the_record_of_the_plain_request(
    client: Client,
) -> None:
    stage = client.stage(
        Template(IOFQ, params={'sample': {'file': SAMPLE}}, blanks=('can',))
    )
    staged = client.compute(stage, {'can': {'file': CAN}})
    plain = client.compute(IOFQ, {'sample': {'file': SAMPLE}, 'can': {'file': CAN}})

    assert staged.request == plain.request
    assert staged.id != plain.id


def test_pushes_and_reads_of_an_accumulator_make_no_record(client: Client) -> None:
    _pushed({'sample_runs': {'file': SAMPLE}}, {'can_runs': {'file': CAN}})(client)
    assert client.records() == []


def test_a_single_run_spec_has_no_table_for_an_accumulator(client: Client) -> None:
    with pytest.raises(SubmitError, match='table fields'):
        client.accumulator(
            Template(IOFQ, params={'can': {'file': CAN}}, blanks=('sample',))
        )
