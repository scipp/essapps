# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The runs that arrive in the LoKI demo, and its page on real workflows."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from ess.instruments.loki.arrivals import (
    EXPERIMENT,
    Instrument,
    arrivals,
    settings,
)
from ess.instruments.loki.web import build, open_loki
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
    instrument = Instrument(tmp_path)
    client, files, definitions = open_loki(tmp_path)
    with client:
        given = settings(client, instrument)
        app = build(client, files, definitions, settings=given)
        yield TestClient(app), instrument, client


def rows(state: dict[str, Any]) -> dict[str, str]:
    return {row['name']: row['status'] for row in state['rows']}


def test_a_sample_whose_runs_arrived_is_reduced_after_a_rescan(
    page: tuple[TestClient, Instrument, Client],
) -> None:
    http, instrument, client = page
    for sample, role, source in arrivals(3):
        instrument.measure(role, sample, source)

    scanned = http.post('/api/scan').json()
    reduced = http.post('/api/reduce', json={'which': 'remaining'}).json()
    client.wait(client.records())

    assert rows(scanned) == {'AgBeh': 'ready', 'porous silica': 'incomplete'}
    assert reduced['notice'] == 'Reducing 1 sample run.'
    assert rows(http.get('/api/state').json())['AgBeh'] == 'done'
