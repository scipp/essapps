# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The backend's log: what it holds, and a backend started from it."""

import json
import shutil
import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field

from ess.apps import (
    Accumulator,
    Backend,
    Client,
    NotReadable,
    Record,
    Request,
    Status,
    SubmitError,
    Template,
    dataset,
)
from ess.apps.log import (
    Event,
    Finished,
    Log,
    NewRecord,
    NewStateRecord,
    Opened,
    Pushed,
    Submitted,
)
from ess.apps.testing import FakeDatasets, StagedFunction, kept_rows, read_dataset
from ess.spec import AccumulatorRef, DatasetRef, OutputRef, WorkflowSpec


class SampleRun(BaseModel):
    file: Path


class Window(BaseModel):
    start: float = 0.0
    stop: float = 1.0


class LoadParams(BaseModel):
    window: Window = Field(default_factory=Window)
    sample: SampleRun


class Amount(BaseModel):
    """A value one workflow returns and another takes by reference."""

    value: float


class ValueOutputs(BaseModel):
    value: Amount


LOAD = WorkflowSpec(
    name='load',
    version=1,
    title='load',
    description='load',
    params=LoadParams,
    outputs=ValueOutputs,
)


class Weight(BaseModel):
    factor: float = 1.0


class TotalParams(BaseModel):
    weight: Weight | None = None
    part: list[Amount] = Field(min_length=1)


TOTAL = WorkflowSpec(
    name='total',
    version=1,
    title='total',
    description='a total of parts',
    params=TotalParams,
    outputs=ValueOutputs,
    tables=('part',),
)


def total(params: TotalParams) -> ValueOutputs:
    factor = 1.0 if params.weight is None else params.weight.factor
    return ValueOutputs(value=Amount(value=factor * sum(p.value for p in params.part)))


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

    def load(params: LoadParams) -> ValueOutputs:
        window = params.window
        windows.append(window)
        loading.wait(timeout=5)
        value = read_dataset(params.sample.file) * (window.stop - window.start)
        return ValueOutputs(value=Amount(value=value))

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
    return {'sample': {'file': dataset(run=n)}}


def _total(client: Client) -> Accumulator:
    """An accumulator of TOTAL, the weighted sum of a table of parts."""
    return client.accumulator(Template(TOTAL, blanks=('part',)))


def _read(total: Accumulator) -> dict[str, Any]:
    """The params of a request of TOTAL that reads the value of ``total``."""
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
        str(n): first.compute(LOAD, params=_sample(n), label='loads', member=str(n))
        for n in (1, 2, 3)
    }
    total = _total(first)
    for load in loads.values():
        total.push(part=[load.ref('value')])
    read = first.compute(TOTAL, params=_read(total))

    again = restart(tmp_path / 'log')

    assert again.records() == first.records()
    assert again.records(label='loads') == list(loads.values())
    state = _record(again, read.request.params['part'][0].record)
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
        LOAD, params={'window': Window(start=1.0, stop=2.0), **_sample(3)}
    )

    again = restart(tmp_path / 'log')
    rerun = again.compute(LOAD, params=load.request.params)

    assert again.records()[0] == load
    # The log holds the JSON form; the binding gets the type the params declare.
    assert load.request.params['window'] == {'start': 1.0, 'stop': 2.0}
    assert windows == [Window(start=1.0, stop=2.0)] * 2
    assert again.output(rerun, 'value') == Amount(value=3.0)


def test_an_accumulator_is_logged_as_its_template_and_pushes_and_a_read_as_a_record(
    tmp_path: Path, start: Callable[[Path], Client]
) -> None:
    client = start(tmp_path / 'log')
    loads = [client.compute(LOAD, params=_sample(n)) for n in (1, 2, 3)]
    total = _total(client)
    for load in loads:
        total.push(part=[load.ref('value')])
    read = client.compute(TOTAL, params=_read(total))

    events = Log.read(tmp_path / 'log')
    assert events[6:10] == [
        Opened(
            accumulator=total.id,
            proposal='p1',
            template=Template(TOTAL, blanks=('part',)),
            params={'weight': None, 'part': []},
        ),
        *(
            Pushed(
                accumulator=total.id,
                params={'part': [load.ref('value')]},
            )
            for load in loads
        ),
    ]
    state, reader = events[10].records  # the record of the state, then its reader
    assert state == NewStateRecord(
        id=state.id, accumulator=total.id, pushes=3, outputs=('value',)
    )
    assert reader.id == read.id
    assert read.request.params['part'] == [OutputRef(record=state.id, output='value')]
    assert _record(client, state.id).request == Request(
        TOTAL,
        params={
            'weight': None,
            'part': [load.ref('value') for load in loads],
        },
    )
    assert client.output(read, 'value') == Amount(value=6.0)


def test_a_push_is_logged_as_its_rows_by_table_with_references_as_written(
    tmp_path: Path, start: Callable[[Path], Client]
) -> None:
    client = start(tmp_path / 'log')
    loads = [client.compute(LOAD, params=_sample(n)) for n in (1, 2)]
    total = _total(client)
    total.push(part=[load.ref('value') for load in loads])

    (pushed,) = [e for e in Log.read(tmp_path / 'log') if isinstance(e, Pushed)]
    assert pushed == Pushed(
        accumulator=total.id,
        params={'part': [load.ref('value') for load in loads]},
    )
    assert all(isinstance(ref, OutputRef) for ref in pushed.params['part'])
    assert client.output(total, 'value') == Amount(value=3.0)


def test_a_reference_is_logged_in_its_json_form_and_read_back(tmp_path: Path) -> None:
    sample = DatasetRef(dataset='uuid:run-1')
    part = OutputRef(record='r1', output='values', key='0')
    events: list[Event] = [
        _submitted('r2', Request(LOAD, params={'sample': {'file': sample}})),
        Pushed(
            accumulator='a1',
            params={'part': [part]},
        ),
    ]
    _write(tmp_path / 'log', *events)

    submitted, pushed = (tmp_path / 'log').read_text().splitlines()
    assert '"sample":{"file":{"@ref":"dataset","dataset":"uuid:run-1"}}' in submitted
    assert '{"@ref":"output","record":"r1","output":"values","key":"0"}' in pushed
    assert Log.read(tmp_path / 'log') == events


def test_records_pending_in_the_log_are_cancelled_at_a_restart(
    tmp_path: Path,
    start: Callable[[Path], Client],
    restart: Callable[[Path], Client],
    loading: threading.Event,
) -> None:
    first = start(tmp_path / 'log')
    done = first.compute(LOAD, params=_sample(1))
    loading.clear()
    pending = first.submit(LOAD, params=_sample(2))
    total = _total(first)
    total.push(part=[done.ref('value')])
    total.push(part=[pending.ref('value')])  # waits for ``pending``
    read = first.submit(TOTAL, params=_read(total))
    state = _record(first, read.request.params['part'][0].record)

    again = restart(tmp_path / 'log')
    loading.set()

    records = [pending, state, read]
    assert again.status(records) == [Status.CANCELLED] * 3  # no client keeps them
    assert again.failure(records) == ['the backend restarted'] * 3
    with pytest.raises(SubmitError, match=f'record {pending.id} cancelled'):
        again.submit(TOTAL, params={'part': [pending.ref('value')]})
    with pytest.raises(SubmitError, match='is not kept by this client'):
        again.submit(TOTAL, params=_read(total))  # the accumulator did not survive


def test_outputs_are_not_in_the_log(
    tmp_path: Path,
    start: Callable[[Path], Client],
    restart: Callable[[Path], Client],
) -> None:
    load = start(tmp_path / 'log').compute(LOAD, params=_sample(1))

    again = restart(tmp_path / 'log')

    assert again.wait(load) == Status.COMPLETED
    with pytest.raises(NotReadable, match='not kept by this client'):
        again.output(load, 'value')


def test_a_record_whose_output_is_not_kept_is_refused_at_the_push(
    tmp_path: Path,
    start: Callable[[Path], Client],
    restart: Callable[[Path], Client],
) -> None:
    load = start(tmp_path / 'log').compute(LOAD, params=_sample(1))

    again = restart(tmp_path / 'log')

    total = _total(again)
    with pytest.raises(SubmitError, match='not kept by this client'):
        total.push(part=[load.ref('value')])


def test_the_log_holds_submissions_finished_records_accumulators_and_pushes_only(
    tmp_path: Path, start: Callable[[Path], Client]
) -> None:
    client = start(tmp_path / 'log')
    client.stage(Template(LOAD, params=_sample(1), blanks=('window',)))
    total = _total(client)
    load = client.compute(LOAD, params=_sample(1))
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
    load = client.compute(LOAD, params=_sample(1))
    written = len(Log.read(tmp_path / 'log'))

    with pytest.raises(SubmitError, match='not a parameter'):
        client.submit(LOAD, params={'bins': 2, **_sample(1)})
    with pytest.raises(SubmitError, match='blanks of an accumulator'):
        client.accumulator(Template(TOTAL))
    with pytest.raises(SubmitError, match='makes no accumulators'):
        client.accumulator(Template(LOAD, blanks=('sample',)))
    total = _total(client)
    total.push(part=[load.ref('value')])
    written += 2  # the accumulator opened, and the push
    other = AccumulatorRef(accumulator=total.id, output='other')
    with pytest.raises(SubmitError, match='has no output'):
        client.submit(TOTAL, params={'part': [other]})
    with pytest.raises(SubmitError, match='not a blank table'):
        total.push(other=[load.ref('value')])
    with pytest.raises(SubmitError, match='gives no rows'):
        total.push(part=[])
    with pytest.raises(SubmitError, match=r'part\[1\]: repeats part\[0\]'):
        total.push(part=[load.ref('value')])

    assert len(Log.read(tmp_path / 'log')) == written


def test_a_write_that_fails_leaves_the_log_as_it_was(
    tmp_path: Path, datasets: FakeDatasets
) -> None:
    resource = pytest.importorskip('resource')
    path = tmp_path / 'log'
    request = Request(
        LOAD, params={'sample': {'file': datasets.resolve(dataset(run=1))}}
    )
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
    request = Request(
        LOAD, params={'sample': {'file': datasets.resolve(dataset(run=1))}}
    )
    _write(path, _submitted('load', request))
    with path.open('ab') as file:
        file.write(b'{"kind":"finish')  # the backend stopped while writing

    _write(path, Finished(record='load', status=Status.COMPLETED))

    assert [type(e) for e in Log.read(path)] == [Submitted, Finished]


def test_a_line_that_is_not_an_event_is_refused(
    tmp_path: Path, start: Callable[[Path], Client]
) -> None:
    start(tmp_path / 'log').compute(LOAD, params=_sample(1))
    lines = (tmp_path / 'log').read_bytes().splitlines(keepends=True)
    (tmp_path / 'log').write_bytes(b''.join([lines[0], b'{}\n', *lines[1:]]))

    with pytest.raises(ValueError, match='kind'):
        Log.read(tmp_path / 'log')


SPEC = {'name': 'load', 'version': 1}


@pytest.mark.parametrize(
    'event',
    [
        {
            'kind': 'submitted',
            'time': '2026-10-01T00:00:00Z',
            'proposal': 'p1',
            'submitter': 'anna',
            'records': [
                {
                    'id': 'r1',
                    'outputs': ['value'],
                    'request': {
                        'spec': SPEC,
                        'params': {},
                        'inputs': {'sample': {'dataset': 'run:1'}},
                    },
                }
            ],
        },
        {
            'kind': 'opened',
            'accumulator': 'a1',
            'proposal': 'p1',
            'template': {'spec': SPEC, 'params': {}, 'blanks': []},
            'params': {},
            'inputs': {'sample': {'dataset': 'run:1'}},
        },
        {
            'kind': 'opened',
            'accumulator': 'a1',
            'proposal': 'p1',
            'template': {'spec': SPEC, 'params': {}, 'inputs': {}, 'blanks': []},
            'params': {},
        },
        {
            'kind': 'pushed',
            'accumulator': 'a1',
            'table': 'part',
            'params': {},
            'inputs': {'part': [{'kind': 'output', 'record': 'r', 'output': 'o'}]},
        },
    ],
)
def test_a_line_with_a_field_an_event_lacks_is_refused_naming_the_line(
    tmp_path: Path, event: dict[str, Any]
) -> None:
    path = tmp_path / 'log'
    finished = Finished(record='r0', status=Status.COMPLETED).model_dump_json()
    path.write_text(f'{finished}\n{json.dumps(event)}\n')

    for read in (Log.read, Log):
        with pytest.raises(
            ValueError, match=r'log, line 2: not an event of a log of this format'
        ):
            read(path)
