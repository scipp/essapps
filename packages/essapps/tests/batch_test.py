# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Apply, the deliberate operations, the trigger loop, and the batch table.

See docs/developer/rules.md.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pandas as pd
import pytest
import scipp as sc
from pydantic import BaseModel, Field

from ess.apps.backend import SubmitError
from ess.apps.batch import (
    TriggerLoop,
    TriggerStatus,
    apply,
    backlog,
    batch_table,
    candidates,
    dataset_table,
    reprocess,
    retry,
    shadowed,
    trigger_status,
)
from ess.apps.client import Client
from ess.apps.examples import (
    COMBINE,
    CONTRIBUTE,
    EXPORT,
    FLOORED,
    LOAD,
    NORMALIZE,
    REBIN,
    SUBTRACT,
    SUM,
    LoadOutputs,
    LoadParams,
    load_workflow,
    write_run,
)
from ess.apps.records import RunRecord, Status, Template
from ess.apps.rules import (
    Between,
    Bound,
    Complete,
    Follows,
    Like,
    Lookup,
    LookupEntry,
    Nearest,
    RetryPolicy,
    Rule,
    Selector,
    Series,
)
from ess.apps.sources import Dataset, FieldExtractor, FolderSource
from ess.apps.spec import DatasetRef, OpaqueFile, WorkflowSpec, as_ref, dataset_ref
from ess.apps.testing import FakeDatasetSource


@pytest.fixture
def scan(datasets: Path) -> dict[str, DatasetRef]:
    """Two more runs in the folder the client's dataset source reads."""
    write_run(datasets / 'dream_2.h5', [1.0, 2.0], uuid='dream-2')
    write_run(datasets / 'dream_3.h5', [3.0, 4.0], uuid='dream-3')
    return {'300K': dataset_ref(uuid='dream-2'), '310K': dataset_ref(uuid='dream-3')}


@pytest.fixture
def samples(client: Client, tmp_path: Path) -> FakeDatasetSource:
    """Datasets carrying the fields a lookup and a selector match on."""
    source = FakeDatasetSource(
        Dataset(
            proposals=['p1'],
            path=write_run(tmp_path / 'v1.h5', [1.0, 2.0]),
            pid='pid/1',
            run=11,
            metadata={'sample': 'vanadium', 'angle': 0.4},
        ),
        Dataset(
            proposals=['p1'],
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
    client: Client,
    template: Template,
    run_ref: DatasetRef,
    samples: FakeDatasetSource,
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
    assert scales[str(run_ref)] == 2.0  # the template, matching no entry
    origin = group['pid:pid/2'].origin
    assert origin.pinned == {'scale': 4.0}
    assert origin.entries == {'pid:pid/2': 'rest'}
    assert origin.template == 'load-defaults/v1'
    # The template's blank is filled in params.
    assert as_ref(group['pid:pid/1'].params['run']) == dataset_ref(pid='pid/1')


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
    assert [r.request.member_key for r in client.records(label='scan1')] == [
        '300K',
        '310K',
    ]


def test_a_pinned_stand_in_is_recorded_as_the_identity_it_names(
    client: Client, template: Template, run_ref: DatasetRef
) -> None:
    """So a reprocess, which carries what was pinned, names the same dataset."""
    typed = dataset_ref(instrument='dream', run=1)
    group = apply(client, template, pinned={'m': {'run': typed}}, label='scan')
    (record,) = client.submit_group(group).values()
    assert record.request.origin.pinned == {'run': run_ref.model_dump()}
    assert record.request.params['run'] == run_ref.model_dump()


def test_a_pid_minted_after_reduction_leaves_the_member_as_it_was(
    client: Client, template: Template, tmp_path: Path
) -> None:
    uuid_only = Dataset(
        path=write_run(tmp_path / 'v9.h5', [1.0, 2.0]),
        proposals=['p1'],
        uuid='u9',
        run=9,
        metadata={'sample': 'vanadium'},
    )
    client.backend.sources.append(FakeDatasetSource(uuid_only))
    rule = Rule(
        name='auto-load',
        template=template,
        selector=Selector(match={'sample': Like(pattern='*')}),
    )
    (first,) = TriggerLoop(client, rule).run_once()
    client.backend.sources[-1] = FakeDatasetSource(replace(uuid_only, pid='pid/9'))
    assert TriggerLoop(client, rule).run_once() == []
    moved = rule.revise(template=rule.template.revise(scale=7.0))
    assert list(reprocess(client, moved)) == ['uuid:u9']
    assert client.recompute(first).status == Status.COMPLETED


def test_a_group_from_apply_shares_a_held_stage_and_a_plain_mapping_does_not(
    client: Client, scan: dict[str, DatasetRef]
) -> None:
    sums = Template(
        spec=NORMALIZE, params={'floor': 1.5, 'scale': 2.0}, blanks=('runs',)
    )
    runs = list(scan.values())
    pinned = {'one': {'runs': runs[:1]}, 'both': {'runs': runs}}
    group = apply(client, sums, pinned=pinned, label='sums')
    assert group.vary == ('runs',)
    held = client.submit_group(group)
    assert held['both'].reused
    plain = client.submit_group(dict(apply(client, sums, pinned=pinned, label='p')))
    assert not any(r.reused for r in plain.values())
    assert held['both'].request.params == plain['both'].request.params


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
    assert sorted(backlog(client, rule)) == ['uuid:dream-2', 'uuid:dream-3']
    rule.exclude('uuid:dream-3', 'chopper was off')
    assert sorted(backlog(client, rule)) == ['uuid:dream-2']


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
            Dataset(proposals=['p1'], path=tmp_path / 'gone.h5', pid='pid/9'),
            locates=False,
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
    assert trigger_status(client, rule, vanadium).state == 'fires'

    narrow = rule.model_copy(
        update={'selector': Selector(match={'sample': Like(pattern='sio*')})}
    )
    assert trigger_status(client, narrow, vanadium).reason == (
        'the selector does not match'
    )

    late = rule.model_copy(update={'selector': Selector(after=Bound(run=11))})
    assert trigger_status(client, late, vanadium).state == 'skips'
    assert trigger_status(client, late, sio2).state == 'fires'

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
            proposals=['p1'],
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
        FakeDatasetSource(
            Dataset(
                path=write_run(tmp_path / 'r1.h5', [1.0]), proposals=['p1'], uuid='r1'
            )
        )
    )
    loop = TriggerLoop(client, Rule(name='bad', template=bad))
    assert loop.run_once() == []
    assert any('bins' in refusal for refusal in loop.refusals.values())


def test_a_failure_the_policy_names_is_retried_up_to_the_limit(
    client: Client, template: Template, tmp_path: Path
) -> None:
    client.backend.sources.append(
        FakeDatasetSource(
            Dataset(proposals=['p1'], path=tmp_path / 'gone.h5', pid='pid/9'),
            locates=False,
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
            Dataset(proposals=['p1'], path=tmp_path / 'gone.h5', pid='pid/9'),
            locates=False,
        )
    )
    loop = TriggerLoop(client, Rule(name='auto', template=template))
    loop.run_once()
    loop.run_once()
    assert len(client.records(label='auto', member_key='pid:pid/9')) == 1


def start_only(entry: Mapping[str, Any], path: Path) -> dict[str, Any]:
    return {'start': entry['start']}


def test_a_dataset_the_extractor_gives_no_usable_fields_is_refused_alone(
    client: Client, template: Template, run_ref: DatasetRef, tmp_path: Path
) -> None:
    folder = tmp_path / 'loki'
    folder.mkdir()
    for run in (1, 2):
        write_run(folder / f'loki_{run}.h5', [1.0], uuid=f'loki-{run}')
    client.backend.sources.append(
        FolderSource(
            folder,
            '*.h5',
            proposal='p1',
            journal={1: {'start': 10}, 2: {'start': '10:00'}},
            fields=FieldExtractor(start_only, order='start'),
        )
    )
    rule = Rule(name='auto', template=template)
    loop = TriggerLoop(client, rule)
    fired = loop.run_once()
    assert sorted(r.request.member_key for r in fired) == [
        str(run_ref),
        'uuid:loki-1',
    ]
    refusal = (
        "uuid:loki-2: its order field 'start' is '10:00', not a number or a datetime"
    )
    assert loop.refusals == {'auto uuid:loki-2': refusal}
    bad = {str(d.ref): d for d in client.datasets()}['uuid:loki-2']
    assert trigger_status(client, rule, bad) == TriggerStatus(
        state='skips', reason=refusal
    )


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


def series_rule(
    lookup: Lookup | None = None, fire: Literal['each'] | Complete = 'each'
) -> Rule:
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
        series=Series(key='sample', fire=fire),
    )


def sample(path: Path, values: list[float], pid: str, **metadata: str) -> Dataset:
    return Dataset(
        proposals=['p1'],
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
    # The loop submits with the template's blanks as the hint, so the session's
    # stage over the list contributes only the new run.
    assert second.reused
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


def test_a_series_lists_its_runs_in_run_order_whatever_the_arrival(
    client: Client, tmp_path: Path
) -> None:
    """A run arriving late goes into its place, not at the end."""
    source = FakeDatasetSource()
    client.backend.sources.append(source)
    loop = TriggerLoop(client, series_rule())
    for run in (1, 3, 2):
        source.add(
            Dataset(
                proposals=['p1'],
                path=write_run(tmp_path / f'{run}.h5', [1.0, 2.0]),
                uuid=f'loki-{run}',
                instrument='loki',
                run=run,
                metadata={'sample': 'sio2'},
            )
        )
        (latest,) = client.wait(loop.run_once())
    assert listed(latest) == ['uuid:loki-1', 'uuid:loki-2', 'uuid:loki-3']


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


def test_a_series_that_fires_when_complete_waits_for_its_count(
    client: Client, tmp_path: Path
) -> None:
    source = FakeDatasetSource()
    client.backend.sources.append(source)
    rule = series_rule(fire=Complete(count=3))
    loop = TriggerLoop(client, rule)
    for i in (1, 2):
        source.add(sample(tmp_path / f'{i}.h5', [1.0, 2.0], f'pid/{i}'))
        assert loop.run_once() == []
    assert loop.waiting == {
        f'series pid:pid/{i}': 'series sio2: 2 of 3 members' for i in (1, 2)
    }
    status = trigger_status(client, rule, client.datasets()[-1])
    assert status.state == 'waits'

    source.add(sample(tmp_path / '3.h5', [1.0, 2.0], 'pid/3'))
    (complete,) = client.wait(loop.run_once())
    assert listed(complete) == ['pid:pid/1', 'pid:pid/2', 'pid:pid/3']
    # A run after completion fires like any arrival.
    source.add(sample(tmp_path / '4.h5', [1.0, 2.0], 'pid/4'))
    (grown,) = client.wait(loop.run_once())
    assert listed(grown)[-1] == 'pid:pid/4'
    assert loop.waiting == {}


def test_a_series_of_fixed_roles_fires_when_every_role_is_present(
    client: Client, tmp_path: Path
) -> None:
    """A scatter run waits for its transmission run, and fires with both."""
    source = FakeDatasetSource(
        sample(tmp_path / 'a.h5', [1.0, 2.0], 'pid/1', role='scatter')
    )
    client.backend.sources.append(source)
    loop = TriggerLoop(
        client, series_rule(fire=Complete(roles=('scatter', 'transmission')))
    )
    assert loop.run_once() == []
    assert "['transmission']" in loop.waiting['series pid:pid/1']

    source.add(sample(tmp_path / 'b.h5', [1.0, 1.0], 'pid/2', role='transmission'))
    (both,) = client.wait(loop.run_once())
    assert listed(both) == ['pid:pid/1', 'pid:pid/2']


def test_an_exclusion_counts_against_completion(client: Client, tmp_path: Path) -> None:
    source = FakeDatasetSource(
        sample(tmp_path / '1.h5', [1.0, 2.0], 'pid/1'),
        sample(tmp_path / '2.h5', [1.0, 2.0], 'pid/2'),
    )
    client.backend.sources.append(source)
    rule = series_rule(fire=Complete(count=2))
    client.wait(TriggerLoop(client, rule).run_once())
    rule.exclude('pid:pid/1', 'bad sample alignment')
    group = apply(client, rule, client.datasets()[-1:])
    assert dict(group) == {}
    assert group.waiting == {'sio2': 'series sio2: 1 of 2 members'}


def test_a_series_is_complete_by_a_count_or_by_roles() -> None:
    with pytest.raises(ValueError, match='not both'):
        Complete()
    with pytest.raises(ValueError, match='not both'):
        Complete(count=2, roles=('sample',))


NOISY = LookupEntry(
    name='noisy', match={'mode': Like(pattern='noisy')}, fills={'floor': 3.0}
)
NOISY_ROW = NOISY.model_copy(update={'fills': {'runs.floor': 3.0}})


def test_each_run_of_a_series_is_filled_into_its_own_row(
    client: Client, tmp_path: Path
) -> None:
    """A fill named for a column of the rows fills the row of the run it matched."""
    source = FakeDatasetSource(sample(tmp_path / 'a.h5', [1.0, 2.0], 'pid/1'))
    client.backend.sources.append(source)
    floors = Lookup(
        name='floors',
        entries=(NOISY_ROW,),
    )
    rule = Rule(
        name='floored',
        template=Template(
            spec=FLOORED.id,
            params={'scale': 2.0},
            blanks=('runs',),
            dataset_field='runs.run',
        ),
        lookup=floors,
        series=Series(key='sample'),
    )
    loop = TriggerLoop(client, rule)
    client.wait(loop.run_once())

    source.add(sample(tmp_path / 'b.h5', [2.0, 2.0], 'pid/2', mode='noisy'))
    (second,) = client.wait(loop.run_once())
    assert second.status == Status.COMPLETED, second.failure
    assert second.request.params['runs'] == [
        {'run': {'dataset': 'pid:pid/1'}, 'floor': 0.0},
        {'run': {'dataset': 'pid:pid/2'}, 'floor': 3.0},
    ]
    assert second.request.origin.entries == {'pid:pid/2': 'noisy'}
    # Run b is cut entirely at its floor and adds only to the denominator.
    normalized = client.output(second, 'normalized')
    assert list(normalized.values) == [1.0 / 7.0 * 2.0, 2.0 / 7.0 * 2.0]


def test_runs_a_lookup_fills_otherwise_refuse_the_series(
    client: Client, tmp_path: Path
) -> None:
    """A field of the request has one value, so runs must fill it alike."""
    source = FakeDatasetSource(sample(tmp_path / 'a.h5', [1.0, 2.0], 'pid/1'))
    client.backend.sources.append(source)
    loop = TriggerLoop(client, series_rule(Lookup(name='floors', entries=(NOISY,))))
    client.wait(loop.run_once())

    source.add(sample(tmp_path / 'b.h5', [2.0, 2.0], 'pid/2', mode='noisy'))
    assert loop.run_once() == []
    assert 'one value per field' in loop.refusals['series pid:pid/2']


def floored_rule(lookup: Lookup | None, series: Series | None = None) -> Rule:
    """A rule whose datasets are the run column of the rows of ``FLOORED``."""
    return Rule(
        name='floored',
        template=Template(
            spec=FLOORED.id,
            params={'scale': 2.0},
            blanks=('runs',),
            dataset_field='runs.run',
        ),
        lookup=lookup,
        series=series,
    )


@pytest.mark.parametrize(
    ('fills', 'message'),
    [
        ({'runs.flor': 3.0}, "'runs.flor' is no column of 'runs'"),
        ({'scale.x': 3.0}, "'scale.x' is no column of 'runs'"),
        ({'runs.floor.x': 3.0}, "'runs.floor.x' is no column of 'runs'"),
        ({'flor': 3.0}, "'flor' is not a parameter of normalize-floored/v1"),
        ({'runs': []}, "'runs' fills 'runs.run', which a dataset fills"),
        ({'runs.run': {'dataset': 'pid:x'}}, "'runs.run' fills 'runs.run'"),
    ],
)
def test_a_fill_that_names_no_parameter_or_column_is_refused(
    client: Client, tmp_path: Path, fills: dict[str, Any], message: str
) -> None:
    """A row drops a column it does not declare, so a typo would leave the default."""
    dataset = sample(tmp_path / 'a.h5', [1.0, 2.0], 'pid/1', mode='noisy')
    client.backend.sources.append(FakeDatasetSource(dataset))
    lookup = Lookup(name='floors', entries=(NOISY.model_copy(update={'fills': fills}),))
    rule = floored_rule(lookup, Series(key='sample'))
    with pytest.raises(ValueError, match=re.escape(f'entry noisy: {message}')):
        apply(client, rule, [dataset])


def test_a_column_fill_is_refused_where_a_dataset_fills_no_row(
    client: Client, tmp_path: Path
) -> None:
    """A rule that follows fills a row whole, the outputs of the record it follows."""
    dataset = sample(tmp_path / 'a.h5', [1.0, 2.0], 'pid/1', mode='noisy')
    fills = {'parts.numerator': 1.0}
    rule = Rule(
        name='combine',
        template=Template(spec=COMBINE.id, params={'scale': 2.0}, blanks=('parts',)),
        lookup=Lookup(name='l', entries=(NOISY.model_copy(update={'fills': fills}),)),
        follows=Follows(label='contribute'),
        series=Series(key='sample'),
    )
    with pytest.raises(ValueError, match="names a column, but a dataset fills 'parts'"):
        apply(client, rule, [dataset])


def test_a_row_outside_a_series_is_a_list_of_one_row(
    client: Client, tmp_path: Path
) -> None:
    source = FakeDatasetSource(
        sample(tmp_path / 'a.h5', [2.0, 2.0], 'pid/1', mode='noisy')
    )
    client.backend.sources.append(source)
    rule = floored_rule(Lookup(name='floors', entries=(NOISY_ROW,)))
    (record,) = client.wait(TriggerLoop(client, rule).run_once())
    assert record.status == Status.COMPLETED, record.failure
    assert record.request.params['runs'] == [
        {'run': {'dataset': 'pid:pid/1'}, 'floor': 3.0}
    ]


class TransmittedRun(BaseModel):
    """A row with two dataset columns: a run, and the transmission run it needs."""

    run: OpaqueFile
    transmission: OpaqueFile


class TransmittedParams(BaseModel):
    runs: list[TransmittedRun] = Field(min_length=1)


class RowCount(BaseModel):
    rows: int


TRANSMITTED = WorkflowSpec(
    name='transmitted',
    version=1,
    title='Rows with a transmission run',
    description='Counts its rows; each names a run and its transmission run.',
    params=TransmittedParams,
    outputs=RowCount,
)


def count_rows() -> Any:
    def run(params: TransmittedParams, inputs: Any) -> dict[str, Any]:
        return {'rows': len(params.runs)}

    return run


def transmitted_rule(selector: Selector, transmission: Dataset) -> Rule:
    """A series over runs, each filled with ``transmission`` as its own column."""
    fills = {'runs.transmission': transmission.ref.model_dump()}
    return Rule(
        name='transmitted',
        template=Template(
            spec=TRANSMITTED.id, blanks=('runs',), dataset_field='runs.run'
        ),
        lookup=Lookup(name='t', entries=(LookupEntry(name='all', fills=fills),)),
        selector=selector,
        series=Series(key='sample'),
    )


SCATTER = Selector(match={'role': Like(pattern='scatter')})


def test_a_dataset_only_in_another_column_is_no_member_of_the_series(
    client: Client, tmp_path: Path
) -> None:
    """The loop fires on a transmission run that a row named, once it is selected."""
    client.backend.registry.bind(TRANSMITTED, count_rows)
    transmission = sample(tmp_path / 't.h5', [1.0], 'pid/t', role='transmission')
    client.backend.sources.append(
        FakeDatasetSource(
            transmission, sample(tmp_path / 'a.h5', [1.0], 'pid/a', role='scatter')
        )
    )
    scatter = transmitted_rule(SCATTER, transmission)
    (first,) = client.wait(TriggerLoop(client, scatter).run_once())
    assert first.status == Status.COMPLETED, first.failure

    every = scatter.revise(selector=Selector())
    status = trigger_status(client, every, transmission)
    assert status.state == 'fires', status.reason


def test_a_reprocess_offers_only_the_series_whose_runs_a_record_lists(
    client: Client, tmp_path: Path
) -> None:
    """A transmission run of another sample names no series to reprocess."""
    client.backend.registry.bind(TRANSMITTED, count_rows)
    empty = sample(
        tmp_path / 't.h5', [1.0], 'pid/t', sample='empty', role='transmission'
    )
    client.backend.sources.append(
        FakeDatasetSource(
            empty, sample(tmp_path / 'a.h5', [1.0], 'pid/a', role='scatter')
        )
    )
    rule = transmitted_rule(SCATTER, empty)
    client.wait(TriggerLoop(client, rule).run_once())
    assert list(reprocess(client, rule.revise())) == ['sio2']


def test_a_series_with_a_failed_run_is_retried_without_it_once_excluded(
    client: Client, tmp_path: Path
) -> None:
    """A run that cannot be read fails the sum, visibly, until someone excludes it."""
    client.backend.sources.append(
        FakeDatasetSource(
            Dataset(
                proposals=['p1'],
                path=tmp_path / 'gone.h5',
                pid='pid/1',
                metadata={'sample': 'sio2'},
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
    assert list(table.loc['pid:pid/1', ['rule', 'lookup', 'entries', 'status']]) == [
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


# A nearest fill


def loki(path: Path, run: int, role: str, **fields: str) -> Dataset:
    return Dataset(
        proposals=['p1'],
        path=write_run(path, [1.0, 2.0]),
        uuid=f'loki-{run}',
        instrument='loki',
        run=run,
        metadata={'role': role} | fields,
    )


@pytest.fixture
def cans_and_samples(client: Client, tmp_path: Path) -> FakeDatasetSource:
    """A stray sample before any can, two cans, two samples, a third can, and
    two more samples -- in run-number order."""
    source = FakeDatasetSource(
        *(
            loki(tmp_path / f'{run}.h5', run, role)
            for run, role in enumerate(
                ['sample', 'can', 'can', 'sample', 'sample', 'can', 'sample', 'sample'],
                start=1,
            )
        )
    )
    client.backend.sources.append(source)
    return source


def subtract_rule(**nearest: Any) -> Rule:
    """Subtract from each sample the can that ``Nearest(**nearest)`` picks."""
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
                    fills={
                        'can': Nearest(match={'role': Like(pattern='can')}, **nearest)
                    },
                ),
            ),
        ),
        selector=Selector(match={'role': Like(pattern='sample')}),
    )


@pytest.fixture
def before_rule(cans_and_samples: FakeDatasetSource) -> Rule:
    return subtract_rule()


def cans(records: Iterable[RunRecord]) -> dict[str, str]:
    """The can each member was filled with, by member key."""
    return {
        str(r.request.member_key): as_ref(r.request.params['can']).dataset
        for r in records
    }


def test_a_nearest_fill_is_by_default_the_nearest_earlier_match(
    client: Client, before_rule: Rule
) -> None:
    loop = TriggerLoop(client, before_rule)
    fired = loop.run_once()
    assert [r.status for r in fired] == [Status.COMPLETED] * 4
    assert cans(fired) == {
        'uuid:loki-4': 'uuid:loki-3',
        'uuid:loki-5': 'uuid:loki-3',
        'uuid:loki-7': 'uuid:loki-6',
        'uuid:loki-8': 'uuid:loki-6',
    }
    refusal = loop.refusals['subtract uuid:loki-1']
    assert 'uuid:loki-1: no dataset matching' in refusal
    assert "before it for 'can'" in refusal


def test_backlog_and_reprocess_resolve_the_same_can_as_the_live_loop(
    client: Client, before_rule: Rule
) -> None:
    """The nearest fill is anchored to the member's own dataset, so it is stable
    under a reprocess, unlike a fill resolved against the time of resolution."""
    live = {
        r.request.member_key: r for r in TriggerLoop(client, before_rule).run_once()
    }

    later = Rule.over(
        client.datasets(),
        name='subtract-backlog',
        template=before_rule.template,
        lookup=before_rule.lookup,
        selector=Selector(
            match={'role': Like(pattern='sample'), 'run': Between(low=2)}
        ),
    )
    back = client.submit_group(backlog(client, later))
    assert cans(back.values()) == cans(live.values())

    moved = before_rule.revise(template=before_rule.template.revise())
    again = client.submit_group(reprocess(client, moved))
    assert cans(again.values()) == cans(live.values())


def test_a_member_waits_for_a_match_after_it(
    client: Client, cans_and_samples: FakeDatasetSource, tmp_path: Path
) -> None:
    """The samples after the last can wait, visibly, and fire once one is measured."""
    rule = subtract_rule(direction='after')
    loop = TriggerLoop(client, rule)
    assert cans(loop.run_once()) == {
        'uuid:loki-1': 'uuid:loki-2',
        'uuid:loki-4': 'uuid:loki-6',
        'uuid:loki-5': 'uuid:loki-6',
    }
    assert set(loop.waiting) == {'subtract uuid:loki-7', 'subtract uuid:loki-8'}
    sample = {str(d.ref): d for d in client.datasets()}['uuid:loki-7']
    status = trigger_status(client, rule, sample)
    assert status.state == 'waits'
    assert "after it yet for 'can'" in status.reason

    cans_and_samples.add(loki(tmp_path / '9.h5', 9, 'can'))
    assert cans(loop.run_once()) == {
        'uuid:loki-7': 'uuid:loki-9',
        'uuid:loki-8': 'uuid:loki-9',
    }
    assert loop.waiting == {}


def test_either_direction_takes_the_nearer_match_and_waits_for_the_one_after(
    client: Client, cans_and_samples: FakeDatasetSource
) -> None:
    """Run 4 lies one run after can 3 and two before can 6: can 3; run 5 the
    other way round: can 6; a tie would go to the can before."""
    loop = TriggerLoop(client, subtract_rule(direction='either'))
    assert cans(loop.run_once()) == {
        'uuid:loki-1': 'uuid:loki-2',
        'uuid:loki-4': 'uuid:loki-3',
        'uuid:loki-5': 'uuid:loki-6',
    }
    assert set(loop.waiting) == {'subtract uuid:loki-7', 'subtract uuid:loki-8'}


def test_backlog_and_reprocess_leave_out_the_members_that_wait(
    client: Client, before_rule: Rule
) -> None:
    """A member that waits is listed beside the group, as the live loop lists it,
    and does not stop the others."""
    TriggerLoop(client, before_rule).run_once()
    after = (
        before_rule.lookup.entries[0]
        .fills['can']
        .model_copy(update={'direction': 'after'})
    )
    lookup = Lookup(
        name='cans', entries=(LookupEntry(name='can', fills={'can': after}),)
    )

    again = reprocess(client, before_rule.revise(lookup=lookup))
    assert sorted(again) == ['uuid:loki-4', 'uuid:loki-5']
    assert sorted(again.waiting) == ['uuid:loki-7', 'uuid:loki-8']
    assert "after it yet for 'can'" in again.waiting['uuid:loki-7']

    later = Rule.over(
        client.datasets(),
        name='subtract-backlog',
        template=before_rule.template,
        lookup=lookup,
        selector=Selector(
            match={'role': Like(pattern='sample'), 'run': Between(low=2)}
        ),
    )
    back = backlog(client, later)
    assert sorted(back) == ['uuid:loki-4', 'uuid:loki-5']
    assert back.waiting == again.waiting


def test_a_pinned_can_makes_a_member_that_waits(
    client: Client, cans_and_samples: FakeDatasetSource
) -> None:
    """The pinned value replaces the nearest fill, which is then not resolved."""
    rule = subtract_rule(direction='after')
    loop = TriggerLoop(client, rule)
    loop.run_once()
    seven = {str(d.ref): d for d in client.datasets()}['uuid:loki-7']
    pinned = {'uuid:loki-7': {'can': dataset_ref(instrument='loki', run=6)}}
    (record,) = client.submit_group(apply(client, rule, [seven], pinned)).values()
    assert record.status == Status.COMPLETED, record.failure
    assert cans([record]) == {'uuid:loki-7': 'uuid:loki-6'}
    assert list(record.request.origin.pinned) == ['can']
    assert loop.run_once() == []
    assert set(loop.waiting) == {'subtract uuid:loki-8'}


def test_a_member_refused_for_want_of_a_can_is_made_with_a_pinned_one(
    client: Client, before_rule: Rule
) -> None:
    first = {str(d.ref): d for d in client.datasets()}['uuid:loki-1']
    status = trigger_status(client, before_rule, first)
    assert status.state == 'skips'
    assert "before it for 'can'" in status.reason
    pinned = {'uuid:loki-1': {'can': dataset_ref(instrument='loki', run=2)}}
    group = apply(client, before_rule, [first], pinned)
    assert cans(client.submit_group(group).values()) == {'uuid:loki-1': 'uuid:loki-2'}
    assert trigger_status(client, before_rule, first).reason == (
        '1 record(s) under the label'
    )


def test_either_direction_breaks_a_tie_toward_the_match_before_in_the_extractors_order(
    client: Client, tmp_path: Path
) -> None:
    """By start time the sample lies ten minutes after one can and ten minutes
    before the other; by run number it would lie after both."""

    def dataset(run: int, role: str, minute: int) -> Dataset:
        return Dataset(
            proposals=['p1'],
            path=write_run(tmp_path / f'{run}.h5', [1.0, 2.0]),
            uuid=f'loki-{run}',
            instrument='loki',
            run=run,
            metadata={
                'role': role,
                'start': datetime(2026, 9, 1, 10, minute, tzinfo=UTC),
            },
            order='start',
        )

    client.backend.sources.append(
        FakeDatasetSource(
            dataset(1, 'can', 0), dataset(2, 'can', 20), dataset(3, 'sample', 10)
        )
    )
    fired = TriggerLoop(client, subtract_rule(direction='either')).run_once()
    assert cans(fired) == {'uuid:loki-3': 'uuid:loki-1'}


def test_a_nearest_fill_pairs_by_the_fields_named_same(
    client: Client, tmp_path: Path
) -> None:
    """Two sample holders in turn: each sample gets the can of its own holder."""
    client.backend.sources.append(
        FakeDatasetSource(
            loki(tmp_path / '1.h5', 1, 'can', holder='a'),
            loki(tmp_path / '2.h5', 2, 'can', holder='b'),
            loki(tmp_path / '3.h5', 3, 'sample', holder='a'),
            loki(tmp_path / '4.h5', 4, 'sample', holder='b'),
        )
    )
    fired = TriggerLoop(client, subtract_rule(same=('holder',))).run_once()
    assert cans(fired) == {'uuid:loki-3': 'uuid:loki-1', 'uuid:loki-4': 'uuid:loki-2'}


# Rules over the completed records of another rule


def settle(client: Client, loop: TriggerLoop) -> None:
    """Run the loop until it fires on nothing, as passes over time would."""
    while fired := loop.run_once():
        client.wait(fired)


def contribute_and_combine() -> tuple[Rule, Rule]:
    """A contribution per run, and a combine over the contributions per sample."""
    contribute = Rule(
        name='contribute',
        template=Template(spec=CONTRIBUTE.id, params={'floor': 1.5}, blanks=('run',)),
    )
    combine = Rule(
        name='combine',
        template=Template(spec=COMBINE.id, params={'scale': 2.0}, blanks=('parts',)),
        follows=Follows(label='contribute'),
        series=Series(key='sample'),
    )
    return contribute, combine


def test_a_combine_follows_the_contributions_of_a_series_as_it_grows(
    client: Client, tmp_path: Path
) -> None:
    """One rule reduces each run to its contribution, a second combines the
    contributions of each sample, fired again as the sample gets more runs."""
    source = FakeDatasetSource()
    client.backend.sources.append(source)
    contribute, combine = contribute_and_combine()
    loop = TriggerLoop(client, contribute, combine)
    runs = [[1.0, 2.0, 3.0, 4.0], [2.0, 2.0, 2.0, 2.0], [4.0, 3.0, 2.0, 1.0]]
    for i, values in enumerate(runs, start=1):
        source.add(sample(tmp_path / f'{i}.h5', values, f'pid/{i}'))
        settle(client, loop)

    combined = client.records(label='combine', member_key='sio2')
    assert len(combined) == 3
    latest = client.latest('combine', 'sio2')
    parts = client.batch('contribute')
    assert latest.request.params['parts'] == [
        {
            name: part.ref(name).model_dump(mode='json')
            for name in ('numerator', 'denominator')
        }
        for part in parts
    ]
    (at_once,) = client.wait(
        [
            client.run(
                NORMALIZE,
                {
                    'runs': [dataset_ref(pid=f'pid/{i}') for i in (1, 2, 3)],
                    'floor': 1.5,
                    'scale': 2.0,
                },
            )
        ]
    )
    assert latest.status == Status.COMPLETED, latest.failure
    assert sc.identical(
        client.output(latest, 'normalized'), client.output(at_once, 'normalized')
    )
    assert loop.run_once() == []


def test_a_second_phase_fans_out_over_the_keys_the_first_found(
    client: Client, tmp_path: Path
) -> None:
    """SUM stands in for a first phase whose output is keyed by what it read;
    the second phase makes one request per key."""
    client.backend.sources.append(
        FakeDatasetSource(
            sample(tmp_path / 'a.h5', [1.0, 2.0], 'pid/1'),
            sample(tmp_path / 'b.h5', [5.0, 6.0], 'pid/2'),
        )
    )
    first = Rule(
        name='first',
        template=Template(spec=SUM.id, blanks=('runs',)),
        series=Series(key='sample', fire=Complete(count=2)),
    )
    second = Rule(
        name='second',
        template=Template(spec=EXPORT.id, blanks=('data',)),
        follows=Follows(label='first', output='per_run'),
    )
    loop = TriggerLoop(client, first, second)
    settle(client, loop)

    (phase1,) = client.batch('first')
    exports = client.batch('second')
    assert [r.request.member_key for r in exports] == ['sio2[0]', 'sio2[1]']
    assert [r.request.refs() for r in exports] == [
        [phase1.ref('per_run', key)] for key in ('0', '1')
    ]
    assert [client.output(r, 'csv').read_text() for r in exports] == [
        'x,counts\n0.0,1.0\n1.0,2.0\n',
        'x,counts\n0.0,5.0\n1.0,6.0\n',
    ]


def test_a_combine_waits_on_a_failed_contribution_until_it_is_excluded(
    client: Client, tmp_path: Path
) -> None:
    client.backend.sources += [
        FakeDatasetSource(sample(tmp_path / 'a.h5', [1.0, 2.0], 'pid/1')),
        FakeDatasetSource(
            Dataset(
                proposals=['p1'],
                path=tmp_path / 'gone.h5',
                pid='pid/2',
                metadata={'sample': 'sio2'},
            ),
            locates=False,
        ),
    ]
    contribute, combine = contribute_and_combine()
    loop = TriggerLoop(client, contribute, combine)
    settle(client, loop)
    assert client.records(label='combine') == []
    reason = 'series sio2: contribute pid:pid/2 failed'
    assert loop.waiting == {
        'combine pid:pid/1': reason,
        'combine pid:pid/2': reason,
    }

    combine.exclude('pid:pid/2', 'file lost')
    settle(client, loop)
    (combined,) = client.records(label='combine')
    assert combined.status == Status.COMPLETED, combined.failure
    assert len(combined.request.params['parts']) == 1


def test_a_combine_keeps_a_member_whose_correction_failed(
    client: Client, tmp_path: Path
) -> None:
    """The combine waits rather than lose a contribution it had."""
    second = sample(tmp_path / 'b.h5', [2.0, 2.0], 'pid/2')
    source = FakeDatasetSource(sample(tmp_path / 'a.h5', [1.0, 2.0], 'pid/1'), second)
    client.backend.sources.append(source)
    contribute, combine = contribute_and_combine()
    loop = TriggerLoop(client, contribute, combine)
    settle(client, loop)
    (first,) = client.records(label='combine')
    assert len(first.request.params['parts']) == 2

    second.path.unlink()
    client.wait(client.submit_group(apply(client, contribute, [second])).values())
    source.add(sample(tmp_path / 'c.h5', [3.0, 3.0], 'pid/3'))
    settle(client, loop)
    assert client.records(label='combine') == [first]
    assert 'contribute pid:pid/2 failed' in loop.waiting['combine pid:pid/3']


def test_a_rule_follows_the_series_of_another_by_the_fields_its_runs_share(
    client: Client, tmp_path: Path
) -> None:
    """A sum per angle, then a sum of those per sample: two levels of a series."""
    client.backend.sources.append(
        FakeDatasetSource(
            sample(tmp_path / '1.h5', [1.0, 2.0], 'pid/1', angle='a'),
            sample(tmp_path / '2.h5', [3.0, 4.0], 'pid/2', angle='a'),
            sample(tmp_path / '3.h5', [5.0, 6.0], 'pid/3', angle='b'),
        )
    )
    per_angle = Rule(
        name='per-angle',
        template=Template(spec=SUM.id, blanks=('runs',)),
        series=Series(key='angle'),
    )
    per_sample = Rule(
        name='per-sample',
        template=Template(spec=SUM.id, blanks=('runs',)),
        follows=Follows(label='per-angle', output='total'),
        series=Series(key='sample'),
    )
    settle(client, TriggerLoop(client, per_angle, per_sample))

    angles = client.batch('per-angle')
    assert [r.request.member_key for r in angles] == ['a', 'b']
    (total,) = client.batch('per-sample')
    assert total.request.member_key == 'sio2'
    assert total.request.refs() == [r.ref('total') for r in angles]
    assert list(client.output(total, 'total').values) == [9.0, 12.0]


class Found(BaseModel):
    parts: dict[str, int]


class RunParams(BaseModel):
    run: OpaqueFile


FIND = WorkflowSpec(
    name='find',
    version=1,
    title='Find parts',
    description='Finds no parts in a run.',
    params=RunParams,
    outputs=Found,
)


def find_nothing() -> Any:
    def run(params: RunParams, inputs: Any) -> dict[str, Any]:
        return {'parts': {}}

    return run


def test_an_empty_collection_output_makes_no_candidate(
    client: Client, samples: FakeDatasetSource
) -> None:
    client.backend.registry.bind(FIND, find_nothing)
    find = Rule(name='find', template=Template(spec=FIND.id, blanks=('run',)))
    each = Rule(
        name='each',
        template=Template(spec=EXPORT.id, blanks=('data',)),
        follows=Follows(label='find', output='parts'),
    )
    settle(client, TriggerLoop(client, find))
    assert len(client.batch('find')) == 2
    assert candidates(client, each) == []


def load_and_export() -> tuple[Rule, Rule]:
    """A rule per dataset, and one that exports each of its results."""
    load = Rule(name='load', template=Template(spec=LOAD.id, blanks=('run',)))
    export = Rule(
        name='export',
        template=Template(spec=EXPORT.id, blanks=('data',)),
        follows=Follows(label='load', output='data'),
    )
    return load, export


def test_a_correction_of_a_followed_record_fires_the_follower_once_more(
    client: Client, samples: FakeDatasetSource
) -> None:
    load, export = load_and_export()
    loop = TriggerLoop(client, load, export)
    settle(client, loop)
    exported = {r.request.member_key: r for r in client.batch('export')}
    assert list(exported) == ['pid:pid/1', 'pid:pid/2']
    followed = {_member(c): c for c in candidates(client, export)}
    assert trigger_status(client, export, followed['pid:pid/1']).reason == (
        '1 record(s) under the label'
    )

    pinned = {'pid:pid/1': {'scale': 3.0}}
    group = apply(client, load, [followed['pid:pid/1'].dataset], pinned)
    client.wait(client.submit_group(group).values())
    corrected = {_member(c): c for c in candidates(client, export)}['pid:pid/1']
    assert trigger_status(client, export, corrected).state == 'fires'
    settle(client, loop)
    assert len(client.records(label='export', member_key='pid:pid/1')) == 2
    assert len(client.records(label='export', member_key='pid:pid/2')) == 1


def _member(candidate: Any) -> str:
    return str(candidate.record.request.member_key)


def test_backlog_and_reprocess_of_a_rule_that_follows(
    client: Client, samples: FakeDatasetSource
) -> None:
    load, export = load_and_export()
    settle(client, TriggerLoop(client, load))
    later = Rule.over(
        client.datasets(),
        name='export',
        template=export.template,
        follows=export.follows,
    )
    assert trigger_status(client, later, candidates(client, later)[0]).reason == (
        "before the rule's bound"
    )
    back = client.submit_group(backlog(client, later))
    assert sorted(back) == ['pid:pid/1', 'pid:pid/2']
    assert all(r.status == Status.COMPLETED for r in client.wait(back.values()))

    moved = later.revise(template=later.template.revise())
    again = reprocess(client, moved)
    assert sorted(again) == ['pid:pid/1', 'pid:pid/2']
    assert [r.refs() for r in again.values()] == [
        r.request.refs() for r in back.values()
    ]
