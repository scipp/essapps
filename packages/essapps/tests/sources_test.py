# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A folder is the local application's dataset source.

See docs/developer/rules.md.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from ess.apps.rules import precedes
from ess.apps.sources import (
    AS_IS,
    Dataset,
    FieldExtractor,
    FolderSource,
    field_extractor,
)
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


def test_the_journal_entry_is_the_fields_of_a_run_by_default(tmp_path: Path) -> None:
    write(tmp_path, 'dream_1.nxs')
    write(tmp_path, 'dream_2.nxs')
    source = FolderSource(tmp_path, journal={1: {'sample': 'vanadium'}})
    first, second = source.new_datasets('p1')
    assert first.fields == {'run': 1, 'sample': 'vanadium'}
    assert second.fields == {'run': 2}


def title_fields(entry: Mapping[str, Any], path: Path) -> dict[str, Any]:
    """An instrument whose convention is a role prefix on the run title."""
    role, _, sample = entry['title'].partition(': ')
    return {'role': role, 'sample': sample, 'start': entry['start']}


def test_the_instruments_extractor_derives_the_fields(tmp_path: Path) -> None:
    write(tmp_path, 'loki_1.nxs')
    source = FolderSource(
        tmp_path,
        journal={1: {'title': 'can: quartz cell', 'start': 10}},
        fields=FieldExtractor(title_fields),
    )
    (dataset,) = source.new_datasets('p1')
    assert dataset.fields == {
        'run': 1,
        'role': 'can',
        'sample': 'quartz cell',
        'start': 10,
    }


def test_datasets_are_ordered_by_the_field_the_extractor_declares(
    tmp_path: Path,
) -> None:
    """Run numbers need not follow acquisition: here run 2 was measured first."""
    write(tmp_path, 'loki_1.nxs')
    write(tmp_path, 'loki_2.nxs')
    journal = {
        1: {'title': 'sample: a', 'start': 20},
        2: {'title': 'can: a', 'start': 10},
    }
    ordered = FolderSource(
        tmp_path, journal=journal, fields=FieldExtractor(title_fields, order='start')
    )
    first, second = ordered.new_datasets('p1')
    assert precedes(second, first)
    by_run = FolderSource(
        tmp_path, journal=journal, fields=FieldExtractor(title_fields)
    )
    first, second = by_run.new_datasets('p1')
    assert precedes(first, second)


def test_an_instrument_without_a_registered_extractor_keeps_the_entry_as_is() -> None:
    assert field_extractor('dream') is AS_IS
