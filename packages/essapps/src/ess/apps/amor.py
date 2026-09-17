# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Amor reflectometry bound to the spec vocabulary: R(Q) per angle, then a stitch.

This binding exists to probe two things the LoKI binding does not reach.

*Collections on both sides.* The combine takes a dict of curve references, one
per sample rotation, and returns a dict of scaled curves alongside the stitched
one. An element of that dict is referenced by key, so a curve can be fed into a
second combine without the first being read whole.

*A combine that is not additive.* Reflectivity curves measured at different
angles do not add: the scale factors come from a fit over the whole set, and the
stitch is a variance-weighted mean on a common Q grid. The sketch says such a
combine stays an ordinary workflow over member outputs (D15), and that is what
``amor-combine`` is: a plain callable with a collection-valued parameter, no
contribution, and no ``contribute``/``combine``/``finalize``.

The workflow is the one of the ``amor-reduction`` tutorial in essreflectometry.
The tutorial's ``batch_compute(..., scale_to_overlap=...)`` does in one call
what three requests do here: reduce each run, fit the scale factors over all
curves, and apply the factor to each run. The last step is expressible because
``scale_factors`` is a literal collection output and ``scale_factor`` is a
parameter of the member spec that may hold a reference to one element of it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, NewType

import scipp as sc
from ess import amor
from ess.amor import data
from ess.amor.types import ChopperPhase
from ess.reduce.spec.parameters import QEdges, Scale, WavelengthEdges
from ess.reflectometry.tools import combine_curves, scale_for_reflectivity_overlap
from ess.reflectometry.types import (
    BeamDivergenceLimits,
    DetectorRotation,
    Filename,
    QBins,
    ReferenceRun,
    ReflectivityOverQ,
    RunType,
    SampleRotation,
    SampleRotationOffset,
    SampleRun,
    SampleSize,
    WavelengthBins,
    YIndexLimits,
    ZIndexLimits,
)
from pydantic import BaseModel, Field, model_validator

from .binding import Inputs, Registry, resolve
from .spec import Array, ArraySpec, NexusFile, OutputRef, WorkflowSpec
from .warm import WarmPipeline

CURVE = ArraySpec(
    dims=('Q',),
    unit='dimensionless',
    coords={'Q': '1/Å', 'Q_resolution': '1/Å'},
)
"""A reflectivity curve: what a member produces and what the combine consumes."""


# Parameter models the spec vocabulary does not have.


class PixelRange(BaseModel, frozen=True):
    """
    Lower and upper detector pixel index of the region holding signal.

    ``ess.reduce.spec.parameters`` has ranges in physical units only, so the
    index ranges Amor masks on have no vocabulary item to reuse.
    """

    low: int = Field(ge=0)
    high: int = Field(ge=0)

    @model_validator(mode='after')
    def _ordered(self) -> PixelRange:
        if self.high <= self.low:
            raise ValueError('high must be greater than low')
        return self

    @property
    def limits(self) -> tuple[sc.Variable, sc.Variable]:
        return sc.scalar(self.low), sc.scalar(self.high)


class AngleRange(BaseModel, frozen=True):
    """An angular range in degrees; ``RangeModel`` has no angle subclass."""

    start: float
    stop: float

    @model_validator(mode='after')
    def _ordered(self) -> AngleRange:
        if self.stop <= self.start:
            raise ValueError('stop must be greater than start')
        return self

    @property
    def limits(self) -> tuple[sc.Variable, sc.Variable]:
        return sc.scalar(self.start, unit='deg'), sc.scalar(self.stop, unit='deg')


class QRange(BaseModel, frozen=True):
    """A Q interval, for the critical edge the scaling is anchored on."""

    start: float = Field(gt=0.0)
    stop: float = Field(gt=0.0)

    @model_validator(mode='after')
    def _ordered(self) -> QRange:
        if self.stop <= self.start:
            raise ValueError('stop must be greater than start')
        return self

    @property
    def interval(self) -> tuple[sc.Variable, sc.Variable]:
        unit = '1/angstrom'
        return sc.scalar(self.start, unit=unit), sc.scalar(self.stop, unit=unit)


def _edges(model: QEdges | WavelengthEdges, dim: str) -> sc.Variable:
    """Bin edges from the spec's edges model."""
    space = sc.geomspace if model.scale == Scale.LOG else sc.linspace
    return space(
        dim, model.start, model.stop, model.num_bins + 1, unit=model.unit.value
    )


# R(Q) of one sample run

WavelengthGrid = NewType('WavelengthGrid', WavelengthEdges)
DetectorYRange = NewType('DetectorYRange', PixelRange)
DetectorZRange = NewType('DetectorZRange', PixelRange)
DivergenceRange = NewType('DivergenceRange', AngleRange)
RotationOffset = NewType('RotationOffset', float)
PhaseOffset = NewType('PhaseOffset', float)
SampleWidth = NewType('SampleWidth', float)
QNumBins = NewType('QNumBins', int)
ScaleFactor = NewType('ScaleFactor', float)
ReflectivityCurve = NewType('ReflectivityCurve', sc.DataArray)


def wavelength_bins(grid: WavelengthGrid) -> WavelengthBins:
    return WavelengthBins(_edges(grid, 'wavelength'))


def y_index_limits(y: DetectorYRange) -> YIndexLimits:
    return YIndexLimits(y.limits)


def z_index_limits(z: DetectorZRange) -> ZIndexLimits:
    return ZIndexLimits(z.limits)


def beam_divergence_limits(angles: DivergenceRange) -> BeamDivergenceLimits:
    return BeamDivergenceLimits(angles.limits)


def sample_rotation_offset(offset: RotationOffset) -> SampleRotationOffset[RunType]:
    """The tutorial's correction of the angle written in the file, for both runs."""
    return SampleRotationOffset[RunType](sc.scalar(offset, unit='deg'))


def chopper_phase(phase: PhaseOffset) -> ChopperPhase[RunType]:
    return ChopperPhase[RunType](sc.scalar(phase, unit='deg'))


def sample_size(width: SampleWidth) -> SampleSize[RunType]:
    return SampleSize[RunType](sc.scalar(width, unit='mm'))


def q_bins(
    detector_rotation: DetectorRotation[SampleRun],
    sample_rotation: SampleRotation[SampleRun],
    wavelength: WavelengthBins,
    divergence: BeamDivergenceLimits,
    num_bins: QNumBins,
) -> QBins:
    """
    The Q grid of this run, with only the bin count taken from the request.

    ``ess.amor.utils.qgrid`` derives the accessible Q range from the geometry of
    the run, and the ranges of two sample rotations differ, which is what makes
    stitching necessary. A request that set the edges itself would put every
    angle on one grid and destroy that, so the parameter is the bin count and
    the range stays derived.
    """
    grid = amor.utils.qgrid(detector_rotation, sample_rotation, wavelength, divergence)
    return QBins(sc.geomspace('Q', grid[0], grid[-1], num_bins + 1))


def reflectivity_curve(
    reflectivity: ReflectivityOverQ, scale: ScaleFactor
) -> ReflectivityCurve:
    """
    The histogram of R(Q), scaled.

    ``ReflectivityOverQ`` is binned events, and the scaling fit and the stitch
    both work on histograms, so the member's output is the histogram: an
    ``ArraySpec`` with ``binned=False`` is the only way a spec can say which of
    the two a downstream parameter needs.
    """
    return ReflectivityCurve(reflectivity.hist() * sc.scalar(scale))


class ReflectivityParams(BaseModel):
    sample_run: NexusFile
    reference_run: NexusFile
    rotation_offset: float = 0.05
    chopper_phase: float = -7.5
    sample_size: float = 10.0
    wavelength: WavelengthEdges = WavelengthEdges(
        start=2.8, stop=12.5, num_bins=2000, scale=Scale.LOG
    )
    y_index_limits: PixelRange = PixelRange(low=11, high=41)
    z_index_limits: PixelRange = PixelRange(low=80, high=370)
    beam_divergence: AngleRange = AngleRange(start=-0.75, stop=0.75)
    q_num_bins: int = Field(default=500, ge=1)
    scale_factor: float | OutputRef = 1.0


class ReflectivityOutputs(BaseModel):
    reflectivity: Array(CURVE)


def reflectivity_workflow() -> WarmPipeline:
    """
    One sample run against one reference run.

    ``sample_run`` is a stage input rather than a held parameter, which is what
    makes a series of rotations affordable: the reduced reference depends on the
    reference run alone, so it sits at the stage's frontier and the 120 MB
    supermirror measurement is reduced once per session no matter how many
    sample runs follow. Held instead, it would be reduced again for every member,
    because a stage holds one frontier and a change to any held parameter
    discards it. Across processes it is reduced per run either way; making that
    reuse explicit would need a second spec whose output is the reduced
    reference, chained as LoKI's beam centre is.
    """
    pipeline = amor.AmorWorkflow()
    for provider in (
        wavelength_bins,
        y_index_limits,
        z_index_limits,
        beam_divergence_limits,
        sample_rotation_offset,
        chopper_phase,
        sample_size,
        q_bins,
        reflectivity_curve,
    ):
        pipeline.insert(provider)
    return WarmPipeline(
        pipeline,
        keys={
            'sample_run': Filename[SampleRun],
            'reference_run': Filename[ReferenceRun],
            'rotation_offset': RotationOffset,
            'chopper_phase': PhaseOffset,
            'sample_size': SampleWidth,
            'wavelength': WavelengthGrid,
            'y_index_limits': DetectorYRange,
            'z_index_limits': DetectorZRange,
            'beam_divergence': DivergenceRange,
            'q_num_bins': QNumBins,
            'scale_factor': ScaleFactor,
        },
        resolve={'sample_run': 'path', 'reference_run': 'path'},
        targets={'reflectivity': ReflectivityCurve},
        stage_inputs=['sample_run', 'q_num_bins', 'scale_factor'],
    )


REFLECTIVITY = WorkflowSpec(
    name='amor-reflectivity',
    version=1,
    title='Amor reflectivity',
    description=(
        'R(Q) of one Amor sample run normalised by a supermirror reference run, '
        'as in the essreflectometry amor-reduction tutorial.'
    ),
    params=ReflectivityParams,
    outputs=ReflectivityOutputs,
)


# Stitching several angles into one curve


class CombineParams(BaseModel):
    curves: dict[str, Array(CURVE)]
    q: QEdges = QEdges(start=0.0035, stop=0.3, num_bins=200, scale=Scale.LOG)
    critical_edge: QRange | None = Field(
        default=None,
        description=(
            'Q interval where the reflectivity is known to be 1. Without it the '
            'curve of lowest Q is the reference and is not scaled.'
        ),
    )


class CombineOutputs(BaseModel):
    combined: Array(CURVE)
    scaled: dict[str, Array(CURVE)]
    scale_factors: dict[str, float]


def combine_workflow() -> Any:
    """
    A plain callable, not a :class:`WarmPipeline` and not a declared combine.

    Nothing here is a sciline graph: the scale factors come from a fit over all
    curves at once, so there is no per-member stage to hold and nothing the
    framework could chain or fold. Every curve is read on every call, which is
    what the sketch means by recomputing an opaque combine on every arrival.
    """

    def run(params: CombineParams, inputs: Inputs) -> dict[str, Any]:
        curves = resolve(params.curves, 'array', inputs)
        factors = scale_for_reflectivity_overlap(
            sc.DataGroup(curves),
            critical_edge_interval=(
                None if params.critical_edge is None else params.critical_edge.interval
            ),
        )
        scaled = {key: curve * factors[key] for key, curve in curves.items()}
        return {
            'combined': combine_curves(scaled, _edges(params.q, 'Q')),
            'scaled': scaled,
            'scale_factors': {key: float(factor) for key, factor in factors.items()},
        }

    return run


COMBINE = WorkflowSpec(
    name='amor-combine',
    version=1,
    title='Amor stitched reflectivity',
    description=(
        'Scale reflectivity curves measured at different sample rotations so '
        'that they overlap, and average them onto one Q grid.'
    ),
    params=CombineParams,
    outputs=CombineOutputs,
)


def registry() -> Registry:
    reg = Registry()
    reg.bind(REFLECTIVITY, reflectivity_workflow)
    reg.bind(COMBINE, combine_workflow)
    return reg


def cache() -> Path:
    """The folder ``ess.amor.data`` caches the tutorial files in."""
    return Path(data.amor_run(608)).parent
