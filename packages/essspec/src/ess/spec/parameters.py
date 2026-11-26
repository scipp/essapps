# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Building blocks of params models: the markers ``File`` and ``Referenced``,
file field types of common kinds and roles, units, and range and edges models.

The module needs no scipp. :mod:`ess.spec.conversions` converts validated
values to scipp objects.

It provides no value defaults, such as start, stop and number of bins, since
they depend on the workflow and the instrument. A workflow author sets them at
the use site::

    class MyParams(pydantic.BaseModel):
        wavelength: WavelengthEdges = pydantic.Field(
            title='Wavelength bins',
            description='Bin edges for binning in wavelength.',
            default=WavelengthEdges(start=1.0, stop=10.0, num_bins=200),
        )
"""

from __future__ import annotations

import functools
from abc import ABC
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Final, TypeVar

from pydantic import (
    BaseModel,
    Field,
    GetCoreSchemaHandler,
    ValidationError,
    ValidationInfo,
    ValidatorFunctionWrapHandler,
    field_validator,
    model_validator,
)
from pydantic_core import CoreSchema, ErrorDetails, PydanticCustomError, core_schema

from .refs import FileRef, OutputRef, Ref, as_ref


class File:
    """
    The marker of a *file field*, ``Annotated[Path, File(**metadata)]``.

    ``packages/essspec/docs/params.md`` (Files) states which files fit.

    Validated with the context ``{'paths': {identity: local path}}``, as the
    workspace validates a request, the marker replaces the stored form of a
    file by its local path, validated as ``Path``, and rejects any other
    value. Validated without ``paths``, as in direct use, the field accepts a
    ``Path`` as it is. Two markers are equal if their metadata are equal,
    each value of the same type, so ``File(n=1)`` and ``File(n=True)`` differ.

    Parameters
    ----------
    kind:
        What the file holds, such as ``'raw'``, the file of a run.
    **metadata:
        Further metadata that a fitting file has, or the ``role``: of the runs a
        picker offers, such as ``'sample'``, or what a file of another kind holds.

    Raises
    ------
    TypeError
        If a value of the metadata is not a string, a number, or a bool.
    """

    __slots__ = ('_metadata',)

    def __init__(self, kind: str = 'raw', **metadata: Any) -> None:
        metadata = {'kind': kind, **metadata}
        if bad := {k: v for k, v in metadata.items() if type(v) not in _SCALARS}:
            raise TypeError(
                'the metadata of a file field are strings, numbers, or bools, '
                f'as JSON holds them, not {bad}'
            )
        self._metadata = tuple(metadata.items())

    @property
    def metadata(self) -> dict[str, Any]:
        """The metadata of the files for the field."""
        return dict(self._metadata)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, File):
            return NotImplemented
        return self._typed == other._typed

    def __hash__(self) -> int:
        return hash(self._typed)

    @property
    def _typed(self) -> frozenset[tuple[str, type, Any]]:
        return frozenset((k, type(v), v) for k, v in self._metadata)

    def __repr__(self) -> str:
        return f'File({", ".join(f"{k}={v!r}" for k, v in self._metadata)})'

    def __get_pydantic_core_schema__(
        self, source: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.with_info_wrap_validator_function(_local, handler(source))


_SCALARS = (str, int, float, bool)


def _local(
    value: Any, validate: ValidatorFunctionWrapHandler, info: ValidationInfo
) -> Any:
    """
    The local path of ``value``, the stored form of a file, given by the
    context, validated as the field's type, ``Path``.
    """
    given = _given(value, info, 'paths', FileRef)
    if given is None:
        return validate(value)
    paths, ref = given
    if ref.file not in paths:
        raise PydanticCustomError('file', f'file {ref} has no local path')
    # A path validated from JSON is a string.
    return validate(str(paths[ref.file]))


def _given(
    value: Any, info: ValidationInfo, key: str, kind: Any
) -> tuple[Any, Ref] | None:
    """
    What the context holds under ``key``, and ``value`` as the reference of
    ``kind`` that the stored form holds; ``None`` without ``key``, as in
    direct use. Raises for a value of another source (see :func:`not_its_source`).
    """
    held = (info.context or {}).get(key)
    if held is None:
        return None
    ref = as_ref(value) if isinstance(value, Mapping) else None
    if not isinstance(ref, kind):
        raise not_its_source(value, file=kind is FileRef)
    return held, ref


def not_its_source(value: Any, file: bool) -> PydanticCustomError:
    """
    The error that ``value`` is not a file, at a file field if ``file``, or
    not a reference to an output, at a field typed ``Referenced[...]``.
    """
    if file:
        return PydanticCustomError(
            'file_ref',
            f'{value!r} is not a file; a file field is written as file(...), '
            'whose JSON form is {"@ref": "file", "file": NAME}',
        )
    return PydanticCustomError(
        'output_ref',
        'is not a reference to an output, as the field is typed Referenced[...]',
    )


class ReferencedDataInvalid(Exception):
    """
    The data read for ``ref``, a reference to an output, fail a check of the
    ``T`` of ``Referenced[T]``, with ``errors``. It is not a ``ValueError``, so
    that pydantic does not try the next member of a union with the
    reference: the request phase checked that the output is of ``T``, so the
    data are rejected (see :func:`ess.spec.validation.validated`).
    """

    def __init__(self, ref: OutputRef, errors: list[ErrorDetails]) -> None:
        super().__init__(ref, errors)
        self.ref = ref
        self.errors = errors


class _Referenced:
    """
    The marker of :data:`Referenced`. Validated with the context
    ``{'read': read}``, as the workspace validates a request when it runs, it
    replaces a reference to an output with its data, ``read(ref)``,
    validated as ``T``, and rejects any other value. Data of ``T`` that fail
    a check of ``T``, such as an ``AfterValidator``, raise
    :class:`ReferencedDataInvalid`. Validated without ``read``, as in direct
    use, it accepts ``T`` as it is.
    """

    def __repr__(self) -> str:
        return 'REFERENCED'

    def __get_pydantic_core_schema__(
        self, source: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.with_info_wrap_validator_function(
            functools.partial(_read, source), handler(source)
        )


def _read(
    data_type: Any,
    value: Any,
    validate: ValidatorFunctionWrapHandler,
    info: ValidationInfo,
) -> Any:
    """
    The data of ``value``, a reference to an output, read by the context and
    validated as ``data_type``, the ``T`` of ``Referenced[T]``.
    """
    given = _given(value, info, 'read', OutputRef)
    if given is None:
        return validate(value)
    read, ref = given
    data = read(ref)
    if not isinstance(data, data_type):
        # Data of another member Referenced[...] of a union, which takes them.
        return validate(data)
    try:
        return validate(data)
    except ValidationError as error:
        raise ReferencedDataInvalid(ref, error.errors()) from None


REFERENCED: Final = _Referenced()
"""The marker of :data:`Referenced`, in the metadata of an ``Annotated`` type."""

_T = TypeVar('_T')

Referenced = Annotated[_T, REFERENCED]
"""
``Referenced[T]`` types a field that a reference to an output fills, with data
that fit ``T``, a group, such as ``mask: Referenced[PixelMask] | None``.

It is ``Annotated[T, REFERENCED]``, so pydantic and a type checker see ``T``.
The request type has a reference instead (see :mod:`ess.spec.request_types`).
See ``packages/essspec/docs/params.md`` (References to outputs).
"""

RunFile = Annotated[Path, File()]
"""Types a file field for the raw file of a run, whose picker offers any run."""

SampleRunFile = Annotated[Path, File(role='sample')]
"""Types a file field for the raw file of a run used as the sample."""

BackgroundRunFile = Annotated[Path, File(role='background')]
"""
Types a file field for the raw file of a run used as the background, such as a
SANS can.
"""

TransmissionRunFile = Annotated[Path, File(role='transmission')]
"""Types a file field for the raw file of a run used for a transmission."""

EmptyBeamRunFile = Annotated[Path, File(role='empty_beam')]
"""Types a file field for the raw file of a run used as the empty beam."""

VanadiumRunFile = Annotated[Path, File(role='vanadium')]
"""Types a file field for the raw file of a run used as the vanadium."""

MaskFile = Annotated[Path, File(kind='mask')]
"""Types a file field that a mask fills."""


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
