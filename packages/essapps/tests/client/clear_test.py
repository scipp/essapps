# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Clearing the outputs of records."""

import threading

import pytest

from ess.apps import Client, NotReadable, Status, SubmitError, Template
from ess.apps.testing import FakeStore

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

    with pytest.raises(ValueError, match=f'^record {load.id} is pending$'):
        client.clear(load)
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
        ValueError, match=f'^record {first.id} is read by pending record {reader.id}$'
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

    with pytest.raises(
        ValueError, match=f'^record {load.id} is read by an open stage or accumulator$'
    ):
        client.clear(load)
    client.release(stage)
    client.clear(load)
