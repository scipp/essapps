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

from ess.spec import WorkflowSpec


def allowed_spec() -> WorkflowSpec:
    class TransmissionRun(BaseModel):
        file: Path

    class SampleRun(BaseModel):
        file: Path
        transmission: TransmissionRun | None = None

    class WavelengthRange(BaseModel):
        start: float
        stop: float

    class PixelMask(BaseModel, arbitrary_types_allowed=True):
        mask: sc.DataArray

    class Reduction(BaseModel):
        bins: Annotated[int, Gt(0)] = 50
        range: WavelengthRange | None = None
        banks: list[int] = []
        mask: PixelMask | None = None

    class GivenCentre(BaseModel):
        kind: Literal['given'] = 'given'
        x: float = 0.0
        y: float = 0.0

    class FindCentre(BaseModel):
        kind: Literal['find'] = 'find'
        tolerance: Annotated[float, Gt(0)] = 1e-3

    class IofQParams(BaseModel):
        reduction: Reduction = Reduction()
        centre: Annotated[GivenCentre | FindCentre, Field(discriminator='kind')]
        sample_runs: list[SampleRun]

    class IofQOutputs(BaseModel, arbitrary_types_allowed=True):
        iofq: sc.DataArray
        mask: PixelMask

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
    class SampleRun(BaseModel):
        file: Path

    class WavelengthRange(BaseModel):
        start: float
        stop: float

    class GivenCentre(BaseModel):
        kind: Literal['given'] = 'given'

    class FindCentre(BaseModel):
        kind: Literal['find'] = 'find'

    RunNumber = NewType('RunNumber', int)

    class MaskedRun(BaseModel, arbitrary_types_allowed=True):
        file: Path
        mask: sc.DataArray

    class Reduction(BaseModel):
        bins: int = Field(10, alias='nbins')
        start: float | None = None
        limits: tuple[float, float] = (0.0, 1.0)
        children: list[Reduction] = []
        range: WavelengthRange = {'start': 1.0, 'stop': 2.0}

        @computed_field
        def width(self) -> float:
            return 0.0

    class Centring(BaseModel):
        centre: GivenCentre | FindCentre

    class Sample(BaseModel):
        run: RunNumber

    class Params(BaseModel):
        bins: int = 10
        run: MaskedRun
        reduction: Reduction
        centring: Centring
        sample: Sample
        sample_runs: list[SampleRun] = []
        iofq: SampleRun

        @field_validator('bins')
        @classmethod
        def _positive(cls, bins: int) -> int:
            return bins

    class Outputs(BaseModel, arbitrary_types_allowed=True):
        iofq: sc.DataArray
        width: float | str

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
        'MaskedRun.mask: no request writes it',
        'Reduction.bins: a field of a group has no alias',
        'Reduction.start: typed X | None with X not a group',
        'Reduction.limits: tuple is not a scalar',
        'Reduction.children: Reduction: a group is not at a place inside itself',
        'Reduction.range: the default',
        "Reduction: no serializers or computed fields, {'computed_fields'",
        'Centring.centre: a union of groups is a parameter',
        'Sample.run: RunNumber is a NewType',
        'Params.bins: int is not a group',
        'sample_runs: a table has no default',
        'iofq: a field of both Params and Outputs',
        "Params: no decorators but model validators, {'field_validators'",
        'Outputs.width: an output is a type',
    ],
)
def test_refused_example_breaks_each_rule(refusal: str, rule: str) -> None:
    assert rule in refusal
