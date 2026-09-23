# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Section H of docs/developer/user-stories.md: operations.
"""

from pathlib import Path

import pytest

from ess.apps.client import Client, local
from ess.apps.datastore import MissingCopyError
from ess.apps.examples import LOAD, REBIN
from ess.apps.records import Status
from ess.apps.testing import FakeDatasetSource, FakePublisher

from .conftest import Measure


def test_h1_disk_fills_up(service: Client, measure: Measure) -> None:
    (loaded,) = service.wait([service.run(LOAD, {'run': measure(1)})])
    provenance = service.provenance(loaded)

    service.drop(loaded.ref('data'))

    assert service.record(loaded.id) == loaded
    assert service.provenance(loaded) == provenance
    with pytest.raises(MissingCopyError):
        service.output(loaded, 'data')


# The old backend never reaps its runners; a real upgrade ends its process.
@pytest.mark.filterwarnings('ignore:subprocess .* is still running:ResourceWarning')
def test_h2_backend_upgrade_with_runs_in_flight(
    tmp_path: Path, catalogue: FakeDatasetSource, measure: Measure
) -> None:
    """A restart over the same store stands in for the upgrade; no schema changes."""

    def deploy() -> Client:
        return local(
            tmp_path / 'service',
            instrument='dream',
            proposal='p1',
            submitter='operator',
            registry='ess.apps.examples:registry',
            throwaway=True,
            sources=[catalogue],
        )

    old = deploy()
    running = old.run(LOAD, {'run': measure(1)})
    queued = old.run(REBIN, {'data': running.ref('data')})
    old.close()

    new = deploy()
    done = new.wait([running, queued])

    assert [(r.status, r.failure) for r in done] == 2 * [(Status.COMPLETED, None)]
    assert [r.id for r in new.records()] == [running.id, queued.id]
    new.close()


@pytest.mark.xfail(
    raises=AttributeError,
    strict=True,
    reason="nothing exports and drops a proposal's records and disk copies together",
)
def test_h3_proposal_ends(
    client: Client, measure: Measure, scicat: FakePublisher
) -> None:
    loaded = client.run(LOAD, {'run': measure(1), 'scale': 2.0})
    # allow_reused: the toy workflows are bound in process
    pid = client.publish(loaded.ref('data'), 'scicat', allow_reused=True)

    client.drop_proposal()

    assert client.records() == []
    assert scicat.entries[pid]['snapshot']['params']['scale'] == 2.0
