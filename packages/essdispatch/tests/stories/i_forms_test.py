# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A value typed in by one user and computed by another: the request gives each
value, or each row, inline or by reference.
"""

from ess.dispatch import Client, Status, Template

from .conftest import (
    CENTRED_MULTI,
    CONTRIBUTE,
    FIND_CENTRE,
    SLICER,
    SLICES,
    Centre,
    Measure,
    TimeRange,
)


def test_i1_time_slices_found_by_a_first_reduction(
    client: Client, measure: Measure
) -> None:
    run = measure(611, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    slicer = client.compute(SLICER, params={'log': run})
    n = len(client.output(slicer, 'slices'))  # a reference names one row
    slices = client.accumulator(
        Template(SLICES, params={'sample': run}, blanks=('time_range',))
    )
    found = [slicer.ref('slices', key=str(i)) for i in range(n)]
    slices.push(time_range=found)

    assert client.output(slices, 'sliced').value == 21.0
    provenance = client.provenance(slices)
    assert provenance.request.params['time_range'] == found
    assert provenance.records() == [slicer]

    typed_in = slices.push(time_range=[TimeRange(start=0, stop=2)])  # slice 0
    assert client.wait(typed_in) == Status.FAILED
    assert client.failure(typed_in).startswith('push 2 failed: time_range[3]: repeats')
    assert client.output(slices, 'sliced').value == 21.0  # the sum goes on


def test_i2_beam_centre_typed_in_or_computed(client: Client, measure: Measure) -> None:
    direct_beam = measure(600, [0.5, 1.5])
    samples = [measure(611, [3.0, 4.0]), measure(612, [5.0, 6.0])]
    can = measure(620, [1.0, 1.0])
    finder = client.compute(FIND_CENTRE, params={'direct_beam': direct_beam})
    computed = client.accumulator(
        Template(
            CENTRED_MULTI,
            params={'centre': finder.ref('centre')},
            blanks=('sample', 'can'),
        )
    )
    typed = client.accumulator(
        Template(
            CENTRED_MULTI, params={'centre': Centre(x=1.0)}, blanks=('sample', 'can')
        )
    )
    for accumulator in (computed, typed):
        accumulator.push(sample=samples)
        accumulator.push(can=[can])

    assert client.output(finder, 'centre') == Centre(x=1.0)
    assert [
        client.output(accumulator, 'centred').values.tolist()
        for accumulator in (computed, typed)
    ] == [[6.0, 8.0], [6.0, 8.0]]
    assert client.provenance(computed).records() == [finder]
    assert client.provenance(typed).request.params['centre'] == {'x': 1.0}


def test_i3_a_correction_computed_for_one_run_and_not_another(
    client: Client, measure: Measure
) -> None:
    r611, r612 = measure(611, [1.0, 3.0]), measure(612, [2.0, 2.0])
    can = measure(620, [1.0, 1.0])
    t611 = client.compute(CONTRIBUTE, params={'sample': r611})  # transmission 1/4
    total = client.accumulator(
        Template(
            CENTRED_MULTI, params={'centre': Centre(x=0.0)}, blanks=('sample', 'can')
        )
    )
    total.push(sample=[{'file': r611, 'transmission': t611.ref('transmission')}, r612])
    total.push(can=[can])

    assert client.output(total, 'centred').values.tolist() == [1.25, 1.75]
    assert client.provenance(total).records() == [t611]
