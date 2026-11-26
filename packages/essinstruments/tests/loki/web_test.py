# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The LoKI batch page on a workspace folder."""

from pathlib import Path
from typing import Any

from ess.batch import open_workspace
from ess.instruments.loki.bindings import BINDINGS
from ess.instruments.loki.web import build
from fastapi.testclient import TestClient

from ess.apps.folder import write_files_table


def register(folder: Path, *runs: tuple[int, str, str]) -> None:
    """
    List ``runs``, each a run, its role and its sample, in ``files.csv``, each
    with an empty file.
    """
    (folder / 'raw').mkdir(exist_ok=True)
    for r, _, _ in runs:
        (folder / 'raw' / f'{r}.nxs').touch()
    write_files_table(
        folder,
        [
            {'file': f'raw/{r}.nxs', 'run': r, 'kind': 'raw', 'role': role}
            | {'sample': sample}
            for r, role, sample in runs
        ],
    )


def test_the_first_batch_follows_only_if_asked(tmp_path: Path) -> None:
    register(tmp_path, (1, 'sample', 'a'), (2, 'transmission', 'a'))
    client, files, definitions = open_workspace(tmp_path, BINDINGS)
    with client:
        intake = build(client, files, definitions).state.pages.intake
        assert intake.following is None
        (label,) = intake.manager.headers()
        intake.manager.remove(label)
        intake = build(client, files, definitions, follow=True).state.pages.intake
        assert intake.following == next(iter(intake.manager.headers()))


def test_a_run_registered_while_the_page_runs_is_added_by_a_step_of_follow(
    tmp_path: Path,
) -> None:
    def names(state: dict[str, Any]) -> list[str]:
        return [row['name'] for row in state['rows']]

    first = [(1, 'sample', 'a'), (2, 'transmission', 'a')]
    register(tmp_path, *first)
    client, files, definitions = open_workspace(tmp_path, BINDINGS)
    with client:
        app = build(client, files, definitions, follow=True)
        http = TestClient(app, base_url=str(TestClient(app).get('/').url))
        before = names(http.get('/api/state').json())
        register(tmp_path, *first, (3, 'sample', 'b'), (4, 'transmission', 'b'))
        relisted = names(http.post('/api/relist').json())
        app.state.pages.tick()
        stepped = names(http.get('/api/state').json())

    assert (before, relisted, stepped) == (['a'], ['a'], ['a', 'b'])


def test_the_output_tab_offers_nxcansas_first(tmp_path: Path) -> None:
    register(tmp_path, (1, 'sample', 'a'), (2, 'transmission', 'a'))
    client, files, definitions = open_workspace(tmp_path, BINDINGS)
    with client:
        app = build(client, files, definitions)
        http = TestClient(app, base_url=str(TestClient(app).get('/').url))
        output = http.get('/api/state').json()['output']

    assert [(f['name'], f['outputs']) for f in output['formats']] == [
        ('nxcansas', ['iofq']),
        ('scipp-hdf5', None),
    ]
    assert output['options']['format'] == 'nxcansas'
