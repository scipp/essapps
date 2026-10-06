# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Section F of docs/developer/user-stories.md: publication and provenance."""

# ruff: noqa: F821

from collections.abc import Callable

import pytest

from ess.dispatch import Client, SubmitError, Template

from .conftest import BEAM_CENTRE, IOFQ, Measure


@pytest.mark.xfail(
    reason='client.publish, scicat, and Provenance.software are not implemented'
)
def test_f1_publish_then_trace_six_months_later(
    client: Client, measure: Measure
) -> None:
    centre_run, run = measure(1, [1.0, 1.0, 1.0, 1.0]), measure(2, [2.0, 3.0, 4.0, 5.0])
    centre = client.compute(BEAM_CENTRE, {'run': centre_run})
    tune = client.stage(
        Template(
            IOFQ,
            params={'run': run, 'beam_centre': centre.ref('centre')},
            blanks=('threshold',),
        )
    )
    result = client.compute(tune, {'threshold': 1.5})
    pid = client.publish(result.ref('iofq'), 'scicat')
    plain = client.compute(IOFQ, result.request.params)

    provenance = scicat.entries[pid].provenance
    assert provenance == client.provenance(result) == client.provenance(plain)
    assert set(provenance.datasets()) == {centre_run, run}
    assert [r.request.params for r in provenance.records()] == [centre.request.params]
    assert {'essdispatch', 'scipp'} <= provenance.software.keys()


@pytest.mark.xfail(
    reason='records name no software versions, so upgrade takes no versions=; '
    'client.recompute and Provenance.software are not implemented'
)
def test_f2_reproduce_after_two_upgrades(
    client: Client, measure: Measure, upgrade: Callable[..., Client]
) -> None:
    result = client.compute(
        IOFQ, {'run': measure(1, [1.0, 2.0, 3.0, 4.0]), 'threshold': 1.5}
    )
    upgrade(versions={'scipp': '99.0'})

    with pytest.raises(SubmitError, match='scipp'):
        client.recompute(result)
    again = client.compute(result.request.spec, result.request.params)

    assert again.request == result.request
    assert client.provenance(again).software['scipp'] == '99.0'
    assert client.provenance(result).software['scipp'] != '99.0'
