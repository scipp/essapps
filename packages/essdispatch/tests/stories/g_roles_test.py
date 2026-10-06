# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Section G of docs/developer/user-stories.md: roles and deployment."""

# ruff: noqa: F821

from collections.abc import Callable

import pytest

from ess.dispatch import Client, SubmitError, local
from ess.dispatch.testing import FakeDatasets

from .conftest import BEAM_CENTRE, IOFQ, Measure, iofq


@pytest.mark.xfail(
    reason='client.publish, scicat, and Provenance.software are not implemented'
)
def test_g2_developer_iterates_on_a_workflow(
    measure: Measure, datasets: FakeDatasets
) -> None:
    draft = iofq
    dev = local(proposal='p1', datasets=datasets, bind={IOFQ: draft})
    result = dev.compute(IOFQ, {'run': measure(1, [1.0, 2.0, 3.0, 4.0])})
    pid = dev.publish(result.ref('iofq'), 'scicat')

    assert dev.provenance(result).software['sans-iofq'] == 'bound in notebook'
    assert scicat.entries[pid].provenance == dev.provenance(result)


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
