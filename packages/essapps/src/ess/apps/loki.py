# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
LoKI at Larmor bound to the spec vocabulary: a beam centre and an I(Q) reduction.

The two specs are the instrument half of the notebook story in
``notebooks/loki-session.ipynb``: the beam centre is computed once from the
sample run, its output feeds the I(Q) reduction as a reference, and the Q
binning parameters are what a slider moves, so that the session stages the
reduction over them and a rerun costs only the part of the sciline graph that
depends on them.

The workflow is the one of the ``loki-iofq`` tutorial in esssans. Everything the
tutorial sets that is not a parameter here -- the detector name, gravity
correction, the uncertainty broadcast mode, and the pixel masks -- is an
instrument default of the factory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, NewType

import sciline
import scipp as sc
from ess import loki, sans
from ess.loki import data
from ess.reduce.spec.conversions import edges_to_variable
from ess.reduce.spec.parameters import QEdges, WavelengthEdges
from ess.sans.types import (
    BackgroundRun,
    BackgroundSubtractedIofQ,
    BeamCenter,
    CorrectForGravity,
    DirectBeamFilename,
    EmptyBeamRun,
    Filename,
    NeXusDetectorName,
    QBins,
    ReturnEvents,
    SampleRun,
    TransmissionRun,
    UncertaintyBroadcastMode,
    WavelengthBins,
)
from pydantic import BaseModel, Field

from .adapter import PipelineAdapter
from .binding import Inputs, Registry
from .spec import (
    Array,
    ArraySpec,
    NexusFile,
    OpaqueFile,
    OutputRef,
    Quantity,
    WorkflowSpec,
)

DETECTOR = 'larmor_detector'


def _instrument_pipeline() -> sciline.Pipeline:
    """
    The LoKI@Larmor pipeline with the settings that are not request parameters.

    The pixel masks are applied here because ``with_pixel_mask_filenames``
    rewrites the ``DetectorMasks`` node into a mapped-and-reduced subgraph rather
    than setting a value: the number of masks changes the graph, so a mask cannot
    be a parameter that the runner assigns. The sketch wants the masks on the
    record, since they change the result; expressing that needs a way for a
    binding to rebuild the graph from a collection-valued parameter, which the
    callable contract (D8) does not have.
    """
    pipeline = loki.LokiAtLarmorWorkflow()
    pipeline = sans.with_pixel_mask_filenames(
        pipeline, masks=[str(p) for p in data.loki_tutorial_mask_filenames()]
    )
    pipeline[NeXusDetectorName] = DETECTOR
    pipeline[CorrectForGravity] = True
    pipeline[UncertaintyBroadcastMode] = UncertaintyBroadcastMode.upper_bound
    pipeline[ReturnEvents] = False
    return pipeline


# The beam centre


class BeamCenterParams(BaseModel):
    sample_run: NexusFile


class BeamCenterOutputs(BaseModel):
    center: Quantity = Field(description="Detector-plane offset of the beam, in m.")


def beam_center_workflow() -> Any:
    """
    A plain callable, not a :class:`PipelineAdapter`.

    ``beam_center_from_center_of_mass`` takes a pipeline and returns a vector; it
    is not a sciline provider, so there is no key to compute and nothing for a
    stage to hold. The pipeline is therefore built per run and shared with
    nothing, including the I(Q) reduction that consumes the result.
    """

    def run(params: BeamCenterParams, inputs: Inputs) -> dict[str, Any]:
        pipeline = _instrument_pipeline()
        pipeline[Filename[SampleRun]] = str(inputs.path(params.sample_run))
        center = sans.beam_center_from_center_of_mass(pipeline)
        return {'center': Quantity(value=tuple(center.value), unit=str(center.unit))}

    return run


BEAM_CENTER = WorkflowSpec(
    name='loki-beam-center',
    version=1,
    title='LoKI beam centre',
    description='Beam centre of a LoKI@Larmor sample run from the centre of mass.',
    params=BeamCenterParams,
    outputs=BeamCenterOutputs,
)


# I(Q)

QEdgesParam = NewType('QEdgesParam', QEdges)
WavelengthEdgesParam = NewType('WavelengthEdgesParam', WavelengthEdges)
BeamCenterQuantity = NewType('BeamCenterQuantity', Quantity)


def q_edges(edges: QEdgesParam) -> QBins:
    """Spec-vocabulary edges as the variable the workflow wants."""
    return QBins(edges_to_variable(edges, 'Q'))


def wavelength_edges(edges: WavelengthEdgesParam) -> WavelengthBins:
    return WavelengthBins(edges_to_variable(edges, 'wavelength'))


def beam_center_vector(center: BeamCenterQuantity) -> BeamCenter:
    """A spec-vocabulary quantity as the vector the workflow wants."""
    return BeamCenter(sc.vector(value=list(center.value), unit=center.unit))


class IofQParams(BaseModel):
    sample_run: NexusFile
    sample_transmission_run: NexusFile
    background_run: NexusFile
    background_transmission_run: NexusFile
    empty_beam_run: NexusFile
    direct_beam: OpaqueFile
    beam_center: Quantity | OutputRef
    wavelength: WavelengthEdges = WavelengthEdges(start=1.0, stop=13.0, num_bins=200)
    q: QEdges = QEdges(start=0.01, stop=0.3, num_bins=100)


class IofQOutputs(BaseModel):
    iofq: Array(ArraySpec(dims=('Q',), coords={'Q': '1/angstrom'}))


def iofq_workflow() -> PipelineAdapter:
    pipeline = _instrument_pipeline()
    for provider in (q_edges, wavelength_edges, beam_center_vector):
        pipeline.insert(provider)
    return PipelineAdapter(
        pipeline,
        keys={
            'sample_run': Filename[SampleRun],
            'sample_transmission_run': Filename[TransmissionRun[SampleRun]],
            'background_run': Filename[BackgroundRun],
            'background_transmission_run': Filename[TransmissionRun[BackgroundRun]],
            'empty_beam_run': Filename[EmptyBeamRun],
            'direct_beam': DirectBeamFilename,
            'beam_center': BeamCenterQuantity,
            'wavelength': WavelengthEdgesParam,
            'q': QEdgesParam,
        },
        resolve={
            'sample_run': 'path',
            'sample_transmission_run': 'path',
            'background_run': 'path',
            'background_transmission_run': 'path',
            'empty_beam_run': 'path',
            'direct_beam': 'path',
        },
        targets={'iofq': BackgroundSubtractedIofQ},
        default_stage_inputs=['q'],
    )


IOFQ = WorkflowSpec(
    name='loki-iofq',
    version=1,
    title='LoKI I(Q)',
    description=(
        'Background-subtracted I(Q) of a LoKI@Larmor sample run, as in the '
        'esssans loki-iofq tutorial.'
    ),
    params=IofQParams,
    outputs=IofQOutputs,
)


def registry() -> Registry:
    reg = Registry()
    reg.bind(BEAM_CENTER, beam_center_workflow)
    reg.bind(IOFQ, iofq_workflow)
    return reg


def cache() -> Path:
    """The folder ``ess.loki.data`` caches the tutorial files in."""
    return Path(data.loki_tutorial_background_run_60393()).parent
