# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from pathlib import Path

import pytest

from ess.apps.records import RunRecord, RunRequest, Status
from ess.apps.spec import Ref, SpecId
from ess.apps.store import RecordStore, StoreLockedError

SPEC = SpecId(name='reduce', version=1)


def request(**kwargs) -> RunRequest:
    base = {'spec': SPEC, 'instrument': 'dream', 'proposal': 'p1', 'submitter': 'simon'}
    return RunRequest(**(base | kwargs))


@pytest.fixture
def store(tmp_path: Path):
    with RecordStore(tmp_path / 'records.db') as s:
        yield s


def test_add_get_round_trips_record(store: RecordStore) -> None:
    record = RunRecord(request=request(params={'threshold': 3}))
    store.add(record)
    assert store.get(record.id) == record
    assert record.id in store
    assert 'nope' not in store


def test_update_changes_status_and_missing_record_raises(store: RecordStore) -> None:
    record = RunRecord(request=request())
    store.add(record)
    record.status = Status.RUNNING
    store.update(record)
    assert store.get(record.id).status == Status.RUNNING
    with pytest.raises(KeyError):
        store.update(RunRecord(request=request()))


def test_list_filters_by_proposal_spec_status_slot_and_batch(
    store: RecordStore,
) -> None:
    a = RunRecord(request=request(slot='tune'))
    b = RunRecord(request=request(proposal='p2', batch='b1', member_key='300K'))
    c = RunRecord(request=request(spec=SpecId(name='other', version=2)))
    c.status = Status.COMPLETED
    for r in (a, b, c):
        store.add(r)
    assert [r.id for r in store.list(proposal='p1')] == [a.id, c.id]
    assert [r.id for r in store.list(slot='tune')] == [a.id]
    assert [r.id for r in store.list(batch='b1')] == [b.id]
    assert [r.id for r in store.list(spec=SPEC)] == [a.id, b.id]
    assert [r.id for r in store.list(status=Status.COMPLETED)] == [c.id]
    assert [r.id for r in store.list(limit=1)] == [a.id]


def test_latest_in_slot_is_the_newest_record(store: RecordStore) -> None:
    first = RunRecord(request=request(slot='tune'))
    second = RunRecord(request=request(slot='tune'))
    store.add(first)
    store.add(second)
    assert store.latest('tune', 'p1').id == second.id
    assert store.latest('tune', 'p2') is None


def test_referencing_finds_records_by_output_of_producer(store: RecordStore) -> None:
    producer = RunRecord(request=request())
    consumer = RunRecord(
        request=request(params={'vanadium': Ref(record=producer.id, output='result')})
    )
    other = RunRecord(request=request())
    for r in (producer, consumer, other):
        store.add(r)
    assert store.referencing(producer.id) == [consumer.id]
    assert store.referencing(producer.id, 'result') == [consumer.id]
    assert store.referencing(producer.id, 'other') == []
    assert store.referencing(other.id) == []


def test_registry_records_where_copies_are(store: RecordStore, tmp_path: Path) -> None:
    ref = Ref(record='r1', output='result')
    assert store.location(ref) is None
    store.register(ref, tmp_path / 'r1.h5', store_owned=True)
    assert store.location(ref) == (tmp_path / 'r1.h5', True)
    keyed = Ref(record='r1', output='banks', key='b0')
    store.register(keyed, tmp_path / 'b0.h5', store_owned=False)
    assert store.location(keyed) == (tmp_path / 'b0.h5', False)
    store.unregister(ref)
    assert store.location(ref) is None
    assert store.location(keyed) is not None


def test_second_writer_is_refused(tmp_path: Path) -> None:
    with RecordStore(tmp_path / 'records.db'), pytest.raises(StoreLockedError):
        RecordStore(tmp_path / 'records.db')


def test_store_reopens_after_close(tmp_path: Path) -> None:
    record = RunRecord(request=request())
    with RecordStore(tmp_path / 'records.db') as s:
        s.add(record)
    with RecordStore(tmp_path / 'records.db') as s:
        assert s.get(record.id) == record
