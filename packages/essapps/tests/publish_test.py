# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Only finalized data enters the catalogue (D11)."""

import pytest

from ess.apps.client import Client
from ess.apps.examples import LOAD, REBIN
from ess.apps.spec import Ref
from ess.apps.testing import FakePublisher


def test_publish_writes_out_and_carries_a_provenance_snapshot(
    client: Client, run_ref: Ref
) -> None:
    loaded = client.run(LOAD, {'run': run_ref})
    rebinned = client.run(REBIN, {'data': Ref(record=loaded.id, output='data')})
    publisher = FakePublisher()
    ref = Ref(record=rebinned.id, output='result')
    pid = client.publish(ref, publisher, allow_reused=True)
    assert client.record(rebinned.id).published == {'result': pid}
    assert client.backend.data.has_copy(ref)
    entry = publisher.entries[pid]
    assert entry['path'].exists()
    snapshot = entry['snapshot']
    assert snapshot['spec'] == 'rebin/v1'
    assert snapshot['params']['bins'] == 4
    assert snapshot['inputs'][0]['spec'] == 'load/v1'
    assert snapshot['inputs'][0]['raw'][0]['path'].endswith('run1.h5')
    assert 'essapps' in snapshot['package_versions']


def test_publish_is_idempotent(client: Client, run_ref: Ref) -> None:
    loaded = client.run(LOAD, {'run': run_ref})
    publisher = FakePublisher()
    ref = Ref(record=loaded.id, output='data')
    first = client.publish(ref, publisher, allow_reused=True)
    assert client.publish(ref, publisher, allow_reused=True) == first
    assert len(publisher.entries) == 1


def test_publish_refuses_reused_and_in_process_records_by_default(
    client: Client, run_ref: Ref
) -> None:
    client.run(LOAD, {'run': run_ref}, slot='s')
    second = client.run(LOAD, {'run': run_ref, 'scale': 2.0}, slot='s')
    assert second.reused
    with pytest.raises(ValueError, match='reused'):
        client.publish(Ref(record=second.id, output='data'), FakePublisher())
    first = client.records(slot='s')[0]
    with pytest.raises(ValueError, match='in-process'):
        client.publish(Ref(record=first.id, output='data'), FakePublisher())
