# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A value typed in by one user and computed by another: the request gives each
value, or each row, inline or by reference.
"""

import pytest

from ess.apps import Client, NotReadable, Template

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
    slicer = client.compute(SLICER, params={'sample': run})
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

    slices.push(time_range=[TimeRange(start=0, stop=2)])  # slice 0, typed in
    with pytest.raises(NotReadable, match=r'push 1 failed: time_range\[3\]: repeats'):
        client.output(slices, 'sliced')


def test_i2_beam_centre_typed_in_or_computed(client: Client, measure: Measure) -> None:
    direct_beam = measure(600, [0.5, 1.5])
    sample = measure(611, [12.0, 36.0])
    can = measure(620, [1.0, 1.0])
    finder = client.compute(FIND_CENTRE, params={'direct_beam': direct_beam})
    t611 = client.compute(CONTRIBUTE, params={'sample': sample})
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
        accumulator.push(
            sample=[{'file': sample, 'transmission': t611.ref('transmission')}]
        )
        accumulator.push(can=[can])

    assert client.output(finder, 'centre') == Centre(x=1.0)
    assert [
        client.output(accumulator, 'centred').values.tolist()
        for accumulator in (computed, typed)
    ] == [[1.0, 7.0], [1.0, 7.0]]
    assert client.provenance(computed).records() == [finder, t611]
    assert client.provenance(typed).request.params['centre'] == {'x': 1.0}


def test_i3_a_transmission_computed_for_each_run(
    client: Client, measure: Measure
) -> None:
    runs = [measure(611, [1.0, 3.0]), measure(612, [4.0, 4.0])]
    can = measure(620, [1.0, 1.0])
    # transmissions 1/4 and 1/2
    t611, t612 = (client.compute(CONTRIBUTE, params={'sample': run}) for run in runs)
    total = client.accumulator(
        Template(
            CENTRED_MULTI, params={'centre': Centre(x=0.0)}, blanks=('sample', 'can')
        )
    )
    total.push(
        sample=[
            {'file': run, 'transmission': t.ref('transmission')}
            for run, t in zip(runs, (t611, t612), strict=True)
        ]
    )
    total.push(can=[can])

    assert client.output(total, 'centred').values.tolist() == [1.25, 1.75]
    assert client.provenance(total).records() == [t611, t612]
