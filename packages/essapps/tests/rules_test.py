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
from ess.apps.batch import try_apply
from ess.apps.testing import FakeDatasets, FakeStore, read_dataset
from ess.spec import DatasetModel, WorkflowSpec, function_binding


class SampleRun(DatasetModel):
    """A run of a sample."""


class CanRun(DatasetModel):
    """A run of the empty can, subtracted from a sample run."""


class ReferenceRun(DatasetModel):
    """A run of a reference, which a sample run is divided by."""


class MaskFile(DatasetModel, kind='mask'):
    """A mask, which takes mask datasets only."""


class LoadParams(BaseModel):
    sample: SampleRun


class LoadMaskParams(BaseModel):
    mask: MaskFile


class SubtractParams(BaseModel):
    sample: SampleRun
    can: CanRun


class NormalizeParams(BaseModel):
    sample: SampleRun
    reference: ReferenceRun


class SumRunsParams(BaseModel):
    runs: list[SampleRun] = Field(min_length=1)


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
LOAD_MASK = _spec('load-mask', LoadMaskParams)
SUBTRACT = _spec('subtract', SubtractParams)
NORMALIZE = _spec('normalize', NormalizeParams)
SUM_RUNS = _spec('sum-runs', SumRunsParams, tables=('runs',))


def load(params: LoadParams) -> ValueOutputs:
    return ValueOutputs(value=read_dataset(params.sample.path))


def load_mask(params: LoadMaskParams) -> ValueOutputs:
    return ValueOutputs(value=read_dataset(params.mask.path))


def subtract(params: SubtractParams) -> ValueOutputs:
    return ValueOutputs(
        value=read_dataset(params.sample.path) - read_dataset(params.can.path)
    )


def sum_runs(params: SumRunsParams) -> ValueOutputs:
    return ValueOutputs(value=sum(read_dataset(run.path) for run in params.runs))


def normalize(params: NormalizeParams) -> ValueOutputs:
    return ValueOutputs(
        value=read_dataset(params.sample.path) / read_dataset(params.reference.path)
    )


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
            function_binding(LOAD_MASK, load_mask),
            function_binding(SUBTRACT, subtract),
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
        at='sample',
        lookup=Lookup({'can': LAST_CAN}),
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


def test_apply_fills_the_blank_at_and_the_blank_the_lookup_fills(
    client: Client, datasets: FakeDatasets
) -> None:
    can = datasets.measure(1, 2.0, role='can')
    sample = datasets.measure(2, 5.0, role='sample')
    template = Template(SUBTRACT, blanks=('sample', 'can'))
    cans = Lookup({'can': LAST_CAN})

    subtracted = apply(template, [sample], client.datasets, at='sample', lookup=cans)

    assert subtracted['2'].params == {'sample': sample, 'can': can}
    assert client.output(client.compute(subtracted['2']), 'value') == 3.0


def test_apply_raises_for_a_dataset_whose_lookup_finds_nothing(
    client: Client, datasets: FakeDatasets
) -> None:
    sample = datasets.measure(2, 5.0, role='sample')
    template = Template(SUBTRACT, blanks=('sample', 'can'))
    cans = Lookup({'can': LAST_CAN})

    with pytest.raises(LookupError, match='before run 2'):
        apply(template, [sample], client.datasets, at='sample', lookup=cans)


def test_try_apply_leaves_out_a_dataset_whose_lookup_finds_nothing(
    client: Client, datasets: FakeDatasets
) -> None:
    early = datasets.measure(1, 5.0, role='sample')
    can = datasets.measure(2, 1.0, role='can')
    later = datasets.measure(3, 7.0, role='sample')
    template = Template(SUBTRACT, blanks=('sample', 'can'))
    cans = Lookup({'can': LAST_CAN})

    requests, failed = try_apply(
        template, [early, later], client.datasets, at='sample', lookup=cans
    )

    assert {m: r.params for m, r in requests.items()} == {
        '3': {'sample': later, 'can': can}
    }
    assert list(failed) == [early]
    assert 'before run 1' in str(failed[early])


def test_a_rule_whose_requests_are_refused_does_not_stop_the_others(
    client: Client, datasets: FakeDatasets
) -> None:
    misplaced = Rule(
        'misplaced',
        Template(LOAD_MASK, blanks=('mask',)),
        selector=Selector(role='sample'),  # raw datasets, which no mask fits
        label='misplaced',
        at='mask',
    )
    load = Rule(
        'load',
        Template(LOAD, blanks=('sample',)),
        selector=Selector(role='sample'),
        label='loaded',
        at='sample',
    )
    loop = TriggerLoop(client, rules=[misplaced, load])
    datasets.measure(2, 5.0, role='sample')

    (loaded,) = loop.step()

    assert loaded.label == 'loaded'
    assert loop.status(misplaced).reason == (
        "2: mask: dataset uuid:run-2 does not fit MaskFile, which takes datasets "
        "with {'kind': 'mask'}"
    )
    assert loop.status(load).reason is None


def test_a_rule_fills_a_dataset_model_with_the_datasets_that_fit_it(
    client: Client, datasets: FakeDatasets
) -> None:
    rule = Rule(
        'mask',
        Template(LOAD_MASK, blanks=('mask',)),
        selector=Selector(kind='mask'),
        label='masks',
        at='mask',
    )
    datasets.measure(1, 5.0, role='sample')
    mask = datasets.measure(2, 3.0, kind='mask')

    (loaded,) = TriggerLoop(client, rules=[rule]).step()

    assert loaded.request.params == {'mask': mask}
    assert client.output(loaded, 'value') == 3.0


@pytest.mark.parametrize(
    ('blanks', 'at', 'reason'),
    [
        (
            ('sample', 'reference'),
            'sample',
            "a dataset fills one blank; ('sample', 'reference') leave",
        ),
        (
            ('sample',),
            'reference',
            "a dataset fills the blank 'sample', not 'reference'",
        ),
    ],
)
@pytest.mark.parametrize('series', [None, 'sample'])
def test_a_rule_that_no_dataset_fills_is_refused_when_made(
    blanks: tuple[str, ...], at: str, reason: str, series: str | None
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


def test_a_dataset_fills_the_blank_at(client: Client, datasets: FakeDatasets) -> None:
    sample = datasets.measure(1, 6.0, role='sample')
    reference = datasets.measure(2, 2.0, role='reference')
    template = Template(NORMALIZE, params={'reference': reference}, blanks=('sample',))

    normalized = apply(template, [sample], client.datasets, at='sample')

    assert normalized['1'].params == {'sample': sample, 'reference': reference}
    assert client.output(client.compute(normalized['1']), 'value') == 3.0
    with pytest.raises(
        ValueError, match="a dataset fills the blank 'sample', not 'reference'"
    ):
        apply(template, [sample], client.datasets, at='reference')


def test_apply_refuses_an_at_that_the_lookup_fills(
    client: Client, datasets: FakeDatasets
) -> None:
    sample = datasets.measure(2, 5.0, role='sample')
    template = Template(SUBTRACT, blanks=('sample', 'can'))
    cans = Lookup({'can': LAST_CAN})

    with pytest.raises(
        ValueError, match="a dataset fills the blank 'sample', not 'can'"
    ):
        apply(template, [sample], client.datasets, at='can', lookup=cans)


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
        at='sample',
    )
    # holds the first at the place, and the second elsewhere
    client.submit(template.fill(params={'sample': first}), label='normalized')

    (submitted,) = TriggerLoop(client, rules=[rule]).step()

    assert submitted.member == '2'
    assert submitted.request.params['sample'] == second


@pytest.fixture
def summed() -> Rule:
    return Rule(
        'summed',
        Template(SUM_RUNS, blanks=('runs',)),
        selector=Selector(role='sample'),
        label='summed',
        series='sample',
        at='runs',
    )


def test_a_series_fills_a_row_of_the_blank_with_each_dataset(
    client: Client, datasets: FakeDatasets, summed: Rule
) -> None:
    loop = TriggerLoop(client, rules=[summed])
    first = datasets.measure(1, 2.0, role='sample', sample='si')
    (one,) = loop.step()
    second = datasets.measure(2, 3.0, role='sample', sample='si')
    (two,) = loop.step()

    assert one.request.params['runs'] == [first]
    assert two.request.params['runs'] == [first, second]
    assert client.output(two, 'value') == 5.0
    assert loop.step() == []  # both handled, each in a row


def test_a_series_has_handled_a_dataset_that_a_record_holds_in_a_row(
    client: Client, datasets: FakeDatasets, summed: Rule
) -> None:
    first = datasets.measure(1, 2.0, role='sample', sample='si')
    second = datasets.measure(2, 3.0, role='sample', sample='si')
    client.submit(
        summed.template.fill(params={'runs': [first, second]}), label='summed'
    )

    assert TriggerLoop(client, rules=[summed]).step() == []
