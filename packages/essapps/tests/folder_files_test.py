# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from pathlib import Path

import pytest

from ess.apps import FolderFiles, Selector, file
from ess.spec import FileRef


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    for name in ('a.nxs', 'b.nxs', 'mask.xml'):
        (tmp_path / name).touch()
    return tmp_path


@pytest.fixture
def files(folder: Path) -> FolderFiles:
    return FolderFiles(
        folder,
        [
            {'file': 'a.nxs', 'run': 1, 'role': 'sample'},
            {'file': 'b.nxs', 'run': 2, 'role': 'background', 'pid': 'x/y'},
            {'file': 'mask.xml', 'kind': 'mask'},
        ],
    )


def test_a_file_is_named_by_run_pid_or_path(files: FolderFiles, folder: Path) -> None:
    assert files.path(files.resolve(file(run=1))) == folder / 'a.nxs'
    assert files.path(files.resolve(file(pid='x/y'))) == folder / 'b.nxs'
    by_path = files.resolve(file(path=folder / 'mask.xml'))
    assert files.path(by_path) == folder / 'mask.xml'


def test_an_identity_resolves_to_itself(files: FolderFiles) -> None:
    identity = files.resolve(file(run=1))
    assert files.resolve(identity) == identity


@pytest.mark.parametrize(
    'name',
    [file(run=3), file(pid='nope'), file(path='/nowhere/a.nxs')],
    ids=['run', 'pid', 'path'],
)
def test_an_unknown_name_raises_keyerror(files: FolderFiles, name: FileRef) -> None:
    with pytest.raises(KeyError):
        files.resolve(name)


def test_metadata_are_the_row_without_the_file_name(files: FolderFiles) -> None:
    assert files.metadata(files.resolve(file(run=1))) == {'run': 1, 'role': 'sample'}


def test_a_change_to_returned_metadata_stays_out_of_the_source(
    files: FolderFiles,
) -> None:
    ref = files.resolve(file(run=1))
    files.metadata(ref)['role'] = 'other'
    assert files.metadata(ref)['role'] == 'sample'


def test_list_selects_by_metadata_in_table_order(files: FolderFiles) -> None:
    raw = files.list(Selector())
    assert [files.metadata(r)['run'] for r in raw] == [1, 2]
    assert len(files.list(Selector(kind='mask'))) == 1
    assert files.list(Selector(role='sample')) == [files.resolve(file(run=1))]


def test_watch_ends_after_the_existing_files(files: FolderFiles) -> None:
    assert list(files.watch(Selector())) == files.list(Selector())


def test_a_row_of_a_missing_file_is_refused(folder: Path) -> None:
    with pytest.raises(FileNotFoundError, match='gone'):
        FolderFiles(folder, [{'file': 'gone.nxs'}])


def test_two_rows_of_one_file_are_refused(folder: Path) -> None:
    with pytest.raises(ValueError, match='two rows name'):
        FolderFiles(folder, [{'file': 'a.nxs'}, {'file': 'a.nxs'}])


@pytest.mark.parametrize('field', ['run', 'pid'])
def test_two_rows_of_one_run_or_pid_are_refused(folder: Path, field: str) -> None:
    with pytest.raises(ValueError, match='two rows give'):
        FolderFiles(folder, [{'file': 'a.nxs', field: 1}, {'file': 'b.nxs', field: 1}])
