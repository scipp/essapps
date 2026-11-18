# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The LoKI batch page on a workspace folder."""

from pathlib import Path

from ess.instruments.loki.batch import SOURCES
from ess.instruments.loki.specs import CALIBRATED_DETECTOR, IOFQ
from ess.instruments.loki.web import build, open_loki
from fastapi.testclient import TestClient

from ess.apps.batch import BatchManager
from ess.apps.folder import write_files_table


def test_a_run_registered_while_the_page_runs_shows_after_a_rescan(
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

    (tmp_path / 'raw').mkdir()
    for run in (1, 2, 3, 4):
        (tmp_path / 'raw' / f'{run}.nxs').touch()
    first = [(1, 'sample', 'a'), (2, 'transmission', 'a')]
    register(*first)
    client, files, definitions = open_loki(tmp_path)
    with client:
        app = build(client, files, definitions)
        http = TestClient(app, base_url=str(TestClient(app).get('/').url))
        before = [row['name'] for row in http.get('/api/state').json()['rows']]
        register(*first, (3, 'sample', 'b'), (4, 'transmission', 'b'))
        after = [row['name'] for row in http.post('/api/scan').json()['rows']]

    assert (before, after) == (['a'], ['a', 'b'])


def test_a_folder_with_batches_of_other_specs_only_gets_a_batch_of_iofq(
    tmp_path: Path,
) -> None:
    write_files_table(tmp_path, [])
    client, files, definitions = open_loki(tmp_path)
    with client:
        before = BatchManager(client, sources=SOURCES, definitions=definitions)
        before.create(CALIBRATED_DETECTOR.name, title='detector', label='detector')
        build(client, files, definitions)
        after = BatchManager(client, sources=SOURCES, definitions=definitions)
        specs = sorted(header.spec for header in after.headers().values())

    assert specs == sorted([CALIBRATED_DETECTOR.name, IOFQ.name])
