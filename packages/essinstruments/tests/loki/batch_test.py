# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""LoKI's pairing on plain data, and its batch on the files of esssans' tutorial."""

from collections.abc import Iterator
from pathlib import Path

import pandas as pd
import pytest
from ess.batch import Batch, FileInfo, left_to_reduce, ready, rows_of
from ess.batch.table import dtypes
from ess.instruments.loki.batch import SAMPLE_RUNS, pair_by_sample
from ess.instruments.loki.bindings import BINDINGS
from ess.instruments.loki.specs import (
    CALIBRATED_DETECTOR,
    IOFQ,
    MASKS,
    BeamCentre,
    IofQParams,
)

from ess.apps import Client, FolderFiles, file, local
from ess.spec import FileRef


def info(run: int, **metadata: object) -> FileInfo:
    return FileInfo(FileRef(file=f'uuid:{run}'), {'run': run, **metadata})


def test_a_column_of_iofq_has_the_dtype_of_its_parameter() -> None:
    assert dtypes(IofQParams) == {
        'sample': 'string',
        'sample_transmission': 'string',
        'can': 'string',
        'can_transmission': 'string',
        'empty_beam': 'string',
        'direct_beam': 'string',
        'masks': 'string',
        'beam_centre': 'object',
        'wavelength': 'object',
        'q': 'object',
        'corrections': 'object',
    }


def test_a_sample_run_pairs_with_the_last_transmission_run_of_its_sample() -> None:
    rows = pair_by_sample(
        [
            info(1, role='sample', sample='water'),
            info(2, role='transmission', sample='water'),
            info(3, role='transmission', sample='water'),
            info(4, role='transmission', sample='sds'),
        ]
    )

    assert rows.to_dict('index') == {
        '1': {
            'run': 1,
            'name': 'water',
            'sample': 'uuid:1',
            'sample_transmission': 'uuid:3',
        }
    }


def test_the_rows_do_not_depend_on_the_order_the_files_are_listed_in() -> None:
    files = [
        info(10, role='sample', sample='water'),
        info(9, role='transmission', sample='water'),
        info(11, role='transmission', sample='water'),
        info(2, role='sample', sample='water'),
    ]

    rows = pair_by_sample(files)

    pd.testing.assert_frame_equal(rows, pair_by_sample(files[::-1]))
    assert rows['sample_transmission'].to_dict() == {'2': 'uuid:11', '10': 'uuid:11'}


def test_the_run_of_a_row_is_its_run_number_and_empty_without_one() -> None:
    rows = pair_by_sample(
        [
            info(60339, role='sample', sample='water'),
            FileInfo(FileRef(file='uuid:x'), {'role': 'sample', 'sample': 'oil'}),
            FileInfo(FileRef(file='uuid:y'), {'run': 'tmp', 'role': 'sample'}),
        ]
    )

    assert rows['run'].to_dict() == {'60339': 60339, 'uuid:x': None, 'tmp': None}


def test_a_sample_measured_twice_has_two_rows() -> None:
    rows = pair_by_sample(
        [info(1, role='sample', sample='w'), info(2, role='sample', sample='w')]
    )

    assert list(rows.index) == ['1', '2']


@pytest.mark.parametrize(
    'files',
    [
        [info(1, role='sample')],
        [info(1, role='sample', sample='w')],
    ],
)
def test_a_sample_run_without_a_partner_has_an_empty_cell(
    files: list[FileInfo],
) -> None:
    assert pd.isna(pair_by_sample(files).at['1', 'sample_transmission'])


def test_other_runs_make_no_row() -> None:
    assert pair_by_sample([info(1, role='background'), info(2)]).empty


def test_the_source_gives_its_columns_without_files(tmp_path: Path) -> None:
    with local(proposal='p1', files=FolderFiles(tmp_path, []), bind=BINDINGS) as client:
        batch = Batch(client, IOFQ, label='iofq', rows=rows_of(client, SAMPLE_RUNS))

    assert batch.parameter_columns == ('sample', 'sample_transmission')


@pytest.fixture
def client(tutorial_dir: Path, mask_file: Path, direct_beam: Path) -> Iterator[Client]:
    def run(number: int, **metadata: object) -> dict[str, object]:
        return {'file': f'{number}-2022-02-28_2215.nxs', 'run': number, **metadata}

    table = [
        run(60339, role='sample', sample='water'),
        run(60250, role='sample', sample='water'),
        run(60394, role='transmission', sample='water'),
        run(60393, role='background'),
        run(60392),
        {'file': mask_file.name, 'kind': 'mask'},
        {'file': direct_beam.name, 'kind': 'calibration', 'role': 'direct_beam'},
    ]
    files = FolderFiles(tutorial_dir, table)
    with local(proposal='p1', files=files, bind=BINDINGS) as client:
        yield client


def test_a_batch_of_iofq_over_real_workflows(
    client: Client,
    mask_file: Path,
    direct_beam: Path,
    tutorial_centre: BeamCentre,
) -> None:
    detector = client.submit(CALIBRATED_DETECTOR, params={'run': file(run=60339)})
    masks = client.submit(
        MASKS,
        params={
            'detector': detector.ref('detector'),
            'mask_files': [file(path=mask_file)],
        },
    )
    settings = {
        'can': file(run=60393),
        'can_transmission': file(run=60392),
        'empty_beam': file(run=60392),
        'direct_beam': file(path=direct_beam),
        'wavelength': {'start': 1.0, 'stop': 13.0, 'num_bins': 200},
        'q': {'start': 0.01, 'stop': 0.3, 'num_bins': 100},
        'masks': masks.ref('masks'),
        'beam_centre': tutorial_centre.model_dump(),
    }
    batch = Batch(
        client,
        IOFQ,
        label='iofq',
        rows=rows_of(client, SAMPLE_RUNS),
        settings=settings,
        staged=True,
    )
    assert ready(batch.state()) == ['60250', '60339']

    records = batch.reduce(ready(batch.state()))

    assert client.wait(records) == {'60250': 'completed', '60339': 'completed'}
    assert client.output(records['60250'], 'iofq').intensity.sizes == {'Q': 100}
    assert left_to_reduce(batch.state()) == []
    batch.close()
