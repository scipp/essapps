# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""A declared additive combine: contribute, combine, finalize (D15)."""

from pathlib import Path
from typing import Any, NewType

import pytest
import sciline
from pydantic import ValidationError

from ess.apps.aggregation import AggregatePipeline
from ess.apps.backend import SubmitError
from ess.apps.client import Client, local
from ess.apps.examples import (
    LOAD,
    NORMALIZE,
    NORMALIZE_WIRING,
    Counts,
    NormalizeParams,
    RunFile,
    denominator,
    load_counts,
    normalize_pipeline,
    normalize_workflow,
    normalized,
    numerator,
    registry,
    write_run,
)
from ess.apps.records import Status
from ess.apps.sources import FolderSource
from ess.apps.spec import DatasetRef, Ref
from ess.apps.testing import assert_combine_is_associative
from ess.apps.warm import equal


@pytest.fixture(params=[False, True], ids=['session', 'subprocess'])
def client(request: pytest.FixtureRequest, tmp_path: Path, datasets: Path):
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


@pytest.fixture
def runs(datasets: Path) -> list[DatasetRef]:
    """Three runs of the same experiment: the members of a series."""
    for i, values in enumerate(
        [[1.0, 2.0, 3.0, 4.0], [2.0, 2.0, 2.0, 2.0], [4.0, 3.0, 2.0, 1.0]], start=1
    ):
        write_run(datasets / f'dream_{i}.h5', values)
    return [DatasetRef(instrument='dream', run=i) for i in (1, 2, 3)]


def build(
    pipeline: sciline.Pipeline | None = None, **changes: Any
) -> AggregatePipeline:
    """The wiring the example ships, with what a test changes."""
    return AggregatePipeline(
        pipeline if pipeline is not None else normalize_pipeline(),
        **(NORMALIZE_WIRING | changes),
    )


Baseline = NewType('Baseline', float)


def baseline() -> Baseline:
    """A value no member affects; an accumulation key it must not be."""
    return Baseline(1.0)


# Binding: the declaration against the graph


def test_the_three_entry_points_compose_into_the_single_callable(
    datasets: Path,
) -> None:
    run = write_run(datasets / 'a.h5', [1.0, 2.0, 3.0, 4.0])
    workflow = normalize_workflow()
    params = NormalizeParams(run=run, floor=1.5, scale=2.0)
    composed = workflow.finalize(workflow.contribute(params), params)
    assert equal(workflow(params)['normalized'], composed['normalized'])


def test_the_combine_of_the_example_is_associative(datasets: Path) -> None:
    runs = [
        write_run(datasets / 'a.h5', [1.0, 2.0, 3.0, 4.0]),
        write_run(datasets / 'b.h5', [2.0, 2.0, 2.0, 2.0]),
        write_run(datasets / 'c.h5', [4.0, 3.0, 2.0, 1.0]),
    ]
    assert_combine_is_associative(
        normalize_workflow,
        [NormalizeParams(run=run, floor=1.5, scale=2.0) for run in runs],
    )


def test_a_finalize_parameter_that_contribute_reads_is_refused() -> None:
    """``floor`` decides what a contribution holds, so a change invalidates it."""
    with pytest.raises(ValueError, match='contribute reads them too'):
        build(finalize_params=frozenset({'floor', 'scale'}))


def test_a_parameter_the_contribution_does_not_depend_on_is_refused() -> None:
    """Declaring nothing for finalize makes ``scale`` a member key, which it is not."""
    with pytest.raises(ValueError, match="does not depend on \\['scale'\\]"):
        build(finalize_params=frozenset())


def test_an_accumulation_key_that_does_not_depend_on_the_members_is_refused() -> None:
    with pytest.raises(ValueError, match='do not depend on the members'):
        build(
            sciline.Pipeline(
                [load_counts, numerator, denominator, normalized, baseline]
            ),
            accumulation_keys=NORMALIZE_WIRING['accumulation_keys']
            | {'baseline': Baseline},
        )


def test_a_finalize_parameter_the_outputs_do_not_depend_on_is_refused() -> None:
    Unused = NewType('Unused', float)
    with pytest.raises(ValueError, match='outputs do not depend on them'):
        build(
            keys=NORMALIZE_WIRING['keys'] | {'unused': Unused},
            finalize_params=frozenset({'scale', 'unused'}),
        )


def test_a_finalize_parameter_change_does_not_recontribute(datasets: Path) -> None:
    """Rebuilding the finalize stage must not read the members again."""
    loads: list[Path] = []

    def counted(path: RunFile) -> Counts:
        loads.append(path)
        return load_counts(path)

    run = write_run(datasets / 'a.h5', [1.0, 2.0, 3.0, 4.0])
    workflow = build(sciline.Pipeline([counted, numerator, denominator, normalized]))
    contribution = workflow.contribute(NormalizeParams(run=run, floor=1.5))
    first = workflow.finalize(contribution, NormalizeParams(run=run, scale=1.0))
    second = workflow.finalize(contribution, NormalizeParams(run=run, scale=2.0))
    assert loads == [run]
    assert equal(second['normalized'], first['normalized'] * 2.0)


# The three executions


def contribute(client: Client, run: DatasetRef, **params: Any) -> str:
    request = client.request(
        NORMALIZE, {'run': run, 'floor': 1.5, **params}, stage='contribute'
    )
    return client.submit(request).id


def test_a_member_run_produces_the_contribution_and_no_other_output(
    client: Client, runs: list[DatasetRef]
) -> None:
    (member,) = client.wait([contribute(client, runs[0])])
    assert member.status == Status.COMPLETED, member.failure
    assert member.output_names() == {'contribution'}
    assert set(client.output(member, 'contribution')) == {'numerator', 'denominator'}


def test_one_shot_a_batch_and_a_chained_series_agree(
    client: Client, runs: list[DatasetRef]
) -> None:
    params = {'floor': 1.5, 'scale': 2.0}
    one_shot = client.run(NORMALIZE, {'run': runs[0], **params})

    # A chained series: one combine record per arrival, over the previous combine.
    first = contribute(client, runs[0])
    client.wait([first])
    chain = client.run(
        NORMALIZE,
        {'scale': 2.0},
        stage='combine',
        contributions=[client.record(first)],
    )
    second = contribute(client, runs[1])
    client.wait([chain.id, second])
    chained = client.run(
        NORMALIZE,
        {'scale': 2.0},
        stage='combine',
        contributions=[client.record(chain.id), client.record(second)],
    )

    # A batch: both members and one combine over them, submitted together.
    batch = client.submit_group(
        {
            'a': client.request(
                NORMALIZE, {'run': runs[0], **params}, stage='contribute'
            ),
            'b': client.request(
                NORMALIZE, {'run': runs[1], **params}, stage='contribute'
            ),
            'combine': client.request(
                NORMALIZE,
                {'scale': 2.0},
                stage='combine',
                contributions=[
                    Ref(record='@a', output='contribution'),
                    Ref(record='@b', output='contribution'),
                ],
            ),
        }
    )
    done = client.wait([one_shot, chain, chained, batch['combine']])
    assert [r.status for r in done] == [Status.COMPLETED] * 4, [r.failure for r in done]
    one_shot, chain, chained, batch_combine = done
    # A series of one member is the one-shot run of that member.
    assert equal(
        client.output(chain, 'normalized'), client.output(one_shot, 'normalized')
    )
    # Two members combined in one request or one at a time give the same result.
    assert equal(
        client.output(chained, 'normalized'),
        client.output(batch_combine, 'normalized'),
    )
    assert client.output(chained, 'contribution')['denominator'].value == 18.0


def test_a_combine_carrying_a_contribute_parameter_is_refused(
    client: Client, runs: list[DatasetRef]
) -> None:
    member = contribute(client, runs[0])
    client.wait([member])
    with pytest.raises(SubmitError, match="'floor' is contribute's"):
        client.run(
            NORMALIZE,
            {'scale': 2.0, 'floor': 1.5},
            stage='combine',
            contributions=[client.record(member)],
        )


def test_members_that_disagree_on_a_contribute_parameter_are_refused(
    client: Client, runs: list[DatasetRef]
) -> None:
    a = contribute(client, runs[0], floor=1.5)
    b = contribute(client, runs[1], floor=0.0)
    client.wait([a, b])
    with pytest.raises(SubmitError, match='contributed with floor='):
        client.run(
            NORMALIZE,
            {'scale': 2.0},
            stage='combine',
            contributions=[client.record(a), client.record(b)],
        )


def test_a_contribution_of_another_spec_is_refused(
    client: Client, runs: list[DatasetRef]
) -> None:
    loaded = client.wait([client.run(LOAD, {'run': runs[0]})])[0]
    with pytest.raises(SubmitError, match='contributed by load/v1'):
        client.run(
            NORMALIZE,
            {'scale': 2.0},
            stage='combine',
            contributions=[Ref(record=loaded.id, output='data')],
        )


def test_a_stage_and_its_contributions_cannot_disagree(client: Client) -> None:
    """One fact, said once: a combine request is the one with contributions."""
    contribution = Ref(record='r1', output='contribution')
    with pytest.raises(ValidationError, match='at least one contribution'):
        client.request(NORMALIZE, {'scale': 2.0}, stage='combine')
    with pytest.raises(ValidationError, match='no other stage references any'):
        client.request(NORMALIZE, {'floor': 1.5}, contributions=[contribution])


def test_a_spec_without_a_contribution_has_only_whole_runs(
    client: Client, runs: list[DatasetRef]
) -> None:
    with pytest.raises(SubmitError, match='declares no contribution'):
        client.run(LOAD, {'run': runs[0]}, stage='contribute')
