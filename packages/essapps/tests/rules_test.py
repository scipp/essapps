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
    SubmitError,
    Template,
    TriggerLoop,
    apply,
    file,
)
from ess.apps.batch import try_apply
from ess.apps.testing import FakeFiles, FakeStore, read_file
from ess.spec import FileModel, WorkflowSpec, function_binding


class SampleRun(FileModel):
    """A run of a sample."""


class CanRun(FileModel):
    """A run of the empty can, subtracted from a sample run."""


class ReferenceRun(FileModel):
    """A run of a reference, which a sample run is divided by."""


class MaskFile(FileModel, kind='mask'):
    """A mask, which takes mask files only."""


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


class Scale(BaseModel):
    factor: float = 1.0


class ScaleParams(BaseModel):
    sample: SampleRun
    scale: Scale = Field(default_factory=Scale)


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
SCALE = _spec('scale', ScaleParams)


def load(params: LoadParams) -> ValueOutputs:
    return ValueOutputs(value=read_file(params.sample.path))


def load_mask(params: LoadMaskParams) -> ValueOutputs:
    return ValueOutputs(value=read_file(params.mask.path))


def subtract(params: SubtractParams) -> ValueOutputs:
    return ValueOutputs(
        value=read_file(params.sample.path) - read_file(params.can.path)
    )


def sum_runs(params: SumRunsParams) -> ValueOutputs:
    return ValueOutputs(value=sum(read_file(run.path) for run in params.runs))


def normalize(params: NormalizeParams) -> ValueOutputs:
    return ValueOutputs(
        value=read_file(params.sample.path) / read_file(params.reference.path)
    )


def scale(params: ScaleParams) -> ValueOutputs:
    return ValueOutputs(value=params.scale.factor * read_file(params.sample.path))


LAST_CAN = LastBefore(Selector(role='can'))


@pytest.fixture
def files(tmp_path: Path) -> FakeFiles:
    return FakeFiles(proposal='p1', directory=tmp_path)


@pytest.fixture
def client(files: FakeFiles) -> Iterator[Client]:
    backend = Backend(
        files,
        [
            function_binding(LOAD, load),
            function_binding(LOAD_MASK, load_mask),
            function_binding(SUBTRACT, subtract),
            function_binding(NORMALIZE, normalize),
            function_binding(SUM_RUNS, sum_runs),
            function_binding(SCALE, scale),
        ],
        store=FakeStore(),
    )
    yield Client(backend, proposal='p1', submitter='anna')
    backend.close()


def test_a_file_whose_lookup_finds_nothing_is_skipped_until_it_does(
    client: Client, files: FakeFiles
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
    files.measure(2, 5.0, role='sample')  # no can measured before it
    files.measure(3, 1.0, role='can')
    files.measure(4, 7.0, role='sample')

    (first,) = loop.step()
    skipped = loop.status(subtract).reason
    files.measure(1, 2.0, role='can')  # an earlier can arrives late
    (second,) = loop.step()

    assert (first.member, client.output(first, 'value')) == ('4', 6.0)
    assert skipped == (
        "skipped uuid:run-2: no file matching "
        "Selector({'kind': 'raw', 'role': 'can'}) before run 2"
    )
    assert (second.member, client.output(second, 'value')) == ('2', 3.0)
    assert loop.status(subtract).reason is None


def test_apply_fills_the_blank_at_and_the_blank_the_lookup_fills(
    client: Client, files: FakeFiles
) -> None:
    can = files.measure(1, 2.0, role='can')
    sample = files.measure(2, 5.0, role='sample')
    template = Template(SUBTRACT, blanks=('sample', 'can'))
    cans = Lookup({'can': LAST_CAN})

    subtracted = apply(template, [sample], client.files, at='sample', lookup=cans)

    assert subtracted['2'].params == {'sample': sample, 'can': can}
    assert client.output(client.compute(subtracted['2']), 'value') == 3.0


def test_apply_raises_for_a_file_whose_lookup_finds_nothing(
    client: Client, files: FakeFiles
) -> None:
    sample = files.measure(2, 5.0, role='sample')
    template = Template(SUBTRACT, blanks=('sample', 'can'))
    cans = Lookup({'can': LAST_CAN})

    with pytest.raises(LookupError, match='before run 2'):
        apply(template, [sample], client.files, at='sample', lookup=cans)


def test_try_apply_leaves_out_a_file_whose_lookup_finds_nothing(
    client: Client, files: FakeFiles
) -> None:
    early = files.measure(1, 5.0, role='sample')
    can = files.measure(2, 1.0, role='can')
    later = files.measure(3, 7.0, role='sample')
    template = Template(SUBTRACT, blanks=('sample', 'can'))
    cans = Lookup({'can': LAST_CAN})

    requests, failed = try_apply(
        template, [early, later], client.files, at='sample', lookup=cans
    )

    assert {m: r.params for m, r in requests.items()} == {
        '3': {'sample': later, 'can': can}
    }
    assert list(failed) == [early]
    assert 'before run 1' in str(failed[early])


def test_apply_at_a_list_parameter_makes_requests_the_backend_refuses(
    client: Client, files: FakeFiles
) -> None:
    sample = files.measure(1, 2.0, role='sample')
    template = Template(SUM_RUNS, blanks=('runs',))

    summed = apply(template, [sample], client.files, at='runs')

    with pytest.raises(
        SubmitError,
        match=r'^runs: uuid:run-1 is a reference to a whole list; a reference stands',
    ):
        client.submit(summed['1'])


def test_a_rule_whose_requests_are_refused_does_not_stop_the_others(
    client: Client, files: FakeFiles
) -> None:
    unknown = Rule(
        'unknown',
        Template(NORMALIZE, params={'reference': file(run=9)}, blanks=('sample',)),
        selector=Selector(role='sample'),
        label='normalized',
        at='sample',
    )
    load = Rule(
        'load',
        Template(LOAD, blanks=('sample',)),
        selector=Selector(role='sample'),
        label='loaded',
        at='sample',
    )
    loop = TriggerLoop(client, rules=[unknown, load])
    files.measure(2, 5.0, role='sample')

    (loaded,) = loop.step()

    assert loaded.label == 'loaded'
    assert loop.status(unknown).reason == '2: reference: unknown file run:9'
    assert loop.status(load).reason is None


def test_a_rule_fills_a_file_model_with_the_files_that_fit_it(
    client: Client, files: FakeFiles
) -> None:
    rule = Rule(
        'mask',
        Template(LOAD_MASK, blanks=('mask',)),
        selector=Selector(kind='mask'),
        label='masks',
        at='mask',
    )
    files.measure(1, 5.0, role='sample')
    mask = files.measure(2, 3.0, kind='mask')

    (loaded,) = TriggerLoop(client, rules=[rule]).step()

    assert loaded.request.params == {'mask': mask}
    assert client.output(loaded, 'value') == 3.0


@pytest.mark.parametrize(
    ('blanks', 'at', 'reason'),
    [
        (
            ('sample', 'reference'),
            'sample',
            "a file fills one blank; ('sample', 'reference') leave",
        ),
        (
            ('sample',),
            'reference',
            "a file fills the blank 'sample', not 'reference'",
        ),
    ],
)
@pytest.mark.parametrize('series', [None, 'sample'])
def test_a_rule_that_no_file_fills_is_refused_when_made(
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


def _rule(template: Template, at: str, **kw: Any) -> Rule:
    kw.setdefault('selector', Selector(role='sample'))
    return Rule('rule', template, label='rule', at=at, **kw)


@pytest.mark.parametrize(
    ('rule', 'reason'),
    [
        (
            _rule(Template(LOAD_MASK, blanks=('mask',)), 'mask'),
            "Selector({'kind': 'raw', 'role': 'sample'}) lists files that do not "
            "fit MaskFile at mask; a selector names each of {'kind': 'mask'}",
        ),
        (
            _rule(
                Template(SUBTRACT, blanks=('sample', 'can')),
                'sample',
                lookup=Lookup({'can': LastBefore(Selector(kind='mask', role='can'))}),
            ),
            "Selector({'kind': 'mask', 'role': 'can'}) lists files that do not "
            "fit CanRun at can; a selector names each of {'kind': 'raw'}",
        ),
        (
            _rule(Template(SUM_RUNS, blanks=('runs',)), 'runs'),
            'runs is a list parameter; a series fills it',
        ),
        (
            _rule(Template(LOAD, blanks=('sample',)), 'sample', series='sample'),
            'a series fills a list parameter, not sample',
        ),
        (
            _rule(Template(SCALE, blanks=('scale',)), 'scale'),
            'scale is not a parameter of a file model',
        ),
    ],
    ids=['selector', 'lookup-selector', 'list', 'series', 'not-a-file-model'],
)
def test_a_rule_that_could_not_submit_what_it_finds_is_refused_by_the_loop(
    client: Client, rule: Rule, reason: str
) -> None:
    with pytest.raises(ValueError, match=f'^rule: {re.escape(reason)}$'):
        TriggerLoop(client, rules=[rule])


def test_a_rule_may_select_by_more_than_the_metadata_of_its_file_model(
    client: Client, files: FakeFiles
) -> None:
    rule = _rule(
        Template(LOAD_MASK, blanks=('mask',)),
        'mask',
        selector=Selector(kind='mask', instrument='loki'),
    )
    files.measure(1, 5.0, kind='mask', instrument='dream')
    loki = files.measure(2, 3.0, kind='mask', instrument='loki')

    (loaded,) = TriggerLoop(client, rules=[rule]).step()

    assert loaded.request.params == {'mask': loki}


def test_a_file_fills_the_blank_at(client: Client, files: FakeFiles) -> None:
    sample = files.measure(1, 6.0, role='sample')
    reference = files.measure(2, 2.0, role='reference')
    template = Template(NORMALIZE, params={'reference': reference}, blanks=('sample',))

    normalized = apply(template, [sample], client.files, at='sample')

    assert normalized['1'].params == {'sample': sample, 'reference': reference}
    assert client.output(client.compute(normalized['1']), 'value') == 3.0
    with pytest.raises(
        ValueError, match="a file fills the blank 'sample', not 'reference'"
    ):
        apply(template, [sample], client.files, at='reference')


def test_apply_refuses_an_at_that_the_lookup_fills(
    client: Client, files: FakeFiles
) -> None:
    sample = files.measure(2, 5.0, role='sample')
    template = Template(SUBTRACT, blanks=('sample', 'can'))
    cans = Lookup({'can': LAST_CAN})

    with pytest.raises(ValueError, match="a file fills the blank 'sample', not 'can'"):
        apply(template, [sample], client.files, at='can', lookup=cans)


def test_a_rule_has_handled_a_file_only_at_the_place_it_fills(
    client: Client, files: FakeFiles
) -> None:
    first = files.measure(1, 6.0, role='sample')
    second = files.measure(2, 2.0, role='sample')
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


def test_a_series_fills_a_row_of_the_blank_with_each_file(
    client: Client, files: FakeFiles, summed: Rule
) -> None:
    loop = TriggerLoop(client, rules=[summed])
    first = files.measure(1, 2.0, role='sample', sample='si')
    (one,) = loop.step()
    second = files.measure(2, 3.0, role='sample', sample='si')
    (two,) = loop.step()

    assert one.request.params['runs'] == [first]
    assert two.request.params['runs'] == [first, second]
    assert client.output(two, 'value') == 5.0
    assert loop.step() == []  # both handled, each in a row


def test_a_series_has_handled_a_file_that_a_record_holds_in_a_row(
    client: Client, files: FakeFiles, summed: Rule
) -> None:
    first = files.measure(1, 2.0, role='sample', sample='si')
    second = files.measure(2, 3.0, role='sample', sample='si')
    client.submit(
        summed.template.fill(params={'runs': [first, second]}), label='summed'
    )

    assert TriggerLoop(client, rules=[summed]).step() == []
