# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A sum over runs as one request whose run parameter is a list: the binding
contributes each run and accumulates at the author's accumulation keys.

See docs/developer/aggregation.md.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sciline
import scipp as sc
from pydantic import BaseModel

from ess.apps.adapter import MakeAggregation, PipelineAdapter, Wiring
from ess.apps.backend import SubmitError
from ess.apps.client import Client, local
from ess.apps.examples import (
    ACCUMULATORS,
    BACKGROUND,
    COMBINE,
    CONTRIBUTE,
    FLOORED,
    NORMALIZE,
    Counts,
    Denominator,
    Floor,
    Normalized,
    Numerator,
    RunFile,
    Scale,
    add,
    floored_aggregation,
    normalize_aggregation,
    normalize_pipeline,
    registry,
    write_run,
)
from ess.apps.records import Status, Template
from ess.apps.sources import FolderSource
from ess.apps.spec import DatasetRef, OpaqueFile, dataset_ref
from ess.apps.testing import LocalInputs, equal

PARAMS = {'floor': 1.5, 'scale': 2.0}


@pytest.fixture
def runs(datasets: Path) -> list[DatasetRef]:
    """Three runs of the same experiment: the members of a sum."""
    for i, values in enumerate(
        [[1.0, 2.0, 3.0, 4.0], [2.0, 2.0, 2.0, 2.0], [4.0, 3.0, 2.0, 1.0]], start=1
    ):
        write_run(datasets / f'dream_{i}.h5', values, uuid=f'dream-{i}')
    return [dataset_ref(uuid=f'dream-{i}') for i in (1, 2, 3)]


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
        sources=[FolderSource(datasets, '*.h5', proposal='p1')],
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
    """Every denominator pushed into an accumulator: one per run contributed."""
    return []


@pytest.fixture
def built() -> list[sciline.Pipeline]:
    """The pipeline of every aggregation built: one per snapshot of its values."""
    return []


def counted(
    pushed: list[Any], built: list[sciline.Pipeline], *members: Any
) -> MakeAggregation:
    """The example's aggregation, with its builds and denominators counted."""

    def make(pipeline: sciline.Pipeline) -> sciline.Aggregation:
        built.append(pipeline)
        return sciline.Aggregation(
            pipeline,
            members=list(members),
            accumulators={
                Numerator: ACCUMULATORS[Numerator],
                Denominator: lambda: CountingAccumulator(pushed),
            },
        )

    return make


TARGETS = {'normalized': Normalized, 'numerator': Numerator, 'denominator': Denominator}


@pytest.fixture
def session(
    tmp_path: Path, datasets: Path, pushed: list[Any], built: list[sciline.Pipeline]
) -> Iterator[Client]:
    """A session whose aggregations are counted, over a list and over rows."""
    counted_registry = registry()
    counted_registry.bind(
        NORMALIZE,
        lambda: PipelineAdapter(
            normalize_pipeline(),
            keys={'runs': RunFile, 'floor': Floor, 'scale': Scale},
            resolve={'runs': 'path'},
            targets=TARGETS,
            aggregations={'runs': counted(pushed, built, RunFile)},
        ),
    )
    counted_registry.bind(
        FLOORED,
        lambda: PipelineAdapter(
            normalize_pipeline(),
            keys={'runs': {'run': RunFile, 'floor': Floor}, 'scale': Scale},
            resolve={'runs': {'run': 'path'}},
            targets=TARGETS,
            aggregations={'runs': counted(pushed, built, RunFile, Floor)},
        ),
    )
    client = local(
        tmp_path / 'session',
        instrument='dream',
        proposal='p1',
        submitter='simon',
        registry=counted_registry,
        sources=[FolderSource(datasets, '*.h5', proposal='p1')],
    )
    yield client
    client.close()


def by_sciline(datasets: Path, runs: tuple[int, ...] = (1, 2, 3), **params: Any) -> Any:
    """The same sum, with sciline alone."""
    params = PARAMS | params
    pipeline = normalize_pipeline()
    pipeline[Floor] = params['floor']
    pipeline[Scale] = params['scale']
    table = {i: {RunFile: datasets / f'dream_{i}.h5'} for i in runs}
    return normalize_aggregation(pipeline).compute(table)[Normalized]


def test_a_sum_equals_the_sciline_aggregation(
    client: Client, runs: list[DatasetRef], datasets: Path
) -> None:
    (total,) = client.wait([client.run(NORMALIZE, PARAMS | {'runs': runs})])
    assert total.status == Status.COMPLETED, total.failure
    assert equal(client.output(total, 'normalized'), by_sciline(datasets))


def test_the_record_of_a_sum_names_its_runs(
    client: Client, runs: list[DatasetRef]
) -> None:
    (total,) = client.wait([client.run(NORMALIZE, PARAMS | {'runs': runs})])
    assert total.request.params['runs'] == [ref.model_dump() for ref in runs]
    assert client.provenance(total)['raw'] == [ref.model_dump() for ref in runs]
    assert set(total.checksums) == {str(ref) for ref in runs}


def test_an_intermediate_of_a_sum_is_its_accumulated_value(
    client: Client, runs: list[DatasetRef]
) -> None:
    sum_ = Template(spec=NORMALIZE, params=PARAMS | {'runs': runs})
    (inside,) = client.wait([client.run(sum_.cut(outputs=('denominator',)))])
    assert inside.status == Status.COMPLETED, inside.failure
    assert client.output(inside, 'denominator').value == 10.0 + 8.0 + 10.0


def test_two_lists_of_runs_are_summed_separately(
    client: Client, datasets: Path
) -> None:
    refs = [
        dataset_ref(path=write_run(datasets / f'other_{i}.h5', [float(i)] * 2))
        for i in (1, 2, 3, 4)
    ]
    request = {'sample_runs': refs[2:], 'background_runs': refs[:2]}
    (result,) = client.wait([client.run(BACKGROUND, request)])
    assert result.status == Status.COMPLETED, result.failure
    assert client.output(result, 'subtracted').values.tolist() == [4.0, 4.0]


def test_a_value_per_run_is_a_column_of_the_member_table(
    client: Client, runs: list[DatasetRef], datasets: Path
) -> None:
    floors = (1.5, 0.0, 3.0)
    rows = [{'run': run, 'floor': f} for run, f in zip(runs, floors, strict=True)]
    (total,) = client.wait([client.run(FLOORED, {'runs': rows, 'scale': 2.0})])
    assert total.status == Status.COMPLETED, total.failure
    pipeline = normalize_pipeline()
    pipeline[Scale] = 2.0
    table = {
        i: {RunFile: datasets / f'dream_{i}.h5', Floor: f}
        for i, f in zip((1, 2, 3), floors, strict=True)
    }
    expected = floored_aggregation(pipeline).compute(table)[Normalized]
    assert equal(client.output(total, 'normalized'), expected)
    # Rows that hold data references are a blank of a template saved from it.
    assert Template.from_request('t', total.request, FLOORED).blanks == ('runs',)


def test_contribute_and_combine_over_references_equal_the_list_form(
    client: Client, runs: list[DatasetRef]
) -> None:
    """A sum split into two specs, both built from the one aggregation."""
    parts = client.wait(
        [client.run(CONTRIBUTE, {'run': run, 'floor': PARAMS['floor']}) for run in runs]
    )
    refs = [
        {'numerator': part.ref('numerator'), 'denominator': part.ref('denominator')}
        for part in parts
    ]
    (combined,) = client.wait(
        [client.run(COMBINE, {'parts': refs, 'scale': PARAMS['scale']})]
    )
    (at_once,) = client.wait([client.run(NORMALIZE, PARAMS | {'runs': runs})])
    assert combined.status == Status.COMPLETED, combined.failure
    assert equal(
        client.output(combined, 'normalized'), client.output(at_once, 'normalized')
    )


def test_a_sum_without_runs_is_refused_at_submit(client: Client) -> None:
    """At least one run is part of the spec's signature."""
    with pytest.raises(SubmitError, match='runs'):
        client.run(NORMALIZE, PARAMS | {'runs': []})


# What a session holds: the accumulation over the runs it has seen


def test_a_growing_sum_contributes_only_the_new_run(
    session: Client, runs: list[DatasetRef], pushed: list[Any], datasets: Path
) -> None:
    """Every record lists all its runs; the held stage pushes only the new one."""
    growing = Template(spec=NORMALIZE, params=PARAMS, blanks=('runs',))
    totals = []
    for k in (1, 2, 3):
        totals.append(session.run(growing, {'runs': runs[:k]}))
        assert len(pushed) == k
    assert [t.reused for t in totals] == [False, True, True]
    assert equal(session.output(totals[-1], 'normalized'), by_sciline(datasets))


def test_a_removed_run_accumulates_the_rest_afresh(
    session: Client, runs: list[DatasetRef], pushed: list[Any], datasets: Path
) -> None:
    """Nothing is taken out of an accumulator, so a shorter list starts afresh."""
    growing = Template(spec=NORMALIZE, params=PARAMS, blanks=('runs',))
    session.run(growing, {'runs': runs})
    fewer = session.run(growing, {'runs': [runs[0], runs[2]]})
    assert len(pushed) == 3 + 2
    assert equal(session.output(fewer, 'normalized'), by_sciline(datasets, (1, 3)))


def test_a_run_whose_file_changed_is_contributed_again(
    session: Client, runs: list[DatasetRef], pushed: list[Any], datasets: Path
) -> None:
    """A run acquired again keeps its identity; its bytes decide what is held."""
    growing = Template(spec=NORMALIZE, params=PARAMS, blanks=('runs',))
    session.run(growing, {'runs': runs})
    write_run(datasets / 'dream_2.h5', [5.0, 5.0, 5.0, 5.0], uuid='dream-2')
    again = session.run(growing, {'runs': runs})
    assert not again.reused
    assert len(pushed) == 3 + 3
    assert equal(session.output(again, 'normalized'), by_sciline(datasets))


def test_a_parameter_read_after_the_sum_reuses_the_accumulation(
    session: Client, runs: list[DatasetRef], pushed: list[Any], datasets: Path
) -> None:
    tune = Template(
        spec=NORMALIZE, params={'runs': runs, 'floor': 1.5}, blanks=('scale',)
    )
    results = [session.run(tune, {'scale': scale}) for scale in (1.0, 2.0)]
    assert len(pushed) == 3
    assert equal(session.output(results[-1], 'normalized'), by_sciline(datasets))


def test_a_parameter_the_runs_contribute_with_accumulates_again(
    session: Client, runs: list[DatasetRef], pushed: list[Any], datasets: Path
) -> None:
    """The contributions depend on ``floor``, so each value accumulates afresh."""
    tune = Template(
        spec=NORMALIZE, params={'runs': runs, 'scale': 2.0}, blanks=('floor',)
    )
    results = [session.run(tune, {'floor': floor}) for floor in (1.5, 0.0)]
    assert len(pushed) == 3 + 3
    assert equal(
        session.output(results[-1], 'normalized'), by_sciline(datasets, floor=0.0)
    )


def test_a_parameter_the_runs_contribute_with_builds_a_new_aggregation(
    session: Client, runs: list[DatasetRef], built: list[sciline.Pipeline]
) -> None:
    """
    Each value of ``floor`` builds a new aggregation, so what the runs share is
    computed again rather than held: the cost aggregation.md names.
    """
    tune = Template(
        spec=NORMALIZE, params={'runs': runs, 'scale': 2.0}, blanks=('floor',)
    )
    session.run(tune, {'floor': 1.5})
    before = len(built)
    session.run(tune, {'floor': 0.0})
    assert len(built) == before + 1
    assert built[-1].compute(Floor) == 0.0


def floored_by_sciline(datasets: Path, floors: tuple[float, ...]) -> Any:
    """The sum over rows of ``FLOORED``, with sciline alone."""
    pipeline = normalize_pipeline()
    pipeline[Scale] = 2.0
    table = {
        i: {RunFile: datasets / f'dream_{i}.h5', Floor: f}
        for i, f in enumerate(floors, start=1)
    }
    return floored_aggregation(pipeline).compute(table)[Normalized]


def test_a_growing_list_of_rows_contributes_only_the_new_row(
    session: Client, runs: list[DatasetRef], pushed: list[Any], datasets: Path
) -> None:
    floors = (1.5, 0.0, 3.0)
    rows = [{'run': run, 'floor': f} for run, f in zip(runs, floors, strict=True)]
    growing = Template(spec=FLOORED, params={'scale': 2.0}, blanks=('runs',))
    totals = []
    for k in (1, 2, 3):
        totals.append(session.run(growing, {'runs': rows[:k]}))
        assert len(pushed) == k
    assert [t.reused for t in totals] == [False, True, True]
    assert equal(
        session.output(totals[-1], 'normalized'), floored_by_sciline(datasets, floors)
    )

    # Another floor for the first run is another first row: all accumulate afresh.
    changed = session.run(growing, {'runs': [rows[0] | {'floor': 0.0}, *rows[1:]]})
    assert len(pushed) == 3 + 3
    assert equal(
        session.output(changed, 'normalized'),
        floored_by_sciline(datasets, (0.0, 0.0, 3.0)),
    )


@pytest.fixture
def floored_params(runs: list[DatasetRef]) -> Any:
    return FLOORED.params.model_validate({'runs': [{'run': runs[0]}]})


def test_rows_without_an_aggregation_are_refused() -> None:
    with pytest.raises(ValueError, match='need an aggregation'):
        PipelineAdapter(
            normalize_pipeline(),
            keys={'runs': {'run': RunFile, 'floor': Floor}},
            targets=TARGETS,
        )


def test_the_form_of_rows_is_given_per_column() -> None:
    with pytest.raises(ValueError, match='not keyed like its keys'):
        PipelineAdapter(
            normalize_pipeline(),
            keys={'runs': {'run': RunFile, 'floor': Floor}},
            resolve={'runs': 'path'},
            targets=TARGETS,
            aggregations={'runs': floored_aggregation},
        )


def test_columns_that_are_not_the_member_keys_are_refused(
    floored_params: Any, runs: list[DatasetRef], datasets: Path
) -> None:
    workflow = PipelineAdapter(
        normalize_pipeline(),
        keys={'runs': {'run': RunFile}, 'scale': Scale},
        resolve={'runs': {'run': 'path'}},
        targets=TARGETS,
        aggregations={'runs': floored_aggregation},
    )
    inputs = LocalInputs({runs[0]: datasets / 'dream_1.h5'})
    with pytest.raises(ValueError, match='but its aggregation has the member keys'):
        workflow.stage(floored_params, (), ('normalized',), inputs)


def test_two_aggregations_that_accumulate_one_key_are_refused(
    runs: list[DatasetRef], datasets: Path
) -> None:
    """Otherwise the final stage would read the accumulation of one of them only."""

    class Twice(BaseModel):
        runs: list[OpaqueFile]
        more: list[OpaqueFile]

    workflow = PipelineAdapter(
        normalize_pipeline(),
        keys={'runs': RunFile, 'more': RunFile},
        resolve={'runs': 'path', 'more': 'path'},
        targets=TARGETS,
        aggregations={'runs': normalize_aggregation, 'more': normalize_aggregation},
    )
    params = Twice(runs=runs[:1], more=runs[1:2])
    inputs = LocalInputs(
        {ref: datasets / f'dream_{i}.h5' for i, ref in enumerate(runs, 1)}
    )
    with pytest.raises(ValueError, match='share accumulation keys'):
        workflow.stage(params, (), ('normalized',), inputs)


def test_each_column_of_a_row_is_resolved_in_its_own_form(
    runs: list[DatasetRef], datasets: Path
) -> None:
    """A run as a path and a precomputed curve as an array, in one row."""

    class RunWithCurve(BaseModel):
        run: OpaqueFile
        curve: OpaqueFile

    curve = sc.array(dims=['x'], values=[1.0, 2.0])
    curve_ref = dataset_ref(path=datasets / 'curve.h5')
    curve.save_hdf5(datasets / 'curve.h5')
    wiring = Wiring(
        keys={'runs': {'run': RunFile, 'curve': Counts}},
        resolve={'runs': {'run': 'path', 'curve': 'array'}},
    )
    inputs = LocalInputs(
        {runs[0]: datasets / 'dream_1.h5', curve_ref: datasets / 'curve.h5'}
    )
    row = wiring.row('runs', RunWithCurve(run=runs[0], curve=curve_ref), inputs)
    assert row[RunFile] == datasets / 'dream_1.h5'
    assert equal(row[Counts], curve)


def test_an_output_that_needs_each_run_is_refused(datasets: Path) -> None:
    """The counts of one run are no value accumulated over the runs."""
    path = write_run(datasets / 'dream_1.h5', [1.0, 2.0])
    ref = dataset_ref(path=path)
    workflow = PipelineAdapter(
        normalize_pipeline(),
        keys={'runs': RunFile, 'floor': Floor, 'scale': Scale},
        resolve={'runs': 'path'},
        targets={'counts': Counts},
        aggregations={'runs': normalize_aggregation},
    )
    params = NORMALIZE.params.model_validate({'runs': [ref]})
    with pytest.raises(ValueError, match='no value they need accumulates'):
        workflow.stage(params, (), ('counts',), LocalInputs({ref: path}))
