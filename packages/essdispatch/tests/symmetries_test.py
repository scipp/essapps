# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The symmetries of docs/developer/README.md on a toy package that offers a
single-run and a multi-run spec of one reduction, as
docs/developer/three-ways-to-run-a-spec.html shows them: two bindings of one
sciline pipeline of one sample run and one can run.

A run holds counts per pixel. A run's I(Q) is its counts summed into ``bins``
bins over the pixels (the numerator) divided by its total counts (the
denominator); the can's I(Q) is subtracted. Over several runs of a kind, the
numerators and the denominators are summed before dividing.
"""

from collections.abc import Callable, Iterator, Mapping
from typing import Any, NewType

import numpy as np
import pytest
import sciline
import scipp as sc
import scipp.testing
from pydantic import BaseModel

from ess.dispatch import Client, SubmitError, Template, dataset, local
from ess.dispatch.testing import FakeDatasets
from ess.spec import Array, NexusFile, WorkflowSpec
from ess.spec.pipeline import AccumulatingPipelineBinding, PipelineBinding


class IofQParams(BaseModel):
    run: NexusFile
    can: NexusFile
    bins: int = 2


class SampleRow(BaseModel):
    run: NexusFile


class CanRow(BaseModel):
    run: NexusFile


class MultiIofQParams(BaseModel):
    sample_runs: list[SampleRow]
    can_runs: list[CanRow]
    bins: int = 2


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


def sample_numerator(run: SampleFile, bins: Bins) -> SampleNumerator:
    return SampleNumerator(np.asarray(run, dtype=float).reshape(bins, -1).sum(axis=1))


def sample_denominator(run: SampleFile) -> SampleDenominator:
    return SampleDenominator(float(np.sum(run)))


def can_numerator(run: CanFile, bins: Bins) -> CanNumerator:
    return CanNumerator(np.asarray(run, dtype=float).reshape(bins, -1).sum(axis=1))


def can_denominator(run: CanFile) -> CanDenominator:
    return CanDenominator(float(np.sum(run)))


def iofq(
    sample_num: SampleNumerator,
    sample_den: SampleDenominator,
    can_num: CanNumerator,
    can_den: CanDenominator,
) -> IofQ:
    return IofQ(
        sc.array(dims=['q'], values=sample_num / sample_den - can_num / can_den)
    )


SANS = sciline.Pipeline(
    [sample_numerator, sample_denominator, can_numerator, can_denominator, iofq]
)
SINGLE = PipelineBinding(
    SANS,
    params={'run': SampleFile, 'can': CanFile, 'bins': Bins},
    outputs={'iofq': IofQ},
)
MULTI = AccumulatingPipelineBinding(
    SANS,
    params={'bins': Bins},
    tables={'sample_runs': {'run': SampleFile}, 'can_runs': {'run': CanFile}},
    outputs={'iofq': IofQ},
    accumulate=(SampleNumerator, SampleDenominator, CanNumerator, CanDenominator),
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
    return client.output(client.compute(IOFQ, {'run': SAMPLE, 'can': CAN}), 'iofq')


def _one_row(client: Client) -> sc.Variable:
    tables = {'sample_runs': [{'run': SAMPLE}], 'can_runs': [{'run': CAN}]}
    return client.output(client.compute(IOFQ_MULTI, tables), 'iofq')


def _through_a_stage(client: Client) -> sc.Variable:
    stage = client.stage(
        Template(IOFQ, params={'run': SAMPLE, 'can': CAN}, blanks=('bins',))
    )
    client.compute(stage, {'bins': 1})
    return client.output(client.compute(stage, {'bins': 2}), 'iofq')


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
    'arrival': _pushed({'sample_runs': {'run': SAMPLE}, 'can_runs': {'run': CAN}}),
    'order, sample first': _pushed(
        {'sample_runs': {'run': SAMPLE}}, {'can_runs': {'run': CAN}}
    ),
    'order, can first': _pushed(
        {'can_runs': {'run': CAN}}, {'sample_runs': {'run': SAMPLE}}
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


def test_a_call_through_a_stage_makes_the_record_of_the_plain_request(
    client: Client,
) -> None:
    stage = client.stage(Template(IOFQ, params={'run': SAMPLE}, blanks=('can',)))
    staged = client.compute(stage, {'can': CAN})
    plain = client.compute(IOFQ, {'run': SAMPLE, 'can': CAN})

    assert staged.request == plain.request
    assert staged.id != plain.id


def test_pushes_and_reads_of_an_accumulator_make_no_record(client: Client) -> None:
    _pushed({'sample_runs': {'run': SAMPLE}}, {'can_runs': {'run': CAN}})(client)
    assert client.records() == []


def test_a_single_run_spec_has_no_table_for_an_accumulator(client: Client) -> None:
    with pytest.raises(SubmitError, match='table fields'):
        client.accumulator(Template(IOFQ, params={'can': CAN}, blanks=('run',)))
