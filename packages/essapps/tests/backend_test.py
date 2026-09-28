# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The session shape: everything in one process, outputs staying in memory."""

import hashlib
from pathlib import Path
from typing import Any

import pytest
import scipp as sc
from pydantic import BaseModel

from ess.apps.backend import SubmitError
from ess.apps.client import Client, local, local_backend
from ess.apps.datastore import MissingCopyError, Serializers
from ess.apps.examples import (
    COMBINE,
    EXPORT,
    FAIL,
    FLOORED,
    HISTOGRAM,
    LOAD,
    REBIN,
    SUM,
    load_workflow,
    registry,
    write_run,
)
from ess.apps.records import RunRequest, Status, Template
from ess.apps.runner import file_checksum
from ess.apps.sources import Dataset, FolderSource
from ess.apps.spec import (
    DatasetRef,
    Format,
    OpaqueFile,
    OutputRef,
    SpecId,
    WorkflowSpec,
    as_ref,
    dataset_ref,
)
from ess.apps.testing import FakeAccess, FakeDatasetSource

from .conftest import make_client

PART = OutputRef(record='r1', output='numerator')


class ScaledByFive(BaseModel):
    """The params of ``LOAD`` with another default for ``scale``."""

    run: OpaqueFile
    scale: float = 5.0


def test_a_request_holds_plain_params_and_reads_back_equal(
    client: Client, run_ref: DatasetRef
) -> None:
    request = client.request(LOAD, {'run': run_ref, 'scale': 2.0})
    assert request.params == {'run': run_ref.model_dump(), 'scale': 2.0}
    # A request without outputs is recorded with the spec's results.
    recorded = client.record(client.submit(request).id).request
    assert recorded == request.model_copy(update={'outputs': ('data', 'total')})


def test_a_plain_run_records_every_parameter_its_default_filled(
    client: Client, run_ref: DatasetRef
) -> None:
    record = client.run(LOAD, {'run': run_ref})
    assert record.request.params == {'run': run_ref.model_dump(), 'scale': 1.0}
    assert client.record(record.id).request.params == record.request.params


def test_a_default_given_or_omitted_shares_one_held_stage(
    client: Client, run_ref: DatasetRef
) -> None:
    data = client.run(LOAD, {'run': run_ref}).ref('data')
    omitted = Template(spec=HISTOGRAM, params={'data': data}, blanks=('bins',))
    given = Template(
        spec=HISTOGRAM, params={'data': data, 'threshold': 0.0}, blanks=('bins',)
    )
    client.run(omitted, {'bins': 2})
    assert client.run(given, {'bins': 3}).reused


def test_a_value_is_recorded_in_the_form_its_field_gives_it(
    client: Client, run_ref: DatasetRef
) -> None:
    """``1`` given for a float field is ``1.0``: one recorded value."""
    records = [
        client.run(LOAD, {'run': run_ref, 'scale': 1}),
        client.run(LOAD, {'run': run_ref, 'scale': 1.0}),
        client.run(LOAD, {'run': run_ref}),
    ]
    assert all(type(r.request.params['scale']) is float for r in records)
    assert all(r.request.params == records[0].request.params for r in records)


def test_a_call_of_a_held_stage_records_what_a_plain_run_records(
    client: Client, run_ref: DatasetRef
) -> None:
    """Where a session cuts the pipeline is not part of what ran."""
    data = client.run(LOAD, {'run': run_ref}).ref('data')
    tune = Template(spec=HISTOGRAM, params={'data': data}, blanks=('bins',))
    client.run(tune, {'bins': 2})
    held = client.run(tune, {'bins': 3})
    plain = client.run(HISTOGRAM, {'data': data, 'bins': 3})
    assert held.reused
    assert not plain.reused
    assert held.request == plain.request


def test_a_plain_run_records_every_field_of_its_request(
    client: Client, run_ref: DatasetRef
) -> None:
    record = client.run(LOAD, {'run': run_ref, 'scale': 2.0})
    dumped = client.record(record.id).request.model_dump(mode='json')
    assert dumped == {
        'spec': {'name': 'load', 'version': 1},
        'params': {'run': run_ref.model_dump(), 'scale': 2.0},
        'outputs': ['data', 'total'],
        'instrument': 'dream',
        'proposal': 'p1',
        'submitter': 'simon',
        'label': None,
        'member_key': None,
        'origin': {
            'template': None,
            'rule': None,
            'lookup': None,
            'entries': {},
            'pinned': {},
        },
    }


def test_a_recompute_runs_with_the_defaults_recorded_at_submit(
    tmp_path: Path, datasets: Path, run_ref: DatasetRef
) -> None:
    """A spec whose default changed does not change what a recompute runs."""
    first = make_client(tmp_path / 'store', datasets)
    record = first.run(LOAD, {'run': run_ref})
    first.close()
    reg = registry()
    reg.bind(LOAD.model_copy(update={'params': ScaledByFive}), load_workflow)
    client = local(
        tmp_path / 'store',
        instrument='dream',
        proposal='p1',
        submitter='simon',
        registry=reg,
        sources=[FolderSource(datasets, '*.h5', proposal='p1')],
    )
    again = client.recompute(record)
    fresh = client.run(LOAD, {'run': run_ref})
    client.close()
    assert again.request == record.request
    assert again.request.params['scale'] == 1.0
    assert fresh.request.params['scale'] == 5.0


def test_a_held_stage_is_named_by_the_values_not_varied_in_any_order(
    client: Client, run_ref: DatasetRef
) -> None:
    data = client.run(LOAD, {'run': run_ref}).ref('data')

    def tune(**params: object) -> Template:
        return Template(spec=HISTOGRAM, params=params, blanks=('bins',))

    first = client.run(tune(data=data, threshold=1.0), {'bins': 2})
    second = client.run(tune(threshold=1.0, data=data), {'bins': 3})
    other = client.run(tune(data=data, threshold=2.0), {'bins': 3})
    assert first.id != second.id
    assert second.reused
    assert not other.reused


def test_a_dataset_reference_is_located_and_checksummed_at_dispatch(
    client: Client, run_ref: DatasetRef, run_file: Path
) -> None:
    record = client.run(LOAD, {'run': run_ref})
    assert record.status == Status.COMPLETED, record.failure
    assert as_ref(record.request.params['run']) == run_ref
    checksum = hashlib.sha256(run_file.read_bytes()).hexdigest()
    assert record.checksums == {str(run_ref): checksum}
    provenance = client.provenance(record)
    assert provenance['raw'] == [run_ref.model_dump()]
    assert provenance['outputs'] == ['data', 'total']


def test_a_dataset_in_a_varied_parameter_is_checksummed(
    client: Client, run_ref: DatasetRef, run_file: Path
) -> None:
    load = Template(spec=LOAD, params={'scale': 2.0}, blanks=('run',))
    record = client.run(load, {'run': run_ref})
    assert record.status == Status.COMPLETED, record.failure
    checksum = hashlib.sha256(run_file.read_bytes()).hexdigest()
    assert record.checksums == {str(run_ref): checksum}
    assert client.provenance(record)['raw'] == [run_ref.model_dump()]


@pytest.fixture
def counting_source(run_file: Path) -> FakeDatasetSource:
    """A source that records every dataset it was asked to locate."""
    return FakeDatasetSource(
        Dataset(
            path=run_file,
            proposals=['p1'],
            sha256=file_checksum(run_file),
            instrument='dream',
            run=1,
        )
    )


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
    assert counting_client.provenance(record)['raw'] == [run_ref.model_dump()]


def test_a_run_number_or_a_path_is_recorded_as_the_identity_it_names(
    client: Client, run_ref: DatasetRef, run_file: Path
) -> None:
    for typed in (dataset_ref(instrument='dream', run=1), dataset_ref(path=run_file)):
        record = client.run(LOAD, {'run': typed})
        assert record.status == Status.COMPLETED, record.failure
        assert record.request.params['run'] == run_ref.model_dump()


def test_a_dataset_no_source_knows_is_refused_at_submit(
    client: Client, tmp_path: Path
) -> None:
    outside = write_run(tmp_path / 'elsewhere.h5', [1.0, 2.0])
    for ref in (dataset_ref(instrument='dream', run=77), dataset_ref(path=outside)):
        with pytest.raises(SubmitError, match='no source knows it'):
            client.run(LOAD, {'run': ref})
    assert client.records() == []


def test_a_run_number_naming_two_datasets_is_refused(
    client: Client, datasets: Path
) -> None:
    write_run(datasets / 'dream_7.h5', [1.0])
    write_run(datasets / 'DREAM_7.h5', [2.0])
    with pytest.raises(SubmitError, match='names several datasets'):
        client.run(LOAD, {'run': dataset_ref(instrument='dream', run=7)})


def test_a_file_that_moved_is_found_by_its_identity_on_recompute(
    client: Client, run_ref: DatasetRef, run_file: Path, datasets: Path
) -> None:
    record = client.run(LOAD, {'run': run_ref})
    run_file.rename(datasets / 'renamed.h5')
    again = client.recompute(record)
    assert again.status == Status.COMPLETED, again.failure
    assert again.request == record.request


@pytest.mark.parametrize('change', ['rewrite', 'delete'])
def test_a_recompute_over_a_dataset_no_source_knows_any_more_is_refused(
    client: Client, run_ref: DatasetRef, run_file: Path, change: str
) -> None:
    """A file identified by its bytes and rewritten in place is another dataset."""
    record = client.run(LOAD, {'run': run_ref})
    if change == 'rewrite':
        write_run(run_file, [9.0])
    else:
        run_file.unlink()
    with pytest.raises(SubmitError, match=f'{run_ref}: no source knows it'):
        client.recompute(record)


def test_bytes_that_differ_from_the_sha256_they_are_located_by_fail_the_run(
    tmp_path: Path, run_file: Path
) -> None:
    """The file changed between locating it and reading it."""
    stale = Dataset(path=run_file, proposals=['p1'], sha256='0' * 64)
    client = local(
        tmp_path / 'store',
        instrument='dream',
        proposal='p1',
        submitter='simon',
        registry=registry(),
        sources=[FakeDatasetSource(stale)],
    )
    record = client.run(LOAD, {'run': stale.ref})
    client.close()
    assert record.status == Status.FAILED
    assert f'now has sha256 {file_checksum(run_file)}' in record.failure.message


def test_stand_ins_in_a_list_and_beside_a_group_reference_are_resolved(
    client: Client, run_ref: DatasetRef, run_file: Path, datasets: Path
) -> None:
    other = write_run(datasets / 'dream_2.h5', [1.0] * 8, uuid='dream-2')
    group = client.submit_group(
        {
            'a': client.request(LOAD, {'run': dataset_ref(path=other)}),
            'b': client.request(
                SUM,
                {
                    'runs': [
                        OutputRef(record='@a', output='data'),
                        dataset_ref(instrument='dream', run=1),
                        dataset_ref(path=run_file),
                    ]
                },
            ),
        }
    )
    assert group['a'].request.params['run'] == dataset_ref(uuid='dream-2').model_dump()
    assert group['b'].request.params['runs'] == [
        group['a'].ref('data').model_dump(),
        run_ref.model_dump(),
        run_ref.model_dump(),
    ]


def test_a_dataset_two_sources_know_by_different_identities_is_one(
    tmp_path: Path, datasets: Path
) -> None:
    """A catalogue knows the file by its PID and UUID, the folder by its UUID."""
    path = write_run(datasets / 'dream_3.h5', [1.0], uuid='u3')
    catalogue = FakeDatasetSource(
        Dataset(
            path=path,
            proposals=['p1', 'p2'],
            pid='pid/3',
            uuid='u3',
            instrument='dream',
            run=3,
        )
    )
    client = make_client(tmp_path / 'store', datasets, sources=[catalogue])
    (dataset,) = client.datasets(['p1', 'p2'])
    record = client.run(LOAD, {'run': dataset_ref(instrument='dream', run=3)})
    client.close()
    assert (dataset.ref, dataset.proposals) == (dataset_ref(pid='pid/3'), ['p1', 'p2'])
    assert record.status == Status.COMPLETED, record.failure
    assert record.request.params['run'] == dataset_ref(pid='pid/3').model_dump()


def test_a_dataset_of_two_proposals_is_readable_through_either(
    tmp_path: Path, run_file: Path
) -> None:
    shared = Dataset(path=run_file, proposals=['p1', 'p2'], pid='pid/shared')
    backend = local_backend(
        tmp_path / 'store', registry=registry(), sources=[FakeDatasetSource(shared)]
    )
    reports = {}
    for proposal in ('p1', 'p2', 'p3'):
        client = Client(backend, instrument='dream', proposal=proposal, submitter='x')
        reports[proposal] = client.validate(client.request(LOAD, {'run': shared.ref}))
    backend.close()
    assert reports['p1'].ok
    assert reports['p2'].ok
    assert not reports['p3'].ok


def test_a_dataset_whose_bytes_are_missing_fails_the_run_naming_the_sources(
    tmp_path: Path, datasets: Path
) -> None:
    landing = Dataset(path=tmp_path / 'gone.h5', proposals=['p1'], pid='pid/9')
    client = make_client(
        tmp_path / 'store',
        datasets,
        sources=[FakeDatasetSource(landing, locates=False)],
    )
    record = client.run(LOAD, {'run': landing.ref})
    client.close()
    assert record.status == Status.FAILED
    assert record.failure.kind == 'missing-dataset'
    assert str(datasets) in record.failure.message


@pytest.fixture
def facility(tmp_path: Path) -> FakeDatasetSource:
    """A vanadium run of the instrument's commissioning proposal."""
    path = write_run(tmp_path / 'vanadium.h5', [2.0, 2.0])
    return FakeDatasetSource(
        Dataset(path=path, proposals=['commissioning'], pid='pid/vanadium')
    )


@pytest.mark.parametrize(
    ('grants', 'errors'),
    [
        ({'simon': {'commissioning'}}, ()),
        (
            {},
            (
                "pid:pid/vanadium: belongs to proposals ['commissioning'], which "
                'simon may not read',
            ),
        ),
    ],
)
def test_a_dataset_of_another_proposal_needs_read_access(
    tmp_path: Path,
    datasets: Path,
    facility: FakeDatasetSource,
    grants: dict[str, set[str]],
    errors: tuple[str, ...],
) -> None:
    client = make_client(
        tmp_path / 'store', datasets, sources=[facility], access=FakeAccess(grants)
    )
    request = client.request(LOAD, {'run': dataset_ref(pid='pid/vanadium')})
    report = client.validate(request)
    client.close()
    assert report.errors == errors


def test_an_output_of_another_proposal_needs_read_access(
    tmp_path: Path, facility: FakeDatasetSource
) -> None:
    backend = local_backend(
        tmp_path / 'store',
        registry=registry(),
        sources=[facility],
        access=FakeAccess({'eve': {'commissioning'}}),
    )
    scientist = Client(
        backend, instrument='dream', proposal='commissioning', submitter='anna'
    )
    vanadium = scientist.run(LOAD, {'run': dataset_ref(pid='pid/vanadium')})
    eve, mallory = (
        Client(backend, instrument='dream', proposal='p2', submitter=submitter)
        for submitter in ('eve', 'mallory')
    )
    normalize = {'data': vanadium.ref('data')}
    readable = eve.validate(eve.request(REBIN, normalize))
    refused = mallory.validate(mallory.request(REBIN, normalize))
    backend.close()
    assert readable.ok
    assert refused.errors == (
        f'{vanadium.ref("data")}: belongs to proposal commissioning, which '
        'mallory may not read',
    )


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
    assert record.request.params['scale'] == 2.0
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


def test_write_out_into_a_folder_copies_the_file(
    client: Client, run_ref: DatasetRef, tmp_path: Path
) -> None:
    record = client.run(LOAD, {'run': run_ref})
    path = client.write_out(record.ref('data'), tmp_path / 'out')
    assert path.parent == tmp_path / 'out'
    assert sc.identical(Serializers().load(path), client.output(record.ref('data')))


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
    rebinned_data = client.output(rebinned)
    assert rebinned_data.sizes == {'x': 2}
    # The total, 36 counts, is added to each of the 8 points before binning.
    assert rebinned_data.sum().value == 36.0 + 8 * 36.0
    assert client.backend.record_store.referencing(loaded.id, 'data') == [rebinned.id]
    # The snapshot keeps the reference and carries the value it fed from the
    # snapshot of the record it came from.
    snapshot = client.provenance(rebinned)
    assert snapshot['params']['offset']['record'] == loaded.id
    (upstream,) = snapshot['inputs']
    assert upstream['literals']['total'] == {'value': 36.0, 'unit': 'counts'}


def test_a_tuned_parameter_comes_out_of_a_held_stage_within_a_session(
    client: Client, run_ref: DatasetRef
) -> None:
    data = client.run(LOAD, {'run': run_ref}).ref('data')
    tune = Template(
        spec=HISTOGRAM, params={'data': data}, blanks=('bins',), name='tune'
    )
    first = client.run(tune, {'bins': 2})
    second = client.run(tune, {'bins': 3})
    assert not first.reused
    assert second.reused
    assert first.request.params['bins'] == 2
    assert second.request.params['bins'] == 3
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


def test_a_missing_required_parameter_is_refused_at_submit(client: Client) -> None:
    """Every request is checked against the whole params model."""
    report = client.validate(client.request(LOAD, {}))
    assert report.errors == ('run: Field required',)
    with pytest.raises(SubmitError, match='run: Field required'):
        client.run(LOAD, {})


@pytest.mark.parametrize(
    ('params', 'outputs', 'message'),
    [
        ({'bogus': 1}, (), 'bogus: not a parameter'),
        ({}, ('nope',), 'not an output'),
        ({'scale': 'x'}, (), 'scale'),
    ],
)
def test_validation_checks_the_names_of_a_request(
    client: Client, run_ref: DatasetRef, params, outputs, message
) -> None:
    request = RunRequest(
        spec=LOAD.id,
        params={'run': run_ref, **params},
        outputs=outputs,
        instrument=client.instrument,
        proposal=client.proposal,
        submitter=client.submitter,
    )
    report = client.validate(request)
    assert any(message in e for e in report.errors), report.errors


def test_submit_refuses_a_varied_name_that_is_not_a_parameter(
    client: Client, run_ref: DatasetRef
) -> None:
    request = client.request(LOAD, {'run': run_ref})
    with pytest.raises(SubmitError, match='bogus: varied but not a parameter'):
        client.submit(request, vary=('bogus',))
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


@pytest.mark.parametrize(
    ('spec', 'params', 'message'),
    [
        (
            FLOORED,
            {'runs': [{'run': dataset_ref(path='a.h5'), 'flor': 3.0}]},
            'runs[0].flor: not a field of FlooredRun',
        ),
        (
            COMBINE,
            {'parts': [{'numerator': PART, 'denominator': PART, 'total': PART}]},
            'parts[0].total: not a field of Contribution',
        ),
    ],
)
def test_validation_checks_the_names_in_a_row(
    client: Client, spec: WorkflowSpec, params: dict[str, Any], message: str
) -> None:
    """A row drops a name it does not declare, and the default would stand."""
    report = client.validate(client.request(spec, params))
    assert message in report.errors


def test_validation_checks_a_reference_in_a_row(
    client: Client, run_ref: DatasetRef
) -> None:
    """A reference in a row fills the column of the row model it sits in."""
    loaded = client.run(LOAD, {'run': run_ref})
    for ref, message in [
        (loaded.ref('total'), 'literal output cannot fill a data field'),
        (loaded.ref('data'), 'output into opaque field'),
        (OutputRef(record='nope', output='data'), 'no such record'),
    ]:
        report = client.validate(client.request(FLOORED, {'runs': [{'run': ref}]}))
        assert any(message in e for e in report.errors), report.errors


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
    retried = client.backend.retry(first.id, client.submitter)
    assert retried.supersedes == first.id
    assert client.latest('tune').id == retried.id

    corrected = client.run(LOAD, {'run': run_ref, 'scale': 2.0}, label='tune')
    assert corrected.supersedes == retried.id
    assert client.latest('tune').id == corrected.id


def test_two_requests_in_one_group_chain_in_group_order(
    client: Client, run_ref: DatasetRef
) -> None:
    """Within a group, a later request supersedes the earlier one in the group,
    not the record that was latest before the group was submitted."""
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
    # The element of the collection, 72 counts, is added to each of the 8 points.
    assert client.output(rebinned).sum().value == 36.0 + 8 * 72.0


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
