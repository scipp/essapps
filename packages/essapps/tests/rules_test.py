# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The trigger loop: what one step submits, and why a rule submits nothing."""

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field

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
from ess.apps.testing import FakeDatasets, FakeStore, read_dataset
from ess.spec import WorkflowSpec, function_binding


class Run(BaseModel):
    file: Path


class ScaledRun(BaseModel):
    """A run and its factor."""

    file: Path
    factor: float = 1.0


class LoadParams(BaseModel):
    sample: Run


class SubtractParams(BaseModel):
    sample: Run
    can: ScaledRun


class Sample(BaseModel):
    """A sample, which holds its run: a file two fields below the parameter."""

    run: ScaledRun
    label: str = ''


class NestedParams(BaseModel):
    sample: Sample


class NormalizeParams(BaseModel):
    sample: ScaledRun
    reference: Run


class SumRunsParams(BaseModel):
    runs: list[ScaledRun] = Field(min_length=1)


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
NORMALIZE = _spec('normalize', NormalizeParams)
SUM_RUNS = _spec('sum-runs', SumRunsParams, tables=('runs',))
NESTED = _spec('nested', NestedParams)


def load(params: LoadParams) -> ValueOutputs:
    return ValueOutputs(value=read_dataset(params.sample.file))


def subtract(params: SubtractParams) -> ValueOutputs:
    sample, can = params.sample, params.can
    return ValueOutputs(
        value=read_dataset(sample.file) - can.factor * read_dataset(can.file)
    )


def nested(params: NestedParams) -> ValueOutputs:
    run = params.sample.run
    return ValueOutputs(value=run.factor * read_dataset(run.file))


def sum_runs(params: SumRunsParams) -> ValueOutputs:
    return ValueOutputs(
        value=sum(run.factor * read_dataset(run.file) for run in params.runs)
    )


def normalize(params: NormalizeParams) -> ValueOutputs:
    sample = params.sample.factor * read_dataset(params.sample.file)
    return ValueOutputs(value=sample / read_dataset(params.reference.file))


LAST_CAN = LastBefore(Selector(role='can'))


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
            function_binding(NORMALIZE, normalize),
            function_binding(SUM_RUNS, sum_runs),
            function_binding(NESTED, nested),
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
        at=('sample', 'file'),
        lookup=Lookup({('can', 'file'): LastBefore(Selector(role='can'))}),
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
    cans = Lookup({('can', 'file'): LastBefore(Selector(role='can'))})

    with pytest.raises(LookupError, match='before run 2'):
        apply(template, [sample], client.datasets, at=('sample', 'file'), lookup=cans)


def test_a_lookup_gives_the_other_fields_of_the_blank_it_fills(
    client: Client, datasets: FakeDatasets
) -> None:
    can = datasets.measure(1, 2.0, role='can')
    sample = datasets.measure(2, 5.0, role='sample')
    cans = Lookup(
        {('can', 'file'): LastBefore(Selector(role='can'))},
        fields={'can': {'factor': 2.0}},
    )
    template = Template(SUBTRACT, blanks=('sample', 'can'))

    subtracted = apply(
        template, [sample], client.datasets, at=('sample', 'file'), lookup=cans
    )

    assert subtracted['2'].params['can'] == {'file': can, 'factor': 2.0}
    assert client.output(client.compute(subtracted['2']), 'value') == 1.0


@pytest.mark.parametrize(
    ('rules', 'fields', 'reason'),
    [
        ({('can',): LAST_CAN}, None, "a lookup fills a field of a blank, such as"),
        ({'can': LAST_CAN}, None, "a lookup fills a field of a blank, such as"),
        (
            {('can', 'file'): LAST_CAN, ('can', 'other'): LAST_CAN},
            None,
            'a lookup fills one place per blank',
        ),
        (
            {('can', 'file'): LAST_CAN},
            {'sample': {'factor': 2.0}},
            "fields of ['sample'], which the lookup does not fill",
        ),
        (
            {('can', 'file'): LAST_CAN},
            {'can': {'file': None}},
            "('can', 'file') holds the dataset; fields gives it too",
        ),
    ],
)
def test_a_lookup_that_fills_no_single_field_of_each_blank_is_refused(
    rules: dict[Any, LastBefore], fields: dict[str, Any] | None, reason: str
) -> None:
    with pytest.raises(ValueError, match=re.escape(reason)):
        Lookup(rules, fields=fields)


def test_a_rule_whose_requests_are_refused_does_not_stop_the_others(
    client: Client, datasets: FakeDatasets
) -> None:
    misplaced = Rule(
        'misplaced',
        Template(LOAD, blanks=('sample',)),
        selector=Selector(role='sample'),
        label='misplaced',
        at=('sample', 'path'),  # Run has no field path
    )
    load = Rule(
        'load',
        Template(LOAD, blanks=('sample',)),
        selector=Selector(role='sample'),
        label='loaded',
        at=('sample', 'file'),
    )
    loop = TriggerLoop(client, rules=[misplaced, load])
    datasets.measure(2, 5.0, role='sample')

    (loaded,) = loop.step()

    assert loaded.label == 'loaded'
    assert (
        loop.status(misplaced).reason
        == '2: sample.path: Extra inputs are not permitted'
    )
    assert loop.status(load).reason is None


@pytest.mark.parametrize(
    ('blanks', 'at', 'reason'),
    [
        (
            ('sample', 'reference'),
            ('sample', 'file'),
            "a dataset fills one blank; ('sample', 'reference') leave",
        ),
        (('sample',), ('reference', 'file'), 'a dataset fills a field of the blank'),
        (('sample',), ('sample',), 'a dataset fills a field of the blank sample'),
        (('sample',), (), 'a dataset fills a field of the blank sample'),
    ],
)
@pytest.mark.parametrize('series', [None, 'sample'])
def test_a_rule_that_no_dataset_fills_is_refused_when_made(
    blanks: tuple[str, ...],
    at: tuple[str, ...],
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


def test_a_dataset_fills_the_place_at_in_a_blank_group(
    client: Client, datasets: FakeDatasets
) -> None:
    sample = datasets.measure(1, 6.0, role='sample')
    reference = datasets.measure(2, 2.0, role='reference')
    template = Template(
        NORMALIZE, params={'reference': {'file': reference}}, blanks=('sample',)
    )

    normalized = apply(template, [sample], client.datasets, at=('sample', 'file'))

    assert normalized['1'].params['sample'] == {'file': sample}
    assert client.output(client.compute(normalized['1']), 'value') == 3.0
    with pytest.raises(ValueError, match='a dataset fills a field of the blank sample'):
        apply(template, [sample], client.datasets, at=('sample',))


def test_apply_gives_the_other_fields_of_the_blank(
    client: Client, datasets: FakeDatasets
) -> None:
    sample = datasets.measure(1, 6.0, role='sample')
    reference = datasets.measure(2, 2.0, role='reference')
    template = Template(
        NORMALIZE, params={'reference': {'file': reference}}, blanks=('sample',)
    )

    normalized = apply(
        template,
        [sample],
        client.datasets,
        at=('sample', 'file'),
        fields={'factor': 2.0},
    )

    assert normalized['1'].params['sample'] == {'file': sample, 'factor': 2.0}
    assert client.output(client.compute(normalized['1']), 'value') == 6.0
    with pytest.raises(
        ValueError,
        match=re.escape("('sample', 'file') holds the dataset; fields gives it too"),
    ):
        apply(
            template,
            [sample],
            client.datasets,
            at=('sample', 'file'),
            fields={'file': reference},
        )


@pytest.mark.parametrize('series', [None, 'sample'])
def test_a_rule_whose_fields_give_the_place_at_is_refused_when_made(
    series: str | None,
) -> None:
    with pytest.raises(
        ValueError,
        match=re.escape("('sample', 'file') holds the dataset; fields gives it too"),
    ):
        Rule(
            'normalize',
            Template(NORMALIZE, blanks=('sample',)),
            selector=Selector(role='sample'),
            label='normalized',
            at=('sample', 'file'),
            fields={'file': None},
            series=series,
        )


def test_a_dataset_fills_a_place_below_a_group_inside_the_blank(
    client: Client, datasets: FakeDatasets
) -> None:
    sample = datasets.measure(1, 3.0, role='sample')
    template = Template(NESTED, blanks=('sample',))
    at = ('sample', 'run', 'file')
    fields = {'run': {'factor': 2.0}, 'label': 'si'}

    filled = apply(template, [sample], client.datasets, at=at, fields=fields)

    assert filled['1'].params['sample'] == {
        'run': {'factor': 2.0, 'file': sample},
        'label': 'si',
    }
    assert fields == {'run': {'factor': 2.0}, 'label': 'si'}  # not changed
    assert client.output(client.compute(filled['1']), 'value') == 6.0
    with pytest.raises(
        ValueError,
        match=re.escape(
            "('sample', 'run') holds the dataset at ('sample', 'run', 'file'); "
            'fields gives 2.0 there'
        ),
    ):
        apply(template, [sample], client.datasets, at=at, fields={'run': 2.0})


def test_a_rule_gives_the_other_fields_of_the_blank(
    client: Client, datasets: FakeDatasets
) -> None:
    reference = datasets.measure(1, 2.0, role='reference')
    rule = Rule(
        'normalize',
        Template(
            NORMALIZE, params={'reference': {'file': reference}}, blanks=('sample',)
        ),
        selector=Selector(role='sample'),
        label='normalized',
        at=('sample', 'file'),
        fields={'factor': 2.0},
    )
    sample = datasets.measure(2, 6.0, role='sample')

    (normalized,) = TriggerLoop(client, rules=[rule]).step()

    assert normalized.request.params['sample'] == {'file': sample, 'factor': 2.0}
    assert client.output(normalized, 'value') == 6.0


def test_a_rule_has_handled_a_dataset_only_at_the_place_it_fills(
    client: Client, datasets: FakeDatasets
) -> None:
    first = datasets.measure(1, 6.0, role='sample')
    second = datasets.measure(2, 2.0, role='sample')
    template = Template(
        NORMALIZE, params={'reference': {'file': second}}, blanks=('sample',)
    )
    rule = Rule(
        'normalize',
        template,
        selector=Selector(role='sample'),
        label='normalized',
        at=('sample', 'file'),
    )
    # holds the first at the place, and the second elsewhere
    client.submit(template.fill(params={'sample': {'file': first}}), label='normalized')

    (submitted,) = TriggerLoop(client, rules=[rule]).step()

    assert submitted.member == '2'
    assert submitted.request.params['sample'] == {'file': second, 'factor': 1.0}


def test_a_series_fills_the_place_at_in_each_row(
    client: Client, datasets: FakeDatasets
) -> None:
    rule = Rule(
        'summed',
        Template(SUM_RUNS, blanks=('runs',)),
        selector=Selector(role='sample'),
        label='summed',
        series='sample',
        at=('runs', 'file'),
    )
    loop = TriggerLoop(client, rules=[rule])
    first = datasets.measure(1, 2.0, role='sample', sample='si')
    (one,) = loop.step()
    second = datasets.measure(2, 3.0, role='sample', sample='si')
    (two,) = loop.step()

    assert one.request.params['runs'] == [{'file': first, 'factor': 1.0}]
    assert two.request.params['runs'] == [
        {'file': first, 'factor': 1.0},
        {'file': second, 'factor': 1.0},
    ]
    assert client.output(two, 'value') == 5.0
    assert loop.step() == []  # both handled, each at its place in a row


def test_a_series_gives_the_other_fields_of_each_row(
    client: Client, datasets: FakeDatasets
) -> None:
    rule = Rule(
        'summed',
        Template(SUM_RUNS, blanks=('runs',)),
        selector=Selector(role='sample'),
        label='summed',
        series='sample',
        at=('runs', 'file'),
        fields={'factor': 2.0},
    )
    first = datasets.measure(1, 2.0, role='sample', sample='si')
    second = datasets.measure(2, 3.0, role='sample', sample='si')

    (summed,) = TriggerLoop(client, rules=[rule]).step()

    assert summed.request.params['runs'] == [
        {'file': first, 'factor': 2.0},
        {'file': second, 'factor': 2.0},
    ]
    assert client.output(summed, 'value') == 10.0
