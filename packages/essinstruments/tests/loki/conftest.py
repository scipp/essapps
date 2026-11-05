# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from pathlib import Path

import pytest
import scipp as sc
from ess import sans
from ess.instruments.loki.specs import BeamCentre, Corrections
from ess.loki import data
from ess.loki.larmor_workflow import LokiAtLarmorWorkflow
from ess.sans.types import (
    BackgroundRun,
    BackgroundSubtractedIofQ,
    BeamCenter,
    CorrectForGravity,
    DirectBeamFilename,
    EmptyBeamRun,
    Filename,
    QBins,
    ReturnEvents,
    SampleRun,
    TransmissionRun,
    UncertaintyBroadcastMode,
    WavelengthBins,
)

from ess.spec.parameters import QEdges, WavelengthEdges


@pytest.fixture(scope='session')
def tutorial_centre() -> BeamCentre:
    """The beam centre that esssans' tutorial workflow holds for run 60339."""
    return BeamCentre(x=-0.02914868, y=-0.01816138)


@pytest.fixture(scope='session')
def sample() -> Path:
    return Path(data.loki_tutorial_sample_run_60339())


@pytest.fixture(scope='session')
def other_sample() -> Path:
    return Path(data.loki_tutorial_sample_run_60250())


@pytest.fixture(scope='session')
def mask_file() -> Path:
    return Path(data.loki_tutorial_mask_filenames()[0])


@pytest.fixture(scope='session')
def iofq_values(tutorial_centre: BeamCentre) -> dict[str, object]:
    """The values of I(Q) other than its masks and direct beam, as esssans' tutorial."""
    return {
        'sample': Path(data.loki_tutorial_sample_run_60339()),
        'sample_transmission': Path(data.loki_tutorial_sample_transmission_run()),
        'can': Path(data.loki_tutorial_background_run_60393()),
        'can_transmission': Path(data.loki_tutorial_run_60392()),
        'empty_beam': Path(data.loki_tutorial_run_60392()),
        'beam_centre': tutorial_centre,
        'wavelength': WavelengthEdges(start=1.0, stop=13.0, num_bins=200),
        'q': QEdges(start=0.01, stop=0.3, num_bins=100),
        'corrections': Corrections(gravity=True, uncertainties='upper_bound'),
    }


@pytest.fixture(scope='session')
def direct_beam() -> Path:
    return Path(data.loki_tutorial_direct_beam_all_pixels())


@pytest.fixture(scope='session')
def plain_esssans_iofq(
    mask_file: Path, direct_beam: Path, tutorial_centre: BeamCentre
) -> sc.DataArray:
    """I(Q) as esssans computes it, reading the masks from their files."""
    workflow = LokiAtLarmorWorkflow()
    workflow[WavelengthBins] = sc.linspace(
        'wavelength', 1.0, 13.0, 201, unit='angstrom'
    )
    workflow[QBins] = sc.linspace('Q', 0.01, 0.3, 101, unit='1/angstrom')
    workflow[CorrectForGravity] = True
    workflow[UncertaintyBroadcastMode] = UncertaintyBroadcastMode.upper_bound
    workflow[ReturnEvents] = False
    workflow[DirectBeamFilename] = str(direct_beam)
    workflow[BeamCenter] = sc.vector(
        [tutorial_centre.x, tutorial_centre.y, 0.0], unit='m'
    )
    workflow[Filename[SampleRun]] = str(data.loki_tutorial_sample_run_60339())
    workflow[Filename[BackgroundRun]] = str(data.loki_tutorial_background_run_60393())
    workflow[Filename[TransmissionRun[SampleRun]]] = str(
        data.loki_tutorial_sample_transmission_run()
    )
    workflow[Filename[TransmissionRun[BackgroundRun]]] = str(
        data.loki_tutorial_run_60392()
    )
    workflow[Filename[EmptyBeamRun]] = str(data.loki_tutorial_run_60392())
    workflow = sans.with_pixel_mask_filenames(workflow, [str(mask_file)])
    return workflow.compute(BackgroundSubtractedIofQ)
