# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from pathlib import Path

import pytest

from ess.apps.backend import SubmitError
from ess.apps.client import Client
from ess.apps.examples import LOAD, REBIN, write_run
from ess.apps.records import Status
from ess.apps.spec import FILE_SPEC, Ref
from ess.apps.templates import Template, TriggerLoop, batch
from ess.apps.testing import Dataset, FakeDatasetSource


@pytest.fixture
def template(client: Client, run_ref: Ref) -> Template:
    request = client.request(LOAD, {'run': run_ref, 'scale': 2.0})
    return Template.from_request('load-defaults', request, LOAD)


def test_saving_a_request_blanks_its_data_fields(template: Template) -> None:
    assert template.blanks == ('run',)
    assert template.params == {'scale': 2.0}
    with pytest.raises(ValueError, match="needs \\['run'\\]"):
        template.fill()


def test_revising_makes_a_new_version_by_copy(template: Template) -> None:
    revised = template.revise(scale=3.0)
    assert revised.version == 2
    assert revised.derived_from == 'load-defaults/v1'
    assert template.params == {'scale': 2.0}
    assert revised.params == {'scale': 3.0}


def test_batch_runs_members_by_key(
    client: Client, template: Template, tmp_path: Path
) -> None:
    runs = {
        '300K': client.file(write_run(tmp_path / 'a.h5', [1.0, 2.0])),
        '310K': client.file(write_run(tmp_path / 'b.h5', [3.0, 4.0])),
    }
    records = batch(
        client, template, {k: {'run': v} for k, v in runs.items()}, batch_id='scan1'
    )
    assert {k: r.status for k, r in records.items()} == dict.fromkeys(
        runs, Status.COMPLETED
    )
    assert records['310K'].outputs['total']['value'] == 14.0
    assert [r.request.member_key for r in client.records(batch='scan1')] == [
        '300K',
        '310K',
    ]


def test_batch_is_refused_whole(
    client: Client, template: Template, run_ref: Ref
) -> None:
    with pytest.raises(ValueError, match='needs'):
        batch(client, template, {'a': {'run': run_ref}, 'b': {}}, batch_id='scan2')
    assert client.records(batch='scan2') == []
    bad = template.revise(scale='not a number')
    with pytest.raises(SubmitError):
        batch(client, bad, {'a': {'run': run_ref}}, batch_id='scan3')
    assert client.records(batch='scan3') == []


def test_trigger_loop_fires_once_per_dataset_and_reports_refusals(
    client: Client, template: Template, tmp_path: Path
) -> None:
    source = FakeDatasetSource()
    loop = TriggerLoop(
        client,
        template,
        source,
        rule=lambda ds: {} if ds.metadata.get('type') == 'sample' else None,
        dataset_field='run',
    )
    assert loop.run_once() == []
    sample = Dataset('pid/1', write_run(tmp_path / 'r1.h5', [1.0]), {'type': 'sample'})
    source.add(sample)
    source.add(Dataset('pid/2', tmp_path / 'r2.h5', {'type': 'background'}))
    (fired,) = loop.run_once()
    assert fired.status == Status.COMPLETED
    assert fired.request.batch == 'load-defaults/v1'
    assert loop.status.fired == 1
    assert loop.run_once() == []
    file_record = client.record(fired.request.refs()[0].record)
    assert file_record.spec == FILE_SPEC.id
    assert file_record.request.params['origin'] == {'pid': 'pid/1'}


def test_trigger_loop_refusal_is_visible(client: Client, tmp_path: Path) -> None:
    bad = Template(name='bad', spec=REBIN.id, params={'bins': 0}, blanks=('data',))
    source = FakeDatasetSource()
    source.add(Dataset('pid/1', write_run(tmp_path / 'r1.h5', [1.0])))
    loop = TriggerLoop(client, bad, source, rule=lambda ds: {}, dataset_field='data')
    assert loop.run_once() == []
    assert loop.status.last_refusal is not None
    assert any('bins' in e for e in loop.status.last_errors)
