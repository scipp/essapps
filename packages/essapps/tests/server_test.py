# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The HTTP transport: a real server thread, a RemoteBackend through Client."""

from contextlib import closing
from pathlib import Path

import pytest
import scipp as sc

from ess.apps.backend import SubmitError
from ess.apps.client import Client
from ess.apps.datastore import Serializers
from ess.apps.examples import LOAD, REBIN
from ess.apps.records import Status
from ess.apps.remote import remote
from ess.apps.spec import DatasetRef, OutputRef, SpecId


def test_output_matches_a_local_run_and_a_literal_comes_back_inline(
    client: Client, remote_client: Client, run_ref: DatasetRef
) -> None:
    (local_record,) = client.wait([client.run(LOAD, {'run': run_ref, 'scale': 2.0})])
    expected = client.output(local_record.ref('data'))

    remote_record = remote_client.run(LOAD, {'run': run_ref, 'scale': 2.0})
    (record,) = remote_client.wait([remote_record])
    assert record.status == Status.COMPLETED, record.failure
    assert sc.identical(remote_client.output(record.ref('data')), expected)
    total = remote_client.output(record.ref('total'))
    assert total == {'value': 72.0, 'unit': 'counts'}


def test_write_out_downloads_into_a_folder_or_names_the_servers_path(
    remote_client: Client, run_ref: DatasetRef, tmp_path: Path
) -> None:
    (record,) = remote_client.wait([remote_client.run(LOAD, {'run': run_ref})])
    ref = record.ref('data')
    downloaded = remote_client.write_out(ref, tmp_path / 'out')
    assert downloaded.parent == tmp_path / 'out'
    assert sc.identical(Serializers().load(downloaded), remote_client.output(ref))
    # The server runs on this machine, so its own path is visible here.
    assert remote_client.write_out(ref).exists()


def test_validate_reports_errors_and_submit_of_the_same_request_raises(
    remote_client: Client,
) -> None:
    request = remote_client.request(LOAD, {'scale': 2.0})  # missing 'run'
    report = remote_client.validate(request)
    assert not report.ok
    with pytest.raises(SubmitError) as exc_info:
        remote_client.submit(request)
    assert exc_info.value.reports


def test_record_of_an_unknown_id_raises_key_error(remote_client: Client) -> None:
    with pytest.raises(KeyError):
        remote_client.record('no-such-record')


def test_specs_and_spec_are_served(remote_client: Client) -> None:
    specs = remote_client.specs()
    assert any(s.name == 'load' for s in specs)
    spec = remote_client.spec(SpecId(name='load', version=1))
    assert 'run' in spec.params_schema['properties']


def test_datasets_and_pick_see_the_run_file(
    remote_client: Client, run_ref: DatasetRef
) -> None:
    assert run_ref in [d.ref for d in remote_client.datasets()]
    assert run_ref in [c.ref for c in remote_client.pick()]


def test_cancel_propagates_to_a_record_waiting_on_the_producer(
    remote_client: Client, run_ref: DatasetRef
) -> None:
    group = remote_client.submit_group(
        {
            'a': remote_client.request(LOAD, {'run': run_ref}),
            'b': remote_client.request(
                REBIN, {'data': OutputRef(record='@a', output='data')}
            ),
        }
    )
    remote_client.cancel(group['a'])
    (a, b) = remote_client.wait(group.values())
    assert a.status == Status.CANCELLED
    assert b.status == Status.CANCELLED


def test_recompute_derives_a_new_record_from_the_old(
    remote_client: Client, run_ref: DatasetRef
) -> None:
    (record,) = remote_client.wait([remote_client.run(LOAD, {'run': run_ref})])
    new = remote_client.recompute(record)
    assert new.derives_from is not None
    assert new.derives_from.record == record.id
    assert new.derives_from.reason == 'recompute'


def test_publish_is_idempotent_and_an_unknown_publisher_raises(
    remote_client: Client, run_ref: DatasetRef
) -> None:
    (record,) = remote_client.wait([remote_client.run(LOAD, {'run': run_ref})])
    ref = record.ref('data')
    pid = remote_client.publish(ref, 'fake', allow_reused=True)
    assert remote_client.publish(ref, 'fake', allow_reused=True) == pid
    with pytest.raises(KeyError):
        remote_client.publish(ref, 'nope', allow_reused=True)


def test_provenance_of_a_completed_record(
    remote_client: Client, run_ref: DatasetRef
) -> None:
    (record,) = remote_client.wait([remote_client.run(LOAD, {'run': run_ref})])
    provenance = remote_client.provenance(record)
    assert provenance['record'] == record.id
    assert provenance['spec'] == 'load/v1'


def test_two_clients_on_one_server_see_only_their_own_proposal(
    server_url: str, run_ref: DatasetRef
) -> None:
    with (
        closing(
            remote(server_url, instrument='dream', proposal='p1', submitter='simon')
        ) as p1,
        closing(
            remote(server_url, instrument='dream', proposal='p2', submitter='simon')
        ) as p2,
    ):
        record = p2.run(LOAD, {'run': run_ref})
        p2.wait([record])
        assert record.id not in [r.id for r in p1.records()]
        assert record.id in [r.id for r in p2.records()]


def test_output_of_a_dropped_copy_raises_lookup_error(
    remote_client: Client, run_ref: DatasetRef
) -> None:
    (record,) = remote_client.wait([remote_client.run(LOAD, {'run': run_ref})])
    ref = record.ref('data')
    remote_client.drop(ref)
    with pytest.raises(LookupError):
        remote_client.output(ref)
