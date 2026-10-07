# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Section B of docs/developer/user-stories.md: manual and interactive reduction."""

from ess.dispatch import Client, Template, apply

from .conftest import ANGLE, CUT, IOFQ, NORMALIZE, Measure


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
    total = client.accumulator(
        Template(NORMALIZE, params={'scale': 2.0}, blanks=('runs',))
    )
    for run in (r611, r612):
        total.push({'runs': {'run': run}})
    first = client.output(total, 'normalized')  # runs 611 and 612

    total.push({'runs': {'run': r613}})  # 611 and 612 are not reduced again
    added = client.output(total, 'normalized')  # all three

    rows = [{'run': run} for run in (r611, r613)]
    again = client.compute(NORMALIZE, {'runs': rows, 'scale': 2.0})

    assert [
        value.values.tolist()
        for value in (first, added, client.output(again, 'normalized'))
    ] == [[0.5, 1.5], [0.75, 1.25], [1.0, 1.0]]
    assert client.provenance(total).datasets() == [r611, r612, r613]
    assert client.provenance(again).datasets() == [r611, r613]
    assert client.records() == [again]


def test_b4_explore_a_4d_volume(client: Client, measure: Measure) -> None:
    run = measure(1, [1.0, 2.0, 3.0, 4.0])
    volume = client.compute(ANGLE, {'run': run})
    cuts = [client.output(volume, 'counts', select={'q': i}) for i in range(4)]
    fit = client.compute(CUT, {'data': volume.ref('counts'), 'index': 2})

    assert [c.value for c in cuts] == [1.0, 2.0, 3.0, 4.0]
    assert client.output(fit, 'cut').value == 3.0
    assert client.records() == [volume, fit]  # the selections made no record
