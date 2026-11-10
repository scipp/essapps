# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)


"""Waiting for pending references, failure and its propagation, cancelling."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from ess.apps import Client, Record, RecordFailed, Request, Status, Workspace, file
from ess.apps.log import Event, Log
from ess.apps.testing import FakeFiles
from ess.spec import Binding

from . import toys


class FullDisk(Log):
    """A log in memory whose appends fail once the disk is full."""

    def __init__(self) -> None:
        super().__init__()
        self.full = False
        self.closed = False

    def append(self, event: Event) -> Event:
        if self.full:
            raise OSError('no space left on device')
        return super().append(event)

    def close(self) -> None:
        self.closed = True
        super().close()


def _sum(*records: Record, output: str = 'value') -> Request:
    """TOTAL of an output of each record."""
    return Request(toys.TOTAL, params={'parts': [r.ref(output) for r in records]})


def test_a_request_waits_for_its_pending_references(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    a, b = client.submit([toys.load_request(1), toys.load_request(2)])
    total = client.submit(_sum(a, b))

    assert client.status(total) is Status.PENDING
    loading.set()
    assert client.output(total, 'value') == toys.Amount(value=3.0)


def test_a_failed_input_fails_everything_downstream(
    client: Client, loading: threading.Event, unreadable: Request
) -> None:
    loading.clear()
    bad, good = client.submit([unreadable, toys.load_request(1)])
    first = client.submit(_sum(bad, good))
    second = client.submit(_sum(first, good))
    loading.set()

    assert client.wait([bad, good, first, second]) == [
        Status.FAILED,
        Status.COMPLETED,
        Status.FAILED,
        Status.FAILED,
    ]
    assert client.failure(first) == f'record {bad.id} failed'
    assert client.failure(second) == f'record {first.id} failed'


def test_a_long_chain_fails_as_a_whole(
    client: Client, loading: threading.Event, unreadable: Request
) -> None:
    loading.clear()
    chain = [client.submit(unreadable)]
    for _ in range(1500):
        a, b = chain[-1].ref('value'), chain[0].ref('extra')
        chain.append(client.submit(toys.TOTAL, params={'parts': [a, b]}))
    loading.set()

    assert set(client.wait(chain)) == {Status.FAILED}


def test_a_failure_names_the_type_of_the_workflow_s_error(
    client: Client, unreadable: Request
) -> None:
    failed = client.compute(unreadable)

    assert client.failure(failed) == 'OSError: file signature not found'


def test_a_workflow_that_returns_none_as_a_required_output_fails(
    client: Client,
) -> None:
    nothing = client.compute(toys.NOTHING, params={'sample': file(run=1)})

    assert (
        client.failure(nothing) == 'the workflow of nothing/v1 returned None as value'
    )


def test_a_workflow_that_returns_no_outputs_instance_fails(client: Client) -> None:
    untyped = client.compute(toys.UNTYPED, params={'parts': [toys.Amount(value=1.0)]})

    assert client.failure(untyped) == (
        'the workflow of untyped/v1 returned dict, not Value'
    )


def test_cancel_ends_what_has_not_started(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    a, b = client.submit([toys.load_request(1), toys.load_request(2)])
    total = client.submit(_sum(a, b))
    client.cancel(total)
    loading.set()

    assert client.wait([a, b, total]) == ['completed', 'completed', 'cancelled']


def test_cancel_drops_what_is_running(
    in_place: Client, loads: list[Record], copying: toys.Copying
) -> None:
    client = in_place
    copying.go.clear()
    running = client.submit(toys.COPY, params={'source': loads[0].ref('value')})
    assert copying.started.wait(timeout=5)
    client.cancel(running)
    copying.go.set()

    assert client.wait(running) is Status.CANCELLED
    with pytest.raises(
        RecordFailed, match=f'^record {running.id} cancelled$'
    ) as raised:
        client.output(running, 'value')
    assert (raised.value.status, raised.value.reason) == (Status.CANCELLED, None)


def test_a_failed_append_stops_the_workspace(
    files: FakeFiles, bindings: list[Binding], loading: threading.Event
) -> None:
    log = FullDisk()
    workspace = Workspace(proposal='p1', files=files, bind=bindings, log=log)
    client = Client(workspace, submitter='anna')
    loading.clear()
    record = client.submit(toys.load_request(1))
    log.full = True
    threading.Timer(0.05, loading.set).start()  # once the wait below has started

    with pytest.raises(RuntimeError, match='no space left on device'):
        client.wait(record)  # its Finished event was not appended
    with pytest.raises(RuntimeError, match='no space left on device'):
        client.submit(toys.load_request(2))
    with pytest.raises(RuntimeError, match='no space left on device'):
        workspace.close()


def test_a_failed_append_wakes_what_waits(
    files: FakeFiles, bindings: list[Binding], loading: threading.Event
) -> None:
    log = FullDisk()
    workspace = Workspace(proposal='p1', files=files, bind=bindings, log=log)
    client = Client(workspace, submitter='anna')
    loading.clear()
    record = client.submit(toys.load_request(1))
    cursor = client.changes().cursor

    with ThreadPoolExecutor() as pool:
        waiting = pool.submit(client.changes, after=cursor, timeout=60)
        time.sleep(0.05)  # so that it is waiting
        log.full = True
        with pytest.raises(OSError, match='no space left on device'):
            client.cancel(record)  # nothing else wakes the wait
        # Well within the 5 s after which ``load`` gives up and wakes it anyway.
        with pytest.raises(RuntimeError, match='no space left on device'):
            waiting.result(timeout=2)
    loading.set()
    with pytest.raises(RuntimeError, match='no space left on device'):
        workspace.close()


def test_a_failed_append_while_closing_lets_go_and_raises(
    files: FakeFiles, bindings: list[Binding], loading: threading.Event
) -> None:
    log = FullDisk()
    workspace = Workspace(proposal='p1', files=files, bind=bindings, log=log)
    client = Client(workspace, submitter='anna', on_close=workspace.close)
    loading.clear()
    client.submit(toys.load_request(1))  # closing cancels it, which logs
    log.full = True
    threading.Timer(0.05, loading.set).start()  # closing waits for the workflow

    with pytest.raises(RuntimeError, match='no space left on device') as raised:
        client.close()
    assert isinstance(raised.value.__cause__, OSError)
    assert log.closed
    events = len(log)
    with pytest.raises(RuntimeError, match='no space left on device'):
        client.close()
    assert len(log) == events
