# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)


"""Accumulator.push, and the order of pushes against reads."""

import threading

import pytest

from ess.apps import Client, Record, Request, Status, SubmitError, Template, file
from ess.apps.testing import FakeFiles

from .conftest import (
    BOTH,
    DIGITS,
    LOAD,
    SIDES,
    SUM,
    TOTAL,
    WEIGHTED,
    Amount,
    Copying,
    Subtracting,
    WeightedPart,
    _array_total,
    _copy,
    _listed,
    _load,
    _read,
    _sides,
    _sides_of,
    _start,
    _state,
    _total,
)


def test_a_reference_in_a_push_must_fit_its_row(client: Client) -> None:
    load = client.submit(_load(1))
    weighted = client.accumulator(Template(WEIGHTED, blanks=('parts',)))

    with pytest.raises(
        SubmitError, match=r'^parts\[0\]: .*, of type Amount, does not fit'
    ):
        weighted.push(parts=[load.ref('value')])


def test_a_push_takes_rows_as_a_request_does(client: Client) -> None:
    rows = [WeightedPart(value=1.0, weight=3.0), {'value': 2.0, 'weight': 1.0}]
    weighted = client.accumulator(Template(WEIGHTED, blanks=('parts',)))
    for row in rows:
        weighted.push(parts=[row])
    read = client.compute(_read(weighted))
    plain = client.compute(WEIGHTED, params={'parts': rows})

    assert (
        client.output(read, 'value')
        == client.output(plain, 'value')
        == Amount(value=5.0)
    )
    assert _state(client, read).request == plain.request


def test_a_push_is_refused_as_the_request_over_it_alone(client: Client) -> None:
    released = client.compute(_load(1))
    client.release(released)
    total = _total(client)
    summed = client.accumulator(Template(SUM, blanks=('runs',)))
    pushes = [
        (total, TOTAL, {'parts': [{'value': 'x'}]}),  # request_types refuses it
        (total, TOTAL, {'parts': [released.ref('value')]}),  # a record not kept
        (summed, SUM, {'runs': [file(run=9)]}),  # an unknown file
    ]
    for accumulator, spec, rows in pushes:
        with pytest.raises(SubmitError) as refused:
            client.submit(spec, params=rows)
        with pytest.raises(SubmitError) as pushed:
            accumulator.push(**rows)
        assert str(pushed.value) == str(refused.value)


def test_a_push_takes_lists_of_rows(client: Client) -> None:
    load = client.compute(_load(1))
    total = _total(client)
    plain = {'@ref': 'output', 'record': load.id, 'output': 'value'}
    for rows in (load.ref('value'), plain, 'x'):
        with pytest.raises(TypeError, match='parts: the rows of a table are a list'):
            total.push(parts=rows)  # type: ignore[arg-type]


def test_a_push_into_two_tables_gives_one_state(
    in_place: Client, loads: list[Record]
) -> None:
    client = in_place
    both = client.accumulator(Template(BOTH, blanks=('samples', 'backgrounds')))
    tables = {
        'samples': [loads[0].ref('value')],
        'backgrounds': [loads[1].ref('value')],
    }
    both.push(**tables)
    plain = client.compute(BOTH, params=tables)

    assert _sides(client.output(both)) == {'difference': [-1.0], 'total': [3.0]}
    assert _sides(client.output(plain)) == _sides(client.output(both))
    assert client.provenance(both).request == plain.request
    with pytest.raises(
        SubmitError, match=r'backgrounds\[1\]: repeats backgrounds\[0\]'
    ):
        both.push(samples=[loads[1].ref('value')], backgrounds=tables['backgrounds'])
    assert _listed(client.output(both, 'total')) == [3.0]  # a refused push adds no row


def test_a_push_takes_a_pending_record_and_refuses_one_that_did_not_complete(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    pending, cancelled = (client.submit(_load(n)) for n in (1, 2))
    client.cancel(cancelled)
    total = _total(client)
    with pytest.raises(SubmitError, match=f'{cancelled.id} cancelled'):
        total.push(parts=[cancelled.ref('value')])
    total.push(parts=[pending.ref('value')])  # added once pending completes
    read = client.submit(_read(total))
    loading.set()

    assert client.output(read, 'value') == Amount(value=1.0)


def test_concurrent_pushes_add_in_the_order_they_are_logged(
    client: Client, files: FakeFiles
) -> None:
    runs = [files.measure(n, float(n)) for n in range(3, 10)]
    loads = client.compute([Request(LOAD, params={'sample': run}) for run in runs])
    digits = client.accumulator(Template(DIGITS, blanks=('parts',)))
    pushes = [
        threading.Thread(target=lambda x=x: digits.push(parts=[x.ref('value')]))
        for x in loads
    ]
    for push in pushes:
        push.start()
    for push in pushes:
        push.join()
    read = client.compute(_read(digits))
    pushed = _state(client, read).request.params  # in the order they were logged
    plain = client.compute(DIGITS, params=pushed)

    assert client.output(read, 'value') == client.output(plain, 'value')


def test_a_push_is_added_after_a_request_that_has_not_started(
    client: Client, loading: threading.Event
) -> None:
    loads = client.compute([_load(n) for n in (1, 2)])
    total = _total(client)
    total.push(parts=[loads[0].ref('value')])
    loading.clear()
    pending = client.submit(_load(2))
    parts = [total.ref('value'), pending.ref('value')]
    summed = client.submit(TOTAL, params={'parts': parts})  # once pending completes
    total.push(parts=[loads[1].ref('value')])  # returns at once
    read = _start(lambda: client.output(total, 'value'))

    with pytest.raises(TimeoutError):
        read.result(timeout=0.05)  # the push waits for summed
    loading.set()
    assert read.result(timeout=5) == Amount(value=3.0)
    assert client.output(summed, 'value') == Amount(
        value=3.0
    )  # read 1.0 before the push


def test_a_push_is_added_once_the_requests_that_read_the_state_before_it_have_run(
    in_place: Client, loads: list[Record], copying: Copying
) -> None:
    client = in_place
    copying.go.clear()
    total = _array_total(client)
    total.push(parts=[loads[0].ref('value')])
    copied = _copy(client, total)
    copying.started.wait(timeout=5)  # the copy runs, and reads the value
    total.push(parts=[loads[1].ref('value')])  # returns at once
    read = _start(lambda: client.output(total, 'value'))

    with pytest.raises(TimeoutError):
        read.result(timeout=0.05)
    copying.go.set()
    assert _listed(read.result(timeout=5)) == [3.0]
    assert _listed(client.output(copied, 'value')) == [1.0]


def test_cancelling_a_request_that_has_not_started_lets_the_push_be_added(
    client: Client, loading: threading.Event
) -> None:
    loads = client.compute([_load(n) for n in (1, 2)])
    total = _total(client)
    total.push(parts=[loads[0].ref('value')])
    loading.clear()
    pending = client.submit(_load(2))
    parts = [total.ref('value'), pending.ref('value')]
    summed = client.submit(TOTAL, params={'parts': parts})
    total.push(parts=[loads[1].ref('value')])
    read = _start(lambda: client.output(total, 'value'))

    with pytest.raises(TimeoutError):
        read.result(timeout=0.05)
    client.cancel(summed)
    assert read.result(timeout=5) == Amount(value=3.0)
    assert client.status([pending, summed]) == [Status.PENDING, Status.CANCELLED]
    loading.set()


def test_a_push_is_added_once_a_cancelled_request_that_still_runs_returns(
    in_place: Client, loads: list[Record], copying: Copying
) -> None:
    client = in_place
    copying.go.clear()
    total = _array_total(client)
    total.push(parts=[loads[0].ref('value')])
    copied = _copy(client, total)
    copying.started.wait(timeout=5)
    client.cancel(copied)
    total.push(parts=[loads[1].ref('value')])
    read = _start(lambda: client.output(total, 'value'))

    with pytest.raises(TimeoutError):
        read.result(timeout=0.05)
    copying.go.set()
    assert _listed(read.result(timeout=5)) == [3.0]
    assert client.status(copied) is Status.CANCELLED


def test_a_push_is_added_once_a_read_of_the_outputs_in_progress_is_done(
    in_place: Client, loads: list[Record], subtracting: Subtracting
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push(samples=[loads[0].ref('value')])
    subtracting.compute.clear()
    read = _start(lambda: client.output(sides, 'difference'))
    subtracting.computing.wait(timeout=5)  # the read computes the output
    subtracting.adding.clear()
    sides.push(backgrounds=[loads[1].ref('value')])  # returns at once

    assert not subtracting.adding.wait(timeout=0.05)
    subtracting.compute.set()
    assert _listed(read.result(timeout=5)) == [1.0]
    assert _listed(client.output(sides, 'difference')) == [-1.0]


def test_a_push_proceeds_after_readers_whose_outputs_failed(
    in_place: Client, loads: list[Record], subtracting: Subtracting
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push(samples=[loads[0].ref('value')])
    subtracting.failing = True
    copied = _copy(client, sides, 'difference')
    client.wait(copied)
    subtracting.failing = True
    with pytest.raises(ValueError, match=r'^cannot compute$'):
        client.output(sides, 'difference')

    _start(lambda: sides.push(backgrounds=[loads[1].ref('value')])).result(5)
    state = _state(client, copied)
    assert client.failure([state, copied]) == [
        'ValueError: cannot compute',
        f'record {state.id} failed',
    ]
    assert _listed(client.output(sides, 'difference')) == [-1.0]


def test_a_push_of_several_rows_gives_one_state(
    in_place: Client, loads: list[Record]
) -> None:
    client = in_place
    sides = _sides_of(client)
    rows = [x.ref('value') for x in loads]
    sides.push(samples=rows)
    copied = _copy(client, sides, 'difference')
    plain = client.compute(SIDES, params={'samples': rows, 'backgrounds': []})

    assert _state(client, copied).request == plain.request
