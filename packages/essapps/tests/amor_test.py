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
from ess.apps.records import StageRecord
from ess.apps.sources import FolderSource
from ess.apps.spec import dataset_ref

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


def members(client: Client, **overrides: Any) -> dict[int, StageRecord]:
    """
    One reduced sample run per rotation: one batch, as ``apply`` would submit it.

    All four are one stage over the sample run with the same params,
    share the label, and are told apart by their member key.
    """
    params = reflectivity_params(SAMPLE_RUNS[0], **overrides)
    del params['sample_run']
    stage = client.workflow(amor.REFLECTIVITY, params).stage(
        inputs=['sample_run'], label=LABEL
    )
    records = {}
    for run in SAMPLE_RUNS:
        record = stage.compute(
            {'sample_run': dataset_ref(instrument='amor', run=run)},
            member_key=str(run),
        )
        assert record.failure is None, record.failure
        records[run] = record
    return records


def test_curves_of_four_rotations_stitch_into_one(client: Client) -> None:
    curves = members(client)
    for run, record in curves.items():
        assert client.output(record, 'reflectivity').sizes == {'Q': 200}, run
    # The first member builds the stage over the sample run and the other three
    # are served from it.
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
    # The rerun supersedes the member it corrects. Its scale factor gives it a
    # workflow ID of its own, so no held stage fits it.
    assert rescaled.supersedes == curves[608].id
    assert not rescaled.reused
    assert rescaled.resolved_params['scale_factor'] == factor
    expected = client.output(combined, 'scaled', key='608')
    assert client.output(rescaled, 'reflectivity').sum().value == pytest.approx(
        expected.sum().value
    )


def test_a_stage_over_the_bin_count_is_served_after_its_first_call(
    client: Client,
) -> None:
    params = reflectivity_params(SAMPLE_RUNS[0])
    del params['q_num_bins']
    rebin = client.workflow(amor.REFLECTIVITY, params).stage(
        inputs=['q_num_bins'], label='rebin'
    )
    first, second, third = (rebin.compute({'q_num_bins': n}) for n in (200, 100, 50))
    assert [r.reused for r in (first, second, third)] == [False, True, True]
    assert client.output(third, 'reflectivity').sizes == {'Q': 50}
