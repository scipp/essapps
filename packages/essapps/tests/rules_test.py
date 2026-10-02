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
    apply,
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


def test_a_dataset_whose_lookup_finds_nothing_is_skipped_until_it_does(
    client: Client, datasets: FakeDatasets
) -> None:
    subtract = Rule(
        'subtract',
        Template(SUBTRACT, blanks=('run', 'can')),
        selector=Selector(role='sample'),
        label='subtracted',
        lookup=Lookup(can=LastBefore(Selector(role='can'))),
    )
    loop = TriggerLoop(client, rules=[subtract])
    datasets.measure(2, 5.0, role='sample')  # no can measured before it
    datasets.measure(3, 1.0, role='can')
    datasets.measure(4, 7.0, role='sample')

    (first,) = loop.step()
    skipped = loop.status(subtract).reason
    datasets.measure(1, 2.0, role='can')  # an earlier can arrives late
    (second,) = loop.step()

    assert (first.member, client.output(first, 'value')) == ('4', 6.0)
    assert skipped == (
        "skipped uuid:run-2: no dataset matching "
        "Selector({'kind': 'raw', 'role': 'can'}) before run 2"
    )
    assert (second.member, client.output(second, 'value')) == ('2', 3.0)
    assert loop.status(subtract).reason is None


def test_apply_raises_for_a_dataset_whose_lookup_finds_nothing(
    client: Client, datasets: FakeDatasets
) -> None:
    sample = datasets.measure(2, 5.0, role='sample')
    template = Template(SUBTRACT, blanks=('run', 'can'))
    cans = Lookup(can=LastBefore(Selector(role='can')))

    with pytest.raises(LookupError, match='before run 2'):
        apply(template, [sample], client.datasets, lookup=cans)


def test_a_rule_whose_requests_cannot_be_made_does_not_stop_the_others(
    client: Client, datasets: FakeDatasets
) -> None:
    no_lookup = Rule(
        'no-lookup',
        Template(SUBTRACT, blanks=('run', 'can')),  # a dataset fills only one
        selector=Selector(role='sample'),
        label='subtracted',
    )
    load = Rule(
        'load',
        Template(LOAD, blanks=('run',)),
        selector=Selector(role='sample'),
        label='loaded',
    )
    loop = TriggerLoop(client, rules=[no_lookup, load])
    datasets.measure(1, 5.0, role='sample')

    (loaded,) = loop.step()

    assert loaded.label == 'loaded'
    assert loop.status(no_lookup).reason == (
        "a dataset fills one blank; ('run', 'can') leave ['run', 'can']"
    )
    assert loop.status(load).reason is None
