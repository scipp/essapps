# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Clearing the outputs of records."""

import threading

import pytest

from ess.apps import (
    Client,
    NotCleared,
    NotReadable,
    Status,
    Stopped,
    SubmitError,
    Template,
    Workspace,
    local,
)
from ess.apps.log import Cleared, Event, Log
from ess.apps.testing import FakeFiles, FakeStore
from ess.spec import function_binding

from . import toys


def test_a_cleared_record_keeps_its_request_status_and_provenance(
    summed: Client,
) -> None:
    load = summed.compute(toys.load_request(1), persist=True)
    shift = summed.compute(toys.read_request(load), persist=True)
    provenance = summed.provenance(shift)
    software = summed.software(shift)

    summed.clear([load, shift])

    assert summed.cleared([load, shift]) == [True, True]
    assert summed.status([load, shift]) == [Status.COMPLETED, Status.COMPLETED]
    assert summed.records(ids=[shift.id]) == [shift]
    assert summed.provenance(shift) == provenance
    assert summed.software(shift) == software


def test_a_read_of_a_cleared_output_is_refused_and_the_store_holds_none(
    summed: Client, store: FakeStore
) -> None:
    load = summed.compute(toys.load_request(1), persist=True)

    summed.clear(load)

    assert (load.id, 'value') not in store
    assert summed.readable(load) == ()
    cleared = f'^record {load.id}: its outputs are cleared$'
    with pytest.raises(NotReadable, match=cleared):
        summed.output(load, 'value')
    with pytest.raises(NotReadable, match=cleared):
        summed.output(load)


def test_a_cleared_record_held_in_memory_cannot_be_read(client: Client) -> None:
    load = client.compute(toys.load_request(1))

    client.clear(load)

    with pytest.raises(NotReadable, match=f'^record {load.id}: its outputs are'):
        client.output(load, 'value')


def test_a_request_that_references_a_cleared_output_is_refused(
    summed: Client,
) -> None:
    load = summed.compute(toys.load_request(1), persist=True)
    summed.clear(load)

    with pytest.raises(
        SubmitError, match=rf'^source: record {load.id}: its outputs are cleared$'
    ):
        summed.submit(toys.read_request(load))


def test_a_pending_record_is_not_cleared(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    load = client.submit(toys.load_request(1))

    with pytest.raises(NotCleared) as refused:
        client.clear(load)
    assert (refused.value.record, refused.value.held_by) == (load.id, 'pending')
    loading.set()
    client.wait(load)
    assert client.cleared(load) is False


def test_nothing_is_cleared_while_a_pending_record_reads_one_of_the_records(
    client: Client, loading: threading.Event
) -> None:
    first, second = client.compute([toys.load_request(1), toys.load_request(2)])
    loading.clear()
    client.submit(toys.load_request(1))  # holds the only worker
    reader = client.submit(toys.read_request(first))

    with pytest.raises(
        NotCleared, match=f'^record {first.id} is read by record {reader.id}$'
    ):
        client.clear([second, first])
    assert client.cleared([first, second]) == [False, False]
    loading.set()
    client.wait(reader)
    client.clear([first, second])
    assert client.cleared([first, second]) == [True, True]


def test_clearing_a_cleared_record_again_deletes_what_the_store_still_holds(
    summed: Client, store: FakeStore
) -> None:
    load = summed.compute(toys.load_request(1), persist=True)
    summed.clear(load)
    store.write(load.id, 'value', 1.0)  # as a crash before the delete leaves it

    summed.clear(load)

    assert (load.id, 'value') not in store


def test_a_client_learns_of_a_clear_from_the_changes(summed: Client) -> None:
    load = summed.compute(toys.load_request(1), persist=True)
    cursor = summed.changes().cursor

    summed.clear(load)

    assert summed.changes(after=cursor).records == (load.id,)


def test_a_record_that_a_stage_reads_is_not_cleared_until_the_stage_is_released(
    client: Client,
) -> None:
    load = client.compute(toys.load_request(1))
    stage = client.stage(
        Template(toys.SHIFT, params={'source': load.ref('value')}, blanks=['shift'])
    )

    with pytest.raises(NotCleared) as refused:
        client.clear(load)
    assert refused.value.held_by == 'stage'
    assert refused.value.reader == stage.id
    client.release(stage)
    client.clear(load)


class FailingLog(Log):
    """A log whose append of a ``Cleared`` fails, as on a full disk."""

    def append(self, event: Event) -> Event:
        if isinstance(event, Cleared):
            raise OSError('no space left on device')
        return super().append(event)


def test_a_failed_append_of_a_clear_stops_the_workspace_and_keeps_the_files(
    files: FakeFiles, store: FakeStore
) -> None:
    workspace = Workspace(
        proposal='p1',
        files=files,
        bind=[function_binding(toys.LOAD, toys.load_run)],
        log=FailingLog(),
        store=store,
    )
    client = Client(workspace, submitter='anna')
    load = client.compute(toys.load_request(1), persist=True)

    with pytest.raises(OSError, match='no space left'):
        client.clear(load)
    assert (load.id, 'value') in store
    with pytest.raises(Stopped):
        client.status(load)
    with pytest.raises(Stopped):
        workspace.close()


class FailingDelete(FakeStore):
    """A fake store whose first delete fails."""

    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    def delete(self, record: str) -> None:
        if not self.failed:
            self.failed = True
            raise OSError('device busy')
        super().delete(record)


def test_a_failed_delete_leaves_the_record_cleared_and_a_second_clear_deletes(
    files: FakeFiles,
) -> None:
    store = FailingDelete()
    with local(
        proposal='p1',
        files=files,
        bind=[function_binding(toys.LOAD, toys.load_run)],
        store=store,
    ) as client:
        load = client.compute(toys.load_request(1), persist=True)

        with pytest.raises(OSError, match='device busy'):
            client.clear(load)
        assert client.cleared(load) is True
        assert (load.id, 'value') in store
        with pytest.raises(NotReadable, match='its outputs are cleared'):
            client.output(load, 'value')
        client.clear(load)
        assert (load.id, 'value') not in store
