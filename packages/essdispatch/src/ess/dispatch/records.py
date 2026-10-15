# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Requests and records: what a user asks for and what the backend made of it.

Both are plain data. A request names a spec, the values of its parameters,
and its inputs, each by reference; a record is the request with every value
filled in, as the backend accepted it. A record never changes. Its status,
which changes once from pending to finished, is asked of the client.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Collection, Mapping
from dataclasses import KW_ONLY, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from ess.spec import (
    AccumulatorRef,
    DatasetRef,
    OutputRef,
    Ref,
    WorkflowSpec,
    as_ref,
    walk_refs,
)


class SubmitError(ValueError):
    """A request that cannot run, refused before any record exists."""


def validation_problems(error: ValidationError) -> list[str]:
    """What a validation error finds wrong, each with its place."""
    return [
        f"{'.'.join(map(str, e['loc'])) or 'params'}: {e['msg']}"
        for e in error.errors()
    ]


def model_of(
    model: type[BaseModel], names: Collection[str], *, rows: bool = False
) -> type[BaseModel]:
    """
    A model of the fields ``names`` of ``model`` alone, with its config and
    without its validators, which read whole requests: it validates each value
    by its own field. A spec's params and inputs models have no field
    validators; a check of one value belongs to the value's type.

    With ``rows``, each field is a column of rows pushed into a table: it is
    validated by its type alone, without its constraints on the whole table,
    such as its length.
    """
    return _model_of(model, frozenset(names), rows)


@functools.cache
def _model_of(
    model: type[BaseModel], names: frozenset[str], rows: bool
) -> type[BaseModel]:
    fields: dict[str, Any] = {
        f: (info.annotation, Field() if rows else info)
        for f, info in model.model_fields.items()
        if f in names
    }
    return create_model(model.__name__, __config__=model.model_config, **fields)


def map_refs(value: Any, fn: Callable[[Any], Any]) -> Any:
    """``value`` with each reference in dicts and lists replaced by ``fn(ref)``."""
    if (ref := as_ref(value)) is not None:
        return fn(ref)
    if isinstance(value, dict):
        return {k: map_refs(v, fn) for k, v in value.items()}
    if isinstance(value, list):
        return [map_refs(v, fn) for v in value]
    return value


def as_values(value: Any) -> Any:
    """
    ``value`` with each model in it, but a reference, as a dict of the fields
    that were given.

    So a request, a template, and a push take a params model or a group as a
    model or as a dict alike. Defaults are left out, as they are of a dict, so
    that the backend fills in those of its own spec.
    """
    if isinstance(value, BaseModel) and not isinstance(value, Ref):
        return value.model_dump(exclude_unset=True)
    if isinstance(value, dict):
        return {k: as_values(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [as_values(v) for v in value]
    return value


def as_refs(value: Any) -> Any:
    """``value`` with references read as references, not dicts."""
    return map_refs(value, lambda ref: ref)


def _given(values: Mapping[str, Any] | BaseModel | None) -> dict[str, Any]:
    """The values of one side of a request, as :func:`as_values` gives them."""
    return {} if values is None else as_values(values)


class SpecId(BaseModel, frozen=True):
    """The identity of a spec: its name and interface version."""

    name: str
    version: int

    @classmethod
    def of(cls, spec: WorkflowSpec[Any, Any, Any] | SpecId | dict[str, Any]) -> SpecId:
        if isinstance(spec, SpecId):
            return spec
        if isinstance(spec, WorkflowSpec):
            return cls(name=spec.name, version=spec.version)
        return cls.model_validate(spec)

    def __str__(self) -> str:
        return f'{self.name}/v{self.version}'


class Request(BaseModel, frozen=True):
    """
    A spec, the values of its parameters, and its inputs, by name.

    ``params`` is a params model or a dict of its fields, each a group as a
    model or a dict. ``inputs`` holds a reference for each input, or a list of
    references for a list field; a file is always given by reference, such as
    ``dataset(path=...)``. Every reference of a request is in its inputs, so
    the references of a request are found without its spec.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    spec: SpecId
    params: dict[str, Any] = Field(default_factory=dict)
    inputs: dict[str, Any] = Field(default_factory=dict)

    def __init__(
        self,
        spec: WorkflowSpec[Any, Any, Any] | SpecId,
        *,
        params: Mapping[str, Any] | BaseModel | None = None,
        inputs: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(
            spec=SpecId.of(spec), params=_given(params), inputs=as_refs(_given(inputs))
        )

    def upstream(self) -> list[OutputRef]:
        """The outputs of other records this request reads."""
        return [ref for _, ref in walk_refs(self.inputs) if isinstance(ref, OutputRef)]

    def accumulators(self) -> list[AccumulatorRef]:
        """
        The outputs of accumulators this request reads; a record's request
        reads none, since submitting replaces each by an output of the record
        of the state it reads.
        """
        return [
            ref for _, ref in walk_refs(self.inputs) if isinstance(ref, AccumulatorRef)
        ]

    def datasets(self) -> list[DatasetRef]:
        """The datasets this request names directly."""
        return [ref for _, ref in walk_refs(self.inputs) if isinstance(ref, DatasetRef)]


@dataclass(frozen=True)
class Template:
    """
    A spec, some values, and blanks that each use fills.

    Values are given by side, as a request's are. A blank is a parameter, an
    input, or a table, named by its index; a template fixes a whole table or
    leaves it blank. Naming a field as a blank drops the value given for it,
    so a template can be made from any request's values or a whole params
    model, which it keeps as a dict. Change a template with
    ``dataclasses.replace``.
    """

    spec: SpecId
    _: KW_ONLY
    params: dict[str, Any] = field(default_factory=dict)
    inputs: dict[str, Any] = field(default_factory=dict)
    blanks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        def kept(values: Mapping[str, Any]) -> dict[str, Any]:
            return {k: v for k, v in values.items() if k not in self.blanks}

        object.__setattr__(self, 'spec', SpecId.of(self.spec))
        object.__setattr__(self, 'params', kept(_given(self.params)))
        object.__setattr__(self, 'inputs', kept(as_refs(_given(self.inputs))))
        object.__setattr__(self, 'blanks', tuple(self.blanks))

    def fill(
        self,
        *,
        params: Mapping[str, Any] | BaseModel | None = None,
        inputs: Mapping[str, Any] | None = None,
    ) -> Request:
        """
        The request with the blanks filled by ``params`` and ``inputs``; a
        blank table by its index and the columns attached to it.
        """
        params, inputs = _given(params), _given(inputs)
        given = params.keys() | inputs.keys()
        if missing := [b for b in self.blanks if b not in given]:
            raise ValueError(f'fill the blanks {self.blanks}, missing {missing}')
        if fixed := sorted(given & (self.params.keys() | self.inputs.keys())):
            raise ValueError(f'{fixed}: fixed by the template, not blanks')
        return Request(
            self.spec,
            params={**self.params, **params},
            inputs={**self.inputs, **inputs},
        )


class Status(StrEnum):
    PENDING = 'pending'
    COMPLETED = 'completed'
    FAILED = 'failed'
    CANCELLED = 'cancelled'


class Record(BaseModel, frozen=True):
    """
    A request as the backend accepted it.

    ``request`` has every value filled in, in JSON's types: dataset names
    resolved, the defaults of params and inputs filled, a column left out of
    a table filled, and each reference to an accumulator replaced by a
    reference to the record of the state it reads, a record of the plain
    request of that state.
    ``outputs`` lists the output names the spec declares; ``label`` and
    ``member`` are given at submission and do not change the result.

    A record never changes, so every copy of it is equal. Its status, which
    changes once from pending to finished, is asked of the client.
    """

    id: str
    request: Request
    proposal: str
    submitter: str
    created: datetime
    outputs: tuple[str, ...]
    label: str | None = None
    member: str | None = None

    @property
    def spec(self) -> SpecId:
        return self.request.spec

    def ref(self, output: str) -> OutputRef:
        if output not in self.outputs:
            raise KeyError(f'{self.spec} has no output {output!r}')
        return OutputRef(record=self.id, output=output)

    def refs(self, *outputs: str) -> dict[str, OutputRef]:
        """A reference to each of ``outputs`` by name; to every output if none."""
        return {name: self.ref(name) for name in outputs or self.outputs}
