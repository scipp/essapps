# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Shared vocabulary of workflow parameter models.

Common building blocks for workflow params models: the markers of a field
that a file fills and of a field that a reference fills, the types of file
fields of common kinds, constrained unit choices, and range/edges models with
cross-field validation. Purely declarative — no scipp; converting validated
values into scipp objects is the workflow implementation's concern (see
:mod:`ess.spec.conversions`).

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
    The marker of a field that a file fills, a *file field*, typed
    ``Annotated[Path, File(**metadata)]``; it holds the metadata that a file
    must have to fill the field, each a string, a number, or a bool. A file
    fits the field if its metadata have each key and value of the marker's
    :attr:`metadata`; ``kind`` is ``'raw'`` unless given. This module types
    the file fields of common kinds, :data:`RunFile` and :data:`MaskFile`,
    so that each kind is spelled once; a file field of other metadata is
    typed where it is used::

        class IofQParams(BaseModel):
            sample: RunFile
            mask: MaskFile | None = None

    pydantic, a type checker, and the binding see ``Path``: the params model
    holds the local path of the file. A request writes a file as
    ``file(...)``, whose JSON form is ``{'@ref': 'file', 'file': ...}``, so
    the request type has a :class:`~ess.spec.refs.FileRef` in the field's
    place (see :mod:`ess.spec.request_types`). The backend resolves the name
    to the file's identity when the request is submitted, with a resolver of
    files given the marker, and the file source checks that the file is
    known, that the request's proposal may read it, and that its metadata
    fit the marker's.

    Validated with the context ``{'paths': {identity: local path}}``, as the
    backend validates a request, the marker replaces the stored form of a
    file by its local path, validated as ``Path``, and refuses any other
    value. Validated without ``paths``, as in direct use, the field takes a
    ``Path`` as it is, ``IofQParams(sample=Path('run-101.nxs'))``.

    The marker carries no validators: one that reads the file would read it
    from a network mount when the request is submitted. A check of the
    content of a file belongs to the workflow. Two markers are equal if
    their metadata are, each value of the same type, so that ``File(n=1)``
    and ``File(n=True)`` are two markers.
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
        """The metadata that a file must have to fill the field."""
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
    direct use. Refuses a value of another source (see :func:`not_its_source`).
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
            'file_ref', f'{value!r} is not a file; a file field is written as file(...)'
        )
    return PydanticCustomError(
        'output_ref',
        'is not a reference to an output, as the field is typed Referenced[...]',
    )


class DataRefused(Exception):
    """
    The data read for ``ref``, a reference to an output, fail a check of the
    ``T`` of ``Referenced[T]``, with ``errors``. It is not a ``ValueError``, so
    that pydantic does not try the next member of a union with the
    reference: the request phase checked that the output is of ``T``, so the
    data are refused (see :func:`ess.spec.request_types.validated`).
    """

    def __init__(self, ref: OutputRef, errors: list[ErrorDetails]) -> None:
        super().__init__(ref, errors)
        self.ref = ref
        self.errors = errors


class _Referenced:
    """
    The marker of :data:`Referenced`. Validated with the context
    ``{'read': read}``, as the backend validates a request when it runs, it
    replaces a reference to an output with its data, ``read(ref)``,
    validated as ``T``, and refuses any other value. Data of ``T`` that fail
    a check of ``T``, such as an ``AfterValidator``, raise
    :class:`DataRefused`. Validated without ``read``, as in direct use, it
    takes ``T`` as it is.
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
        raise DataRefused(ref, error.errors()) from None


REFERENCED: Final = _Referenced()
"""The marker of :data:`Referenced`, in the metadata of an ``Annotated`` type."""

_T = TypeVar('_T')

Referenced = Annotated[_T, REFERENCED]
"""
``Referenced[T]`` types a field that a reference to an output fills, whose
data fit ``T``, a group. A field that takes a value written inline too is a
union::

    class IofQParams(BaseModel):
        mask: Referenced[PixelMask] | None = None
        centre: BeamCentre | Referenced[BeamCentre] = BeamCentre()

    class Sample(BaseModel):
        run: RunFile
        transmission: Referenced[Transmission]

It is ``Annotated[T, REFERENCED]``, so pydantic and a type checker see
``T``: the params model holds the data, as the binding takes them. The
request type has a reference instead (see :mod:`ess.spec.request_types`).
The rules are stated in :mod:`ess.spec._workflow_spec`.
"""

RunFile = Annotated[Path, File()]
"""Types a file field that the raw file of a run fills."""

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
