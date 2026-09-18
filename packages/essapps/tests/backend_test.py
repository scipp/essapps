# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The session shape: everything in one process, outputs staying in memory."""

import hashlib
from pathlib import Path

import pytest
import scipp as sc

from ess.apps.backend import SubmitError
from ess.apps.client import Client, local
from ess.apps.datastore import MissingCopyError
from ess.apps.examples import (
    EXPORT,
    FAIL,
    HISTOGRAM,
    LOAD,
    REBIN,
    SUM,
    registry,
    write_run,
)
from ess.apps.records import Status
from ess.apps.sources import Dataset
from ess.apps.spec import DatasetRef, Format, OutputRef, SpecId, as_ref, dataset_ref
from ess.apps.testing import FakeDatasetSource


def test_a_dataset_reference_is_located_and_checksummed_at_dispatch(
    client: Client, run_ref: DatasetRef, run_file: Path
) -> None:
    record = client.run(LOAD, {'run': run_ref})
    assert record.status == Status.COMPLETED, record.failure
    assert as_ref(record.request.params['run']) == run_ref
    checksum = hashlib.sha256(run_file.read_bytes()).hexdigest()
    assert record.checksums == {str(run_ref): checksum}
    assert record.resolved_params['run'] == {'dataset': 'run:dream/1'}
    assert client.provenance(record)['raw'] == [{'dataset': 'run:dream/1'}]


@pytest.fixture
def counting_source(run_file: Path) -> FakeDatasetSource:
    """A source that records every dataset it was asked to locate."""
    return FakeDatasetSource(Dataset(path=run_file, instrument='dream', run=1))


@pytest.fixture
def counting_client(tmp_path: Path, counting_source: FakeDatasetSource):
    client = local(
        tmp_path / 'store',
        instrument='dream',
        proposal='p1',
        submitter='simon',
        registry=registry(),
        sources=[counting_source],
    )
    yield client
    client.close()


def test_a_dataset_two_parameters_name_is_one_origin(
    counting_client: Client, counting_source: FakeDatasetSource, run_ref: DatasetRef
) -> None:
    """One dataset, however many parameters name it: located once, listed once."""
    record = counting_client.run(SUM, {'runs': [run_ref, run_ref]})
    assert record.status == Status.COMPLETED, record.failure
    assert counting_source.located == [run_ref]
    assert counting_client.provenance(record)['raw'] == [{'dataset': 'run:dream/1'}]


def test_a_dataset_no_source_has_fails_the_run(client: Client) -> None:
    record = client.run(LOAD, {'run': dataset_ref(instrument='dream', run=77)})
    assert record.status == Status.FAILED
    assert record.failure.kind == 'missing-dataset'


def test_a_dataset_identified_by_path_is_its_own_location(
    client: Client, tmp_path: Path
) -> None:
    outside = write_run(tmp_path / 'elsewhere.h5', [1.0, 2.0])
    record = client.run(LOAD, {'run': dataset_ref(path=outside)})
    assert record.status == Status.COMPLETED, record.failure
    assert record.outputs['total']['value'] == 3.0


def test_a_dataset_cannot_fill_a_literal_field(client: Client, run_ref: DatasetRef):
    """A literal field admits a value or an output of a record, never a dataset."""
    report = client.validate(
        client.request(REBIN, {'data': run_ref, 'offset': run_ref})
    )
    assert report.layers == ('schema', 'params')
    assert any(e.startswith('offset') for e in report.errors)


def test_the_picker_lists_completed_outputs_and_datasets(
    client: Client, run_ref: DatasetRef
) -> None:
    loaded = client.run(LOAD, {'run': run_ref})
    rows = {str(row.ref): row for row in client.pick()}
    assert rows[str(run_ref)].format is None
    assert rows[str(run_ref)].display['name'] == 'dream_1.h5'
    assert rows[str(loaded.ref('data'))].format is Format.SCIPP
    assert rows[str(loaded.ref('data'))].display['name'] == 'load/v1 data'
    assert 'total' not in {getattr(r.ref, 'output', None) for r in client.pick()}
    arrays = {str(row.ref) for row in client.pick(Format.SCIPP)}
    assert arrays == {str(run_ref), str(loaded.ref('data'))}
    assert {str(row.ref) for row in client.pick(Format.NEXUS)} == {str(run_ref)}


def test_run_completes_with_inline_and_stored_outputs(
    client: Client, run_ref: DatasetRef
) -> None:
    record = client.run(LOAD, {'run': run_ref, 'scale': 2.0})
    assert record.status == Status.COMPLETED
    assert record.outputs == {'total': {'value': 72.0, 'unit': 'counts'}}
    assert record.stored_outputs == [record.ref('data')]
    assert record.resolved_params['scale'] == 2.0
    assert record.binding == 'in_process'
    assert 'essapps' in record.package_versions
    assert client.output(record, 'data').sum().value == 72.0
    assert client.output(record.ref('total')) == {'value': 72.0, 'unit': 'counts'}


def test_session_outputs_stay_in_memory_until_written_out(
    client: Client, run_ref: DatasetRef
) -> None:
    record = client.run(LOAD, {'run': run_ref})
    data = client.backend.data
    assert data.in_cache(record.ref('data'))
    assert not data.has_copy(record.ref('data'))
    path = client.write_out(record.ref('data'))
    assert path.exists()
    assert data.has_copy(record.ref('data'))


def test_chaining_through_memory_and_literal_outputs(
    client: Client, run_ref: DatasetRef
) -> None:
    loaded = client.run(LOAD, {'run': run_ref})
    rebinned = client.run(
        REBIN, {'data': loaded.ref('data'), 'bins': 2, 'offset': loaded.ref('total')}
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


def test_a_tuned_parameter_comes_out_of_a_held_stage_within_a_session(
    client: Client, run_ref: DatasetRef
) -> None:
    data = client.run(LOAD, {'run': run_ref}).ref('data')
    first = client.run(HISTOGRAM, {'data': data, 'bins': 2}, label='tune')
    second = client.run(HISTOGRAM, {'data': data, 'bins': 3}, label='tune')
    assert not first.reused
    assert second.reused
    assert client.latest('tune').id == second.id
    assert [r.id for r in client.records(label='tune')] == [first.id, second.id]


def test_group_with_pending_outputs_runs_in_dependency_order(
    client: Client, run_ref: DatasetRef
) -> None:
    group = client.submit_group(
        {
            'sum': client.request(
                SUM,
                {
                    'runs': [
                        OutputRef(record='@a', output='data'),
                        OutputRef(record='@b', output='data'),
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
    total = client.output(group['sum'].ref('total'))
    assert total.sum().value == 36.0 * 3
    per_run = client.output(group['sum'].ref('per_run', '1'))
    assert per_run.sum().value == 72.0
    assert group['sum'].request.params['runs'][0]['record'] == group['a'].id


def test_group_is_refused_whole_when_one_member_is_invalid(
    client: Client, run_ref: DatasetRef
) -> None:
    with pytest.raises(SubmitError, match='no group member'):
        client.submit_group(
            {
                'a': client.request(LOAD, {'run': run_ref}),
                'sum': client.request(
                    SUM, {'runs': [OutputRef(record='@nope', output='data')]}
                ),
            }
        )
    assert client.records() == []


@pytest.mark.parametrize(
    ('params', 'message'),
    [
        ({'run': {'record': 'zzz', 'output': 'file'}}, 'no such record'),
        ({'run': 'not-a-ref-or-path-dict', 'scale': 'x'}, 'scale'),
        ({}, 'run'),
        ({'run': {'instrument': 'dream', 'run': 1}, 'sacle': 2.0}, 'sacle'),
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


def test_validation_checks_reference_types(client: Client, run_ref: DatasetRef) -> None:
    loaded = client.run(LOAD, {'run': run_ref})
    literal_into_data = client.validate(
        client.request(REBIN, {'data': loaded.ref('total')})
    )
    assert any(
        'literal output cannot fill a data field' in e for e in literal_into_data.errors
    )
    data_into_literal = client.validate(
        client.request(
            REBIN, {'data': loaded.ref('data'), 'offset': loaded.ref('data')}
        )
    )
    assert any(
        'data cannot fill a literal field' in e for e in data_into_literal.errors
    )
    missing_output = client.validate(
        client.request(REBIN, {'data': loaded.ref('nope')})
    )
    assert any('no output' in e for e in missing_output.errors)
    unknown_spec = client.validate(client.request(SpecId(name='nope', version=1)))
    assert unknown_spec.layers == ('schema',)


def test_reference_across_proposals_is_refused(
    client: Client, run_ref: DatasetRef
) -> None:
    loaded = client.run(LOAD, {'run': run_ref})
    other = Client(client.backend, instrument='dream', proposal='p2', submitter='eve')
    report = other.validate(other.request(REBIN, {'data': loaded.ref('data')}))
    assert any('belongs to proposal p1' in e for e in report.errors)


def test_failure_is_structured_and_propagates_to_dependents(
    client: Client, run_ref: DatasetRef
) -> None:
    group = client.submit_group(
        {
            'bad': client.request(FAIL, {'message': 'no monitor'}),
            'sum': client.request(
                SUM, {'runs': [OutputRef(record='@bad', output='data')]}
            ),
        }
    )
    assert group['bad'].status == Status.FAILED
    assert group['bad'].failure.kind == 'RuntimeError'
    assert group['bad'].failure.message == 'no monitor'
    assert group['sum'].status == Status.FAILED
    assert group['sum'].failure.kind == 'upstream'


def test_a_request_on_a_failed_record_fails_at_once(client: Client) -> None:
    """Nothing transitions after the producer ended, so the wait would never end."""
    failed = client.run(FAIL, {'message': 'no monitor'})
    assert failed.status == Status.FAILED
    consumer = client.run(REBIN, {'data': failed.ref('data')})
    assert consumer.status == Status.FAILED
    assert consumer.failure.kind == 'upstream'
    assert failed.id in consumer.failure.message


def test_missing_collection_key_fails_the_consumer_only(
    client: Client, run_ref: DatasetRef
) -> None:
    summed = client.submit_group(
        {
            'a': client.request(LOAD, {'run': run_ref}),
            'sum': client.request(
                SUM, {'runs': [OutputRef(record='@a', output='data')]}
            ),
        }
    )['sum']
    consumer = client.run(SUM, {'runs': [summed.ref('per_run', '7')]})
    assert consumer.status == Status.FAILED
    assert consumer.failure.kind == 'missing-key'
    assert client.record(summed.id).status == Status.COMPLETED


def test_retry_and_recompute_link_to_the_old_record(
    client: Client, run_ref: DatasetRef
) -> None:
    old = client.run(LOAD, {'run': run_ref})
    new = client.recompute(old)
    assert new.id != old.id
    assert new.derives_from.record == old.id
    assert new.derives_from.reason == 'recompute'
    assert new.request == old.request
    assert new.status == Status.COMPLETED


def test_a_retry_and_a_correction_supersede_the_current_head(
    client: Client, run_ref: DatasetRef
) -> None:
    first = client.run(LOAD, {'run': run_ref}, label='tune')
    assert first.supersedes is None
    retried = client.backend.retry(first.id)
    assert retried.supersedes == first.id
    assert client.latest('tune').id == retried.id

    corrected = client.run(LOAD, {'run': run_ref, 'scale': 2.0}, label='tune')
    assert corrected.supersedes == retried.id
    assert client.latest('tune').id == corrected.id


def test_two_requests_in_one_group_chain_in_group_order(
    client: Client, run_ref: DatasetRef
) -> None:
    """Within a group, a later request supersedes the earlier one in the group,
    not the head that was current before the group was submitted."""
    group = client.submit_group(
        {
            'a': client.request(LOAD, {'run': run_ref}, label='tune', member_key='k'),
            'b': client.request(
                LOAD, {'run': run_ref, 'scale': 2.0}, label='tune', member_key='k'
            ),
        }
    )
    assert group['a'].supersedes is None
    assert group['b'].supersedes == group['a'].id
    assert client.latest('tune', 'k').id == group['b'].id


def test_cancel_terminal_record_is_a_no_op(client: Client, run_ref: DatasetRef) -> None:
    done = client.run(LOAD, {'run': run_ref})
    client.cancel(done)
    assert client.record(done.id).status == Status.COMPLETED


def test_missing_copy_is_reported_after_eviction(
    client: Client, run_ref: DatasetRef
) -> None:
    loaded = client.run(LOAD, {'run': run_ref})
    client.backend.data.evict(loaded.ref('data'))
    with pytest.raises(MissingCopyError):
        client.output(loaded, 'data')
    assert client.recompute(loaded).status == Status.COMPLETED


def test_drop_keeps_the_record(client: Client, run_ref: DatasetRef) -> None:
    loaded = client.run(LOAD, {'run': run_ref})
    client.write_out(loaded.ref('data'))
    client.drop(loaded.ref('data'))
    assert client.record(loaded.id).status == Status.COMPLETED
    with pytest.raises(MissingCopyError):
        client.output(loaded, 'data')


def test_view_returns_plain_arrays(client: Client, run_ref: DatasetRef) -> None:
    loaded = client.run(LOAD, {'run': run_ref})
    v = client.view(loaded.ref('data'))
    assert v['dims'] == ['x']
    assert v['unit'] == 'counts'
    assert list(v['values']) == [1, 2, 3, 4, 5, 6, 7, 8]
    assert list(v['coords']['x']) == list(range(8))
    assert not isinstance(v['values'], sc.Variable)


def test_record_ref_names_the_single_output_or_demands_a_name(
    client: Client, run_ref: DatasetRef
) -> None:
    loaded = client.run(LOAD, {'run': run_ref})
    with pytest.raises(ValueError, match="has outputs \\['data', 'total'\\]"):
        loaded.ref()
    assert loaded.ref('data') == OutputRef(record=loaded.id, output='data')
    rebinned = client.run(REBIN, {'data': loaded.ref('data')})
    assert rebinned.ref() == OutputRef(record=rebinned.id, output='result')
    failed = client.run(FAIL)
    with pytest.raises(ValueError, match='is failed'):
        failed.ref()


def test_element_of_a_literal_collection_output_is_inlined(
    client: Client, run_ref: DatasetRef
) -> None:
    loaded = client.run(LOAD, {'run': run_ref})
    summed = client.run(SUM, {'runs': [loaded.ref('data'), loaded.ref('data')]})
    rebinned = client.run(
        REBIN,
        {'data': loaded.ref('data'), 'offset': summed.ref('totals', 'x')},
    )
    assert rebinned.status == Status.COMPLETED, rebinned.failure
    assert rebinned.resolved_params['offset'] == {'value': 72.0, 'unit': 'counts'}


def test_cyclic_group_is_refused(client: Client) -> None:
    with pytest.raises(SubmitError, match='cycle'):
        client.submit_group(
            {
                'a': client.request(
                    SUM, {'runs': [OutputRef(record='@b', output='total')]}
                ),
                'b': client.request(
                    SUM, {'runs': [OutputRef(record='@a', output='total')]}
                ),
            }
        )
    assert client.records() == []


def test_session_file_output_is_readable(client: Client, run_ref: DatasetRef) -> None:
    exported = client.run(
        EXPORT, {'data': client.run(LOAD, {'run': run_ref}).ref('data')}
    )
    assert exported.status == Status.COMPLETED, exported.failure
    assert client.output(exported).read_bytes().startswith(b'x,')


def test_evicted_session_input_is_a_missing_copy_status(
    client: Client, run_ref: DatasetRef
) -> None:
    loaded = client.run(LOAD, {'run': run_ref})
    client.backend.data.evict(loaded.ref('data'))
    rebinned = client.run(REBIN, {'data': loaded.ref('data')})
    assert rebinned.status == Status.FAILED
    assert rebinned.failure.kind == 'missing-copy'
