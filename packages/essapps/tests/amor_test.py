# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Amor's four sample rotations reduced and stitched through the framework.

Collection outputs consumed whole and element by element, and a combine that is
not additive.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip('ess.amor')

from ess.apps import amor
from ess.apps.client import Client, local
from ess.apps.records import RunRecord
from ess.apps.sources import FolderSource
from ess.apps.spec import dataset_ref
from ess.apps.stages import Stages
from ess.apps.testing import LocalInputs

SAMPLE_RUNS = (608, 609, 610, 611)
REFERENCE_RUN = 614
IDENTITY = r'amor\d+n(?P<run>\d+)'
CRITICAL_EDGE = {'start': 0.01, 'stop': 0.014}
LABEL = 'reflectivity'

# The tutorial files carry transformations scippnexus cannot read, and the
# tutorial silences the warnings; ``filterwarnings = ["error"]`` in the project
# configuration would otherwise turn them into failures.
pytestmark = [
    pytest.mark.filterwarnings('ignore:Failed to convert'),
    pytest.mark.filterwarnings('ignore:Invalid transformation'),
    pytest.mark.filterwarnings('ignore:invalid value encountered'),
]


@pytest.fixture(scope='module')
def cache() -> Path:
    try:
        folder = amor.cache()
    except Exception as e:  # pragma: no cover - depends on the local cache
        pytest.skip(f'the Amor tutorial files are not available: {e}')
    missing = [
        run
        for run in (*SAMPLE_RUNS, REFERENCE_RUN)
        if not list(folder.glob(f'*{run}.hdf'))
    ]
    if missing:
        pytest.skip(f'Amor tutorial runs {missing} are not cached in {folder}')
    return folder


@pytest.fixture
def client(cache: Path, tmp_path: Path) -> Iterator[Client]:
    session = local(
        tmp_path / 'store',
        instrument='amor',
        proposal='p1',
        submitter='test',
        registry=amor.registry(),
        sources=[FolderSource(cache, identity=IDENTITY, instrument='amor')],
    )
    yield session
    session.close()


def reflectivity_params(run: int, **overrides: Any) -> dict[str, Any]:
    return {
        'sample_run': dataset_ref(instrument='amor', run=run),
        'reference_run': dataset_ref(instrument='amor', run=REFERENCE_RUN),
        'q_num_bins': 200,
    } | overrides


def members(client: Client, **overrides: Any) -> dict[int, RunRecord]:
    """
    One reduced sample run per rotation: one batch, as ``apply`` would submit it.

    All four share the label and are told apart by their member key, so each one
    after the first has the previous member as its predecessor (D14).
    """
    records = {}
    for run in SAMPLE_RUNS:
        record = client.run(
            amor.REFLECTIVITY,
            reflectivity_params(run, **overrides),
            label=LABEL,
            member_key=str(run),
        )
        assert record.failure is None, record.failure
        records[run] = record
    return records


def test_curves_of_four_rotations_stitch_into_one(client: Client) -> None:
    curves = members(client)
    for run, record in curves.items():
        assert client.output(record, 'reflectivity').sizes == {'Q': 200}, run
    # The binding names the sample run as the default stage input, so the first
    # member builds a stage over it and the other three, which differ from their
    # predecessor in nothing else, are served from it.
    assert [r.reused for r in curves.values()] == [False, True, True, True]

    combined = client.run(
        amor.COMBINE,
        {
            'curves': {str(run): r.ref('reflectivity') for run, r in curves.items()},
            'critical_edge': CRITICAL_EDGE,
        },
        label='stitched',
    )
    assert combined.failure is None, combined.failure
    assert client.output(combined, 'combined').sizes == {'Q': 200}

    # The scaled curves are a collection output: stored and served by key.
    assert combined.output_keys('scaled') == {str(run) for run in SAMPLE_RUNS}
    for run in SAMPLE_RUNS:
        assert client.output(combined, 'scaled', key=str(run)).sizes == {'Q': 200}

    # Ranges differ per rotation, so the fit has something to do.
    factors = client.output(combined, 'scale_factors')
    assert set(factors) == {str(run) for run in SAMPLE_RUNS}
    assert len(set(factors.values())) == len(SAMPLE_RUNS)

    provenance = client.provenance(combined)
    assert {p['record'] for p in provenance['inputs']} == {
        r.id for r in curves.values()
    }


def test_an_element_of_a_collection_output_feeds_the_next_combine(
    client: Client,
) -> None:
    curves = members(client)
    first = client.run(
        amor.COMBINE,
        {
            'curves': {
                str(run): curves[run].ref('reflectivity') for run in SAMPLE_RUNS[:2]
            },
            'critical_edge': CRITICAL_EDGE,
        },
    )
    assert first.failure is None, first.failure

    # One element of the first combine's collection output, by key, next to a
    # curve that has not been through a combine at all.
    second = client.run(
        amor.COMBINE,
        {
            'curves': {
                '608': first.ref('scaled', key='608'),
                '610': curves[610].ref('reflectivity'),
            },
            'critical_edge': CRITICAL_EDGE,
        },
    )
    assert second.failure is None, second.failure
    assert client.output(second, 'combined').sizes == {'Q': 200}
    assert [r.record for r in second.request.refs()] == [first.id, curves[610].id]


def test_a_fitted_scale_factor_feeds_back_into_the_member_that_produced_it(
    client: Client,
) -> None:
    """The round trip the tutorial's ``scale_to_overlap`` does in one call."""
    curves = members(client)
    combined = client.run(
        amor.COMBINE,
        {
            'curves': {str(run): r.ref('reflectivity') for run, r in curves.items()},
            'critical_edge': CRITICAL_EDGE,
        },
    )
    assert combined.failure is None, combined.failure
    factor = client.output(combined, 'scale_factors')['608']
    assert factor != 1.0

    rescaled = client.run(
        amor.REFLECTIVITY,
        reflectivity_params(608, scale_factor=combined.ref('scale_factors', key='608')),
        label=LABEL,
        member_key='608',
    )
    assert rescaled.failure is None, rescaled.failure
    # The rerun supersedes the member it corrects, so its predecessor is that
    # member and the field they differ in is the scale factor. The members' stage
    # fixes that field, so this request does not fit it and builds its own.
    assert not rescaled.reused
    assert rescaled.resolved_params['scale_factor'] == factor
    expected = client.output(combined, 'scaled', key='608')
    assert client.output(rescaled, 'reflectivity').sum().value == pytest.approx(
        expected.sum().value
    )


def local_inputs(cache: Path) -> LocalInputs:
    """The tutorial files by the reference the dataset source gives them."""
    return LocalInputs(
        {
            dataset_ref(instrument='amor', run=run): next(cache.glob(f'*{run}.hdf'))
            for run in (*SAMPLE_RUNS, REFERENCE_RUN)
        }
    )


def test_without_the_bindings_hint_the_second_member_names_the_stage_input(
    cache: Path,
) -> None:
    """
    A new member of a batch has the previous member as its predecessor.

    The first member has no predecessor and, without a default, nothing to build
    a stage from: it computes everything and holds nothing. The second differs
    from it in the sample run alone, which names the stage input, and the rest of
    the batch is served from that stage.
    """
    inputs = local_inputs(cache)
    workflow = amor.reflectivity_workflow()
    workflow.default_stage_inputs = frozenset()
    stages = Stages()
    reused = []
    for run in SAMPLE_RUNS:
        params = amor.ReflectivityParams(**reflectivity_params(run))
        called, hit = stages.workflow_for(
            amor.REFLECTIVITY.id,
            workflow,
            params,
            inputs,
            label=LABEL,
            member_key=str(run),
        )
        called(params, inputs)
        reused.append(hit)
    assert reused == [False, False, True, True]


def test_a_moved_binning_parameter_builds_a_stage_of_its_own(cache: Path) -> None:
    """
    The first request stages over the sample run, which is the binding's hint.
    Moving the bin count fits no held stage, so the session stages over that
    instead, and a further move is served from it.
    """
    paths = {
        run: next(cache.glob(f'*{run}.hdf')) for run in (SAMPLE_RUNS[0], REFERENCE_RUN)
    }
    refs = {name: dataset_ref(path=path) for name, path in paths.items()}
    inputs = LocalInputs({ref: paths[name] for name, ref in refs.items()})
    workflow = amor.reflectivity_workflow()
    stages = Stages()

    def call(**overrides: Any) -> bool:
        params = amor.ReflectivityParams(
            **{
                'sample_run': refs[SAMPLE_RUNS[0]],
                'reference_run': refs[REFERENCE_RUN],
                'q_num_bins': 200,
                **overrides,
            }
        )
        called, reused = stages.workflow_for(
            amor.REFLECTIVITY.id, workflow, params, inputs, 'reflectivity'
        )
        called(params, inputs)
        return reused

    assert not call()
    assert not call(q_num_bins=100)
    assert call(q_num_bins=50)
    assert call()
