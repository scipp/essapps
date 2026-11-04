# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Section F of docs/developer/user-stories.md: publication and provenance."""

# ruff: noqa: F821

from collections.abc import Callable

import pytest

from ess.apps import Client, SubmitError, Template

from . import toys


@pytest.mark.xfail(
    reason='client.publish, scicat, and Provenance.software are not implemented'
)
def test_f1_publish_then_trace_six_months_later(
    client: Client, measure: toys.Measure
) -> None:
    centre_run, run = measure(1, [1.0, 1.0, 1.0, 1.0]), measure(2, [2.0, 3.0, 4.0, 5.0])
    centre = client.compute(toys.BEAM_CENTRE, params={'direct_beam': centre_run})
    tune = client.stage(
        Template(
            toys.IOFQ,
            params={
                'sample': run,
                'beam_centre': centre.ref('centre'),
                'normalization': None,
            },
            blanks=('reduction',),
        )
    )
    result = client.compute(tune, params={'reduction': toys.Reduction(threshold=1.5)})
    pid = client.publish(result.ref('iofq'), 'scicat')
    plain = client.compute(toys.IOFQ, params=result.request.params)

    provenance = scicat.entries[pid].provenance
    assert provenance == client.provenance(result) == client.provenance(plain)
    assert set(provenance.files()) == {centre_run, run}
    assert [r.request.params for r in provenance.records()] == [centre.request.params]
    assert {'essapps', 'scipp'} <= provenance.software.keys()


@pytest.mark.xfail(
    reason='records name no software versions, so upgrade takes no versions=; '
    'client.recompute and Provenance.software are not implemented'
)
def test_f2_reproduce_after_two_upgrades(
    client: Client, measure: toys.Measure, upgrade: Callable[..., Client]
) -> None:
    result = client.compute(
        toys.IOFQ,
        params={
            'reduction': toys.Reduction(threshold=1.5),
            'sample': measure(1, [1.0, 2.0, 3.0, 4.0]),
            'normalization': None,
        },
    )
    upgraded = upgrade(versions={'scipp': '99.0'})

    with pytest.raises(SubmitError, match='scipp'):
        upgraded.recompute(result)
    again = upgraded.compute(result.request.spec, params=result.request.params)

    assert again.request == result.request
    assert upgraded.provenance(again).software['scipp'] == '99.0'
    assert upgraded.provenance(result).software['scipp'] != '99.0'
