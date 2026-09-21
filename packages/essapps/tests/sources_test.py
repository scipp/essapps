# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A folder is the local application's dataset source.

See docs/developer/rules.md.
"""

from pathlib import Path

import pytest

from ess.apps.sources import Dataset, FolderSource
from ess.apps.spec import dataset_ref


def write(folder: Path, name: str) -> Path:
    path = folder / name
    path.write_bytes(b'nexus')
    return path


def test_a_file_is_identified_by_the_run_it_carries(tmp_path: Path) -> None:
    write(tmp_path, 'dream_4711.nxs')
    (dataset,) = FolderSource(tmp_path).new_datasets('p1')
    assert dataset.ref == dataset_ref(instrument='dream', run=4711)


def test_a_file_carrying_no_run_is_identified_by_its_path(tmp_path: Path) -> None:
    path = write(tmp_path, 'calibration.nxs')
    (dataset,) = FolderSource(tmp_path).new_datasets('p1')
    assert dataset.ref == dataset_ref(path=path)


def test_a_pid_is_the_identity_when_the_source_knows_one(tmp_path: Path) -> None:
    dataset = Dataset(path=tmp_path / 'x.nxs', pid='20.500/abc', instrument='dream')
    assert dataset.ref == dataset_ref(pid='20.500/abc')


def test_the_pattern_selects_what_the_folder_offers(tmp_path: Path) -> None:
    write(tmp_path, 'dream_1.nxs')
    write(tmp_path, 'notes.txt')
    source = FolderSource(tmp_path, '*.nxs')
    assert [d.path.name for d in source.new_datasets('p1')] == ['dream_1.nxs']


def test_locate_finds_the_bytes_of_a_known_identity_only(tmp_path: Path) -> None:
    path = write(tmp_path, 'dream_4711.nxs')
    source = FolderSource(tmp_path)
    assert source.locate(dataset_ref(instrument='dream', run=4711)) == path
    assert source.locate(dataset_ref(instrument='dream', run=1)) is None
    assert source.locate(dataset_ref(pid='20.500/abc')) is None


def test_a_file_that_moved_is_located_where_it_is_now(tmp_path: Path) -> None:
    """Identity is not location: the same run in another folder still resolves."""
    write(tmp_path, 'dream_4711.nxs')
    moved = tmp_path / 'archive'
    moved.mkdir()
    (tmp_path / 'dream_4711.nxs').rename(moved / 'dream_4711.nxs')
    assert (
        FolderSource(tmp_path).locate(dataset_ref(instrument='dream', run=4711)) is None
    )
    assert FolderSource(moved).locate(dataset_ref(instrument='dream', run=4711)) == (
        moved / 'dream_4711.nxs'
    )


def test_another_filename_shape_gives_the_run_identity(tmp_path: Path) -> None:
    """LoKI tutorial files are ``<run>-<date>.nxs`` and carry no instrument."""
    write(tmp_path, '60393-2022-02-28_2215.nxs')
    source = FolderSource(
        tmp_path, '*.nxs', identity=r'(?P<run>\d+)-.*', instrument='loki'
    )
    (dataset,) = source.new_datasets('p1')
    assert dataset.ref == dataset_ref(instrument='loki', run=60393)


def test_a_run_number_without_an_instrument_is_refused(tmp_path: Path) -> None:
    write(tmp_path, '60393-2022-02-28_2215.nxs')
    source = FolderSource(tmp_path, '*.nxs', identity=r'(?P<run>\d+)-.*')
    with pytest.raises(ValueError, match='no instrument'):
        source.new_datasets('p1')
