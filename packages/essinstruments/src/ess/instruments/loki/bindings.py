# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The bindings of LoKI's specs over esssans, for the LoKI detector test at Larmor.

The masks and the beam centre are functions, since esssans builds its masks by
changing a pipeline, and its beam-centre finder takes a pipeline. I(Q) is a
pipeline, so that it serves stages. Its masks are a value given to the pipeline,
which replaces the provider of esssans that reads them from a file and the
sample run.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import sciline
import scipp as sc
from ess import sans
from ess.loki.larmor_workflow import LokiAtLarmorWorkflow
from ess.sans.types import (
    BackgroundRun,
    BackgroundSubtractedIofQ,
    BeamCenter,
    CorrectedDetector,
    CorrectForGravity,
    DetectorMasks,
    DirectBeam,
    DirectBeamFilename,
    EmptyBeamRun,
    Filename,
    Numerator,
    QBins,
    ReturnEvents,
    SampleRun,
    TransmissionFraction,
    TransmissionRun,
    UncertaintyBroadcastMode,
    WavelengthBins,
)

from ess.spec import function_binding
from ess.spec.conversions import edges_to_variable
from ess.spec.parameters import QEdges, WavelengthEdges
from ess.spec.pipeline import PipelineBinding

from .specs import (
    BEAM_CENTRE,
    IOFQ,
    MASKS,
    BeamCentre,
    BeamCentreOutputs,
    BeamCentreParams,
    Corrections,
    DetectorImage,
    IofQ,
    MasksOutputs,
    MasksParams,
    PixelMasks,
    TransmissionCurves,
)

_NO_CORRECTION = sc.vector([0.0, 0.0, 0.0], unit='m')
"""The value of esssans' key that applies no beam-centre correction."""


def _run_workflow(run: Path) -> sciline.Pipeline:
    """The workflow of the detector of one run, with no beam-centre correction."""
    workflow = LokiAtLarmorWorkflow()
    workflow[Filename[SampleRun]] = str(run)
    workflow[BeamCenter] = _NO_CORRECTION
    return workflow


def _image(detector: sc.DataArray) -> DetectorImage:
    return DetectorImage(counts=detector.bins.sum())


def compute_masks(params: MasksParams) -> MasksOutputs:
    workflow = _run_workflow(params.run)
    masks: dict[str, sc.Variable] = {}
    if params.mask_files:
        # esssans names each mask by the path of its file, which differs by host.
        by_path = sans.with_pixel_mask_filenames(
            workflow, [str(path) for path in params.mask_files]
        ).compute(DetectorMasks)
        masks = {Path(path).name: mask for path, mask in by_path.items()}
    workflow[DetectorMasks] = masks
    detector = workflow.compute(CorrectedDetector[SampleRun, Numerator])
    return MasksOutputs(masks=PixelMasks(masks=masks), image=_image(detector))


def find_beam_centre(params: BeamCentreParams) -> BeamCentreOutputs:
    workflow = _run_workflow(params.sample)
    workflow[DetectorMasks] = params.masks.masks
    key = CorrectedDetector[SampleRun, Numerator]
    detector = workflow.compute(key)
    workflow[key] = detector  # so that the finder does not load the run again
    centre = sans.beam_center_from_center_of_mass(workflow).to(unit='m')
    return BeamCentreOutputs(
        centre=BeamCentre(
            x=centre.fields.x.value, y=centre.fields.y.value, z=centre.fields.z.value
        ),
        image=_image(detector),
    )


def _detector_masks(masks: PixelMasks) -> DetectorMasks:
    return DetectorMasks(masks.masks)


def _beam_center(centre: BeamCentre) -> BeamCenter:
    return BeamCenter(sc.vector([centre.x, centre.y, centre.z], unit='m'))


def _wavelength_bins(edges: WavelengthEdges) -> WavelengthBins:
    return WavelengthBins(edges_to_variable(edges, dim='wavelength'))


def _q_bins(edges: QEdges) -> QBins:
    return QBins(edges_to_variable(edges, dim='Q'))


def _correct_for_gravity(corrections: Corrections) -> CorrectForGravity:
    return CorrectForGravity(corrections.gravity)


def _uncertainties(corrections: Corrections) -> UncertaintyBroadcastMode:
    return UncertaintyBroadcastMode[corrections.uncertainties]


def _iofq(intensity: BackgroundSubtractedIofQ) -> IofQ:
    return IofQ(intensity=intensity)


def _transmission(
    sample: TransmissionFraction[SampleRun],
    can: TransmissionFraction[BackgroundRun],
) -> TransmissionCurves:
    return TransmissionCurves(sample=sample, can=can)


def _iofq_pipeline(fixed: Mapping[str, Any]) -> sciline.Pipeline:
    pipeline = LokiAtLarmorWorkflow()
    for provider in (
        _detector_masks,
        _beam_center,
        _wavelength_bins,
        _q_bins,
        _correct_for_gravity,
        _uncertainties,
        _iofq,
        _transmission,
    ):
        pipeline.insert(provider)
    pipeline[ReturnEvents] = False
    if (direct_beam := fixed['direct_beam']) is None:
        pipeline[DirectBeam] = None
    else:
        pipeline[DirectBeamFilename] = str(direct_beam)
    return pipeline


masks_binding = function_binding(MASKS, compute_masks)
beam_centre_binding = function_binding(BEAM_CENTRE, find_beam_centre)
iofq_binding = PipelineBinding(
    IOFQ,
    _iofq_pipeline,
    keys={
        'sample': (Filename[SampleRun],),
        'sample_transmission': (Filename[TransmissionRun[SampleRun]],),
        'can': (Filename[BackgroundRun],),
        'can_transmission': (Filename[TransmissionRun[BackgroundRun]],),
        'empty_beam': (Filename[EmptyBeamRun],),
        'direct_beam': (),
    },
)

BINDINGS = [masks_binding, beam_centre_binding, iofq_binding]
