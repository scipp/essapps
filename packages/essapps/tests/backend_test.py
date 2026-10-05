# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Scheduling and checking in the backend, through the client."""

import threading
from collections.abc import Iterator
from itertools import islice
from typing import Any

import pytest
from ess.reduce.spec import Array, NexusFile, WorkflowSpec
from pydantic import BaseModel

from ess.apps import (
    Backend,
    Client,
    Record,
    Request,
    Selector,
    Status,
    SubmitError,
    dataset,
)
from ess.apps.testing import FakeDatasets


class RunParams(BaseModel):
    run: NexusFile


class ValueOutputs(BaseModel):
    value: Array()  # type: ignore[valid-type]


class AddParams(BaseModel):
    a: Array()  # type: ignore[valid-type]
    b: Array()  # type: ignore[valid-type]
    offset: float = 0.0


def _spec(name: str, params: type[BaseModel]) -> WorkflowSpec:
    return WorkflowSpec(
        name=name,
        version=1,
        title=name,
        description=name,
        params=params,
        outputs=ValueOutputs,
    )


LOAD = _spec('load', RunParams)
ADD = _spec('add', AddParams)
NOTHING = _spec('nothing', RunParams)  # its workflow returns no outputs


class Gate:
    """Holds every LOAD until opened, so that its dependents stay pending."""

    def __init__(self) -> None:
        self.open = threading.Event()

    def load(self, run: float) -> dict[str, Any]:
        self.open.wait(timeout=5)
        if run < 0:
            raise ValueError('negative run')
        return {'value': run}


def add(a: float, b: float, offset: float) -> dict[str, Any]:
    return {'value': a + b + offset}


@pytest.fixture
def gate() -> Gate:
    return Gate()


@pytest.fixture
def datasets() -> FakeDatasets:
    datasets = FakeDatasets(proposal='p1')
    for n, value in ((1, 1.0), (2, 2.0), (3, -1.0)):
        datasets.measure(n, value)
    return datasets


@pytest.fixture
def backend(datasets: FakeDatasets, gate: Gate) -> Iterator[Backend]:
    backend = Backend(datasets, {LOAD: gate.load, ADD: add, NOTHING: lambda run: {}})
    yield backend
    gate.open.set()
    backend.close()


@pytest.fixture
def client(backend: Backend) -> Client:
    return Client(backend, proposal='p1', submitter='anna')


def load(n: int) -> Request:
    return Request(LOAD, {'run': dataset(run=n)})


def test_a_request_waits_for_its_pending_inputs(client: Client, gate: Gate) -> None:
    a, b = client.submit([load(1), load(2)])
    total = client.submit(ADD, {'a': a.ref('value'), 'b': b.ref('value')})

    assert client.status(total) is Status.PENDING
    gate.open.set()
    assert client.output(total, 'value') == 3.0


def test_closing_waits_until_no_record_is_pending(
    backend: Backend, client: Client, gate: Gate
) -> None:
    a = client.submit(load(1))
    total = client.submit(ADD, {'a': a.ref('value'), 'b': a.ref('value')})  # waits
    threading.Timer(0.05, gate.open.set).start()
    backend.close()

    assert client.status([a, total]) == [Status.COMPLETED, Status.COMPLETED]


def test_a_failed_input_fails_everything_downstream(client: Client, gate: Gate) -> None:
    bad, good = client.submit([load(3), load(1)])
    first = client.submit(ADD, {'a': bad.ref('value'), 'b': good.ref('value')})
    second = client.submit(ADD, {'a': first.ref('value'), 'b': good.ref('value')})
    gate.open.set()

    assert client.wait([bad, good, first, second]) == [
        Status.FAILED,
        Status.COMPLETED,
        Status.FAILED,
        Status.FAILED,
    ]
    assert client.failure(bad) == 'negative run'
    assert client.failure(first) == f'input {bad.id} failed'
    assert client.failure(second) == f'input {first.id} failed'


def test_cancel_ends_what_has_not_started(client: Client, gate: Gate) -> None:
    a, b = client.submit([load(1), load(2)])
    total = client.submit(ADD, {'a': a.ref('value'), 'b': b.ref('value')})
    client.cancel(total)
    gate.open.set()

    assert client.wait([a, b, total]) == ['completed', 'completed', 'cancelled']


def test_one_refused_request_refuses_the_submission(client: Client) -> None:
    a = client.submit(load(1))
    typo = Request(ADD, {'a': a.ref('value'), 'b': a.ref('value'), 'offset': 'x'})

    with pytest.raises(SubmitError, match='offset'):
        client.submit([load(2), typo])
    assert client.records() == [a]


def test_a_value_that_cannot_be_stored_is_refused(datasets: FakeDatasets) -> None:
    class AnyParams(BaseModel):
        value: Any

    spec = _spec('anything', AnyParams)
    backend = Backend(datasets, {spec: lambda value: {}})
    client = Client(backend, proposal='p1', submitter='anna')
    try:
        with pytest.raises(SubmitError, match=r'^second: value: cannot be stored'):
            client.submit(
                {
                    'first': Request(spec, {'value': 1}),
                    'second': Request(spec, {'value': len}),
                }
            )
        assert client.records() == []
    finally:
        backend.close()


def test_a_reference_to_an_unknown_record_is_refused(client: Client) -> None:
    missing = {'record': 'missing', 'output': 'value'}

    with pytest.raises(SubmitError, match='unknown record missing'):
        client.submit(ADD, {'a': missing, 'b': missing})


def test_a_reference_to_a_missing_output_is_refused(client: Client, gate: Gate) -> None:
    gate.open.set()
    loaded = client.compute(load(1))

    with pytest.raises(SubmitError, match="no output 'total'"):
        client.submit(
            ADD,
            {'a': loaded.ref('value'), 'b': {'record': loaded.id, 'output': 'total'}},
        )


def test_a_reference_to_another_proposal_is_refused(
    backend: Backend, gate: Gate
) -> None:
    gate.open.set()
    theirs = Client(backend, proposal='p2', submitter='eve')
    mine = Client(backend, proposal='p1', submitter='anna')
    loaded = mine.compute(load(1))

    with pytest.raises(SubmitError, match='belongs to proposal p1'):
        theirs.submit(ADD, {'a': loaded.ref('value'), 'b': loaded.ref('value')})


def test_an_unknown_dataset_is_refused(client: Client) -> None:
    with pytest.raises(SubmitError, match='unknown dataset run:9'):
        client.submit(load(9))


def test_a_client_sees_only_the_datasets_of_its_proposal(
    backend: Backend, datasets: FakeDatasets
) -> None:
    mine = Client(backend, proposal='p1', submitter='anna')
    theirs = Client(backend, proposal='p2', submitter='eve')
    other = datasets.measure(4, 4.0, proposal='p2')
    later = datasets.measure(5, 5.0)

    assert mine.datasets.list(Selector()) == [
        datasets.resolve(dataset(run=n)) for n in (1, 2, 3, 5)
    ]
    assert theirs.datasets.list(Selector()) == [other]
    assert list(islice(mine.datasets.watch(Selector()), 4))[-1] == later
    assert theirs.datasets.metadata(other)['run'] == 4
    with pytest.raises(KeyError, match='in proposal p1'):
        mine.datasets.metadata(other)


def test_the_keys_of_a_dict_become_members(client: Client, gate: Gate) -> None:
    gate.open.set()
    records = client.compute({'250K': load(1), '260K': load(2)}, label='scan')

    assert {r.member: r for r in client.records(label='scan')} == records
    assert client.latest('scan', member='260K') == records['260K']


def test_latest_without_a_member_is_the_newest_record_of_any_member(
    client: Client, gate: Gate
) -> None:
    gate.open.set()
    cold = client.submit(load(1), label='scan', member='250K')
    warm = client.submit(load(2), label='scan', member='260K')

    assert client.latest('scan') == warm
    assert client.latest('scan', member='250K') == cold
    with pytest.raises(KeyError, match="'270K'"):
        client.latest('scan', member='270K')


def test_reading_an_output_of_a_failed_record_raises(
    client: Client, gate: Gate
) -> None:
    gate.open.set()
    failed = client.compute(load(3))

    with pytest.raises(RuntimeError, match='negative run'):
        client.output(failed, 'value')


def test_an_unknown_parameter_is_refused(client: Client) -> None:
    with pytest.raises(SubmitError, match="'scale'"):
        client.submit(LOAD, {'run': dataset(run=1), 'scale': 2.0})


def test_a_workflow_that_leaves_out_an_output_fails(client: Client) -> None:
    nothing = client.compute(NOTHING, {'run': dataset(run=1)})

    assert "missing ['value']" in client.failure(nothing)


def test_a_long_chain_fails_as_a_whole(client: Client, gate: Gate) -> None:
    chain = [client.submit(load(3))]
    for _ in range(1500):
        a, b = chain[-1].ref('value'), chain[0].ref('value')
        chain.append(client.submit(ADD, {'a': a, 'b': b}))
    gate.open.set()

    assert set(client.wait(chain)) == {Status.FAILED}


def test_the_same_request_twice_makes_two_records(client: Client, gate: Gate) -> None:
    gate.open.set()
    request = load(1)
    first, second = client.compute([request, request])

    assert first.id != second.id
    assert first.request == second.request
    assert client.records() == [first, second]


def test_a_reference_to_an_element_of_an_output_is_refused(client: Client) -> None:
    a = client.submit(load(1))
    element = {'record': a.id, 'output': 'value', 'key': '0'}

    with pytest.raises(SubmitError, match='element'):
        client.submit(ADD, {'a': element, 'b': a.ref('value')})


def test_cancel_drops_what_is_running(client: Client, gate: Gate) -> None:
    running = client.submit(load(1))
    client.cancel(running)
    gate.open.set()

    assert client.wait(running) is Status.CANCELLED
    with pytest.raises(RuntimeError, match='cancelled'):
        client.output(running, 'value')


def test_a_reference_to_a_failed_record_is_refused(client: Client, gate: Gate) -> None:
    gate.open.set()
    failed = client.compute(load(3))

    with pytest.raises(SubmitError, match=f'a: record {failed.id} failed'):
        client.submit(ADD, {'a': failed.ref('value'), 'b': failed.ref('value')})


def test_a_refusal_names_the_request_and_the_field(client: Client) -> None:
    with pytest.raises(SubmitError, match=r'^second: run: unknown dataset run:9$'):
        client.submit({'first': load(1), 'second': load(9)})


def test_as_completed_yields_records_in_the_order_they_finish(
    client: Client, gate: Gate
) -> None:
    first, second = client.submit([load(1), load(2)])
    finished = client.as_completed([first, second])
    client.cancel(second)
    cancelled = next(finished)
    gate.open.set()
    completed = next(finished)

    assert (cancelled.id, client.status(cancelled)) == (second.id, Status.CANCELLED)
    assert (completed.id, client.status(completed)) == (first.id, Status.COMPLETED)
    assert list(finished) == []


def test_as_completed_yields_failed_records_and_each_record_once(
    client: Client, gate: Gate
) -> None:
    gate.open.set()
    bad, good = client.submit([load(3), load(1)])
    finished = list(client.as_completed([bad, good, bad]))

    assert len(finished) == 2
    assert {r.id: client.status(r) for r in finished} == {
        bad.id: Status.FAILED,
        good.id: Status.COMPLETED,
    }


def test_as_completed_yields_while_its_input_blocks(client: Client, gate: Gate) -> None:
    gate.open.set()
    received = threading.Event()

    def arriving() -> Iterator[Record]:
        yield client.submit(load(1))
        if not received.wait(timeout=5):
            raise TimeoutError('nothing was yielded while the input blocked')
        yield client.submit(load(2))

    values = []
    for record in client.as_completed(arriving()):
        values.append(client.output(record, 'value'))
        received.set()

    assert values == [1.0, 2.0]


def test_an_error_in_the_input_of_as_completed_reaches_the_caller(
    client: Client, gate: Gate
) -> None:
    gate.open.set()

    def arriving() -> Iterator[Record]:
        yield client.submit(load(1))
        raise OSError('the catalogue is gone')

    with pytest.raises(OSError, match='the catalogue is gone'):
        list(client.as_completed(arriving()))


def test_closing_as_completed_stops_consuming_its_input(
    client: Client, gate: Gate
) -> None:
    gate.open.set()
    closed, released = threading.Event(), threading.Event()

    def arriving() -> Iterator[Record]:  # endless, as datasets.watch
        try:
            while True:
                yield client.submit(load(1))
                closed.wait(timeout=5)
        finally:
            released.set()

    finished = client.as_completed(arriving())
    next(finished)
    finished.close()
    closed.set()

    assert released.wait(timeout=5)  # the input was dropped, not exhausted
    assert len(client.records()) <= 2
