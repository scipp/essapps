# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The workspaces of a root folder, and what a workspace is opened with."""

import re
from pathlib import Path

import pytest
from ess.batch import (
    FileSources,
    Header,
    WorkspaceHeader,
    create_workspace,
    file_sources,
    open_workspace,
    remove_workspace,
    workspaces,
)

from ess.apps.files import Selector
from ess.apps.protocol import FileSource
from ess.apps.testing import FakeFiles

LOCAL = file_sources(['local'])
CATALOGUED = WorkspaceHeader(Header('Commissioning', 'Day 1'), '336349', 'scicat:fake')


@pytest.fixture
def made() -> list[tuple[Path, str, FakeFiles]]:
    """The file sources that :func:`fake_scicat` made: folder, proposal, source."""
    return []


@pytest.fixture
def fake_scicat(made: list[tuple[Path, str, FakeFiles]]) -> FileSources:
    """The file source ``scicat:fake``, without a catalogue."""

    def source(folder: Path, proposal: str) -> FileSource:
        files = FakeFiles(proposal, folder)
        made.append((folder, proposal, files))
        return files

    return {'scicat:fake': source}


def test_a_created_workspace_is_listed_with_its_header(tmp_path: Path) -> None:
    local = WorkspaceHeader(Header('Test'))

    create_workspace(tmp_path, 'b-local', local)
    create_workspace(tmp_path, 'a-commissioning', CATALOGUED)

    assert workspaces(tmp_path) == {'a-commissioning': CATALOGUED, 'b-local': local}


def test_a_folder_of_the_root_without_a_workspace_header_is_not_listed(
    tmp_path: Path,
) -> None:
    (tmp_path / 'data').mkdir()

    assert workspaces(tmp_path) == {}


@pytest.mark.parametrize(
    ('name', 'said'), [('a/b', 'use letters'), ('..', 'use letters')]
)
def test_a_name_that_cannot_be_a_label_is_refused(
    tmp_path: Path, name: str, said: str
) -> None:
    with pytest.raises(ValueError, match=said):
        create_workspace(tmp_path / 'root', name, CATALOGUED)

    assert not (tmp_path / 'root').exists()


def test_a_name_of_a_folder_that_exists_is_refused(tmp_path: Path) -> None:
    (tmp_path / 'taken').mkdir()

    with pytest.raises(ValueError, match='taken: the folder exists'):
        create_workspace(tmp_path, 'taken', CATALOGUED)


def test_a_removed_workspace_is_gone_with_its_folder(tmp_path: Path) -> None:
    folder = create_workspace(tmp_path, 'old', CATALOGUED)
    (folder / 'store').mkdir()

    remove_workspace(tmp_path, 'old')

    assert (workspaces(tmp_path), folder.exists()) == ({}, False)


def test_a_folder_without_a_workspace_header_is_not_removed(tmp_path: Path) -> None:
    (tmp_path / 'data').mkdir()

    with pytest.raises(KeyError):
        remove_workspace(tmp_path, 'data')

    assert (tmp_path / 'data').is_dir()


def test_a_workspace_header_that_cannot_be_read_is_refused_with_its_path(
    tmp_path: Path,
) -> None:
    (tmp_path / 'broken').mkdir()
    (tmp_path / 'broken' / 'workspace.json').write_text('{"title": "x"}')

    with pytest.raises(ValueError, match=r'broken/workspace\.json'):
        workspaces(tmp_path)


def test_a_workspace_opens_with_the_proposal_and_files_of_its_header(
    tmp_path: Path,
    fake_scicat: FileSources,
    made: list[tuple[Path, str, FakeFiles]],
) -> None:
    folder = create_workspace(tmp_path, 'commissioning', CATALOGUED)

    client, files, _ = open_workspace(folder, [], allowed=fake_scicat)
    with client:
        assert client.proposal == '336349'
        assert made == [(folder, '336349', files.source)]


def test_a_workspace_of_a_file_source_not_allowed_is_refused_with_a_message(
    tmp_path: Path,
) -> None:
    folder = create_workspace(tmp_path, 'commissioning', CATALOGUED)

    said = (
        'commissioning: its files are of scicat:fake, which this application '
        'does not offer (it offers local)'
    )
    with pytest.raises(ValueError, match=re.escape(said)):
        open_workspace(folder, [], allowed=LOCAL)

    assert not (folder / 'log.jsonl').exists()


def test_a_folder_without_a_workspace_header_is_local_of_the_proposal_local(
    tmp_path: Path, fake_scicat: FileSources
) -> None:
    with pytest.raises(ValueError, match='its files are of local'):
        open_workspace(tmp_path, [], allowed=fake_scicat)
    client, _, _ = open_workspace(tmp_path, [], allowed=LOCAL)
    with client:
        assert client.proposal == 'local'


def test_a_local_workspace_without_a_files_table_opens_and_lists_no_file(
    tmp_path: Path,
) -> None:
    folder = create_workspace(tmp_path, 'new', WorkspaceHeader(Header('New')))

    client, files, _ = open_workspace(folder, [], allowed=LOCAL)
    with client:
        assert files.list(Selector()) == []


def test_a_file_source_of_another_name_is_refused() -> None:
    with pytest.raises(ValueError, match="'scicat:': a file source is"):
        file_sources(['scicat:'])
