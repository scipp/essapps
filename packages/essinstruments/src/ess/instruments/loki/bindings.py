# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The bindings of LoKI's specs over esssans, for the LoKI detector test at Larmor.

The detector, the masks, and the beam centre are functions, since esssans builds its
masks by changing a pipeline, and its beam-centre finder takes a pipeline. Those that
read the detector give esssans what the detector holds in place of the file. I(Q) is a
pipeline, so that it serves stages. Its masks are a value given to the pipeline,
which replaces the provider of esssans that reads them from a file and the
sample run.
"""

from collections.abc import Mapping
from functools import reduce
from operator import or_
from pathlib import Path
from typing import Any

import sciline
import scipp as sc
import scippnexus as snx
from ess import sans
from ess.loki.larmor_workflow import LokiAtLarmorWorkflow
from ess.sans.types import (
    BackgroundRun,
    BackgroundSubtractedIofQ,
    BeamCenter,
    CorrectedDetector,
    CorrectForGravity,
    DetectorIDs,
    DetectorMasks,
    DirectBeam,
    DirectBeamFilename,
    EmptyBeamRun,
    Filename,
    Measurement,
    Numerator,
    Position,
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
    CALIBRATED_DETECTOR,
    CREATE_MASK,
    IOFQ,
    MASKS,
    BeamCentre,
    BeamCentreOutputs,
    BeamCentreParams,
    CalibratedDetectorOutputs,
    CalibratedDetectorParams,
    Corrections,
    CreateMaskParams,
    DetectorImage,
    IofQ,
    MasksOutputs,
    MasksParams,
    PixelMasks,
    TransmissionCurves,
)

_NO_CORRECTION = sc.vector([0.0, 0.0, 0.0], unit='m')
"""The value of esssans' key that applies no beam-centre correction."""

_ANGLE_RANGES = 'angle ranges'
"""The name of the mask of the angle ranges of a record."""


def _run_workflow(run: Path) -> sciline.Pipeline:
    """The workflow of the detector of one run, with no beam-centre correction."""
    workflow = LokiAtLarmorWorkflow()
    workflow[Filename[SampleRun]] = str(run)
    workflow[BeamCenter] = _NO_CORRECTION
    return workflow


def load_detector(params: CalibratedDetectorParams) -> CalibratedDetectorOutputs:
    workflow = _run_workflow(params.run)
    workflow[DetectorMasks] = {}
    detector = CorrectedDetector[SampleRun, Numerator]
    sample = Position[snx.NXsample, SampleRun]
    source = Position[snx.NXsource, SampleRun]
    values = workflow.compute((detector, sample, source))
    counts = values[detector].bins.sum()
    return CalibratedDetectorOutputs(
        detector=DetectorImage(
            counts=counts.assign_coords(
                sample_position=values[sample], source_position=values[source]
            )
        )
    )


def compute_masks(params: MasksParams) -> MasksOutputs:
    names = [path.name for path in params.mask_files]
    if len(set(names)) < len(names):
        # Masks are named by file, so that one mask would replace the other.
        raise ValueError(f'mask files with the same name: {sorted(names)}')
    masks: dict[str, sc.Variable] = {}
    if params.mask_files:
        workflow = LokiAtLarmorWorkflow()
        workflow[DetectorIDs] = params.detector.counts.coords['detector_number']
        # esssans names each mask by the path of its file, which differs by host.
        by_path = sans.with_pixel_mask_filenames(
            workflow, [str(path) for path in params.mask_files]
        ).compute(DetectorMasks)
        masks = {Path(path).name: mask for path, mask in by_path.items()}
    return MasksOutputs(masks=PixelMasks(masks=masks))


def find_beam_centre(params: BeamCentreParams) -> BeamCentreOutputs:
    counts = params.detector.counts.assign_masks(params.masks.masks)
    # The finder raises its cutoff until pixels are left, so without any it never ends.
    if not counts.sum().value > 0:
        raise ValueError('no counts outside the masks to find a beam centre from')
    workflow = LokiAtLarmorWorkflow()
    workflow[Position[snx.NXsample, SampleRun]] = counts.coords['sample_position']
    workflow[Position[snx.NXsource, SampleRun]] = counts.coords['source_position']
    # The finder sums the dimensions that the positions lack, and else expects events.
    workflow[CorrectedDetector[SampleRun, Numerator]] = sc.concat([counts], 'summed')
    centre = sans.beam_center_from_center_of_mass(workflow).to(unit='m')
    return BeamCentreOutputs(
        centre=BeamCentre(
            x=centre.fields.x.value, y=centre.fields.y.value, z=centre.fields.z.value
        )
    )


def _angle_to_beam(counts: sc.DataArray, centre: BeamCentre) -> sc.Variable:
    """The angle of each pixel to the beam, seen from the sample, in degrees."""
    to_pixel = counts.coords['position'] - counts.coords['sample_position']
    beam = sc.vector([centre.x, centre.y, centre.z], unit='m')
    angle = sc.atan2(y=sc.norm(sc.cross(beam, to_pixel)), x=sc.dot(beam, to_pixel))
    return angle.to(unit='deg')


def create_mask(params: CreateMaskParams) -> MasksOutputs:
    angle = _angle_to_beam(params.detector.counts, params.beam_centre)
    mask = reduce(
        or_,
        (
            (angle >= sc.scalar(s.start, unit='deg'))
            & (angle < sc.scalar(s.stop, unit='deg'))
            for s in params.shapes
        ),
    )
    base = {} if params.base is None else params.base.masks
    masks = {name: value.copy() for name, value in base.items()}
    if _ANGLE_RANGES in masks:
        mask = mask | masks[_ANGLE_RANGES]
    masks[_ANGLE_RANGES] = mask
    return MasksOutputs(masks=PixelMasks(masks=masks))


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


def _iofq(
    intensity: BackgroundSubtractedIofQ, measurement: Measurement[SampleRun]
) -> IofQ:
    return IofQ(
        intensity=intensity, title=measurement.title, run=measurement.run_number
    )


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


detector_binding = function_binding(CALIBRATED_DETECTOR, load_detector)
masks_binding = function_binding(MASKS, compute_masks)
beam_centre_binding = function_binding(BEAM_CENTRE, find_beam_centre)
create_mask_binding = function_binding(CREATE_MASK, create_mask)
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

BINDINGS = [
    detector_binding,
    masks_binding,
    beam_centre_binding,
    create_mask_binding,
    iofq_binding,
]
