# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The workflow spec of scipp/ess#690, plus what this framework adds.

Everything a spec author sees comes from :mod:`ess.reduce.spec` and is
re-exported here. The additions are the identity a dataset reference carries,
the declared additive combine, and the derived models the runner and the
backend validate against. This module imports neither scipp nor sciline.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from types import UnionType
from typing import Annotated, Any, Union, get_args, get_origin

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
    'ref_fields',
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


class SpecId(BaseModel, frozen=True):
    name: str = Field(min_length=1)
    version: int = Field(ge=1)

    def __str__(self) -> str:
        return f'{self.name}/v{self.version}'


def _is_collection(annotation: Any) -> bool:
    """Whether the annotation is a ``list`` or a ``dict``, through unions."""
    origin = get_origin(annotation)
    if origin is Annotated:
        return _is_collection(get_args(annotation)[0])
    if origin in (Union, UnionType):
        return any(_is_collection(arg) for arg in get_args(annotation))
    return origin in (list, dict)


class SerializedWorkflowSpec(_SerializedWorkflowSpec, frozen=True):
    """The plain-data form of scipp/ess#690 with the declared additive combine."""

    carry: Mapping[str, str] = {}


class WorkflowSpec(_WorkflowSpec, frozen=True):
    """
    The spec of scipp/ess#690 with the declared additive combine.

    A spec is the signature of one callable and a record one call of it, so an
    aggregation over runs is two specs, a contribute spec and a combine spec,
    and nothing about the split is declared here. What is declared is ``carry``:
    that an output of one run may be carried back as an element of a collection
    parameter of a later run of the same spec, where it stands for all the
    elements it was combined from.
    """

    carry: Mapping[str, str] = Field(
        default_factory=dict,
        description="Collection parameter -> output whose value may be carried back "
        "as one of its elements, where it stands for everything it combined.",
    )

    @model_validator(mode='after')
    def _carry_is_a_collection_of_the_output(self) -> WorkflowSpec:
        """
        What the spec can check on its own: the shapes at the two ends.

        That the combination does not depend on grouping or order is a property
        of the code, which no spec can check;
        :func:`ess.apps.testing.assert_combine_is_associative` checks it.
        """
        params = data_fields(self.params)
        outputs = data_fields(self.outputs)
        for parameter, output in self.carry.items():
            if parameter not in self.params.model_fields:
                raise ValueError(f'no parameter named {parameter!r}')
            if parameter not in params or not _is_collection(
                self.params.model_fields[parameter].annotation
            ):
                raise ValueError(
                    f'parameter {parameter!r} is not a collection of data references'
                )
            if output not in outputs:
                raise ValueError(f'no data output named {output!r}')
            if outputs[output].format is not params[parameter].format:
                raise ValueError(
                    f'output {output!r} is {outputs[output].format} and parameter '
                    f'{parameter!r} takes {params[parameter].format}'
                )
        return self

    @property
    def id(self) -> SpecId:
        return SpecId(name=self.name, version=self.version)

    def serialize(self) -> SerializedWorkflowSpec:
        return SerializedWorkflowSpec(
            **super().serialize().model_dump(), carry=dict(self.carry)
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
