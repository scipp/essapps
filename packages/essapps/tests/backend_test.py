# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Scheduling and checking in the backend, through the client."""

import threading
from collections.abc import Iterator
from typing import Any

import pytest
from ess.reduce.spec import Array, NexusFile, WorkflowSpec
from pydantic import BaseModel

from ess.apps import Backend, Client, Request, Status, SubmitError, dataset
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
    backend = Backend(datasets, {LOAD: gate.load, ADD: add})
    yield backend
    gate.open.set()
    backend.close()


@pytest.fixture
def client(backend: Backend) -> Client:
    return Client(backend, proposal='p1', submitter='anna')


def load(n: int) -> Request:
    return Request(LOAD, {'run': dataset(run=n)})


def test_a_request_waits_for_its_pending_inputs(client: Client, gate: Gate) -> None:
    a, b = load(1), load(2)
    total = Request(ADD, {'a': a.ref('value'), 'b': b.ref('value')})
    records = client.submit({'a': a, 'b': b, 'total': total})

    assert records['total'].status is Status.PENDING
    gate.open.set()
    assert client.output(records['total'], 'value') == 3.0


def test_a_failed_input_fails_everything_downstream(client: Client, gate: Gate) -> None:
    bad, good = load(3), load(1)
    first = Request(ADD, {'a': bad.ref('value'), 'b': good.ref('value')})
    second = Request(ADD, {'a': first.ref('value'), 'b': good.ref('value')})
    records = client.submit([bad, good, first, second])
    gate.open.set()

    bad, good, first, second = client.wait(records)
    assert bad.failure.message == 'negative run'
    assert good.status is Status.COMPLETED
    assert (first.status, first.failure.message) == (
        Status.FAILED,
        f'input {bad.id} failed',
    )
    assert second.failure.message == f'input {first.id} failed'


def test_cancel_ends_what_has_not_started(client: Client, gate: Gate) -> None:
    a, b = load(1), load(2)
    total = Request(ADD, {'a': a.ref('value'), 'b': b.ref('value')})
    records = client.submit([a, b, total])
    client.cancel(records[2])
    gate.open.set()

    statuses = [r.status for r in client.wait(records)]
    assert statuses == [Status.COMPLETED, Status.COMPLETED, Status.CANCELLED]


def test_one_refused_request_refuses_the_submission(client: Client) -> None:
    a = load(1)
    typo = Request(ADD, {'a': a.ref('value'), 'b': a.ref('value'), 'offset': 'x'})

    with pytest.raises(SubmitError, match='offset'):
        client.submit([a, typo])
    assert client.records() == []


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


def test_the_keys_of_a_dict_become_members(client: Client, gate: Gate) -> None:
    gate.open.set()
    records = client.compute({'250K': load(1), '260K': load(2)}, label='scan')

    assert client.members('scan') == records
    assert client.latest('scan', member='260K') == records['260K']
    assert client.labels() == ['scan']


def test_reading_an_output_of_a_failed_record_raises(
    client: Client, gate: Gate
) -> None:
    gate.open.set()
    failed = client.compute(load(3))

    with pytest.raises(RuntimeError, match='negative run'):
        client.output(failed, 'value')
