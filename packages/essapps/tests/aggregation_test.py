# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A sum over runs as two stages of one workflow record: a member stage per run to
the intermediates, and a finalize stage from their accumulation.

See docs/developer/aggregation.md.
"""

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import sciline
import scipp as sc

from ess.apps.adapter import PipelineAdapter
from ess.apps.backend import SubmitError
from ess.apps.client import Client, StageHandle, WorkflowHandle, local
from ess.apps.examples import (
    ACCUMULATORS,
    NORMALIZE,
    Denominator,
    Floor,
    Normalized,
    Numerator,
    RunFile,
    Scale,
    add,
    normalize_aggregation,
    normalize_pipeline,
    registry,
    write_run,
)
from ess.apps.records import Accumulate, StageRecord, Status
from ess.apps.sources import FolderSource
from ess.apps.spec import DatasetRef, dataset_ref
from ess.apps.testing import assert_accumulator_is_associative, equal

PARAMS = {'floor': 1.5, 'scale': 2.0}


@pytest.fixture
def runs(datasets: Path) -> list[DatasetRef]:
    """Three runs of the same experiment: the members of a series."""
    for i, values in enumerate(
        [[1.0, 2.0, 3.0, 4.0], [2.0, 2.0, 2.0, 2.0], [4.0, 3.0, 2.0, 1.0]], start=1
    ):
        write_run(datasets / f'dream_{i}.h5', values)
    return [dataset_ref(instrument='dream', run=i) for i in (1, 2, 3)]


@pytest.fixture(params=[False, True], ids=['session', 'subprocess'])
def client(
    request: pytest.FixtureRequest, tmp_path: Path, datasets: Path
) -> Iterator[Client]:
    """The same runs in both execution shapes; the throwaway one names its registry."""
    client = local(
        tmp_path / 'store',
        instrument='dream',
        proposal='p1',
        submitter='simon',
        registry='ess.apps.examples:registry' if request.param else registry(),
        sources=[FolderSource(datasets, '*.h5')],
        throwaway=request.param,
    )
    yield client
    client.close()


class CountingAccumulator:
    """The example's accumulator, recording every value pushed into it."""

    def __init__(self, pushed: list[Any]) -> None:
        self._accumulator = sciline.Buffered(add)()
        self._pushed = pushed

    def push(self, value: Any) -> None:
        self._pushed.append(value)
        self._accumulator.push(value)

    @property
    def value(self) -> Any:
        return self._accumulator.value


@pytest.fixture
def pushed() -> list[Any]:
    """Every denominator pushed into an accumulator; its length is the cost."""
    return []


@pytest.fixture
def session(tmp_path: Path, datasets: Path, pushed: list[Any]) -> Iterator[Client]:
    """A session whose denominator accumulator is counted."""
    counted = registry()

    def workflow() -> PipelineAdapter:
        return PipelineAdapter(
            normalize_pipeline(),
            keys={'run': RunFile, 'floor': Floor, 'scale': Scale},
            resolve={'run': 'path'},
            targets={
                'normalized': Normalized,
                'numerator': Numerator,
                'denominator': Denominator,
            },
            accumulators={
                Numerator: ACCUMULATORS[Numerator],
                Denominator: lambda: CountingAccumulator(pushed),
            },
        )

    counted.bind(NORMALIZE, workflow)
    client = local(
        tmp_path / 'session',
        instrument='dream',
        proposal='p1',
        submitter='simon',
        registry=counted,
        sources=[FolderSource(datasets, '*.h5')],
    )
    yield client
    client.close()


def member_stage(wf: WorkflowHandle) -> StageHandle:
    return wf.stage(inputs=['run'], outputs=['numerator', 'denominator'])


def finalize_stage(wf: WorkflowHandle) -> StageHandle:
    return wf.stage(inputs=['numerator', 'denominator'], outputs=['normalized'])


def accumulated(members: list[StageRecord]) -> dict[str, Accumulate]:
    """The finalize's inputs: the accumulation of every member's intermediates."""
    return {
        name: Accumulate(accumulate=[m.ref(name) for m in members])
        for name in ('numerator', 'denominator')
    }


def by_sciline(datasets: Path) -> sc.DataArray:
    """The same sum over the three runs, with sciline alone."""
    runs = [datasets / f'dream_{i}.h5' for i in (1, 2, 3)]
    pipeline = normalize_pipeline()
    pipeline[Floor] = PARAMS['floor']
    pipeline[Scale] = PARAMS['scale']
    table = {i: {RunFile: run} for i, run in enumerate(runs)}
    return normalize_aggregation(pipeline).compute(table)[Normalized]


def test_a_member_stage_computes_the_intermediates_only(
    client: Client, runs: list[DatasetRef]
) -> None:
    wf = client.workflow(NORMALIZE, PARAMS)
    (member,) = client.wait([member_stage(wf).compute({'run': runs[0]})])
    assert member.status == Status.COMPLETED, member.failure
    assert member.output_names() == {'numerator', 'denominator'}
    assert client.output(member, 'denominator').value == 10.0


def test_a_finalize_over_the_members_equals_the_sciline_aggregation(
    client: Client, runs: list[DatasetRef], datasets: Path
) -> None:
    wf = client.workflow(NORMALIZE, PARAMS)
    members = [member_stage(wf).compute({'run': run}) for run in runs]
    total = finalize_stage(wf).compute(accumulated(members))
    (done,) = client.wait([total])
    assert done.status == Status.COMPLETED, done.failure
    assert equal(client.output(done, 'normalized'), by_sciline(datasets))


def test_a_growing_series_pushes_only_the_new_member(
    session: Client, runs: list[DatasetRef], pushed: list[Any], datasets: Path
) -> None:
    """The session holds the accumulator; every record still lists all members."""
    wf = session.workflow(NORMALIZE, PARAMS)
    contribute, finalize = member_stage(wf), finalize_stage(wf)
    members: list[StageRecord] = []
    totals = []
    for run in runs:
        members.append(contribute.compute({'run': run}))
        totals.append(finalize.compute(accumulated(members)))
        assert len(pushed) == len(members)
    assert [t.reused for t in totals] == [False, True, True]
    assert len(totals[-1].request.inputs['denominator']['accumulate']) == 3
    assert equal(session.output(totals[-1], 'normalized'), by_sciline(datasets))


def test_a_corrected_member_accumulates_every_member_again(
    session: Client, runs: list[DatasetRef], pushed: list[Any], datasets: Path
) -> None:
    """Nothing is taken out of an accumulator, so a correction starts a fresh one."""
    wf = session.workflow(NORMALIZE, PARAMS)
    contribute, finalize = member_stage(wf), finalize_stage(wf)
    members = [contribute.compute({'run': run}) for run in runs]
    finalize.compute(accumulated(members))
    assert len(pushed) == 3

    write_run(datasets / 'dream_2.h5', [5.0, 5.0, 5.0, 5.0])
    members[1] = contribute.compute({'run': runs[1]})
    corrected = finalize.compute(accumulated(members))
    assert len(pushed) == 6
    assert equal(session.output(corrected, 'normalized'), by_sciline(datasets))


def test_an_accumulation_across_workflow_records_is_refused(
    client: Client, runs: list[DatasetRef]
) -> None:
    """Members and finalize share every parameter by sharing a workflow record."""
    wf = client.workflow(NORMALIZE, PARAMS)
    members = client.wait([member_stage(wf).compute({'run': run}) for run in runs[:2]])
    other = client.workflow(NORMALIZE, PARAMS | {'floor': 0.0})
    request = finalize_stage(other).request(accumulated(members))
    report = client.validate(request)
    assert any('cut from workflow record' in e for e in report.errors)
    with pytest.raises(SubmitError):
        client.submit(request)


@pytest.mark.parametrize('key', [Numerator, Denominator])
def test_the_accumulators_of_the_example_are_associative(
    key: Any, datasets: Path
) -> None:
    make: Callable[[], Any] = ACCUMULATORS[key]
    counts = [
        sc.io.load_hdf5(write_run(datasets / f'{i}.h5', values))
        for i, values in enumerate([[1.0, 2.0], [2.0, 2.0], [4.0, 3.0]])
    ]
    parts = counts if key is Numerator else [c.data.sum() for c in counts]
    assert_accumulator_is_associative(make, parts)
