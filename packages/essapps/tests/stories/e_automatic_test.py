# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Section E of docs/developer/user-stories.md: automatic reduction."""

# ruff: noqa: F821

from collections.abc import Callable

import pytest

from ess.apps import Client, Record, Rule, Selector, Template, TriggerLoop
from ess.apps.testing import FakeFiles
from ess.spec import FileRef

from . import toys

NO_PUBLISH = 'publication is not implemented'


def iofq_rule(template: Template, selector: Selector) -> Rule:
    def submit(client: Client, run: FileRef) -> Record:
        member = str(client.files.metadata(run)['run'])
        request = template.fill(params={'sample': run})
        return client.submit(request, label='iofq', member=member, persist=True)

    return Rule('auto-iofq', submit, selector=selector, label='iofq', at='sample')


def test_e1_series_grows_reduction_follows(
    client: Client, connect: Callable[..., Client], measure: toys.Measure
) -> None:
    reference = measure(1, [1.0, 1.0], role='reference')
    template = Template(toys.STITCH, params={'reference': reference}, blanks=('runs',))

    def stitch(client: Client, run: FileRef) -> Record:
        """Stitch all angles of the run's sample so far, in run order."""
        sample = client.files.metadata(run)['sample']
        angles = client.files.list(Selector(role='sample', sample=sample))
        angles.sort(key=lambda ref: client.files.metadata(ref)['run'])
        return client.submit(
            template.fill(params={'runs': angles}),
            label='reflectivity',
            member=sample,
            persist=True,
        )

    rule = Rule(
        'reflectivity',
        stitch,
        selector=Selector(role='sample'),
        label='reflectivity',
        at='runs',
    )
    loop = TriggerLoop(client, rules=[rule])

    r2 = measure(2, [1.0, 2.0], role='sample', sample='si')
    loop.step()
    r4 = measure(4, [4.0, 8.0], role='sample', sample='si')
    r3 = measure(3, [1.0, 1.0], role='sample', sample='si')
    measure(2, [1.0, 2.0], role='sample', sample='si')
    loop.step()

    user = connect()  # any client of the proposal
    latest = user.latest('reflectivity', member='si')
    assert len(user.records(label='reflectivity')) == 2
    # run order
    assert latest.request.params['runs'] == [r2, r3, r4]
    assert user.output(latest, 'stitched').curve.values.tolist() == [
        1.0,
        2.0,
        2.0,
        2.0,
        2.0,
        4.0,
    ]


def test_e2_automatic_reduction_goes_quiet(
    upgrade: Callable[..., Client], measure: toys.Measure
) -> None:
    rule = iofq_rule(
        Template(toys.IOFQ, params={'normalization': None}, blanks=('sample',)),
        Selector(role='sample'),
    )
    upgraded = upgrade(specs=[toys.IOFQ_V2])  # a workspace without version 1
    loop = TriggerLoop(upgraded, rules=[rule])
    run = measure(1, [1.0, 1.0], role='sample')

    assert loop.step() == []
    assert loop.status(rule).reason == f'skipped {run.file}: unknown spec sans-iofq/v1'


@pytest.mark.xfail(reason=NO_PUBLISH)
def test_e3_reduction_of_our_own_output(
    client: Client, measure: toys.Measure, files: FakeFiles
) -> None:
    rule = iofq_rule(
        Template(toys.IOFQ, params={'normalization': None}, blanks=('sample',)),
        Selector(),
    )
    loop = TriggerLoop(client, rules=[rule])
    measure(1, [1.0, 2.0, 3.0, 4.0])
    (reduced,) = loop.step()
    pid = client.publish(reduced.ref('iofq'), 'scicat')
    files.add_published(scicat.entries[pid])

    assert loop.step() == []


def test_e4_template_improved_during_a_beamtime(
    client: Client, connect: Callable[..., Client], measure: toys.Measure
) -> None:
    samples = Selector(role='sample')
    template = Template(toys.IOFQ, params={'normalization': None}, blanks=('sample',))
    first = measure(1, [1.0, 2.0, 3.0, 4.0], role='sample')
    (before,) = TriggerLoop(client, rules=[iofq_rule(template, samples)]).step()

    improved = Template(
        toys.IOFQ,
        params={'reduction': toys.Reduction(threshold=1.5), 'normalization': None},
        blanks=('sample',),
    )
    second = measure(2, [1.0, 2.0, 3.0, 4.0], role='sample')
    (after,) = TriggerLoop(client, rules=[iofq_rule(improved, samples)]).step()

    user = connect()  # any client of the proposal
    assert after.request.files() == [second]
    assert [user.output(r, 'iofq').values.tolist() for r in (before, after)] == [
        [3.0, 7.0],
        [2.0, 7.0],
    ]
    stale = [
        r
        for r in user.records(label='iofq')
        if r.request.params['reduction']['threshold'] != 1.5
    ]
    assert [r.request.files() for r in stale] == [[first]]
