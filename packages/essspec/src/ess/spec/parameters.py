# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Shared vocabulary of workflow parameter models.

Common building blocks for workflow params models: the file model,
constrained unit choices, and range/edges models with cross-field
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
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Self

from pydantic import (
    BaseModel,
    Field,
    PrivateAttr,
    ValidationInfo,
    field_validator,
    model_serializer,
    model_validator,
)

from .refs import FileRef


class FileModel(BaseModel, frozen=True, extra='forbid'):
    """
    A file that a workflow reads: its identity, given by reference only, and
    its local path when the request runs.

    A subclass is a kind of file. It names the metadata of the files it
    takes, as keywords of the class, each a string, a number, or a bool::

        class MaskFile(FileModel, kind='mask'):
            '''A pixel mask, as XML.'''

    A file fits the model if its metadata have each key and value of the
    model's ``metadata``; a file without ``kind`` is raw. ``metadata`` is
    ``{'kind': 'raw'}`` unless a subclass says otherwise: a subclass with
    keywords merges them over its parent's metadata, one without inherits
    them. A keyword that is a key of pydantic's ``ConfigDict``, such as
    ``title``, ``frozen``, or ``extra``, configures the model instead, so it
    cannot be a metadata key.

    The value of a file model is ``file``, the file's identity, which the
    backend resolves when the request is submitted, so that everything a
    request gives is known then. Its JSON form is the reference to the file,
    ``{'@ref': 'file', 'file': ...}``, of which it is also validated, so that
    a reference is found in a record without the spec. Two file models are
    equal if they are of one class and name one file.

    The local path of the file exists only when the request runs: the
    backend validates the model with the context ``{'paths': {identity:
    local path}}``, and :attr:`path` raises before. A file model holds only
    ``file``, as declared here, and has no validators of its own, since what
    it would check of a file is either metadata, which the fit check covers
    when the request is submitted, or the workflow's to read. No output
    holds a file model, so that every path a workflow gets comes from a file
    that the file source checks. A program without a backend makes a file
    model of a local file with :meth:`local`.
    """

    metadata: ClassVar[dict[str, Any]] = {'kind': 'raw'}
    file: str = Field(min_length=1)
    _path: Path | None = PrivateAttr(None)

    def __init_subclass__(cls, **metadata: Any) -> None:
        super().__init_subclass__()
        if bad := {k: v for k, v in metadata.items() if type(v) not in _SCALARS}:
            raise TypeError(
                f'{cls.__name__}: the metadata of a file model are strings, '
                f'numbers, or bools, as JSON holds them, not {bad}'
            )
        cls.metadata = {**cls.metadata, **metadata}

    @classmethod
    def local(cls, path: Path | str) -> Self:
        """
        The model of the local file ``path``, named ``path:<path>``, as
        ``file(path=...)`` names it, a name that a backend resolves to the
        file's identity.
        """
        identity = f'path:{path}'
        return cls.model_validate(
            {'file': identity}, context={'paths': {identity: Path(path)}}
        )

    @property
    def path(self) -> Path:
        """
        The local path of the file, when the request runs; raises
        ``ValueError`` before, so that a validator that reads it refuses the
        request when it arrives.
        """
        if self._path is None:
            raise ValueError(
                f'{self.file}: a file has a local path only when the request '
                'runs, or if made of a local file with .local(path)'
            )
        return self._path

    @model_validator(mode='before')
    @classmethod
    def _of_ref(cls, value: Any) -> Any:
        if isinstance(value, FileRef):
            return {'file': value.file}
        if isinstance(value, Mapping) and value.get('@ref') == 'file':
            return {k: v for k, v in value.items() if k != '@ref'}
        return value

    @model_validator(mode='after')
    def _with_path(self, info: ValidationInfo) -> Self:
        paths = (info.context or {}).get('paths', {})
        if self.file in paths:
            self._path = Path(paths[self.file])
        return self

    @model_serializer
    def _as_ref(self) -> dict[str, str]:
        return {'@ref': 'file', 'file': self.file}

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FileModel):
            return NotImplemented
        return type(self) is type(other) and self.file == other.file

    def __hash__(self) -> int:
        return hash((type(self), self.file))


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
