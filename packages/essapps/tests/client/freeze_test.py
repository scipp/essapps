# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)


"""Freezing an accumulator."""

import gc
import time
from collections.abc import Callable

import pytest

from ess.apps import Client, NotReadable, Record, Status, SubmitError, Template

from .conftest import (
    BOTH,
    COPY,
    Copying,
    Subtracting,
    _array_total,
    _copy,
    _listed,
    _sides_of,
)


def _eventually(condition: Callable[[], bool]) -> bool:
    """Whether ``condition`` holds within five seconds, as other threads go on."""
    deadline = time.monotonic() + 5
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


def test_a_freeze_is_the_record_of_the_last_state_computed_once(
    in_place: Client, loads: list[Record], subtracting: Subtracting
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push(samples=[loads[0].ref('value')])
    sides.push(backgrounds=[loads[1].ref('value')])
    looked = client.output(sides, 'difference')
    plain = client.provenance(sides).request
    total = client.freeze(sides)

    assert total.request == plain
    assert _listed(client.output(total, 'difference')) == _listed(looked) == [-1.0]
    assert subtracting.computed == 1  # the look and the freeze read one state
    assert client.output(total, 'difference') is subtracting.returned[0]()  # no copy
    assert total in client.records()


def test_a_push_after_a_freeze_is_refused_while_its_record_is_pending(
    in_place: Client, loads: list[Record], subtracting: Subtracting
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push(samples=[loads[0].ref('value')])
    subtracting.compute.clear()
    total = client.freeze(sides)

    with pytest.raises(SubmitError, match=f'frozen as record {total.id}'):
        sides.push(samples=[loads[1].ref('value')])
    with pytest.raises(SubmitError, match=f'frozen as record {total.id}'):
        client.freeze(sides)
    subtracting.compute.set()
    assert _listed(client.output(total, 'total')) == [1.0]


def test_a_frozen_accumulator_is_read_through_its_record(
    in_place: Client, loads: list[Record]
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push(samples=[loads[0].ref('value')])
    total = client.freeze(sides)
    client.wait(total)

    with pytest.raises(NotReadable, match=f'read record {total.id} instead'):
        client.output(sides)
    with pytest.raises(SubmitError, match=f'read record {total.id} instead'):
        _copy(client, sides, 'difference')
    with pytest.raises(NotReadable, match=f'read record {total.id} instead'):
        client.provenance(sides)
    copied = client.compute(COPY, params={'source': total.ref('difference')})
    assert _listed(client.output(copied, 'value')) == [1.0]


def test_templates_and_pushes_reference_a_frozen_record(
    in_place: Client, loads: list[Record]
) -> None:
    client = in_place
    first = _array_total(client)
    first.push(parts=[x.ref('value') for x in loads])
    frozen = client.freeze(first)
    second = _array_total(client)
    second.push(parts=[frozen.ref('value')])
    second.push(parts=[loads[0].ref('value')])
    staged = client.stage(Template(COPY, params={'source': frozen.ref('value')}))

    assert _listed(client.output(second, 'value')) == [4.0]
    assert _listed(client.output(client.compute(staged), 'value')) == [3.0]


def test_a_freeze_whose_plain_request_would_be_refused_is_refused(
    in_place: Client, loads: list[Record]
) -> None:
    client = in_place
    both = client.accumulator(Template(BOTH, blanks=('samples', 'backgrounds')))
    both.push(samples=[loads[0].ref('value')])

    with pytest.raises(SubmitError) as requested:
        client.submit(
            BOTH, params={'samples': [loads[0].ref('value')], 'backgrounds': []}
        )
    with pytest.raises(SubmitError) as frozen:
        client.freeze(both)
    assert str(frozen.value) == str(requested.value)
    both.push(backgrounds=[loads[1].ref('value')])  # takes pushes again
    total = client.freeze(both)
    assert _listed(client.output(total, 'difference')) == [-1.0]


def test_a_cancelled_freeze_leaves_the_accumulator_readable_and_shut(
    in_place: Client, loads: list[Record], subtracting: Subtracting
) -> None:
    client = in_place
    sides = _sides_of(client)
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
    assert _listed(client.output(sides, 'total')) == [1.0]
    again = client.freeze(sides)
    assert _listed(client.output(again, 'total')) == [1.0]


def test_a_failed_freeze_leaves_the_accumulator_readable_and_shut(
    in_place: Client, loads: list[Record], subtracting: Subtracting
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push(samples=[loads[0].ref('value')])
    subtracting.failing = True
    failed = client.freeze(sides)

    assert client.wait(failed) == Status.FAILED
    with pytest.raises(SubmitError, match=f'frozen as record {failed.id}'):
        sides.push(backgrounds=[loads[1].ref('value')])
    assert _listed(client.output(sides, 'total')) == [1.0]
    again = client.freeze(sides)
    assert _listed(client.output(again, 'total')) == [1.0]


def test_a_frozen_driver_is_dropped_once_its_readers_are_done(
    in_place: Client,
    loads: list[Record],
    copying: Copying,
    subtracting: Subtracting,
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push(samples=[loads[0].ref('value')])
    copying.go.clear()
    copied = _copy(client, sides, 'difference')  # pins the same state
    copying.started.wait(timeout=5)
    total = client.freeze(sides)
    client.wait(total)

    assert subtracting.held[0]() is not None  # the copy still reads the state
    copying.go.set()
    client.wait(copied)
    client.release(copied)
    gc.collect()
    assert subtracting.held[0]() is None
    assert _listed(client.output(total, 'difference')) == [1.0]
