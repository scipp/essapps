# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Section C of docs/developer/user-stories.md: chaining.
"""

import pytest

from ess.apps.batch import apply
from ess.apps.client import Client
from ess.apps.examples import EXPORT, HISTOGRAM, LOAD, REBIN, SUBTRACT, SUM
from ess.apps.records import Template
from ess.apps.sources import Dataset
from ess.apps.spec import dataset_ref
from ess.apps.testing import FakeDatasetSource, FakePublisher

from .conftest import Measure


def test_c1_beam_centre_feeds_a_sample_reduction(
    client: Client, measure: Measure
) -> None:
    """LOAD's total stands in for the beam centre, REBIN for the sample reduction."""
    centre = client.run(LOAD, {'run': measure(1, counts=[0.5, 0.5])})
    measure(2, [1.0, 2.0, 3.0, 4.0], role='sample')
    measure(3, [4.0, 3.0, 2.0, 1.0], role='sample')
    samples = [d for d in client.datasets() if d.metadata.get('role') == 'sample']
    reduce = Template(
        spec=REBIN,
        params={'offset': centre.ref('total')},
        blanks=('data',),
        name='reduce',
    )

    reduced = client.wait(client.submit_group(apply(client, reduce, samples)).values())

    assert [client.output(r, 'result').values.tolist() for r in reduced] == [
        [2.0, 3.0, 4.0, 5.0],
        [5.0, 4.0, 3.0, 2.0],
    ]
    provenance = client.provenance(reduced[0])
    assert provenance['raw'] == [{'dataset': 'run:dream/2'}]
    assert [i['raw'] for i in provenance['inputs']] == [[{'dataset': 'run:dream/1'}]]


def test_c2_vanadium_from_the_catalogue(
    service: Client,
    client: Client,
    catalogue: FakeDatasetSource,
    scicat: FakePublisher,
    measure: Measure,
) -> None:
    """The service stands in for the facility that published the vanadium."""
    (vanadium,) = service.wait([service.run(LOAD, {'run': measure(1), 'scale': 0.5})])
    # allow_reused: the toy workflows are bound in process
    pid = service.publish(vanadium.ref('data'), 'scicat', allow_reused=True)
    catalogue.add(Dataset(path=scicat.entries[pid]['path'], pid=pid))

    reduced = client.run(HISTOGRAM, {'data': dataset_ref(pid=pid), 'bins': 2})

    assert client.output(reduced, 'histogram').values.tolist() == [1.5, 3.5]
    assert client.provenance(reduced)['raw'] == [{'dataset': f'pid:{pid}'}]
    assert client.records() == [reduced]  # the vanadium's record is not here


def test_c3_per_bank_diffraction_results(client: Client, measure: Measure) -> None:
    """SUM stands in for the diffraction reduction, its per_run output for the banks."""
    runs = [measure(1, [1.0, 2.0]), measure(2, [5.0, 6.0])]
    loaded = [client.run(LOAD, {'run': run}) for run in runs]
    reduced = client.run(SUM, {'runs': [r.ref('data') for r in loaded]})
    mantle, high_resolution = (reduced.ref('per_run', key) for key in ('0', '1'))

    plot = client.view(mantle)
    exported = client.run(EXPORT, {'data': high_resolution})

    assert reduced.output_keys('per_run') == {'0', '1'}
    assert plot['values'].tolist() == [1.0, 2.0]
    assert exported.request.refs() == [high_resolution]
    assert client.output(exported, 'csv').read_text() == 'x,counts\n0.0,5.0\n1.0,6.0\n'


@pytest.mark.xfail(
    raises=ImportError,
    strict=True,
    reason='no toy spec writes a collection of curves into one file',
)
def test_c4_reflectometry_angle_series(client: Client, measure: Measure) -> None:
    """SUBTRACT stands in for the per-angle reduction, SUM for the stitch."""
    from ess.apps.examples import EXPORT_CURVES

    reference = measure(0, role='reference')
    angles = {
        angle: measure(run, angle=angle)
        for run, angle in enumerate(('0.5', '1.0', '2.0', '4.0'), start=1)
    }

    curves = [
        client.run(SUBTRACT, {'sample': run, 'can': reference})
        for run in angles.values()
    ]
    stitched = client.run(SUM, {'runs': [c.ref('result') for c in curves]})
    scaled = {angle: stitched.ref('per_run', str(i)) for i, angle in enumerate(angles)}
    exported = client.run(EXPORT_CURVES, {'curves': scaled})

    assert stitched.output_keys('per_run') == {'0', '1', '2', '3'}
    assert exported.request.refs() == list(scaled.values())
    assert list(exported.request.params['curves']) == list(angles)


def test_c5_vanadium_and_sample_tuned_together(
    client: Client, measure: Measure
) -> None:
    """LOAD stands in for the vanadium processing, REBIN for the sample reduction."""
    vanadium = Template(
        spec=LOAD,
        params={'run': measure(1, counts=[0.5, 0.5])},
        blanks=('scale',),
        name='vanadium',
    )
    sample = Template(
        spec=REBIN, params={'data': measure(2)}, blanks=('offset',), name='sample'
    )

    results = []
    for scale in (1.0, 2.0):
        processed = client.run(vanadium, {'scale': scale})
        reduced = client.run(sample, {'offset': processed.ref('total')})
        results.append(client.output(reduced, 'result').values.tolist())

    assert results == [[2.0, 3.0, 4.0, 5.0], [3.0, 4.0, 5.0, 6.0]]
    assert reduced.request.refs() == [client.latest('vanadium').ref('total')]
