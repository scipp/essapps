# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The specs of LoKI's reductions.

A reduction is three records, each of which a person may look at before the next::

    masks = client.submit(MASKS, params={'run': file(run=60339),
                                         'mask_files': [file(path=mask_file)]})
    centre = client.submit(BEAM_CENTRE, params={'sample': file(run=60339),
                                                'masks': masks.ref('masks')})
    iofq = client.submit(IOFQ, params={'masks': masks.ref('masks'),
                                       'beam_centre': centre.ref('centre'), ...})

The specs are separate where a value can outlive one sample: the masks hold for an
experiment, and one beam centre can serve the samples of a sample changer, though a
more invasive change of sample needs its own. I(Q) takes the masks
as computed, not their files, so that the can's branch of its graph does not
depend on the sample, and a stage with the sample blank reduces the can once.
"""

from pathlib import Path
from typing import Annotated, Literal

import scipp as sc
from pydantic import BaseModel, Field

from ess.spec import (
    BackgroundRunFile,
    EmptyBeamRunFile,
    File,
    MaskFile,
    Referenced,
    RunFile,
    SampleRunFile,
    TransmissionRunFile,
    WorkflowSpec,
)
from ess.spec.parameters import QEdges, WavelengthEdges

DirectBeamFile = Annotated[Path, File(kind='calibration', role='direct_beam')]
"""Types a file field that a direct-beam function fills."""


class PixelMasks(BaseModel, arbitrary_types_allowed=True):
    """Which pixels to leave out: a bool per pixel, by the name of each mask."""

    masks: dict[str, sc.Variable]


class DetectorImage(BaseModel, arbitrary_types_allowed=True):
    """
    The counts per pixel of one run, with each pixel's position and the masks.

    The positions are those of the file, not corrected by a beam centre. With the
    sample at the origin, as in the files of the detector test at Larmor, a beam
    centre found from this run marks its place on the image as it is.
    """

    counts: sc.DataArray


class BeamCentre(BaseModel):
    """
    A point on the beam, relative to the sample, which gives the direction of the beam.

    ``z`` is the distance from the sample of the plane in which ``x`` and ``y`` were
    determined, since the same direction is a smaller offset on a bank closer to the
    sample. Only the direction counts, so ``x=0, y=0, z=1`` applies no correction.
    """

    x: float = Field(description='Horizontal offset from the sample, in m.')
    y: float = Field(description='Vertical offset from the sample, in m.')
    z: float = Field(gt=0, description='Distance from the sample along the beam, in m.')


class Corrections(BaseModel):
    """How a reduction treats gravity and uncertainties."""

    gravity: bool = Field(
        default=True, description='Correct for the fall of neutrons in gravity.'
    )
    uncertainties: Literal['upper_bound', 'drop'] = Field(
        default='upper_bound',
        description='How to treat the variances of the normalization when they are '
        'broadcast over pixels and wavelengths.',
    )


class IofQ(BaseModel, arbitrary_types_allowed=True):
    """
    I(Q) of a sample, minus that of its can, with the title and the run number of the
    sample's file, or ``None`` where the file has none. An NXcanSAS file holds both,
    and a record's outputs are all that writing one has.
    """

    intensity: sc.DataArray
    title: str | None
    run: str | None


class TransmissionCurves(BaseModel, arbitrary_types_allowed=True):
    """The transmission fraction of the sample and of the can, by wavelength."""

    sample: sc.DataArray
    can: sc.DataArray


class MasksParams(BaseModel):
    run: RunFile = Field(description='The run to show the masks on.')
    mask_files: list[MaskFile] = Field(
        description='The files of the masks. Without any, no pixel is left out.'
    )


class MasksOutputs(BaseModel):
    masks: PixelMasks
    image: DetectorImage


MASKS = WorkflowSpec(
    name='loki-masks',
    version=1,
    title='Pixel masks',
    description='Pixel masks from mask files, shown on the detector image of a run.',
    params=MasksParams,
    outputs=MasksOutputs,
)


class BeamCentreParams(BaseModel):
    sample: SampleRunFile = Field(description='The run to find the beam centre from.')
    masks: Referenced[PixelMasks] = Field(description='The pixels to leave out.')


class BeamCentreOutputs(BaseModel):
    centre: BeamCentre
    image: DetectorImage


BEAM_CENTRE = WorkflowSpec(
    name='loki-beam-centre',
    version=1,
    title='Beam centre',
    description='The beam centre, from the centre of mass of the counts of a run.',
    params=BeamCentreParams,
    outputs=BeamCentreOutputs,
)


class IofQParams(BaseModel):
    sample: SampleRunFile = Field(description='The sample run.')
    sample_transmission: TransmissionRunFile = Field(
        description='The transmission run of the sample.'
    )
    can: BackgroundRunFile = Field(description='The run of the empty can.')
    can_transmission: TransmissionRunFile = Field(
        description='The transmission run of the can.'
    )
    empty_beam: EmptyBeamRunFile = Field(description='The empty-beam run.')
    direct_beam: DirectBeamFile | None = Field(
        description='The direct-beam function. None assumes a detector efficiency '
        'that does not depend on wavelength.'
    )
    masks: Referenced[PixelMasks] = Field(description='The pixels to leave out.')
    beam_centre: BeamCentre | Referenced[BeamCentre] = Field(
        description='The beam centre, typed in or found by a beam-centre record.'
    )
    wavelength: WavelengthEdges = Field(description='Bin edges in wavelength.')
    q: QEdges = Field(description='Bin edges in Q.')
    corrections: Corrections = Field(default_factory=Corrections)


class IofQOutputs(BaseModel):
    iofq: IofQ
    transmission: TransmissionCurves


IOFQ = WorkflowSpec(
    name='loki-iofq',
    version=1,
    title='I(Q)',
    description='I(Q) of one sample run, minus that of one can run.',
    params=IofQParams,
    outputs=IofQOutputs,
)
