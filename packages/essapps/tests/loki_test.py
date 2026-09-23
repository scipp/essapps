# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The LoKI session of ``notebooks/loki-session.ipynb``, without the notebook.

Two records chained by a reference, a rebinning through a stage over the Q
binning under a label, and provenance back to the dataset references the
requests name.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip('ess.loki')

from ess.reduce.spec.parameters import QEdges, WavelengthEdges

from ess.apps import loki
from ess.apps.client import Client, local
from ess.apps.records import Template
from ess.apps.sources import FolderSource
from ess.apps.spec import DatasetRef, dataset_ref

RUNS = {
    'sample_run': 60387,
    'sample_transmission_run': 60386,
    'background_run': 60393,
    'background_transmission_run': 60392,
    'empty_beam_run': 60392,
}
DIRECT_BEAM = 'direct-beam-loki-all-pixels.h5'
IDENTITY = r'(?P<run>\d+)-.*'


@pytest.fixture(scope='module')
def cache() -> Path:
    try:
        folder = loki.cache()
    except Exception as e:  # pragma: no cover - depends on the local cache
        pytest.skip(f'the LoKI tutorial files are not available: {e}')
    missing = [run for run in set(RUNS.values()) if not list(folder.glob(f'{run}-*'))]
    if missing or not (folder / DIRECT_BEAM).is_file():
        pytest.skip(f'LoKI tutorial files are not cached in {folder}')
    return folder


@pytest.fixture
def client(cache: Path, tmp_path: Path) -> Iterator[Client]:
    session = local(
        tmp_path / 'store',
        instrument='loki',
        proposal='p1',
        submitter='test',
        registry=loki.registry(),
        sources=[FolderSource(cache, identity=IDENTITY, instrument='loki')],
    )
    yield session
    session.close()


def q_edges(num_bins: int) -> QEdges:
    return QEdges(start=0.01, stop=0.3, num_bins=num_bins)


def test_beam_centre_feeds_iofq_and_provenance_reaches_the_datasets(
    client: Client, cache: Path
) -> None:
    sample = dataset_ref(instrument='loki', run=RUNS['sample_run'])
    refs = {
        name: dataset_ref(instrument='loki', run=run) for name, run in RUNS.items()
    } | {'direct_beam': dataset_ref(path=cache / DIRECT_BEAM)}
    assert sample in [candidate.ref for candidate in client.pick()]

    center = client.run(loki.BEAM_CENTER, {'sample_run': sample})
    assert center.failure is None, center.failure
    assert center.outputs['center']['unit'] == 'm'

    params = refs | {'beam_center': center.ref()}
    rebin = Template(spec=loki.IOFQ, params=params, blanks=('q',), name='iofq')
    first = client.run(rebin, {'q': q_edges(100)})
    assert first.failure is None, first.failure
    assert client.output(first, 'iofq').sizes == {'Q': 100}
    # 60392 is both the background transmission and the empty beam: one file.
    assert len(first.checksums) == len(set(RUNS.values())) + 1

    # A rebinning under the same label: a new record that supersedes the first,
    # served from the stage the first call built.
    second = client.run(rebin, {'q': q_edges(50)})
    assert second.reused
    assert client.output(second, 'iofq').sizes == {'Q': 50}
    assert client.latest('iofq').id == second.id
    assert [r.id for r in client.records(label='iofq')] == [first.id, second.id]

    # Another wavelength binning is another workflow ID, whose stage is not held.
    wavelength = WavelengthEdges(start=1.0, stop=13.0, num_bins=100)
    rewavelength = Template(
        spec=loki.IOFQ, params=params | {'wavelength': wavelength}, blanks=('q',)
    )
    assert not client.run(rewavelength, {'q': q_edges(50)}).reused

    provenance = client.provenance(second)
    (upstream,) = provenance['inputs']
    assert upstream['record'] == center.id
    assert upstream['spec'] == str(loki.BEAM_CENTER.id)
    assert [DatasetRef(**raw) for raw in upstream['raw']] == [sample]
    assert refs['background_run'] in [DatasetRef(**raw) for raw in provenance['raw']]
