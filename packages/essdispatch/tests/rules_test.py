# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The trigger loop: what one step submits, and why a rule submits nothing."""

from collections.abc import Iterator
from pathlib import Path
from typing import NewType

import pytest
from pydantic import BaseModel

from ess.dispatch import (
    Backend,
    Client,
    LastBefore,
    Lookup,
    Rule,
    Selector,
    SubmitError,
    Template,
    TriggerLoop,
    apply,
)
from ess.dispatch.testing import FakeDatasets, FakeStore, read_dataset
from ess.spec import WorkflowSpec, function_binding

SampleFile = NewType('SampleFile', Path)
CanFile = NewType('CanFile', Path)
ReferenceFile = NewType('ReferenceFile', Path)


class LoadParams(BaseModel):
    sample: SampleFile


class SubtractParams(BaseModel):
    sample: SampleFile
    can: CanFile


class Scale(BaseModel):
    factor: float = 1.0


class ScaleParams(BaseModel):
    scale: Scale
    sample: SampleFile


class Run(BaseModel):
    """A run and its factor: a group that holds a file."""

    run: SampleFile
    factor: float = 1.0


class NormalizeParams(BaseModel):
    sample: Run
    reference: ReferenceFile


class ValueOutputs(BaseModel):
    value: float


def _spec(name: str, params: type[BaseModel]) -> WorkflowSpec:
    return WorkflowSpec(
        name=name,
        version=1,
        title=name,
        description=name,
        params=params,
        outputs=ValueOutputs,
    )


LOAD = _spec('load', LoadParams)
SUBTRACT = _spec('subtract', SubtractParams)
SCALE = _spec('scale', ScaleParams)
NORMALIZE = _spec('normalize', NormalizeParams)


def load(params: LoadParams) -> ValueOutputs:
    return ValueOutputs(value=read_dataset(params.sample))


def subtract(params: SubtractParams) -> ValueOutputs:
    return ValueOutputs(value=read_dataset(params.sample) - read_dataset(params.can))


def scale(params: ScaleParams) -> ValueOutputs:
    return ValueOutputs(value=params.scale.factor * read_dataset(params.sample))


def normalize(params: NormalizeParams) -> ValueOutputs:
    sample = params.sample.factor * read_dataset(params.sample.run)
    return ValueOutputs(value=sample / read_dataset(params.reference))


@pytest.fixture
def datasets(tmp_path: Path) -> FakeDatasets:
    return FakeDatasets(proposal='p1', directory=tmp_path)


@pytest.fixture
def client(datasets: FakeDatasets) -> Iterator[Client]:
    backend = Backend(
        datasets,
        [
            function_binding(LOAD, load),
            function_binding(SUBTRACT, subtract),
            function_binding(SCALE, scale),
            function_binding(NORMALIZE, normalize),
        ],
        store=FakeStore(),
    )
    yield Client(backend, proposal='p1', submitter='anna')
    backend.close()


def test_a_dataset_whose_lookup_finds_nothing_is_skipped_until_it_does(
    client: Client, datasets: FakeDatasets
) -> None:
    subtract = Rule(
        'subtract',
        Template(SUBTRACT, blanks=('sample', 'can')),
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
    template = Template(SUBTRACT, blanks=('sample', 'can'))
    cans = Lookup(can=LastBefore(Selector(role='can')))

    with pytest.raises(LookupError, match='before run 2'):
        apply(template, [sample], client.datasets, lookup=cans)


def test_a_rule_whose_requests_cannot_be_made_does_not_stop_the_others(
    client: Client, datasets: FakeDatasets
) -> None:
    no_lookup = Rule(
        'no-lookup',
        Template(SUBTRACT, blanks=('sample', 'can')),  # a dataset fills only one
        selector=Selector(role='sample'),
        label='subtracted',
    )
    load = Rule(
        'load',
        Template(LOAD, blanks=('sample',)),
        selector=Selector(role='sample'),
        label='loaded',
    )
    loop = TriggerLoop(client, rules=[no_lookup, load])
    datasets.measure(1, 5.0, role='sample')

    (loaded,) = loop.step()

    assert loaded.label == 'loaded'
    assert loop.status(no_lookup).reason == (
        "a dataset fills one blank; ('sample', 'can') leave ['sample', 'can']"
    )
    assert loop.status(load).reason is None


def test_a_dataset_fills_a_blank_file_never_a_group(
    client: Client, datasets: FakeDatasets
) -> None:
    sample = datasets.measure(1, 5.0, role='sample')
    scaled = apply(
        Template(SCALE, params={'scale': Scale(factor=2.0)}, blanks=('sample',)),
        [sample],
        client.datasets,
    )
    rule = Rule(
        'scale',
        Template(SCALE, params={'sample': sample}, blanks=('scale',)),
        selector=Selector(role='sample'),
        label='scaled',
    )
    loop = TriggerLoop(client, rules=[rule])

    assert client.output(client.compute(scaled['1']), 'value') == 10.0
    assert scaled['1'].params['sample'] == sample
    assert loop.step() == []
    assert loop.status(rule).reason == (
        '1: scale: dataset uuid:run-1 does not fit Scale, which is not a file'
    )


def test_a_dataset_fills_the_place_at_in_a_blank_group(
    client: Client, datasets: FakeDatasets
) -> None:
    sample = datasets.measure(1, 6.0, role='sample')
    reference = datasets.measure(2, 2.0, role='reference')
    template = Template(NORMALIZE, params={'reference': reference}, blanks=('sample',))

    normalized = apply(template, [sample], client.datasets, at=('sample', 'run'))
    whole = apply(template, [sample], client.datasets)

    assert normalized['1'].params['sample'] == {'run': sample}
    assert client.output(client.compute(normalized['1']), 'value') == 3.0
    with pytest.raises(
        SubmitError, match=r'^1: sample: uuid:run-1 names the whole of Run'
    ):
        client.submit(whole)
    with pytest.raises(ValueError, match='a dataset fills a place in the blank sample'):
        apply(template, [sample], client.datasets, at=('reference',))


def test_a_rule_has_handled_a_dataset_only_at_the_place_it_fills(
    client: Client, datasets: FakeDatasets
) -> None:
    first = datasets.measure(1, 6.0, role='sample')
    second = datasets.measure(2, 2.0, role='sample')
    template = Template(NORMALIZE, params={'reference': second}, blanks=('sample',))
    rule = Rule(
        'normalize',
        template,
        selector=Selector(role='sample'),
        label='normalized',
        at=('sample', 'run'),
    )
    # holds the first at the place, and the second elsewhere
    client.submit(template.fill(params={'sample': {'run': first}}), label='normalized')

    (submitted,) = TriggerLoop(client, rules=[rule]).step()

    assert submitted.member == '2'
    assert submitted.request.params['sample'] == {'run': second, 'factor': 1.0}


def test_a_series_takes_no_place_at() -> None:
    with pytest.raises(ValueError, match='a series takes no at'):
        Rule(
            'series',
            Template(LOAD, blanks=('sample',)),
            selector=Selector(role='sample'),
            label='series',
            series='sample',
            at=('sample',),
        )
