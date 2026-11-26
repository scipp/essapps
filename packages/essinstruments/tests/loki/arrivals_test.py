# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The runs that arrive in the LoKI demo, and its page on real workflows."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from ess.batch import (
    Header,
    WorkspaceHeader,
    create_workspace,
    file_sources,
    open_workspace,
)
from ess.batch.web import create_app
from ess.instruments.loki.arrivals import (
    EXPERIMENT,
    Instrument,
    arrivals,
    settings,
)
from ess.instruments.loki.bindings import BINDINGS
from ess.instruments.loki.web import build
from fastapi.testclient import TestClient

from ess.apps import Client


def test_the_experiment_comes_again_with_its_samples_numbered() -> None:
    plan = arrivals(len(EXPERIMENT) + 2)

    assert plan[: len(EXPERIMENT)] == list(EXPERIMENT)
    assert plan[len(EXPERIMENT) :] == [
        ('AgBeh #2', 'sample', 60387),
        ('AgBeh #2', 'transmission', 60386),
    ]


@pytest.fixture
def page(tmp_path: Path) -> Iterator[tuple[TestClient, Instrument, Client]]:
    header = WorkspaceHeader(Header('Arrivals'))
    folder = create_workspace(tmp_path, 'arrivals', header)
    instrument = Instrument(folder)
    client, files, definitions = open_workspace(
        folder, header, BINDINGS, allowed=file_sources(['local'])
    )
    with client:
        given = settings(client, instrument)
        app = create_app(build(client, files, definitions, settings=given, follow=True))
        page = TestClient(app).get('/').url
        yield TestClient(app, base_url=str(page)), instrument, client


def rows(state: dict[str, Any]) -> dict[str, str]:
    return {row['name']: row['status'] for row in state['rows']}


@pytest.mark.real_data
def test_a_sample_whose_runs_arrived_is_added_and_reduced_by_a_step_of_follow(
    page: tuple[TestClient, Instrument, Client],
) -> None:
    http, instrument, client = page
    plan = [(role, sample, source) for sample, role, source in arrivals(4)]
    pages = http.app.state.pages  # type: ignore[attr-defined]
    for arrival in plan[:3]:
        instrument.measure(*arrival)

    relisted = http.post('/api/relist').json()
    pages.tick()
    client.wait(client.records())
    first = http.get('/api/state').json()
    instrument.measure(*plan[3])
    pages.tick()
    client.wait(client.records())

    assert rows(relisted) == {}
    assert rows(first) == {'AgBeh': 'done'}
    assert rows(http.get('/api/state').json()) == {
        'AgBeh': 'done',
        'porous silica': 'done',
    }
