# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Section B of docs/developer/user-stories.md: manual and interactive reduction."""

import pytest

from ess.apps import Client, Template, apply

from .conftest import (
    ANGLE,
    CONTRIBUTE,
    CUT,
    FINALIZE,
    IOFQ,
    NORMALIZE,
    PARTS_SUM,
    Measure,
)


def test_b1_tune_a_sans_reduction_and_save_the_result_as_a_template(
    client: Client, measure: Measure
) -> None:
    run = measure(1, [1.0, 2.0, 3.0, 4.0])
    tune = client.stage(
        Template(IOFQ, params={'run': run}, blanks=('bins', 'threshold'))
    )
    for bins, threshold in [(1, 0.0), (4, 0.0), (4, 1.5), (2, 1.5)]:
        client.compute(tune, {'bins': bins, 'threshold': threshold}, label='tuning')

    final = client.latest('tuning')
    beamtime = Template(
        final.request.spec, params=final.request.params, blanks=('run',)
    )
    new = measure(2, [2.0, 1.0, 4.0, 3.0])
    requests = apply(beamtime, [new], client.datasets, member_field='run')
    (reduced,) = client.compute(requests, label='iofq-beamtime').values()

    assert len(client.records(label='tuning')) == 4
    assert (beamtime.params['bins'], beamtime.params['threshold']) == (2, 1.5)
    assert client.output(reduced, 'iofq').values.tolist() == [2.0, 7.0]


def test_b2_add_a_run_to_a_sum_then_start_over_without_one(
    client: Client, measure: Measure
) -> None:
    r611, r612, r613 = (
        measure(611, [1.0, 3.0]),
        measure(612, [2.0, 6.0]),
        measure(613, [3.0, 1.0]),
    )
    contribute = client.stage(Template(CONTRIBUTE, blanks=('run',)))
    total = client.accumulator(PARTS_SUM)
    parts = {}
    for run in (r611, r612):
        parts[run] = client.compute(contribute, {'run': run})
        total.push(parts[run].refs('numerator', 'denominator'))
    first = client.compute(FINALIZE, client.compute(total).refs(), label='sum')

    parts[r613] = client.compute(contribute, {'run': r613})
    total.push(parts[r613].refs('numerator', 'denominator'))
    added = client.compute(FINALIZE, client.compute(total).refs(), label='sum')

    kept = [parts[r611], parts[r613]]
    summed = client.compute(
        PARTS_SUM,
        {'parts': [p.refs('numerator', 'denominator') for p in kept]},
    )
    restarted = client.compute(FINALIZE, summed.refs(), label='sum')
    again = client.compute(NORMALIZE, {'runs': [r611, r613]})

    assert [
        client.output(r, 'normalized').values.tolist()
        for r in (first, added, restarted)
    ] == [[0.25, 0.75], [0.375, 0.625], [0.5, 0.5]]
    assert client.output(again, 'normalized').values.tolist() == [0.5, 0.5]
    assert client.provenance(restarted).datasets() == [r611, r613]
    assert client.records(label='sum') == [first, added, restarted]


@pytest.mark.xfail(reason='views (client.output(..., index=)) are not implemented')
def test_b4_explore_a_4d_volume(client: Client, measure: Measure) -> None:
    run = measure(1, [1.0, 2.0, 3.0, 4.0])
    volume = client.compute(ANGLE, {'run': run})
    cuts = [client.output(volume, 'counts', index=i) for i in range(4)]
    fit = client.compute(CUT, {'data': volume.ref('counts'), 'index': 2})

    assert [c.value for c in cuts] == [1.0, 2.0, 3.0, 4.0]
    assert client.output(fit, 'cut').value == 3.0
    assert client.records() == [volume, fit]
