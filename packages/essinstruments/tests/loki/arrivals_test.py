# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The runs that arrive in the LoKI demo, and its page on real workflows."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from ess.instruments.loki.arrivals import (
    EXPERIMENT,
    ArrivingFiles,
    Instrument,
    arrivals,
    settings,
)
from ess.instruments.loki.bindings import BINDINGS
from ess.instruments.loki.web import build
from fastapi.testclient import TestClient

from ess.apps import Client, Selector, file
from ess.apps.batch import CorrectedFiles, FolderDefinitions
from ess.apps.folder import open_folder


def test_the_experiment_comes_again_with_its_samples_numbered() -> None:
    plan = arrivals(len(EXPERIMENT) + 2)

    assert plan[: len(EXPERIMENT)] == list(EXPERIMENT)
    assert plan[len(EXPERIMENT) :] == [
        ('AgBeh #2', 'sample', 60387),
        ('AgBeh #2', 'transmission', 60386),
    ]


def test_arriving_files_reads_the_table_again_when_it_is_replaced(
    tmp_path: Path,
) -> None:
    def register(*runs: int) -> None:
        lines = ['file,run', *(f'{run}.nxs,{run}' for run in runs)]
        (tmp_path / 'table.partial').write_text('\n'.join(lines))
        (tmp_path / 'table.partial').replace(tmp_path / 'files.csv')

    for run in (1, 2):
        (tmp_path / f'{run}.nxs').touch()
    files = ArrivingFiles(tmp_path)
    register(1)
    assert [files.metadata(r)['run'] for r in files.list(Selector())] == ['1']

    register(1, 2)

    assert [files.metadata(r)['run'] for r in files.list(Selector())] == ['1', '2']
    assert files.resolve(file(run=2)) == files.list(Selector())[1]


@pytest.fixture
def page(tmp_path: Path) -> Iterator[tuple[TestClient, Instrument, Client]]:
    instrument = Instrument(tmp_path)
    definitions = FolderDefinitions(tmp_path)
    files = CorrectedFiles(ArrivingFiles(tmp_path), definitions=definitions)
    with open_folder(tmp_path, BINDINGS, files=files) as client:
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
