# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Section B of docs/developer/user-stories.md: manual and interactive reduction."""

# ruff: noqa: F821

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from ess.apps import Client
from ess.apps.testing import FakeDatasets

from .conftest import (
    ANGLE,
    CONTRIBUTE,
    CUT,
    FINALIZE,
    IOFQ,
    NORMALIZE,
    FakeClock,
    Measure,
)

tuesday = datetime(2026, 9, 8, tzinfo=UTC)


@pytest.mark.xfail(reason='sessions and stages are not implemented')
def test_b1_tune_a_sans_reduction_and_save_the_result_as_a_template(
    client: Client, measure: Measure, datasets: FakeDatasets
) -> None:
    run = measure(1, [1.0, 2.0, 3.0, 4.0])
    with client.session() as session:
        tune = session.stage(
            Template(IOFQ, params={'run': run}, blanks=('bins', 'threshold'))
        )
        for bins, threshold in [(1, 0.0), (4, 0.0), (4, 1.5), (2, 1.5)]:
            client.compute(tune, {'bins': bins, 'threshold': threshold}, label='iofq')

    final = client.latest('iofq')
    beamtime = Template(
        final.request.spec, params=final.request.params, blanks=('run',)
    )
    new = measure(2, [2.0, 1.0, 4.0, 3.0])
    requests = apply(beamtime, [new], datasets, member_field='run')
    (reduced,) = client.wait(client.submit(requests, label='iofq-beamtime')).values()

    assert len(client.records(label='iofq')) == 4
    assert (beamtime.params['bins'], beamtime.params['threshold']) == (2, 1.5)
    assert client.output(reduced, 'iofq').values.tolist() == [2.0, 7.0]


@pytest.mark.xfail(reason='sessions, stages, and accumulators are not implemented')
def test_b2_add_a_run_to_a_sum_then_remove_one(
    client: Client, measure: Measure
) -> None:
    r611, r612, r613 = (
        measure(611, [1.0, 3.0]),
        measure(612, [2.0, 6.0]),
        measure(613, [3.0, 1.0]),
    )
    with client.session() as session:
        contribute = session.stage(Template(CONTRIBUTE, blanks=('run',)))
        total = session.accumulator(PARTS_SUM)
        parts = {}
        for run in (r611, r612):
            parts[run] = client.compute(contribute, {'run': run})
            total.push(parts[run])
        first = client.compute(FINALIZE, client.compute(total).refs(), label='sum')

        parts[r613] = client.compute(contribute, {'run': r613})
        total.push(parts[r613])
        added = client.compute(FINALIZE, client.compute(total).refs(), label='sum')

    kept = [parts[r611], parts[r613]]
    summed = client.compute(
        PARTS_SUM,
        {
            'numerator': [p.ref('numerator') for p in kept],
            'denominator': [p.ref('denominator') for p in kept],
        },
    )
    removed = client.compute(FINALIZE, summed.refs(), label='sum')

    assert [
        client.output(r, 'normalized').values.tolist() for r in (first, added, removed)
    ] == [[0.25, 0.75], [0.375, 0.625], [0.5, 0.5]]
    assert client.output(
        client.compute(NORMALIZE, {'runs': [r611, r612]}), 'normalized'
    ) == client.output(first, 'normalized')
    assert client.provenance(removed).datasets() == [r611, r613]
    assert client.records(label='sum') == [first, added, removed]
    assert len(client.records(spec=CONTRIBUTE)) == 3


@pytest.mark.xfail(reason='sessions and stages are not implemented')
def test_b3_compare_two_parameter_sets_side_by_side(
    client: Client, measure: Measure
) -> None:
    run = measure(1, [1.0, 2.0, 3.0, 4.0])
    with client.session() as session:
        tune = session.stage(
            Template(IOFQ, params={'run': run}, blanks=('bins', 'threshold'))
        )
        plain = client.compute(tune, {'bins': 2, 'threshold': 0.0}, label='iofq')
        masked = client.compute(
            tune, {'bins': 2, 'threshold': 2.5}, label='iofq-masked'
        )
        finer = client.compute(tune, {'bins': 4, 'threshold': 2.5}, label='iofq-masked')

    assert [
        client.output(r, 'iofq').values.tolist() for r in (plain, masked, finer)
    ] == [[3.0, 7.0], [0.0, 7.0], [0.0, 0.0, 3.0, 4.0]]
    assert client.latest('iofq') == plain
    assert client.records(label='iofq-masked') == [masked, finer]


@pytest.mark.xfail(reason='views (client.output(..., index=)) are not implemented')
def test_b4_explore_a_4d_volume(client: Client, measure: Measure) -> None:
    run = measure(1, [1.0, 2.0, 3.0, 4.0])
    volume = client.compute(ANGLE, {'run': run})
    cuts = [client.output(volume, 'counts', index=i) for i in range(4)]
    fit = client.compute(CUT, {'data': volume.ref('counts'), 'index': 2})

    assert [c.value for c in cuts] == [1.0, 2.0, 3.0, 4.0]
    assert client.output(fit, 'cut').value == 3.0
    assert client.records() == [volume, fit]


@pytest.mark.xfail(reason='sessions and stages are not implemented')
def test_b5_notebook_kernel_dies_mid_session(
    client: Client, measure: Measure, connect: Callable[..., Client]
) -> None:
    run = measure(1, [1.0, 2.0, 3.0, 4.0])
    with client.session() as session:
        tune = session.stage(Template(IOFQ, params={'run': run}, blanks=('bins',)))
        tuned = client.compute(tune, {'bins': 1}, label='iofq')
        crash()

    client = connect()
    last = client.latest('iofq')
    with client.session() as session:
        tune = session.stage(
            Template(IOFQ, params={'run': last.request.params['run']}, blanks=('bins',))
        )
        again = client.compute(tune, {'bins': 4}, label='iofq')

    assert last == tuned
    assert client.records(label='iofq') == [tuned, again]
    assert client.output(again, 'iofq').values.tolist() == [1.0, 2.0, 3.0, 4.0]


def test_b6_find_last_weeks_result(
    client: Client, measure: Measure, clock: FakeClock
) -> None:
    run = measure(1, [1.0, 2.0, 3.0, 4.0])
    clock.set(tuesday)
    made = client.compute(IOFQ, {'run': run, 'threshold': 1.5})
    clock.set(tuesday + timedelta(days=7))
    client.compute(IOFQ, {'run': run, 'threshold': 2.5})

    (found,) = client.records(since=tuesday, until=tuesday + timedelta(days=1))
    assert found == made
    assert found.created == tuesday
    assert found.request.params == {
        'run': run,
        'bins': 2,
        'threshold': 1.5,
        'can': None,
        'beam_centre': None,
        'normalization': None,
    }
