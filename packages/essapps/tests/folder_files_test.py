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


def test_a_row_of_a_missing_file_is_refused(folder: Path) -> None:
    with pytest.raises(FileNotFoundError, match='gone'):
        FolderFiles(folder, [{'file': 'gone.nxs'}])


def test_two_rows_of_one_file_are_refused(folder: Path) -> None:
    with pytest.raises(ValueError, match='two rows name'):
        FolderFiles(folder, [{'file': 'a.nxs'}, {'file': 'a.nxs'}])


@pytest.mark.parametrize(('field', 'value'), [('run', 1), ('pid', 'x/1')])
def test_two_rows_of_one_run_or_pid_are_refused(
    folder: Path, field: str, value: object
) -> None:
    rows = [{'file': 'a.nxs', field: value}, {'file': 'b.nxs', field: value}]
    with pytest.raises(ValueError, match='two rows give'):
        FolderFiles(folder, rows)


@pytest.mark.parametrize('pid', ['2d1f-7a3c', '/2d1f-7a3c', '20.500.12269/'])
def test_a_pid_not_written_in_full_is_refused(folder: Path, pid: str) -> None:
    with pytest.raises(ValueError, match=r'a\.nxs is not written in full'):
        FolderFiles(folder, [{'file': 'a.nxs', 'pid': pid}])


def test_a_relative_path_is_relative_to_the_working_directory(
    files: FolderFiles, folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(folder)
    assert file(path='mask.xml') == FileRef.at(folder / 'mask.xml')
    assert files.path(files.resolve(file(path='mask.xml'))) == folder / 'mask.xml'


@pytest.mark.parametrize('lookup', ['path', 'metadata'])
def test_an_identity_the_source_does_not_know_raises_keyerror(
    files: FolderFiles, lookup: str
) -> None:
    with pytest.raises(KeyError, match=r'does not know the file path:gone\.nxs'):
        getattr(files, lookup)(FileRef(file='path:gone.nxs'))


def test_without_a_root_a_file_is_identified_by_its_absolute_path(
    files: FolderFiles, folder: Path
) -> None:
    assert files.resolve(file(run=1)) == FileRef.at(folder / 'a.nxs')


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """The folder of a workspace, with a run and a mask, next to another folder."""
    root = tmp_path / 'workspace'
    (root / 'masks').mkdir(parents=True)
    (tmp_path / 'other').mkdir()
    for path in ('workspace/run-1.nxs', 'workspace/masks/main.xml', 'other/run-2.nxs'):
        (tmp_path / path).touch()
    return root


@pytest.fixture
def rooted(root: Path) -> FolderFiles:
    return FolderFiles(
        root,
        [
            {'file': 'run-1.nxs', 'run': 1, 'pid': '20.500.12269/abc'},
            {'file': 'masks/main.xml', 'kind': 'mask'},
            {'file': '../other/run-2.nxs', 'run': 2},
        ],
        root=root,
    )


def test_a_file_is_identified_by_its_pid_or_else_by_its_path_relative_to_the_root(
    rooted: FolderFiles, root: Path
) -> None:
    assert rooted.list(Selector()) == [
        FileRef(file='pid:20.500.12269/abc'),
        FileRef.at(root.parent / 'other' / 'run-2.nxs'),
    ]
    assert rooted.list(Selector(kind='mask')) == [FileRef(file='path:masks/main.xml')]


@pytest.mark.parametrize(
    ('name', 'identity', 'path'),
    [
        (file(run=1), 'pid:20.500.12269/abc', 'run-1.nxs'),
        (file(pid='20.500.12269/abc'), 'pid:20.500.12269/abc', 'run-1.nxs'),
        (FileRef.at('run-1.nxs'), 'pid:20.500.12269/abc', 'run-1.nxs'),
        (FileRef.at('masks/main.xml'), 'path:masks/main.xml', 'masks/main.xml'),
    ],
)
def test_a_name_resolves_to_the_identity_whose_path_is_absolute(
    rooted: FolderFiles, root: Path, name: FileRef, identity: str, path: str
) -> None:
    ref = rooted.resolve(name)

    assert ref == FileRef(file=identity)
    assert rooted.path(ref) == root / path
    assert rooted.resolve(file(path=root / path)) == ref


def test_one_raw_file_in_two_sources_has_one_identity(tmp_path: Path) -> None:
    identities = set()
    for name in ('a', 'b'):
        (tmp_path / name).mkdir()
        (tmp_path / name / 'run-1.nxs').touch()
        row = {'file': 'run-1.nxs', 'run': 1, 'pid': '20.500.12269/abc'}
        files = FolderFiles(tmp_path / name, [row], root=tmp_path / name)
        identities.add(files.resolve(file(run=1)))

    assert identities == {FileRef(file='pid:20.500.12269/abc')}


@pytest.fixture
def linked(tmp_path: Path) -> Path:
    """A workspace's folder reached through a symbolic link, next to another."""
    (tmp_path / 'real' / 'workspace').mkdir(parents=True)
    (tmp_path / 'real' / 'other').mkdir()
    (tmp_path / 'real' / 'workspace' / 'run-1.nxs').touch()
    (tmp_path / 'real' / 'other' / 'run-2.nxs').touch()
    (tmp_path / 'link').symlink_to(tmp_path / 'real' / 'workspace')
    return tmp_path / 'link'


def test_a_path_through_a_symbolic_link_is_the_real_path(linked: Path) -> None:
    rows = [{'file': 'run-1.nxs', 'run': 1}, {'file': '../other/run-2.nxs', 'run': 2}]
    files = FolderFiles(linked, rows, root=linked)
    other = linked.resolve().parent / 'other' / 'run-2.nxs'

    assert files.resolve(file(run=1)) == FileRef(file='path:run-1.nxs')
    assert files.resolve(file(run=2)) == FileRef.at(other)
    assert files.path(files.resolve(file(run=2))) == other
    assert files.resolve(file(path=linked / 'run-1.nxs')) == files.resolve(file(run=1))
