# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Section D of docs/developer/user-stories.md: batch reduction."""

from collections.abc import Callable
from dataclasses import replace
from itertools import islice

import pytest

from ess.apps import Client, Request, Selector, SubmitError, Template, apply

from .conftest import COPY, CUT, IOFQ, IOFQ_V2, VOLUME, Measure


def test_d1_temperature_scan(
    client: Client,
    measure: Measure,
    corrupt: Callable[..., None],
) -> None:
    temperatures = ['250K', '260K', '270K', '280K', '290K']
    runs = [
        measure(n, [float(n)] * 4, temperature=t)
        for n, t in enumerate(temperatures, start=1)
    ]
    corrupt(runs[1])
    template = Template(IOFQ, params={'bins': 1}, blanks=('run',))
    requests = apply(template, runs, client.datasets, member_field='temperature')
    scan = client.compute(requests, label='scan')

    assert [
        client.output(scan[t], 'iofq').values.tolist() for t in ('250K', '270K', '290K')
    ] == [[4.0], [12.0], [20.0]]
    assert client.wait(scan) == {
        '250K': 'completed',
        '260K': 'failed',
        '270K': 'completed',
        '280K': 'completed',
        '290K': 'completed',
    }
    assert {r.member: r for r in client.records(label='scan')} == scan


def test_d2_overnight_cluster_batch(
    client: Client,
    connect: Callable[..., Client],
    measure: Measure,
    corrupt: Callable[..., None],
    repair: Callable[..., None],
) -> None:
    runs = {str(n): measure(n, [float(n)] * 4) for n in range(1, 31)}
    corrupt(runs['7'])
    for member, run in runs.items():
        client.submit(IOFQ, {'run': run}, label='night', member=member)

    morning = connect()
    night = {r.member: r for r in morning.records(label='night')}
    failed = [night[m] for m, s in morning.wait(night).items() if s == 'failed']
    reasons = morning.failure(failed)
    repair(runs['7'])
    reruns = [
        morning.submit(r.request.spec, r.request.params, label=r.label, member=r.member)
        for r in failed
    ]
    morning.wait(reruns)

    assert [r.member for r in failed] == ['7']
    assert reasons == ['file signature not found']
    assert reruns[0].request == failed[0].request
    assert morning.latest('night', member='7') == reruns[0]
    assert morning.output(reruns[0], 'iofq').values.tolist() == [14.0, 14.0]
    assert morning.status(failed) == ['failed']
    assert len(morning.records(label='night')) == 31


def test_d3_cancel_and_resubmit(client: Client, measure: Measure) -> None:
    runs = [measure(n, [1.0, 1.0, 1.0, 1.0]) for n in range(1, 501)]
    wrong = Template(IOFQ, params={'threshold': 20.0}, blanks=('run',))
    first = client.submit(
        apply(wrong, runs, client.datasets, member_field='run'), label='scan'
    )
    client.cancel(first)
    fixed = replace(wrong, params={'threshold': 0.5})
    second = client.compute(
        apply(fixed, runs, client.datasets, member_field='run'), label='scan'
    )

    assert set(client.wait(first).values()) <= {'completed', 'cancelled'}
    assert [
        client.output(r, 'iofq').values.tolist() for r in second.values()
    ] == 500 * [[2.0, 2.0]]
    assert len(client.records(label='scan')) == 1000


def test_d4_typo_caught_before_500_failures(client: Client, measure: Measure) -> None:
    runs = [measure(n, [1.0, 1.0]) for n in range(1, 501)]
    typo = Template(IOFQ, params={'threshold': '2,5'}, blanks=('run',))
    requests = apply(typo, runs, client.datasets, member_field='run')

    with pytest.raises(SubmitError, match='threshold'):
        client.submit(requests, label='scan')
    assert client.records() == []


def test_d6_rerun_a_batch_with_a_new_workflow_version(
    client: Client, measure: Measure
) -> None:
    template = Template(IOFQ, params={'threshold': 1.5}, blanks=('run',))
    runs = [measure(n, [1.0, 2.0, 3.0, 4.0]) for n in (1, 2, 3)]
    client.compute(
        apply(template, runs, client.datasets, member_field='run'), label='scan'
    )

    before = client.records(label='scan')
    runs = [r.request.params['run'] for r in before]
    renamed = apply(
        replace(template, spec=IOFQ_V2), runs, client.datasets, member_field='run'
    )
    with pytest.raises(SubmitError, match='threshold'):
        client.submit(renamed, label='scan')
    moved = replace(template, spec=IOFQ_V2, params={'mask_below': 1.5})
    requests = apply(moved, runs, client.datasets, member_field='run')
    after = list(client.compute(requests, label='scan').values())

    assert [
        client.output(r, 'iofq').values.tolist() for r in (before[0], after[0])
    ] == [
        [2.0, 7.0],
        [0.0, 2.0, 3.0, 4.0],
    ]
    assert [r.request.params['bins'] for r in (before[0], after[0])] == [2, 4]
    assert len(client.records(label='scan')) == 6


def test_d7_rotation_scan_over_a_thousand_angles(
    client: Client, measure: Measure
) -> None:
    for n in range(1, 1001):
        measure(n, [1.0, float(n)], scan='17')
        if n == 500:
            measure(5, [1.0, 5.0], scan='17')

    cuts, pushed = [], []
    volume = client.accumulator(Template(VOLUME, blanks=('runs',)))
    for run in islice(client.datasets.watch(Selector(scan='17')), 1000):
        volume.push({'runs': {'run': run}})  # waits until the previous cut has run
        pushed.append(run)
        cuts.append(
            client.submit(
                CUT,
                {'data': volume.ref('counts'), 'index': 0},
                label='cut',
                member='17',
            )
        )
    total = client.compute(COPY, {'data': volume.ref('counts')})

    assert [client.output(c, 'cut').value for c in cuts] == [
        float(k) for k in range(1, 1001)
    ]
    assert client.output(total, 'data').values.tolist() == [1000.0, 500500.0]
    provenance = client.provenance(total)
    assert provenance.accumulated == (  # the runs, in push order
        Request(VOLUME, {'runs': [{'run': run} for run in pushed]}),
    )
    assert len(provenance.datasets()) == 1000  # run 5, measured again, once
