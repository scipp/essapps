# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Holders in a session: what they refuse, and the records they make."""

import operator
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any

import pytest
from ess.reduce.spec import Array, NexusFile, OpaqueFile, WorkflowSpec
from pydantic import BaseModel

from ess.apps import (
    AccumulatorSpec,
    Backend,
    Client,
    Request,
    Snapshot,
    SpecId,
    Status,
    SubmitError,
    Template,
    combine,
    dataset,
)
from ess.apps.backend import Entry
from ess.apps.bindings import Function
from ess.apps.log import Log
from ess.apps.testing import FakeDatasets


class RunParams(BaseModel):
    run: NexusFile


class Parts(BaseModel):
    value: Array()  # type: ignore[valid-type]


class LoadOutputs(BaseModel):
    value: Array()  # type: ignore[valid-type]
    extra: Array()  # type: ignore[valid-type]


class ShiftParams(BaseModel):
    value: Array()  # type: ignore[valid-type]
    offset: float = 0.0


class ScaleParams(BaseModel):
    run: NexusFile
    factor: float = 1.0


def _spec(name: str, params: type[BaseModel], outputs: type[BaseModel]) -> WorkflowSpec:
    return WorkflowSpec(
        name=name,
        version=1,
        title=name,
        description=name,
        params=params,
        outputs=outputs,
    )


LOAD = _spec('load', RunParams, LoadOutputs)
SHIFT = _spec('shift', ShiftParams, Parts)
SCALE = _spec('scale', ScaleParams, Parts)
TOTAL = AccumulatorSpec(name='total', version=1, element=Parts)
PAIRS = AccumulatorSpec(name='pairs', version=1, element=LoadOutputs)
DIGITS = AccumulatorSpec(name='digits', version=1, element=Parts)


def pairs(value: list[float], extra: list[float]) -> dict[str, float]:
    """PAIRS as a plain function over lists."""
    return {'value': sum(value), 'extra': sum(extra)}


def append_digit(number: float, digit: float) -> float:
    """An order-sensitive combination, slow enough for pushes to overlap."""
    time.sleep(0.001)
    return number * 10 + digit


class Staging:
    """A binding that records the blanks of each time it is staged."""

    def __init__(self, compute: Callable[[dict[str, Any]], float]) -> None:
        self.staged: list[tuple[str, ...]] = []
        self.fail_next = False
        self._compute = compute

    def stage(self, fixed: Mapping[str, Any], blanks: Sequence[str]) -> Function:
        if self.fail_next:
            self.fail_next = False
            raise OSError('staging failed')
        self.staged.append(tuple(blanks))
        return lambda **values: {'value': self._compute({**fixed, **values})}


@pytest.fixture
def shifting() -> Staging:
    return Staging(lambda p: p['value'] + p['offset'])


@pytest.fixture
def scaling() -> Staging:
    return Staging(lambda p: p['run'] * p['factor'])


@pytest.fixture
def loading() -> threading.Event:
    """Set to let LOAD finish."""
    event = threading.Event()
    event.set()
    return event


@pytest.fixture
def datasets() -> FakeDatasets:
    datasets = FakeDatasets(proposal='p1')
    for n in (1, 2):
        datasets.measure(n, float(n))
    return datasets


@pytest.fixture
def backend(
    datasets: FakeDatasets,
    shifting: Staging,
    scaling: Staging,
    loading: threading.Event,
) -> Iterator[Backend]:
    def load(run: float) -> dict[str, Any]:
        loading.wait(timeout=5)
        return {'value': run, 'extra': -run}

    backend = Backend(
        datasets,
        {
            LOAD: load,
            SHIFT: shifting,
            SCALE: scaling,
            TOTAL: combine(operator.add),
            PAIRS: pairs,
            DIGITS: combine(append_digit),
            FILES_SUM: combine(operator.add),
        },
    )
    yield backend
    backend.close()


@pytest.fixture
def client(backend: Backend) -> Client:
    return Client(backend, proposal='p1', submitter='anna')


def test_calls_through_a_stage_are_staged_once(
    client: Client, shifting: Staging
) -> None:
    load = client.submit(LOAD, {'run': dataset(run=1)})
    with client.session() as session:
        shift = session.stage(
            Template(SHIFT, params={'value': load.ref('value')}, blanks=('offset',))
        )
        shifted = [client.compute(shift, {'offset': x}) for x in (0.5, 1.5)]
    plain = client.compute(SHIFT, {'value': load.ref('value'), 'offset': 1.5})

    assert [client.output(r, 'value') for r in shifted] == [1.5, 2.5]
    assert shifted[1].request == plain.request
    assert shifting.staged == [('offset',), ()]


def test_concurrent_calls_through_a_stage_stage_it_once(
    client: Client, shifting: Staging, loading: threading.Event
) -> None:
    loading.clear()
    load = client.submit(LOAD, {'run': dataset(run=1)})
    with client.session() as session:
        shift = session.stage(
            Template(SHIFT, params={'value': load.ref('value')}, blanks=('offset',))
        )
        pending = [client.submit(shift, {'offset': x}) for x in (1.0, 2.0, 3.0, 4.0)]
        loading.set()
        client.wait(pending)

    assert [client.output(r, 'value') for r in pending] == [2.0, 3.0, 4.0, 5.0]
    assert shifting.staged == [('offset',)]


def test_a_request_through_a_stage_uses_it_after_its_session_ended(
    client: Client, shifting: Staging, loading: threading.Event
) -> None:
    loading.clear()
    load = client.submit(LOAD, {'run': dataset(run=2)})
    with client.session() as session:
        shift = session.stage(
            Template(SHIFT, params={'value': load.ref('value')}, blanks=('offset',))
        )
        pending = client.submit(shift, {'offset': 1.0})
    loading.set()

    assert client.wait(pending) is Status.COMPLETED
    assert client.output(pending, 'value') == 3.0
    assert shifting.staged == [('offset',)]


def test_a_stage_is_staged_again_when_its_dataset_name_resolves_elsewhere(
    client: Client, scaling: Staging, datasets: FakeDatasets
) -> None:
    with client.session() as session:
        scale = session.stage(
            Template(SCALE, params={'run': dataset(run=1)}, blanks=('factor',))
        )
        before = client.compute(scale, {'factor': 2.0})
        datasets.correct(datasets.resolve(dataset(run=1)), run=99)
        datasets.measure(1, 5.0, pid='again')
        after = client.compute(scale, {'factor': 2.0})

    assert [client.output(r, 'value') for r in (before, after)] == [2.0, 10.0]
    assert scaling.staged == [('factor',), ('factor',)]


def test_a_stage_that_failed_to_stage_is_staged_on_the_next_call(
    client: Client, scaling: Staging
) -> None:
    scaling.fail_next = True
    with client.session() as session:
        scale = session.stage(
            Template(SCALE, params={'run': dataset(run=2)}, blanks=('factor',))
        )
        failed = client.compute(scale, {'factor': 2.0})
        scaled = client.compute(scale, {'factor': 2.0})

    assert client.status(failed) is Status.FAILED
    assert 'staging failed' in client.failure(failed)
    assert client.output(scaled, 'value') == 4.0


def test_a_request_through_a_stage_of_another_spec_is_refused(
    client: Client, backend: Backend
) -> None:
    load = client.submit(LOAD, {'run': dataset(run=1)})
    request = Request(SHIFT, {'value': load.ref('value')})
    with client.session() as session:
        scale = session.stage(Template(SCALE, blanks=('factor',)))
        with pytest.raises(SubmitError, match='the stage holds scale'):
            backend.submit(
                [Entry(request, stage=scale.id)], proposal='p1', submitter='x'
            )


def test_a_stage_of_another_proposal_is_refused(
    client: Client, backend: Backend, datasets: FakeDatasets
) -> None:
    datasets.measure(3, 3.0)
    datasets.correct(datasets.resolve(dataset(run=3)), proposal='p2')
    request = Request(SCALE, {'run': dataset(run=3), 'factor': 2.0})
    with client.session() as session:
        scale = session.stage(Template(SCALE, blanks=('factor',)))
        with pytest.raises(SubmitError, match='ended or is unknown'):
            backend.submit(
                [Entry(request, stage=scale.id)], proposal='p2', submitter='x'
            )


def test_a_stage_refuses_blanks_that_are_not_parameters(client: Client) -> None:
    with client.session() as session, pytest.raises(SubmitError, match='speed'):
        session.stage(Template(SCALE, blanks=('speed',)))


def test_ending_a_session_twice_is_harmless(client: Client) -> None:
    with client.session() as session:
        pass
    with session:
        pass

    assert not session.open


def test_a_holder_of_an_ended_session_refuses_calls(client: Client) -> None:
    with client.session() as session:
        stage = session.stage(Template(LOAD, blanks=('run',)))

    with pytest.raises(RuntimeError, match='session'):
        client.submit(stage, {'run': {'dataset': 'run:1'}})
    with pytest.raises(RuntimeError, match='session'):
        session.stage(Template(LOAD, blanks=('run',)))


def test_an_accumulator_pushes_only_the_element_fields(client: Client) -> None:
    loads = [client.submit(LOAD, {'run': {'dataset': f'run:{n}'}}) for n in (1, 2)]
    client.wait(loads)
    with client.session() as session:
        total = session.accumulator(TOTAL)
        for load in loads:
            total.push(load)  # 'extra' is not pushed
        combined = client.compute(total)

    assert combined.submitted == Snapshot(
        spec=SpecId.of(TOTAL), accumulator=total.id, upto=2
    )
    assert client.output(combined, 'value') == 3.0


class Files(BaseModel):
    value: OpaqueFile


FILES_SUM = AccumulatorSpec(name='files-sum', version=1, element=Files)


def test_an_element_must_fit_the_accumulator(client: Client) -> None:
    load = client.submit(LOAD, {'run': {'dataset': 'run:1'}})

    with pytest.raises(SubmitError, match='does not fit'):
        client.submit(FILES_SUM, {'value': [load.ref('value')]})


def test_an_accumulator_spec_takes_lists_of_equal_length(client: Client) -> None:
    loads = [client.submit(LOAD, {'run': {'dataset': f'run:{n}'}}) for n in (1, 2)]

    with pytest.raises(SubmitError, match='same number of elements'):
        client.submit(
            PAIRS,
            {
                'value': [x.ref('value') for x in loads],
                'extra': [loads[0].ref('extra')],
            },
        )
    with pytest.raises(SubmitError, match='same number of elements'):
        client.submit(PAIRS, {'value': [], 'extra': []})


def test_an_element_that_does_not_fit_is_refused_at_the_push(client: Client) -> None:
    load = client.submit(LOAD, {'run': dataset(run=1)})
    with client.session() as session:
        files = session.accumulator(FILES_SUM)
        with pytest.raises(SubmitError, match='does not fit'):
            files.push({'value': load.ref('value')})


def test_a_snapshot_with_nothing_pushed_is_refused(client: Client) -> None:
    with client.session() as session:
        total = session.accumulator(TOTAL)
        with pytest.raises(SubmitError, match='nothing has been pushed'):
            client.submit(total)


def test_a_snapshot_takes_no_label(client: Client) -> None:
    load = client.compute(LOAD, {'run': dataset(run=1)})
    with client.session() as session:
        total = session.accumulator(TOTAL)
        total.push(load)
        with pytest.raises(TypeError, match='no label'):
            client.submit(total, label='total')


def test_an_accumulator_of_an_ended_session_refuses_snapshots(client: Client) -> None:
    load = client.compute(LOAD, {'run': dataset(run=1)})
    with client.session() as session:
        total = session.accumulator(TOTAL)
        total.push(load)

    with pytest.raises(RuntimeError, match='session'):
        total.push(load)
    with pytest.raises(SubmitError, match='ended'):
        client.submit(total)


def test_a_snapshot_completes_at_submission_over_the_elements_pushed_before_it(
    client: Client,
) -> None:
    loads = [client.submit(LOAD, {'run': dataset(run=n)}) for n in (1, 2, 1)]
    client.wait(loads)
    with client.session() as session:
        total = session.accumulator(TOTAL)
        total.push(loads[0])
        total.push(loads[1])
        first = client.submit(total)
        total.push(loads[2])
        second = client.submit(total)

    assert client.status([first, second]) == [Status.COMPLETED] * 2
    assert [client.output(r, 'value') for r in (first, second)] == [3.0, 4.0]
    assert [r.submitted.upto for r in (first, second)] == [2, 3]
    with pytest.raises(TypeError, match='snapshot'):
        first.request
    assert client.provenance(first).records() == loads[:2]
    assert client.provenance(second).records() == loads


def test_a_push_waits_for_its_records_and_takes_only_completed_ones(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    pending, cancelled = (client.submit(LOAD, {'run': dataset(run=n)}) for n in (1, 2))
    client.cancel(cancelled)
    with client.session() as session:
        total = session.accumulator(TOTAL)
        with pytest.raises(SubmitError, match=f'{cancelled.id} cancelled'):
            total.push(cancelled)
        threading.Timer(0.05, loading.set).start()
        total.push(pending)  # pending when pushed: the push waits for it
        snapshot = client.submit(total)

    assert [r.id for r in client.provenance(snapshot).records()] == [pending.id]
    assert client.output(snapshot, 'value') == 1.0


def test_concurrent_pushes_combine_in_the_order_they_are_logged(
    client: Client, datasets: FakeDatasets
) -> None:
    runs = [datasets.measure(n, float(n)) for n in range(3, 10)]
    loads = [client.submit(LOAD, {'run': run}) for run in runs]
    client.wait(loads)
    with client.session() as session:
        digits = session.accumulator(DIGITS)
        pushes = [threading.Thread(target=digits.push, args=(x,)) for x in loads]
        for push in pushes:
            push.start()
        for push in pushes:
            push.join()
        snapshot = client.compute(digits)
    pushed = client.provenance(snapshot).records()  # in the order they were logged
    plain = client.compute(DIGITS, {'value': [x.ref('value') for x in pushed]})

    assert client.output(snapshot, 'value') == client.output(plain, 'value')


def test_an_accumulator_needs_a_binding_that_accumulates(client: Client) -> None:
    loads = [client.submit(LOAD, {'run': dataset(run=n)}) for n in (1, 2)]
    client.wait(loads)
    with client.session() as session, pytest.raises(SubmitError, match='accumulate'):
        session.accumulator(PAIRS)
    plain = client.compute(
        PAIRS,
        {
            'value': [x.ref('value') for x in loads],
            'extra': [x.ref('extra') for x in loads],
        },
    )

    assert [client.output(plain, name) for name in ('value', 'extra')] == [3.0, -3.0]


class Sum:
    """
    ``combine(operator.add)`` that counts the elements its accumulators combine.

    With ``failing`` set, the next element fails to combine.
    """

    def __init__(self) -> None:
        self.pushed = 0
        self.failing = False
        self._sum = combine(operator.add)
        self.stage = self._sum.stage

    def accumulator(self) -> Any:
        return _Summing(self, self._sum.accumulator())


class _Summing:
    def __init__(self, owner: Sum, held: Any) -> None:
        self._owner = owner
        self._held = held

    def push(self, element: Mapping[str, Any]) -> None:
        if self._owner.failing:
            self._owner.failing = False
            raise ValueError('cannot combine')
        self._owner.pushed += 1
        self._held.push(element)

    @property
    def value(self) -> Mapping[str, Any]:
        return self._held.value


@pytest.fixture
def summing() -> Sum:
    return Sum()


@pytest.fixture
def log() -> Log:
    return Log()


@pytest.fixture
def summed(datasets: FakeDatasets, summing: Sum, log: Log) -> Iterator[Client]:
    """A client of a backend that sums TOTAL with ``summing`` and logs to ``log``."""
    backend = Backend(
        datasets,
        {LOAD: lambda run: {'value': run, 'extra': 0.0}, TOTAL: summing},
        log=log,
    )
    yield Client(backend, proposal='p1', submitter='anna')
    backend.close()


def test_an_accumulator_combines_each_element_once(
    summed: Client, summing: Sum
) -> None:
    client = summed
    with client.session() as session:
        total = session.accumulator(TOTAL)
        snapshots = []
        for n in (1, 2, 1, 2):
            total.push(client.compute(LOAD, {'run': dataset(run=n)}))
            snapshots.append(client.submit(total))

    assert [client.output(r, 'value') for r in snapshots] == [1.0, 3.0, 4.0, 6.0]
    assert summing.pushed == 4


def test_a_push_that_fails_to_combine_is_refused_and_stops_the_accumulator(
    summed: Client, summing: Sum, log: Log
) -> None:
    client = summed
    loads = [client.compute(LOAD, {'run': dataset(run=n)}) for n in (1, 2, 1)]
    with client.session() as session:
        total = session.accumulator(TOTAL)
        total.push(loads[0])
        before = client.submit(total)
        summing.failing = True
        logged = len(log)
        with pytest.raises(SubmitError, match=r'^element 1 failed to combine: cannot'):
            total.push(loads[1])
        with pytest.raises(SubmitError, match='stopped: element 1 failed to combine'):
            total.push(loads[2])
        with pytest.raises(SubmitError, match='stopped: element 1 failed to combine'):
            client.submit(total)
        assert len(log) == logged

    assert client.output(before, 'value') == 1.0
