# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The throwaway shape: every run a subprocess, outputs to disk, marker, reconcile."""

import hashlib
import json
from pathlib import Path

import pytest

from ess.apps.client import Client, local
from ess.apps.examples import FAIL, LOAD, REBIN, SUM, write_run
from ess.apps.records import Status
from ess.apps.sources import FolderSource
from ess.apps.spec import DatasetRef, OutputRef, dataset_ref


@pytest.fixture
def client(tmp_path: Path, datasets: Path):
    client = local(
        tmp_path / 'store',
        instrument='dream',
        proposal='p1',
        submitter='simon',
        registry='ess.apps.examples:registry',
        sources=[FolderSource(datasets, '*.h5')],
        throwaway=True,
    )
    yield client
    client.close()


@pytest.fixture
def run_ref(datasets: Path) -> DatasetRef:
    """The run identity ``dream_1.h5`` carries; a subprocess gets a path at dispatch."""
    write_run(datasets / 'dream_1.h5', [1.0, 2.0, 3.0, 4.0])
    return dataset_ref(instrument='dream', run=1)


def test_run_is_dispatched_then_reconciled_from_the_marker(
    client: Client, run_ref: DatasetRef
) -> None:
    record = client.run(LOAD, {'run': run_ref, 'scale': 2.0})
    assert record.status == Status.DISPATCHED
    assert record.launcher_job is not None
    (done,) = client.wait([record])
    assert done.status == Status.COMPLETED, done.failure
    assert done.outputs == {'total': {'value': 20.0, 'unit': 'counts'}}
    assert not done.reused
    data_ref = done.ref('data')
    assert client.backend.data.has_copy(data_ref)
    assert not client.backend.data.in_cache(data_ref)
    assert client.output(data_ref).sum().value == 20.0
    assert client.backend.data.in_cache(data_ref)


def test_a_dataset_is_located_before_the_subprocess_starts(
    client: Client, run_ref: DatasetRef, datasets: Path
) -> None:
    """The subprocess is handed a path; it never asks a dataset source itself."""
    (done,) = client.wait([client.run(LOAD, {'run': run_ref})])
    assert done.status == Status.COMPLETED, done.failure
    file = datasets / 'dream_1.h5'
    checksum = hashlib.sha256(file.read_bytes()).hexdigest()
    assert done.checksums == {str(run_ref): checksum}
    job = json.loads((client.backend.launcher.workdir(done) / 'job.json').read_text())
    assert job['locations'] == {'run:dream/1': str(file)}


def test_map_combine_runs_through_subprocesses(
    client: Client, run_ref: DatasetRef
) -> None:
    group = client.submit_group(
        {
            'a': client.request(LOAD, {'run': run_ref}, label='b1', member_key='a'),
            'b': client.request(
                LOAD, {'run': run_ref, 'scale': 3.0}, label='b1', member_key='b'
            ),
            'sum': client.request(
                SUM,
                {
                    'runs': [
                        OutputRef(record='@a', output='data'),
                        OutputRef(record='@b', output='data'),
                    ]
                },
            ),
        }
    )
    assert group['sum'].status == Status.WAITING
    done = dict(zip(group, client.wait(group.values()), strict=True))
    assert {k: v.status for k, v in done.items()} == dict.fromkeys(
        group, Status.COMPLETED
    )
    assert client.output(done['sum'].ref('total')).sum().value == 40.0
    assert [r.request.member_key for r in client.records(label='b1')] == ['a', 'b']


def test_failure_in_subprocess_carries_the_reason(client: Client) -> None:
    record = client.run(FAIL, {'message': 'bad file'})
    (done,) = client.wait([record])
    assert done.status == Status.FAILED
    assert done.failure.kind == 'RuntimeError'
    assert done.failure.message == 'bad file'
    assert 'RuntimeError' in done.failure.traceback


def test_cancel_kills_the_process_and_dependents(
    client: Client, run_ref: DatasetRef
) -> None:
    group = client.submit_group(
        {
            'a': client.request(LOAD, {'run': run_ref}),
            'sum': client.request(
                SUM, {'runs': [OutputRef(record='@a', output='data')]}
            ),
        }
    )
    client.cancel(group['a'])
    assert client.record(group['a'].id).status == Status.CANCELLED
    assert client.record(group['sum'].id).status == Status.CANCELLED


def test_a_request_on_a_cancelled_record_is_cancelled(
    client: Client, run_ref: DatasetRef
) -> None:
    """The producer ended before the request existed, so no transition reaches it."""
    loaded = client.run(LOAD, {'run': run_ref})
    client.cancel(loaded)
    consumer = client.run(REBIN, {'data': loaded.ref('data')})
    assert consumer.status == Status.CANCELLED


def test_persisted_request_keeps_reference_form(
    client: Client, run_ref: DatasetRef
) -> None:
    (loaded,) = client.wait([client.run(LOAD, {'run': run_ref})])
    rebinned = client.run(
        REBIN, {'data': loaded.ref('data'), 'offset': loaded.ref('total')}
    )
    (done,) = client.wait([rebinned])
    assert done.status == Status.COMPLETED, done.failure
    assert done.request.params['offset'] == {
        'record': loaded.id,
        'output': 'total',
        'key': None,
    }
    # The runner is given the total, 10 counts, added to each of the 4 points.
    assert client.output(done).sum().value == 10.0 + 4 * 10.0
    assert client.provenance(done)['inputs'][0]['spec'] == 'load/v1'


def test_dead_runner_without_marker_is_failed(
    client: Client, run_ref: DatasetRef
) -> None:
    record = client.run(LOAD, {'run': run_ref})
    proc = client.backend.launcher._procs.pop(record.id)
    proc.kill()
    proc.wait()
    (done,) = client.wait([record])
    assert done.status == Status.FAILED
    assert done.failure.kind == 'runner-exit'
