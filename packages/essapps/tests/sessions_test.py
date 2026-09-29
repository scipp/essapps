# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Holders in a session: what they refuse, and the records they make."""

import operator
import threading
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

import pytest
from ess.reduce.spec import Array, NexusFile, OpaqueFile, WorkflowSpec
from pydantic import BaseModel

from ess.apps import (
    AccumulatorSpec,
    Backend,
    Client,
    Function,
    Status,
    SubmitError,
    Template,
    combine,
)
from ess.apps.testing import FakeDatasets


class RunParams(BaseModel):
    run: NexusFile


class Parts(BaseModel):
    value: Array()  # type: ignore[valid-type]


class LoadOutputs(BaseModel):
    value: Array()  # type: ignore[valid-type]
    extra: Array()  # type: ignore[valid-type]


LOAD = WorkflowSpec(
    name='load',
    version=1,
    title='load',
    description='load',
    params=RunParams,
    outputs=LoadOutputs,
)


class ShiftParams(BaseModel):
    value: Array()  # type: ignore[valid-type]
    offset: float = 0.0


SHIFT = WorkflowSpec(
    name='shift',
    version=1,
    title='shift',
    description='shift',
    params=ShiftParams,
    outputs=Parts,
)
TOTAL = AccumulatorSpec(name='total', version=1, element=Parts)
PAIRS = AccumulatorSpec(name='pairs', version=1, element=LoadOutputs)


class Staging:
    """The binding of SHIFT: records the blanks of each time it is staged."""

    def __init__(self) -> None:
        self.staged: list[tuple[str, ...]] = []

    def stage(self, fixed: Mapping[str, Any], blanks: Sequence[str]) -> Function:
        self.staged.append(tuple(blanks))

        def call(**values: Any) -> dict[str, Any]:
            params = {**fixed, **values}
            return {'value': params['value'] + params['offset']}

        return call


@pytest.fixture
def staging() -> Staging:
    return Staging()


@pytest.fixture
def loading() -> threading.Event:
    """Set to let LOAD finish."""
    event = threading.Event()
    event.set()
    return event


@pytest.fixture
def client(staging: Staging, loading: threading.Event) -> Iterator[Client]:
    datasets = FakeDatasets(proposal='p1')
    for n in (1, 2):
        datasets.measure(n, float(n))

    def load(run: float) -> dict[str, Any]:
        loading.wait(timeout=5)
        return {'value': run, 'extra': -run}

    backend = Backend(
        datasets,
        {
            LOAD: load,
            SHIFT: staging,
            TOTAL: combine(operator.add),
            PAIRS: combine(operator.add),
            FILES_SUM: combine(operator.add),
        },
    )
    yield Client(backend, proposal='p1', submitter='anna')
    backend.close()


def test_calls_through_a_stage_are_staged_once(
    client: Client, staging: Staging
) -> None:
    load = client.submit(LOAD, {'run': {'dataset': 'run:1'}})
    with client.session() as session:
        shift = session.stage(
            Template(SHIFT, params={'value': load.ref('value')}, blanks=('offset',))
        )
        shifted = [client.compute(shift, {'offset': x}) for x in (0.5, 1.5)]
    plain = client.compute(SHIFT, {'value': load.ref('value'), 'offset': 1.5})

    assert [client.output(r, 'value') for r in shifted] == [1.5, 2.5]
    assert shifted[1].request == plain.request
    assert staging.staged == [('offset',), ()]


def test_a_request_through_a_stage_runs_in_full_once_its_session_ended(
    client: Client, staging: Staging, loading: threading.Event
) -> None:
    loading.clear()
    load = client.submit(LOAD, {'run': {'dataset': 'run:2'}})
    with client.session() as session:
        shift = session.stage(
            Template(SHIFT, params={'value': load.ref('value')}, blanks=('offset',))
        )
        pending = client.submit(shift, {'offset': 1.0})
    loading.set()
    shifted = client.wait(pending)

    assert shifted.status is Status.COMPLETED
    assert client.output(shifted, 'value') == 3.0
    assert staging.staged == [()]


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
