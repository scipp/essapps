# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Only finalized data enters the catalogue.

See docs/developer/workflow-contract.md.
"""

from pathlib import Path

import pytest

from ess.apps.client import Client
from ess.apps.examples import HISTOGRAM, LOAD, REBIN
from ess.apps.spec import DatasetRef
from ess.apps.testing import FakePublisher

from .conftest import make_client


@pytest.fixture
def publisher() -> FakePublisher:
    return FakePublisher()


@pytest.fixture
def client(tmp_path: Path, datasets: Path, publisher: FakePublisher):
    client = make_client(tmp_path / 'store', datasets, publishers={'fake': publisher})
    yield client
    client.close()


def test_publish_writes_out_and_carries_a_provenance_snapshot(
    client: Client, run_ref: DatasetRef, publisher: FakePublisher
) -> None:
    loaded = client.run(LOAD, {'run': run_ref})
    rebinned = client.run(REBIN, {'data': loaded.ref('data')})
    ref = rebinned.ref('result')
    pid = client.publish(ref, 'fake', allow_reused=True)
    assert client.record(rebinned.id).published == {'result': pid}
    assert client.backend.data.has_copy(ref)
    entry = publisher.entries[pid]
    assert entry['path'].exists()
    snapshot = entry['snapshot']
    assert snapshot['spec'] == 'rebin/v1'
    assert snapshot['params']['bins'] == 4
    assert snapshot['inputs'][0]['spec'] == 'load/v1'
    assert snapshot['inputs'][0]['raw'] == [{'dataset': 'run:dream/1'}]
    assert 'essapps' in snapshot['package_versions']


def test_publish_is_idempotent(
    client: Client, run_ref: DatasetRef, publisher: FakePublisher
) -> None:
    loaded = client.run(LOAD, {'run': run_ref})
    ref = loaded.ref('data')
    first = client.publish(ref, 'fake', allow_reused=True)
    assert client.publish(ref, 'fake', allow_reused=True) == first
    assert len(publisher.entries) == 1


def test_publish_refuses_reused_and_in_process_records_by_default(
    client: Client, run_ref: DatasetRef
) -> None:
    data = client.run(LOAD, {'run': run_ref}).ref('data')
    tune = client.workflow(HISTOGRAM, {'data': data}).stage(inputs=['bins'], label='s')
    tune.compute({'bins': 2})
    second = tune.compute({'bins': 3})
    assert second.reused
    with pytest.raises(ValueError, match='reused'):
        client.publish(second.ref('histogram'), 'fake')
    first = client.records(label='s')[0]
    with pytest.raises(ValueError, match='in-process'):
        client.publish(first.ref('histogram'), 'fake')


class CrashingPublisher:
    def publish(self, path, snapshot):
        raise ConnectionError('catalogue down')


def test_failed_publish_can_be_retried(
    tmp_path: Path, datasets: Path, run_ref: DatasetRef
) -> None:
    client = make_client(
        tmp_path / 'store',
        datasets,
        publishers={'crashing': CrashingPublisher(), 'fake': FakePublisher()},
    )
    loaded = client.run(LOAD, {'run': run_ref})
    with pytest.raises(ConnectionError):
        client.publish(loaded.ref('data'), 'crashing', allow_reused=True)
    assert client.record(loaded.id).publishing == ['data']
    assert client.record(loaded.id).published == {}
    pid = client.publish(loaded.ref('data'), 'fake', allow_reused=True)
    assert client.record(loaded.id).published == {'data': pid}
    assert client.record(loaded.id).publishing == []
    client.close()
