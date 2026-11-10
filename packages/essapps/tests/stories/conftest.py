# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)

"""
Fixtures of the user stories (docs/developer/user-stories.md), over the toys.

After each story, every record whose outputs the backend still keeps is run
again as its request by a client, and must give the same outputs (``replay``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from ess.apps import (
    Backend,
    Client,
    NotReadable,
    Record,
    SpecId,
    Status,
)
from ess.apps.log import Log
from ess.apps.testing import (
    FakeFiles,
    FakeStore,
)
from ess.spec import (
    FileRef,
    OutputRef,
    Ref,
    WorkflowSpec,
)
from ess.spec.refs import map_refs
from ess.spec.testing import assert_close

from .toys import (
    TOYS,
    Measure,
)


@pytest.fixture
def files(tmp_path: Path) -> FakeFiles:
    return FakeFiles(proposal='p1', directory=tmp_path)


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def history() -> Log:
    """The history of the backend, in memory, which an upgrade takes over."""
    return Log()


@pytest.fixture
def backends(
    files: FakeFiles, store: FakeStore, history: Log
) -> Iterator[list[Backend]]:
    """The backend, and those that upgrades started after it; the current one last."""
    backends = [
        Backend(
            proposal='p1',
            files=files,
            bind=list(TOYS.values()),
            log=history,
            store=store,
        )
    ]
    yield backends
    for backend in backends:
        backend.close()


@pytest.fixture
def clients() -> list[Client]:
    """Every client that ``connect`` and ``upgrade`` made."""
    return []


@pytest.fixture
def upgrade(
    files: FakeFiles,
    store: FakeStore,
    history: Log,
    backends: list[Backend],
    clients: list[Client],
) -> Callable[..., Client]:
    """
    A client of a new backend over the same files, history, and store,
    which offers the given toy specs, every one by default; ``connect`` then
    makes clients of it. The upgrade closes the backend before it, which
    cancels the work that persists nothing and waits for the rest.
    """

    def upgrade(specs: list[WorkflowSpec[Any, Any]] | None = None) -> Client:
        backends[-1].close()
        bind = (
            list(TOYS.values())
            if specs is None
            else [TOYS[SpecId.of(s)] for s in specs]
        )
        backends.append(
            Backend(proposal='p1', files=files, bind=bind, log=history, store=store)
        )
        clients.append(Client(backends[-1], submitter='anna'))
        return clients[-1]

    return upgrade


@pytest.fixture
def connect(backends: list[Backend], clients: list[Client]) -> Callable[..., Client]:
    """A new client of the current backend, of proposal p1."""

    def connect(user: str = 'anna') -> Client:
        clients.append(Client(backends[-1], submitter=user))
        return clients[-1]

    return connect


@pytest.fixture
def client(connect: Callable[..., Client]) -> Client:
    return connect()


@pytest.fixture
def measure(files: FakeFiles) -> Measure:
    """
    Make run ``n`` appear as a file that holds the given counts, with the
    given metadata; return its identity.
    """

    def measure(n: int, counts: list[float], **fields: Any) -> FileRef:
        return files.measure(n, counts, **fields)

    return measure


@pytest.fixture
def corrupt(files: FakeFiles) -> Callable[..., None]:
    return files.corrupt


@pytest.fixture
def repair(files: FakeFiles) -> Callable[..., None]:
    return files.repair


@pytest.fixture(autouse=True)
def replay(backends: list[Backend], clients: list[Client]) -> Iterator[None]:
    """
    After the story, a client of the backend that is open runs every completed
    record whose outputs the backend keeps again as its request, and compares
    the outputs.

    This is a safety net over every story: a record says all that its outputs
    depend on. A reference to an output the backend does not keep, such as one
    of the record of an accumulator's state, is replaced by the same output of
    that record run again. No record names an accumulator in a reference:
    each names a record that the provenance finds. It runs before the
    backends are closed.
    """
    yield
    for client in list(clients):
        try:
            records = {r.id: r for r in client.records()}
        except RuntimeError:  # its backend is closed
            continue
        client.wait(list(records.values()))
        for record in records.values():
            client.provenance(record)
        _replay(client, records)
        return  # every client of the backend reads the same values


def _replay(client: Client, records: Mapping[str, Record]) -> None:
    replays: dict[str, Record] = {}

    def kept(ref: OutputRef) -> bool:
        try:
            client.output(records[ref.record], ref.output)
        except NotReadable:
            return False
        return True

    def again(record: Record) -> Record:
        def source(ref: Ref) -> Ref:
            if isinstance(ref, OutputRef) and not kept(ref):
                return ref.model_copy(update={'record': again(records[ref.record]).id})
            return ref

        if record.id not in replays:
            request = record.request
            params = map_refs(request.params, source)
            replays[record.id] = client.compute(record.spec, params=params)
        return replays[record.id]

    for record in list(records.values()):
        if client.status(record) is not Status.COMPLETED:
            continue
        try:
            expected = client.output(record)
        except NotReadable:  # released, or not persisted
            continue
        where = f'{record.id} {record.spec} {record.label} {record.member}'
        assert_close(client.output(again(record)), expected, where=where)
