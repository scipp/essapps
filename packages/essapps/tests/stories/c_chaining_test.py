# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Section C of docs/developer/user-stories.md: chaining."""

# ruff: noqa: F821

import pytest

from ess.apps import Client, dataset
from ess.apps.testing import FakeDatasets

from .conftest import BEAM_CENTRE, EXPORT, IOFQ, STITCH, VANADIUM, Measure


@pytest.mark.xfail(reason='Template and apply are not implemented')
def test_c1_beam_centre_feeds_a_batch_of_sample_reductions(
    client: Client, measure: Measure, datasets: FakeDatasets
) -> None:
    centre_run = measure(1, [1.0, 1.0, 1.0, 1.0])
    centre = client.compute(BEAM_CENTRE, {'run': centre_run}, label='beam-centre')
    samples = [
        measure(2, [2.0, 3.0, 4.0, 5.0], role='sample'),
        measure(3, [5.0, 4.0, 3.0, 2.0], role='sample'),
    ]
    template = Template(
        IOFQ, params={'beam_centre': centre.ref('centre')}, blanks=('run',)
    )

    requests = apply(template, samples, datasets, member_field='run')
    reduced = list(client.wait(client.submit(requests, label='iofq')).values())

    assert [client.output(r, 'iofq').values.tolist() for r in reduced] == [
        [3.0, 7.0],
        [7.0, 3.0],
    ]
    assert set(client.provenance(reduced[0]).datasets()) == {samples[0], centre_run}


@pytest.mark.xfail(
    reason='publish, scicat, and datasets.add_published are not implemented'
)
def test_c2_vanadium_from_the_catalogue(
    client: Client, measure: Measure, datasets: FakeDatasets
) -> None:
    vanadium = other.compute(VANADIUM, {'run': measure(1, [1.0, 1.0]), 'scale': 2.0})
    pid = other.publish(vanadium.ref('normalization'), 'scicat')
    published = datasets.add_published(scicat.entries[pid])

    sample = measure(2, [4.0, 8.0])
    result = client.compute(IOFQ, {'run': sample, 'normalization': dataset(pid=pid)})

    assert client.output(result, 'iofq').values.tolist() == [1.0, 2.0]
    assert set(client.provenance(result).datasets()) == {sample, published}
    assert client.records() == [result]


def test_c4_reflectometry_angle_series(client: Client, measure: Measure) -> None:
    reference = measure(10, [2.0, 2.0], role='reference')
    angles = [
        measure(11, [8.0, 4.0], angle=0.5),
        measure(12, [2.0, 1.0], angle=1.0),
        measure(13, [4.0, 2.0], angle=2.0),
        measure(14, [2.0, 0.5], angle=4.0),
    ]
    stitched = client.submit(STITCH, {'runs': angles, 'reference': reference})
    exported = client.compute(EXPORT, {'data': stitched.ref('stitched')})

    assert client.output(exported, 'text') == '4.0,2.0,2.0,1.0,1.0,0.5,0.5,0.125'
    assert set(client.provenance(exported).datasets()) == {reference, *angles}


@pytest.mark.xfail(reason='sessions, stages, and Template are not implemented')
def test_c5_vanadium_and_sample_tuned_together(
    client: Client, measure: Measure
) -> None:
    vanadium_run, sample = measure(1, [1.0, 1.0]), measure(2, [4.0, 8.0])
    with client.session() as session:
        vanadium = session.stage(
            Template(VANADIUM, params={'run': vanadium_run}, blanks=('scale',))
        )
        reduce = session.stage(
            Template(IOFQ, params={'run': sample}, blanks=('normalization',))
        )
        for scale in (1.0, 2.0):
            processed = client.compute(vanadium, {'scale': scale}, label='vanadium')
            reduced = client.compute(
                reduce, {'normalization': processed.ref('normalization')}, label='iofq'
            )

    assert [
        client.output(r, 'iofq').values.tolist() for r in client.records(label='iofq')
    ] == [[2.0, 4.0], [1.0, 2.0]]
    assert reduced.request.params['normalization'] == client.latest('vanadium').ref(
        'normalization'
    )
