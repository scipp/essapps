# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The backend's log: what it holds, and a backend started from it."""

import shutil
import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NewType

import pytest
from pydantic import BaseModel, Field

from ess.dispatch import (
    Accumulator,
    Backend,
    Client,
    Record,
    Request,
    Status,
    SubmitError,
    Template,
    dataset,
)
from ess.dispatch.log import (
    Event,
    Finished,
    Log,
    NewRecord,
    NewStateRecord,
    Opened,
    Pushed,
    Submitted,
)
from ess.dispatch.testing import FakeDatasets, StagedFunction, kept_rows, read_dataset
from ess.spec import AccumulatorRef, DatasetRef, OutputRef, WorkflowSpec

SampleFile = NewType('SampleFile', Path)
Part = NewType('Part', float)


class Selection(BaseModel):
    window: tuple[float, float] = (0.0, 1.0)


class LoadParams(BaseModel):
    selection: Selection = Field(default_factory=Selection)


class LoadInputs(BaseModel):
    sample: SampleFile


class Value(BaseModel):
    value: float


LOAD = WorkflowSpec(
    name='load',
    version=1,
    title='load',
    description='load',
    params=LoadParams,
    inputs=LoadInputs,
    outputs=Value,
)


class Weight(BaseModel):
    factor: float = 1.0


class PartParams(BaseModel):
    weight: Weight | None = None


class PartInputs(BaseModel):
    part: Part


PART = WorkflowSpec(
    name='part',
    version=1,
    title='part',
    description='one part of a total',
    params=PartParams,
    inputs=PartInputs,
    outputs=Value,
)
TOTAL = PART.tabulate(name='total', version=1, tables={'part': ('weight',)})


def total(params: Any, inputs: Any) -> Value:
    factors = [1.0 if w is None else w.factor for w in params.weight]
    return Value(value=sum(f * p for f, p in zip(factors, inputs.part, strict=True)))


@pytest.fixture
def datasets(tmp_path: Path) -> FakeDatasets:
    datasets = FakeDatasets(proposal='p1', directory=tmp_path)
    for n in (1, 2, 3):
        datasets.measure(n, float(n))
    return datasets


@pytest.fixture
def loading() -> Iterator[threading.Event]:
    """Set to let LOAD finish; set again at the end of a test."""
    event = threading.Event()
    event.set()
    yield event
    event.set()


@pytest.fixture
def windows() -> list[Any]:
    """The ``window`` values LOAD received."""
    return []


@pytest.fixture
def start(
    datasets: FakeDatasets, loading: threading.Event, windows: list[Any]
) -> Iterator[Callable[[Path], Client]]:
    """A client of a new backend over the log at a path."""
    backends: list[Backend] = []

    def load(params: LoadParams, inputs: LoadInputs) -> Value:
        low, high = window = params.selection.window
        windows.append(window)
        loading.wait(timeout=5)
        return Value(value=read_dataset(inputs.sample) * (high - low))

    def start(path: Path) -> Client:
        bind = [StagedFunction(LOAD, load), kept_rows(TOTAL, total)]
        backends.append(Backend(datasets, bind, log=Log(path)))
        return Client(backends[-1], proposal='p1', submitter='anna')

    yield start
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


def _write(path: Path, *events: Event) -> None:
    with Log(path) as log:
        for event in events:
            log.append(event)


def _submitted(record_id: str, request: Request) -> Submitted:
    return Submitted(
        time=datetime(2026, 9, 30, tzinfo=UTC),
        proposal='p1',
        submitter='anna',
        records=(NewRecord(id=record_id, request=request, outputs=('value',)),),
    )


def _sample(n: int) -> dict[str, Any]:
    return {'sample': dataset(run=n)}


def _total(client: Client) -> Accumulator:
    """An accumulator of TOTAL, the weighted sum of a table of parts."""
    return client.accumulator(Template(TOTAL, blanks=('part',)))


def _read(total: Accumulator) -> dict[str, Any]:
    """The inputs of a request of TOTAL that reads the value of ``total``."""
    return {'part': [total.ref('value')]}


def _record(client: Client, record_id: str) -> Record:
    (record,) = [r for r in client.records() if r.id == record_id]
    return record


def test_a_backend_started_from_a_log_has_the_records_of_the_one_that_wrote_it(
    tmp_path: Path,
    start: Callable[[Path], Client],
    restart: Callable[[Path], Client],
) -> None:
    first = start(tmp_path / 'log')
    loads = {
        str(n): first.compute(LOAD, inputs=_sample(n), label='loads', member=str(n))
        for n in (1, 2, 3)
    }
    total = _total(first)
    for load in loads.values():
        total.push(part=[load.ref('value')])
    read = first.compute(TOTAL, inputs=_read(total))

    again = restart(tmp_path / 'log')

    assert again.records() == first.records()
    assert again.records(label='loads') == list(loads.values())
    state = _record(again, read.request.inputs['part'][0].record)
    assert again.provenance(read) == first.provenance(read)
    assert again.provenance(read).records() == [state, *loads.values()]


def test_values_are_the_same_after_a_restart_and_typed_for_the_binding(
    tmp_path: Path,
    start: Callable[[Path], Client],
    restart: Callable[[Path], Client],
    windows: list[Any],
) -> None:
    first = start(tmp_path / 'log')
    load = first.compute(
        LOAD, params={'selection': {'window': (1.0, 2.0)}}, inputs=_sample(3)
    )

    again = restart(tmp_path / 'log')
    rerun = again.compute(LOAD, params=load.request.params, inputs=load.request.inputs)

    assert again.records()[0] == load
    assert load.request.params['selection']['window'] == [1.0, 2.0]  # as JSON holds it
    assert windows == [(1.0, 2.0), (1.0, 2.0)]  # as the params model declares it
    assert again.output(rerun, 'value') == 3.0


def test_an_accumulator_is_logged_as_its_template_and_pushes_and_a_read_as_a_record(
    tmp_path: Path, start: Callable[[Path], Client]
) -> None:
    client = start(tmp_path / 'log')
    loads = [client.compute(LOAD, inputs=_sample(n)) for n in (1, 2, 3)]
    total = _total(client)
    for load in loads:
        total.push(part=[load.ref('value')])
    read = client.compute(TOTAL, inputs=_read(total))

    events = Log.read(tmp_path / 'log')
    assert events[6:10] == [
        Opened(
            accumulator=total.id,
            proposal='p1',
            template=Template(TOTAL, blanks=('part',)),
            params={'weight': []},
            inputs={'part': []},
        ),
        *(
            Pushed(
                accumulator=total.id,
                table='part',
                params={'weight': [None]},
                inputs={'part': [load.ref('value')]},
            )
            for load in loads
        ),
    ]
    state, reader = events[10].records  # the record of the state, then its reader
    assert state == NewStateRecord(
        id=state.id, accumulator=total.id, pushes=3, outputs=('value',)
    )
    assert reader.id == read.id
    assert read.request.inputs == {'part': [OutputRef(record=state.id, output='value')]}
    assert _record(client, state.id).request == Request(
        TOTAL,
        params={'weight': [None] * 3},
        inputs={'part': [load.ref('value') for load in loads]},
    )
    assert client.output(read, 'value') == 6.0


def test_a_push_is_logged_as_its_columns_by_side_with_references_as_written(
    tmp_path: Path, start: Callable[[Path], Client]
) -> None:
    client = start(tmp_path / 'log')
    loads = [client.compute(LOAD, inputs=_sample(n)) for n in (1, 2)]
    total = _total(client)
    total.push(
        part=[load.ref('value') for load in loads], weight=[Weight(factor=3), None]
    )

    (pushed,) = [e for e in Log.read(tmp_path / 'log') if isinstance(e, Pushed)]
    assert pushed == Pushed(
        accumulator=total.id,
        table='part',
        params={'weight': [{'factor': 3.0}, None]},
        inputs={'part': [load.ref('value') for load in loads]},
    )
    assert all(isinstance(ref, OutputRef) for ref in pushed.inputs['part'])
    assert client.output(total, 'value') == 5.0


def test_a_reference_is_logged_in_its_json_form_and_read_back(tmp_path: Path) -> None:
    sample = DatasetRef(dataset='uuid:run-1')
    part = OutputRef(record='r1', output='values', key='0')
    events: list[Event] = [
        _submitted('r2', Request(LOAD, inputs={'sample': sample})),
        Pushed(
            accumulator='a1',
            table='part',
            params={'weight': [None]},
            inputs={'part': [part]},
        ),
    ]
    _write(tmp_path / 'log', *events)

    submitted, pushed = (tmp_path / 'log').read_text().splitlines()
    assert '"sample":{"@ref":"dataset","dataset":"uuid:run-1"}' in submitted
    assert '{"@ref":"output","record":"r1","output":"values","key":"0"}' in pushed
    assert Log.read(tmp_path / 'log') == events


def test_records_pending_in_the_log_are_cancelled_at_a_restart(
    tmp_path: Path,
    start: Callable[[Path], Client],
    restart: Callable[[Path], Client],
    loading: threading.Event,
) -> None:
    first = start(tmp_path / 'log')
    done = first.compute(LOAD, inputs=_sample(1))
    loading.clear()
    pending = first.submit(LOAD, inputs=_sample(2))
    total = _total(first)
    total.push(part=[done.ref('value')])
    total.push(part=[pending.ref('value')])  # waits for ``pending``
    read = first.submit(TOTAL, inputs=_read(total))
    state = _record(first, read.request.inputs['part'][0].record)

    again = restart(tmp_path / 'log')
    loading.set()

    records = [pending, state, read]
    assert again.status(records) == [Status.CANCELLED] * 3  # no client keeps them
    assert again.failure(records) == ['the backend restarted'] * 3
    with pytest.raises(SubmitError, match=f'record {pending.id} cancelled'):
        again.submit(TOTAL, inputs={'part': [pending.ref('value')]})
    with pytest.raises(SubmitError, match='released or is unknown'):
        again.submit(TOTAL, inputs=_read(total))  # the accumulator did not survive


def test_outputs_are_not_in_the_log(
    tmp_path: Path,
    start: Callable[[Path], Client],
    restart: Callable[[Path], Client],
) -> None:
    load = start(tmp_path / 'log').compute(LOAD, inputs=_sample(1))

    again = restart(tmp_path / 'log')

    assert again.wait(load) == Status.COMPLETED
    with pytest.raises(LookupError, match='not kept by this client'):
        again.output(load, 'value')


def test_a_record_whose_output_is_not_kept_is_refused_at_the_push(
    tmp_path: Path,
    start: Callable[[Path], Client],
    restart: Callable[[Path], Client],
) -> None:
    load = start(tmp_path / 'log').compute(LOAD, inputs=_sample(1))

    again = restart(tmp_path / 'log')

    total = _total(again)
    with pytest.raises(SubmitError, match='not kept by this client'):
        total.push(part=[load.ref('value')])


def test_the_log_holds_submissions_finished_records_accumulators_and_pushes_only(
    tmp_path: Path, start: Callable[[Path], Client]
) -> None:
    client = start(tmp_path / 'log')
    client.stage(Template(LOAD, inputs=_sample(1), blanks=('selection',)))
    total = _total(client)
    load = client.compute(LOAD, inputs=_sample(1))
    total.push(part=[load.ref('value')])

    assert [type(e) for e in Log.read(tmp_path / 'log')] == [
        Opened,
        Submitted,
        Finished,
        Pushed,
    ]


def test_a_refused_call_writes_nothing_to_the_log(
    tmp_path: Path, start: Callable[[Path], Client]
) -> None:
    client = start(tmp_path / 'log')
    load = client.compute(LOAD, inputs=_sample(1))
    written = len(Log.read(tmp_path / 'log'))

    with pytest.raises(SubmitError, match='not parameters'):
        client.submit(LOAD, params={'bins': 2}, inputs=_sample(1))
    with pytest.raises(SubmitError, match='blanks of an accumulator'):
        client.accumulator(Template(TOTAL))
    with pytest.raises(SubmitError, match='makes no accumulators'):
        client.accumulator(Template(LOAD, blanks=('sample',)))
    total = _total(client)
    total.push(part=[load.ref('value')])
    written += 2  # the accumulator opened, and the push
    other = AccumulatorRef(accumulator=total.id, output='other')
    with pytest.raises(SubmitError, match='has no output'):
        client.submit(TOTAL, inputs={'part': [other]})
    with pytest.raises(SubmitError, match='not columns of a table'):
        total.push(other=[load.ref('value')])
    with pytest.raises(SubmitError, match='pushed without part'):
        total.push(weight=[Weight()])
    with pytest.raises(SubmitError, match='has no rows'):
        total.push(part=[])
    with pytest.raises(SubmitError, match=r'part\[1\]: repeats part\[0\]'):
        total.push(part=[load.ref('value')])

    assert len(Log.read(tmp_path / 'log')) == written


def test_a_write_that_fails_leaves_the_log_as_it_was(
    tmp_path: Path, datasets: FakeDatasets
) -> None:
    resource = pytest.importorskip('resource')
    path = tmp_path / 'log'
    request = Request(LOAD, inputs={'sample': datasets.resolve(dataset(run=1))})
    with Log(path) as log:
        log.append(_submitted('load', request))
        soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
        # A file limit that a second event exceeds, as a full disk would.
        resource.setrlimit(resource.RLIMIT_FSIZE, (path.stat().st_size + 10, hard))
        try:
            with pytest.raises(OSError, match='too large'):
                log.append(_submitted('again', request))
        finally:
            resource.setrlimit(resource.RLIMIT_FSIZE, (soft, hard))
        log.append(Finished(record='load', status=Status.COMPLETED))

    assert [type(e) for e in Log.read(path)] == [Submitted, Finished]


def test_a_log_file_has_one_writer(tmp_path: Path) -> None:
    path = tmp_path / 'log'
    with Log(path), pytest.raises(RuntimeError, match='held by another log'):
        Log(path)
    Log(path).close()  # free once the first has let go


def test_a_last_line_cut_short_is_dropped(
    tmp_path: Path, datasets: FakeDatasets
) -> None:
    path = tmp_path / 'log'
    request = Request(LOAD, inputs={'sample': datasets.resolve(dataset(run=1))})
    _write(path, _submitted('load', request))
    with path.open('ab') as file:
        file.write(b'{"kind":"finish')  # the backend stopped while writing

    _write(path, Finished(record='load', status=Status.COMPLETED))

    assert [type(e) for e in Log.read(path)] == [Submitted, Finished]


def test_a_line_that_is_not_an_event_is_refused(
    tmp_path: Path, start: Callable[[Path], Client]
) -> None:
    start(tmp_path / 'log').compute(LOAD, inputs=_sample(1))
    lines = (tmp_path / 'log').read_bytes().splitlines(keepends=True)
    (tmp_path / 'log').write_bytes(b''.join([lines[0], b'{}\n', *lines[1:]]))

    with pytest.raises(ValueError, match='kind'):
        Log.read(tmp_path / 'log')
