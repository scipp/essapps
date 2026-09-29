# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Section E of docs/developer/user-stories.md: automatic reduction."""

# ruff: noqa: F821

from collections.abc import Callable
from dataclasses import replace

import pytest

from ess.apps import Client, Rule, Selector, Template, TriggerLoop
from ess.apps.testing import FakeDatasets

from .conftest import IOFQ, IOFQ_V2, STITCH, Measure

NO_PUBLISH = 'publication is not implemented'


def test_e1_series_grows_reduction_follows(
    client: Client, measure: Measure, datasets: FakeDatasets
) -> None:
    reference = measure(1, [1.0, 1.0], role='reference')
    template = Template(STITCH, params={'reference': reference}, blanks=('runs',))
    rule = Rule(
        'reflectivity',
        template,
        selector=Selector(role='sample'),
        series='sample',
        label='reflectivity',
    )
    loop = TriggerLoop(client, datasets, rules=[rule])

    r2 = measure(2, [1.0, 2.0], role='sample', sample='si')
    loop.step()
    r4 = measure(4, [4.0, 8.0], role='sample', sample='si')
    r3 = measure(3, [1.0, 1.0], role='sample', sample='si')
    measure(2, [1.0, 2.0], role='sample', sample='si')
    loop.step()

    curve = client.latest('reflectivity', member='si')
    assert len(client.records(label='reflectivity')) == 2
    assert curve.request.params['runs'] == [r2, r3, r4]
    assert client.output(curve, 'stitched').values.tolist() == [
        1.0,
        2.0,
        2.0,
        2.0,
        2.0,
        4.0,
    ]


def test_e2_automatic_reduction_goes_quiet(
    upgrade: Callable[..., Client], measure: Measure, datasets: FakeDatasets
) -> None:
    rule = Rule(
        'auto-iofq',
        Template(IOFQ, blanks=('run',)),
        selector=Selector(role='sample'),
        label='iofq',
    )
    upgraded = upgrade(specs=[IOFQ_V2])  # a backend without version 1
    loop = TriggerLoop(upgraded, datasets, rules=[rule])
    measure(1, [1.0, 1.0], role='sample')

    assert loop.step() == []
    assert loop.status(rule).reason == '1: unknown spec sans-iofq/v1'


@pytest.mark.xfail(reason=NO_PUBLISH)
def test_e3_reduction_of_our_own_output(
    client: Client, measure: Measure, datasets: FakeDatasets
) -> None:
    rule = Rule(
        'auto-iofq',
        Template(IOFQ, blanks=('run',)),
        selector=Selector(),
        label='iofq',
    )
    loop = TriggerLoop(client, datasets, rules=[rule])
    measure(1, [1.0, 2.0, 3.0, 4.0])
    (reduced,) = loop.step()
    pid = client.publish(reduced.ref('iofq'), 'scicat')
    datasets.add_published(scicat.entries[pid])

    assert loop.step() == []


def test_e4_template_improved_during_a_beamtime(
    client: Client, measure: Measure, datasets: FakeDatasets
) -> None:
    rule = Rule(
        'auto-iofq',
        Template(IOFQ, blanks=('run',)),
        selector=Selector(role='sample'),
        label='iofq',
    )
    first = measure(1, [1.0, 2.0, 3.0, 4.0], role='sample')
    (before,) = TriggerLoop(client, datasets, rules=[rule]).step()

    improved = replace(rule, template=replace(rule.template, params={'threshold': 1.5}))
    second = measure(2, [1.0, 2.0, 3.0, 4.0], role='sample')
    (after,) = TriggerLoop(client, datasets, rules=[improved]).step()

    assert after.request.datasets() == [second]
    assert [client.output(r, 'iofq').values.tolist() for r in (before, after)] == [
        [3.0, 7.0],
        [2.0, 7.0],
    ]
    stale = [
        r for r in client.records(label='iofq') if r.request.params['threshold'] != 1.5
    ]
    assert [r.request.datasets() for r in stale] == [[first]]
