# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)


"""What a client keeps and reads, and closing and ending a client."""

import copy
import threading

import pytest

from ess.apps import (
    Accumulator,
    Backend,
    Client,
    ClientEnded,
    NotReadable,
    Record,
    Selector,
    Status,
    SubmitError,
    Template,
    local,
)
from ess.apps.testing import FakeFiles, read_file
from ess.spec import function_binding

from . import toys


def test_a_released_value_is_not_kept_and_the_record_stays(client: Client) -> None:
    load = client.compute(toys.load_request(1))
    client.release(load)

    not_kept = f'record {load.id} is not kept by this client'
    with pytest.raises(NotReadable, match=not_kept):
        client.output(load, 'value')
    with pytest.raises(SubmitError, match=rf'^source: {not_kept}$'):
        client.submit(toys.read_request(load))
    assert client.records() == [load]
    assert client.status(load) is Status.COMPLETED


def test_a_pending_request_reads_a_value_released_after_its_submission(
    client: Client, loading: threading.Event
) -> None:
    first = client.compute(toys.load_request(1))
    loading.clear()
    second = client.submit(toys.load_request(2))
    total = client.submit(
        toys.TOTAL, params={'parts': [first.ref('value'), second.ref('value')]}
    )
    client.release(first)
    loading.set()

    assert client.output(total, 'value') == toys.Amount(value=3.0)


def test_a_released_pending_record_is_refused_and_still_runs_for_its_readers(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    load = client.submit(toys.load_request(1))
    shifted = client.submit(
        toys.SHIFT,
        params={'shift': toys.Shift(offset=1.0), 'source': load.ref('value')},
    )
    client.release(load)

    not_kept = rf'^source: record {load.id} is not kept by this client$'
    with pytest.raises(SubmitError, match=not_kept):  # however far it has run
        client.submit(toys.read_request(load))
    loading.set()
    assert client.output(shifted, 'value') == toys.Amount(value=2.0)
    assert client.status(load) is Status.COMPLETED


def test_cancelling_work_that_nothing_keeps_passes_along_a_chain_of_any_length(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    chain = [client.submit(toys.load_request(1))]
    for _ in range(1000):
        chain.append(client.submit(toys.read_request(chain[-1])))
    client.release(chain[:-1])
    assert set(client.status(chain)) == {Status.PENDING}  # the last reads them

    client.release(chain[-1])  # now nothing keeps any of them
    loading.set()
    assert set(client.status(chain)) == {Status.CANCELLED}


def test_a_released_pending_record_that_nothing_reads_is_cancelled(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    load = client.submit(toys.load_request(1))
    client.release(load)
    loading.set()

    assert client.status(load) is Status.CANCELLED
    assert client.failure(load) == 'nothing keeps its outputs'
    with pytest.raises(SubmitError, match=rf'^source: record {load.id} cancelled$'):
        client.submit(toys.read_request(load))


def test_closing_a_client_cancels_the_work_that_nothing_else_keeps(
    backend: Backend, client: Client, loading: threading.Event
) -> None:
    other = Client(backend, proposal='p1', submitter='bob')
    loading.clear()
    pending = client.submit(toys.load_request(2))
    shifted = client.submit(toys.read_request(pending))
    client.close()
    loading.set()

    assert other.status([pending, shifted]) == [Status.CANCELLED] * 2


def test_ending_a_client_cancels_the_reads_of_its_accumulator(
    summed_backend: Backend,
    summed: Client,
    summed_loads: list[Record],
    summing: toys.Sum,
) -> None:
    other = Client(summed_backend, proposal='p1', submitter='bob')
    summing.go.clear()
    total = toys.total_accumulator(summed)
    total.push(parts=[summed_loads[0].ref('value')])
    shifted = summed.submit(toys.read_request(total))
    state = toys.state_record(summed, shifted)
    summed.close()
    summing.go.set()

    assert other.status([state, shifted]) == [Status.CANCELLED] * 2


def test_a_client_reads_and_references_only_what_it_keeps(
    backend: Backend, client: Client, loading: threading.Event
) -> None:
    other = Client(backend, proposal='p1', submitter='bob')
    done = client.compute(toys.load_request(1))
    loading.clear()
    pending = client.submit(toys.load_request(2))

    for record in (done, pending):
        not_kept = f'record {record.id} is not kept by this client'
        with pytest.raises(NotReadable, match=not_kept):
            other.output(record, 'value')
        with pytest.raises(SubmitError, match=rf'^source: {not_kept}$'):
            other.submit(toys.read_request(record))
    loading.set()
    assert other.wait(pending) is Status.COMPLETED  # its status is the proposal's


def test_the_stages_and_accumulators_of_another_client_are_refused(
    backend: Backend, client: Client
) -> None:
    theirs = Client(backend, proposal='p1', submitter='bob')
    load = client.compute(toys.load_request(1))
    stage = toys.stage_shift(client, load)
    total = client.accumulator(Template(toys.TOTAL, blanks=('parts',)))
    total.push(parts=[load.ref('value')])

    with pytest.raises(SubmitError, match='the stage was released or is unknown'):
        toys.submit_shift(theirs, stage, 2.0)
    with pytest.raises(SubmitError, match='is not kept by this client'):
        theirs.submit(toys.read_request(total))
    with pytest.raises(NotReadable, match='is not kept by this client'):
        theirs.output(total)


def test_a_read_tells_what_is_unknown_from_what_cannot_be_read(
    client: Client,
) -> None:
    load = client.compute(toys.load_request(1))
    total = client.accumulator(Template(toys.TOTAL, blanks=('parts',)))
    both: list[Record | Accumulator] = [load, total]
    client.release(both)
    unknown_load = load.model_copy(update={'id': 'unknown'})
    unknown_total = copy.copy(total)
    unknown_total.id = 'unknown'

    for released in (
        lambda: client.output(load, 'value'),
        lambda: client.output(total),
    ):
        with pytest.raises(NotReadable, match='is not kept by this client'):
            released()
    with pytest.raises(SubmitError, match='is not kept by this client'):
        total.push(parts=[toys.Amount(value=1.0)])  # a push is refused, not read
    for unknown in (
        lambda: client.output(unknown_load, 'value'),
        lambda: client.output(unknown_total),
    ):
        with pytest.raises(KeyError, match=r"'no .* unknown in proposal p1'"):
            unknown()
    with pytest.raises(KeyError, match="load/v1 has no output 'nope'"):
        client.output(load, 'nope')


def test_a_client_releases_only_what_it_keeps(backend: Backend, client: Client) -> None:
    load = client.compute(toys.load_request(1))
    Client(backend, proposal='p1', submitter='bob').release(load)

    assert client.output(load, 'value') == toys.Amount(value=1.0)


def test_every_call_of_a_closed_client_raises_client_ended(client: Client) -> None:
    load = client.compute(toys.load_request(1))
    stage = toys.stage_shift(client, load)
    total = client.accumulator(Template(toys.TOTAL, blanks=('parts',)))
    total.push(parts=[load.ref('value')])
    client.close()

    calls = [
        lambda: client.submit(toys.load_request(1)),
        lambda: toys.submit_shift(client, stage, 1.0),
        lambda: client.submit(toys.read_request(total)),
        lambda: total.push(parts=[load.ref('value')]),
        lambda: client.status(load),
        lambda: client.output(load, 'value'),
        lambda: client.output(total),
        lambda: client.provenance(total),
        lambda: client.records(),
        lambda: client.readable(load),
        lambda: client.spec(load.spec),
        lambda: client.specs(),
        lambda: client.stage(Template(toys.SCALE, blanks=('speed',))),
        lambda: client.accumulator(Template(toys.TOTAL, blanks=('parts',))),
        lambda: client.release(load),
        lambda: client.files.list(Selector()),
    ]
    for call in calls:
        with pytest.raises(ClientEnded) as raised:
            call()
        assert not isinstance(raised.value, RuntimeError | LookupError)


def test_a_with_block_closes_the_client(backend: Backend) -> None:
    with Client(backend, proposal='p1', submitter='anna') as client:
        load = client.compute(toys.load_request(1))

    with pytest.raises(ClientEnded):
        client.output(load, 'value')
    client.close()  # closing again does nothing


def test_closing_a_local_client_closes_its_backend_once_its_workflows_return(
    files: FakeFiles, loading: threading.Event
) -> None:
    started = threading.Event()
    loaded = []

    def load(params: toys.Sample) -> toys.LoadOutputs:
        started.set()
        loading.wait(timeout=5)
        loaded.append(read_file(params.sample))
        return toys.load_run(params)

    loading.clear()
    with local(
        proposal='p1', files=files, bind=[function_binding(toys.LOAD, load)]
    ) as client:
        client.submit(toys.load_request(1))
        started.wait(timeout=5)
        threading.Timer(0.05, loading.set).start()

    assert loaded == [1.0]  # cancelled when the client closed, but it ran


def test_every_output_of_a_record_or_an_accumulator_is_read_without_a_name(
    client: Client,
) -> None:
    load = client.compute(toys.load_request(1))
    total = toys.total_accumulator(client)
    total.push(parts=[load.ref('value')])

    one = toys.Amount(value=1.0)
    assert client.output(load) == {'value': one, 'extra': toys.Amount(value=-1.0)}
    assert client.output(total) == {'value': one}
    assert load.refs() == {name: load.ref(name) for name in ('value', 'extra')}
    assert total.refs() == {'value': total.ref('value')}


def test_on_close_is_called_once_the_client_has_ended(backend: Backend) -> None:
    def on_close() -> None:
        with pytest.raises(ClientEnded):
            client.records()
        closed.append(True)

    closed: list[bool] = []
    client = Client(backend, proposal='p1', submitter='anna', on_close=on_close)

    client.close()

    assert closed == [True]


def test_closing_a_local_client_cancels_what_has_not_started_before_it_waits(
    files: FakeFiles, loading: threading.Event
) -> None:
    started = threading.Event()
    shifted = []

    def load(params: toys.Sample) -> toys.LoadOutputs:
        started.set()
        loading.wait(timeout=5)
        return toys.load_run(params)

    def shift(params: toys.ShiftParams) -> toys.ShiftOutputs:
        shifted.append(params)
        return toys.shift(params)

    loading.clear()
    bind = [function_binding(toys.LOAD, load), function_binding(toys.SHIFT, shift)]
    with local(proposal='p1', files=files, bind=bind) as client:
        first = client.submit(toys.load_request(1))
        client.submit(toys.SHIFT, params={'source': first.ref('value')})
        started.wait(timeout=5)
        threading.Timer(0.05, loading.set).start()

    assert shifted == []
