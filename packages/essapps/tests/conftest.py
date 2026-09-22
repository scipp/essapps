# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from ess.apps.client import Client, local
from ess.apps.examples import LOAD, registry, write_run
from ess.apps.rules import Template
from ess.apps.sources import DatasetSource, FolderSource
from ess.apps.spec import DatasetRef, dataset_ref


@pytest.fixture
def datasets(tmp_path: Path) -> Path:
    """The folder the local application reads its datasets from."""
    folder = tmp_path / 'datasets'
    folder.mkdir()
    return folder


def make_client(
    root: Path,
    datasets_folder: Path,
    *,
    sources: Iterable[DatasetSource] = (),
    **kwargs: Any,
) -> Client:
    """A client over a fresh store, with the folder source every test needs."""
    return local(
        root,
        instrument='dream',
        proposal='p1',
        submitter='simon',
        registry=registry(),
        sources=[FolderSource(datasets_folder, '*.h5'), *sources],
        **kwargs,
    )


@pytest.fixture
def client(tmp_path: Path, datasets: Path):
    client = make_client(tmp_path / 'store', datasets)
    yield client
    client.close()


@pytest.fixture
def run_file(datasets: Path) -> Path:
    return write_run(datasets / 'dream_1.h5', [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0])


@pytest.fixture
def run_ref(run_file: Path) -> DatasetRef:
    """The run identity ``dream_1.h5`` carries; the folder source locates it."""
    return dataset_ref(instrument='dream', run=1)


@pytest.fixture
def template(client: Client, run_ref: DatasetRef) -> Template:
    request = client.request(LOAD, {'run': run_ref, 'scale': 2.0})
    return Template.from_request('load-defaults', request, LOAD)
