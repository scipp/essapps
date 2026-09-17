# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from pathlib import Path

import pytest
import scipp as sc

from ess.apps.datastore import DataStore, MissingCopyError
from ess.apps.spec import Ref
from ess.apps.store import RecordStore


@pytest.fixture
def store(tmp_path: Path):
    with RecordStore(tmp_path / 'records.db') as records:
        yield DataStore(records, tmp_path / 'data')


def data() -> sc.DataArray:
    return sc.DataArray(sc.arange('x', 4.0, unit='counts'))


def test_put_without_disk_serves_from_cache_only(store: DataStore) -> None:
    ref = Ref(record='r1', output='result')
    store.put(ref, data(), to_disk=False)
    assert store.in_cache(ref)
    assert not store.has_copy(ref)
    assert sc.identical(store.array(ref), data())


def test_put_to_disk_registers_a_copy_that_survives_eviction(store: DataStore) -> None:
    ref = Ref(record='r1', output='result')
    store.put(ref, data(), to_disk=True)
    assert store.has_copy(ref)
    store.evict(ref)
    assert not store.in_cache(ref)
    assert sc.identical(store.array(ref), data())
    assert store.in_cache(ref)


def test_write_out_moves_cached_value_to_disk_once(store: DataStore) -> None:
    ref = Ref(record='r1', output='result')
    store.put(ref, data(), to_disk=False)
    path = store.write_out(ref)
    assert path.exists()
    assert store.write_out(ref) == path


def test_collection_elements_are_stored_individually(store: DataStore) -> None:
    a = Ref(record='r1', output='banks', key='a')
    b = Ref(record='r1', output='banks', key='b')
    store.put(a, data(), to_disk=True)
    store.put(b, data() * 2, to_disk=True)
    store.evict(a)
    store.evict(b)
    assert sc.identical(store.array(b), data() * 2)
    assert not store.in_cache(a)


def test_missing_copy_is_reported_not_recomputed(store: DataStore) -> None:
    with pytest.raises(MissingCopyError):
        store.array(Ref(record='r1', output='result'))


def test_an_adopted_file_returns_the_registered_path(
    store: DataStore, tmp_path: Path
) -> None:
    ref = Ref(record='f1', output='file')
    user_file = tmp_path / 'run.nxs'
    user_file.write_bytes(b'nexus')
    store.adopt(ref, user_file, store_owned=False)
    assert store.path(ref) == user_file


def test_drop_removes_store_copies_only(store: DataStore, tmp_path: Path) -> None:
    ours = Ref(record='r1', output='result')
    store.put(ours, data(), to_disk=True)
    path = store.write_out(ours)
    store.drop(ours)
    assert not path.exists()
    assert not store.has_copy(ours)
    theirs = Ref(record='f1', output='file')
    user_file = tmp_path / 'run.nxs'
    user_file.write_bytes(b'nexus')
    store.adopt(theirs, user_file, store_owned=False)
    with pytest.raises(PermissionError):
        store.drop(theirs)
    assert user_file.exists()


def test_bytes_outputs_are_opaque_files(store: DataStore) -> None:
    ref = Ref(record='r1', output='cif')
    store.put(ref, b'data_x', to_disk=True)
    assert store.path(ref).read_bytes() == b'data_x'


def test_unknown_type_needs_a_serializer(store: DataStore) -> None:
    ref = Ref(record='r1', output='thing')
    with pytest.raises(TypeError, match='serializer'):
        store.put(ref, object(), to_disk=True)
