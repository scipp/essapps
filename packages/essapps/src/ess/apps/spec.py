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
from typing import Any

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
    'data_fields',
    'dataset_path',
    'dataset_ref',
    'dataset_refs',
    'field_of',
    'literal_model',
    'parse_ref',
    'ref_fields',
    'schema_data_fields',
    'submodel',
    'walk_refs',
]


def dataset_ref(
    *,
    pid: str | None = None,
    instrument: str | None = None,
    run: int | None = None,
    path: Path | str | None = None,
) -> DatasetRef:
    """
    A dataset reference from the one identity given.

    The spec keeps a dataset's identity as one string whose meaning is the
    framework's; this is where the framework gives it one. The PID of a
    catalogue dataset, ``pid:<pid>``; the instrument and run number a local
    file carries, which is what a PID is minted from, ``run:<instrument>/<run>``;
    or its path when it carries neither, the one case where a path is an
    identity, ``path:<path>``. Identity is not location: where the bytes are is
    asked of a dataset source at dispatch.
    """
    given = {
        'pid': pid is not None,
        'instrument and run': instrument is not None or run is not None,
        'path': path is not None,
    }
    forms = [name for name, present in given.items() if present]
    if len(forms) != 1:
        raise ValueError(
            'a dataset has exactly one identity, a pid, an instrument and a run '
            f'number, or a path; got {forms or "none"}'
        )
    if pid is not None:
        return DatasetRef(dataset=f'pid:{pid}')
    if path is not None:
        return DatasetRef(dataset=f'path:{path}')
    if instrument is None or run is None:
        raise ValueError('an instrument and a run number identify a file together')
    return DatasetRef(dataset=f'run:{instrument}/{run}')


def dataset_path(ref: DatasetRef) -> Path | None:
    """The path of a dataset identified by one, the inverse of ``dataset_ref``."""
    prefix = 'path:'
    return Path(ref.dataset[len(prefix) :]) if ref.dataset.startswith(prefix) else None


_OUTPUT_REF = re.compile(
    r'(?P<record>[^.]+)\.(?P<output>[^.\[]+)(\[(?P<key>[^\]]+)\])?'
)


def parse_ref(text: str) -> Ref:
    """
    The reference ``text`` denotes, the inverse of ``str(ref)``.

    This is how a reference is written on a command line or in a URL: a
    dataset by its identity, ``pid:...``, ``run:.../...``, or ``path:...``;
    an output of a record, ``<record>.<output>``, or one element of a
    collection output, ``<record>.<output>[<key>]``.
    """
    if text.startswith(('pid:', 'run:', 'path:')):
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
