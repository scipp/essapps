# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The LoKI batch page on a workspace folder."""

from pathlib import Path
from typing import Any

from ess.instruments.loki.web import build, open_loki
from fastapi.testclient import TestClient

from ess.apps.folder import write_files_table


def test_a_run_registered_while_the_page_runs_is_added_by_a_step_of_follow(
    tmp_path: Path,
) -> None:
    def register(*runs: tuple[int, str, str]) -> None:
        write_files_table(
            tmp_path,
            [
                {'file': f'raw/{r}.nxs', 'run': r, 'kind': 'raw', 'role': role}
                | {'sample': sample}
                for r, role, sample in runs
            ],
        )

    def names(state: dict[str, Any]) -> list[str]:
        return [row['name'] for row in state['rows']]

    (tmp_path / 'raw').mkdir()
    for run in (1, 2, 3, 4):
        (tmp_path / 'raw' / f'{run}.nxs').touch()
    first = [(1, 'sample', 'a'), (2, 'transmission', 'a')]
    register(*first)
    client, files, definitions = open_loki(tmp_path)
    with client:
        app = build(client, files, definitions)
        http = TestClient(app, base_url=str(TestClient(app).get('/').url))
        before = names(http.get('/api/state').json())
        register(*first, (3, 'sample', 'b'), (4, 'transmission', 'b'))
        relisted = names(http.post('/api/relist').json())
        pages = app.state.pages
        pages.manager.follow(pages.home())
        pages.tick()
        stepped = names(http.get('/api/state').json())

    assert (before, relisted, stepped) == (['a'], ['a'], ['a', 'b'])
