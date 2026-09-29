# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Holders in a session: what they refuse, and the records they make."""

import operator
from collections.abc import Iterator

import pytest
from ess.reduce.spec import Array, NexusFile, WorkflowSpec
from pydantic import BaseModel

from ess.apps import Backend, Client, Template, accumulator_spec, combine
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
TOTAL = accumulator_spec('total', 1, Parts)


@pytest.fixture
def client() -> Iterator[Client]:
    datasets = FakeDatasets(proposal='p1')
    for n in (1, 2):
        datasets.measure(n, float(n))
    backend = Backend(
        datasets,
        {LOAD: lambda run: {'value': run, 'extra': -run}, TOTAL: combine(operator.add)},
    )
    yield Client(backend, proposal='p1', submitter='anna')
    backend.close()


def test_a_holder_of_an_ended_session_refuses_calls(client: Client) -> None:
    with client.session() as session:
        stage = session.stage(Template(LOAD, blanks=('run',)))

    with pytest.raises(RuntimeError, match='session'):
        client.submit(stage, {'run': {'dataset': 'run:1'}})


def test_an_accumulator_pushes_only_the_element_fields(client: Client) -> None:
    loads = [client.submit(LOAD, {'run': {'dataset': f'run:{n}'}}) for n in (1, 2)]
    with client.session() as session:
        total = session.accumulator(TOTAL)
        for load in loads:
            total.push(load)  # pending; 'extra' is not pushed
        combined = client.compute(total)

    assert combined.request.params == {'value': [load.ref('value') for load in loads]}
    assert client.output(combined, 'value') == 3.0
