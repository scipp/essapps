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
    Record,
    Rule,
    Selector,
    Status,
    SubmitError,
    Template,
    TriggerLoop,
    apply,
    file,
)
from ess.apps.testing import CountingClient, FakeFiles, FakeStore, read_file
from ess.spec import (
    FileRef,
    MaskFile,
    Referenced,
    RunFile,
    WorkflowSpec,
    function_binding,
)


class LoadParams(BaseModel):
    sample: RunFile


class LoadMaskParams(BaseModel):
    mask: MaskFile


class NormalizeParams(BaseModel):
    sample: RunFile
    reference: RunFile


class SumRunsParams(BaseModel):
    runs: list[RunFile] = Field(min_length=1)


class Group(BaseModel):
    run: RunFile


class LoadGroupParams(BaseModel):
    group: Group


class Centre(BaseModel):
    x: float


class CentreOutputs(BaseModel):
    centre: Centre


class ReduceParams(BaseModel):
    sample: RunFile
    transmission: RunFile
    centre: Referenced[Centre]


class ValueOutputs(BaseModel):
    value: float


def _spec(name: str, params: type[BaseModel], **kw: Any) -> WorkflowSpec:
    kw.setdefault('outputs', ValueOutputs)
    return WorkflowSpec(
        name=name, version=1, title=name, description=name, params=params, **kw
    )


LOAD = _spec('load', LoadParams)
LOAD_GROUP = _spec('load-group', LoadGroupParams)
LOAD_MASK = _spec('load-mask', LoadMaskParams)
NORMALIZE = _spec('normalize', NormalizeParams)
SUM_RUNS = _spec('sum-runs', SumRunsParams, tables=('runs',))
FIND_CENTRE = _spec('find-centre', LoadParams, outputs=CentreOutputs)
REDUCE = _spec('reduce', ReduceParams)


def load(params: LoadParams) -> ValueOutputs:
    return ValueOutputs(value=read_file(params.sample))


def load_group(params: LoadGroupParams) -> ValueOutputs:
    return ValueOutputs(value=read_file(params.group.run))


def load_mask(params: LoadMaskParams) -> ValueOutputs:
    return ValueOutputs(value=read_file(params.mask))


def sum_runs(params: SumRunsParams) -> ValueOutputs:
    return ValueOutputs(value=sum(read_file(run) for run in params.runs))


def normalize(params: NormalizeParams) -> ValueOutputs:
    return ValueOutputs(value=read_file(params.sample) / read_file(params.reference))


def find_centre(params: LoadParams) -> CentreOutputs:
    return CentreOutputs(centre=Centre(x=read_file(params.sample) / 10))


def reduce(params: ReduceParams) -> ValueOutputs:
    transmission = read_file(params.transmission)
    return ValueOutputs(value=read_file(params.sample) / transmission + params.centre.x)


@pytest.fixture
def files(tmp_path: Path) -> FakeFiles:
    return FakeFiles(proposal='p1', directory=tmp_path)


@pytest.fixture
def client(files: FakeFiles) -> Iterator[Client]:
    backend = Backend(
        files,
        [
            function_binding(LOAD, load),
            function_binding(LOAD_GROUP, load_group),
            function_binding(LOAD_MASK, load_mask),
            function_binding(NORMALIZE, normalize),
            function_binding(SUM_RUNS, sum_runs),
            function_binding(FIND_CENTRE, find_centre),
            function_binding(REDUCE, reduce),
        ],
        store=FakeStore(),
    )
    yield Client(backend, proposal='p1', submitter='anna')
    backend.close()


def _rule(template: Template, at: str, *, name: str = 'rule', **kw: Any) -> Rule:
    """A rule that fills the template's one blank ``at`` with each file."""

    def submit(client: Client, ref: FileRef) -> Record:
        member = str(client.files.metadata(ref)['run'])
        request = template.fill(params={at: ref})
        return client.submit(request, label=label, member=member, persist=True)

    label = kw.setdefault('label', name)
    kw.setdefault('selector', Selector(role='sample'))
    return Rule(name, submit, at=at, **kw)


@pytest.fixture
def loading() -> Rule:
    return _rule(Template(LOAD, blanks=('sample',)), 'sample', label='loaded')


# What one file needs, as an application decides. The metadata stand in for what the
# files will say of their use: `run_label` is shared by the runs of one sample, and
# `run_type` says what each measured.
def _transmission_of(client: Client, sample: FileRef) -> FileRef:
    label = client.files.metadata(sample)['run_label']
    found = client.files.list(Selector(run_type='trans', run_label=label))
    if not found:
        raise LookupError(f'no transmission run of {label} yet')
    return found[-1]


def _centre_of(client: Client, sample: FileRef) -> Record:
    """The sample's centre: one that an earlier call submitted, or a new one."""
    member = str(client.files.metadata(sample)['run'])
    for record in reversed(client.records(label='centres')):
        if record.member == member and client.status(record) not in (
            Status.FAILED,
            Status.CANCELLED,
        ):
            return record
    return client.submit(
        FIND_CENTRE,
        params={'sample': sample},
        label='centres',
        member=member,
        persist=True,
    )


def _reduce(client: Client, sample: FileRef, transmission: Any) -> Record:
    return client.submit(
        REDUCE,
        params={
            'sample': sample,
            'transmission': transmission,
            'centre': _centre_of(client, sample).ref('centre'),
        },
        label='reduced',
        member=str(client.files.metadata(sample)['run']),
        persist=True,
    )


def _reduce_with_its_own_centre(client: Client, sample: FileRef) -> Record:
    return _reduce(client, sample, _transmission_of(client, sample))


@pytest.fixture
def reducing() -> Rule:
    return Rule(
        'reduce',
        _reduce_with_its_own_centre,
        selector=Selector(run_type='sans'),
        label='reduced',
        at='sample',
    )


def test_one_file_may_submit_a_chain_of_requests(
    client: Client, files: FakeFiles, reducing: Rule
) -> None:
    sample = files.measure(1, 8.0, run_type='sans', run_label='water')
    files.measure(2, 2.0, run_type='trans', run_label='water')

    (reduced,) = TriggerLoop(client, rules=[reducing]).step()

    assert reduced.request.params['sample'] == sample
    assert client.output(reduced, 'value') == 8.0 / 2.0 + 0.8
    assert len(client.records(label='centres')) == 1


def test_a_file_the_function_skips_is_tried_again(
    client: Client, files: FakeFiles, reducing: Rule
) -> None:
    loop = TriggerLoop(client, rules=[reducing])
    sample = files.measure(1, 8.0, run_type='sans', run_label='water')

    assert loop.step() == []
    assert loop.status(reducing).reason == (
        f'skipped {sample.file}: no transmission run of water yet'
    )

    files.measure(2, 2.0, run_type='trans', run_label='water')  # arrives after it
    (reduced,) = loop.step()

    assert reduced.request.params['sample'] == sample
    assert loop.status(reducing).reason is None
    assert loop.step() == []


def test_a_file_that_cannot_go_on_does_not_hold_back_the_others(
    client: Client, files: FakeFiles, reducing: Rule
) -> None:
    waiting = files.measure(1, 8.0, run_type='sans', run_label='water')
    other = files.measure(2, 6.0, run_type='sans', run_label='sds')
    files.measure(3, 3.0, run_type='trans', run_label='sds')
    loop = TriggerLoop(client, rules=[reducing])

    (reduced,) = loop.step()

    assert reduced.request.params['sample'] == other
    assert waiting.file in loop.status(reducing).reason


def test_two_runs_of_one_sample_are_two_files(
    client: Client, files: FakeFiles, reducing: Rule
) -> None:
    runs = [files.measure(n, 8.0, run_type='sans', run_label='water') for n in (1, 2)]
    files.measure(3, 2.0, run_type='trans', run_label='water')

    reduced = TriggerLoop(client, rules=[reducing]).step()

    assert [r.request.params['sample'] for r in reduced] == runs


def test_a_call_after_a_refusal_reuses_what_it_submitted(
    client: Client, files: FakeFiles
) -> None:
    rule = Rule(
        'reduce',
        lambda client, sample: _reduce(client, sample, file(run=2)),
        selector=Selector(run_type='sans'),
        label='reduced',
        at='sample',
    )
    files.measure(1, 8.0, run_type='sans')  # run 2 is not known yet
    loop = TriggerLoop(client, rules=[rule])

    assert loop.step() == []
    assert 'unknown file run:2' in loop.status(rule).reason
    assert loop.step() == []

    files.measure(2, 2.0)
    (reduced,) = TriggerLoop(client, rules=[rule]).step()  # as after a restart

    assert client.output(reduced, 'value') == 8.0 / 2.0 + 0.8
    assert len(client.records(label='centres')) == 1


def test_a_file_whose_centre_failed_settles_with_a_failed_record(
    client: Client, files: FakeFiles, reducing: Rule
) -> None:
    sample = files.measure(1, 8.0, run_type='sans', run_label='water')
    files.measure(2, 2.0, run_type='trans', run_label='water')
    files.corrupt(sample)
    loop = TriggerLoop(client, rules=[reducing])
    failed_centre = client.submit(
        FIND_CENTRE, params={'sample': sample}, label='centres', member='1'
    )
    assert client.wait(failed_centre) == 'failed'

    # A new centre may fail before the reduction references it: then the next step
    # tries again.
    settled: list[Record] = []
    for _ in range(100):
        if settled := loop.step():
            break
        client.wait(client.records(label='centres'))

    (reduced,) = settled
    assert client.wait(reduced) == 'failed'
    assert loop.step() == []


def test_a_function_that_raises_value_error_skips_the_file(
    client: Client, files: FakeFiles
) -> None:
    def submit(client: Client, ref: FileRef) -> Record:
        raise ValueError('not this one')

    rule = Rule('rule', submit, selector=Selector(), label='rule', at='sample')
    run = files.measure(1, 2.0)
    loop = TriggerLoop(client, rules=[rule])

    assert loop.step() == []
    assert loop.status(rule).reason == f'skipped {run.file}: not this one'


def test_any_other_exception_of_a_function_stops_the_loop(
    client: Client, files: FakeFiles
) -> None:
    def submit(client: Client, ref: FileRef) -> Record:
        raise RuntimeError('a bug')

    rule = Rule('rule', submit, selector=Selector(), label='rule', at='sample')
    files.measure(1, 2.0)

    with pytest.raises(RuntimeError, match='a bug'):
        TriggerLoop(client, rules=[rule]).step()


def test_rules_of_two_labels_each_handle_a_file(
    client: Client, files: FakeFiles
) -> None:
    template = Template(LOAD, blanks=('sample',))
    rules = [_rule(template, 'sample', name=name) for name in ('first', 'second')]
    files.measure(1, 2.0, role='sample')

    settled = TriggerLoop(client, rules=rules).step()

    assert [r.label for r in settled] == ['first', 'second']


def test_a_file_in_a_group_at_the_parameter_does_not_count_as_handled(
    client: Client, files: FakeFiles
) -> None:
    def submit(client: Client, ref: FileRef) -> Record:
        raise ValueError('called')

    rule = Rule('rule', submit, selector=Selector(), label='x', at='group')
    run = files.measure(1, 2.0)
    client.submit(LOAD_GROUP, params={'group': {'run': run}}, label='x')
    loop = TriggerLoop(client, rules=[rule])

    loop.step()

    assert loop.status(rule).reason == f'skipped {run.file}: called'


def test_two_rules_of_one_name_are_refused(client: Client, loading: Rule) -> None:
    with pytest.raises(ValueError, match=r"two rules are named \['rule'\]"):
        TriggerLoop(client, rules=[loading, loading])


@pytest.mark.parametrize(
    'returned',
    ['nothing', 'another-label', 'another-file'],
)
def test_a_function_whose_result_does_not_settle_the_file_stops_the_loop(
    client: Client, files: FakeFiles, returned: str
) -> None:
    other = files.measure(9, 1.0)

    def submit(client: Client, ref: FileRef) -> Any:
        if returned == 'nothing':
            return None
        sample = other if returned == 'another-file' else ref
        label = 'elsewhere' if returned == 'another-label' else 'rule'
        return client.submit(LOAD, params={'sample': sample}, label=label, persist=True)

    rule = Rule(
        'rule', submit, selector=Selector(role='sample'), label='rule', at='sample'
    )
    run = files.measure(1, 2.0, role='sample')

    with pytest.raises(TypeError, match=f'rule: submit for {re.escape(run.file)}'):
        TriggerLoop(client, rules=[rule]).step()


def test_a_rule_whose_requests_are_refused_does_not_stop_the_others(
    client: Client, files: FakeFiles, loading: Rule
) -> None:
    unknown = _rule(
        Template(NORMALIZE, params={'reference': file(run=9)}, blanks=('sample',)),
        'sample',
        name='unknown',
        label='normalized',
    )
    loop = TriggerLoop(client, rules=[unknown, loading])
    run = files.measure(2, 5.0, role='sample')

    (loaded,) = loop.step()

    assert loaded.label == 'loaded'
    assert loop.status(unknown).reason == (
        f'skipped {run.file}: reference: unknown file run:9'
    )
    assert loop.status(loading).reason is None


def test_a_rule_says_when_there_is_no_new_file(client: Client, loading: Rule) -> None:
    loop = TriggerLoop(client, rules=[loading])

    assert loop.step() == []
    assert loop.status(loading).reason == 'no new file'


def test_a_rule_submits_each_new_file_though_one_cannot_be_read(
    client: Client, files: FakeFiles
) -> None:
    rule = _rule(Template(LOAD, blanks=('sample',)), 'sample', selector=Selector())
    loop = TriggerLoop(client, rules=[rule])
    runs = [files.measure(n, float(n)) for n in (1, 2, 3)]
    files.corrupt(runs[0])

    submitted = loop.step()

    assert [r.request.params['sample'] for r in submitted] == runs
    assert client.wait(submitted) == ['failed', 'completed', 'completed']
    assert loop.status(rule).reason is None
    assert loop.step() == []  # a failed record settles its file


def test_a_rule_selects_any_kind_of_file(client: Client, files: FakeFiles) -> None:
    rule = _rule(
        Template(LOAD_MASK, blanks=('mask',)),
        'mask',
        selector=Selector(kind='mask', instrument='loki'),
    )
    files.measure(1, 5.0, role='sample')
    files.measure(2, 5.0, kind='mask', instrument='dream')
    loki = files.measure(3, 3.0, kind='mask', instrument='loki')

    (loaded,) = TriggerLoop(client, rules=[rule]).step()

    assert loaded.request.params == {'mask': loki}
    assert client.output(loaded, 'value') == 3.0


def test_a_rule_has_handled_a_file_only_at_the_place_it_fills(
    client: Client, files: FakeFiles
) -> None:
    first = files.measure(1, 6.0, role='sample')
    second = files.measure(2, 2.0, role='sample')
    template = Template(NORMALIZE, params={'reference': second}, blanks=('sample',))
    rule = _rule(template, 'sample', label='normalized')
    # holds the first at the place, and the second elsewhere
    client.submit(template.fill(params={'sample': first}), label='normalized')

    (submitted,) = TriggerLoop(client, rules=[rule]).step()

    assert submitted.member == '2'
    assert submitted.request.params['sample'] == second


def _sum_all_so_far(client: Client, ref: FileRef) -> Record:
    runs = client.files.list(Selector(role='sample'))
    template = Template(SUM_RUNS, blanks=('runs',))
    return client.submit(
        template.fill(params={'runs': runs}), label='summed', persist=True
    )


@pytest.fixture
def summed() -> Rule:
    return Rule(
        'summed',
        _sum_all_so_far,
        selector=Selector(role='sample'),
        label='summed',
        at='runs',
    )


def test_a_record_settles_each_file_in_a_row_of_a_list(
    client: Client, files: FakeFiles, summed: Rule
) -> None:
    loop = TriggerLoop(client, rules=[summed])
    first = files.measure(1, 2.0, role='sample')
    (one,) = loop.step()
    second = files.measure(2, 3.0, role='sample')
    third = files.measure(3, 4.0, role='sample')
    (two,) = loop.step()  # one call: its record settles both new files

    assert one.request.params['runs'] == [first]
    assert two.request.params['runs'] == [first, second, third]
    assert client.output(two, 'value') == 9.0
    assert loop.step() == []


def test_a_loop_started_on_a_history_has_handled_the_files_in_rows(
    client: Client, files: FakeFiles, summed: Rule
) -> None:
    files.measure(1, 2.0, role='sample')
    files.measure(2, 3.0, role='sample')
    client.wait(TriggerLoop(client, rules=[summed]).step())

    assert TriggerLoop(client, rules=[summed]).step() == []


def test_an_idle_step_reads_no_records(
    client: Client, files: FakeFiles, loading: Rule
) -> None:
    counting = CountingClient(client)
    loop = TriggerLoop(counting, rules=[loading])  # type: ignore[arg-type]
    files.measure(1, 2.0, role='sample')
    client.wait(loop.step())
    loop.step()  # reads the record the first step submitted
    counting.reads.clear()

    assert loop.step() == []
    assert counting.reads['records'] == 0


def test_a_step_after_a_new_record_reads_only_that_record(
    client: Client, files: FakeFiles, loading: Rule
) -> None:
    counting = CountingClient(client)
    loop = TriggerLoop(counting, rules=[loading])  # type: ignore[arg-type]
    files.measure(1, 2.0, role='sample')
    client.wait(loop.step())
    loop.step()
    measured = files.measure(2, 3.0)
    template = Template(LOAD, blanks=('sample',))
    other = client.compute(template.fill(params={'sample': measured}), label='other')
    counting.reads.clear()

    assert loop.step() == []
    assert counting.reads['records'] == 1
    assert client.output(other, 'value') == 3.0


def test_a_loop_started_on_a_history_does_not_submit_the_handled_files_again(
    client: Client, files: FakeFiles, loading: Rule
) -> None:
    files.measure(1, 2.0, role='sample')
    files.measure(2, 3.0, role='sample')
    client.wait(TriggerLoop(client, rules=[loading]).step())
    counting = CountingClient(client)

    late = TriggerLoop(counting, rules=[loading])  # type: ignore[arg-type]

    assert late.step() == []
    assert counting.reads['records'] == 2
    assert len(client.records(label='loaded')) == 2


def test_two_rules_with_one_label_in_one_step_submit_each_file_once(
    client: Client, files: FakeFiles, loading: Rule
) -> None:
    again = Rule(
        'again',
        loading.submit,
        selector=loading.selector,
        label=loading.label,
        at=loading.at,
    )
    loop = TriggerLoop(client, rules=[loading, again])
    files.measure(1, 2.0, role='sample')

    assert len(loop.step()) == 1
    assert loop.status(again).reason == 'no new file'
    assert loop.step() == []
    assert len(client.records(label='loaded')) == 1


def test_apply_fills_the_blank_at_for_each_file(
    client: Client, files: FakeFiles
) -> None:
    sample = files.measure(1, 6.0, role='sample')
    reference = files.measure(2, 2.0, role='reference')
    template = Template(NORMALIZE, params={'reference': reference}, blanks=('sample',))

    normalized = apply(template, [sample], client.files, at='sample')

    assert normalized['1'].params == {'sample': sample, 'reference': reference}
    assert client.output(client.compute(normalized['1']), 'value') == 3.0


@pytest.mark.parametrize(
    ('blanks', 'at', 'reason'),
    [
        (('sample', 'reference'), 'sample', 'a file fills one blank'),
        (('sample',), 'reference', "a file fills the blank 'sample', not 'reference'"),
    ],
)
def test_apply_refuses_a_template_that_a_file_does_not_fill(
    client: Client, files: FakeFiles, blanks: tuple[str, ...], at: str, reason: str
) -> None:
    sample = files.measure(1, 6.0, role='sample')

    with pytest.raises(ValueError, match=re.escape(reason)):
        apply(Template(NORMALIZE, blanks=blanks), [sample], client.files, at=at)


def test_apply_refuses_two_files_of_one_member(
    client: Client, files: FakeFiles
) -> None:
    runs = [files.measure(n, 1.0, sample='water') for n in (1, 2)]
    template = Template(LOAD, blanks=('sample',))

    with pytest.raises(ValueError, match='two files have sample water'):
        apply(template, runs, client.files, at='sample', member_field='sample')


def test_apply_raises_for_a_file_without_the_member_field(
    client: Client, files: FakeFiles
) -> None:
    run = files.measure(1, 1.0)
    template = Template(LOAD, blanks=('sample',))

    with pytest.raises(LookupError):
        apply(template, [run], client.files, at='sample', member_field='sample')


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


def test_a_batch_with_a_file_that_cannot_be_read_fails_only_its_record(
    client: Client, files: FakeFiles
) -> None:
    runs = [files.measure(n, float(n)) for n in (1, 2, 3)]
    files.corrupt(runs[1])

    requests = apply(
        Template(LOAD, blanks=('sample',)), runs, client.files, at='sample'
    )
    scan = client.compute(requests, label='scan')

    assert client.wait(scan) == {'1': 'completed', '2': 'failed', '3': 'completed'}
