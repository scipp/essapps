# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Section G of docs/developer/user-stories.md: roles and deployment."""

# ruff: noqa: F821

from collections.abc import Callable

import pytest

from ess.apps import Client, SubmitError

from .conftest import BEAM_CENTRE, IOFQ, VANADIUM, Measure


@pytest.mark.xfail(reason='grants across proposals are not implemented')
def test_g1_instrument_scientist_prepares_a_beamtime(
    connect: Callable[..., Client], measure: Measure
) -> None:
    scientist = connect(proposal='commissioning', user='anna')
    user = connect(proposal='p2', user='eve')
    vanadium = scientist.compute(
        VANADIUM, {'run': measure(1, [1.0, 1.0], proposal='commissioning')}
    )
    sample = measure(2, [2.0, 2.0, 2.0, 2.0], proposal='p2')
    result = user.compute(
        IOFQ, {'run': sample, 'normalization': vanadium.ref('normalization')}
    )

    assert user.output(result, 'iofq').values.tolist() == [2.0, 2.0]
    with pytest.raises(SubmitError, match='p2'):
        scientist.compute(IOFQ, {'run': sample})
    assert scientist.records() == [vanadium]


@pytest.mark.xfail(
    reason='local(bind=...), publish, and provenance.software are not implemented'
)
def test_g2_developer_iterates_on_a_workflow(measure: Measure) -> None:
    draft = make_iofq_workflow()
    dev = local(proposal='p1', bind={IOFQ: draft})
    result = dev.compute(IOFQ, {'run': measure(1, [1.0, 2.0, 3.0, 4.0])})
    pid = dev.publish(result.ref('iofq'), 'scicat')

    assert dev.provenance(result).software['sans-iofq'] == 'bound in notebook'
    assert scicat.entries[pid].provenance == dev.provenance(result)


def test_g4_two_notebooks_on_one_machine(
    connect: Callable[..., Client], measure: Measure
) -> None:
    first, second = connect(), connect()
    centre = first.compute(
        BEAM_CENTRE, {'run': measure(1, [1.0, 1.0, 1.0, 1.0])}, label='beam-centre'
    )
    found = second.latest('beam-centre')
    result = second.compute(
        IOFQ,
        {'run': measure(2, [2.0, 3.0, 4.0, 5.0]), 'beam_centre': found.ref('centre')},
    )

    assert found == centre
    assert second.output(result, 'iofq').values.tolist() == [3.0, 7.0]


def test_g5_reference_across_proposals_refused(
    connect: Callable[..., Client], measure: Measure
) -> None:
    theirs = connect(proposal='p1', user='anna')
    mine = connect(proposal='p2', user='eve')
    centre = theirs.compute(BEAM_CENTRE, {'run': measure(1, [1.0, 1.0], proposal='p1')})
    sample = measure(2, [2.0, 2.0], proposal='p2')

    with pytest.raises(SubmitError, match='p1'):
        mine.compute(IOFQ, {'run': sample, 'beam_centre': centre.ref('centre')})
    assert mine.records() == []
