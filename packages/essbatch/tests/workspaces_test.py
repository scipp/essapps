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
    read_workspace_header,
    remove_workspace,
    workspaces,
    write_workspace_header,
)

from ess.apps.files import Selector
from ess.apps.folder import open_folder
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


def test_the_workspaces_of_a_root_are_listed_by_name(tmp_path: Path) -> None:
    for name in ['b', 'c', 'a']:
        create_workspace(tmp_path, name, CATALOGUED)

    assert workspaces(tmp_path) == ['a', 'b', 'c']


def test_a_created_workspace_has_its_header(tmp_path: Path) -> None:
    folder = create_workspace(tmp_path, 'commissioning', CATALOGUED)

    assert read_workspace_header(folder) == CATALOGUED


@pytest.mark.parametrize('name', ['data', '.removing-old-1'])
def test_a_folder_without_a_header_or_whose_name_is_no_label_is_not_listed(
    tmp_path: Path, name: str
) -> None:
    (tmp_path / name).mkdir()
    if name.startswith('.'):
        write_workspace_header(tmp_path / name, CATALOGUED)

    assert workspaces(tmp_path) == []


@pytest.mark.parametrize('name', ['a/b', '..'])
def test_a_name_that_cannot_be_a_label_is_refused(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match='use letters'):
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

    assert list(tmp_path.iterdir()) == []


def test_a_removal_that_fails_halfway_leaves_no_workspace_and_the_name_free(
    tmp_path: Path,
) -> None:
    folder = create_workspace(tmp_path, 'old', CATALOGUED)
    stuck = folder / 'store'
    stuck.mkdir()
    (stuck / 'output.h5').write_text('')
    stuck.chmod(0o500)  # its file cannot be deleted
    try:
        with pytest.raises(OSError, match='Permission denied'):
            remove_workspace(tmp_path, 'old')

        assert workspaces(tmp_path) == []
        create_workspace(tmp_path, 'old', CATALOGUED)
    finally:
        for left in tmp_path.glob('.removing-old-*/store'):
            left.chmod(0o700)


def test_a_workspace_that_a_process_holds_is_not_removed(tmp_path: Path) -> None:
    folder = create_workspace(tmp_path, 'old', WorkspaceHeader(Header('Old')))

    with open_folder(folder, [], files=FakeFiles('local', folder)):
        with pytest.raises(RuntimeError, match='held by another process'):
            remove_workspace(tmp_path, 'old')

    assert workspaces(tmp_path) == ['old']


def test_a_workspace_folder_that_is_a_link_is_not_removed(tmp_path: Path) -> None:
    target = create_workspace(tmp_path / 'elsewhere', 'real', CATALOGUED)
    (tmp_path / 'root').mkdir()
    (tmp_path / 'root' / 'linked').symlink_to(target)

    with pytest.raises(ValueError, match=r'linked is a link to .*; delete the link'):
        remove_workspace(tmp_path / 'root', 'linked')

    assert workspaces(tmp_path / 'root') == ['linked']
    assert read_workspace_header(target) == CATALOGUED


def test_a_folder_without_a_workspace_header_is_not_removed(tmp_path: Path) -> None:
    (tmp_path / 'data').mkdir()

    with pytest.raises(KeyError):
        remove_workspace(tmp_path, 'data')

    assert (tmp_path / 'data').is_dir()


def test_a_folder_without_a_workspace_header_says_how_to_write_one(
    tmp_path: Path,
) -> None:
    said = f'{tmp_path} has no workspace.json: "essbatch -w {tmp_path} files" writes'
    with pytest.raises(ValueError, match=re.escape(said)):
        read_workspace_header(tmp_path)


def test_a_workspace_header_that_cannot_be_read_is_refused_with_its_path(
    tmp_path: Path,
) -> None:
    (tmp_path / 'workspace.json').write_text('{"title": "x"}')

    with pytest.raises(ValueError, match=re.escape(f'{tmp_path}/workspace.json')):
        read_workspace_header(tmp_path)


def test_a_workspace_opens_with_the_proposal_and_files_of_its_header(
    tmp_path: Path,
    fake_scicat: FileSources,
    made: list[tuple[Path, str, FakeFiles]],
) -> None:
    folder = create_workspace(tmp_path, 'commissioning', CATALOGUED)

    client, files, _ = open_workspace(folder, CATALOGUED, [], allowed=fake_scicat)
    with client:
        assert client.proposal == '336349'
        assert made == [(folder, '336349', files.source)]


def test_a_workspace_of_a_file_source_not_allowed_is_refused_with_a_message(
    tmp_path: Path,
) -> None:
    folder = create_workspace(tmp_path, 'commissioning', CATALOGUED)

    said = (
        'commissioning: its file source is scicat:fake, which this application '
        'does not allow (it allows local)'
    )
    with pytest.raises(ValueError, match=re.escape(said)):
        open_workspace(folder, CATALOGUED, [], allowed=LOCAL)

    assert not (folder / 'log.jsonl').exists()


def test_a_local_workspace_without_a_files_table_opens_and_lists_no_file(
    tmp_path: Path,
) -> None:
    header = WorkspaceHeader(Header('New'))
    folder = create_workspace(tmp_path, 'new', header)

    client, files, _ = open_workspace(folder, header, [], allowed=LOCAL)
    with client:
        assert files.list(Selector()) == []


def test_a_file_source_of_another_name_is_refused() -> None:
    with pytest.raises(ValueError, match="'scicat:': a file source is"):
        file_sources(['scicat:'])
