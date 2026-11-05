# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The three specs through a client of an in-process backend, over a folder of
files and a table a person fills.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
import scipp as sc
from ess.instruments.loki.bindings import BINDINGS
from ess.instruments.loki.specs import BEAM_CENTRE, IOFQ, MASKS, BeamCentre

from ess.apps import Client, FolderFiles, SubmitError, Template, file, local


@pytest.fixture
def client(sample: Path, mask_file: Path, direct_beam: Path) -> Iterator[Client]:
    table = [
        {'file': '60339-2022-02-28_2215.nxs', 'run': 60339, 'role': 'sample'},
        {'file': '60250-2022-02-28_2215.nxs', 'run': 60250, 'role': 'sample'},
        {'file': '60393-2022-02-28_2215.nxs', 'run': 60393, 'role': 'background'},
        {'file': '60394-2022-02-28_2215.nxs', 'run': 60394, 'role': 'transmission'},
        {'file': '60392-2022-02-28_2215.nxs', 'run': 60392, 'role': 'transmission'},
        {'file': mask_file.name, 'kind': 'mask'},
        {'file': direct_beam.name, 'kind': 'calibration', 'role': 'direct_beam'},
    ]
    files = FolderFiles(sample.parent, table)
    with local(proposal='p1', files=files, bind=BINDINGS) as client:
        yield client


@pytest.fixture
def iofq_settings(direct_beam: Path) -> dict[str, object]:
    return {
        'can': file(run=60393),
        'can_transmission': file(run=60392),
        'empty_beam': file(run=60392),
        'direct_beam': file(path=direct_beam),
        'wavelength': {'start': 1.0, 'stop': 13.0, 'num_bins': 200},
        'q': {'start': 0.01, 'stop': 0.3, 'num_bins': 100},
    }


def test_masks_then_centre_then_iofq(
    client: Client,
    mask_file: Path,
    iofq_settings: dict[str, object],
    plain_esssans_iofq: sc.DataArray,
    tutorial_centre: BeamCentre,
) -> None:
    masks = client.submit(
        MASKS,
        params={'run': file(run=60339), 'mask_files': [file(path=mask_file)]},
    )
    centre = client.submit(
        BEAM_CENTRE,
        params={'sample': file(run=60339), 'masks': masks.ref('masks')},
    )
    iofq = client.submit(
        IOFQ,
        params={
            **iofq_settings,
            'sample': file(run=60339),
            'sample_transmission': file(run=60394),
            'masks': masks.ref('masks'),
            'beam_centre': centre.ref('centre'),
        },
    )
    client.wait(iofq)
    found = client.output(centre, 'centre')
    assert found.x == pytest.approx(tutorial_centre.x)
    assert found.y == pytest.approx(tutorial_centre.y)
    # esssans gives the tutorial's distance to five decimals.
    assert found.z == pytest.approx(tutorial_centre.z, abs=1e-5)
    # The centre is found, not typed. That moves I(Q) far less than its noise, but
    # by more than a relative tolerance can tell: a subtracted intensity can be small.
    computed = client.output(iofq, 'iofq').intensity.data
    difference = sc.abs(sc.values(computed) - sc.values(plain_esssans_iofq.data))
    assert sc.all(difference < 0.1 * sc.stddevs(plain_esssans_iofq.data)).value


def test_batch_through_a_stage_with_one_can(
    client: Client,
    mask_file: Path,
    iofq_settings: dict[str, object],
    tutorial_centre: BeamCentre,
) -> None:
    masks = client.submit(
        MASKS,
        params={'run': file(run=60339), 'mask_files': [file(path=mask_file)]},
    )
    template = Template(
        IOFQ,
        params={
            **iofq_settings,
            'masks': masks.ref('masks'),
            'beam_centre': tutorial_centre.model_dump(),
        },
        blanks=('sample', 'sample_transmission'),
    )
    stage = client.stage(template)
    records = {
        run: client.submit(
            stage,
            params={
                'sample': file(run=run),
                'sample_transmission': file(run=60394),
            },
        )
        for run in (60339, 60250)
    }
    client.wait(list(records.values()))
    plain = client.compute(
        template.fill(
            params={
                'sample': file(run=60250),
                'sample_transmission': file(run=60394),
            }
        )
    )
    assert sc.identical(
        client.output(records[60250], 'iofq').intensity,
        client.output(plain, 'iofq').intensity,
    )


def test_a_run_of_another_role_is_refused(
    client: Client, mask_file: Path, iofq_settings: dict[str, object]
) -> None:
    masks = client.submit(
        MASKS,
        params={'run': file(run=60339), 'mask_files': [file(path=mask_file)]},
    )
    with pytest.raises(SubmitError, match='does not fit the field'):
        client.submit(
            IOFQ,
            params={
                **iofq_settings,
                'sample': file(run=60393),
                'sample_transmission': file(run=60394),
                'masks': masks.ref('masks'),
                'beam_centre': {'x': 0.0, 'y': 0.0, 'z': 1.0},
            },
        )


def test_a_typed_beam_centre_without_a_distance_is_refused_at_submit(
    client: Client, mask_file: Path, iofq_settings: dict[str, object]
) -> None:
    masks = client.submit(
        MASKS,
        params={'run': file(run=60339), 'mask_files': [file(path=mask_file)]},
    )
    with pytest.raises(SubmitError, match='beam_centre'):
        client.submit(
            IOFQ,
            params={
                **iofq_settings,
                'sample': file(run=60339),
                'sample_transmission': file(run=60394),
                'masks': masks.ref('masks'),
                'beam_centre': {'x': 0.01, 'y': 0.01, 'z': 0.0},
            },
        )
