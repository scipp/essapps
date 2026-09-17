# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from pathlib import Path

import pytest

from ess.apps.client import local
from ess.apps.examples import registry, write_run
from ess.apps.sources import FolderSource
from ess.apps.spec import DatasetRef


@pytest.fixture
def datasets(tmp_path: Path) -> Path:
    """The folder the local application reads its datasets from."""
    folder = tmp_path / 'datasets'
    folder.mkdir()
    return folder


@pytest.fixture
def client(tmp_path: Path, datasets: Path):
    client = local(
        tmp_path / 'store',
        instrument='dream',
        proposal='p1',
        submitter='simon',
        registry=registry(),
        sources=[FolderSource(datasets, '*.h5')],
    )
    yield client
    client.close()


@pytest.fixture
def run_file(datasets: Path) -> Path:
    return write_run(datasets / 'dream_1.h5', [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0])


@pytest.fixture
def run_ref(run_file: Path) -> DatasetRef:
    """The run identity ``dream_1.h5`` carries; the folder source locates it."""
    return DatasetRef(instrument='dream', run=1)
