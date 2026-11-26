# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The LoKI batch page on a workspace folder."""

from pathlib import Path
from typing import Any

import pytest
from ess.batch import (
    CorrectedFiles,
    FileSources,
    FolderDefinitions,
    Header,
    WorkspaceHeader,
    create_workspace,
    file_sources,
    open_workspace,
)
from ess.batch.web import create_app
from ess.instruments.loki.bindings import BINDINGS
from ess.instruments.loki.web import build, launch, main
from fastapi.testclient import TestClient

from ess.apps import Client
from ess.apps.files import FolderFiles
from ess.apps.folder import write_files_table

LOCAL = WorkspaceHeader(Header('LoKI'))


def opened(
    folder: Path,
) -> tuple[Client, CorrectedFiles, FolderDefinitions]:
    """The local workspace in ``folder``, opened with LoKI's bindings."""
    return open_workspace(folder, LOCAL, BINDINGS, allowed=file_sources(['local']))


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
    folder = create_workspace(tmp_path, 'loki', LOCAL)
    register(folder, (1, 'sample', 'a'), (2, 'transmission', 'a'))
    client, files, definitions = opened(folder)
    with client:
        intake = build(client, files, definitions).intake
        assert intake.following is None
        (label,) = intake.manager.headers()
        intake.manager.remove(label)
        intake = build(client, files, definitions, follow=True).intake
        assert intake.following == next(iter(intake.manager.headers()))


def test_a_run_registered_while_the_page_runs_is_added_by_a_step_of_follow(
    tmp_path: Path,
) -> None:
    def names(state: dict[str, Any]) -> list[str]:
        return [row['name'] for row in state['rows']]

    first = [(1, 'sample', 'a'), (2, 'transmission', 'a')]
    folder = create_workspace(tmp_path, 'loki', LOCAL)
    register(folder, *first)
    client, files, definitions = opened(folder)
    with client:
        pages = build(client, files, definitions, follow=True)
        app = create_app(pages)
        http = TestClient(app, base_url=str(TestClient(app).get('/').url))
        before = names(http.get('/api/state').json())
        register(folder, *first, (3, 'sample', 'b'), (4, 'transmission', 'b'))
        relisted = names(http.post('/api/relist').json())
        pages.tick()
        stepped = names(http.get('/api/state').json())

    assert (before, relisted, stepped) == (['a'], ['a'], ['a', 'b'])


def test_the_output_tab_offers_nxcansas_first(tmp_path: Path) -> None:
    folder = create_workspace(tmp_path, 'loki', LOCAL)
    register(folder, (1, 'sample', 'a'), (2, 'transmission', 'a'))
    client, files, definitions = opened(folder)
    with client:
        app = create_app(build(client, files, definitions))
        http = TestClient(app, base_url=str(TestClient(app).get('/').url))
        output = http.get('/api/state').json()['output']

    assert [(f['name'], f['outputs']) for f in output['formats']] == [
        ('nxcansas', ['iofq']),
        ('scipp-hdf5', None),
    ]
    assert output['options']['format'] == 'nxcansas'


def test_a_workspace_opens_with_the_first_batch_of_its_file_source(
    tmp_path: Path,
) -> None:
    local = create_workspace(tmp_path, 'local', LOCAL)
    register(local, (1, 'sample', 'a'), (2, 'transmission', 'a'))
    catalogued = WorkspaceHeader(Header('Catalogued'), 'p1', 'scicat:fake')
    create_workspace(tmp_path, 'catalogued', catalogued)
    allowed: FileSources = {
        **file_sources(['local']),
        'scicat:fake': lambda folder, proposal: FolderFiles(folder, []),
    }

    with TestClient(launch(tmp_path, allowed)) as http:
        titles = {
            name: [
                b['title']
                for b in http.get(f'/workspaces/{name}/api/batches').json()['rows']
            ]
            for name in ('local', 'catalogued')
        }

    assert titles == {'local': ['Sample runs'], 'catalogued': ['Raw runs']}


def test_the_server_refuses_to_allow_no_file_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_:
        main([str(tmp_path), '--no-local'])

    assert exit_.value.code == 2
    assert '--no-local needs --scicat' in capsys.readouterr().err


def test_a_login_that_fails_at_the_start_ends_the_server_with_a_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_:
        main([str(tmp_path), '--scicat', 'no-such-profile'])

    assert exit_.value.code == 1
    assert capsys.readouterr().err.startswith(
        'Cannot log in to SciCat no-such-profile: '
    )
