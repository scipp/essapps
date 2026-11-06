# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Persisting outputs: what the store keeps, who reads it, and a restart."""

import gc
import shutil
import threading
import uuid
import weakref
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field

from ess.apps import (
    Backend,
    Client,
    NotReadable,
    Record,
    RecordFailed,
    Status,
    SubmitError,
    Template,
    file,
    local,
)
from ess.apps.log import Finished, Log
from ess.apps.testing import (
    FakeFiles,
    FakeStore,
    StagedFunction,
    kept_rows,
    read_file,
)
from ess.spec import (
    Binding,
    Referenced,
    RunFile,
    WorkflowSpec,
    function_binding,
)


class Amount(BaseModel):
    """
    A number that one workflow outputs and another takes: a group, since a
    reference stands for a group only.
    """

    value: float


class Box:
    def __init__(self, value: float) -> None:
        self.value = value


class RunParams(BaseModel):
    sample: RunFile


class Loaded(BaseModel):
    value: Amount
    extra: Amount
    note: Amount | None = None


class SourceParams(BaseModel):
    source: Referenced[Amount]


class OffsetParams(BaseModel):
    source: Referenced[Amount]
    shift: Referenced[Amount]


class TotalParams(BaseModel):
    part: list[Referenced[Amount]] = Field(min_length=1)


class Value(BaseModel):
    value: Amount


class Listed(BaseModel):
    value: list[float]


class Boxed(BaseModel, arbitrary_types_allowed=True):
    """
    A group that holds a data value, so a reference fills its place,
    ``Referenced[...]``.
    """

    box: Box


class BoxOutputs(BaseModel):
    value: Boxed


class BoxParams(BaseModel):
    source: Referenced[Boxed]


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
    return {'sample': file(run=n)}


class GatedStore(FakeStore):
    """
    A fake store whose writes wait until ``gate`` is set; ``entered`` is set
    once one waits. ``forgotten`` holds the values it no longer holds, as if
    dropped to free disk space, by record and output. ``reads`` holds each
    value read, by record and output.
    """

    def __init__(self) -> None:
        super().__init__()
        self.gate = threading.Event()
        self.gate.set()
        self.entered = threading.Event()
        self.forgotten: set[tuple[str, str]] = set()
        self.reads: list[tuple[str, str]] = []

    def write(self, record: str, output: str, value: Any) -> None:
        self.entered.set()
        assert self.gate.wait(timeout=5), 'a test left the gate closed'
        super().write(record, output, value)

    def read(self, record: str, output: str) -> Any:
        if (record, output) in self.forgotten:
            raise KeyError((record, output))
        self.reads.append((record, output))
        return super().read(record, output)


@pytest.fixture
def files(tmp_path: Path) -> FakeFiles:
    files = FakeFiles(proposal='p1', directory=tmp_path)
    for n in (1, 2):
        files.measure(n, float(n))
    return files


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
        run = read_file(params.sample)
        loading.wait(timeout=5)
        return Loaded(value=Amount(value=run), extra=Amount(value=-run))

    def box(params: RunParams) -> BoxOutputs:
        boxes.append(weakref.ref(value := Box(read_file(params.sample))))
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
        function_binding(LIST, lambda p: Listed(value=[read_file(p.sample)])),
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
    files: FakeFiles,
    store: GatedStore,
    bind: list[Binding],
    log: Log,
) -> Iterator[Backend]:
    backend = Backend(files, bind, store=store, log=log)
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
    assert client.failure(load) == 'the write failed: OSError: no space left on device'
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
        'the write failed: OSError: no space left on device',
        f'record {load.id} failed',
    ]


def test_persisting_is_refused_for_what_cannot_be_persisted(
    client: Client, files: FakeFiles
) -> None:
    zero = Amount(value=0.0)

    with pytest.raises(SubmitError, match=r"no outputs \['other'\] to persist"):
        client.submit(LOAD, params=_sample(1), persist=('other',))
    with local(
        proposal='p1',
        files=files,
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
    files: FakeFiles, bind: list[Binding], store: GatedStore
) -> None:
    client = local(proposal='p1', files=files, bind=bind, store=store)
    store.gate.clear()
    load = client.submit(LOAD, params=_sample(1), persist=True)
    closing = threading.Thread(target=client.close)
    closing.start()
    closing.join(timeout=0.1)
    assert closing.is_alive()

    store.gate.set()
    closing.join(timeout=5)
    assert (load.id, 'value') in store


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
    listed = client.compute(LIST, params=_sample(1), persist=True)
    other = connect()

    other.output(listed, 'value').append(99.0)
    assert client.output(listed, 'value') == [1.0]
    assert other.output(listed, 'value') == [1.0]


def test_references_to_one_persisted_output_read_it_from_the_store_once(
    client: Client, store: GatedStore
) -> None:
    load = client.compute(LOAD, params=_sample(1), persist=('value',))
    assert client.output(load, 'value') == Amount(value=1.0)  # from the store
    store.reads.clear()

    both = {'source': load.ref('value'), 'shift': load.ref('value')}
    offset = client.compute(OFFSET, params=both)

    assert client.output(offset, 'value') == Amount(value=2.0)
    assert store.reads == [(load.id, 'value')]


# A restart over the same log and store


@pytest.fixture
def start(
    files: FakeFiles, bind: list[Binding], store: GatedStore
) -> Iterator[Callable[[Path], Client]]:
    """A client of a new backend over the log at a path, with the same store."""
    backends: list[Backend] = []

    def start(path: Path) -> Client:
        backends.append(Backend(files, bind, log=Log(path), store=store))
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


def _referenced(client: Client, record: Record) -> tuple[str, ...]:
    """The outputs of the record that a submission of the client may reference."""
    accepted = []
    for name in record.outputs:
        try:
            client.submit(SHIFT, params={'source': record.ref(name)})
        except SubmitError:
            continue
        accepted.append(name)
    return tuple(accepted)


def test_readable_gives_every_output_returned_of_a_record_the_client_keeps(
    client: Client,
) -> None:
    load = client.compute(LOAD, params=_sample(1))  # returns no note

    assert client.readable(load) == ('value', 'extra') == _referenced(client, load)


def test_readable_gives_every_output_of_a_pending_record_without_waiting(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    load = client.submit(LOAD, params=_sample(1))

    assert client.readable(load) == ('value', 'extra', 'note')
    assert client.status(load) is Status.PENDING
    assert _referenced(client, load) == ('value', 'extra', 'note')
    loading.set()


def test_readable_gives_nothing_of_a_record_released_or_of_another_client(
    client: Client, connect: Callable[[], Client]
) -> None:
    released, kept = (
        client.compute(LOAD, params=_sample(1)),
        client.compute(LOAD, params=_sample(2)),
    )
    client.release(released)
    other = connect()

    assert client.readable([released, kept]) == [(), ('value', 'extra')]
    assert other.readable({'kept': kept}) == {'kept': ()}
    assert _referenced(client, released) == _referenced(other, kept) == ()


def test_readable_gives_the_outputs_persisted_to_every_client(
    client: Client, connect: Callable[[], Client], store: GatedStore
) -> None:
    store.gate.clear()
    load = client.submit(LOAD, params=_sample(1), persist=('value',))
    other = connect()

    # The write is pending: a submission is accepted, and waits for it.
    assert client.readable(load) == other.readable(load) == ('value',)
    assert _referenced(other, load) == ('value',)
    store.gate.set()
    client.wait(load)
    assert client.readable(load) == other.readable(load) == ('value',)
    assert _referenced(client, load) == ('value',)


def test_readable_gives_nothing_whose_write_failed(
    client: Client, connect: Callable[[], Client], store: GatedStore
) -> None:
    store.fill()
    load = client.compute(LOAD, params=_sample(1), persist=('value',))
    other = connect()

    assert other.readable(load) == () == _referenced(other, load)
    assert client.readable(load) == ()


def test_readable_gives_nothing_of_a_record_that_failed_or_was_cancelled(
    client: Client, store: GatedStore, loading: threading.Event
) -> None:
    store.fill()
    failed = client.submit(LOAD, params=_sample(1), persist=True)
    client.wait(failed)
    loading.clear()
    cancelled = client.submit(LOAD, params=_sample(2))
    client.cancel(cancelled)

    assert client.status([failed, cancelled]) == [Status.FAILED, Status.CANCELLED]
    assert client.readable([failed, cancelled]) == [(), ()]
    assert _referenced(client, failed) == _referenced(client, cancelled) == ()
    loading.set()


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


def test_a_read_of_a_persisted_output_waits_for_its_write_and_no_other_does(
    client: Client, connect: Callable[[], Client], store: GatedStore
) -> None:
    store.gate.clear()
    load = client.submit(LOAD, params=_sample(1), persist=('value',))
    other = connect()

    with ThreadPoolExecutor() as pool:
        value = pool.submit(other.output, load, 'value')
        assert store.entered.wait(timeout=5)
        with pytest.raises(NotReadable, match='not kept by this client'):
            other.output(load, 'extra')  # never written, so nothing to wait for
        store.gate.set()
        assert value.result(timeout=5) == Amount(value=1.0)


def test_a_pending_record_that_persists_outlives_its_release_and_its_last_reader(
    client: Client, store: GatedStore
) -> None:
    store.gate.clear()
    load = client.submit(LOAD, params=_sample(1), persist=True)
    shift = client.submit(SHIFT, params={'source': load.ref('value')})

    client.release(load)  # the store keeps it, not the client
    client.release(shift)  # cancels the reader, which lets go of ``load``

    assert client.status(load) is Status.PENDING
    store.gate.set()
    assert client.wait(load) is Status.COMPLETED
    assert client.output(load, 'value') == Amount(value=1.0)


def test_reading_every_output_gives_those_persisted(
    client: Client, connect: Callable[[], Client]
) -> None:
    load = client.compute(LOAD, params=_sample(1), persist=('value',))

    expected = {'value': Amount(value=1.0)}
    assert client.output(load) == expected
    assert connect().output(load) == expected


def test_a_persisted_value_is_read_from_the_store_while_a_reader_holds_it(
    client: Client,
    connect: Callable[[], Client],
    store: GatedStore,
    loading: threading.Event,
) -> None:
    kept = client.compute(LOAD, params=_sample(1))
    loading.clear()
    pending = client.submit(LOAD, params=_sample(2))
    store.gate.clear()
    shifted = client.submit(SHIFT, params={'source': kept.ref('value')}, persist=True)
    reader = client.submit(
        OFFSET,
        params={'source': shifted.ref('value'), 'shift': pending.ref('value')},
    )
    store.gate.set()
    client.wait(shifted)

    assert connect().output(shifted, 'value') == Amount(value=11.0)
    assert store.reads == [(shifted.id, 'value')]  # not the value ``reader`` holds
    loading.set()
    assert client.output(reader, 'value') == Amount(value=13.0)


def test_a_client_that_persists_a_freeze_does_not_keep_it(
    client: Client, store: GatedStore
) -> None:
    load = client.compute(LOAD, params=_sample(1))
    total = client.accumulator(Template(TOTAL, blanks=('part',)))
    total.push(part=[load.ref('value')])
    frozen = client.freeze(total, persist=True)
    client.wait(frozen)
    store.forgotten.add((frozen.id, 'value'))

    with pytest.raises(NotReadable, match='the store no longer holds'):
        client.output(frozen, 'value')
