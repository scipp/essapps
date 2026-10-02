# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Section S of docs/developer/user-stories.md: small stories, one concept each."""

import scipp as sc

from ess.apps import (
    Client,
    LastBefore,
    Lookup,
    Request,
    Selector,
    Template,
    apply,
)

from .conftest import (
    BACKGROUND,
    CONTRIBUTE,
    IOFQ,
    NORMALIZE,
    PARTS_SUM,
    VANADIUM,
    Measure,
)


def test_s1_reduce_one_run(client: Client, measure: Measure) -> None:
    run = measure(1, counts=[1.0, 2.0, 3.0, 4.0])
    result = client.compute(IOFQ, {'run': run, 'threshold': 1.5})

    assert client.output(result, 'iofq').values.tolist() == [2.0, 7.0]
    assert result.request.params['bins'] == 2
    assert result.request.datasets() == [run]


def test_s2_tune_one_parameter(client: Client, measure: Measure) -> None:
    run = measure(1, counts=[1.0, 2.0, 3.0, 4.0])
    tune = client.stage(Template(IOFQ, params={'run': run}, blanks=('bins',)))
    for bins in (1, 2, 4):
        result = client.compute(tune, {'bins': bins}, label='tuning')

    assert client.latest('tuning') == result
    assert client.output(result, 'iofq').values.tolist() == [1.0, 2.0, 3.0, 4.0]
    plain = client.compute(IOFQ, result.request.params)
    assert sc.identical(client.output(plain, 'iofq'), client.output(result, 'iofq'))


def test_s3_look_at_a_value_inside_a_reduction(
    client: Client, measure: Measure
) -> None:
    run = measure(1, [1.0, 2.0, 3.0, 4.0])
    result = client.compute(IOFQ, {'run': run, 'threshold': 2.5})

    assert client.output(result, 'masked').values.tolist() == [0.0, 0.0, 3.0, 4.0]
    assert client.output(result, 'iofq').values.tolist() == [0.0, 7.0]


def test_s4_submit_a_chain_without_waiting(client: Client, measure: Measure) -> None:
    r611, r612 = measure(611, [1.0, 3.0]), measure(612, [2.0, 6.0])
    parts = client.submit([Request(CONTRIBUTE, {'run': run}) for run in (r611, r612)])
    summed = client.submit(
        PARTS_SUM, {'parts': [p.refs('numerator', 'denominator') for p in parts]}
    )

    assert client.output(summed, 'numerator').values.tolist() == [3.0, 9.0]


def test_s5_sum_runs(client: Client, measure: Measure) -> None:
    runs = [measure(1, [1.0, 2.0]), measure(2, [1.0, 2.0]), measure(3, [0.0, 2.0])]
    total = client.compute(NORMALIZE, {'runs': runs, 'scale': 2.0})

    assert client.output(total, 'normalized').values.tolist() == [0.5, 1.5]
    assert total.request.datasets() == runs


def test_s6_sum_sample_runs_and_background_runs(
    client: Client, measure: Measure
) -> None:
    samples = [measure(1, [5.0, 5.0]), measure(2, [7.0, 5.0])]
    backgrounds = [measure(3, [1.0, 1.0]), measure(4, [1.0, 2.0])]
    result = client.compute(
        BACKGROUND, {'sample_runs': samples, 'background_runs': backgrounds}
    )

    assert client.output(result, 'subtracted').values.tolist() == [10.0, 7.0]
    assert result.request.datasets() == samples + backgrounds


def test_s7_reduce_each_sample_with_the_can_measured_before_it(
    client: Client, measure: Measure
) -> None:
    can_1 = measure(1, [1.0, 1.0], role='can')
    first = measure(2, [5.0, 6.0], role='sample')
    can_3 = measure(3, [2.0, 2.0], role='can')
    second = measure(4, [6.0, 9.0], role='sample')
    template = Template(IOFQ, blanks=('run', 'can'))
    cans = Lookup(can=LastBefore(Selector(role='can')))

    requests = apply(
        template, [first, second], client.datasets, member_field='run', lookup=cans
    )
    reduced = list(client.compute(requests, label='iofq').values())

    assert [client.output(r, 'iofq').values.tolist() for r in reduced] == [
        [4.0, 5.0],
        [4.0, 7.0],
    ]
    assert [r.request.datasets() for r in reduced] == [
        [first, can_1],
        [second, can_3],
    ]


def test_s8_trace_a_result_to_raw_data(client: Client, measure: Measure) -> None:
    vanadium_run, sample = measure(1, [1.0, 1.0]), measure(2, [4.0, 8.0])
    vanadium = client.compute(VANADIUM, {'run': vanadium_run, 'scale': 2.0})
    result = client.compute(
        IOFQ, {'run': sample, 'normalization': vanadium.ref('normalization')}
    )

    provenance = client.provenance(result)
    assert set(provenance.datasets()) == {sample, vanadium_run}
    (upstream,) = provenance.records()
    assert upstream.request.params['scale'] == 2.0
    assert client.output(result, 'iofq').values.tolist() == [1.0, 2.0]
