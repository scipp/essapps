# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Persisting outputs: what the store keeps, who reads it, and a restart."""

import operator
import shutil
import threading
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from ess.dispatch import (
    Backend,
    Client,
    Status,
    SubmitError,
    Template,
    dataset,
    local,
)
from ess.dispatch.log import Log, Persist
from ess.dispatch.testing import FakeDatasets, FakeStore
from ess.spec import Array, NexusFile, WorkflowSpec, combine


class RunParams(BaseModel):
    run: NexusFile


class Loaded(BaseModel):
    value: Array()  # type: ignore[valid-type]
    extra: Array()  # type: ignore[valid-type]
    note: Array() | None = None  # type: ignore[valid-type]


class Value(BaseModel):
    value: Array()  # type: ignore[valid-type]


class ShiftParams(BaseModel):
    value: Array()  # type: ignore[valid-type]


class Parts(BaseModel):
    parts: list[Value]


def _spec(name: str, params: type[BaseModel], outputs: type[BaseModel]) -> WorkflowSpec:
    return WorkflowSpec(
        name=name,
        version=1,
        title=name,
        description=name,
        params=params,
        outputs=outputs,
    )


LOAD = _spec('load', RunParams, Loaded)
SHIFT = _spec('shift', ShiftParams, Value)
TOTAL = _spec('total', Parts, Value)


class GatedStore(FakeStore):
    """A fake store whose writes wait until ``gate`` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = threading.Event()
        self.gate.set()

    def write(self, record: str, output: str, value: Any) -> None:
        self.gate.wait(timeout=5)
        super().write(record, output, value)


@pytest.fixture
def datasets() -> FakeDatasets:
    datasets = FakeDatasets(proposal='p1')
    for n in (1, 2):
        datasets.measure(n, float(n))
    return datasets


@pytest.fixture
def store() -> GatedStore:
    return GatedStore()


@pytest.fixture
def loading() -> Iterator[threading.Event]:
    """Set to let LOAD finish; set again at the end of a test."""
    event = threading.Event()
    event.set()
    yield event
    event.set()


@pytest.fixture
def bind(loading: threading.Event) -> dict[WorkflowSpec, Any]:
    def load(run: float) -> dict[str, float]:
        loading.wait(timeout=5)
        return {'value': run, 'extra': -run}

    return {
        LOAD: load,
        SHIFT: lambda value: {'value': value + 10.0},
        TOTAL: combine(operator.add),
    }


@pytest.fixture
def backend(
    datasets: FakeDatasets, store: GatedStore, bind: dict[WorkflowSpec, Any]
) -> Iterator[Backend]:
    backend = Backend(datasets, bind, store=store)
    yield backend
    store.gate.set()
    backend.close()


@pytest.fixture
def connect(backend: Backend) -> Callable[[], Client]:
    return lambda: Client(backend, proposal='p1', submitter='anna')


@pytest.fixture
def client(connect: Callable[[], Client]) -> Client:
    return connect()


def test_persisted_at_submission_the_store_keeps_the_outputs_named(
    client: Client, connect: Callable[[], Client], store: GatedStore
) -> None:
    load = client.compute(LOAD, {'run': dataset(run=1)}, persist=('value',))

    other = connect()
    assert other.output(load, 'value') == 1.0
    assert (load.id, 'value') in store
    assert (load.id, 'extra') not in store
    with pytest.raises(LookupError, match='not kept by this client'):
        client.output(load, 'extra')  # dropped once the record completed
    with pytest.raises(SubmitError, match='not kept by this client'):
        client.submit(SHIFT, {'value': load.ref('extra')})
    shift = other.compute(SHIFT, {'value': load.ref('value')})
    assert other.output(shift, 'value') == 11.0


def test_a_record_persisted_at_submission_completes_once_written(
    client: Client, store: GatedStore
) -> None:
    store.gate.clear()
    load = client.submit(LOAD, {'run': dataset(run=1)}, persist=True)

    assert client.status(load) is Status.PENDING
    store.gate.set()
    assert client.wait(load) is Status.COMPLETED
    assert client.output(load) == {'value': 1.0, 'extra': -1.0}


def test_a_failed_write_fails_a_record_persisted_at_submission(
    client: Client, store: GatedStore
) -> None:
    store.fill()
    load = client.submit(LOAD, {'run': dataset(run=1)}, persist=True)

    assert client.wait(load) is Status.FAILED
    assert client.failure(load) == 'the write failed: no space left on device'
    with pytest.raises(RuntimeError, match='no space left'):
        client.output(load, 'value')
    with pytest.raises(SubmitError, match='failed'):
        client.submit(SHIFT, {'value': load.ref('value')})


def test_a_reader_of_a_record_persisted_at_submission_fails_with_its_write(
    client: Client, store: GatedStore
) -> None:
    store.gate.clear()
    load = client.submit(LOAD, {'run': dataset(run=1)}, persist=True)
    shift = client.submit(SHIFT, {'value': load.ref('value')})
    store.fill()
    store.gate.set()

    assert client.wait([load, shift]) == [Status.FAILED, Status.FAILED]
    assert client.failure([load, shift]) == [
        'the write failed: no space left on device',
        f'input {load.id} failed',
    ]


def test_persist_keeps_the_client_s_hold_and_lets_other_clients_read(
    client: Client, connect: Callable[[], Client], store: GatedStore
) -> None:
    load = client.compute(LOAD, {'run': dataset(run=1)})
    other = connect()
    with pytest.raises(SubmitError, match='not kept by this client'):
        other.submit(SHIFT, {'value': load.ref('value')})

    store.gate.clear()
    client.persist(load, 'value')
    shift = other.submit(SHIFT, {'value': load.ref('value')})  # waits for the write
    assert client.output(load, 'value') == 1.0  # from memory meanwhile
    assert other.status(shift) is Status.PENDING
    store.gate.set()

    assert other.output(shift, 'value') == 11.0
    assert other.output(load, 'value') == 1.0
    client.release(load)
    assert client.output(load, 'value') == 1.0  # from the store
    with pytest.raises(LookupError, match='not kept by this client'):
        client.output(load, 'extra')


def test_a_failed_write_of_persist_fails_only_the_persist_request(
    client: Client, connect: Callable[[], Client], store: GatedStore
) -> None:
    load = client.compute(LOAD, {'run': dataset(run=1)})
    store.gate.clear()
    client.persist(load)
    other = connect()
    shift = other.submit(SHIFT, {'value': load.ref('value')})
    store.fill()
    store.gate.set()

    assert other.wait(shift) is Status.FAILED
    assert 'the write failed: no space left' in other.failure(shift)
    assert client.status(load) is Status.COMPLETED
    assert client.output(load, 'value') == 1.0  # the client still keeps it
    with pytest.raises(LookupError, match='the write failed: no space left'):
        other.output(load, 'value')
    with pytest.raises(SubmitError, match='the write failed: no space left'):
        other.submit(SHIFT, {'value': load.ref('value')})

    store.free()
    client.persist(load)  # ask again
    assert other.output(load, 'value') == 1.0


def test_persisting_what_is_persisted_logs_nothing(
    datasets: FakeDatasets, bind: dict[WorkflowSpec, Any], store: GatedStore
) -> None:
    log = Log()
    backend = Backend(datasets, bind, log=log, store=store)
    client = Client(backend, proposal='p1', submitter='anna')
    load = client.compute(LOAD, {'run': dataset(run=1)})
    client.persist(load, 'value')
    client.persist([load, load], 'value', 'extra')
    client.release(load)
    assert client.output(load, 'extra') == -1.0
    backend.close()

    persisted = [e for e in log if isinstance(e, Persist)]
    assert [e.outputs for e in persisted] == [('value',), ('extra',)]


def test_persist_is_refused_for_what_the_client_cannot_persist(
    client: Client, connect: Callable[[], Client], datasets: FakeDatasets
) -> None:
    load = client.compute(LOAD, {'run': dataset(run=1)})

    with pytest.raises(SubmitError, match='not kept by this client'):
        connect().persist(load)
    with pytest.raises(SubmitError, match=r"no outputs \['other'\]"):
        client.persist(load, 'other')
    with pytest.raises(SubmitError, match=r"no outputs \['other'\] to persist"):
        client.submit(LOAD, {'run': dataset(run=1)}, persist=('other',))
    with local(proposal='p1', datasets=datasets, bind={LOAD: lambda run: {}}) as alone:
        with pytest.raises(SubmitError, match='needs a store'):
            alone.submit(LOAD, {'run': dataset(run=1)}, persist=True)


def test_an_output_the_workflow_did_not_return_is_persisted_as_not_returned(
    client: Client, connect: Callable[[], Client]
) -> None:
    load = client.compute(LOAD, {'run': dataset(run=1)}, persist=True)

    with pytest.raises(LookupError, match='did not return it'):
        connect().output(load, 'note')
    assert connect().output(load) == {'value': 1.0, 'extra': -1.0}


def test_releasing_a_pending_record_persisted_by_the_client_still_writes_it(
    client: Client, connect: Callable[[], Client], loading: threading.Event
) -> None:
    loading.clear()
    load = client.submit(LOAD, {'run': dataset(run=1)})
    client.persist(load, 'value')
    client.release(load)  # the persist request keeps it
    loading.set()

    assert connect().output(load, 'value') == 1.0


def test_a_frozen_accumulator_persisted_is_kept_by_the_store(
    client: Client, connect: Callable[[], Client]
) -> None:
    load = client.compute(LOAD, {'run': dataset(run=1)})
    total = client.accumulator(Template(TOTAL, blanks=('parts',)))
    total.push({'parts': load.refs('value')})
    frozen = client.freeze(total, persist=True)

    assert connect().output(frozen, 'value') == 1.0
    assert client.output(frozen, 'value') == 1.0
    with pytest.raises(LookupError, match=f'read record {frozen.id} instead'):
        client.output(total, 'value')


def test_closing_a_local_client_waits_until_what_is_persisted_is_written(
    datasets: FakeDatasets, bind: dict[WorkflowSpec, Any], store: GatedStore
) -> None:
    client = local(proposal='p1', datasets=datasets, bind=bind, store=store)
    load = client.compute(LOAD, {'run': dataset(run=1)})
    store.gate.clear()
    client.persist(load)
    closing = threading.Thread(target=client.close)
    closing.start()
    closing.join(timeout=0.1)
    assert closing.is_alive()

    store.gate.set()
    closing.join(timeout=5)
    assert (load.id, 'value') in store


# A restart over the same log and store


@pytest.fixture
def start(
    datasets: FakeDatasets, bind: dict[WorkflowSpec, Any], store: GatedStore
) -> Iterator[Callable[[Path], Client]]:
    """A client of a new backend over the log at a path, with the same store."""
    backends: list[Backend] = []

    def start(path: Path) -> Client:
        backends.append(Backend(datasets, bind, log=Log(path), store=store))
        return Client(backends[-1], proposal='p1', submitter='anna')

    yield start
    store.gate.set()
    for backend in backends:
        backend.close()


@pytest.fixture
def restart(
    tmp_path: Path, start: Callable[[Path], Client]
) -> Callable[[Path], Client]:
    """A client of a backend over a copy of a log, as if the old one had stopped."""

    def restart(path: Path) -> Client:
        copy = tmp_path / uuid.uuid4().hex
        shutil.copy(path, copy)
        return start(copy)

    return restart


def test_a_restart_runs_pending_persisted_work_and_cancels_the_rest(
    tmp_path: Path,
    start: Callable[[Path], Client],
    restart: Callable[[Path], Client],
    loading: threading.Event,
) -> None:
    first = start(tmp_path / 'log')
    done = first.compute(LOAD, {'run': dataset(run=2)}, persist=('value',))
    loading.clear()
    load = first.submit(LOAD, {'run': dataset(run=1)})  # read by persisted work
    alone = first.submit(LOAD, {'run': dataset(run=2)})  # read by nothing persisted
    total = first.submit(
        TOTAL, {'parts': [load.refs('value'), done.refs('value')]}, persist=True
    )

    again = restart(tmp_path / 'log')
    loading.set()

    assert again.wait([load, alone, total]) == [
        Status.COMPLETED,
        Status.CANCELLED,
        Status.COMPLETED,
    ]
    assert again.failure(alone) == 'the backend restarted'
    assert again.output(total, 'value') == 3.0


def test_a_restart_fails_persisted_work_that_reads_what_is_gone(
    tmp_path: Path,
    start: Callable[[Path], Client],
    restart: Callable[[Path], Client],
    loading: threading.Event,
) -> None:
    first = start(tmp_path / 'log')
    kept = first.compute(LOAD, {'run': dataset(run=1)})
    loading.clear()
    pending = first.submit(LOAD, {'run': dataset(run=2)})
    total = first.accumulator(Template(TOTAL, blanks=('parts',)))
    total.push({'parts': pending.refs('value')})  # its state waits for ``pending``
    unpersisted = first.submit(
        TOTAL, {'parts': [kept.refs('value'), pending.refs('value')]}, persist=True
    )
    of_state = first.submit(SHIFT, {'value': total.ref('value')}, persist=True)
    (input_,) = of_state.request.inputs()
    state = next(r for r in first.records() if r.id == input_.record)
    downstream = first.submit(SHIFT, {'value': pending.ref('value')}, persist=True)

    again = restart(tmp_path / 'log')
    loading.set()

    records = [pending, unpersisted, state, of_state, downstream]
    assert again.wait(records) == [
        Status.COMPLETED,
        Status.FAILED,
        Status.FAILED,
        Status.FAILED,
        Status.COMPLETED,
    ]
    assert again.failure(records[1:4]) == [
        f'the backend restarted: input {kept.id} output value was not persisted',
        'the backend restarted: the held state it reads is gone',
        f'the backend restarted: input {state.id} failed',
    ]


def test_a_write_that_had_not_ended_at_a_restart_has_failed(
    tmp_path: Path,
    start: Callable[[Path], Client],
    restart: Callable[[Path], Client],
    store: GatedStore,
) -> None:
    first = start(tmp_path / 'log')
    load = first.compute(LOAD, {'run': dataset(run=1)})
    store.gate.clear()
    first.persist(load, 'value')

    again = restart(tmp_path / 'log')

    with pytest.raises(LookupError, match='the write failed: the backend restarted'):
        again.output(load, 'value')
    with pytest.raises(SubmitError, match='the write failed: the backend restarted'):
        again.submit(SHIFT, {'value': load.ref('value')})
