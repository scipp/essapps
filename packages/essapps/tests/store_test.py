# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from ess.apps.records import (
    Accumulate,
    StageRecord,
    StageRequest,
    Status,
    WorkflowRecord,
)
from ess.apps.spec import OutputRef, SpecId, dataset_ref
from ess.apps.store import RecordStore, StoreLockedError

SPEC = SpecId(name='reduce', version=1)


def request(
    *,
    spec: SpecId = SPEC,
    proposal: str = 'p1',
    params: dict[str, Any] | None = None,
    **kwargs: Any,
) -> StageRequest:
    workflow = WorkflowRecord(
        spec=spec, params=params or {}, instrument='dream', proposal=proposal
    )
    return StageRequest(workflow=workflow, submitter='simon', **kwargs)


@pytest.fixture
def store(tmp_path: Path):
    with RecordStore(tmp_path / 'records.db') as s:
        yield s


def test_add_get_round_trips_record(store: RecordStore) -> None:
    record = StageRecord(request=request(params={'threshold': 3}))
    store.add(record)
    assert store.get(record.id) == record
    assert record.id in store
    assert 'nope' not in store


def test_update_changes_status_and_missing_record_raises(store: RecordStore) -> None:
    record = StageRecord(request=request())
    store.add(record)
    record.status = Status.RUNNING
    store.update(record)
    assert store.get(record.id).status == Status.RUNNING
    with pytest.raises(KeyError):
        store.update(StageRecord(request=request()))


def test_members_to_retry_skips_members_in_flight_or_completed(
    store: RecordStore,
) -> None:
    members = {
        'failed': Status.FAILED,
        'cancelled': Status.CANCELLED,
        'running': Status.RUNNING,
        'waiting': Status.WAITING,
        'completed': Status.COMPLETED,
    }
    for key, status in members.items():
        record = StageRecord(request=request(label='auto', member_key=key))
        store.add(record)
        record.status = status
        store.update(record)
    offered = store.members_to_retry('auto', 'p1')
    assert [r.request.member_key for r in offered] == ['cancelled', 'failed']


def test_list_filters_by_proposal_spec_status_label_and_member(
    store: RecordStore,
) -> None:
    a = StageRecord(request=request(label='tune'))
    b = StageRecord(request=request(proposal='p2', label='scan', member_key='300K'))
    c = StageRecord(request=request(spec=SpecId(name='other', version=2)))
    c.status = Status.COMPLETED
    for r in (a, b, c):
        store.add(r)
    assert [r.id for r in store.list(proposal='p1')] == [a.id, c.id]
    assert [r.id for r in store.list(label='tune')] == [a.id]
    assert [r.id for r in store.list(label='scan')] == [b.id]
    assert [r.id for r in store.list(member_key='300K')] == [b.id]
    assert [r.id for r in store.list(spec=SPEC)] == [a.id, b.id]
    assert [r.id for r in store.list(status=Status.COMPLETED)] == [c.id]
    assert [r.id for r in store.list(limit=1)] == [a.id]


def test_latest_is_the_head_of_the_labels_chain(store: RecordStore) -> None:
    first = StageRecord(request=request(label='tune'))
    store.add(first)
    second = StageRecord(request=request(label='tune'), supersedes=first.id)
    store.add(second)
    assert store.latest('tune', 'p1').id == second.id
    assert store.latest('tune', 'p2') is None


def test_the_head_does_not_depend_on_creation_time(store: RecordStore) -> None:
    """Several writers may submit under one label from different hosts, so the
    order under a label cannot depend on a clock."""
    first = StageRecord(request=request(label='tune'))
    store.add(first)
    second = StageRecord(
        request=request(label='tune'),
        supersedes=first.id,
        created=first.created - timedelta(hours=1),
    )
    store.add(second)
    assert store.latest('tune', 'p1').id == second.id


def test_latest_per_member_key_supersedes_only_within_the_member(
    store: RecordStore,
) -> None:
    def member(key: str) -> StageRecord:
        return StageRecord(request=request(label='scan', member_key=key))

    first, other = member('300K'), member('310K')
    store.add(first, other)
    corrected = StageRecord(
        request=request(label='scan', member_key='300K'), supersedes=first.id
    )
    store.add(corrected)
    assert store.latest('scan', 'p1', member_key='300K').id == corrected.id
    assert store.latest('scan', 'p1', member_key='310K').id == other.id
    assert store.latest('scan', 'p1', member_key='320K') is None
    # The slot form: no record under 'scan' carries a NULL member key.
    assert store.latest('scan', 'p1') is None


def test_batch_table_is_the_head_per_member_key(store: RecordStore) -> None:
    def member(key: str | None) -> StageRecord:
        return StageRecord(request=request(label='scan', member_key=key))

    first, other = member('300K'), member('310K')
    by_hand = member(None)
    elsewhere = StageRecord(request=request(label='other', member_key='300K'))
    store.add(first, other, by_hand, elsewhere)
    corrected = StageRecord(
        request=request(label='scan', member_key='300K'), supersedes=first.id
    )
    store.add(corrected)
    assert [r.id for r in store.batch('scan', 'p1')] == [
        by_hand.id,
        corrected.id,
        other.id,
    ]
    assert store.batch('scan', 'p2') == []


def test_referencing_finds_records_by_output_of_producer(store: RecordStore) -> None:
    producer = StageRecord(request=request())
    consumer = StageRecord(
        request=request(
            params={'vanadium': OutputRef(record=producer.id, output='result')}
        )
    )
    other = StageRecord(request=request())
    for r in (producer, consumer, other):
        store.add(r)
    assert store.referencing(producer.id) == [consumer.id]
    assert store.referencing(producer.id, 'result') == [consumer.id]
    assert store.referencing(producer.id, 'other') == []
    assert store.referencing(other.id) == []


def test_referencing_finds_references_in_stage_inputs_and_accumulations(
    store: RecordStore,
) -> None:
    a, b = StageRecord(request=request()), StageRecord(request=request())
    finalize = StageRecord(
        request=request(
            inputs={
                'numerator': Accumulate(
                    accumulate=[
                        OutputRef(record=a.id, output='numerator'),
                        OutputRef(record=b.id, output='numerator'),
                    ]
                ),
                'denominator': OutputRef(record=a.id, output='denominator'),
            }
        )
    )
    store.add(a, b, finalize)
    assert store.referencing(a.id) == [finalize.id]
    assert store.referencing(a.id, 'denominator') == [finalize.id]
    assert store.referencing(b.id, 'numerator') == [finalize.id]
    assert store.get(finalize.id) == finalize


def test_registry_records_where_copies_are(store: RecordStore, tmp_path: Path) -> None:
    ref = OutputRef(record='r1', output='result')
    assert store.location(ref) is None
    store.register(ref, tmp_path / 'r1.h5', store_owned=True)
    assert store.location(ref) == (tmp_path / 'r1.h5', True)
    keyed = OutputRef(record='r1', output='banks', key='b0')
    store.register(keyed, tmp_path / 'b0.h5', store_owned=False)
    assert store.location(keyed) == (tmp_path / 'b0.h5', False)
    copied = dataset_ref(instrument='dream', run=1)
    store.register(copied, tmp_path / 'dream_1.h5', store_owned=True)
    assert store.location(copied) == (tmp_path / 'dream_1.h5', True)
    store.unregister(ref)
    assert store.location(ref) is None
    assert store.location(keyed) is not None


def test_second_writer_is_refused(tmp_path: Path) -> None:
    with RecordStore(tmp_path / 'records.db'), pytest.raises(StoreLockedError):
        RecordStore(tmp_path / 'records.db')


def test_store_reopens_after_close(tmp_path: Path) -> None:
    record = StageRecord(request=request())
    with RecordStore(tmp_path / 'records.db') as s:
        s.add(record)
    with RecordStore(tmp_path / 'records.db') as s:
        assert s.get(record.id) == record
