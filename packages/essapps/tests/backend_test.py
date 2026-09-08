# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The session shape: everything in one process, outputs staying in memory."""

from pathlib import Path

import pytest
import scipp as sc

from ess.apps.backend import SubmitError
from ess.apps.client import Client
from ess.apps.datastore import MissingCopyError
from ess.apps.examples import FAIL, LOAD, REBIN, SUM
from ess.apps.records import Status
from ess.apps.spec import FILE_SPEC, Ref, SpecId


def ref(record, output, key=None) -> Ref:
    return Ref(record=record.id, output=output, key=key)


def test_file_record_is_created_once_per_path(client: Client, run_file: Path) -> None:
    first = client.file(run_file)
    assert client.file(run_file) == first
    record = client.record(first.record)
    assert record.spec == FILE_SPEC.id
    assert record.status == Status.COMPLETED
    assert record.request.params['origin']['path'] == str(run_file.resolve())
    assert client.output(first) == run_file


def test_run_completes_with_inline_and_stored_outputs(
    client: Client, run_ref: Ref
) -> None:
    record = client.run(LOAD, {'run': run_ref, 'scale': 2.0})
    assert record.status == Status.COMPLETED
    assert record.outputs == {'total': {'value': 72.0, 'unit': 'counts'}}
    assert record.stored_outputs == [ref(record, 'data')]
    assert record.resolved_params['scale'] == 2.0
    assert record.binding == 'in_process'
    assert 'essapps' in record.package_versions
    assert client.output(record, 'data').sum().value == 72.0
    assert client.output(ref(record, 'total')) == {'value': 72.0, 'unit': 'counts'}


def test_session_outputs_stay_in_memory_until_written_out(
    client: Client, run_ref: Ref
) -> None:
    record = client.run(LOAD, {'run': run_ref})
    data = client.backend.data
    assert data.in_cache(ref(record, 'data'))
    assert not data.has_copy(ref(record, 'data'))
    path = client.write_out(ref(record, 'data'))
    assert path.exists()
    assert data.has_copy(ref(record, 'data'))


def test_chaining_through_memory_and_literal_outputs(
    client: Client, run_ref: Ref
) -> None:
    loaded = client.run(LOAD, {'run': run_ref})
    rebinned = client.run(
        REBIN, {'data': ref(loaded, 'data'), 'bins': 2, 'offset': ref(loaded, 'total')}
    )
    assert rebinned.status == Status.COMPLETED, rebinned.failure
    assert rebinned.request.params['offset'] == {
        'record': loaded.id,
        'output': 'total',
        'key': None,
    }
    assert rebinned.resolved_params['offset'] == {'value': 36.0, 'unit': 'counts'}
    assert client.output(rebinned).sizes == {'x': 2}
    assert client.backend.records.referencing(loaded.id, 'data') == [rebinned.id]


def test_warm_workflow_is_reused_within_a_session(client: Client, run_ref: Ref) -> None:
    first = client.run(LOAD, {'run': run_ref}, slot='tune')
    second = client.run(LOAD, {'run': run_ref, 'scale': 3.0}, slot='tune')
    assert not first.reused
    assert second.reused
    assert client.latest('tune').id == second.id
    assert [r.id for r in client.records(slot='tune')] == [first.id, second.id]


def test_group_with_pending_outputs_runs_in_dependency_order(
    client: Client, run_ref: Ref
) -> None:
    group = client.submit_group(
        {
            'sum': client.request(
                SUM,
                {
                    'runs': [
                        Ref(record='@a', output='data'),
                        Ref(record='@b', output='data'),
                    ]
                },
            ),
            'a': client.request(LOAD, {'run': run_ref}),
            'b': client.request(LOAD, {'run': run_ref, 'scale': 2.0}),
        }
    )
    assert {k: v.status for k, v in group.items()} == dict.fromkeys(
        group, Status.COMPLETED
    )
    total = client.output(ref(group['sum'], 'total'))
    assert total.sum().value == 36.0 * 3
    per_run = client.output(ref(group['sum'], 'per_run', '1'))
    assert per_run.sum().value == 72.0
    assert group['sum'].request.params['runs'][0]['record'] == group['a'].id


def test_group_is_refused_whole_when_one_member_is_invalid(
    client: Client, run_ref: Ref
) -> None:
    with pytest.raises(SubmitError, match='no group member'):
        client.submit_group(
            {
                'a': client.request(LOAD, {'run': run_ref}),
                'sum': client.request(
                    SUM, {'runs': [Ref(record='@nope', output='data')]}
                ),
            }
        )
    assert client.records() == [client.record(run_ref.record)]


@pytest.mark.parametrize(
    ('params', 'message'),
    [
        ({'run': {'record': 'zzz', 'output': 'file'}}, 'no such record'),
        ({'run': 'not-a-ref-or-path-dict', 'scale': 'x'}, 'scale'),
        ({}, 'run'),
    ],
)
def test_validation_reports_errors_before_any_record_exists(
    client: Client, params, message
) -> None:
    report = client.validate(client.request(LOAD, params))
    assert not report.ok
    assert any(message in e for e in report.errors)
    with pytest.raises(SubmitError):
        client.submit(client.request(LOAD, params))
    assert client.records() == []


def test_validation_checks_reference_types(client: Client, run_ref: Ref) -> None:
    loaded = client.run(LOAD, {'run': run_ref})
    literal_into_data = client.validate(
        client.request(REBIN, {'data': ref(loaded, 'total')})
    )
    assert any(
        'literal output cannot fill a data field' in e for e in literal_into_data.errors
    )
    data_into_literal = client.validate(
        client.request(
            REBIN, {'data': ref(loaded, 'data'), 'offset': ref(loaded, 'data')}
        )
    )
    assert any(
        'data cannot fill a literal field' in e for e in data_into_literal.errors
    )
    missing_output = client.validate(
        client.request(REBIN, {'data': ref(loaded, 'nope')})
    )
    assert any('no output' in e for e in missing_output.errors)
    unknown_spec = client.validate(client.request(SpecId(name='nope', version=1)))
    assert unknown_spec.layers == ('schema',)


def test_reference_across_proposals_is_refused(client: Client, run_ref: Ref) -> None:
    other = Client(client.backend, instrument='dream', proposal='p2', submitter='eve')
    report = other.validate(other.request(LOAD, {'run': run_ref}))
    assert any('belongs to proposal p1' in e for e in report.errors)


def test_failure_is_structured_and_propagates_to_dependents(
    client: Client, run_ref: Ref
) -> None:
    group = client.submit_group(
        {
            'bad': client.request(FAIL, {'message': 'no monitor'}),
            'sum': client.request(SUM, {'runs': [Ref(record='@bad', output='data')]}),
        }
    )
    assert group['bad'].status == Status.FAILED
    assert group['bad'].failure.kind == 'RuntimeError'
    assert group['bad'].failure.message == 'no monitor'
    assert group['sum'].status == Status.FAILED
    assert group['sum'].failure.kind == 'upstream'


def test_missing_collection_key_fails_the_consumer_only(
    client: Client, run_ref: Ref
) -> None:
    summed = client.submit_group(
        {
            'a': client.request(LOAD, {'run': run_ref}),
            'sum': client.request(SUM, {'runs': [Ref(record='@a', output='data')]}),
        }
    )['sum']
    consumer = client.run(SUM, {'runs': [ref(summed, 'per_run', '7')]})
    assert consumer.status == Status.FAILED
    assert consumer.failure.kind == 'missing-key'
    assert client.record(summed.id).status == Status.COMPLETED


def test_retry_and_recompute_link_to_the_old_record(
    client: Client, run_ref: Ref
) -> None:
    old = client.run(LOAD, {'run': run_ref})
    new = client.recompute(old)
    assert new.id != old.id
    assert new.derives_from.record == old.id
    assert new.derives_from.reason == 'recompute'
    assert new.request == old.request
    assert new.status == Status.COMPLETED


def test_cancel_terminal_record_is_a_no_op(client: Client, run_ref: Ref) -> None:
    done = client.run(LOAD, {'run': run_ref})
    client.cancel(done)
    assert client.record(done.id).status == Status.COMPLETED


def test_missing_copy_is_reported_after_eviction(client: Client, run_ref: Ref) -> None:
    loaded = client.run(LOAD, {'run': run_ref})
    client.backend.data.evict(ref(loaded, 'data'))
    with pytest.raises(MissingCopyError):
        client.output(loaded, 'data')
    assert client.recompute(loaded).status == Status.COMPLETED


def test_drop_keeps_the_record(client: Client, run_ref: Ref) -> None:
    loaded = client.run(LOAD, {'run': run_ref})
    client.write_out(ref(loaded, 'data'))
    client.drop(ref(loaded, 'data'))
    assert client.record(loaded.id).status == Status.COMPLETED
    with pytest.raises(MissingCopyError):
        client.output(loaded, 'data')


def test_view_returns_plain_arrays(client: Client, run_ref: Ref) -> None:
    loaded = client.run(LOAD, {'run': run_ref})
    v = client.view(ref(loaded, 'data'))
    assert v['dims'] == ['x']
    assert v['unit'] == 'counts'
    assert list(v['values']) == [1, 2, 3, 4, 5, 6, 7, 8]
    assert list(v['coords']['x']) == list(range(8))
    assert not isinstance(v['values'], sc.Variable)
