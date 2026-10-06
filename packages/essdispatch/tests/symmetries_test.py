# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The symmetries of docs/developer/README.md on a toy package that offers a
single-run and a multi-run spec of one reduction, as
docs/developer/three-ways-to-run-a-spec.html shows them.

A run holds counts per pixel. A run's I(Q) is its counts summed into ``bins``
bins over the pixels (the numerator) divided by its total counts (the
denominator); the can's I(Q) is subtracted. Over several runs of a kind, the
numerators and the denominators are summed before dividing, so a held state
keeps the sums and not an I(Q).
"""

from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any

import numpy as np
import pytest
import scipp as sc
import scipp.testing
from pydantic import BaseModel

from ess.dispatch import Client, SubmitError, Template, dataset, local
from ess.dispatch.testing import FakeDatasets
from ess.spec import (
    Array,
    Function,
    HeldState,
    NexusFile,
    WorkflowSpec,
)
from ess.spec.testing import check_arrival_and_order, check_caching, check_one_row


class IofQParams(BaseModel):
    run: NexusFile
    can: NexusFile | None = None
    bins: int = 2


class SampleRow(BaseModel):
    run: NexusFile


class CanRow(BaseModel):
    run: NexusFile


class MultiIofQParams(BaseModel):
    sample_runs: list[SampleRow]
    can_runs: list[CanRow] = []
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


def _parts(run: Any, bins: int) -> np.ndarray:
    """A run's numerator per bin, followed by its denominator."""
    counts = np.asarray(run, dtype=float)
    return np.append(counts.reshape(bins, -1).sum(axis=1), counts.sum())


def _iofq(sample: np.ndarray, can: np.ndarray | None) -> dict[str, sc.Variable]:
    result = sample[:-1] / sample[-1]
    if can is not None:
        result = result - can[:-1] / can[-1]
    return {'iofq': sc.array(dims=['q'], values=result)}


def iofq(run: Any, can: Any, bins: int) -> dict[str, sc.Variable]:
    return _iofq(_parts(run, bins), None if can is None else _parts(can, bins))


class Sums:
    def __init__(self, bins: int) -> None:
        self._bins = bins
        self._sums: dict[str, np.ndarray] = {}

    def push(self, rows: Mapping[str, Mapping[str, Any]]) -> None:
        for table, row in rows.items():
            parts = _parts(row['run'], self._bins)
            if table in self._sums:
                self._sums[table] += parts
            else:
                self._sums[table] = parts

    def outputs(self) -> Mapping[str, Any]:
        return _iofq(self._sums['sample_runs'], self._sums.get('can_runs'))


class MultiIofQ:
    """The multi-run binding: a held state of sums, and a plain request over them."""

    def stage(self, fixed: Mapping[str, Any], blanks: Sequence[str]) -> Function:
        def call(**values: Any) -> Mapping[str, Any]:
            params = {**fixed, **values}
            held = self.held_state({'bins': params['bins']})
            for table in ('sample_runs', 'can_runs'):
                for row in params[table]:
                    held.push({table: row})
            return held.outputs()

        return call

    def held_state(self, fixed: Mapping[str, Any]) -> HeldState:
        return Sums(**fixed)


def test_the_multi_run_binding_keeps_the_symmetries_its_author_promises() -> None:
    sample, can = [[1.0, 3.0, 2.0, 6.0], [2.0, 2.0, 0.0, 4.0]], [[1.0, 1.0, 1.0, 1.0]]
    tables = {
        'sample_runs': [{'run': run} for run in sample],
        'can_runs': [{'run': run} for run in can],
    }
    check_one_row(
        iofq,
        MultiIofQ(),
        {'run': sample[0], 'can': can[0], 'bins': 2},
        {
            'sample_runs': tables['sample_runs'][:1],
            'can_runs': tables['can_runs'],
            'bins': 2,
        },
    )
    check_caching(MultiIofQ(), tables, [{'bins': 2}, {'bins': 1}, {'bins': 2}])
    check_arrival_and_order(MultiIofQ(), {'bins': 2}, tables)


@pytest.fixture(
    params=[MultiIofQ(), MultiIofQ().stage({}, ())],
    ids=['held state', 'plain function'],
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
        proposal='p1', datasets=datasets, bind={IOFQ: iofq, IOFQ_MULTI: request.param}
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
