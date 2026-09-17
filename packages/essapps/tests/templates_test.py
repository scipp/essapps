# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from pathlib import Path

import pytest

from ess.apps.backend import SubmitError
from ess.apps.client import Client
from ess.apps.examples import LOAD, REBIN, write_run
from ess.apps.records import Status
from ess.apps.sources import Dataset
from ess.apps.spec import DatasetRef
from ess.apps.templates import Template, TriggerLoop, batch
from ess.apps.testing import FakeDatasetSource


@pytest.fixture
def template(client: Client, run_ref: DatasetRef) -> Template:
    request = client.request(LOAD, {'run': run_ref, 'scale': 2.0})
    return Template.from_request('load-defaults', request, LOAD)


@pytest.fixture
def scan(datasets: Path) -> dict[str, DatasetRef]:
    """Two more runs in the folder the client's dataset source reads."""
    write_run(datasets / 'dream_2.h5', [1.0, 2.0])
    write_run(datasets / 'dream_3.h5', [3.0, 4.0])
    return {
        '300K': DatasetRef(instrument='dream', run=2),
        '310K': DatasetRef(instrument='dream', run=3),
    }


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
    client: Client, template: Template, scan: dict[str, DatasetRef]
) -> None:
    records = batch(
        client, template, {k: {'run': v} for k, v in scan.items()}, label='scan1'
    )
    assert {k: r.status for k, r in records.items()} == dict.fromkeys(
        scan, Status.COMPLETED
    )
    assert records['310K'].outputs['total']['value'] == 14.0
    assert [r.request.member_key for r in client.records(label='scan1')] == [
        '300K',
        '310K',
    ]


def test_a_corrected_member_supersedes_the_batch_record(
    client: Client, template: Template, scan: dict[str, DatasetRef]
) -> None:
    first = batch(
        client, template, {k: {'run': v} for k, v in scan.items()}, label='scan1'
    )
    corrected = client.run(
        LOAD,
        {'run': scan['300K'], 'scale': 4.0},
        label='scan1',
        member_key='300K',
    )
    assert [r.id for r in client.batch('scan1')] == [corrected.id, first['310K'].id]
    assert client.latest('scan1', '300K').id == corrected.id
    assert client.latest('scan1', '310K').id == first['310K'].id


def test_batch_is_refused_whole(
    client: Client, template: Template, run_ref: DatasetRef
) -> None:
    with pytest.raises(ValueError, match='needs'):
        batch(client, template, {'a': {'run': run_ref}, 'b': {}}, label='scan2')
    assert client.records(label='scan2') == []
    bad = template.revise(scale='not a number')
    with pytest.raises(SubmitError):
        batch(client, bad, {'a': {'run': run_ref}}, label='scan3')
    assert client.records(label='scan3') == []


def test_trigger_loop_fires_once_per_dataset_and_reports_refusals(
    client: Client, template: Template, tmp_path: Path
) -> None:
    source = FakeDatasetSource()
    client.sources.append(source)
    loop = TriggerLoop(
        client,
        template,
        rule=lambda ds: {} if ds.metadata.get('type') == 'sample' else None,
        dataset_field='run',
    )
    assert loop.run_once() == []
    source.add(
        Dataset(
            path=write_run(tmp_path / 'r1.h5', [1.0]),
            pid='pid/1',
            metadata={'type': 'sample'},
        )
    )
    source.add(Dataset(path=tmp_path / 'r2.h5', pid='pid/2', metadata={'type': 'bg'}))
    (fired,) = loop.run_once()
    assert fired.status == Status.COMPLETED, fired.failure
    assert fired.request.label == 'load-defaults'
    assert fired.request.member_key == 'dataset:pid/1'
    assert fired.request.datasets() == [DatasetRef(pid='pid/1')]
    assert loop.status.fired == 1
    assert loop.run_once() == []


def test_trigger_loop_refusal_is_visible(client: Client, tmp_path: Path) -> None:
    bad = Template(name='bad', spec=REBIN.id, params={'bins': 0}, blanks=('data',))
    client.sources.append(
        FakeDatasetSource(Dataset(path=write_run(tmp_path / 'r1.h5', [1.0])))
    )
    loop = TriggerLoop(client, bad, rule=lambda ds: {}, dataset_field='data')
    assert loop.run_once() == []
    assert loop.status.last_refusal is not None
    assert any('bins' in e for e in loop.status.last_errors)
