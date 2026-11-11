# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""LoKI's pairing on plain data, and its batch on the files of esssans' tutorial."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from ess.instruments.loki.batch import iofq_batch, pair_by_sample
from ess.instruments.loki.bindings import BINDINGS
from ess.instruments.loki.specs import CALIBRATED_DETECTOR, MASKS, BeamCentre

from ess.apps import Client, FolderFiles, file, local
from ess.apps.batch import FileInfo, ready, remaining
from ess.spec import FileRef


def info(run: int, **metadata: object) -> FileInfo:
    return FileInfo(FileRef(file=f'uuid:{run}'), {'run': run, **metadata})


def test_a_sample_run_pairs_with_the_last_transmission_run_of_its_sample() -> None:
    rows = pair_by_sample(
        [
            info(1, role='sample', sample='water'),
            info(2, role='transmission', sample='water'),
            info(3, role='transmission', sample='water'),
            info(4, role='transmission', sample='sds'),
        ]
    )

    assert [(r.key, r.values['sample_transmission'].file) for r in rows] == [
        ('1', 'uuid:3')
    ]
    assert rows[0].info == {'sample': 'water'}


def test_a_sample_measured_twice_has_two_rows() -> None:
    rows = pair_by_sample(
        [info(1, role='sample', sample='w'), info(2, role='sample', sample='w')]
    )

    assert [r.key for r in rows] == ['1', '2']


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
    assert 'sample_transmission' not in pair_by_sample(files)[0].values


def test_other_runs_make_no_row() -> None:
    assert pair_by_sample([info(1, role='background'), info(2)]) == []


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
    batch = iofq_batch(client)
    batch.scan()
    assert ready(batch.state()) == ['60339', '60250']

    records = batch.reduce(settings, ready(batch.state()))

    assert client.wait(records) == {'60339': 'completed', '60250': 'completed'}
    assert client.output(records['60250'], 'iofq').intensity.sizes == {'Q': 100}
    assert remaining(batch.state(settings)) == []
    batch.close()
