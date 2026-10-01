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
    Template,
    combine,
    dataset,
)
from ess.apps.log import Event, Finished, Log, NewRecord, Pushed, Submitted
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
    with Log(path) as log:
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
        snapshot = first.compute(total)

    again = restart(tmp_path / 'log')

    assert again.records() == first.records()
    assert again.members('loads') == loads
    assert again.records(spec=TOTAL) == [snapshot]
    assert again.provenance(snapshot) == first.provenance(snapshot)
    assert again.provenance(snapshot).records() == list(loads.values())


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

    *_, logged, finished = Log.read(tmp_path / 'log')
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

    assert again.wait(pending) == Status.COMPLETED
    assert again.output(pending, 'value') == 2.0


def test_holders_do_not_survive_a_restart(
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

    assert again.wait(load) == Status.COMPLETED
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
    snapshot = _record(client, 'snapshot')

    assert client.wait(snapshot) == Status.FAILED
    assert 'accumulator ended' in client.failure(snapshot)


def test_provenance_refuses_a_snapshot_whose_elements_the_log_lacks(
    tmp_path: Path, start: Callable[[Path], Client], datasets: FakeDatasets
) -> None:
    run = datasets.resolve(dataset(run=1))
    _write(
        tmp_path / 'log',
        _submitted('load', Request(LOAD, {'run': run, 'window': [0.0, 1.0]})),
        Finished(record='load', status=Status.COMPLETED),
        Pushed(
            accumulator='a', element={'value': OutputRef(record='load', output='value')}
        ),
        _submitted(
            'snapshot', Snapshot(spec=SpecId.of(TOTAL), accumulator='a', upto=2)
        ),
        Finished(record='snapshot', status=Status.COMPLETED),
    )

    client = start(tmp_path / 'log')

    with pytest.raises(LookupError, match='lacks elements'):
        client.provenance(_record(client, 'snapshot'))


def test_the_log_holds_submissions_finished_records_and_pushes_only(
    tmp_path: Path, start: Callable[[Path], Client]
) -> None:
    client = start(tmp_path / 'log')
    with client.session() as session:
        session.stage(Template(LOAD, blanks=('window',)))
        total = session.accumulator(TOTAL)
        total.push(client.compute(LOAD, {'run': dataset(run=1)}))

    assert [type(e) for e in Log.read(tmp_path / 'log')] == [
        Submitted,
        Finished,
        Pushed,
    ]


def test_a_refused_call_writes_nothing_to_the_log(
    tmp_path: Path, start: Callable[[Path], Client]
) -> None:
    client = start(tmp_path / 'log')
    load = client.compute(LOAD, {'run': dataset(run=1)})
    written = len(Log.read(tmp_path / 'log'))

    with pytest.raises(SubmitError, match='not parameters'):
        client.submit(LOAD, {'run': dataset(run=1), 'bins': 2})
    with client.session() as session:
        total = session.accumulator(TOTAL)
        with pytest.raises(SubmitError, match='nothing has been pushed'):
            client.submit(total)
        with pytest.raises(SubmitError, match='fields'):
            total.push({'other': load.ref('value')})

    assert len(Log.read(tmp_path / 'log')) == written


def test_a_write_that_fails_leaves_the_log_as_it_was(
    tmp_path: Path, datasets: FakeDatasets
) -> None:
    resource = pytest.importorskip('resource')
    path = tmp_path / 'log'
    run = datasets.resolve(dataset(run=1))
    with Log(path) as log:
        log.append(_submitted('load', Request(LOAD, {'run': run})))
        soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
        # A file limit that a second event exceeds, as a full disk would.
        resource.setrlimit(resource.RLIMIT_FSIZE, (path.stat().st_size + 10, hard))
        try:
            with pytest.raises(OSError, match='too large'):
                log.append(_submitted('again', Request(LOAD, {'run': run})))
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
    run = datasets.resolve(dataset(run=1))
    _write(path, _submitted('load', Request(LOAD, {'run': run})))
    with path.open('ab') as file:
        file.write(b'{"kind":"finish')  # the backend stopped while writing

    _write(path, Finished(record='load', status=Status.COMPLETED))

    assert [type(e) for e in Log.read(path)] == [Submitted, Finished]


def test_a_line_that_is_not_an_event_is_refused(
    tmp_path: Path, start: Callable[[Path], Client]
) -> None:
    start(tmp_path / 'log').compute(LOAD, {'run': dataset(run=1)})
    lines = (tmp_path / 'log').read_bytes().splitlines(keepends=True)
    (tmp_path / 'log').write_bytes(b''.join([lines[0], b'{}\n', *lines[1:]]))

    with pytest.raises(ValueError, match='kind'):
        Log.read(tmp_path / 'log')
