# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Workflow specifications and the parameter vocabulary.

Mirrors the shape proposed in scipp/ess#690 (identity, one params model, declared
outputs) with the extensions the architecture sketch needs under D13: outputs are a
model in the same vocabulary as parameters, a data-reference field type marks the
fields that hold data rather than literals, and collections of either are allowed on
both sides. Once scipp/ess#690 merges, this module shrinks to the extensions.

The spec is pure interface: no factory, no sciline keys. Binding a spec to code is
in :mod:`ess.apps.binding`. This module imports neither scipp nor sciline.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field
from pydantic.fields import FieldInfo


class NoParams(BaseModel):
    """Params model for workflows that take no configuration."""

    model_config = ConfigDict(extra='forbid')


class Ref(BaseModel, frozen=True):
    """Output ``output`` of record ``record``, optionally one element ``key`` of it."""

    record: str
    output: str
    key: str | None = None

    def __str__(self) -> str:
        key = f'[{self.key}]' if self.key is not None else ''
        return f'{self.record}.{self.output}{key}'


class Kind(StrEnum):
    """How a data reference is materialized for the workflow callable."""

    NEXUS = 'nexus'
    """A raw NeXus file; the callable receives a local path."""
    OPAQUE = 'opaque'
    """A file of a format the framework does not know; a local path."""
    ARRAY = 'array'
    """A scipp object; served from memory or scipp HDF5."""


class ArraySpec(BaseModel, frozen=True):
    """Structural description of an array: dims, units, whether it is binned."""

    dims: tuple[str, ...] = Field(description="Dimension names, outermost first.")
    unit: str | None = None
    coords: dict[str, str | None] = Field(default_factory=dict)
    binned: bool = Field(
        default=False, description="Event data; never viewed directly."
    )


@dataclass(frozen=True)
class DataRef:
    """
    Field annotation marking a data-reference field.

    In a request such a field holds a :class:`Ref`; when the callable runs it holds
    the materialized value, a path for files or a scipp object for arrays. The
    union type on the field admits both forms; this annotation says which one the
    framework must produce.
    """

    kind: Kind
    array: ArraySpec | None = None

    def __get_pydantic_json_schema__(self, core_schema: Any, handler: Any) -> Any:
        schema = handler(core_schema)
        schema['dataRef'] = {'kind': self.kind.value}
        if self.array is not None:
            schema['dataRef']['array'] = self.array.model_dump()
        return schema


NexusFile = Annotated[Ref | Path, DataRef(kind=Kind.NEXUS)]
OpaqueFile = Annotated[Ref | Path | bytes, DataRef(kind=Kind.OPAQUE)]
"""A file the framework cannot read; a workflow returns one as bytes."""


def Array(spec: ArraySpec | None = None) -> Any:
    """Type of a field holding a scipp object, constrained by ``spec`` if given."""
    return Annotated[Ref | Any, DataRef(kind=Kind.ARRAY, array=spec)]


class Quantity(BaseModel, frozen=True):
    """A scalar or short vector with a unit; the small-value type outputs may use."""

    value: float | tuple[float, ...]
    unit: str | None = None


class SpecId(BaseModel, frozen=True):
    name: str = Field(min_length=1)
    version: int = Field(ge=1)

    def __str__(self) -> str:
        return f'{self.name}/v{self.version}'


class WorkflowSpec(BaseModel, frozen=True):
    """
    Implementation-independent description of a workflow's interface.

    ``params`` and ``outputs`` are pydantic model classes over one vocabulary, so an
    output field of one spec can feed a parameter field of another when their
    types match. ``cheap`` names the parameters a warm workflow can change without
    recomputing the expensive part; it is what lets a UI offer a slider.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str = Field(min_length=1)
    version: int = Field(ge=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    params: type[BaseModel] = NoParams
    outputs: type[BaseModel]
    cheap: frozenset[str] = frozenset()
    code_revision: str | None = Field(
        default=None, description="Git commit or package version of the workflow code."
    )

    @property
    def id(self) -> SpecId:
        return SpecId(name=self.name, version=self.version)


def data_ref(field: FieldInfo) -> DataRef | None:
    """The :class:`DataRef` annotation of a field, if it is a data-reference field."""
    return next((m for m in field.metadata if isinstance(m, DataRef)), None)


def _element_type(annotation: Any) -> Any:
    """The element type of a list or dict annotation, else None."""
    origin = get_origin(annotation)
    if origin is list:
        return get_args(annotation)[0]
    if origin is dict:
        return get_args(annotation)[1]
    return None


def _annotation_data_ref(annotation: Any) -> DataRef | None:
    if get_origin(annotation) is Annotated:
        return next(
            (m for m in get_args(annotation)[1:] if isinstance(m, DataRef)), None
        )
    return None


def data_ref_fields(model: type[BaseModel]) -> dict[str, DataRef]:
    """
    Data-reference fields of a model by name.

    A collection field whose elements are data references counts as one; every
    element shares the annotation.
    """
    found: dict[str, DataRef] = {}
    for name, field in model.model_fields.items():
        ref = data_ref(field)
        if ref is None and (element := _element_type(field.annotation)) is not None:
            ref = _annotation_data_ref(element)
        if ref is not None:
            found[name] = ref
    return found


def walk_refs(value: Any, path: str = '') -> Iterator[tuple[str, Ref]]:
    """Yield every :class:`Ref` in a plain (JSON-shaped) value, with its path."""
    if isinstance(value, Ref):
        yield path, value
    elif isinstance(value, dict):
        if set(value) <= {'record', 'output', 'key'} and 'record' in value:
            yield path, Ref.model_validate(value)
        else:
            for k, v in value.items():
                yield from walk_refs(v, f'{path}.{k}' if path else str(k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from walk_refs(v, f'{path}[{i}]')


class LocalOrigin(BaseModel, frozen=True):
    """A file on a user's disk; the checksum is taken the first time it is read."""

    path: Path
    checksum: str | None = None


class PidOrigin(BaseModel, frozen=True):
    """A catalogue dataset."""

    pid: str


class FileParams(BaseModel, frozen=True):
    origin: LocalOrigin | PidOrigin


class FileOutputs(BaseModel):
    file: OpaqueFile


FILE_SPEC = WorkflowSpec(
    name='file',
    version=1,
    title='File',
    description='A file record: no workflow, one output, the file.',
    params=FileParams,
    outputs=FileOutputs,
)
