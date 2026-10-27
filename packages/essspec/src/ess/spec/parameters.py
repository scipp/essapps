# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Shared vocabulary of workflow parameter models.

Common building blocks for workflow params models: the dataset model of a
file, constrained unit choices, and range/edges models with cross-field
validation. Purely declarative — no scipp;
converting validated values into scipp objects is the workflow implementation's
concern (see :mod:`ess.spec.conversions`).

Value defaults (start, stop, number of bins) are deliberately not provided
here: sensible values depend on the workflow and instrument, so workflow
authors set them at the use site, next to the field's title and description::

    class MyParams(pydantic.BaseModel):
        wavelength: WavelengthEdges = pydantic.Field(
            title='Wavelength bins',
            description='Bin edges for binning in wavelength.',
            default=WavelengthEdges(start=1.0, stop=10.0, num_bins=200),
        )
"""

from __future__ import annotations

from abc import ABC
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from pydantic import (
    BaseModel,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)


class DatasetModel(BaseModel):
    """
    A kind of file: a group of the local path of a dataset's file, given by
    reference to a dataset only.

    A subclass names the metadata of the datasets it takes, as keywords of
    the class, each a string, a number, or a bool::

        class MaskFile(DatasetModel, kind='mask'):
            '''A pixel mask, as XML.'''

    A dataset fits the model if its metadata have each key and value of the
    model's ``metadata``; a dataset without ``kind`` is raw. ``metadata`` is
    ``{'kind': 'raw'}`` unless a subclass says otherwise: a subclass with
    keywords merges them over its parent's metadata, one without inherits
    them. A keyword that is a key of pydantic's ``ConfigDict``, such as
    ``title``, ``frozen``, or ``extra``, configures the model instead, so it
    cannot be a metadata key.

    A request gives a dataset model by reference to a dataset, which the
    backend resolves to the dataset's identity when the request is
    submitted. When the request runs, the backend makes the model of the
    dataset's local path, ``MaskFile(path=<local path>)``, so that a
    validator of the model, such as a check of the file's suffix, runs with
    the local path. A dataset model holds only ``path``, as declared here,
    and no output holds a dataset model, so that every path a workflow gets
    comes from a dataset that the dataset source checks.
    """

    metadata: ClassVar[dict[str, Any]] = {'kind': 'raw'}
    path: Path

    def __init_subclass__(cls, **metadata: Any) -> None:
        super().__init_subclass__()
        if bad := {k: v for k, v in metadata.items() if type(v) not in _SCALARS}:
            raise TypeError(
                f'{cls.__name__}: the metadata of a dataset model are strings, '
                f'numbers, or bools, as JSON holds them, not {bad}'
            )
        cls.metadata = {**cls.metadata, **metadata}


_SCALARS = (str, int, float, bool)


class Scale(StrEnum):
    """Spacing of generated bin edges."""

    LINEAR = 'linear'
    LOG = 'log'


class TimeUnit(StrEnum):
    """Allowed units for time."""

    NS = 'ns'
    US = 'us'
    MICROSECOND = 'µs'
    MS = 'ms'
    S = 's'


class WavelengthUnit(StrEnum):
    """Allowed units for wavelength."""

    ANGSTROM = 'Å'
    NANOMETER = 'nm'


class DspacingUnit(StrEnum):
    """Allowed units for d-spacing."""

    ANGSTROM = 'Å'
    NANOMETER = 'nm'


class LengthUnit(StrEnum):
    """Allowed units for length."""

    METER = 'm'
    CENTIMETER = 'cm'
    MILLIMETER = 'mm'


class AngleUnit(StrEnum):
    """Allowed units for angles."""

    DEGREE = 'deg'
    RADIAN = 'rad'


class QUnit(StrEnum):
    """Allowed units for momentum transfer Q."""

    INVERSE_ANGSTROM = '1/Å'
    INVERSE_NANOMETER = '1/nm'


class EnergyUnit(StrEnum):
    """Allowed units for energy transfer."""

    MILLI_EV = 'meV'
    MICRO_EV = 'µeV'


class Quantity(BaseModel, frozen=True):
    """
    A scalar or short vector with a unit, as plain data that a request can
    give inline.

    The small-value type for values that a user reads or types, such as a
    scale factor. As a parameter it is a group like any other, or a field of
    one.
    """

    value: float | tuple[float, ...] = Field(description="The value(s).")
    unit: str | None = Field(default=None, description="Unit, if any.")


class RangeModel(BaseModel, ABC):
    """Base model for a value range. Subclasses constrain the unit."""

    start: float = Field(description="Start of the range.")
    stop: float = Field(description="Stop of the range.")
    unit: str

    @field_validator('stop')
    @classmethod
    def stop_must_be_greater_than_start(cls, v: float, info: ValidationInfo) -> float:
        start = info.data.get('start')
        if start is not None and v <= start:
            raise ValueError('stop must be greater than start')
        return v


class EdgesModel(BaseModel, ABC):
    """Base model for bin edges. Subclasses constrain the unit."""

    start: float = Field(description="First bin edge.")
    stop: float = Field(description="Last bin edge.")
    num_bins: int = Field(ge=1, le=10000, description="Number of bins.")
    scale: Scale = Field(
        default=Scale.LINEAR,
        description="Spacing of the edges, either 'linear' or 'log'.",
    )
    unit: str

    @field_validator('stop')
    @classmethod
    def stop_must_be_greater_than_start(cls, v: float, info: ValidationInfo) -> float:
        start = info.data.get('start')
        if start is not None and v <= start:
            raise ValueError('stop must be greater than start')
        return v

    @model_validator(mode='after')
    def start_must_be_positive_if_log(self) -> EdgesModel:
        if self.scale == Scale.LOG and self.start <= 0:
            raise ValueError("start must be positive when scale is 'log'")
        return self


class TOARange(RangeModel):
    """Time-of-arrival range."""

    unit: TimeUnit = Field(
        default=TimeUnit.MICROSECOND, description="Unit of the range bounds."
    )


class WavelengthRange(RangeModel):
    """Wavelength range."""

    unit: WavelengthUnit = Field(
        default=WavelengthUnit.ANGSTROM, description="Unit of the range bounds."
    )


class TOAEdges(EdgesModel):
    """Time-of-arrival bin edges."""

    unit: TimeUnit = Field(default=TimeUnit.MS, description="Unit of the edges.")


class WavelengthEdges(EdgesModel):
    """Wavelength bin edges."""

    unit: WavelengthUnit = Field(
        default=WavelengthUnit.ANGSTROM, description="Unit of the edges."
    )


class DspacingEdges(EdgesModel):
    """D-spacing bin edges."""

    unit: DspacingUnit = Field(
        default=DspacingUnit.ANGSTROM, description="Unit of the edges."
    )


class TwoThetaEdges(EdgesModel):
    """Scattering angle (two-theta) bin edges."""

    unit: AngleUnit = Field(default=AngleUnit.DEGREE, description="Unit of the edges.")


class ThetaEdges(EdgesModel):
    """Theta bin edges."""

    unit: AngleUnit = Field(default=AngleUnit.DEGREE, description="Unit of the edges.")


class QEdges(EdgesModel):
    """Momentum transfer (Q) bin edges."""

    unit: QUnit = Field(
        default=QUnit.INVERSE_ANGSTROM, description="Unit of the edges."
    )


class EnergyEdges(EdgesModel):
    """Energy transfer bin edges."""

    unit: EnergyUnit = Field(
        default=EnergyUnit.MILLI_EV, description="Unit of the edges."
    )
