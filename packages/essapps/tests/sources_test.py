# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A folder is the local application's dataset source.

See docs/developer/rules.md.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ess.apps.examples import write_run
from ess.apps.rules import precedes
from ess.apps.runner import file_checksum
from ess.apps.sources import (
    AS_IS,
    Dataset,
    FieldExtractor,
    FolderSource,
    field_extractor,
)
from ess.apps.spec import dataset_ref


def write(folder: Path, name: str) -> Path:
    """A file whose bytes, its name, differ from every other file's."""
    path = folder / name
    path.write_bytes(name.encode())
    return path


def test_a_file_is_identified_by_the_uuid_it_carries(tmp_path: Path) -> None:
    write_run(tmp_path / 'dream_4711.nxs', [1.0], uuid='05165700-0292-5e93')
    (dataset,) = FolderSource(tmp_path, proposal='p1').datasets(['p1'])
    assert dataset.ref == dataset_ref(uuid='05165700-0292-5e93')


def test_a_file_carrying_no_uuid_is_identified_by_its_bytes(tmp_path: Path) -> None:
    path = write(tmp_path, 'calibration.nxs')
    (dataset,) = FolderSource(tmp_path, proposal='p1').datasets(['p1'])
    assert dataset.ref == dataset_ref(sha256=file_checksum(path))


def test_the_run_a_file_name_carries_is_a_field(tmp_path: Path) -> None:
    write(tmp_path, 'dream_4711.nxs')
    (dataset,) = FolderSource(tmp_path, proposal='p1').datasets(['p1'])
    assert (dataset.instrument, dataset.fields) == ('dream', {'run': 4711})


def test_a_pid_is_the_identity_when_the_source_knows_one(tmp_path: Path) -> None:
    dataset = Dataset(
        path=tmp_path / 'x.nxs', proposals=['p1'], pid='20.500/abc', uuid='u'
    )
    assert dataset.ref == dataset_ref(pid='20.500/abc')


def test_a_dataset_without_an_identity_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match='needs a pid, uuid, or sha256'):
        Dataset(path=tmp_path / 'x.nxs', proposals=['p1'])


def test_a_folder_lists_its_datasets_for_its_own_proposal_only(
    tmp_path: Path,
) -> None:
    write(tmp_path, 'dream_1.nxs')
    source = FolderSource(tmp_path, proposal='p1')
    assert [d.proposals for d in source.datasets(['p2', 'p1'])] == [['p1']]
    assert source.datasets(['p2']) == []


def test_the_pattern_selects_what_the_folder_offers(tmp_path: Path) -> None:
    write(tmp_path, 'dream_1.nxs')
    write(tmp_path, 'notes.txt')
    source = FolderSource(tmp_path, '*.nxs', proposal='p1')
    assert [d.path.name for d in source.datasets(['p1'])] == ['dream_1.nxs']


def test_a_run_number_or_a_path_finds_the_dataset(tmp_path: Path) -> None:
    path = write(tmp_path, 'dream_4711.nxs')
    source = FolderSource(tmp_path, proposal='p1')
    identity = dataset_ref(sha256=file_checksum(path))
    run = dataset_ref(instrument='dream', run=4711)
    for ref in (identity, run, dataset_ref(path=path)):
        found = source.find(ref)
        assert found is not None
        assert found.ref == identity
    assert source.find(dataset_ref(instrument='dream', run=1)) is None
    assert source.find(dataset_ref(instrument='loki', run=4711)) is None


def test_a_run_number_that_names_two_datasets_finds_none(tmp_path: Path) -> None:
    """A copy of a run under another name is two files with one run number."""
    write(tmp_path, 'dream_1.nxs')
    write(tmp_path, 'DREAM_1.nxs')
    source = FolderSource(tmp_path, proposal='p1')
    with pytest.raises(ValueError, match='names several datasets'):
        source.find(dataset_ref(instrument='dream', run=1))


def test_locate_finds_the_bytes_of_a_known_identity_only(tmp_path: Path) -> None:
    path = write(tmp_path, 'dream_4711.nxs')
    source = FolderSource(tmp_path, proposal='p1')
    assert source.locate(dataset_ref(sha256=file_checksum(path))) == path
    assert source.locate(dataset_ref(pid='20.500/abc')) is None


def test_a_file_that_moved_keeps_its_identity(tmp_path: Path) -> None:
    """Identity is not location: the same file in another folder still resolves."""
    path = write(tmp_path, 'dream_4711.nxs')
    identity = dataset_ref(sha256=file_checksum(path))
    moved = tmp_path / 'archive'
    moved.mkdir()
    path.rename(moved / 'renamed.nxs')
    assert FolderSource(tmp_path, proposal='p1').locate(identity) is None
    assert FolderSource(moved, proposal='p1').locate(identity) == (
        moved / 'renamed.nxs'
    )


def test_another_filename_shape_gives_the_run(tmp_path: Path) -> None:
    """LoKI tutorial files are ``<run>-<date>.nxs`` and carry no instrument."""
    write(tmp_path, '60393-2022-02-28_2215.nxs')
    source = FolderSource(
        tmp_path, '*.nxs', proposal='p1', stem=r'(?P<run>\d+)-.*', instrument='loki'
    )
    (dataset,) = source.datasets(['p1'])
    assert (dataset.instrument, dataset.run) == ('loki', 60393)


def test_a_run_number_without_an_instrument_is_refused(tmp_path: Path) -> None:
    write(tmp_path, '60393-2022-02-28_2215.nxs')
    source = FolderSource(tmp_path, '*.nxs', proposal='p1', stem=r'(?P<run>\d+)-.*')
    with pytest.raises(ValueError, match='no instrument'):
        source.datasets(['p1'])


def test_the_journal_entry_is_the_fields_of_a_run_by_default(tmp_path: Path) -> None:
    write(tmp_path, 'dream_1.nxs')
    write(tmp_path, 'dream_2.nxs')
    source = FolderSource(tmp_path, proposal='p1', journal={1: {'sample': 'vanadium'}})
    first, second = source.datasets(['p1'])
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
        proposal='p1',
        journal={1: {'title': 'can: quartz cell', 'start': 10}},
        fields=FieldExtractor(title_fields),
    )
    (dataset,) = source.datasets(['p1'])
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
        tmp_path,
        proposal='p1',
        journal=journal,
        fields=FieldExtractor(title_fields, order='start'),
    )
    first, second = ordered.datasets(['p1'])
    assert precedes(second, first)
    by_run = FolderSource(
        tmp_path, proposal='p1', journal=journal, fields=FieldExtractor(title_fields)
    )
    first, second = by_run.datasets(['p1'])
    assert precedes(first, second)


def test_an_order_value_that_is_no_number_or_datetime_is_refused(
    tmp_path: Path,
) -> None:
    """A journal gives times as strings, which order but do not subtract."""
    write(tmp_path, 'loki_1.nxs')
    write(tmp_path, 'loki_2.nxs')
    journal = {
        1: {'title': 'sample: a', 'start': datetime(2026, 9, 1, 10, tzinfo=UTC)},
        2: {'title': 'can: a', 'start': '2026-09-01T10:00'},
    }
    source = FolderSource(
        tmp_path,
        proposal='p1',
        journal=journal,
        fields=FieldExtractor(title_fields, order='start'),
    )
    first, second = source.datasets(['p1'])
    assert first.error is None
    assert second.error == (
        f"{second.ref}: its order field 'start' is '2026-09-01T10:00', "
        'not a number or a datetime'
    )


def test_an_extractor_failing_on_one_dataset_leaves_the_others(tmp_path: Path) -> None:
    write(tmp_path, 'loki_1.nxs')
    write(tmp_path, 'loki_2.nxs')
    source = FolderSource(
        tmp_path,
        proposal='p1',
        journal={1: {'title': 'can: a', 'start': 10}},
        fields=FieldExtractor(title_fields),
    )
    first, second = source.datasets(['p1'])
    assert first.fields['role'] == 'can'
    assert second.error == f"{second.ref}: no fields derived: KeyError('title')"


def test_an_instrument_without_a_registered_extractor_keeps_the_entry_as_is() -> None:
    assert field_extractor('dream') is AS_IS
