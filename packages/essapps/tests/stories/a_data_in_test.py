# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Section A of docs/developer/user-stories.md: getting data in.
"""

from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from ess.apps.backend import SubmitError
from ess.apps.client import Client, local
from ess.apps.examples import LOAD, registry, write_run
from ess.apps.sources import Dataset, FolderSource
from ess.apps.spec import DatasetRef
from ess.apps.testing import FakeDatasetSource

from .conftest import Measure

Ingest = Callable[..., DatasetRef]


@pytest.fixture
def ingest(tmp_path: Path, catalogue: FakeDatasetSource) -> Ingest:
    """
    Ingest a dataset into SciCat: its file lands and the catalogue lists it by PID.

    Returns the dataset's identity, which is its PID.
    """
    folder = tmp_path / 'scicat'
    folder.mkdir()

    def ingest(
        pid: str, counts: Sequence[float] = (1.0, 2.0, 3.0, 4.0), run: int | None = None
    ) -> DatasetRef:
        path = write_run(folder / f'{pid.rsplit("/", 1)[-1]}.h5', list(counts))
        dataset = Dataset(path=path, pid=pid, instrument='dream', run=run)
        catalogue.add(dataset)
        return dataset.ref

    return ingest


def test_a1_browse_a_local_folder_next_to_a_catalogue_reference(
    tmp_path: Path, catalogue: FakeDatasetSource, ingest: Ingest
) -> None:
    """LOAD stands in for the instrument's preview spec."""
    folder = tmp_path / 'local'
    folder.mkdir()
    a = write_run(folder / 'sample_a.h5', [1.0, 2.0])
    b = write_run(folder / 'sample_b.h5', [3.0, 1.0])
    reference = ingest('20.500.12269/vanadium', [2.0, 2.0])
    app = local(
        tmp_path / 'app',
        instrument='dream',
        proposal='p1',
        submitter='simon',
        registry=registry(),
        sources=[FolderSource(folder), catalogue],
    )

    listed = app.datasets()
    plots = [app.run(LOAD, {'run': dataset.ref}) for dataset in listed]
    views = [app.view(plot.ref('data'))['values'].tolist() for plot in plots]
    records = app.records()
    app.close()

    assert [plot.request.params['run'] for plot in plots] == [
        {'dataset': f'path:{a}'},
        {'dataset': f'path:{b}'},
        {'dataset': 'pid:20.500.12269/vanadium'},
    ]
    assert views == [[1.0, 2.0], [3.0, 1.0], [2.0, 2.0]]
    assert records == plots  # the listing recorded nothing
    assert catalogue.located == [reference]  # located only when its run needed it


@pytest.mark.xfail(
    raises=SubmitError,
    strict=True,
    reason='a run number is not resolved to a dataset at submission; a data '
    'field takes only a reference',
)
def test_a2_run_number_instead_of_file(client: Client, ingest: Ingest) -> None:
    dataset = ingest('20.500.12269/4711', run=4711)

    loaded = client.run(LOAD, {'run': 4711})

    assert loaded.request.datasets() == [dataset]


@pytest.mark.xfail(
    raises=AssertionError,
    strict=True,
    reason='a catalogue file is located again at every run and never kept as a '
    'copy in the data store',
)
def test_a3_work_without_the_facility_mount(
    client: Client, catalogue: FakeDatasetSource, ingest: Ingest
) -> None:
    vanadium = ingest('20.500.12269/vanadium')

    runs = [client.run(LOAD, {'run': vanadium, 'scale': scale}) for scale in (1.0, 2.0)]

    assert catalogue.located == [vanadium]  # fetched once
    assert [run.request.datasets() for run in runs] == [[vanadium], [vanadium]]


@pytest.mark.xfail(
    raises=AttributeError,
    strict=True,
    reason='no way to copy a local file into the data store, so there are no '
    'bytes to drop',
)
def test_a4_mistaken_copy_into_the_shared_service(
    service: Client, tmp_path: Path
) -> None:
    private = service.upload(write_run(tmp_path / 'private.h5', [1.0, 2.0]))
    (loaded,) = service.wait([service.run(LOAD, {'run': private})])

    service.drop(private)
    (recomputed,) = service.wait([service.recompute(loaded)])

    assert service.record(loaded.id) == loaded
    assert recomputed.failure.kind == 'missing-dataset'


@pytest.mark.xfail(
    raises=AssertionError,
    strict=True,
    reason='the catalogue fake only adds entries, and the listing keeps the first '
    'entry per dataset, so a corrected entry is not shown',
)
def test_a5_metadata_corrected_after_the_fact(
    client: Client, catalogue: FakeDatasetSource, measure: Measure
) -> None:
    runs = [measure(run, sample='water') for run in range(1, 11)]
    reduced = [client.run(LOAD, {'run': run}) for run in runs]
    fourth = client.datasets()[3]

    catalogue.add(replace(fourth, metadata={'sample': 'heavy water'}))  # in SciCat
    listed = client.datasets()

    assert listed[3].metadata == {'sample': 'heavy water'}
    assert client.records() == reduced
