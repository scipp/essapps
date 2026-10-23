# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Section G of docs/developer/user-stories.md: roles and deployment."""

from collections.abc import Callable

import pytest

from ess.dispatch import Client, SubmitError, local
from ess.dispatch.testing import FakeDatasets
from ess.spec import function_binding

from .conftest import BEAM_CENTRE, IOFQ, Measure, iofq


def test_g2_developer_iterates_on_a_workflow(
    measure: Measure, datasets: FakeDatasets
) -> None:
    draft = iofq
    dev = local(proposal='p1', datasets=datasets, bind=[function_binding(IOFQ, draft)])
    result = dev.compute(
        IOFQ, params={'sample': {'file': measure(1, [1.0, 2.0, 3.0, 4.0])}}
    )

    assert dev.output(result, 'iofq').values.tolist() == [3.0, 7.0]


def test_g5_reference_across_proposals_refused(
    connect: Callable[..., Client], measure: Measure
) -> None:
    theirs = connect(proposal='p1', user='anna')
    mine = connect(proposal='p2', user='eve')
    centre = theirs.compute(
        BEAM_CENTRE,
        params={'direct_beam': {'file': measure(1, [1.0, 1.0], proposal='p1')}},
    )
    sample = measure(2, [2.0, 2.0], proposal='p2')

    with pytest.raises(SubmitError, match='p1'):
        mine.compute(
            IOFQ,
            params={'sample': {'file': sample}, 'beam_centre': centre.ref('centre')},
        )
    assert mine.records() == []
