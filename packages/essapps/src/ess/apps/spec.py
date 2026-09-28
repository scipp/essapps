# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The workflow spec of scipp/ess#690, plus what this framework adds.

Everything a spec author sees comes from :mod:`ess.reduce.spec` and is
re-exported here. The additions are the identity a dataset reference carries,
the exposed intermediates, and the derived models the runner and the
backend validate against. This module imports neither scipp nor sciline.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, get_args, get_origin

from ess.reduce.spec import (
    Array,
    ArraySpec,
    DataField,
    DatasetRef,
    Format,
    NexusFile,
    NoParams,
    OpaqueFile,
    OutputRef,
    Quantity,
    Ref,
    as_ref,
    data_fields,
    ref_fields,
    walk_refs,
)
from ess.reduce.spec import SerializedWorkflowSpec as _SerializedWorkflowSpec
from ess.reduce.spec import WorkflowSpec as _WorkflowSpec
from pydantic import BaseModel, Field, create_model, model_validator

__all__ = [
    'Array',
    'ArraySpec',
    'DataField',
    'DatasetRef',
    'Format',
    'NexusFile',
    'NoParams',
    'OpaqueFile',
    'OutputRef',
    'Quantity',
    'Ref',
    'SerializedWorkflowSpec',
    'SpecId',
    'WorkflowSpec',
    'as_ref',
    'data_field_at',
    'data_fields',
    'dataset_ref',
    'dataset_refs',
    'field_of',
    'literal_model',
    'parse_ref',
    'ref_fields',
    'row_model',
    'schema_columns',
    'schema_data_fields',
    'submodel',
    'walk_refs',
]


IDENTITIES = ('pid', 'uuid', 'sha256')
"""What identifies a dataset, in order of preference."""

STAND_INS = ('run', 'path')
"""What a person may type in place of an identity; resolved at submit."""


def dataset_ref(
    *,
    pid: str | None = None,
    uuid: str | None = None,
    sha256: str | None = None,
    instrument: str | None = None,
    run: int | None = None,
    path: Path | str | None = None,
) -> DatasetRef:
    """
    A dataset reference from the one identity or stand-in given.

    The spec keeps a dataset's identity as one string whose meaning is the
    framework's; this is where the framework gives it one. A dataset has one
    or more of these identities, and a reference by any of them names it: the
    PID of its catalogue entry, ``pid:<pid>``; the UUID its NeXus file carries
    in ``entry/entry_identifier_uuid``, ``uuid:<uuid>``; the sha256 of the
    bytes of a file that carries neither, ``sha256:<hex>``. A new record names
    it by the first it has. A moved file keeps its identity: where the bytes
    are is asked of a dataset source at dispatch.

    The instrument and run number, ``run:<instrument>/<run>``, and a path,
    ``path:<path>``, are stand-ins a person may type. The backend resolves a
    stand-in to the dataset's identity when the request is submitted, and the
    record holds the identity.
    """
    given = {
        'pid': pid is not None,
        'uuid': uuid is not None,
        'sha256': sha256 is not None,
        'instrument and run': instrument is not None or run is not None,
        'path': path is not None,
    }
    forms = [name for name, present in given.items() if present]
    if len(forms) != 1:
        raise ValueError(
            'a dataset reference has exactly one of a pid, a uuid, a sha256, '
            f'an instrument and a run number, or a path; got {forms or "none"}'
        )
    for prefix, value in (('pid', pid), ('uuid', uuid), ('sha256', sha256)):
        if value is not None:
            return DatasetRef(dataset=f'{prefix}:{value}')
    if path is not None:
        return DatasetRef(dataset=f'path:{path}')
    if instrument is None or run is None:
        raise ValueError('an instrument and a run number name a run together')
    return DatasetRef(dataset=f'run:{instrument}/{run}')


_OUTPUT_REF = re.compile(
    r'(?P<record>[^.]+)\.(?P<output>[^.\[]+)(\[(?P<key>[^\]]+)\])?'
)


def parse_ref(text: str) -> Ref:
    """
    The reference ``text`` denotes, the inverse of ``str(ref)``.

    This is how a reference is written on a command line or in a URL: a
    dataset by its identity or a stand-in, see :func:`dataset_ref`;
    an output of a record, ``<record>.<output>``, or one element of a
    collection output, ``<record>.<output>[<key>]``.
    """
    if text.partition(':')[0] in (*IDENTITIES, *STAND_INS):
        return DatasetRef(dataset=text)
    if (match := _OUTPUT_REF.fullmatch(text)) is not None:
        return OutputRef(
            record=match['record'], output=match['output'], key=match['key']
        )
    raise ValueError(f'not a reference: {text!r}')


class SpecId(BaseModel, frozen=True):
    name: str = Field(min_length=1)
    version: int = Field(ge=1)

    def __str__(self) -> str:
        return f'{self.name}/v{self.version}'

    @classmethod
    def parse(cls, text: str) -> SpecId:
        """The spec id ``text`` denotes, the inverse of ``str(spec_id)``."""
        name, sep, version = text.rpartition('/v')
        if not sep or not name or not version.isdigit():
            raise ValueError(f'not a spec id: {text!r}')
        return cls(name=name, version=int(version))


class SerializedWorkflowSpec(_SerializedWorkflowSpec, frozen=True):
    """The plain-data form of scipp/ess#690 with the exposed intermediates."""

    intermediates: tuple[str, ...] = ()

    @property
    def id(self) -> SpecId:
        return SpecId(name=self.name, version=self.version)

    @property
    def results(self) -> tuple[str, ...]:
        """The outputs a plain run computes: those that are not intermediates."""
        names = self.outputs_schema.get('properties', {})
        return tuple(n for n in names if n not in self.intermediates)


class WorkflowSpec(_WorkflowSpec, frozen=True):
    """
    The spec of scipp/ess#690 with the exposed intermediates.

    A spec is the signature of a pipeline: every parameter and every value a
    caller may ask for. ``intermediates`` names the outputs that are not results
    of a plain run but values inside the pipeline, such as a detector image,
    which a request computes only when it names them. What depends on what is
    known only to the binding.
    """

    intermediates: tuple[str, ...] = Field(
        default=(),
        description="Outputs that a plain run does not compute; a request "
        "computes them when it names them.",
    )

    @model_validator(mode='after')
    def _intermediates_are_outputs(self) -> WorkflowSpec:
        unknown = sorted(set(self.intermediates) - set(self.outputs.model_fields))
        if unknown:
            raise ValueError(f'intermediates {unknown} are not outputs')
        return self

    @property
    def id(self) -> SpecId:
        return SpecId(name=self.name, version=self.version)

    @property
    def results(self) -> tuple[str, ...]:
        """The outputs a plain run computes: those that are not intermediates."""
        return tuple(
            n for n in self.outputs.model_fields if n not in self.intermediates
        )

    def serialize(self) -> SerializedWorkflowSpec:
        return SerializedWorkflowSpec(
            **super().serialize().model_dump(), intermediates=self.intermediates
        )


def submodel(
    model: type[BaseModel], names: Iterable[str], suffix: str
) -> type[BaseModel]:
    """A model of the named fields of ``model``, with their annotations intact."""
    fields = {
        name: (model.model_fields[name].annotation, model.model_fields[name])
        for name in names
    }
    return create_model(f'{model.__name__}{suffix}', **fields)


def literal_model(model: type[BaseModel]) -> type[BaseModel]:
    """
    The fields of ``model`` that hold values rather than references to data.

    The runner validates what a callable returns through this: the data fields it
    returns are objects, checked against their ``ArraySpec`` and stored, and the
    rest are literals the record keeps inline.
    """
    data = data_fields(model)
    return submodel(model, [n for n in model.model_fields if n not in data], 'Literals')


def field_of(path: str) -> str:
    """The parameter field a reference path belongs to: ``banks.a`` is ``banks``."""
    return path.split('.')[0].split('[')[0]


def row_model(model: type[BaseModel], name: str) -> type[BaseModel] | None:
    """
    The model of each element of a list parameter of rows, or None.

    A row is one member of a sum whose member table has several columns, such
    as a run and its own transmission run; its fields are the columns.
    """
    annotation = model.model_fields[name].annotation
    if get_origin(annotation) is not list:
        return None
    (item,) = get_args(annotation)
    return item if isinstance(item, type) and issubclass(item, BaseModel) else None


def schema_columns(schema: dict[str, Any], name: str) -> tuple[str, ...] | None:
    """
    The columns of a list parameter of rows in a serialized params schema, or
    None: :func:`row_model` for a spec known by its schema only.
    """
    items = schema.get('properties', {}).get(name, {}).get('items', {})
    prefix, ref = '#/$defs/', items.get('$ref', '')
    if not ref.startswith(prefix):
        return None
    return tuple(schema['$defs'][ref[len(prefix) :]].get('properties', {}))


_ROW_PATH = re.compile(r'(?P<field>[^.\[]+)\[\d+\]\.(?P<column>.+)')


def data_field_at(model: type[BaseModel], path: str) -> DataField | None:
    """
    The data field a reference at ``path`` fills, or None for a literal field.

    ``path`` is as :func:`walk_refs` writes it. A reference in a row of a list
    parameter, ``runs[0].run``, fills the ``run`` field of the row model.
    """
    match = _ROW_PATH.fullmatch(path)
    if match is not None and (row := row_model(model, match['field'])) is not None:
        return data_field_at(row, match['column'])
    return data_fields(model).get(field_of(path))


def schema_data_fields(schema: dict[str, Any]) -> dict[str, DataField]:
    """
    The data fields of a serialized params or outputs schema, by name.

    Reads the ``dataField`` key ess.reduce writes into a property's JSON
    schema: on the property itself for a plain field, or on ``items`` or
    ``additionalProperties`` for a list or dict of one declared type.
    """
    fields = {}
    for name, prop in schema.get('properties', {}).items():
        nested = prop.get('items', prop.get('additionalProperties', {}))
        found = prop.get('dataField') or (
            nested.get('dataField') if isinstance(nested, dict) else None
        )
        if found is not None:
            fields[name] = DataField(
                format=Format(found['format']),
                array=ArraySpec(**found['array']) if 'array' in found else None,
            )
    return fields


def dataset_refs(params: Any) -> list[DatasetRef]:
    """
    The distinct datasets a plain value names; never pending.

    Two parameters may name one dataset -- a run that is both the background
    transmission and the empty beam -- which is one origin of a run and one file
    to locate and hash.
    """
    refs: Iterator[DatasetRef] = (
        ref for _, ref in walk_refs(params) if isinstance(ref, DatasetRef)
    )
    return list(dict.fromkeys(refs))
