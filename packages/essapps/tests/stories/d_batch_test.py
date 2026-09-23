# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Section D of docs/developer/user-stories.md: batch.
"""

from pathlib import Path

import pytest

from ess.apps.backend import SubmitError
from ess.apps.batch import apply, batch_table, retry
from ess.apps.client import Client, local
from ess.apps.examples import LOAD
from ess.apps.records import Status, Template
from ess.apps.testing import FakeDatasetSource

from .conftest import Measure


def test_d1_temperature_scan(
    client: Client, measure: Measure, catalogue: FakeDatasetSource, tmp_path: Path
) -> None:
    scan = {
        f'{240 + 10 * run}K': measure(run, counts=[float(run)] * 2)
        for run in range(1, 6)
    }
    catalogue.locate(scan['260K']).write_bytes(b'truncated')
    template = Template(name='load-defaults', spec=LOAD, blanks=('run',))

    client.submit_group(
        apply(
            client,
            template,
            pinned={kelvin: {'run': run} for kelvin, run in scan.items()},
            label='scan',
        )
    )
    chosen = {
        kelvin: client.output(client.latest('scan', kelvin), 'data')
        for kelvin in ('250K', '270K', '290K')
    }
    exported = [
        client.write_out(record.ref('data'), into=tmp_path / 'export')
        for record in client.batch('scan')
        if record.status == Status.COMPLETED
    ]

    assert {kelvin: data.values.tolist() for kelvin, data in chosen.items()} == {
        '250K': [1.0, 1.0],
        '270K': [3.0, 3.0],
        '290K': [5.0, 5.0],
    }
    assert batch_table(client, 'scan')['status'].to_dict() == {
        '250K': 'completed',
        '260K': 'failed',
        '270K': 'completed',
        '280K': 'completed',
        '290K': 'completed',
    }
    assert client.latest('scan', '260K').failure.kind == 'OSError'
    assert len(exported) == 4


# The backend that restarts never reaps its runners; a real restart ends its process.
@pytest.mark.filterwarnings('ignore:subprocess .* is still running:ResourceWarning')
def test_d2_overnight_cluster_batch(
    tmp_path: Path, catalogue: FakeDatasetSource, measure: Measure
) -> None:
    def backend() -> Client:
        return local(
            tmp_path / 'cluster',
            instrument='dream',
            proposal='p1',
            submitter='simon',
            registry='ess.apps.examples:registry',
            throwaway=True,
            sources=[catalogue],
        )

    good, bad = measure(1), measure(2)
    catalogue.locate(bad).write_bytes(b'truncated')
    template = Template(name='nmx-defaults', spec=LOAD, blanks=('run',))
    web = backend()
    submitted = web.submit_group(apply(web, template, web.datasets(), label='night'))
    web.close()  # the backend restarts while the runs are in flight

    web = backend()
    web.wait(submitted.values())
    table = batch_table(web, 'night')
    failed = web.latest('night', str(bad))
    measure(2)  # the transfer is repeated
    (rerun,) = web.wait(web.submit_group(retry(web, template, label='night')).values())

    assert table['status'].to_dict() == {str(good): 'completed', str(bad): 'failed'}
    assert web.output(web.latest('night', str(good)), 'total')['value'] == 10.0
    assert failed.failure.kind == 'OSError'
    assert rerun.supersedes == failed.id
    assert rerun.status == Status.COMPLETED
    web.close()


def test_d3_cancel_and_resubmit(service: Client, measure: Measure) -> None:
    for run in (1, 2, 3):
        measure(run)
    wrong = Template(
        name='load-defaults', spec=LOAD, params={'scale': 20.0}, blanks=('run',)
    )
    service.submit_group(apply(service, wrong, service.datasets(), label='scan'))

    for record in service.batch('scan'):
        service.cancel(record)
    fixed = wrong.revise(scale=2.0)
    resubmitted = service.submit_group(
        apply(service, fixed, service.datasets(), label='scan')
    )
    service.wait(resubmitted.values())

    assert [r.status for r in service.records(label='scan')] == [
        Status.CANCELLED
    ] * 3 + [Status.COMPLETED] * 3
    assert batch_table(service, 'scan')['template'].tolist() == ['load-defaults/v2'] * 3


def test_d4_typo_caught_before_500_failures(client: Client, measure: Measure) -> None:
    """A decimal comma stands in for a value only the params model refuses."""
    for run in (1, 2):
        measure(run)
    typo = Template(
        name='load-defaults', spec=LOAD, params={'scale': '2,5'}, blanks=('run',)
    )
    group = apply(client, typo, client.datasets(), label='scan')

    reports = [client.validate(request) for request in group.values()]
    with pytest.raises(SubmitError):
        client.submit_group(group)

    assert [report.errors for report in reports] == [
        ('scale: Input should be a valid number, unable to parse string as a number',)
    ] * 2
    assert client.records() == []


def test_d5_understand_why_a_run_failed(
    client: Client, measure: Measure, catalogue: FakeDatasetSource
) -> None:
    good, bad, repeat = measure(1), measure(2), measure(3)
    catalogue.locate(bad).write_bytes(b'truncated')
    template = Template(name='load-defaults', spec=LOAD, blanks=('run',))
    client.submit_group(
        apply(
            client,
            template,
            pinned={'250K': {'run': good}, '260K': {'run': bad}},
            label='scan',
        )
    )

    failed = client.latest('scan', '260K')
    rerun = client.submit_group(
        apply(client, template, pinned={'260K': {'run': repeat}}, label='scan')
    )['260K']

    assert failed.failure.model_dump(exclude={'traceback'}) == {
        'kind': 'OSError',
        'message': 'Unable to synchronously open file (file signature not found)',
    }
    assert rerun.supersedes == failed.id
    assert batch_table(client, 'scan')[['status', 'run']].to_dict('index') == {
        '250K': {'status': 'completed', 'run': good},
        '260K': {'status': 'completed', 'run': repeat},
    }


@pytest.mark.xfail(
    raises=ModuleNotFoundError,
    strict=True,
    reason='no toy workflow has a second spec version, and Template.revise '
    'changes only params, not the spec',
)
def test_d6_rerun_last_years_batch_with_a_new_workflow_version(
    client: Client, measure: Measure
) -> None:
    template = Template(
        name='load-defaults', spec=LOAD, params={'scale': 2.0}, blanks=('run',)
    )
    last_year = client.submit_group(
        apply(
            client,
            template,
            pinned={'250K': {'run': measure(1)}, '260K': {'run': measure(2)}},
            label='scan',
        )
    )
    from ess.apps.examples_v2 import LOAD as LOAD_V2  # renames scale to factor

    runs = batch_table(client, 'scan')[['run']]
    with pytest.raises(SubmitError, match='scale: not a parameter of load/v2'):
        client.submit_group(
            apply(client, template.revise(spec=LOAD_V2), pinned=runs, label='scan')
        )
    moved = template.revise(spec=LOAD_V2, params={'factor': 2.0})
    rerun = client.submit_group(apply(client, moved, pinned=runs, label='scan'))

    assert [(str(r.spec), r.request.origin.template) for r in rerun.values()] == [
        ('load/v2', 'load-defaults/v2')
    ] * 2
    assert [r.supersedes for r in rerun.values()] == [r.id for r in last_year.values()]
    assert client.output(last_year['250K'], 'total') == {
        'value': 20.0,
        'unit': 'counts',
    }
