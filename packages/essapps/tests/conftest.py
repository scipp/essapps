# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
import threading
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from ess.apps.client import Client, local, local_backend
from ess.apps.examples import LOAD, registry, write_run
from ess.apps.remote import remote
from ess.apps.rules import Template
from ess.apps.server import create_app
from ess.apps.sources import DatasetSource, FolderSource
from ess.apps.spec import DatasetRef, dataset_ref
from ess.apps.testing import FakePublisher


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


@pytest.fixture
def server_url(tmp_path: Path, datasets: Path) -> Iterator[str]:
    """A real server, in a background thread, listening on a free port."""
    backend = local_backend(
        tmp_path / 'server',
        registry='ess.apps.examples:registry',
        throwaway=True,
        sources=[FolderSource(datasets, '*.h5')],
        publishers={'fake': FakePublisher()},
    )
    app = create_app(backend, poll_interval=0.05)
    config = uvicorn.Config(app, host='127.0.0.1', port=0, log_level='warning')
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f'http://127.0.0.1:{port}'
    server.should_exit = True
    thread.join()
    backend.close()


@pytest.fixture
def remote_client(server_url: str) -> Iterator[Client]:
    client = remote(server_url, instrument='dream', proposal='p1', submitter='simon')
    yield client
    client.close()
