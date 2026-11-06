# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The feed of changes: which records of a proposal changed since a position."""

import shutil
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from ess.apps import Backend, Changes, Client, ClientEnded, RecordStatus, Status
from ess.apps.log import Log
from ess.apps.testing import FakeFiles, FakeStore
from ess.spec import Binding

from . import toys


class CountingFeed(Backend):
    """Logs how many record IDs each read of the feed returned."""

    def __init__(self, files: FakeFiles, bindings: list[Binding]) -> None:
        super().__init__(files, bindings)
        self.read: list[int] = []

    def changes(self, after: int, client: str, *, timeout: float = 0.0) -> Changes:
        changes = super().changes(after, client, timeout=timeout)
        self.read.append(len(changes.records))
        return changes


def until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert condition()


@pytest.fixture
def stored(
    files: FakeFiles, bindings: list[Binding], tmp_path: Path
) -> Iterator[Client]:
    """A client of a backend with a store and a log file."""
    backend = Backend(files, bindings, store=FakeStore(), log=Log(tmp_path / 'log'))
    yield Client(backend, proposal='p1', submitter='anna')
    backend.close()


def test_after_zero_gives_every_record_and_a_later_call_only_what_changed(
    client: Client,
) -> None:
    first, second = client.compute([toys.load_request(1), toys.load_request(2)])

    everything = client.changes(after=0)
    assert everything.records == (first.id, second.id)

    third = client.compute(toys.load_request(1))
    assert client.changes(after=everything.cursor).records == (third.id,)
    assert client.changes().records == (first.id, second.id, third.id)


def test_without_a_change_the_reply_has_no_records_and_the_same_cursor(
    client: Client,
) -> None:
    client.compute(toys.load_request(1))
    changes = client.changes()

    assert client.changes(after=changes.cursor) == Changes((), changes.cursor)


def test_a_finish_reports_the_record_again(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    record = client.submit(toys.load_request(1))
    submitted = client.changes()
    assert submitted.records == (record.id,)

    loading.set()
    client.wait(record)

    assert client.changes(after=submitted.cursor).records == (record.id,)


def test_an_id_that_changed_twice_is_in_one_reply_once(client: Client) -> None:
    record = client.compute(toys.load_request(1))  # submitted, then finished

    changes = client.changes()

    assert changes.records == (record.id,)
    assert changes.cursor == 2


def test_a_record_changes_when_it_is_submitted_and_when_it_finishes(
    stored: Client,
) -> None:
    load = stored.compute(toys.load_request(1), persist=True)

    assert stored.changes(after=0) == Changes((load.id,), 2)
    assert stored.changes(after=1) == Changes((load.id,), 2)


def test_opening_an_accumulator_and_a_push_report_nothing_of_their_own(
    client: Client,
) -> None:
    load = client.compute(toys.load_request(1))
    done = client.changes()

    total = toys.total_accumulator(client)
    total.push(parts=[load.ref('value')])

    assert client.changes(after=done.cursor) == Changes((), done.cursor)


def test_reading_a_state_reports_its_state_record(client: Client) -> None:
    load = client.compute(toys.load_request(1))
    total = toys.total_accumulator(client)
    total.push(parts=[load.ref('value')])
    done = client.changes()

    read = client.compute(toys.read_request(total))

    state = toys.state_record(client, read)
    assert set(client.changes(after=done.cursor).records) == {state.id, read.id}


def test_another_client_of_the_proposal_sees_the_changes(
    backend: Backend, client: Client
) -> None:
    other = Client(backend, proposal='p1', submitter='bob')
    record = client.compute(toys.load_request(1))

    assert other.changes() == client.changes()
    assert other.changes().records == (record.id,)


def test_a_client_of_another_proposal_sees_none_and_its_cursor_is_unaffected(
    backend: Backend, client: Client, files: FakeFiles
) -> None:
    files.measure(4, 4.0, proposal='p2')
    theirs = Client(backend, proposal='p2', submitter='eve')
    client.compute(toys.load_request(1))

    assert theirs.changes() == Changes((), 0)

    ours = client.changes()
    elsewhere = theirs.compute(toys.load_request(4))

    assert client.changes() == ours
    assert theirs.changes().records == (elsewhere.id,)
    assert theirs.changes(after=0).cursor == 2


def test_a_backend_restarted_from_its_log_gives_the_same_feed(
    files: FakeFiles, bindings: list[Binding], stored: Client, tmp_path: Path
) -> None:
    load = stored.compute(toys.load_request(1))
    other = stored.compute(toys.load_request(2))
    assert stored.changes().cursor == 4
    copy = tmp_path / 'copy'
    shutil.copy(tmp_path / 'log', copy)

    backend = Backend(files, bindings, store=FakeStore(), log=Log(copy))
    try:
        again = Client(backend, proposal='p1', submitter='anna')
        assert again.changes().records == (load.id, other.id)
        for cursor in range(stored.changes().cursor + 1):
            assert again.changes(after=cursor) == stored.changes(after=cursor)
    finally:
        backend.close()


@pytest.mark.parametrize('after', [-1, 3])
def test_a_position_outside_the_feed_raises(client: Client, after: int) -> None:
    client.compute(toys.load_request(1))  # two changes

    with pytest.raises(ValueError, match='position'):
        client.changes(after=after)


def test_records_with_ids_are_in_the_order_given(client: Client) -> None:
    first, second = client.compute([toys.load_request(1), toys.load_request(2)])

    assert client.records(ids=[second.id, first.id]) == [second, first]
    assert client.records(ids=[]) == []


def test_records_with_an_unknown_id_raise(client: Client) -> None:
    with pytest.raises(KeyError, match='no record nope'):
        client.records(ids=['nope'])


def test_records_with_an_id_of_another_proposal_raise(
    backend: Backend, client: Client, files: FakeFiles
) -> None:
    files.measure(4, 4.0, proposal='p2')
    theirs = Client(backend, proposal='p2', submitter='eve')
    elsewhere = theirs.compute(toys.load_request(4))

    with pytest.raises(KeyError, match=elsewhere.id):
        client.records(ids=[elsewhere.id])


def test_records_with_a_label_and_ids_raise(client: Client) -> None:
    with pytest.raises(ValueError, match='label or ids'):
        client.records(label='scan', ids=[])


def test_changes_waits_for_a_change_up_to_its_timeout(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    load = client.submit(toys.load_request(1))
    cursor = client.changes().cursor

    with ThreadPoolExecutor() as pool:
        reply = pool.submit(client.changes, after=cursor, timeout=60)
        loading.set()
        assert reply.result(timeout=5) == Changes((load.id,), cursor + 1)


def test_changes_is_woken_by_a_submission_of_another_client(
    backend: Backend, client: Client
) -> None:
    other = Client(backend, proposal='p1', submitter='bob')
    cursor = client.changes().cursor

    with ThreadPoolExecutor() as pool:
        reply = pool.submit(client.changes, after=cursor, timeout=60)
        load = other.submit(toys.load_request(1))
        assert load.id in reply.result(timeout=5).records


def test_changes_gives_no_ids_once_its_timeout_has_passed(client: Client) -> None:
    client.compute(toys.load_request(1))
    cursor = client.changes().cursor

    assert client.changes(after=cursor, timeout=0.01) == Changes((), cursor)


def test_a_position_outside_the_feed_raises_without_waiting(client: Client) -> None:
    with pytest.raises(ValueError, match='position'):
        client.changes(after=1, timeout=60)


def test_a_client_that_ends_raises_in_changes_and_in_wait(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    load = client.submit(toys.load_request(1))
    cursor = client.changes().cursor

    with ThreadPoolExecutor() as pool:
        changes = pool.submit(client.changes, after=cursor, timeout=60)
        wait = pool.submit(client.wait, load)
        client.close()
        with pytest.raises(ClientEnded):
            changes.result(timeout=5)
        with pytest.raises(ClientEnded):
            wait.result(timeout=5)
    loading.set()


def test_a_read_waiting_when_its_client_ends_raises(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    load = client.submit(toys.load_request(1))

    with ThreadPoolExecutor() as pool:
        read = pool.submit(client.output, load, 'value')
        time.sleep(0.05)  # so that the read is waiting
        client.close()
        with pytest.raises(ClientEnded):
            read.result(timeout=5)
    loading.set()


def test_a_read_of_an_accumulator_waiting_when_its_client_ends_raises(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    total = toys.total_accumulator(client)
    total.push(parts=[client.submit(toys.load_request(1)).ref('value')])

    with ThreadPoolExecutor() as pool:
        read = pool.submit(client.output, total, 'value')
        time.sleep(0.05)  # so that the read is waiting
        client.close()
        with pytest.raises(ClientEnded):
            read.result(timeout=5)
    loading.set()


def test_waits_read_each_change_of_the_feed_once(
    files: FakeFiles, bindings: list[Binding], loading: threading.Event
) -> None:
    backend = CountingFeed(files, bindings)
    client = Client(backend, proposal='p1', submitter='anna')
    runs = 20

    for n in range(runs):
        loading.clear()
        threading.Timer(0.001, loading.set).start()
        client.compute(toys.load_request(1 + n % 2))
    backend.close()

    assert backend.read  # the waits did wait
    assert sum(backend.read) <= 2 * runs  # a submission and a finish of each


def test_a_client_that_ends_with_nothing_pending_raises_in_changes(
    client: Client,
) -> None:
    client.compute(toys.load_request(1))
    cursor = client.changes().cursor

    with ThreadPoolExecutor() as pool:
        changes = pool.submit(client.changes, after=cursor, timeout=60)
        time.sleep(0.05)  # so that it is waiting: closing logs nothing to wake it
        client.close()
        with pytest.raises(ClientEnded):
            changes.result(timeout=5)


class FinishesAfterStatus:
    """
    Delegates to a backend. The first status it answers lets the record finish
    before it returns, so the finish falls between a client's reading of the status
    and its next call.
    """

    def __init__(self, backend: Backend, loading: threading.Event) -> None:
        self._backend = backend
        self._loading = loading

    def __getattr__(self, name: str) -> Any:
        return getattr(self._backend, name)

    def status(self, ids: Sequence[str], client: str) -> list[RecordStatus]:
        statuses = self._backend.status(ids, client)
        if not self._loading.is_set():
            self._loading.set()
            until(
                lambda: (
                    self._backend.status(ids, client)[0].status is not Status.PENDING
                )
            )
        return statuses


def test_wait_sees_a_finish_right_after_it_read_the_status(
    backend: Backend, loading: threading.Event
) -> None:
    gated = FinishesAfterStatus(backend, loading)
    client = Client(gated, proposal='p1', submitter='anna')
    loading.clear()
    load = client.submit(toys.load_request(1))

    with ThreadPoolExecutor() as pool:
        # The record finishes right after the status was read, so the wait must
        # not sleep through its _WAIT.
        assert pool.submit(client.wait, load).result(timeout=5) is Status.COMPLETED


def test_a_record_stays_in_the_order_of_its_first_change_when_it_changes_last(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    first = client.submit(toys.load_request(1))
    second = client.submit(toys.load_request(2))
    client.cancel(second)  # second finishes first
    loading.set()
    client.wait(first)

    assert client.changes().records == (first.id, second.id)
