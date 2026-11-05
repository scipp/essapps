# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The feed of changes: which records of a proposal changed since a position."""

import shutil
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from ess.apps import Backend, Changes, Client
from ess.apps.log import Log
from ess.apps.testing import FakeFiles, FakeStore
from ess.spec import Binding

from . import toys


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


def test_a_record_is_reported_in_the_order_of_its_first_change(
    stored: Client, loading: threading.Event
) -> None:
    loading.clear()
    first = stored.submit(toys.load_request(1))
    second = stored.submit(toys.load_request(2))
    loading.set()
    stored.wait([first, second])
    stored.persist(first, 'value')  # a later change of the first

    assert stored.changes().records == (first.id, second.id)


def test_a_persist_request_reports_the_record_and_so_does_its_write(
    stored: Client,
) -> None:
    load = stored.compute(toys.load_request(1))
    done = stored.changes()

    stored.persist(load, 'value')
    requested = stored.changes(after=done.cursor)
    assert requested.records == (load.id,)

    until(lambda: stored.changes(after=requested.cursor).records == (load.id,))


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
    stored.persist(load, 'value')
    other = stored.compute(toys.load_request(2))
    until(lambda: stored.changes().cursor == 6)
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
