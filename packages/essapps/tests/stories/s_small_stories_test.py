# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Section S of docs/developer/user-stories.md: small stories, one mechanism each.
"""

import pytest
import scipp as sc

from ess.apps.backend import SubmitError
from ess.apps.batch import apply
from ess.apps.client import Client
from ess.apps.examples import BACKGROUND, HISTOGRAM, LOAD, NORMALIZE, SUBTRACT, SUM
from ess.apps.records import Template
from ess.apps.rules import AsOf, Like, Lookup, LookupEntry, Rule, Selector
from ess.apps.spec import OutputRef
from ess.apps.testing import equal

from .conftest import Measure


def test_s1_reduce_one_run(client: Client, measure: Measure) -> None:
    run = measure(1, counts=[1.0, 2.0, 3.0])

    loaded = client.run(LOAD, {'run': run, 'scale': 2.0})

    assert client.output(loaded, 'total') == {'value': 12.0, 'unit': 'counts'}
    assert loaded.request.datasets() == [run]
    assert loaded.request.params['scale'] == 2.0
    assert client.run(LOAD, {'run': run}).request.params['scale'] == 1.0  # default


def test_s2_tune_one_parameter(client: Client, measure: Measure) -> None:
    data = client.run(LOAD, {'run': measure(1)}).ref('data')
    plot = Template(
        spec=HISTOGRAM, params={'data': data}, blanks=('bins',), name='plot'
    )

    for bins in (2, 3, 4):
        result = client.run(plot, {'bins': bins})

    assert result.reused  # the filtering before the binning did not run again
    assert client.latest('plot') == result
    plain = client.run(HISTOGRAM, result.request.params)  # the record alone
    assert equal(client.output(plain, 'histogram'), client.output(result, 'histogram'))


def test_s3_look_at_a_value_inside_a_reduction(
    client: Client, measure: Measure
) -> None:
    normalize = Template(
        spec=NORMALIZE, params={'runs': [measure(1, counts=[1.0, 3.0])]}
    )

    inside = client.run(normalize.cut(outputs=('numerator',)))

    assert client.view(inside.ref('numerator'))['values'].tolist() == [1.0, 3.0]


def test_s4_submit_a_chain_in_one_go(client: Client, measure: Measure) -> None:
    a, b = measure(1), measure(2)

    group = client.submit_group(
        {
            'a': client.request(LOAD, {'run': a}),
            'b': client.request(LOAD, {'run': b}),
            'sum': client.request(
                SUM,
                {
                    'runs': [
                        OutputRef(record='@a', output='data'),
                        OutputRef(record='@b', output='data'),
                    ]
                },
            ),
        }
    )

    total = client.wait([group['sum']])[0]
    assert total.request.refs() == [group['a'].ref('data'), group['b'].ref('data')]
    assert client.output(total, 'total').values.tolist() == [2.0, 4.0, 6.0, 8.0]


def test_s5_sum_runs(client: Client, measure: Measure) -> None:
    runs = [measure(1, [1.0, 2.0]), measure(2, [3.0, 2.0]), measure(3, [0.0, 4.0])]

    total = client.run(NORMALIZE, {'runs': runs, 'scale': 2.0})

    summed = sc.array(dims=['x'], values=[4.0, 8.0], unit='counts')
    expected = summed / summed.sum() * 2.0
    assert sc.allclose(client.output(total, 'normalized').data, expected)
    assert total.request.datasets() == runs


def test_s6_sum_sample_and_background_runs(client: Client, measure: Measure) -> None:
    samples = [measure(1, [5.0, 5.0]), measure(2, [7.0, 5.0])]
    backgrounds = [measure(3, [1.0, 1.0]), measure(4, [1.0, 2.0])]

    result = client.run(
        BACKGROUND, {'sample_runs': samples, 'background_runs': backgrounds}
    )

    assert client.output(result, 'subtracted').values.tolist() == [10.0, 7.0]
    assert result.request.datasets() == samples + backgrounds


def test_s7_reduce_each_sample_with_the_can_measured_before_it(
    client: Client, measure: Measure
) -> None:
    measure(1, [1.0, 1.0], role='can')
    measure(2, [5.0, 5.0], role='sample')
    measure(3, [2.0, 2.0], role='can')
    measure(4, [6.0, 6.0], role='sample')
    rule = Rule(
        name='subtract',
        template=Template(
            name='subtract-defaults',
            spec=SUBTRACT,
            blanks=('sample', 'can'),
            dataset_field='sample',
        ),
        lookup=Lookup(
            name='cans',
            entries=(
                LookupEntry(
                    name='can', fills={'can': AsOf(match={'role': Like(pattern='can')})}
                ),
            ),
        ),
        selector=Selector(match={'role': Like(pattern='sample')}),
    )
    samples = [d for d in client.datasets() if rule.selector.selects(d)]

    group = apply(client, rule, samples)
    results = client.wait(client.submit_group(group).values())

    assert [client.output(r, 'result').values.tolist() for r in results] == [
        [4.0, 4.0],
        [4.0, 4.0],
    ]


def test_s8_trace_a_result_to_raw_data(client: Client, measure: Measure) -> None:
    run = measure(1)
    loaded = client.run(LOAD, {'run': run, 'scale': 2.0})
    hist = client.run(HISTOGRAM, {'data': loaded.ref('data'), 'bins': 2})

    provenance = client.provenance(hist)

    assert provenance['params']['bins'] == 2
    (source,) = provenance['inputs']
    assert source['params']['scale'] == 2.0
    assert source['raw'] == [{'dataset': 'run:dream/1'}]
