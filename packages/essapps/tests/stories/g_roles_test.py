# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Section G of docs/developer/user-stories.md: roles and deployment.
"""

from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from ess.apps.backend import Backend, SubmitError
from ess.apps.binding import Registry
from ess.apps.client import Client, local, local_backend
from ess.apps.examples import HISTOGRAM, LOAD, REBIN, histogram_workflow, registry
from ess.apps.records import Status, Template
from ess.apps.store import StoreLockedError
from ess.apps.testing import FakeDatasetSource, FakePublisher

from .conftest import Measure


@pytest.fixture
def backend(tmp_path: Path, catalogue: FakeDatasetSource) -> Iterator[Backend]:
    """The instrument's backend, which the clients of every proposal share."""
    backend = local_backend(
        tmp_path / 'dream', registry=registry(), sources=[catalogue]
    )
    yield backend
    backend.close()


@pytest.mark.xfail(
    raises=AttributeError,
    strict=True,
    reason='nothing marks an artefact instrument-shared',
)
def test_g1_instrument_scientist_prepares_a_beamtime(
    backend: Backend, measure: Measure
) -> None:
    scientist = Client(
        backend, instrument='dream', proposal='commissioning', submitter='anna'
    )
    user = Client(backend, instrument='dream', proposal='p2', submitter='eve')
    vanadium = scientist.run(LOAD, {'run': measure(1)})

    scientist.share(vanadium)
    normalized = user.run(REBIN, {'data': vanadium.ref('data')})

    assert normalized.status == Status.COMPLETED


def test_g2_developer_iterates_on_a_workflow(
    tmp_path: Path,
    catalogue: FakeDatasetSource,
    measure: Measure,
    scicat: FakePublisher,
) -> None:
    workflows = Registry()
    workflows.bind(HISTOGRAM, histogram_workflow)
    notebook = local(
        tmp_path / 'notebook',
        instrument='dream',
        proposal='p1',
        submitter='dev',
        registry=workflows,
        sources=[catalogue],
        publishers={'scicat': scicat},
    )

    result = notebook.run(HISTOGRAM, {'data': measure(1), 'bins': 2})

    assert notebook.output(result, 'histogram').values.tolist() == [3.0, 7.0]
    assert notebook.provenance(result)['binding'] == 'in_process'
    with pytest.raises(ValueError, match='in-process'):
        notebook.publish(result.ref('histogram'), 'scicat')
    notebook.close()


@pytest.mark.xfail(
    raises=ImportError,
    strict=True,
    reason='question: whether interactive work uses sessions, and where a session runs',
)
def test_g3_local_application_remote_compute(service: Client, measure: Measure) -> None:
    from ess.apps.session import Session

    (loaded,) = service.wait([service.run(LOAD, {'run': measure(1)})])
    laptop = Session(service)
    plot = Template(
        spec=HISTOGRAM, params={'data': loaded.ref('data')}, blanks=('bins',)
    )

    for bins in (2, 3, 4):
        tuned = laptop.run(plot, {'bins': bins})

    assert tuned.reused
    assert service.record(tuned.id) == tuned


@pytest.mark.xfail(
    raises=StoreLockedError,
    strict=True,
    reason='question: each notebook is its own backend over its own store, so one '
    'cannot reference what the other made',
)
def test_g4_two_notebooks_on_one_machine(
    tmp_path: Path, catalogue: FakeDatasetSource, measure: Measure
) -> None:
    def notebook() -> Client:
        return local(
            tmp_path / 'notebook',
            instrument='dream',
            proposal='p1',
            submitter='simon',
            registry=registry(),
            sources=[catalogue],
        )

    with closing(notebook()) as first, closing(notebook()) as second:
        loaded = first.run(LOAD, {'run': measure(1)})

        rebinned = second.run(REBIN, {'data': loaded.ref('data')})

    assert rebinned.status == Status.COMPLETED


def test_g5_reference_across_proposals_refused(
    backend: Backend, measure: Measure
) -> None:
    theirs = Client(backend, instrument='dream', proposal='p1', submitter='simon')
    mine = Client(backend, instrument='dream', proposal='p2', submitter='eve')
    loaded = theirs.run(LOAD, {'run': measure(1)})

    with pytest.raises(SubmitError, match='belongs to proposal p1'):
        mine.run(REBIN, {'data': loaded.ref('data')})

    assert mine.records() == []
