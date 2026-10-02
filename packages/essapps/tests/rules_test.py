# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The trigger loop: what one step submits, and why a rule submits nothing."""

from collections.abc import Iterator

import pytest
from ess.reduce.spec import Array, NexusFile, WorkflowSpec
from pydantic import BaseModel

from ess.apps import (
    Backend,
    Client,
    LastBefore,
    Lookup,
    Rule,
    Selector,
    Template,
    TriggerLoop,
)
from ess.apps.testing import FakeDatasets


class RunParams(BaseModel):
    run: NexusFile


class SubtractParams(BaseModel):
    run: NexusFile
    can: NexusFile


class ValueOutputs(BaseModel):
    value: Array()  # type: ignore[valid-type]


def _spec(name: str, params: type[BaseModel]) -> WorkflowSpec:
    return WorkflowSpec(
        name=name,
        version=1,
        title=name,
        description=name,
        params=params,
        outputs=ValueOutputs,
    )


LOAD = _spec('load', RunParams)
SUBTRACT = _spec('subtract', SubtractParams)


@pytest.fixture
def datasets() -> FakeDatasets:
    return FakeDatasets(proposal='p1')


@pytest.fixture
def client(datasets: FakeDatasets) -> Iterator[Client]:
    backend = Backend(
        datasets,
        {
            LOAD: lambda run: {'value': run},
            SUBTRACT: lambda run, can: {'value': run - can},
        },
    )
    yield Client(backend, proposal='p1', submitter='anna')
    backend.close()


def test_a_rule_whose_lookup_finds_nothing_does_not_stop_the_others(
    client: Client, datasets: FakeDatasets
) -> None:
    subtract = Rule(
        'subtract',
        Template(SUBTRACT, blanks=('run', 'can')),
        selector=Selector(role='sample'),
        label='subtracted',
        lookup=Lookup(can=LastBefore(Selector(role='can'))),
    )
    load = Rule(
        'load',
        Template(LOAD, blanks=('run',)),
        selector=Selector(role='sample'),
        label='loaded',
    )
    loop = TriggerLoop(client, rules=[subtract, load])
    datasets.measure(1, 5.0, role='sample')  # no can measured before it

    (loaded,) = loop.step()

    assert loaded.label == 'loaded'
    assert loop.status(subtract).reason == (
        "no dataset matching Selector({'kind': 'raw', 'role': 'can'}) before run 1"
    )
    assert loop.status(load).reason is None
