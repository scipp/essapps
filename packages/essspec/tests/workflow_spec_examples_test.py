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

from ess.spec import DatasetModel, WorkflowSpec


def allowed_spec() -> WorkflowSpec:
    class RunFile(DatasetModel, kind='raw'): ...

    class WavelengthRange(BaseModel):
        start: float
        stop: float

    class PixelMask(BaseModel, arbitrary_types_allowed=True):
        mask: sc.DataArray

    class Reduction(BaseModel):
        bins: Annotated[int, Gt(0)] = 50
        banks: list[int] = []

    class GivenCentre(BaseModel):
        kind: Literal['given'] = 'given'
        x: float = 0.0
        y: float = 0.0

    class FindCentre(BaseModel):
        kind: Literal['find'] = 'find'
        tolerance: Annotated[float, Gt(0)] = 1e-3

    class IofQParams(BaseModel):
        reduction: Reduction = Reduction()
        range: WavelengthRange | None = None
        mask: PixelMask | None = None
        centre: Annotated[GivenCentre | FindCentre, Field(discriminator='kind')]
        transmission: RunFile
        sample_runs: list[RunFile]

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
        tables=('sample_runs',),
    )


def refused_spec() -> WorkflowSpec:
    class RunFile(DatasetModel, kind='raw'): ...

    class WavelengthRange(BaseModel):
        start: float
        stop: float

    class PixelMask(BaseModel, arbitrary_types_allowed=True):
        mask: sc.DataArray

    class GivenCentre(BaseModel):
        kind: Literal['given'] = 'given'

    class FindCentre(BaseModel):
        kind: Literal['find'] = 'find'

    RunNumber = NewType('RunNumber', int)

    class Geometry(BaseModel):
        file: Path

    class CalibrationFile(DatasetModel, kind='calibration'):
        run: int

    class Reduction(BaseModel):
        bins: int = Field(10, alias='nbins')
        start: float | None = None
        limits: tuple[float, float] = (0.0, 1.0)
        range: WavelengthRange

        @computed_field
        def width(self) -> float:
            return 0.0

    class Centring(BaseModel):
        centre: GivenCentre | FindCentre

    class Sample(BaseModel):
        run: RunNumber

    class Params(BaseModel):
        bins: int = 10
        reduction: Reduction
        centring: Centring
        sample: Sample
        geometry: Geometry
        calibration: CalibrationFile
        transmissions: list[RunFile] | None = None
        masks: list[PixelMask | None] = []
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
        'Reduction.start: a field is never typed X | None',
        'Reduction.limits: tuple is not a scalar',
        'Reduction.range: WavelengthRange is a group',
        "Reduction: no serializers or computed fields, {'computed_fields'",
        'Centring.centre: GivenCentre | FindCentre is a union of groups',
        'Sample.run: RunNumber is a NewType',
        'Geometry.file: a Path is outside a dataset model',
        "CalibrationFile: a dataset model holds only dataset, not ['run']",
        'Params.transmissions: a list parameter is not typed list[X] | None',
        'Params.masks: the element of a list parameter is not typed X | None',
        'Params.range: the default',
        'Outputs.raw: RunFile is a dataset model; no output holds one',
        'Params.bins: int is not a group',
        'sample_runs: a table has no default',
        'iofq: a field of both Params and Outputs',
        "Params: no decorators but model validators, {'field_validators'",
        'Outputs.width: an output is a type',
    ],
)
def test_refused_example_breaks_each_rule(refusal: str, rule: str) -> None:
    assert rule in refusal
