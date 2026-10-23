# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Persisting outputs: what the store keeps, who reads it, and a restart."""

import gc
import shutil
import threading
import uuid
import weakref
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, NewType

import pytest
from pydantic import BaseModel, Field

from ess.dispatch import (
    Backend,
    Client,
    ClientEnded,
    NotReadable,
    RecordFailed,
    Status,
    SubmitError,
    Template,
    dataset,
    local,
)
from ess.dispatch.log import Finished, Log, Persist
from ess.dispatch.testing import (
    FakeDatasets,
    FakeStore,
    StagedFunction,
    kept_rows,
    read_dataset,
)
from ess.spec import Binding, WorkflowSpec, function_binding


class Amount(BaseModel):
    """
    A number that one workflow outputs and another takes: a group, since a
    reference stands for a group or a file only.
    """

    value: float


SampleFile = NewType('SampleFile', Path)


class Box:
    def __init__(self, value: float) -> None:
        self.value = value


class RunParams(BaseModel):
    sample: SampleFile


class Loaded(BaseModel):
    value: Amount
    extra: Amount
    note: Amount | None = None


class SourceParams(BaseModel):
    source: Amount


class OffsetParams(BaseModel):
    source: Amount
    shift: Amount


class TotalParams(BaseModel):
    part: list[Amount] = Field(min_length=1)


class Value(BaseModel):
    value: Amount


class Listed(BaseModel):
    value: list[float]


class Boxed(BaseModel, arbitrary_types_allowed=True):
    """A group that holds a data value, so is given by reference only."""

    box: Box


class BoxOutputs(BaseModel):
    value: Boxed


class BoxParams(BaseModel):
    source: Boxed


def _spec(
    name: str,
    params: type[BaseModel],
    outputs: type[BaseModel],
    tables: tuple[str, ...] = (),
) -> WorkflowSpec[Any, Any]:
    return WorkflowSpec(
        name=name,
        version=1,
        title=name,
        description=name,
        params=params,
        outputs=outputs,
        tables=tables,
    )


LOAD = _spec('load', RunParams, Loaded)
SHIFT = _spec('shift', SourceParams, Value)
TOTAL = _spec('total', TotalParams, Value, tables=('part',))
LIST = _spec('list', RunParams, Listed)  # a value that can be changed in place
BOX = _spec('box', RunParams, BoxOutputs)  # a value that can be referenced weakly
UNBOX = _spec('unbox', BoxParams, Value)
OFFSET = _spec('offset', OffsetParams, Value)


def _sample(n: int) -> dict[str, Any]:
    return {'sample': dataset(run=n)}


class GatedStore(FakeStore):
    """
    A fake store whose writes wait until ``gate`` is set; ``entered`` is set
    once one waits. ``forgotten`` holds the values it no longer holds, as if
    dropped to free disk space, by record and output.
    """

    def __init__(self) -> None:
        super().__init__()
        self.gate = threading.Event()
        self.gate.set()
        self.entered = threading.Event()
        self.forgotten: set[tuple[str, str]] = set()

    def write(self, record: str, output: str, value: Any) -> None:
        self.entered.set()
        assert self.gate.wait(timeout=5), 'a test left the gate closed'
        super().write(record, output, value)

    def read(self, record: str, output: str) -> Any:
        if (record, output) in self.forgotten:
            raise KeyError((record, output))
        return super().read(record, output)


@pytest.fixture
def datasets(tmp_path: Path) -> FakeDatasets:
    datasets = FakeDatasets(proposal='p1', directory=tmp_path)
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
def boxes() -> list[weakref.ref[Box]]:
    """A weak reference to each value that BOX returned."""
    return []


@pytest.fixture
def bind(loading: threading.Event, boxes: list[weakref.ref[Box]]) -> list[Binding]:
    def load(params: RunParams) -> Loaded:
        run = read_dataset(params.sample)
        loading.wait(timeout=5)
        return Loaded(value=Amount(value=run), extra=Amount(value=-run))

    def box(params: RunParams) -> BoxOutputs:
        boxes.append(weakref.ref(value := Box(read_dataset(params.sample))))
        return BoxOutputs(value=Boxed(box=value))

    def total(params: TotalParams) -> Value:
        return Value(value=Amount(value=sum(part.value for part in params.part)))

    def shift(params: SourceParams) -> Value:
        return Value(value=Amount(value=params.source.value + 10.0))

    def offset(params: OffsetParams) -> Value:
        return Value(value=Amount(value=params.source.value + params.shift.value))

    return [
        function_binding(LOAD, load),
        function_binding(SHIFT, shift),
        kept_rows(TOTAL, total),
        function_binding(LIST, lambda p: Listed(value=[read_dataset(p.sample)])),
        function_binding(BOX, box),
        function_binding(
            UNBOX, lambda p: Value(value=Amount(value=p.source.box.value))
        ),
        StagedFunction(OFFSET, offset),
    ]


@pytest.fixture
def log() -> Log:
    return Log()


@pytest.fixture
def backend(
    datasets: FakeDatasets,
    store: GatedStore,
    bind: list[Binding],
    log: Log,
) -> Iterator[Backend]:
    backend = Backend(datasets, bind, store=store, log=log)
    yield backend
    store.gate.set()
    backend.close()


@pytest.fixture
def connect(backend: Backend) -> Callable[[], Client]:
    return lambda: Client(backend, proposal='p1', submitter='anna')


@pytest.fixture
def client(connect: Callable[[], Client]) -> Client:
    return connect()


def test_a_value_the_store_no_longer_holds_cannot_be_read(
    client: Client, store: GatedStore
) -> None:
    load = client.compute(LOAD, params=_sample(1), persist=True)
    store.forgotten.add((load.id, 'value'))

    with pytest.raises(NotReadable, match='the store no longer holds'):
        client.output(load, 'value')


def test_persisted_at_submission_the_store_keeps_the_outputs_named(
    client: Client, connect: Callable[[], Client], store: GatedStore
) -> None:
    load = client.compute(LOAD, params=_sample(1), persist=('value',))

    other = connect()
    assert other.output(load, 'value') == Amount(value=1.0)
    assert (load.id, 'value') in store
    assert (load.id, 'extra') not in store
    with pytest.raises(NotReadable, match='not kept by this client'):
        client.output(load, 'extra')  # dropped once the record completed
    with pytest.raises(SubmitError, match='not kept by this client'):
        client.submit(SHIFT, params={'source': load.ref('extra')})
    shift = other.compute(SHIFT, params={'source': load.ref('value')})
    assert other.output(shift, 'value') == Amount(value=11.0)


def test_a_record_persisted_at_submission_completes_once_written(
    client: Client, store: GatedStore
) -> None:
    store.gate.clear()
    load = client.submit(LOAD, params=_sample(1), persist=True)

    assert client.status(load) is Status.PENDING
    store.gate.set()
    assert client.wait(load) is Status.COMPLETED
    assert client.output(load) == {
        'value': Amount(value=1.0),
        'extra': Amount(value=-1.0),
    }


def test_a_failed_write_fails_a_record_persisted_at_submission(
    client: Client, store: GatedStore
) -> None:
    store.fill()
    load = client.submit(LOAD, params=_sample(1), persist=True)

    assert client.wait(load) is Status.FAILED
    assert client.failure(load) == 'the write failed: no space left on device'
    with pytest.raises(RecordFailed, match='no space left'):
        client.output(load, 'value')
    with pytest.raises(SubmitError, match='failed'):
        client.submit(SHIFT, params={'source': load.ref('value')})


def test_a_reader_of_a_record_persisted_at_submission_fails_with_its_write(
    client: Client, store: GatedStore
) -> None:
    store.gate.clear()
    load = client.submit(LOAD, params=_sample(1), persist=True)
    shift = client.submit(SHIFT, params={'source': load.ref('value')})
    store.fill()
    store.gate.set()

    assert client.wait([load, shift]) == [Status.FAILED, Status.FAILED]
    assert client.failure([load, shift]) == [
        'the write failed: no space left on device',
        f'record {load.id} failed',
    ]


def test_persist_keeps_the_client_s_hold_and_lets_other_clients_read(
    client: Client, connect: Callable[[], Client], store: GatedStore
) -> None:
    load = client.compute(LOAD, params=_sample(1))
    other = connect()
    with pytest.raises(SubmitError, match='not kept by this client'):
        other.submit(SHIFT, params={'source': load.ref('value')})

    store.gate.clear()
    client.persist(load, 'value')
    # The request waits for the write.
    shift = other.submit(SHIFT, params={'source': load.ref('value')})
    assert client.output(load, 'value') == Amount(value=1.0)  # from memory meanwhile
    assert other.status(shift) is Status.PENDING
    store.gate.set()

    assert other.output(shift, 'value') == Amount(value=11.0)
    assert other.output(load, 'value') == Amount(value=1.0)
    client.release(load)
    assert client.output(load, 'value') == Amount(value=1.0)  # from the store
    with pytest.raises(NotReadable, match='not kept by this client'):
        client.output(load, 'extra')


def test_a_failed_write_of_persist_fails_only_the_persist_request(
    client: Client, connect: Callable[[], Client], store: GatedStore
) -> None:
    load = client.compute(LOAD, params=_sample(1))
    store.gate.clear()
    client.persist(load)
    other = connect()
    shift = other.submit(SHIFT, params={'source': load.ref('value')})
    store.fill()
    store.gate.set()

    assert other.wait(shift) is Status.FAILED
    assert 'the write failed: no space left' in other.failure(shift)
    assert client.status(load) is Status.COMPLETED
    assert client.output(load, 'value') == Amount(
        value=1.0
    )  # the client still keeps it
    with pytest.raises(NotReadable, match='the write failed: no space left'):
        other.output(load, 'value')
    with pytest.raises(SubmitError, match='the write failed: no space left'):
        other.submit(SHIFT, params={'source': load.ref('value')})

    store.free()
    client.persist(load)  # ask again
    assert other.output(load, 'value') == Amount(value=1.0)


def test_persisting_what_is_persisted_logs_nothing(client: Client, log: Log) -> None:
    load = client.compute(LOAD, params=_sample(1))
    client.persist(load, 'value')
    client.persist([load, load], 'value', 'extra')
    client.release(load)
    assert client.output(load, 'extra') == Amount(value=-1.0)

    persisted = [e for e in log if isinstance(e, Persist)]
    assert [e.outputs for e in persisted] == [('value',), ('extra',)]


def test_persist_is_refused_for_what_the_client_cannot_persist(
    client: Client, connect: Callable[[], Client], datasets: FakeDatasets
) -> None:
    load = client.compute(LOAD, params=_sample(1))
    zero = Amount(value=0.0)

    with pytest.raises(SubmitError, match='not kept by this client'):
        connect().persist(load)
    with pytest.raises(SubmitError, match=r"no outputs \['other'\]"):
        client.persist(load, 'other')
    with pytest.raises(SubmitError, match=r"no outputs \['other'\] to persist"):
        client.submit(LOAD, params=_sample(1), persist=('other',))
    with local(
        proposal='p1',
        datasets=datasets,
        bind=[function_binding(LOAD, lambda p: Loaded(value=zero, extra=zero))],
    ) as alone:
        with pytest.raises(SubmitError, match='needs a store'):
            alone.submit(LOAD, params=_sample(1), persist=True)


def test_an_output_the_workflow_did_not_return_is_persisted_as_not_returned(
    client: Client, connect: Callable[[], Client]
) -> None:
    load = client.compute(LOAD, params=_sample(1), persist=True)

    with pytest.raises(NotReadable, match='did not return it'):
        connect().output(load, 'note')
    assert connect().output(load) == {
        'value': Amount(value=1.0),
        'extra': Amount(value=-1.0),
    }


def test_releasing_a_pending_record_persisted_by_the_client_still_writes_it(
    client: Client, connect: Callable[[], Client], loading: threading.Event
) -> None:
    loading.clear()
    load = client.submit(LOAD, params=_sample(1))
    client.persist(load, 'value')
    client.release(load)  # the persist request keeps it
    loading.set()

    assert connect().output(load, 'value') == Amount(value=1.0)


def test_a_frozen_accumulator_persisted_is_kept_by_the_store(
    client: Client, connect: Callable[[], Client]
) -> None:
    load = client.compute(LOAD, params=_sample(1))
    total = client.accumulator(Template(TOTAL, blanks=('part',)))
    total.push(part=[load.ref('value')])
    frozen = client.freeze(total, persist=True)

    assert connect().output(frozen, 'value') == Amount(value=1.0)
    assert client.output(frozen, 'value') == Amount(value=1.0)
    with pytest.raises(NotReadable, match=f'read record {frozen.id} instead'):
        client.output(total, 'value')


def test_closing_a_local_client_waits_until_what_is_persisted_is_written(
    datasets: FakeDatasets, bind: list[Binding], store: GatedStore
) -> None:
    client = local(proposal='p1', datasets=datasets, bind=bind, store=store)
    load = client.compute(LOAD, params=_sample(1))
    store.gate.clear()
    client.persist(load)
    closing = threading.Thread(target=client.close)
    closing.start()
    closing.join(timeout=0.1)
    assert closing.is_alive()

    store.gate.set()
    closing.join(timeout=5)
    assert (load.id, 'value') in store


def test_persisting_a_pending_record_does_not_delay_its_completion(
    client: Client, store: GatedStore, loading: threading.Event
) -> None:
    loading.clear()
    load = client.submit(LOAD, params=_sample(1))
    client.persist(load, 'value')
    store.gate.clear()
    loading.set()

    assert client.wait(load) is Status.COMPLETED  # while its write waits
    assert client.output(load, 'value') == Amount(value=1.0)
    store.gate.set()


def test_a_record_lets_go_of_its_references_before_its_write(
    client: Client, store: GatedStore, boxes: list[weakref.ref[Box]]
) -> None:
    box = client.submit(BOX, params=_sample(1))
    store.gate.clear()
    unbox = client.submit(UNBOX, params={'source': box.ref('value')}, persist=True)
    client.release(box)  # only ``unbox`` reads it
    assert store.entered.wait(timeout=5)

    gc.collect()
    assert boxes[0]() is None  # dropped while the write of ``unbox`` waits
    assert client.status(unbox) is Status.PENDING
    store.gate.set()
    assert client.wait(unbox) is Status.COMPLETED


def test_another_client_reads_a_persisted_value_from_the_store(
    client: Client, connect: Callable[[], Client]
) -> None:
    listed = client.compute(LIST, params=_sample(1))
    client.persist(listed)
    other = connect()

    other.output(listed, 'value').append(99.0)
    assert client.output(listed, 'value') == [1.0]  # the client's own value
    assert other.output(listed, 'value') == [1.0]


def test_reading_every_output_raises_if_a_write_failed(
    client: Client, connect: Callable[[], Client], store: GatedStore
) -> None:
    load = client.compute(LOAD, params=_sample(1))
    store.fill()
    client.persist(load, 'value')

    with pytest.raises(NotReadable, match='the write failed: no space left'):
        connect().output(load)
    store.free()
    client.persist(load, 'value')
    assert connect().output(load) == {'value': Amount(value=1.0)}


def test_closing_a_client_raises_if_a_write_it_asked_for_failed(
    client: Client, store: GatedStore
) -> None:
    load = client.compute(LOAD, params=_sample(1))
    shift = client.compute(SHIFT, params={'source': load.ref('value')})
    store.fill()
    client.persist(load, 'value')
    client.persist(shift)

    with pytest.raises(RuntimeError, match='writes failed') as raised:
        client.close()
    assert f'record {load.id} output value: no space left' in str(raised.value)
    assert f'record {shift.id} output value: no space left' in str(raised.value)
    with pytest.raises(ClientEnded):
        client.output(load, 'value')


def test_closing_a_client_whose_failed_write_succeeded_again_raises_nothing(
    client: Client, connect: Callable[[], Client], store: GatedStore
) -> None:
    load = client.compute(LOAD, params=_sample(1))
    store.fill()
    client.persist(load, 'value')
    with pytest.raises(NotReadable, match='the write failed'):
        connect().output(load, 'value')  # waits for the write
    store.free()
    client.persist(load, 'value')

    client.close()


def test_a_frozen_record_kept_by_the_client_is_persisted_later(
    client: Client, connect: Callable[[], Client]
) -> None:
    load = client.compute(LOAD, params=_sample(1))
    total = client.accumulator(Template(TOTAL, blanks=('part',)))
    total.push(part=[load.ref('value')])
    frozen = client.freeze(total)
    client.persist(frozen)

    assert connect().output(frozen, 'value') == Amount(value=1.0)


# A restart over the same log and store


@pytest.fixture
def start(
    datasets: FakeDatasets, bind: list[Binding], store: GatedStore
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
    done = first.compute(LOAD, params=_sample(2), persist=('value',))
    loading.clear()
    load = first.submit(LOAD, params=_sample(1))  # read by persisted work
    alone = first.submit(LOAD, params=_sample(2))  # read by nothing persisted
    total = first.submit(
        TOTAL, params={'part': [load.ref('value'), done.ref('value')]}, persist=True
    )

    again = restart(tmp_path / 'log')
    loading.set()

    assert again.wait([load, alone, total]) == [
        Status.COMPLETED,
        Status.CANCELLED,
        Status.COMPLETED,
    ]
    assert again.failure(alone) == 'the backend restarted'
    assert again.output(total, 'value') == Amount(value=3.0)


def test_a_restart_fails_persisted_work_that_reads_what_is_gone(
    tmp_path: Path,
    start: Callable[[Path], Client],
    restart: Callable[[Path], Client],
    loading: threading.Event,
) -> None:
    first = start(tmp_path / 'log')
    kept = first.compute(LOAD, params=_sample(1))
    loading.clear()
    pending = first.submit(LOAD, params=_sample(2))
    total = first.accumulator(Template(TOTAL, blanks=('part',)))
    total.push(part=[pending.ref('value')])  # its state waits for ``pending``
    unpersisted = first.submit(
        TOTAL, params={'part': [kept.ref('value'), pending.ref('value')]}, persist=True
    )
    of_state = first.submit(SHIFT, params={'source': total.ref('value')}, persist=True)
    (input_,) = of_state.request.upstream()
    state = next(r for r in first.records() if r.id == input_.record)
    downstream = first.submit(
        SHIFT, params={'source': pending.ref('value')}, persist=True
    )

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
        f'the backend restarted: record {kept.id} output value was not persisted',
        'the backend restarted: the driver it reads is gone',
        f'the backend restarted: record {state.id} failed',
    ]


def test_a_restart_runs_a_call_through_a_stage_as_its_plain_request(
    tmp_path: Path,
    start: Callable[[Path], Client],
    restart: Callable[[Path], Client],
    loading: threading.Event,
) -> None:
    first = start(tmp_path / 'log')
    kept = first.compute(LOAD, params=_sample(1))
    stored = first.compute(LOAD, params=_sample(2), persist=('value',))
    loading.clear()
    offset = first.submit(LOAD, params=_sample(2))
    calls = []
    for fixed in (kept, stored):
        stage = first.stage(
            Template(OFFSET, params={'source': fixed.ref('value')}, blanks=('shift',))
        )
        first.compute(stage, params={'shift': kept.ref('value')})  # stages it
        calls.append(
            first.submit(stage, params={'shift': offset.ref('value')}, persist=True)
        )

    again = restart(tmp_path / 'log')
    loading.set()

    assert again.wait(calls) == [Status.FAILED, Status.COMPLETED]
    assert again.failure(calls[0]) == (
        f'the backend restarted: record {kept.id} output value was not persisted'
    )
    assert again.output(calls[1], 'value') == Amount(value=4.0)


def test_a_write_that_had_not_ended_at_a_restart_has_failed(
    tmp_path: Path,
    start: Callable[[Path], Client],
    restart: Callable[[Path], Client],
    store: GatedStore,
) -> None:
    first = start(tmp_path / 'log')
    load = first.compute(LOAD, params=_sample(1))
    store.gate.clear()
    first.persist(load, 'value')

    again = restart(tmp_path / 'log')

    with pytest.raises(NotReadable, match='the write failed: the backend restarted'):
        again.output(load, 'value')
    with pytest.raises(SubmitError, match='the write failed: the backend restarted'):
        again.submit(SHIFT, params={'source': load.ref('value')})


def test_a_restart_runs_again_a_record_written_but_not_finished(
    tmp_path: Path, start: Callable[[Path], Client], store: GatedStore
) -> None:
    first = start(tmp_path / 'log')
    load = first.compute(LOAD, params=_sample(1), persist=('value',))
    lines = (tmp_path / 'log').read_bytes().splitlines(keepends=True)
    assert isinstance(Log.read(tmp_path / 'log')[-1], Finished)
    (tmp_path / 'crashed').write_bytes(b''.join(lines[:-1]))  # before it finished

    again = start(tmp_path / 'crashed')

    assert again.wait(load) is Status.COMPLETED
    assert again.output(load, 'value') == Amount(value=1.0)  # written again


def test_a_restart_runs_a_pending_record_that_the_client_persisted(
    tmp_path: Path,
    start: Callable[[Path], Client],
    restart: Callable[[Path], Client],
    loading: threading.Event,
) -> None:
    first = start(tmp_path / 'log')
    loading.clear()
    load = first.submit(LOAD, params=_sample(1))
    first.persist(load, 'value')

    again = restart(tmp_path / 'log')
    loading.set()

    assert again.wait(load) is Status.COMPLETED
    assert again.output(load, 'value') == Amount(value=1.0)


def test_a_restart_fails_a_reader_of_a_write_that_was_lost(
    tmp_path: Path,
    start: Callable[[Path], Client],
    restart: Callable[[Path], Client],
    store: GatedStore,
) -> None:
    first = start(tmp_path / 'log')
    load = first.compute(LOAD, params=_sample(1))
    store.gate.clear()
    first.persist(load, 'value')
    shift = first.submit(SHIFT, params={'source': load.ref('value')}, persist=True)

    again = restart(tmp_path / 'log')

    assert again.wait(shift) is Status.FAILED
    assert again.failure(shift) == (
        f'the backend restarted: record {load.id} output value: '
        'the write failed: the backend restarted'
    )
