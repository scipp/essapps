# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The rows of a batch and the latest result of each, over a client whose backend
runs fakes of LoKI's specs, so that no tutorial file is read.
"""

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import scipp as sc
from ess.instruments.loki.batch import Row, latest_results, rows
from ess.instruments.loki.specs import (
    BEAM_CENTRE,
    IOFQ,
    MASKS,
    BeamCentre,
    BeamCentreOutputs,
    DetectorImage,
    IofQ,
    IofQOutputs,
    MasksOutputs,
    PixelMasks,
    TransmissionCurves,
)

from ess.apps import Client, Listing, Record, local
from ess.apps.testing import FakeFiles
from ess.spec import FileRef, function_binding


def curve() -> sc.DataArray:
    return sc.DataArray(sc.ones(dims=['Q'], shape=[3], unit='counts'))


def fake_masks(params: Any) -> MasksOutputs:
    return MasksOutputs(masks=PixelMasks(masks={}), image=DetectorImage(counts=curve()))


def fake_centre(params: Any) -> BeamCentreOutputs:
    return BeamCentreOutputs(
        centre=BeamCentre(x=0.0, y=0.0, z=1.0), image=DetectorImage(counts=curve())
    )


def fake_iofq(params: Any) -> IofQOutputs:
    if params.q.num_bins == 1:
        raise ZeroDivisionError('one bin')
    return IofQOutputs(
        iofq=IofQ(intensity=curve()),
        transmission=TransmissionCurves(sample=curve(), can=curve()),
    )


@pytest.fixture
def files(tmp_path: Path) -> FakeFiles:
    return FakeFiles('p1', tmp_path)


@pytest.fixture
def client(files: FakeFiles) -> Iterator[Client]:
    bind = [
        function_binding(MASKS, fake_masks),
        function_binding(BEAM_CENTRE, fake_centre),
        function_binding(IOFQ, fake_iofq),
    ]
    with local(proposal='p1', files=files, bind=bind) as client:
        yield client


def measure(
    files: FakeFiles, run: int, role: str, sample: str | None = None
) -> FileRef:
    named = {} if sample is None else {'sample': sample}
    return files.measure(run, {}, role=role, **named)


def test_a_sample_run_is_paired_with_the_transmission_run_of_its_sample(
    files: FakeFiles, client: Client
) -> None:
    water = measure(files, 1, 'sample', 'water')
    sds = measure(files, 2, 'sample', 'sds')
    sds_transmission = measure(files, 3, 'transmission', 'sds')
    water_transmission = measure(files, 4, 'transmission', 'water')

    assert rows(client.files) == [
        Row('water', water, water_transmission),
        Row('sds', sds, sds_transmission),
    ]


def test_the_last_measured_of_several_transmission_runs_is_the_rows(
    files: FakeFiles, client: Client
) -> None:
    water = measure(files, 1, 'sample', 'water')
    measure(files, 2, 'transmission', 'water')
    last = measure(files, 3, 'transmission', 'water')
    measure(files, 4, 'transmission', 'sds')

    assert rows(client.files) == [Row('water', water, last)]


def test_a_sample_measured_twice_gives_rows_with_different_names(
    files: FakeFiles, client: Client
) -> None:
    first = measure(files, 1, 'sample', 'water')
    second = measure(files, 2, 'sample', 'water')
    transmission = measure(files, 3, 'transmission', 'water')

    assert rows(client.files) == [
        Row('water (1)', first, transmission),
        Row('water (2)', second, transmission),
    ]


def test_a_sample_run_without_a_transmission_run_is_a_row_without_one(
    files: FakeFiles, client: Client
) -> None:
    water = measure(files, 1, 'sample', 'water')
    measure(files, 2, 'transmission', 'sds')

    assert rows(client.files) == [Row('water', water, None)]


def test_a_sample_run_without_a_sample_name_is_named_by_its_run_number(
    files: FakeFiles, client: Client
) -> None:
    unnamed = measure(files, 7, 'sample')
    measure(files, 8, 'transmission')  # named by nothing, so it pairs with nothing

    assert rows(client.files) == [Row('7', unnamed, None)]


def test_only_sample_runs_are_rows(files: FakeFiles, client: Client) -> None:
    measure(files, 1, 'background', 'water')
    measure(files, 2, 'transmission', 'water')
    files.measure(3, {}, kind='mask', sample='water')

    assert rows(client.files) == []


@pytest.fixture
def settings(files: FakeFiles, client: Client) -> dict[str, Any]:
    masks = client.compute(
        MASKS, params={'run': measure(files, 90, 'sample'), 'mask_files': []}
    )
    return {
        'can': measure(files, 91, 'background'),
        'can_transmission': measure(files, 92, 'transmission'),
        'empty_beam': measure(files, 92, 'transmission'),
        'direct_beam': None,
        'masks': masks.ref('masks'),
        'beam_centre': {'x': 0.0, 'y': 0.0, 'z': 1.0},
        'wavelength': {'start': 1.0, 'stop': 13.0, 'num_bins': 2},
        'q': {'start': 0.01, 'stop': 0.3, 'num_bins': 2},
    }


def reduce(
    client: Client, settings: dict[str, Any], sample: FileRef, transmission: FileRef
) -> Record:
    params = {**settings, 'sample': sample, 'sample_transmission': transmission}
    return client.compute(IOFQ, params=params)


def test_the_latest_result_of_a_sample_run_is_its_newest_record(
    files: FakeFiles, client: Client, settings: dict[str, Any]
) -> None:
    water = measure(files, 1, 'sample', 'water')
    sds = measure(files, 2, 'sample', 'sds')
    transmission = measure(files, 3, 'transmission', 'water')
    first = reduce(client, settings, water, transmission)
    other = reduce(client, settings, sds, transmission)
    assert latest_results(Listing(client)) == {water: first, sds: other}

    second = reduce(client, settings, water, transmission)

    assert latest_results(Listing(client)) == {water: second, sds: other}


def test_a_newer_record_that_failed_is_the_latest_result(
    files: FakeFiles, client: Client, settings: dict[str, Any]
) -> None:
    water = measure(files, 1, 'sample', 'water')
    transmission = measure(files, 3, 'transmission', 'water')
    reduce(client, settings, water, transmission)
    one_bin = {**settings, 'q': {'start': 0.01, 'stop': 0.3, 'num_bins': 1}}

    failed = reduce(client, one_bin, water, transmission)

    assert client.failure(failed) is not None
    assert latest_results(Listing(client)) == {water: failed}


def test_a_record_of_another_spec_is_not_a_result(
    files: FakeFiles, client: Client, settings: dict[str, Any]
) -> None:
    water = measure(files, 1, 'sample', 'water')
    transmission = measure(files, 3, 'transmission', 'water')
    result = reduce(client, settings, water, transmission)
    client.compute(BEAM_CENTRE, params={'sample': water, 'masks': settings['masks']})

    assert latest_results(Listing(client)) == {water: result}


def test_the_batch_module_needs_neither_panel_nor_plopp() -> None:
    code = (
        'import sys, ess.instruments.loki.batch;'
        "sys.exit(any(m.split('.')[0] in ('panel', 'plopp') for m in sys.modules))"
    )
    result = subprocess.run([sys.executable, '-c', code], check=False)  # noqa: S603
    assert result.returncode == 0
