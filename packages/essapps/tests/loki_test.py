# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The LoKI session of ``notebooks/loki-session.ipynb``, without the notebook.

Two records chained by a reference, a rebinning under a label, and provenance
back to the dataset references the requests name.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip('ess.loki')

from ess.apps import loki
from ess.apps.client import Client, local
from ess.apps.sources import FolderSource
from ess.apps.spec import DatasetRef
from ess.apps.testing import LocalInputs

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


def iofq_params(
    inputs: dict[str, Any], center: Any, **overrides: Any
) -> dict[str, Any]:
    return inputs | {'beam_center': center, 'q_bins': 100} | overrides


def test_beam_centre_feeds_iofq_and_provenance_reaches_the_datasets(
    client: Client, cache: Path
) -> None:
    sample = DatasetRef(instrument='loki', run=RUNS['sample_run'])
    refs = {
        name: DatasetRef(instrument='loki', run=run) for name, run in RUNS.items()
    } | {'direct_beam': DatasetRef(path=cache / DIRECT_BEAM)}
    assert sample in [candidate.ref for candidate in client.pick()]

    center = client.run(loki.BEAM_CENTER, {'sample_run': sample})
    assert center.failure is None, center.failure
    assert center.outputs['center']['unit'] == 'm'

    first = client.run(loki.IOFQ, iofq_params(refs, center.ref()), label='iofq')
    assert first.failure is None, first.failure
    assert client.output(first, 'iofq').sizes == {'Q': 100}
    # 60392 is both the background transmission and the empty beam: one file.
    assert len(first.checksums) == len(set(RUNS.values())) + 1

    # A rebinning under the same label: a new record that supersedes the first.
    second = client.run(
        loki.IOFQ, iofq_params(refs, center.ref(), q_bins=50), label='iofq'
    )
    assert second.reused
    assert client.output(second, 'iofq').sizes == {'Q': 50}
    assert client.latest('iofq').id == second.id
    assert [r.id for r in client.records(label='iofq')] == [first.id, second.id]

    provenance = client.provenance(second)
    (upstream,) = provenance['inputs']
    assert upstream['record'] == center.id
    assert upstream['spec'] == str(loki.BEAM_CENTER.id)
    assert [DatasetRef(**raw) for raw in upstream['raw']] == [sample]
    assert refs['background_run'] in [DatasetRef(**raw) for raw in provenance['raw']]


def test_only_a_change_to_a_stage_input_reuses_the_warm_stage(cache: Path) -> None:
    """
    ``WarmPipeline.reused`` is the signal that the held part was kept, and
    it is what a record's ``reused`` flag reports: the run came out of the warm
    stage rather than merely out of a kept callable.
    """
    paths = {name: next(cache.glob(f'{run}-*')) for name, run in RUNS.items()} | {
        'direct_beam': cache / DIRECT_BEAM
    }
    refs = {name: DatasetRef(path=path) for name, path in paths.items()}
    inputs = LocalInputs({ref: paths[name] for name, ref in refs.items()})
    center = loki.beam_center_workflow()(
        loki.BeamCenterParams(sample_run=refs['sample_run']), inputs
    )['center']
    workflow = loki.iofq_workflow()

    def call(**overrides: Any) -> None:
        workflow(loki.IofQParams(**iofq_params(refs, center, **overrides)), inputs)

    call()
    assert not workflow.reused
    call(q_bins=50)
    assert workflow.reused
    call(wavelength_bins=100)
    assert not workflow.reused
