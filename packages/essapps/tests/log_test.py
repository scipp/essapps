# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The backend's log: what it holds, and a backend started from it."""

import operator
import shutil
import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from ess.reduce.spec import Array, NexusFile, OutputRef, WorkflowSpec
from pydantic import BaseModel

from ess.apps import (
    AccumulatorSpec,
    Backend,
    Client,
    Record,
    Request,
    SpecId,
    Status,
    SubmitError,
    combine,
    dataset,
)
from ess.apps.log import (
    AccumulatorOpened,
    Event,
    Finished,
    Log,
    NewRecord,
    Pushed,
    SessionOpened,
    StageOpened,
    Submitted,
)
from ess.apps.records import Snapshot
from ess.apps.testing import FakeDatasets


class LoadParams(BaseModel):
    run: NexusFile
    window: tuple[float, float] = (0.0, 1.0)


class Value(BaseModel):
    value: Array()  # type: ignore[valid-type]


LOAD = WorkflowSpec(
    name='load',
    version=1,
    title='load',
    description='load',
    params=LoadParams,
    outputs=Value,
)
TOTAL = AccumulatorSpec(name='total', version=1, element=Value)


@pytest.fixture
def datasets() -> FakeDatasets:
    datasets = FakeDatasets(proposal='p1')
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

    def load(run: float, window: tuple[float, float]) -> dict[str, float]:
        windows.append(window)
        loading.wait(timeout=5)
        return {'value': run * (window[1] - window[0])}

    def start(path: Path) -> Client:
        bind = {LOAD: load, TOTAL: combine(operator.add)}
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
    log = Log(path)
    for event in events:
        log.append(event)


def _submitted(
    record_id: str, submitted: Request | Snapshot, **fields: Any
) -> Submitted:
    return Submitted(
        time=datetime(2026, 9, 30, tzinfo=UTC),
        proposal='p1',
        submitter='anna',
        records=(
            NewRecord(id=record_id, submitted=submitted, outputs=('value',), **fields),
        ),
    )


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
        str(n): first.compute(
            LOAD, {'run': dataset(run=n)}, label='loads', member=str(n)
        )
        for n in (1, 2, 3)
    }
    with first.session() as session:
        total = session.accumulator(TOTAL)
        for load in loads.values():
            total.push(load)
        snapshot = first.compute(total, label='total')

    again = restart(tmp_path / 'log')

    assert again.records() == first.records()
    assert again.members('loads') == loads
    assert again.latest('total').request.params == {
        'value': [load.ref('value') for load in loads.values()]
    }
    assert again.latest('total').request == snapshot.request


def test_values_are_the_same_after_a_restart_and_typed_for_the_binding(
    tmp_path: Path,
    start: Callable[[Path], Client],
    restart: Callable[[Path], Client],
    windows: list[Any],
) -> None:
    first = start(tmp_path / 'log')
    load = first.compute(LOAD, {'run': dataset(run=3), 'window': (1.0, 2.0)})

    again = restart(tmp_path / 'log')
    rerun = again.compute(LOAD, load.request.params)

    assert again.records()[0] == load
    assert load.request.params['window'] == [1.0, 2.0]  # as JSON holds it
    assert windows == [(1.0, 2.0), (1.0, 2.0)]  # as the params model declares it
    assert again.output(rerun, 'value') == 3.0


def test_a_snapshot_is_logged_as_its_accumulator_and_a_count_then_finished(
    tmp_path: Path, start: Callable[[Path], Client]
) -> None:
    client = start(tmp_path / 'log')
    with client.session() as session:
        total = session.accumulator(TOTAL)
        for n in (1, 2, 3):
            total.push(client.compute(LOAD, {'run': dataset(run=n)}))
        snapshot = client.submit(total)

    *_, logged, finished, _ = Log(tmp_path / 'log')  # the last is the session's end
    assert isinstance(logged, Submitted)
    assert logged.records[0].submitted == Snapshot(
        spec=snapshot.spec, accumulator=total.id, upto=3
    )
    assert finished == Finished(record=snapshot.id, status=Status.COMPLETED)
    assert client.output(snapshot, 'value') == 6.0


def test_a_record_pending_in_the_log_runs_after_a_restart(
    tmp_path: Path,
    start: Callable[[Path], Client],
    restart: Callable[[Path], Client],
    loading: threading.Event,
) -> None:
    loading.clear()
    pending = start(tmp_path / 'log').submit(LOAD, {'run': dataset(run=2)})

    again = restart(tmp_path / 'log')
    loading.set()
    done = again.wait(pending)

    assert done.status == Status.COMPLETED
    assert again.output(done, 'value') == 2.0


def test_a_record_pending_through_a_stage_runs_without_it_after_a_restart(
    tmp_path: Path, start: Callable[[Path], Client], datasets: FakeDatasets
) -> None:
    run = datasets.resolve(dataset(run=2))
    _write(
        tmp_path / 'log',
        SessionOpened(session='s', proposal='p1'),
        StageOpened(stage='t', session='s', spec=SpecId.of(LOAD), blanks=('window',)),
        _submitted('x', Request(LOAD, {'run': run, 'window': [0.0, 1.0]}), stage='t'),
    )

    client = start(tmp_path / 'log')
    done = client.wait(_record(client, 'x'))

    assert done.status == Status.COMPLETED
    assert client.output(done, 'value') == 2.0


def test_a_restart_closes_the_sessions_left_open(
    tmp_path: Path,
    start: Callable[[Path], Client],
    restart: Callable[[Path], Client],
) -> None:
    first = start(tmp_path / 'log')
    load = first.compute(LOAD, {'run': dataset(run=1)})
    total = first.session().accumulator(TOTAL)
    total.push(load)

    again = restart(tmp_path / 'log')

    with pytest.raises(SubmitError, match='ended'):
        again.submit(total)


def test_outputs_are_not_in_the_log(
    tmp_path: Path,
    start: Callable[[Path], Client],
    restart: Callable[[Path], Client],
) -> None:
    load = start(tmp_path / 'log').compute(LOAD, {'run': dataset(run=1)})

    again = restart(tmp_path / 'log')

    assert again.wait(load).status == Status.COMPLETED
    with pytest.raises(LookupError, match='no output'):
        again.output(load, 'value')


def test_a_record_whose_output_is_not_kept_is_refused_at_the_push(
    tmp_path: Path,
    start: Callable[[Path], Client],
    restart: Callable[[Path], Client],
) -> None:
    load = start(tmp_path / 'log').compute(LOAD, {'run': dataset(run=1)})

    again = restart(tmp_path / 'log')

    with again.session() as session:
        total = session.accumulator(TOTAL)
        with pytest.raises(SubmitError, match='no output'):
            total.push(load)


def test_a_snapshot_left_pending_by_a_crash_fails_after_a_restart(
    tmp_path: Path, start: Callable[[Path], Client], datasets: FakeDatasets
) -> None:
    run = datasets.resolve(dataset(run=1))
    _write(
        tmp_path / 'log',
        SessionOpened(session='s', proposal='p1'),
        AccumulatorOpened(accumulator='a', session='s', spec=SpecId.of(TOTAL)),
        _submitted('load', Request(LOAD, {'run': run, 'window': [0.0, 1.0]})),
        Finished(record='load', status=Status.COMPLETED),
        Pushed(
            accumulator='a', element={'value': OutputRef(record='load', output='value')}
        ),
        _submitted(
            'snapshot', Snapshot(spec=SpecId.of(TOTAL), accumulator='a', upto=1)
        ),
    )

    client = start(tmp_path / 'log')
    snapshot = client.wait(_record(client, 'snapshot'))

    assert snapshot.status == Status.FAILED
    assert snapshot.failure is not None
    assert 'no output' in snapshot.failure.message


def test_a_refused_call_writes_nothing_to_the_log(
    tmp_path: Path, datasets: FakeDatasets, start: Callable[[Path], Client]
) -> None:
    log = Log(tmp_path / 'log')
    bind = {LOAD: lambda run, window: {'value': run}, TOTAL: combine(operator.add)}
    backend = Backend(datasets, bind, log=log)
    ended, other = backend.open_session('p1'), backend.open_session('p2')
    backend.close_session(ended)
    written = len(log)

    with pytest.raises(SubmitError, match='session'):
        backend.open_stage(ended, SpecId.of(LOAD), ('window',), 'p1')
    with pytest.raises(SubmitError, match='session'):
        backend.open_accumulator(ended, SpecId.of(TOTAL), 'p1')
    with pytest.raises(SubmitError, match='session'):
        backend.open_stage(other, SpecId.of(LOAD), ('window',), 'p1')
    backend.close()

    assert len(log) == written
    start(tmp_path / 'log')  # the log can be read again


def test_a_last_line_cut_short_is_dropped(
    tmp_path: Path,
    start: Callable[[Path], Client],
    restart: Callable[[Path], Client],
) -> None:
    first = start(tmp_path / 'log')
    load = first.compute(LOAD, {'run': dataset(run=1)})
    with (tmp_path / 'log').open('ab') as file:
        file.write(b'{"kind":"finish')  # the backend stopped while writing

    again = start(tmp_path / 'log')
    later = again.compute(LOAD, {'run': dataset(run=2)})

    assert restart(tmp_path / 'log').records() == [load, later]


def test_a_line_that_is_not_an_event_is_refused(
    tmp_path: Path, start: Callable[[Path], Client]
) -> None:
    start(tmp_path / 'log').compute(LOAD, {'run': dataset(run=1)})
    lines = (tmp_path / 'log').read_bytes().splitlines(keepends=True)
    (tmp_path / 'log').write_bytes(b''.join([lines[0], b'{}\n', *lines[1:]]))

    with pytest.raises(ValueError, match='kind'):
        Log(tmp_path / 'log')
