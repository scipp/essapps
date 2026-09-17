# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Workflow specifications and the parameter vocabulary.

Mirrors the shape proposed in scipp/ess#690 (identity, one params model, declared
outputs, data-reference fields, collections on both sides) with the extensions the
architecture sketch needs under D13 and D15: dataset identities, a contribution
output, and the parameters finalize reads. Once scipp/ess#690 merges, this module
shrinks to the extensions.

The spec is pure interface: no factory, no sciline keys. Binding a spec to code is
in :mod:`ess.apps.binding`. This module imports neither scipp nor sciline.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import UnionType
from typing import Annotated, Any, Union, get_args, get_origin

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    create_model,
    model_validator,
)


class NoParams(BaseModel):
    """Params model for workflows that take no configuration."""

    model_config = ConfigDict(extra='forbid')


class Ref(BaseModel, frozen=True, extra='forbid'):
    """Output ``output`` of record ``record``, optionally one element ``key`` of it."""

    record: str
    output: str
    key: str | None = None

    def __str__(self) -> str:
        key = f'[{self.key}]' if self.key is not None else ''
        return f'{self.record}.{self.output}{key}'


class DatasetRef(BaseModel, frozen=True, extra='forbid'):
    """
    Identity of data the framework did not compute: the second form of reference.

    Exactly one identity is set: the PID of a catalogue dataset; the instrument
    and run number a local file carries, which is what a PID is minted from; or
    its path when it carries neither, the one case where a path is an identity.
    Identity is not location: where the bytes are is asked of a dataset source at
    dispatch (D7). A dataset reference is never pending and has no record.
    """

    pid: str | None = None
    instrument: str | None = None
    run: int | None = None
    path: Path | None = None

    @model_validator(mode='after')
    def _one_identity(self) -> DatasetRef:
        forms = {
            'pid': self.pid is not None,
            'instrument and run': self.instrument is not None or self.run is not None,
            'path': self.path is not None,
        }
        given = [name for name, present in forms.items() if present]
        if len(given) != 1:
            raise ValueError(
                'a dataset has exactly one identity, a pid, an instrument and a '
                f'run number, or a path; got {given or "none"}'
            )
        if given == ['instrument and run'] and None in (self.instrument, self.run):
            raise ValueError('an instrument and a run number identify a file together')
        return self

    def __str__(self) -> str:
        if self.pid is not None:
            return f'dataset:{self.pid}'
        if self.run is not None:
            return f'dataset:{self.instrument}/{self.run}'
        return f'dataset:{self.path}'


Reference = Ref | DatasetRef
"""What a parameter field of matching type may hold instead of a literal."""


class Format(StrEnum):
    """What the bytes of a data reference are."""

    NEXUS = 'nexus'
    """A raw NeXus file."""
    SCIPP = 'scipp'
    """A scipp object, held in memory or as scipp HDF5; structure by ArraySpec."""
    OPAQUE = 'opaque'
    """A file of a format the framework does not read, such as CIF or ORSO."""


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

    Such a field holds a :class:`Ref` or a :class:`DatasetRef`, in a request and
    inside the callable alike; the annotation says what the referenced bytes are,
    so that the backend can check a reference against the field it fills and a
    picker can list candidates. How the callable gets at the bytes is not the
    spec's concern: it asks the runner for a path or an object (D8).
    """

    format: Format
    array: ArraySpec | None = None

    def __get_pydantic_json_schema__(self, core_schema: Any, handler: Any) -> Any:
        schema = handler(core_schema)
        schema['dataRef'] = {'format': self.format.value}
        if self.array is not None:
            schema['dataRef']['array'] = self.array.model_dump()
        return schema


NexusFile = Annotated[Reference, DataRef(format=Format.NEXUS)]
"""A raw NeXus file."""
OpaqueFile = Annotated[Reference, DataRef(format=Format.OPAQUE)]
"""A file the framework cannot read."""


def Array(spec: ArraySpec | None = None) -> Any:
    """Type of a field referencing a scipp object with the structure ``spec``."""
    return Annotated[Reference, DataRef(format=Format.SCIPP, array=spec)]


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
    types match. ``contribution`` marks the output a combine request combines, and
    ``finalize_params`` the parameters the finalize stage reads (D15).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str = Field(min_length=1)
    version: int = Field(ge=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    params: type[BaseModel] = NoParams
    outputs: type[BaseModel]
    code_revision: str | None = Field(
        default=None, description="Git commit or package version of the workflow code."
    )
    contribution: str | None = Field(
        default=None,
        description="Output field holding the value at the accumulation keys (D15).",
    )
    finalize_params: frozenset[str] = Field(
        default=frozenset(),
        description="Parameters finalize reads; every other parameter is contribute's.",
    )

    @model_validator(mode='after')
    def _contribution_is_declared(self) -> WorkflowSpec:
        if self.contribution is not None:
            if self.contribution not in self.outputs.model_fields:
                raise ValueError(f'no output named {self.contribution!r}')
            # A member run produces the contribution alone, and its outputs are
            # validated against the full model, so the rest must be optional.
            required = sorted(
                name
                for name, field in self.outputs.model_fields.items()
                if name != self.contribution and field.is_required()
            )
            if required:
                raise ValueError(
                    f'a member run produces only {self.contribution!r}, so the '
                    f'outputs {required} must be optional'
                )
        if self.finalize_params and self.contribution is None:
            raise ValueError('finalize parameters without a contribution output')
        unknown = self.finalize_params - set(self.params.model_fields)
        if unknown:
            raise ValueError(f'no parameters named {sorted(unknown)}')
        return self

    @property
    def id(self) -> SpecId:
        return SpecId(name=self.name, version=self.version)

    @property
    def contribute_params(self) -> frozenset[str]:
        """Parameters contribute reads: every one finalize does not (D15)."""
        return frozenset(self.params.model_fields) - self.finalize_params


def finalize_model(spec: WorkflowSpec) -> type[BaseModel]:
    """
    The parameter model of a combine request: the fields finalize reads.

    A combine request carries these and no others, so it is validated against a
    model of exactly them, and a combine form asks the spec for the same thing.
    """
    declared = spec.params.model_fields
    fields = {
        name: (declared[name].annotation, declared[name])
        for name in sorted(spec.finalize_params)
    }
    return create_model(f'{spec.params.__name__}Finalize', **fields)


def literal_model(model: type[BaseModel]) -> type[BaseModel]:
    """
    The fields of ``model`` that hold values rather than references to data.

    The runner validates what a callable returns through this: the data fields it
    returns are objects, checked against their ``ArraySpec`` and stored, and the
    rest are literals the record keeps inline.
    """
    data = data_ref_fields(model)
    fields = {
        name: (field.annotation, field)
        for name, field in model.model_fields.items()
        if name not in data
    }
    return create_model(f'{model.__name__}Literals', **fields)


def _members(annotation: Any) -> Iterator[Any]:
    """The annotation and, through unions, optionals, and collections, its parts."""
    yield annotation
    origin = get_origin(annotation)
    if origin is Annotated:
        yield from _members(get_args(annotation)[0])
    elif origin in (Union, UnionType):
        for arg in get_args(annotation):
            yield from _members(arg)
    elif origin is list:
        yield from _members(get_args(annotation)[0])
    elif origin is dict:
        yield from _members(get_args(annotation)[1])


def _data_ref(annotation: Any) -> DataRef | None:
    """The DataRef annotation anywhere in a field's type."""
    for member in _members(annotation):
        if get_origin(member) is Annotated:
            for metadata in get_args(member)[1:]:
                if isinstance(metadata, DataRef):
                    return metadata
    return None


def data_ref_fields(model: type[BaseModel]) -> dict[str, DataRef]:
    """
    Data-reference fields of a model by name.

    Optional fields and collections of references count; every element of a
    collection shares the annotation.
    """
    fields = {}
    for name, field in model.model_fields.items():
        ref = next((m for m in field.metadata if isinstance(m, DataRef)), None)
        if ref is None:
            ref = _data_ref(field.annotation)
        if ref is not None:
            fields[name] = ref
    return fields


def ref_fields(model: type[BaseModel]) -> set[str]:
    """Fields that may hold a reference: data fields and literal-or-reference unions."""
    return {
        name
        for name, field in model.model_fields.items()
        if name in data_ref_fields(model)
        or any(member in (Ref, DatasetRef) for member in _members(field.annotation))
    }


def as_ref(value: Any) -> Reference | None:
    """
    The reference a plain value denotes, if it is one: the one place that decides.

    A reference is a model, or the dict it dumps to once a request has been
    through the store. The two forms are told apart by their fields; a dict whose
    fields could be a dataset identity but do not validate as one, such as the
    params of a workflow with a ``run`` parameter, is not a reference.
    """
    if isinstance(value, Ref | DatasetRef):
        return value
    if isinstance(value, dict):
        if 'record' in value and set(value) <= _REF_KEYS:
            return Ref.model_validate(value)
        if value and set(value) <= _DATASET_KEYS:
            with contextlib.suppress(ValidationError):
                return DatasetRef.model_validate(value)
    return None


_REF_KEYS = frozenset(Ref.model_fields)
_DATASET_KEYS = frozenset(DatasetRef.model_fields)


def field_of(path: str) -> str:
    """The parameter field a reference path belongs to: ``banks.a`` is ``banks``."""
    return path.split('.')[0].split('[')[0]


def dataset_refs(params: Any) -> list[DatasetRef]:
    """
    The distinct datasets a plain value names; never pending.

    Two parameters may name one dataset -- a run that is both the background
    transmission and the empty beam -- which is one origin of a run and one file
    to locate and hash.
    """
    refs = (ref for _, ref in walk_refs(params) if isinstance(ref, DatasetRef))
    return list(dict.fromkeys(refs))


def walk_refs(value: Any, path: str = '') -> Iterator[tuple[str, Reference]]:
    """Yield every reference in a plain (JSON-shaped) value, with its path."""
    if (ref := as_ref(value)) is not None:
        yield path, ref
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from walk_refs(v, f'{path}.{k}' if path else str(k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from walk_refs(v, f'{path}[{i}]')
