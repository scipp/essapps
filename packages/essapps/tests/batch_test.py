# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Apply, the deliberate operations, the trigger loop, and the batch table.

See docs/developer/rules.md.
"""

from pathlib import Path

import pandas as pd
import pytest
import scipp as sc
from pydantic import BaseModel

from ess.apps.backend import SubmitError
from ess.apps.batch import (
    TriggerLoop,
    apply,
    backlog,
    batch_table,
    dataset_table,
    reprocess,
    retry,
    shadowed,
    trigger_status,
)
from ess.apps.client import Client
from ess.apps.examples import (
    LOAD,
    NORMALIZE,
    REBIN,
    SUBTRACT,
    LoadOutputs,
    LoadParams,
    load_workflow,
    write_run,
)
from ess.apps.records import RunRecord, Status, Template
from ess.apps.rules import (
    AsOf,
    Between,
    Bound,
    Like,
    Lookup,
    LookupEntry,
    RetryPolicy,
    Rule,
    Selector,
    Series,
)
from ess.apps.sources import Dataset
from ess.apps.spec import DatasetRef, WorkflowSpec, as_ref, dataset_ref
from ess.apps.testing import FakeDatasetSource


@pytest.fixture
def scan(datasets: Path) -> dict[str, DatasetRef]:
    """Two more runs in the folder the client's dataset source reads."""
    write_run(datasets / 'dream_2.h5', [1.0, 2.0])
    write_run(datasets / 'dream_3.h5', [3.0, 4.0])
    return {
        '300K': dataset_ref(instrument='dream', run=2),
        '310K': dataset_ref(instrument='dream', run=3),
    }


@pytest.fixture
def samples(client: Client, tmp_path: Path) -> FakeDatasetSource:
    """Datasets carrying the fields a lookup and a selector match on."""
    source = FakeDatasetSource(
        Dataset(
            path=write_run(tmp_path / 'v1.h5', [1.0, 2.0]),
            pid='pid/1',
            run=11,
            metadata={'sample': 'vanadium', 'angle': 0.4},
        ),
        Dataset(
            path=write_run(tmp_path / 's1.h5', [3.0, 4.0]),
            pid='pid/2',
            run=12,
            metadata={'sample': 'sio2', 'angle': 1.2},
        ),
    )
    client.backend.sources.append(source)
    return source


@pytest.fixture
def rule(template: Template, client: Client, samples: FakeDatasetSource) -> Rule:
    """A rule over the sample datasets, bounded below every one of them."""
    return Rule(
        name='auto-load',
        template=template,
        selector=Selector(match={'sample': Like(pattern='*')}),
    )


# Apply and the precedence ladder


def test_the_ladder_is_template_then_lookup_entry_then_typed_values(
    client: Client, template: Template, samples: FakeDatasetSource
) -> None:
    lookup = Lookup(
        name='by-sample',
        entries=(
            LookupEntry(
                name='vanadium',
                match={'sample': Like(pattern='van*')},
                fills={'scale': 3.0},
            ),
            LookupEntry(name='rest'),
        ),
    )
    group = apply(
        client,
        template,
        client.datasets(),
        {'pid:pid/2': {'scale': 4.0}},
        lookup=lookup,
        label='ladder',
    )
    scales = {key: request.params['scale'] for key, request in group.items()}
    assert scales['pid:pid/1'] == 3.0  # the lookup entry over the template
    assert scales['pid:pid/2'] == 4.0  # what was pinned over both
    assert scales['run:dream/1'] == 2.0  # the template, matching no entry
    origin = group['pid:pid/2'].origin
    assert origin.pinned == {'scale': 4.0}
    assert origin.entry == 'rest'
    assert origin.template == 'load-defaults/v1'
    # Each member varies the template's blank, whose value is in params.
    assert as_ref(group['pid:pid/1'].params['run']) == dataset_ref(pid='pid/1')
    assert group['pid:pid/1'].vary == ('run',)


def test_apply_without_datasets_is_the_batch_form(
    client: Client, template: Template, scan: dict[str, DatasetRef]
) -> None:
    group = apply(
        client, template, pinned={k: {'run': v} for k, v in scan.items()}, label='scan1'
    )
    records = client.submit_group(group)
    assert {k: r.status for k, r in records.items()} == dict.fromkeys(
        scan, Status.COMPLETED
    )
    assert records['310K'].outputs['total']['value'] == 14.0
    # Members that agree on everything but the blanks share one workflow ID.
    assert len({r.request.workflow_id for r in records.values()}) == 1
    assert [r.request.member_key for r in client.records(label='scan1')] == [
        '300K',
        '310K',
    ]


def test_apply_accepts_a_frame_indexed_by_member_key(
    client: Client, template: Template, scan: dict[str, DatasetRef]
) -> None:
    frame = pd.DataFrame(
        {'run': list(scan.values()), 'scale': [1.0, 5.0]}, index=list(scan)
    )
    group = apply(client, template, pinned=frame, label='scan1')
    assert [r.params['scale'] for r in group.values()] == [1.0, 5.0]
    assert client.submit_group(group)['310K'].outputs['total']['value'] == 35.0


def test_a_blank_cell_of_a_typed_frame_falls_through_to_the_template(
    client: Client, template: Template, scan: dict[str, DatasetRef]
) -> None:
    frame = pd.DataFrame(
        {'run': list(scan.values()), 'scale': [None, 5.0]}, index=list(scan)
    )
    group = apply(client, template, pinned=frame, label='scan1')
    assert [r.params['scale'] for r in group.values()] == [2.0, 5.0]
    assert [list(r.origin.pinned) for r in group.values()] == [
        ['run'],
        ['run', 'scale'],
    ]


def test_a_corrected_member_supersedes_the_batch_record(
    client: Client, template: Template, scan: dict[str, DatasetRef]
) -> None:
    first = client.submit_group(
        apply(
            client,
            template,
            pinned={k: {'run': v} for k, v in scan.items()},
            label='scan1',
        )
    )
    corrected = client.run(
        LOAD, {'run': scan['300K'], 'scale': 4.0}, label='scan1', member_key='300K'
    )
    assert [r.id for r in client.batch('scan1')] == [corrected.id, first['310K'].id]
    assert client.latest('scan1', '300K').id == corrected.id


def test_a_batch_is_refused_whole(
    client: Client, template: Template, run_ref: DatasetRef
) -> None:
    with pytest.raises(ValueError, match='needs'):
        apply(client, template, pinned={'a': {'run': run_ref}, 'b': {}}, label='scan2')
    assert client.records(label='scan2') == []
    bad = template.revise(scale='not a number')
    with pytest.raises(SubmitError):
        client.submit_group(
            apply(client, bad, pinned={'a': {'run': run_ref}}, label='scan3')
        )
    assert client.records(label='scan3') == []


# The three deliberate operations


def test_the_backlog_is_what_lies_before_a_new_rules_bound(
    client: Client, template: Template, run_file: Path, scan: dict[str, DatasetRef]
) -> None:
    rule = Rule.over(
        client.datasets(),
        name='auto',
        template=template,
        selector=Selector(match={'run': Between(low=2)}),
    )
    assert rule.selector.after == Bound.newest(client.datasets())
    assert sorted(backlog(client, rule)) == ['run:dream/2', 'run:dream/3']
    rule.exclude('run:dream/3', 'chopper was off')
    assert sorted(backlog(client, rule)) == ['run:dream/2']


def test_reprocess_offers_the_members_an_older_rule_version_made(
    client: Client, rule: Rule
) -> None:
    TriggerLoop(client, rule).run_once()
    assert reprocess(client, rule) == {}
    moved = rule.revise(template=rule.template.revise(scale=7.0))
    group = reprocess(client, moved)
    assert sorted(group) == ['pid:pid/1', 'pid:pid/2']
    assert group['pid:pid/1'].params['scale'] == 7.0
    assert group['pid:pid/1'].origin.rule == 'auto-load/v2'


def test_reprocess_carries_the_typed_values_forward(client: Client, rule: Rule) -> None:
    client.submit_group(
        apply(client, rule, client.datasets(), {'pid:pid/1': {'scale': 8.0}})
    )
    moved = rule.revise(template=rule.template.revise(scale=7.0))
    group = reprocess(client, moved)
    assert group['pid:pid/1'].params['scale'] == 8.0  # pinned, carried forward
    assert group['pid:pid/2'].params['scale'] == 7.0  # filled again


def test_an_excluded_member_is_not_reprocessed(client: Client, rule: Rule) -> None:
    TriggerLoop(client, rule).run_once()
    moved = rule.revise(template=rule.template.revise(scale=7.0))
    moved.exclude('pid:pid/1', 'chopper was off')
    assert sorted(reprocess(client, moved)) == ['pid:pid/2']


def test_shadowed_reports_a_typed_value_whose_fill_changed(
    client: Client, rule: Rule, samples: FakeDatasetSource
) -> None:
    lookup = Lookup(
        name='by-sample',
        entries=(
            LookupEntry(
                name='vanadium',
                match={'sample': Like(pattern='van*')},
                fills={'scale': 3.0},
            ),
        ),
    )
    with_lookup = rule.model_copy(update={'lookup': lookup})
    client.submit_group(
        apply(
            client,
            with_lookup,
            client.datasets(),
            {'pid:pid/1': {'scale': 3.0}, 'pid:pid/2': {'scale': 1.0}},
        )
    )
    revised_lookup = lookup.model_copy(
        update={
            'entries': (
                LookupEntry(
                    name='vanadium',
                    match={'sample': Like(pattern='van*')},
                    fills={'scale': 9.0},
                ),
            )
        }
    )
    revised = with_lookup.revise(lookup=revised_lookup)
    frame = shadowed(client, revised, with_lookup)
    assert list(frame.index) == ['pid:pid/1']
    row = frame.loc['pid:pid/1']
    assert row['field'] == 'scale'
    assert row['pinned'] == 3.0
    assert row['was'] == 3.0
    assert row['now'] == 9.0


def test_shadowed_is_empty_when_nothing_is_stale(
    client: Client, rule: Rule, samples: FakeDatasetSource
) -> None:
    TriggerLoop(client, rule).run_once()
    empty = shadowed(client, rule, rule)
    assert list(empty.columns) == ['field', 'pinned', 'was', 'now']
    assert empty.empty


def test_retry_offers_the_members_whose_latest_record_failed(
    client: Client, template: Template, tmp_path: Path
) -> None:
    client.backend.sources.append(
        FakeDatasetSource(
            Dataset(path=tmp_path / 'gone.h5', pid='pid/9'), locates=False
        )
    )
    rule = Rule(name='auto', template=template)
    TriggerLoop(client, rule).run_once()
    stuck = client.records(label='auto', member_key='pid:pid/9')
    assert [r.failure.kind for r in stuck] == ['missing-dataset']
    assert sorted(retry(client, rule)) == ['pid:pid/9']
    assert retry(client, rule, label='nothing') == {}


# The trigger loop


def test_the_loops_five_clauses_are_answered_for_one_dataset(
    client: Client, rule: Rule, samples: FakeDatasetSource
) -> None:
    vanadium, sio2 = client.datasets()[-2:]
    assert trigger_status(client, rule, vanadium).fires

    narrow = rule.model_copy(
        update={'selector': Selector(match={'sample': Like(pattern='sio*')})}
    )
    assert trigger_status(client, narrow, vanadium).reason == (
        'the selector does not match'
    )

    late = rule.model_copy(update={'selector': Selector(after=Bound(run=11))})
    assert not trigger_status(client, late, vanadium).fires
    assert trigger_status(client, late, sio2).fires

    rule.exclude(str(vanadium.ref), 'chopper was off')
    assert 'chopper was off' in trigger_status(client, rule, vanadium).reason

    rule.active = False
    assert trigger_status(client, rule, sio2).reason == 'rule auto-load is paused'
    rule.active = True

    TriggerLoop(client, rule).run_once()
    assert trigger_status(client, rule, sio2).reason == '1 record(s) under the label'


def test_the_loop_fires_once_per_dataset_and_a_restart_changes_nothing(
    client: Client, rule: Rule, samples: FakeDatasetSource, tmp_path: Path
) -> None:
    loop = TriggerLoop(client, rule)
    fired = loop.run_once()
    assert [r.request.member_key for r in fired] == ['pid:pid/1', 'pid:pid/2']
    assert [r.status for r in fired] == [Status.COMPLETED] * 2
    assert [r.request.label for r in fired] == ['auto-load'] * 2
    assert fired[0].request.origin.rule == 'auto-load/v1'
    assert loop.run_once() == []

    # A second loop over the same store knows nothing and fires on nothing.
    assert TriggerLoop(client, rule).run_once() == []

    samples.add(
        Dataset(
            path=write_run(tmp_path / 's2.h5', [5.0]),
            pid='pid/3',
            run=13,
            metadata={'sample': 'sio2'},
        )
    )
    assert [r.request.member_key for r in loop.run_once()] == ['pid:pid/3']


def test_a_paused_rule_fires_on_what_arrived_when_it_is_resumed(
    client: Client, rule: Rule
) -> None:
    rule.active = False
    loop = TriggerLoop(client, rule)
    assert loop.run_once() == []
    rule.active = True
    assert len(loop.run_once()) == 2


def test_a_refusal_is_visible_and_fires_no_record(
    client: Client, tmp_path: Path
) -> None:
    bad = Template(name='bad', spec=REBIN.id, params={'bins': 0}, blanks=('data',))
    client.backend.sources.append(
        FakeDatasetSource(Dataset(path=write_run(tmp_path / 'r1.h5', [1.0])))
    )
    loop = TriggerLoop(client, Rule(name='bad', template=bad))
    assert loop.run_once() == []
    assert any('bins' in refusal for refusal in loop.refusals.values())


def test_a_failure_the_policy_names_is_retried_up_to_the_limit(
    client: Client, template: Template, tmp_path: Path
) -> None:
    client.backend.sources.append(
        FakeDatasetSource(
            Dataset(path=tmp_path / 'gone.h5', pid='pid/9'), locates=False
        )
    )
    rule = Rule(
        name='auto',
        template=template,
        retry=RetryPolicy(kinds=('missing-dataset',), limit=3),
    )
    loop = TriggerLoop(client, rule)
    for _ in range(4):
        loop.run_once()
    assert len(client.records(label='auto', member_key='pid:pid/9')) == 3
    assert (
        'retried 3 times' in trigger_status(client, rule, client.datasets()[-1]).reason
    )


def test_a_failure_the_policy_does_not_name_is_not_retried(
    client: Client, template: Template, tmp_path: Path
) -> None:
    client.backend.sources.append(
        FakeDatasetSource(
            Dataset(path=tmp_path / 'gone.h5', pid='pid/9'), locates=False
        )
    )
    loop = TriggerLoop(client, Rule(name='auto', template=template))
    loop.run_once()
    loop.run_once()
    assert len(client.records(label='auto', member_key='pid:pid/9')) == 1


# A rule's label is reserved


def test_a_bare_request_under_a_reserved_label_is_refused(
    client: Client, rule: Rule, run_ref: DatasetRef
) -> None:
    TriggerLoop(client, rule)  # reserves 'auto-load' for the rule
    report = client.validate(
        client.request(rule.template.spec, {'run': run_ref}, label=rule.name)
    )
    assert not report.ok
    assert any(
        f"label {rule.name!r} is reserved for rule {rule.name!r}; apply the rule "
        "instead" in e
        for e in report.errors
    )
    with pytest.raises(SubmitError, match='reserved for rule'):
        client.submit(
            client.request(rule.template.spec, {'run': run_ref}, label=rule.name)
        )


def test_apply_on_the_rule_passes_its_own_reservation(
    client: Client, rule: Rule, samples: FakeDatasetSource
) -> None:
    """A person adding a dataset the selector missed goes through ``apply``,
    which sets ``origin.rule``, so the reservation lets it through."""
    TriggerLoop(client, rule)
    missed = client.datasets()[-1]
    records = client.submit_group(apply(client, rule, [missed]))
    assert records[str(missed.ref)].status == Status.COMPLETED
    assert str(missed.ref) in batch_table(client, rule).index


def test_the_reservation_does_not_affect_other_labels(
    client: Client, rule: Rule, run_ref: DatasetRef
) -> None:
    TriggerLoop(client, rule)
    record = client.run(LOAD, {'run': run_ref}, label='unrelated')
    assert record.status == Status.COMPLETED


# A series


def series_rule(lookup: Lookup | None = None) -> Rule:
    """A rule whose request per sample sums every run of that sample."""
    return Rule(
        name='series',
        template=Template(
            name='normalize',
            spec=NORMALIZE.id,
            params={'floor': 1.5, 'scale': 2.0},
            blanks=('runs',),
        ),
        lookup=lookup,
        series=Series(key='sample'),
    )


def sample(path: Path, values: list[float], pid: str, **metadata: str) -> Dataset:
    return Dataset(
        path=write_run(path, values),
        pid=pid,
        metadata={'sample': 'sio2'} | metadata,
    )


def listed(record: RunRecord) -> list[str]:
    """The runs a series request names."""
    return [str(as_ref(run)) for run in record.request.params['runs']]


def test_each_arrival_submits_one_request_over_every_run_of_its_series(
    client: Client, tmp_path: Path
) -> None:
    source = FakeDatasetSource(sample(tmp_path / 'a.h5', [1.0, 2.0, 3.0, 4.0], 'pid/1'))
    client.backend.sources.append(source)
    loop = TriggerLoop(client, series_rule())
    (first,) = client.wait(loop.run_once())
    assert first.request.member_key == 'sio2'
    assert listed(first) == ['pid:pid/1']
    assert first.request.outputs == ('normalized',)

    source.add(sample(tmp_path / 'b.h5', [2.0, 2.0, 2.0, 2.0], 'pid/2'))
    (second,) = client.wait(loop.run_once())
    assert second.status == Status.COMPLETED, second.failure
    assert listed(second) == ['pid:pid/1', 'pid:pid/2']
    # Successive requests supersede each other under the series value.
    assert second.supersedes == first.id
    assert [r.id for r in client.batch('series')] == [second.id]
    assert loop.run_once() == []


def test_a_series_request_equals_a_sum_over_its_runs(
    client: Client, tmp_path: Path
) -> None:
    source = FakeDatasetSource()
    client.backend.sources.append(source)
    loop = TriggerLoop(client, series_rule())
    for i, values in enumerate([[1.0, 2.0], [2.0, 2.0], [4.0, 3.0]], start=1):
        source.add(sample(tmp_path / f'{i}.h5', values, f'pid/{i}'))
        (latest,) = client.wait(loop.run_once())
    runs = [dataset_ref(pid=f'pid/{i}') for i in (1, 2, 3)]
    (at_once,) = client.wait(
        [client.run(NORMALIZE, {'runs': runs, 'floor': 1.5, 'scale': 2.0})]
    )
    assert latest.status == Status.COMPLETED, latest.failure
    assert sc.identical(
        client.output(latest, 'normalized'), client.output(at_once, 'normalized')
    )


def test_a_run_acquired_again_is_counted_once(client: Client, tmp_path: Path) -> None:
    """A series request lists each run once, so applying the rule again is safe."""
    first = sample(tmp_path / 'a.h5', [1.0, 2.0, 3.0, 4.0], 'pid/1')
    source = FakeDatasetSource(first)
    client.backend.sources.append(source)
    rule = series_rule()
    loop = TriggerLoop(client, rule)
    client.wait(loop.run_once())
    source.add(sample(tmp_path / 'b.h5', [2.0, 2.0, 2.0, 2.0], 'pid/2'))
    client.wait(loop.run_once())

    write_run(first.path, [5.0, 5.0, 5.0, 5.0])
    (again,) = client.wait(client.submit_group(apply(client, rule, [first])).values())
    assert listed(again) == ['pid:pid/1', 'pid:pid/2']
    assert again.status == Status.COMPLETED, again.failure
    # 5 + 2 counts per point over a denominator of 20 + 8, scaled by 2.
    normalized = client.output(again, 'normalized')
    assert list(normalized.values) == [(5.0 + 2.0) / 28.0 * 2.0] * 4


def test_an_excluded_run_leaves_the_series(client: Client, tmp_path: Path) -> None:
    source = FakeDatasetSource(sample(tmp_path / 'a.h5', [1.0, 2.0, 3.0, 4.0], 'pid/1'))
    client.backend.sources.append(source)
    rule = series_rule()
    loop = TriggerLoop(client, rule)
    client.wait(loop.run_once())
    rule.exclusions['pid:pid/1'] = 'bad sample alignment'

    source.add(sample(tmp_path / 'b.h5', [2.0, 2.0, 2.0, 2.0], 'pid/2'))
    (latest,) = client.wait(loop.run_once())
    assert listed(latest) == ['pid:pid/2']
    # 2 counts per point over the remaining run's total of 8, scaled by 2.
    normalized = client.output(latest, 'normalized')
    assert list(normalized.values) == [2.0 / 8.0 * 2.0] * 4


def test_runs_a_lookup_fills_otherwise_refuse_the_series(
    client: Client, tmp_path: Path
) -> None:
    """One request has one value per parameter, so every run is filled alike."""
    source = FakeDatasetSource(sample(tmp_path / 'a.h5', [1.0, 2.0], 'pid/1'))
    client.backend.sources.append(source)
    floors = Lookup(
        name='floors',
        entries=(
            LookupEntry(
                name='noisy',
                match={'mode': Like(pattern='noisy')},
                fills={'floor': 3.0},
            ),
        ),
    )
    loop = TriggerLoop(client, series_rule(floors))
    client.wait(loop.run_once())

    source.add(sample(tmp_path / 'b.h5', [2.0, 2.0], 'pid/2', mode='noisy'))
    assert loop.run_once() == []
    assert 'filled otherwise' in loop.refusals['series pid:pid/2']


def test_a_series_with_a_failed_run_is_retried_without_it_once_excluded(
    client: Client, tmp_path: Path
) -> None:
    """A run that cannot be read fails the sum, visibly, until someone excludes it."""
    client.backend.sources.append(
        FakeDatasetSource(
            Dataset(
                path=tmp_path / 'gone.h5', pid='pid/1', metadata={'sample': 'sio2'}
            ),
            locates=False,
        )
    )
    rule = series_rule()
    loop = TriggerLoop(client, rule)
    (failed,) = client.wait(loop.run_once())
    assert failed.failure.kind == 'missing-dataset'

    client.backend.sources.append(
        FakeDatasetSource(sample(tmp_path / 'b.h5', [2.0, 2.0], 'pid/2'))
    )
    (still,) = client.wait(loop.run_once())
    assert listed(still) == ['pid:pid/1', 'pid:pid/2']
    assert still.failure.kind == 'missing-dataset'

    rule.exclusions['pid:pid/1'] = 'file lost'
    (recovered,) = client.wait(client.submit_group(retry(client, rule)).values())
    assert listed(recovered) == ['pid:pid/2']
    assert recovered.status == Status.COMPLETED, recovered.failure


# The batch table


def test_the_batch_table_is_a_query_over_the_records(
    client: Client, rule: Rule
) -> None:
    lookup = Lookup(
        name='by-sample',
        entries=(LookupEntry(name='vanadium', match={'sample': Like(pattern='van*')}),),
    )
    with_lookup = rule.model_copy(update={'lookup': lookup})
    client.submit_group(
        apply(
            client,
            with_lookup,
            client.datasets()[-2:],
            {'pid:pid/2': {'scale': 5.0}},
        )
    )
    with_lookup.exclude('pid:pid/9', 'chopper was off')
    table = batch_table(client, with_lookup)
    assert table.index.name == 'member'
    assert list(table.index) == ['pid:pid/1', 'pid:pid/2', 'pid:pid/9']
    assert list(table.loc['pid:pid/1', ['rule', 'lookup', 'entry', 'status']]) == [
        'auto-load/v1',
        'by-sample/v1',
        'vanadium',
        'completed',
    ]
    assert table.loc['pid:pid/9', 'reason'] == 'chopper was off'


def test_the_batch_table_shows_the_values_that_differ_per_member(
    client: Client, rule: Rule
) -> None:
    client.submit_group(
        apply(client, rule, client.datasets()[-2:], {'pid:pid/2': {'scale': 5.0}})
    )
    table = batch_table(client, rule)
    # The template's blank, which the rule filled and nobody pinned.
    assert list(table['run']) == [dataset_ref(pid='pid/1'), dataset_ref(pid='pid/2')]
    # A pinned field shows the template's value for the members that did not pin it.
    assert list(table['scale']) == [2.0, 5.0]
    assert list(table['pinned']) == ['', 'scale']


class Window(BaseModel):
    low: float = 0.0
    high: float = 1.0


class WindowedParams(LoadParams):
    window: Window = Window()


WINDOWED = WorkflowSpec(
    name='windowed',
    version=1,
    title='Windowed load',
    description='Load with a parameter that is a model.',
    params=WindowedParams,
    outputs=LoadOutputs,
)


@pytest.fixture
def windowed(client: Client, samples: FakeDatasetSource) -> Rule:
    """A rule whose template holds a model, applied with one member's pinned values."""
    client.backend.registry.bind(WINDOWED, load_workflow)
    rule = Rule(
        name='windowed',
        template=Template(
            name='windowed',
            spec=WINDOWED.id,
            params={'window': Window()},
            blanks=('run',),
        ),
        selector=Selector(match={'sample': Like(pattern='*')}),
    )
    pinned = {'pid:pid/2': {'window': Window(low=0.5)}}
    client.submit_group(apply(client, rule, client.datasets()[-2:], pinned))
    return rule


def test_the_batch_table_shows_a_model_as_one_column_per_leaf(
    client: Client, windowed: Rule
) -> None:
    table = batch_table(client, windowed)
    assert 'window' not in table
    assert list(table['window.low']) == [0.0, 0.5]
    assert list(table['window.high']) == [1.0, 1.0]
    assert list(table['pinned']) == ['', 'window']


def test_shadowed_names_the_leaves_of_a_typed_model_whose_fill_changed(
    client: Client, windowed: Rule
) -> None:
    revised = windowed.revise(
        template=windowed.template.revise(window=Window(high=2.0))
    )
    frame = shadowed(client, revised, windowed)
    assert list(frame.index) == ['pid:pid/2']
    assert list(frame.iloc[0]) == ['window.high', 1.0, 1.0, 2.0]


def test_the_batch_table_of_a_label_needs_no_rule(
    client: Client, template: Template, scan: dict[str, DatasetRef]
) -> None:
    client.submit_group(
        apply(
            client,
            template,
            pinned={k: {'run': v} for k, v in scan.items()},
            label='scan1',
        )
    )
    table = batch_table(client, 'scan1')
    assert list(table.index) == ['300K', '310K']
    assert set(table['template']) == {'load-defaults/v1'}
    assert table.loc['300K', 'run'] == scan['300K']


def test_the_dataset_table_joins_onto_a_rules_batch_table(
    client: Client, rule: Rule
) -> None:
    client.submit_group(apply(client, rule, client.datasets()[-2:]))
    datasets = dataset_table(client)
    assert datasets.index.name == 'dataset'
    table = batch_table(client, rule).join(datasets['sample'])
    assert table.index.name == 'member'
    assert list(table['sample']) == [d.fields['sample'] for d in client.datasets()[-2:]]


# An as-of fill


@pytest.fixture
def cans_and_samples(client: Client, tmp_path: Path) -> FakeDatasetSource:
    """A stray sample before any can, two cans, two samples, a third can, and
    two more samples -- in run-number order."""

    def dataset(name: str, run: int, role: str) -> Dataset:
        return Dataset(
            path=write_run(tmp_path / f'{name}.h5', [1.0, 2.0]),
            instrument='loki',
            run=run,
            metadata={'role': role},
        )

    source = FakeDatasetSource(
        dataset('sample0', 1, 'sample'),
        dataset('can1', 2, 'can'),
        dataset('can2', 3, 'can'),
        dataset('sample1', 4, 'sample'),
        dataset('sample2', 5, 'sample'),
        dataset('can3', 6, 'can'),
        dataset('sample3', 7, 'sample'),
        dataset('sample4', 8, 'sample'),
    )
    client.backend.sources.append(source)
    return source


@pytest.fixture
def as_of_rule(cans_and_samples: FakeDatasetSource) -> Rule:
    return Rule(
        name='subtract',
        template=Template(
            name='subtract-defaults',
            spec=SUBTRACT.id,
            blanks=('sample', 'can'),
            dataset_field='sample',
        ),
        lookup=Lookup(
            name='cans',
            entries=(
                LookupEntry(
                    name='can',
                    fills={'can': AsOf(match={'role': Like(pattern='can')})},
                ),
            ),
        ),
        selector=Selector(match={'role': Like(pattern='sample')}),
    )


def _can(record: RunRecord) -> str:
    return as_ref(record.request.params['can']).dataset


def test_an_as_of_fill_is_the_nearest_earlier_matching_dataset(
    client: Client, as_of_rule: Rule, cans_and_samples: FakeDatasetSource
) -> None:
    loop = TriggerLoop(client, as_of_rule)
    fired = loop.run_once()
    assert [r.request.member_key for r in fired] == [
        'run:loki/4',
        'run:loki/5',
        'run:loki/7',
        'run:loki/8',
    ]
    assert [r.status for r in fired] == [Status.COMPLETED] * 4
    by_member = {r.request.member_key: r for r in fired}
    assert _can(by_member['run:loki/4']) == 'run:loki/3'
    assert _can(by_member['run:loki/5']) == 'run:loki/3'
    assert _can(by_member['run:loki/7']) == 'run:loki/6'
    assert _can(by_member['run:loki/8']) == 'run:loki/6'

    refusal = loop.refusals['subtract run:loki/1']
    assert 'run:loki/1: no dataset matching' in refusal
    assert "before it for 'can'" in refusal


def test_backlog_and_reprocess_resolve_the_same_can_as_the_live_loop(
    client: Client, as_of_rule: Rule, cans_and_samples: FakeDatasetSource
) -> None:
    """The as-of fill is anchored to the member's own dataset, so it is stable
    under a reprocess, unlike a fill resolved against the time of resolution."""
    live = {r.request.member_key: r for r in TriggerLoop(client, as_of_rule).run_once()}

    later = Rule.over(
        client.datasets(),
        name='subtract-backlog',
        template=as_of_rule.template,
        lookup=as_of_rule.lookup,
        selector=Selector(
            match={'role': Like(pattern='sample'), 'run': Between(low=2)}
        ),
    )
    back = client.submit_group(backlog(client, later))
    assert set(back) == set(live)
    for member, record in back.items():
        assert _can(record) == _can(live[member])

    moved = as_of_rule.revise(template=as_of_rule.template.revise())
    again = client.submit_group(reprocess(client, moved))
    assert set(again) == set(live)
    for member, record in again.items():
        assert _can(record) == _can(live[member])
