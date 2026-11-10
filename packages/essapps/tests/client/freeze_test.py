# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)


"""Freezing an accumulator."""

import gc
import time
from collections.abc import Callable

import pytest

from ess.apps import Client, NotReadable, Record, Status, SubmitError, Template

from . import toys


def _eventually(condition: Callable[[], bool]) -> bool:
    """Whether ``condition`` holds within five seconds, as other threads go on."""
    deadline = time.monotonic() + 5
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


def test_a_freeze_is_the_record_of_the_last_state_computed_once(
    in_place: Client, loads: list[Record], subtracting: toys.Subtracting
) -> None:
    client = in_place
    sides = toys.sides_accumulator(client)
    sides.push(samples=[loads[0].ref('value')])
    sides.push(backgrounds=[loads[1].ref('value')])
    looked = client.output(sides, 'difference')
    plain = client.provenance(sides).request
    total = client.freeze(sides)

    assert total.request == plain
    assert (
        toys.listed(client.output(total, 'difference')) == toys.listed(looked) == [-1.0]
    )
    assert subtracting.computed == 1  # the look and the freeze read one state
    assert client.output(total, 'difference') is subtracting.returned[0]()  # no copy
    assert total in client.records()


def test_a_push_after_a_freeze_is_refused_while_its_record_is_pending(
    in_place: Client, loads: list[Record], subtracting: toys.Subtracting
) -> None:
    client = in_place
    sides = toys.sides_accumulator(client)
    sides.push(samples=[loads[0].ref('value')])
    subtracting.compute.clear()
    total = client.freeze(sides)

    with pytest.raises(SubmitError, match=f'frozen as record {total.id}'):
        sides.push(samples=[loads[1].ref('value')])
    with pytest.raises(SubmitError, match=f'frozen as record {total.id}'):
        client.freeze(sides)
    subtracting.compute.set()


def test_a_frozen_accumulator_is_read_through_its_record(
    in_place: Client, loads: list[Record]
) -> None:
    client = in_place
    sides = toys.sides_accumulator(client)
    sides.push(samples=[loads[0].ref('value')])
    total = client.freeze(sides)
    client.wait(total)

    with pytest.raises(NotReadable, match=f'read record {total.id} instead'):
        client.output(sides)
    with pytest.raises(NotReadable, match=f'read record {total.id} instead'):
        client.provenance(sides)
    copied = client.compute(toys.COPY, params={'source': total.ref('difference')})
    assert toys.listed(client.output(copied, 'value')) == [1.0]


def test_templates_and_pushes_reference_a_frozen_record(
    in_place: Client, loads: list[Record]
) -> None:
    client = in_place
    first = toys.array_total_accumulator(client)
    first.push(parts=[x.ref('value') for x in loads])
    frozen = client.freeze(first)
    second = toys.array_total_accumulator(client)
    second.push(parts=[frozen.ref('value')])
    second.push(parts=[loads[0].ref('value')])
    staged = client.stage(Template(toys.COPY, params={'source': frozen.ref('value')}))

    assert toys.listed(client.output(second, 'value')) == [4.0]
    assert toys.listed(client.output(client.compute(staged), 'value')) == [3.0]


def test_a_freeze_whose_plain_request_would_be_refused_is_refused(
    in_place: Client, loads: list[Record]
) -> None:
    client = in_place
    both = client.accumulator(Template(toys.BOTH, blanks=('samples', 'backgrounds')))
    both.push(samples=[loads[0].ref('value')])

    with pytest.raises(SubmitError) as requested:
        client.submit(
            toys.BOTH, params={'samples': [loads[0].ref('value')], 'backgrounds': []}
        )
    with pytest.raises(SubmitError) as frozen:
        client.freeze(both)
    assert str(frozen.value) == str(requested.value)
    both.push(backgrounds=[loads[1].ref('value')])  # takes pushes again
    total = client.freeze(both)
    assert toys.listed(client.output(total, 'difference')) == [-1.0]


def test_a_cancelled_freeze_leaves_the_accumulator_readable_and_shut(
    in_place: Client, loads: list[Record], subtracting: toys.Subtracting
) -> None:
    client = in_place
    sides = toys.sides_accumulator(client)
    sides.push(samples=[loads[0].ref('value')])
    subtracting.compute.clear()
    cancelled = client.freeze(sides)
    subtracting.computing.wait(timeout=5)  # it runs, and holds the state
    client.release(cancelled)  # nothing keeps it any more
    subtracting.compute.set()

    assert client.wait(cancelled) == Status.CANCELLED
    # no push comes to drop the outputs computed for the cancelled freeze
    assert _eventually(
        lambda: gc.collect() >= 0 and [r() for r in subtracting.returned] == [None]
    )
    with pytest.raises(SubmitError, match=f'frozen as record {cancelled.id}'):
        sides.push(backgrounds=[loads[1].ref('value')])
    assert toys.listed(client.output(sides, 'total')) == [1.0]
    again = client.freeze(sides)
    assert toys.listed(client.output(again, 'total')) == [1.0]


def test_a_failed_freeze_leaves_the_accumulator_readable_and_shut(
    in_place: Client, loads: list[Record], subtracting: toys.Subtracting
) -> None:
    client = in_place
    sides = toys.sides_accumulator(client)
    sides.push(samples=[loads[0].ref('value')])
    subtracting.failing = True
    failed = client.freeze(sides)

    assert client.wait(failed) == Status.FAILED
    with pytest.raises(SubmitError, match=f'frozen as record {failed.id}'):
        sides.push(backgrounds=[loads[1].ref('value')])
    assert toys.listed(client.output(sides, 'total')) == [1.0]
    again = client.freeze(sides)
    assert toys.listed(client.output(again, 'total')) == [1.0]


def test_a_freeze_cancelled_while_it_waits_for_its_state_lets_go_of_it(
    in_place: Client, loads: list[Record], subtracting: toys.Subtracting
) -> None:
    client = in_place
    sides = toys.sides_accumulator(client)
    sides.push(samples=[loads[0].ref('value')])
    subtracting.go.clear()
    sides.push(backgrounds=[loads[1].ref('value')])  # adds once go is set
    cancelled = client.freeze(sides)  # waits for that push
    copied = client.submit(toys.COPY, params={'source': cancelled.ref('difference')})
    client.cancel(cancelled)
    subtracting.go.set()

    assert client.failure(copied) == f'record {cancelled.id} cancelled'
    assert toys.listed(client.output(sides, 'difference')) == [-1.0]
    with pytest.raises(SubmitError, match=f'frozen as record {cancelled.id}'):
        sides.push(samples=[loads[1].ref('value')])
    again = client.freeze(sides)
    assert toys.listed(client.output(again, 'difference')) == [-1.0]


def test_a_frozen_driver_is_dropped_once_its_readers_are_done(
    in_place: Client, loads: list[Record], subtracting: toys.Subtracting
) -> None:
    client = in_place
    sides = toys.sides_accumulator(client)
    sides.push(samples=[loads[0].ref('value')])
    subtracting.compute.clear()
    read = toys.start_in_thread(lambda: client.output(sides, 'difference'))
    subtracting.computing.wait(timeout=5)  # the read holds the state
    total = client.freeze(sides)  # pins the same state
    subtracting.compute.set()

    assert toys.listed(read.result(timeout=5)) == [1.0]
    assert client.wait(total) is Status.COMPLETED
    gc.collect()
    assert subtracting.held[0]() is None
    assert toys.listed(client.output(total, 'difference')) == [1.0]
