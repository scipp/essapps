# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)


"""Waiting for pending references, failure and its propagation, cancelling."""

import threading

import pytest

from ess.apps import Backend, Client, RecordFailed, Status, file
from ess.apps.log import Event, Log
from ess.apps.testing import FakeFiles
from ess.spec import function_binding

from .conftest import (
    ADD,
    COUNTS,
    LOAD,
    NOTHING,
    SCALED,
    UNTYPED,
    Amount,
    Gate,
    load,
    operands,
)


class FullDisk(Log):
    """A log in memory whose appends fail once the disk is full."""

    def __init__(self) -> None:
        super().__init__()
        self.full = False

    def append(self, event: Event) -> Event:
        if self.full:
            raise OSError('no space left on device')
        return super().append(event)


def test_a_request_waits_for_its_pending_references(client: Client, gate: Gate) -> None:
    a, b = client.submit([load(1), load(2)])
    total = client.submit(ADD, params=operands(a.ref('value'), b.ref('value')))

    assert client.status(total) is Status.PENDING
    gate.open.set()
    assert client.output(total, 'value') == Amount(value=3.0)


def test_a_reference_inside_a_row_waits_for_its_pending_record(
    client: Client, gate: Gate
) -> None:
    a = client.submit(load(1))
    rows = [{'amount': a.ref('value')}, {'scale': 2.0, 'amount': a.ref('value')}]
    summed = client.submit(SCALED, params={'rows': rows})

    assert client.status(summed) is Status.PENDING
    gate.open.set()
    assert client.output(summed, 'value') == Amount(value=3.0)


def test_a_failed_input_fails_everything_downstream(client: Client, gate: Gate) -> None:
    bad, good = client.submit([load(3), load(1)])
    first = client.submit(ADD, params=operands(bad.ref('value'), good.ref('value')))
    second = client.submit(ADD, params=operands(first.ref('value'), good.ref('value')))
    gate.open.set()

    assert client.wait([bad, good, first, second]) == [
        Status.FAILED,
        Status.COMPLETED,
        Status.FAILED,
        Status.FAILED,
    ]
    assert client.failure(bad) == 'ValueError: negative run'
    assert client.failure(first) == f'record {bad.id} failed'
    assert client.failure(second) == f'record {first.id} failed'


def test_a_long_chain_fails_as_a_whole(client: Client, gate: Gate) -> None:
    chain = [client.submit(load(3))]
    for _ in range(1500):
        a, b = chain[-1].ref('value'), chain[0].ref('value')
        chain.append(client.submit(ADD, params=operands(a, b)))
    gate.open.set()

    assert set(client.wait(chain)) == {Status.FAILED}


def test_a_failure_names_the_type_of_the_workflow_s_error(client: Client) -> None:
    failed = client.compute(COUNTS, params={'sample': file(run=1)})

    assert client.failure(failed) == "KeyError: 'counts'"


def test_a_workflow_that_returns_none_as_a_required_output_fails(
    client: Client,
) -> None:
    nothing = client.compute(NOTHING, params={'sample': file(run=1)})

    assert (
        client.failure(nothing) == 'the workflow of nothing/v1 returned None as value'
    )


def test_a_workflow_that_returns_no_outputs_instance_fails(client: Client) -> None:
    untyped = client.compute(UNTYPED, params={'sample': file(run=1)})

    assert client.failure(untyped) == (
        'the workflow of untyped/v1 returned dict, not ValueOutputs'
    )


def test_cancel_ends_what_has_not_started(client: Client, gate: Gate) -> None:
    a, b = client.submit([load(1), load(2)])
    total = client.submit(ADD, params=operands(a.ref('value'), b.ref('value')))
    client.cancel(total)
    gate.open.set()

    assert client.wait([a, b, total]) == ['completed', 'completed', 'cancelled']


def test_cancel_drops_what_is_running(client: Client, gate: Gate) -> None:
    running = client.submit(load(1))
    assert gate.started.wait(timeout=5)
    client.cancel(running)
    gate.open.set()

    assert client.wait(running) is Status.CANCELLED
    with pytest.raises(
        RecordFailed, match=f'^record {running.id} cancelled$'
    ) as raised:
        client.output(running, 'value')
    assert (raised.value.status, raised.value.reason) == (Status.CANCELLED, None)


def test_closing_waits_until_no_record_is_pending(
    backend: Backend, client: Client, gate: Gate
) -> None:
    a = client.submit(load(1))
    total = client.submit(ADD, params=operands(a.ref('value'), a.ref('value')))
    threading.Timer(0.05, gate.open.set).start()
    backend.close()

    assert client.status([a, total]) == [Status.COMPLETED, Status.COMPLETED]


def test_a_failed_append_stops_the_backend(files: FakeFiles, gate: Gate) -> None:
    log = FullDisk()
    backend = Backend(files, [function_binding(LOAD, gate.load)], log=log)
    client = Client(backend, proposal='p1', submitter='anna')
    record = client.submit(load(1))
    log.full = True
    threading.Timer(0.05, gate.open.set).start()  # once the wait below has started

    with pytest.raises(RuntimeError, match='no space left on device'):
        client.wait(record)  # its Finished event was not appended
    with pytest.raises(RuntimeError, match='no space left on device'):
        client.submit(load(2))
    with pytest.raises(RuntimeError, match='no space left on device'):
        backend.close()
