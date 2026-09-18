# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""One pipeline as two plain specs: contribute and combine (D15)."""

from pathlib import Path
from typing import Any, NewType

import pytest
import sciline
import scipp as sc

from ess.apps.aggregation import Aggregation
from ess.apps.client import Client, local
from ess.apps.examples import (
    LOAD,
    NORMALIZE,
    NORMALIZE_COMBINE,
    NORMALIZE_CONTRIBUTE,
    NORMALIZE_WIRING,
    ContributeParams,
    Counts,
    Denominator,
    Floor,
    Normalized,
    Numerator,
    RunFile,
    Scale,
    denominator,
    load_counts,
    normalize_aggregation,
    normalize_pipeline,
    normalized,
    numerator,
    registry,
    write_run,
)
from ess.apps.records import RunRecord, Status
from ess.apps.sources import FolderSource
from ess.apps.spec import DatasetRef, OutputRef, dataset_ref
from ess.apps.testing import LocalInputs, assert_combine_is_associative, equal


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
    return [dataset_ref(instrument='dream', run=i) for i in (1, 2, 3)]


def build(pipeline: sciline.Pipeline | None = None, **changes: Any) -> Aggregation:
    """The wiring the example ships, with what a test changes."""
    return Aggregation(
        pipeline if pipeline is not None else normalize_pipeline(),
        **(NORMALIZE_WIRING | changes),
    )


Baseline = NewType('Baseline', float)


def baseline() -> Baseline:
    """A value no member affects; an accumulation key it must not be."""
    return Baseline(1.0)


# What the adapter checks against the graph when it is built


def test_a_spec_whose_parameters_the_graph_disagrees_with_is_refused() -> None:
    """``floor`` reaches the accumulation keys, so it is not the combine's."""
    with pytest.raises(ValueError, match='normalize-combine/v1 declares params'):
        build(combine=NORMALIZE_COMBINE.model_copy(update={'params': NORMALIZE.params}))


def test_a_member_parameter_the_contribution_does_not_depend_on_is_refused() -> None:
    """``scale`` is read after the accumulation keys, so it cannot be a member's."""
    with pytest.raises(ValueError, match='are not needed by outputs'):
        build(members=['run', 'scale'])


def test_an_accumulation_key_that_does_not_depend_on_the_members_is_refused() -> None:
    with pytest.raises(ValueError, match='do not depend on the members'):
        build(
            sciline.Pipeline(
                [load_counts, numerator, denominator, normalized, baseline]
            ),
            accumulation_keys=NORMALIZE_WIRING['accumulation_keys']
            | {'baseline': Baseline},
        )


def test_a_combine_spec_must_chain_exactly_one_collection() -> None:
    with pytest.raises(ValueError, match='exactly one collection parameter'):
        build(combine=NORMALIZE_COMBINE.model_copy(update={'chain': {}}))


# The two callables


def test_the_combine_of_the_example_is_associative(datasets: Path) -> None:
    runs = [
        write_run(datasets / 'a.h5', [1.0, 2.0, 3.0, 4.0]),
        write_run(datasets / 'b.h5', [2.0, 2.0, 2.0, 2.0]),
        write_run(datasets / 'c.h5', [4.0, 3.0, 2.0, 1.0]),
    ]
    refs = [dataset_ref(path=run) for run in runs]
    aggregation = normalize_aggregation()
    assert_combine_is_associative(
        aggregation.contribute_workflow(),
        aggregation.combine_workflow(),
        NORMALIZE_COMBINE,
        [ContributeParams(run=ref, floor=1.5) for ref in refs],
        {'scale': 2.0},
        LocalInputs(dict(zip(refs, runs, strict=True))),
    )


def counting_pipeline(loads: list[Path]) -> sciline.Pipeline:
    """The example's pipeline, recording every read of a member."""

    def counted(path: RunFile) -> Counts:
        loads.append(path)
        return load_counts(path)

    return sciline.Pipeline([counted, numerator, denominator, normalized])


def test_a_stage_over_a_finalize_parameter_does_not_read_the_members_again(
    datasets: Path,
) -> None:
    """The stage combines once and holds the sum; only the scaling is redone."""
    loads: list[Path] = []
    run = write_run(datasets / 'a.h5', [1.0, 2.0, 3.0, 4.0])
    ref = dataset_ref(path=run)
    inputs = LocalInputs({ref: run})
    aggregation = build(counting_pipeline(loads))
    contribution = aggregation.contribute_workflow()(
        ContributeParams(run=ref, floor=1.5), inputs
    )['contribution']
    held = OutputRef(record='member', output='contribution')
    served = _Served(inputs, {held: contribution})
    combine = aggregation.combine_workflow()

    def params(scale: float) -> Any:
        return NORMALIZE_COMBINE.params.model_validate(
            {'contributions': [held], 'scale': scale}
        )

    stage = combine.stage(params(1.0), {'scale'}, served)
    first = stage(params(1.0), served)
    second = stage(params(2.0), served)
    assert loads == [run]
    assert equal(second['normalized'], first['normalized'] * 2.0)


def test_a_parameter_both_halves_read_reaches_finalize_from_the_contribution(
    datasets: Path,
) -> None:
    """
    ``floor`` decides what a contribution holds, so it is the contribute spec's
    alone and the combine request cannot set it; the finalize half reads it back
    off the contribution with the value the members were reduced with.
    """

    def normalized_over_floor(
        num: Numerator, den: Denominator, scale: Scale, floor: Floor
    ) -> Normalized:
        return Normalized(num / den * scale + sc.scalar(floor))

    run = write_run(datasets / 'a.h5', [1.0, 2.0, 3.0, 4.0])
    ref = dataset_ref(path=run)
    inputs = LocalInputs({ref: run})
    aggregation = build(
        sciline.Pipeline([load_counts, numerator, denominator, normalized_over_floor])
    )
    contribution = aggregation.contribute_workflow()(
        ContributeParams(run=ref, floor=1.5), inputs
    )['contribution']
    held = OutputRef(record='member', output='contribution')
    served = _Served(inputs, {held: contribution})
    params = NORMALIZE_COMBINE.params.model_validate(
        {'contributions': [held], 'scale': 0.0}
    )
    combined = aggregation.combine_workflow()(params, served)
    assert combined['normalized'].sum().value == pytest.approx(4 * 1.5)


class _Served:
    """Inputs that serve contributions a test made, beside the run files."""

    def __init__(self, inputs: LocalInputs, made: dict[Any, Any]) -> None:
        self._inputs = inputs
        self._made = made

    def path(self, ref: Any) -> Path:
        return self._inputs.path(ref)

    def array(self, ref: Any) -> Any:
        return self._made[ref] if ref in self._made else self._inputs.array(ref)


# Through the framework


def contribute(client: Client, run: DatasetRef, **params: Any) -> RunRecord:
    return client.submit(
        client.request(NORMALIZE_CONTRIBUTE, {'run': run, 'floor': 1.5, **params})
    )


def combine(client: Client, *of: RunRecord, output: str = 'contribution') -> RunRecord:
    return client.run(
        NORMALIZE_COMBINE,
        {
            'contributions': [OutputRef(record=r.id, output=output) for r in of],
            'scale': 2.0,
        },
    )


def test_a_member_run_produces_the_contribution_and_no_other_output(
    client: Client, runs: list[DatasetRef]
) -> None:
    (member,) = client.wait([contribute(client, runs[0])])
    assert member.status == Status.COMPLETED, member.failure
    assert member.output_names() == {'contribution'}
    assert set(client.output(member, 'contribution')) == {
        'numerator',
        'denominator',
        'shared',
    }


def test_one_shot_a_batch_and_a_chained_series_agree(
    client: Client, runs: list[DatasetRef]
) -> None:
    one_shot = client.run(NORMALIZE, {'run': runs[0], 'floor': 1.5, 'scale': 2.0})

    # A chained series: one combine record per arrival, over the previous combine.
    first = contribute(client, runs[0])
    client.wait([first])
    chain = combine(client, first)
    second = contribute(client, runs[1])
    client.wait([chain, second])
    chained = combine(client, chain, second)

    # A batch: both members and one combine over them, submitted together.
    batch = client.submit_group(
        {
            'a': client.request(NORMALIZE_CONTRIBUTE, {'run': runs[0], 'floor': 1.5}),
            'b': client.request(NORMALIZE_CONTRIBUTE, {'run': runs[1], 'floor': 1.5}),
            'combine': client.request(
                NORMALIZE_COMBINE,
                {
                    'contributions': [
                        OutputRef(record='@a', output='contribution'),
                        OutputRef(record='@b', output='contribution'),
                    ],
                    'scale': 2.0,
                },
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


def test_members_that_disagree_on_a_shared_parameter_fail_the_combine(
    client: Client, runs: list[DatasetRef]
) -> None:
    """Which parameters may differ is the adapter's knowledge, so the combine
    refuses before computing and the backend never sees the question."""
    a = contribute(client, runs[0], floor=1.5)
    b = contribute(client, runs[1], floor=0.0)
    client.wait([a, b])
    (failed,) = client.wait([combine(client, a, b)])
    assert failed.status == Status.FAILED
    assert 'must share' in failed.failure.message


def test_a_contribution_of_another_spec_passes_validation_and_fails_the_combine(
    client: Client, runs: list[DatasetRef]
) -> None:
    """Chaining between specs is checked by format only, an open point of D15."""
    loaded = client.wait([client.run(LOAD, {'run': runs[0]})])[0]
    assert client.validate(
        client.request(
            NORMALIZE_COMBINE,
            {
                'contributions': [OutputRef(record=loaded.id, output='data')],
                'scale': 2.0,
            },
        )
    ).ok
    (failed,) = client.wait([combine(client, loaded, output='data')])
    assert failed.status == Status.FAILED
