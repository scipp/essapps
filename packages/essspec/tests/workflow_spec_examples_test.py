# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The two examples in the docstring of :mod:`ess.spec._workflow_spec`: keep
them in step with it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, NewType

import pytest
import scipp as sc
from annotated_types import Gt
from pydantic import BaseModel, Field, computed_field, field_validator

from ess.spec import File, Referenced, RunFile, WorkflowSpec


def allowed_spec() -> WorkflowSpec:
    class WavelengthRange(BaseModel):
        start: float
        stop: float

    class Reduction(BaseModel):
        bins: Annotated[int, Gt(0)] = 50
        banks: list[int] = []
        q_max: float | None = None
        range: WavelengthRange | None = None

    class Transmission(BaseModel, arbitrary_types_allowed=True):
        fraction: sc.DataArray

    class Sample(BaseModel):
        run: RunFile
        can: RunFile | None = None
        thickness: float = 1.0
        transmission: Referenced[Transmission]

    class PixelMask(BaseModel, arbitrary_types_allowed=True):
        mask: sc.DataArray

    class GivenCentre(BaseModel):
        kind: Literal['given'] = 'given'
        x: float = 0.0
        y: float = 0.0

    class FindCentre(BaseModel):
        kind: Literal['find'] = 'find'
        tolerance: Annotated[float, Gt(0)] = 1e-3

    class BeamCentre(BaseModel):
        x: float
        y: float

    class IofQParams(BaseModel):
        reduction: Reduction = Reduction()
        mask: Referenced[PixelMask] | None = None
        centre: (
            Annotated[GivenCentre | FindCentre, Field(discriminator='kind')]
            | Referenced[BeamCentre]
        )
        direct_beam: RunFile
        samples: list[Sample]

    class IofQOutputs(BaseModel, arbitrary_types_allowed=True):
        iofq: sc.DataArray
        pixel_mask: PixelMask

    return WorkflowSpec(
        name='iofq',
        version=1,
        title='I(Q)',
        description='Reduce sample runs to I(Q).',
        params=IofQParams,
        outputs=IofQOutputs,
        tables=('samples',),
    )


def refused_spec() -> WorkflowSpec:
    class WavelengthRange(BaseModel):
        start: float
        stop: float

    class PixelMask(BaseModel, arbitrary_types_allowed=True):
        mask: sc.DataArray

    RunNumber = NewType('RunNumber', int)

    class Geometry(BaseModel):
        file: Path

    class Calibration(BaseModel):
        run: Annotated[int, File(kind='calibration')]

    class Flux(BaseModel):
        run: RunFile

    class Reduction(BaseModel):
        bins: int = Field(10, alias='nbins')
        width: float = Field(1.0, exclude=True)

        @computed_field
        def area(self) -> float:
            return 0.0

    class Binning(BaseModel):
        limits: tuple[float, float] = (0.0, 1.0)
        groups: list[list[int]] = []

    class Sample(BaseModel):
        run: RunNumber

    class Specimen(BaseModel):
        inner: Specimen | None = None

    class GivenCentre(BaseModel):
        kind: Literal['given'] = 'given'

    class FindCentre(BaseModel):
        kind: Literal['find'] = 'find'

    class FoundCentre(BaseModel):
        kind: Literal['found'] = 'found'
        x: float
        y: float

    class Params(BaseModel):
        bins: int = 10
        reduction: Reduction
        binning: Binning = Binning()
        sample: Sample
        specimen: Specimen
        geometry: Geometry
        calibration: Calibration
        mask: Referenced[PixelMask | None] = None
        flux: Referenced[Flux]
        centre: Annotated[
            GivenCentre | FindCentre | Referenced[FoundCentre],
            Field(discriminator='kind'),
        ]
        transmissions: list[RunFile] | None = None
        masks: list[Referenced[PixelMask] | None] = []
        range: WavelengthRange = {'start': 1.0, 'stop': 2.0}
        sample_runs: list[RunFile] = []
        iofq: RunFile

        @field_validator('bins')
        @classmethod
        def _positive(cls, bins: int) -> int:
            return bins

    class Outputs(BaseModel, arbitrary_types_allowed=True):
        iofq: sc.DataArray
        width: float | str
        raw: RunFile
        pixel_mask: Referenced[PixelMask]

    return WorkflowSpec(
        name='bad',
        version=1,
        title='Bad',
        description='Breaks each rule once.',
        params=Params,
        outputs=Outputs,
        tables=('sample_runs',),
    )


def test_allowed_example_makes_a_spec() -> None:
    allowed_spec().serialize()


@pytest.fixture(scope='module')
def refusal() -> str:
    with pytest.raises(ValueError, match='bad: ') as info:
        refused_spec()
    return str(info.value)


@pytest.mark.parametrize(
    'rule',
    [
        'Reduction.bins: a field of a group has no alias',
        "Reduction: no serializers or computed fields, {'computed_fields'",
        'Sample.run: RunNumber is a NewType',
        'Geometry.file: a Path without File(...)',
        'Calibration.run: File(...) marks a Path, not int',
        'Params.transmissions: a list is not typed list[X] | None',
        'Params.masks: the element of a list is not typed X | None',
        'Params.range: the default',
        'Reduction.width: a field of a group has no exclude or exclude_if',
        'Params.binning: Binning.limits: tuple[float, float] is not a field type',
        'Binning.groups: a list is not of lists',
        'Params.centre: the discriminator of a union with a member Referenced[...]',
        'Params.specimen: Specimen.inner: Specimen contains itself',
        'Params.mask: Referenced[PixelMask | None]: PixelMask | None is not a group',
        'Params.flux: Flux.run: a Path is in the data of an output',
        'Outputs.pixel_mask: Referenced[...] marks a field of the params',
        'Outputs.raw: a Path is in the data of an output; no output holds a file',
        'sample_runs: a table has no default',
        'iofq: a field of both Params and Outputs',
        "Params: no decorators but model validators, {'field_validators'",
        'Outputs.width: an output is a type',
    ],
)
def test_refused_example_breaks_each_rule(refusal: str, rule: str) -> None:
    assert rule in refusal
