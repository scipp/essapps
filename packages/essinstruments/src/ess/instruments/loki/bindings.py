# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The bindings of LoKI's specs over esssans, for the LoKI detector test at Larmor.

The detector, the masks, and the beam centre are function bindings. The masks and
the beam centre read the detector, and esssans' beam-centre finder accepts a whole
workflow. The functions that read the detector set what it holds in esssans'
workflow, in place of the file.

I(Q) is a pipeline binding, so that it can run in stages. Its masks are a value set
in the pipeline. This replaces esssans' provider that reads them from mask files and
the sample run.

The bank histogram reads the events of a raw ESS file directly, without esssans,
so that it works on files that esssans cannot read yet.
"""

from collections.abc import Mapping
from functools import cache, reduce
from operator import or_
from pathlib import Path
from typing import Any

import numpy as np
import sciline
import scipp as sc
import scippnexus as snx
from ess import sans
from ess.loki.larmor_workflow import LokiAtLarmorWorkflow
from ess.reduce.nexus import open_nexus_file
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
    EmptyDetector,
    Filename,
    Measurement,
    NeutronSourcePosition,
    Numerator,
    PixelMaskFilenames,
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
    BANK_HISTOGRAM,
    BEAM_CENTRE,
    CALIBRATED_DETECTOR,
    CREATE_MASK,
    IOFQ,
    MASKS,
    BankHistogramOutputs,
    BankHistogramParams,
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


@cache
def _base_workflow() -> sciline.Pipeline:
    """The workflow of esssans, built once per process. Requests change a copy."""
    # Building it is slow: sciline inserts each generic key, about 1000 here, as a
    # node of a networkx graph, and rebuilds the graph at each assignment. If that
    # becomes cheap, build a workflow per request instead.
    return LokiAtLarmorWorkflow()


def _workflow() -> sciline.Pipeline:
    """A copy of the workflow of esssans, for one request to change."""
    return _base_workflow().copy()


def _run_workflow(run: Path) -> sciline.Pipeline:
    """The workflow of the detector of one run, with no beam-centre correction."""
    workflow = _workflow()
    workflow[Filename[SampleRun]] = str(run)
    workflow[BeamCenter] = _NO_CORRECTION
    return workflow


def load_detector(params: CalibratedDetectorParams) -> CalibratedDetectorOutputs:
    workflow = _run_workflow(params.run)
    workflow[DetectorMasks] = {}
    detector = CorrectedDetector[SampleRun, Numerator]
    sample = Position[snx.NXsample, SampleRun]
    source = NeutronSourcePosition[SampleRun]
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
    masks: dict[str, sc.Variable] = {}
    if params.mask_files:
        # esssans names a mask of detector IDs by its file name. It raises for two
        # files of the same name, since one mask would replace the other.
        workflow = _workflow()
        workflow[EmptyDetector[SampleRun]] = params.detector.counts
        workflow[PixelMaskFilenames] = [str(path) for path in params.mask_files]
        masks = dict(workflow.compute(DetectorMasks))
    return MasksOutputs(masks=PixelMasks(masks=masks))


def find_beam_centre(params: BeamCentreParams) -> BeamCentreOutputs:
    counts = params.detector.counts.assign_masks(params.masks.masks)
    # The finder raises its cutoff until pixels are left, so without any it never ends.
    if not counts.sum().value > 0:
        raise ValueError('no counts outside the masks to find a beam centre from')
    workflow = _workflow()
    workflow[Position[snx.NXsample, SampleRun]] = counts.coords['sample_position']
    workflow[NeutronSourcePosition[SampleRun]] = counts.coords['source_position']
    # The finder sums the dimensions that the positions lack. Otherwise it expects
    # events.
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
    pipeline = _workflow()
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


def histogram_bank(params: BankHistogramParams) -> BankHistogramOutputs:
    """
    Returns the events of ``loki_detector_<bank>`` histogrammed by
    ``event_time_offset``.

    ``bins`` bins of equal width span the earliest to the latest event. The
    variance of each count equals the count.

    Raises
    ------
    ValueError
        If the file has no such bank, or the bank has no events.
    """
    name = 'event_time_offset'
    path = f'entry/instrument/loki_detector_{params.bank}/detector_events/{name}'
    # As esssans does: open_nexus_file skips locking on a read-only file system,
    # such as that of raw files at ESS.
    with open_nexus_file(params.run) as f:
        if path not in f:
            raise ValueError(f'{params.run.name} has no {path}')
        offsets = f[path][()]
    if not offsets.size:
        raise ValueError(f'bank {params.bank} of {params.run.name} holds no events')
    counts, edges = np.histogram(offsets.values, bins=params.bins)
    counts = counts.astype(np.float64)
    histogram = sc.DataArray(
        sc.array(dims=[name], values=counts, variances=counts, unit='counts'),
        coords={name: sc.array(dims=[name], values=edges, unit=offsets.unit)},
    )
    return BankHistogramOutputs(histogram=histogram)


detector_binding = function_binding(CALIBRATED_DETECTOR, load_detector)
masks_binding = function_binding(MASKS, compute_masks)
beam_centre_binding = function_binding(BEAM_CENTRE, find_beam_centre)
create_mask_binding = function_binding(CREATE_MASK, create_mask)
bank_histogram_binding = function_binding(BANK_HISTOGRAM, histogram_bank)
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
    bank_histogram_binding,
]
