# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from pathlib import Path

import h5py
from ess.instruments.loki.files import read_metadata


def test_a_file_of_the_detector_test_is_a_raw_file_of_the_run_in_its_name(
    sample: Path,
) -> None:
    assert read_metadata(sample) == {
        'kind': 'raw',
        'run': '60339',
        'title': 'My experiment',
    }


def test_a_file_with_an_entry_identifier_is_a_raw_file_of_that_run(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'loki_336349_00000342.hdf'
    with h5py.File(path, 'w') as file:
        entry = file.create_group('entry')
        entry.attrs['NX_class'] = 'NXentry'
        entry['entry_identifier'] = '342'
        entry['title'] = 'empty beam'
        entry.create_group('sample')['name'] = 'water'

    assert read_metadata(path) == {
        'kind': 'raw',
        'run': '342',
        'sample': 'water',
        'title': 'empty beam',
    }


def test_an_entry_identifier_that_is_a_number_gives_the_run_as_its_text(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'raw.hdf'
    with h5py.File(path, 'w') as file:
        entry = file.create_group('entry')
        entry.attrs['NX_class'] = 'NXentry'
        entry['entry_identifier'] = 342

    assert read_metadata(path) == {'kind': 'raw', 'run': '342'}


def test_a_file_with_an_entry_uuid_has_the_pid_the_catalogue_lists_it_by(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'raw.hdf'
    with h5py.File(path, 'w') as file:
        entry = file.create_group('entry')
        entry.attrs['NX_class'] = 'NXentry'
        entry['entry_identifier'] = '342'
        entry['entry_identifier_uuid'] = '2d1f-7a3c'

    assert read_metadata(path) == {
        'kind': 'raw',
        'run': '342',
        'pid': '20.500.12269/2d1f-7a3c',
    }


def test_an_xml_file_is_a_mask(mask_file: Path) -> None:
    assert read_metadata(mask_file) == {'kind': 'mask'}


def test_a_file_that_scipp_wrote_is_a_calibration_without_a_role(
    direct_beam: Path,
) -> None:
    assert read_metadata(direct_beam) == {'kind': 'calibration'}


def test_any_other_file_is_none_of_lokis(tmp_path: Path) -> None:
    (tmp_path / 'notes.txt').write_text('notes')
    with h5py.File(tmp_path / 'other.h5', 'w') as file:
        file['x'] = 1.0

    assert read_metadata(tmp_path / 'notes.txt') is None
    assert read_metadata(tmp_path / 'other.h5') is None
