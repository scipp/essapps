# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The trigger loop: what one step submits, and why a rule submits nothing."""

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NewType

import pytest
from pydantic import BaseModel, Field

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


class SumRunsParams(BaseModel):
    runs: list[Run] = Field(min_length=1)


class ValueOutputs(BaseModel):
    value: float


def _spec(name: str, params: type[BaseModel], **kw: Any) -> WorkflowSpec:
    return WorkflowSpec(
        name=name,
        version=1,
        title=name,
        description=name,
        params=params,
        outputs=ValueOutputs,
        **kw,
    )


LOAD = _spec('load', LoadParams)
SUBTRACT = _spec('subtract', SubtractParams)
SCALE = _spec('scale', ScaleParams)
NORMALIZE = _spec('normalize', NormalizeParams)
SUM_RUNS = _spec('sum-runs', SumRunsParams, tables=('runs',))


def load(params: LoadParams) -> ValueOutputs:
    return ValueOutputs(value=read_dataset(params.sample))


def subtract(params: SubtractParams) -> ValueOutputs:
    return ValueOutputs(value=read_dataset(params.sample) - read_dataset(params.can))


def scale(params: ScaleParams) -> ValueOutputs:
    return ValueOutputs(value=params.scale.factor * read_dataset(params.sample))


def sum_runs(params: SumRunsParams) -> ValueOutputs:
    return ValueOutputs(
        value=sum(run.factor * read_dataset(run.run) for run in params.runs)
    )


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
            function_binding(SUM_RUNS, sum_runs),
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


def test_a_rule_whose_requests_are_refused_does_not_stop_the_others(
    client: Client, datasets: FakeDatasets
) -> None:
    whole_group = Rule(
        'whole-group',
        Template(NORMALIZE, blanks=('sample', 'reference')),  # needs at
        selector=Selector(role='sample'),
        label='normalized',
        lookup=Lookup(reference=LastBefore(Selector(role='reference'))),
    )
    load = Rule(
        'load',
        Template(LOAD, blanks=('sample',)),
        selector=Selector(role='sample'),
        label='loaded',
    )
    loop = TriggerLoop(client, rules=[whole_group, load])
    datasets.measure(1, 2.0, role='reference')
    datasets.measure(2, 5.0, role='sample')

    (loaded,) = loop.step()

    assert loaded.label == 'loaded'
    assert loop.status(whole_group).reason == (
        '2: sample: dataset uuid:run-2 does not fit Run, which is not a file'
    )
    assert loop.status(load).reason is None


@pytest.mark.parametrize(
    ('blanks', 'at', 'reason'),
    [
        (
            ('sample', 'reference'),
            None,
            "a dataset fills one blank; ('sample', 'reference') leave",
        ),
        (('sample',), ('reference',), 'a dataset fills a place in the blank sample'),
        (('sample',), (), 'a dataset fills a place in the blank sample'),
    ],
)
@pytest.mark.parametrize('series', [None, 'sample'])
def test_a_rule_that_no_dataset_fills_is_refused_when_made(
    blanks: tuple[str, ...],
    at: tuple[str, ...] | None,
    reason: str,
    series: str | None,
) -> None:
    with pytest.raises(ValueError, match=re.escape(reason)):
        Rule(
            'normalize',
            Template(NORMALIZE, blanks=blanks),
            selector=Selector(role='sample'),
            label='normalized',
            at=at,
            series=series,
        )


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
        SubmitError, match=r'^1: sample: dataset uuid:run-1 does not fit Run, which'
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


def test_a_series_fills_the_place_at_in_each_row(
    client: Client, datasets: FakeDatasets
) -> None:
    rule = Rule(
        'summed',
        Template(SUM_RUNS, blanks=('runs',)),
        selector=Selector(role='sample'),
        label='summed',
        series='sample',
        at=('runs', 'run'),
    )
    loop = TriggerLoop(client, rules=[rule])
    first = datasets.measure(1, 2.0, role='sample', sample='si')
    (one,) = loop.step()
    second = datasets.measure(2, 3.0, role='sample', sample='si')
    (two,) = loop.step()

    assert one.request.params['runs'] == [{'run': first, 'factor': 1.0}]
    assert two.request.params['runs'] == [
        {'run': first, 'factor': 1.0},
        {'run': second, 'factor': 1.0},
    ]
    assert client.output(two, 'value') == 5.0
    assert loop.step() == []  # both handled, each at its place in a row
