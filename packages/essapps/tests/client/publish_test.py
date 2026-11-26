# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The software that a record is published with."""

import shutil
import threading
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest
from scitacean.testing.client import FakeClient
from scitacean.testing.transfer import FakeFileTransfer

from ess.apps import Client, Record, Workspace
from ess.apps.log import Log
from ess.apps.scicat import Catalogue, publish
from ess.apps.testing import FakeFiles, FakeStore
from ess.apps.writers import DatasetMetadata
from ess.spec import function_binding

from . import toys

YESTERDAY = ('essapps==1.0', 'scipp==25.10.0')
TODAY = ('essapps==2.0', 'scipp==25.11.0')


@pytest.fixture
def scicat() -> FakeClient:
    return FakeClient.without_login(
        url='https://scicat.example.org/api/v3', file_transfer=FakeFileTransfer()
    )


@pytest.fixture
def start(
    files: FakeFiles, loading: threading.Event
) -> Iterator[Callable[..., Client]]:
    """
    A client of a new workspace over a log and a store, in a process with
    ``software``. Its LOAD waits for ``loading``.
    """
    store = FakeStore()
    workspaces: list[Workspace] = []

    def load(params: toys.Sample) -> toys.LoadOutputs:
        loading.wait(timeout=5)
        return toys.load_run(params)

    def start(log: Path, software: Sequence[str]) -> Client:
        workspaces.append(
            Workspace(
                proposal='p1',
                files=files,
                bind=[
                    function_binding(toys.LOAD, load),
                    function_binding(toys.SHIFT, toys.shift),
                ],
                log=Log(log),
                store=store,
                software=lambda: software,
            )
        )
        return Client(workspaces[-1], submitter='anna')

    yield start
    for workspace in workspaces:
        workspace.close()


def _used_software(
    scicat: FakeClient, client: Client, record: Record, tmp_path: Path
) -> list[str]:
    """The software of the dataset that publishing ``record`` makes."""
    data = tmp_path / 'data.txt'
    data.write_text('1.0')
    catalogue = Catalogue(client=scicat, instrument='loki')
    metadata = DatasetMetadata(name='data', owner='Anna', contact_email='a@b.org')
    pid = publish(catalogue, client, record, [data], metadata, 'text')
    return scicat.get_dataset(pid.removeprefix('pid:')).used_software


def test_a_record_is_published_with_its_software_and_that_of_the_records_it_read(
    start: Callable[..., Client], scicat: FakeClient, tmp_path: Path
) -> None:
    yesterday = start(tmp_path / 'log', YESTERDAY)
    load = yesterday.compute(toys.load_request(1), persist=True)
    shutil.copy(tmp_path / 'log', tmp_path / 'today')
    today = start(tmp_path / 'today', TODAY)
    shifted = today.compute(toys.read_request(load))

    assert _used_software(scicat, today, shifted, tmp_path) == sorted(
        {*YESTERDAY, *TODAY}
    )


def test_a_record_that_did_not_run_adds_unknown_to_the_software_published(
    start: Callable[..., Client],
    loading: threading.Event,
    scicat: FakeClient,
    tmp_path: Path,
) -> None:
    client = start(tmp_path / 'log', TODAY)
    loading.clear()
    load = client.submit(toys.load_request(1))
    cancelled = client.submit(toys.read_request(load))
    client.cancel(cancelled)
    loading.set()
    client.wait(load)

    assert _used_software(scicat, client, cancelled, tmp_path) == sorted(
        {'unknown', *TODAY}
    )
