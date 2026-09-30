# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Holders in a session: what they refuse, and the records they make."""

import operator
import threading
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
    Status,
    SubmitError,
    Template,
    combine,
    dataset,
)
from ess.apps.backend import Entry
from ess.apps.bindings import Function
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
            PAIRS: combine(operator.add),
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
        shifted = client.wait(pending)

    assert [client.output(r, 'value') for r in shifted] == [2.0, 3.0, 4.0, 5.0]
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
    shifted = client.wait(pending)

    assert shifted.status is Status.COMPLETED
    assert client.output(shifted, 'value') == 3.0
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

    assert failed.status is Status.FAILED
    assert 'staging failed' in failed.failure.message
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
    with client.session() as session:
        total = session.accumulator(TOTAL)
        for load in loads:
            total.push(load)  # pending; 'extra' is not pushed
        combined = client.compute(total)

    assert combined.request.params == {'value': [load.ref('value') for load in loads]}
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


def test_reading_an_accumulator_with_nothing_pushed_is_refused(client: Client) -> None:
    with client.session() as session:
        total = session.accumulator(TOTAL)
        with pytest.raises(SubmitError, match='nothing has been pushed'):
            client.submit(total)


def test_an_accumulator_of_an_ended_session_refuses_reads(client: Client) -> None:
    load = client.submit(LOAD, {'run': dataset(run=1)})
    with client.session() as session:
        total = session.accumulator(TOTAL)
        total.push(load)

    with pytest.raises(RuntimeError, match='session'):
        total.push(load)
    with pytest.raises(SubmitError, match='ended'):
        client.submit(total)


def test_a_read_combines_the_elements_pushed_before_it(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    loads = [client.submit(LOAD, {'run': dataset(run=n)}) for n in (1, 2, 1)]
    with client.session() as session:
        total = session.accumulator(TOTAL)
        total.push(loads[0])
        total.push(loads[1])
        first = client.submit(total)  # pending until both loads have run
        total.push(loads[2])
        second = client.submit(total)
        loading.set()
        first, second = client.wait([first, second])

    assert [client.output(r, 'value') for r in (first, second)] == [3.0, 4.0]
    assert first.request.params == {'value': [x.ref('value') for x in loads[:2]]}
    assert second.request.params == {'value': [x.ref('value') for x in loads]}


def test_a_read_over_an_element_that_did_not_complete_fails(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    loads = [client.submit(LOAD, {'run': dataset(run=n)}) for n in (1, 2)]
    with client.session() as session:
        total = session.accumulator(TOTAL)
        for load in loads:
            total.push(load)
        read = client.submit(total)
        client.cancel(loads[1])
        loading.set()
        (read,) = client.wait([read])

        assert read.status == Status.FAILED
        assert read.failure is not None
        assert f'{loads[1].id} cancelled' in read.failure.message
        with pytest.raises(SubmitError, match='element 1'):
            client.submit(total)


class Sum:
    """
    ``combine(operator.add)`` that counts the elements its accumulators combine.

    With ``failing`` set to n, the next n elements combine and the one after fails.
    """

    def __init__(self) -> None:
        self.pushed = 0
        self.failing: int | None = None
        self._sum = combine(operator.add)
        self.stage = self._sum.stage

    def accumulator(self) -> Any:
        return _Summing(self, self._sum.accumulator())


class _Summing:
    def __init__(self, owner: Sum, held: Any) -> None:
        self._owner = owner
        self._held = held

    def push(self, element: Mapping[str, Any]) -> None:
        if self._owner.failing == 0:
            self._owner.failing = None
            raise ValueError('cannot combine')
        if self._owner.failing is not None:
            self._owner.failing -= 1
        self._owner.pushed += 1
        self._held.push(element)

    @property
    def value(self) -> Mapping[str, Any]:
        return self._held.value


@pytest.fixture
def summing(datasets: FakeDatasets) -> Iterator[tuple[Client, Sum]]:
    """A client whose backend sums TOTAL with a :class:`Sum`."""
    total = Sum()
    backend = Backend(
        datasets, {LOAD: lambda run: {'value': run, 'extra': 0.0}, TOTAL: total}
    )
    yield Client(backend, proposal='p1', submitter='anna'), total
    backend.close()


def test_an_accumulator_combines_each_element_once(
    summing: tuple[Client, Sum],
) -> None:
    client, total_binding = summing
    with client.session() as session:
        total = session.accumulator(TOTAL)
        reads = []
        for n in (1, 2, 1, 2):
            total.push(client.submit(LOAD, {'run': dataset(run=n)}))
            reads.append(client.submit(total))
        reads = client.wait(reads)

    assert [client.output(r, 'value') for r in reads] == [1.0, 3.0, 4.0, 6.0]
    assert total_binding.pushed == 4


def test_a_read_after_one_that_failed_to_combine_combines_every_element(
    summing: tuple[Client, Sum],
) -> None:
    client, total_binding = summing
    with client.session() as session:
        total = session.accumulator(TOTAL)
        total.push(client.submit(LOAD, {'run': dataset(run=1)}))
        client.compute(total)
        for n in (2, 1):
            total.push(client.submit(LOAD, {'run': dataset(run=n)}))
        total_binding.failing = 1  # run 2 combines, the second run 1 fails
        failed = client.compute(total)
        total.push(client.submit(LOAD, {'run': dataset(run=2)}))
        again = client.compute(total)

    assert failed.status == Status.FAILED
    assert client.output(again, 'value') == 6.0  # 1 + 2 + 1 + 2


def test_a_cancelled_read_leaves_later_reads_correct(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    with client.session() as session:
        total = session.accumulator(TOTAL)
        total.push(client.submit(LOAD, {'run': dataset(run=1)}))
        cancelled = client.submit(total)
        client.cancel(cancelled)
        total.push(client.submit(LOAD, {'run': dataset(run=2)}))
        later = client.submit(total)
        loading.set()
        cancelled, later = client.wait([cancelled, later])

    assert cancelled.status == Status.CANCELLED
    assert client.output(later, 'value') == 3.0


def test_a_read_pending_when_its_session_ends_completes(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    with client.session() as session:
        total = session.accumulator(TOTAL)
        for n in (1, 2):
            total.push(client.submit(LOAD, {'run': dataset(run=n)}))
        read = client.submit(total)
    loading.set()

    assert client.output(client.wait(read), 'value') == 3.0
