# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Shared vocabulary of workflow parameter models.

Common building blocks for workflow params models: the file model, the
marker of a place that a reference fills, constrained unit choices, and
range/edges models with cross-field validation. Purely declarative — no scipp;
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

import inspect
from abc import ABC
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, ClassVar, Final, Self, TypeVar

from pydantic import (
    BaseModel,
    Field,
    PrivateAttr,
    ValidationInfo,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic.json_schema import GetJsonSchemaHandler, JsonSchemaValue
from pydantic_core import CoreSchema, PydanticCustomError

from .refs import FileRef


class FileModel(BaseModel, frozen=True, extra='forbid'):
    """
    A file that a workflow reads: its identity ``file``, and when the
    request runs, its local path :attr:`path`.

    A subclass is a kind of file. Its class keywords are the metadata that a
    file must have to fit the model, each a string, a number, or a bool::

        class MaskFile(FileModel, kind='mask'):
            '''A pixel mask, as XML.'''

    A file fits the model if its metadata have each key and value of the
    model's ``metadata``; a file without ``kind`` is raw. ``metadata`` is
    ``{'kind': 'raw'}`` unless a subclass says otherwise: a subclass with
    keywords merges them over its parent's metadata, one without inherits
    them. A keyword that is a key of pydantic's ``ConfigDict``, such as
    ``title``, ``frozen``, or ``extra``, configures the model instead, so it
    cannot be a metadata key. A subclass adds no fields and no validators
    (see the rules in :mod:`ess.spec._workflow_spec`).

    A request writes a file as ``file(...)``. Its JSON form, which the model
    dumps to and is validated from, is ``{'@ref': 'file', 'file': ...}``, so
    that code reading a record finds its files without the spec (see
    :mod:`ess.spec.refs`). Two file models are equal if they are of one
    class and name one file.

    A request names a file by what a person knows, such as a run number. The
    backend resolves the name to the file's identity when the request is
    submitted, by validating the model with the context
    ``{'files': resolve}``::

        def resolve(name: str, model: type[FileModel]) -> str:
            ...  # the identity of the file, or raise ValueError

        MaskFile.model_validate(
            {'@ref': 'file', 'file': 'run:101'}, context={'files': resolve}
        )

    ``resolve`` raises ``ValueError`` if the file is unknown, if the
    request's proposal may not read it, or if it does not fit the model; its
    message is that of the validation error. Validated from JSON, as a
    request is, a file model takes only ``{'@ref': 'file', ...}``. Validated
    in Python, it also takes ``{'file': ...}``, as ``MaskFile(file=...)``
    gives it.

    The local path of the file exists only when the request runs: the
    backend validates the model with the context ``{'paths': {identity:
    local path}}``, and :attr:`path` raises before. A program without a
    backend makes a file model of a local file with :meth:`local`.
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
    def _of_ref(cls, value: Any, info: ValidationInfo) -> Any:
        if isinstance(value, FileRef):
            value = {'file': value.file}
        elif isinstance(value, Mapping) and value.get('@ref') == 'file':
            value = {k: v for k, v in value.items() if k != '@ref'}
        elif not (
            info.mode == 'python'
            and isinstance(value, Mapping)
            and value.keys() == {'file'}
        ):
            raise PydanticCustomError(
                'file_ref',
                f'{value!r} is not a file; {cls.__name__} is a file model, '
                'written as file(...)',
            )
        resolve = (info.context or {}).get('files')
        if resolve is not None and isinstance(value.get('file'), str):
            try:
                value = {**value, 'file': resolve(value['file'], cls)}
            except ValueError as error:
                raise PydanticCustomError('file', str(error)) from None
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

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        """
        The JSON schema of a file as a request writes it, that of its
        :class:`~ess.spec.refs.FileRef`, titled by the model, with its name
        under ``dataType`` and its metadata under ``metadata``, which say
        which files fit.
        """
        schema = {**handler(FileRef.__pydantic_core_schema__), 'title': cls.__name__}
        schema.pop('description', None)
        if doc := cls.__dict__.get('__doc__'):
            schema['description'] = inspect.cleandoc(doc)
        return {**schema, 'dataType': cls.__name__, 'metadata': cls.metadata}

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FileModel):
            return NotImplemented
        return type(self) is type(other) and self.file == other.file

    def __hash__(self) -> int:
        return hash((type(self), self.file))


_SCALARS = (str, int, float, bool)


class _Referenced:
    def __repr__(self) -> str:
        return 'REFERENCED'


REFERENCED: Final = _Referenced()
"""The marker of :data:`Referenced`, in the metadata of an ``Annotated`` type."""

_T = TypeVar('_T')

Referenced = Annotated[_T, REFERENCED]
"""
``Referenced[T]`` marks a place, a parameter or a row, that a reference to
an output fills, whose data fit ``T``, a group. A place that takes a value
written inline too is a union::

    class IofQParams(BaseModel):
        mask: Referenced[PixelMask] | None = None
        centre: BeamCentre | Referenced[BeamCentre] = BeamCentre()

It is ``Annotated[T, REFERENCED]``, so pydantic and a type checker see
``T``: the params model holds the data, as the binding takes them. The
request type has a reference in its place (see
:mod:`ess.spec.request_types`). The rules are stated in
:mod:`ess.spec._workflow_spec`.
"""


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
