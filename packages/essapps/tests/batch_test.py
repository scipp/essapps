# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Apply, the deliberate operations, the trigger loop, and the batch table (D14)."""

from pathlib import Path

import pandas as pd
import pytest

from ess.apps.backend import SubmitError
from ess.apps.batch import (
    TriggerLoop,
    apply,
    backlog,
    batch_table,
    reprocess,
    rerun,
    trigger_status,
)
from ess.apps.client import Client
from ess.apps.examples import LOAD, NORMALIZE, REBIN, write_run
from ess.apps.records import Status
from ess.apps.rules import (
    Between,
    Bound,
    Like,
    Lookup,
    LookupEntry,
    RetryPolicy,
    Rule,
    Selector,
    Series,
    Template,
)
from ess.apps.sources import Dataset
from ess.apps.spec import DatasetRef, Ref
from ess.apps.testing import FakeDatasetSource


@pytest.fixture
def scan(datasets: Path) -> dict[str, DatasetRef]:
    """Two more runs in the folder the client's dataset source reads."""
    write_run(datasets / 'dream_2.h5', [1.0, 2.0])
    write_run(datasets / 'dream_3.h5', [3.0, 4.0])
    return {
        '300K': DatasetRef(instrument='dream', run=2),
        '310K': DatasetRef(instrument='dream', run=3),
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
    client.sources.append(source)
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
        {'dataset:pid/2': {'scale': 4.0}},
        lookup=lookup,
        label='ladder',
    )
    scales = {key: request.params['scale'] for key, request in group.items()}
    assert scales['dataset:pid/1'] == 3.0  # the lookup entry over the template
    assert scales['dataset:pid/2'] == 4.0  # what was typed over both
    assert scales['dataset:dream/1'] == 2.0  # the template, matching no entry
    typed = group['dataset:pid/2'].submission
    assert typed.typed == {'scale': 4.0}
    assert typed.entry == 'rest'
    assert typed.template == 'load-defaults/v1'
    assert group['dataset:pid/1'].params['run'] == DatasetRef(pid='pid/1')


def test_apply_without_datasets_is_the_batch_form(
    client: Client, template: Template, scan: dict[str, DatasetRef]
) -> None:
    group = apply(
        client, template, typed={k: {'run': v} for k, v in scan.items()}, label='scan1'
    )
    records = client.submit_group(group)
    assert {k: r.status for k, r in records.items()} == dict.fromkeys(
        scan, Status.COMPLETED
    )
    assert records['310K'].outputs['total']['value'] == 14.0
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
    group = apply(client, template, typed=frame, label='scan1')
    assert [r.params['scale'] for r in group.values()] == [1.0, 5.0]
    assert client.submit_group(group)['310K'].outputs['total']['value'] == 35.0


def test_a_corrected_member_supersedes_the_batch_record(
    client: Client, template: Template, scan: dict[str, DatasetRef]
) -> None:
    first = client.submit_group(
        apply(
            client,
            template,
            typed={k: {'run': v} for k, v in scan.items()},
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
        apply(client, template, typed={'a': {'run': run_ref}, 'b': {}}, label='scan2')
    assert client.records(label='scan2') == []
    bad = template.revise(scale='not a number')
    with pytest.raises(SubmitError):
        client.submit_group(
            apply(client, bad, typed={'a': {'run': run_ref}}, label='scan3')
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
    assert sorted(backlog(client, rule)) == ['dataset:dream/2', 'dataset:dream/3']
    rule.exclude('dataset:dream/3', 'chopper was off')
    assert sorted(backlog(client, rule)) == ['dataset:dream/2']


def test_reprocess_offers_the_members_an_older_rule_version_made(
    client: Client, rule: Rule
) -> None:
    TriggerLoop(client, rule).run_once()
    assert reprocess(client, rule) == {}
    moved = rule.revise(template=rule.template.revise(scale=7.0))
    group = reprocess(client, moved)
    assert sorted(group) == ['dataset:pid/1', 'dataset:pid/2']
    assert group['dataset:pid/1'].params['scale'] == 7.0
    assert group['dataset:pid/1'].submission.rule == 'auto-load/v2'


def test_reprocess_carries_the_typed_values_forward(client: Client, rule: Rule) -> None:
    client.submit_group(
        apply(client, rule, client.datasets(), {'dataset:pid/1': {'scale': 8.0}})
    )
    moved = rule.revise(template=rule.template.revise(scale=7.0))
    group = reprocess(client, moved)
    assert group['dataset:pid/1'].params['scale'] == 8.0  # typed, carried forward
    assert group['dataset:pid/2'].params['scale'] == 7.0  # filled again


def test_an_excluded_member_is_not_reprocessed(client: Client, rule: Rule) -> None:
    TriggerLoop(client, rule).run_once()
    moved = rule.revise(template=rule.template.revise(scale=7.0))
    moved.exclude('dataset:pid/1', 'chopper was off')
    assert sorted(reprocess(client, moved)) == ['dataset:pid/2']


def test_rerun_offers_the_members_with_no_completed_record(
    client: Client, template: Template, tmp_path: Path
) -> None:
    client.sources.append(
        FakeDatasetSource(
            Dataset(path=tmp_path / 'gone.h5', pid='pid/9'), locates=False
        )
    )
    rule = Rule(name='auto', template=template)
    TriggerLoop(client, rule).run_once()
    stuck = client.records(label='auto', member_key='dataset:pid/9')
    assert [r.failure.kind for r in stuck] == ['missing-dataset']
    assert sorted(rerun(client, rule)) == ['dataset:pid/9']
    assert rerun(client, rule, label='nothing') == {}


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
    assert [r.request.member_key for r in fired] == ['dataset:pid/1', 'dataset:pid/2']
    assert [r.status for r in fired] == [Status.COMPLETED] * 2
    assert [r.request.label for r in fired] == ['auto-load'] * 2
    assert fired[0].request.submission.rule == 'auto-load/v1'
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
    assert [r.request.member_key for r in loop.run_once()] == ['dataset:pid/3']


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
    client.sources.append(
        FakeDatasetSource(Dataset(path=write_run(tmp_path / 'r1.h5', [1.0])))
    )
    loop = TriggerLoop(client, Rule(name='bad', template=bad))
    assert loop.run_once() == []
    assert any('bins' in refusal for refusal in loop.refusals.values())


def test_a_failure_the_policy_names_is_retried_up_to_the_limit(
    client: Client, template: Template, tmp_path: Path
) -> None:
    client.sources.append(
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
    assert len(client.records(label='auto', member_key='dataset:pid/9')) == 3
    assert (
        'retried 3 times' in trigger_status(client, rule, client.datasets()[-1]).reason
    )


def test_a_failure_the_policy_does_not_name_is_not_retried(
    client: Client, template: Template, tmp_path: Path
) -> None:
    client.sources.append(
        FakeDatasetSource(
            Dataset(path=tmp_path / 'gone.h5', pid='pid/9'), locates=False
        )
    )
    loop = TriggerLoop(client, Rule(name='auto', template=template))
    loop.run_once()
    loop.run_once()
    assert len(client.records(label='auto', member_key='dataset:pid/9')) == 1


# A series


def test_each_arrival_of_a_series_submits_a_member_and_a_chained_combine(
    client: Client, tmp_path: Path
) -> None:
    source = FakeDatasetSource(
        Dataset(
            path=write_run(tmp_path / 'a.h5', [1.0, 2.0, 3.0, 4.0]),
            pid='pid/1',
            metadata={'sample': 'sio2'},
        )
    )
    client.sources.append(source)
    rule = Rule(
        name='series',
        template=Template(
            name='normalize',
            spec=NORMALIZE.id,
            params={'floor': 1.5},
            blanks=('run',),
        ),
        series=Series(key='sample', finalize={'scale': 2.0}),
    )
    loop = TriggerLoop(client, rule)
    first = loop.run_once()
    assert [r.request.stage for r in first] == ['contribute', 'combine']
    assert [r.request.member_key for r in first] == ['dataset:pid/1', 'sio2']
    assert first[1].request.contributions == [
        Ref(record=first[0].id, output='contribution')
    ]
    client.wait(first)

    source.add(
        Dataset(
            path=write_run(tmp_path / 'b.h5', [2.0, 2.0, 2.0, 2.0]),
            pid='pid/2',
            metadata={'sample': 'sio2'},
        )
    )
    member, combine = loop.run_once()
    client.wait([member, combine])
    assert combine.request.contributions == [
        Ref(record=first[1].id, output='contribution'),
        Ref(record=member.id, output='contribution'),
    ]
    assert combine.status == Status.COMPLETED, combine.failure
    # Successive combines supersede each other under the series value.
    assert client.latest('series', 'sio2').id == combine.id
    assert [r.request.member_key for r in client.batch('series')] == [
        'dataset:pid/1',
        'dataset:pid/2',
        'sio2',
    ]


def test_a_series_whose_latest_combine_failed_does_not_freeze(
    client: Client, tmp_path: Path
) -> None:
    """The next arrival chains onto the failed combine, so it must end, not wait."""
    client.sources.append(
        FakeDatasetSource(
            Dataset(
                path=tmp_path / 'gone.h5', pid='pid/1', metadata={'sample': 'sio2'}
            ),
            locates=False,
        )
    )
    rule = Rule(
        name='series',
        template=Template(
            name='normalize', spec=NORMALIZE.id, params={'floor': 1.5}, blanks=('run',)
        ),
        series=Series(key='sample', finalize={'scale': 2.0}),
    )
    loop = TriggerLoop(client, rule)
    member, combine = client.wait(loop.run_once())
    assert member.failure.kind == 'missing-dataset'
    assert combine.status == Status.FAILED

    client.sources.append(
        FakeDatasetSource(
            Dataset(
                path=write_run(tmp_path / 'b.h5', [2.0, 2.0]),
                pid='pid/2',
                metadata={'sample': 'sio2'},
            )
        )
    )
    next_member, next_combine = client.wait(loop.run_once())
    assert next_member.status == Status.COMPLETED, next_member.failure
    assert next_combine.status == Status.FAILED
    assert combine.id in next_combine.failure.message


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
            {'dataset:pid/2': {'scale': 5.0}},
        )
    )
    with_lookup.exclude('dataset:pid/9', 'chopper was off')
    table = batch_table(client, with_lookup)
    assert table.index.name == 'member'
    assert list(table.index) == ['dataset:pid/1', 'dataset:pid/2', 'dataset:pid/9']
    assert list(table.loc['dataset:pid/1', ['rule', 'entry', 'status']]) == [
        'auto-load/v1',
        'vanadium',
        'completed',
    ]
    assert table.loc['dataset:pid/2', 'scale'] == 5.0
    assert table.loc['dataset:pid/9', 'reason'] == 'chopper was off'


def test_the_batch_table_of_a_label_needs_no_rule(
    client: Client, template: Template, scan: dict[str, DatasetRef]
) -> None:
    client.submit_group(
        apply(
            client,
            template,
            typed={k: {'run': v} for k, v in scan.items()},
            label='scan1',
        )
    )
    table = batch_table(client, 'scan1')
    assert list(table.index) == ['300K', '310K']
    assert set(table['template']) == {'load-defaults/v1'}
