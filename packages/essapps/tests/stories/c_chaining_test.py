# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Section C of docs/developer/user-stories.md: chaining."""

# ruff: noqa: F821

import pytest
import scipp as sc

from ess.apps import Client, Template, file
from ess.apps.testing import FakeFiles

from . import toys


def test_c1_beam_centre_feeds_a_batch_of_sample_reductions(
    client: Client, measure: toys.Measure
) -> None:
    centre_run = measure(1, [1.0, 1.0, 1.0, 1.0])
    centre = client.compute(
        toys.BEAM_CENTRE, params={'direct_beam': centre_run}, label='beam-centre'
    )
    samples = [
        measure(2, [2.0, 3.0, 4.0, 5.0], role='sample'),
        measure(3, [5.0, 4.0, 3.0, 2.0], role='sample'),
    ]
    template = Template(
        toys.IOFQ,
        params={'beam_centre': centre.ref('centre'), 'normalization': None},
        blanks=('sample',),
    )

    requests = {
        str(client.files.metadata(f)['run']): template.fill(params={'sample': f})
        for f in samples
    }
    reduced = list(client.compute(requests, label='iofq').values())

    assert [client.output(r, 'iofq').values.tolist() for r in reduced] == [
        [3.0, 7.0],
        [7.0, 3.0],
    ]
    assert set(client.provenance(reduced[0]).files()) == {samples[0], centre_run}


@pytest.mark.xfail(
    reason='publish, scicat, and files.add_published are not implemented'
)
def test_c2_vanadium_from_the_catalogue(
    client: Client, measure: toys.Measure, files: FakeFiles
) -> None:
    vanadium = other.compute(
        toys.VANADIUM,
        params={
            'scaling': toys.Scaling(scale=2.0),
            'vanadium': measure(1, [1.0, 1.0]),
        },
    )
    pid = other.publish(vanadium.ref('normalization'), 'scicat')
    published = files.add_published(scicat.entries[pid])

    sample = measure(2, [4.0, 8.0])
    result = client.compute(
        toys.IOFQ, params={'sample': sample, 'normalization': file(pid=pid)}
    )

    assert client.output(result, 'iofq').values.tolist() == [1.0, 2.0]
    assert result.request.params['normalization'] == published
    assert set(client.provenance(result).files()) == {sample, published}
    assert client.records() == [result]


def test_c4_reflectometry_angle_series(client: Client, measure: toys.Measure) -> None:
    reference = measure(10, [2.0, 2.0], role='reference')
    angles = [
        measure(11, [8.0, 4.0], angle=0.5),
        measure(12, [2.0, 1.0], angle=1.0),
        measure(13, [4.0, 2.0], angle=2.0),
        measure(14, [2.0, 0.5], angle=4.0),
    ]
    series = client.submit(
        toys.STITCH,
        params={
            'runs': angles,
            'reference': reference,
        },
    )
    exported = client.compute(toys.EXPORT, params={'result': series.ref('stitched')})

    assert client.output(exported, 'text') == '4.0,2.0,2.0,1.0,1.0,0.5,0.5,0.125'
    assert set(client.provenance(exported).files()) == {reference, *angles}


def test_c4_angle_series_accumulated_reads_as_the_plain_request(
    client: Client, measure: toys.Measure
) -> None:
    reference = measure(10, [2.0, 2.0], role='reference')
    runs = [
        measure(n, counts)
        for n, counts in [(11, [8.0, 4.0]), (12, [2.0, 1.0]), (13, [4.0, 2.0])]
    ]
    series = client.accumulator(
        Template(toys.STITCH, params={'reference': reference}, blanks=('runs',))
    )
    reads, plains = [], []
    for n, run in enumerate(runs, start=1):
        # STITCH keeps the rows and stitches at each read
        series.push(runs=[run])
        plain = client.compute(
            toys.STITCH,
            params={
                'runs': runs[:n],
                'reference': reference,
            },
        )
        reads.append(client.output(series, 'stitched').curve)
        plains.append(client.output(plain, 'stitched').curve)

    assert all(sc.identical(r, p) for r, p in zip(reads, plains, strict=True))
    assert reads[-1].values.tolist() == [4.0, 2.0, 2.0, 1.0, 1.0, 0.5]


def test_c5_vanadium_and_sample_tuned_together(
    client: Client, measure: toys.Measure
) -> None:
    vanadium_run, sample = measure(1, [1.0, 1.0]), measure(2, [4.0, 8.0])
    vanadium = client.stage(
        Template(toys.VANADIUM, params={'vanadium': vanadium_run}, blanks=('scaling',))
    )
    reduce = client.stage(
        Template(toys.IOFQ, params={'sample': sample}, blanks=('normalization',))
    )
    reduced = []
    for scale in (1.0, 2.0):
        processed = client.compute(
            vanadium, params={'scaling': toys.Scaling(scale=scale)}, label='vanadium'
        )
        normalization = processed.ref('normalization')
        reduced.append(
            client.compute(
                reduce, params={'normalization': normalization}, label='iofq'
            )
        )

    assert [client.output(r, 'iofq').values.tolist() for r in reduced] == [
        [2.0, 4.0],
        [1.0, 2.0],
    ]  # [4, 8] divided by 2, then by 4
    assert client.records(label='iofq') == reduced
    normalization = reduced[-1].request.params['normalization']
    assert normalization == client.latest('vanadium').ref('normalization')
