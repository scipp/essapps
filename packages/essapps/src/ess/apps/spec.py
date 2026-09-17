# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The workflow spec of scipp/ess#690, plus what the architecture sketch adds.

Everything a spec author sees comes from :mod:`ess.reduce.spec` and is
re-exported here. The additions are the identity a dataset reference carries
(D1), the contribution output and the parameters finalize reads (D15), and the
derived models the runner and the backend validate against. This module imports
neither scipp nor sciline.
"""

from __future__ import annotations

from collections.abc import Iterator
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
    'finalize_model',
    'literal_model',
    'ref_fields',
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
    A dataset reference from the one identity given (D1).

    The spec keeps a dataset's identity as one string whose meaning is the
    framework's; this is where the framework gives it one. The PID of a
    catalogue dataset, ``pid:<pid>``; the instrument and run number a local
    file carries, which is what a PID is minted from, ``run:<instrument>/<run>``;
    or its path when it carries neither, the one case where a path is an
    identity, ``path:<path>``. Identity is not location: where the bytes are is
    asked of a dataset source at dispatch (D7).
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


class SerializedWorkflowSpec(_SerializedWorkflowSpec, frozen=True):
    """The plain-data form of scipp/ess#690 with the D15 declaration."""

    contribution: str | None = None
    finalize_params: frozenset[str] = frozenset()


class WorkflowSpec(_WorkflowSpec, frozen=True):
    """
    The spec of scipp/ess#690 with the declared additive combine (D15).

    ``contribution`` marks the output a combine request combines, and
    ``finalize_params`` the parameters the finalize stage reads. Both are read
    by the backend, which cannot import workflow code, which is what earns them
    a place on the spec.
    """

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

    def serialize(self) -> SerializedWorkflowSpec:
        return SerializedWorkflowSpec(
            **super().serialize().model_dump(),
            contribution=self.contribution,
            finalize_params=self.finalize_params,
        )

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
    data = data_fields(model)
    fields = {
        name: (field.annotation, field)
        for name, field in model.model_fields.items()
        if name not in data
    }
    return create_model(f'{model.__name__}Literals', **fields)


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
