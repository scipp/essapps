# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Requests and records: what a user asks for and what the backend made of it.

Both are plain data. A request names a spec and parameter values; a record is
the request with every value filled in, as the backend accepted it. A record
never changes. Its status, which changes once from pending to finished, is
asked of the client.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    create_model,
    field_validator,
)

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


def validation_problems(
    error: ValidationError, at: tuple[str | int, ...] = ()
) -> list[str]:
    """What a validation error finds wrong; the places are prefixed by ``at``."""
    return [
        f"{'.'.join(map(str, (*at, *e['loc']))) or 'params'}: {e['msg']}"
        for e in error.errors()
    ]


def model_without(model: type[BaseModel], left_out: Collection[str]) -> type[BaseModel]:
    """
    ``model`` without the fields ``left_out`` and without its own validators,
    which read whole requests; it validates each value of a template by its
    own field and that field's validators.
    """
    fields: dict[str, Any] = {
        f: (info.annotation, info)
        for f, info in model.model_fields.items()
        if f not in left_out
    }
    return create_model(
        model.__name__,
        __config__=model.model_config,
        __validators__=_field_validators(model, fields),
        **fields,
    )


def _field_validators(
    model: type[BaseModel], fields: Collection[str]
) -> dict[str, Any]:
    """
    The field validators of ``model`` that validate any of ``fields``, for a
    model of these fields alone; ``create_model`` takes them as validators.
    """
    validators = {}
    for name, decorator in model.__pydantic_decorators__.field_validators.items():
        info = decorator.info
        names = ['*'] if '*' in info.fields else [f for f in info.fields if f in fields]
        if names:
            func = decorator.func  # bound to ``model`` if a classmethod
            unbound = classmethod(func.__func__) if inspect.ismethod(func) else func
            validators[name] = field_validator(*names, mode=info.mode)(unbound)
    return validators


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

    So a request, a template, and a push take a params model, a group, or a
    row as a model or as a dict alike. Defaults are left out, as they are of a
    dict, so that the backend fills in those of its own spec.
    """
    if isinstance(value, BaseModel) and not isinstance(value, Ref):
        return as_refs(value.model_dump(exclude_unset=True))
    if isinstance(value, dict):
        return {k: as_values(v) for k, v in value.items()}
    if isinstance(value, list):
        return [as_values(v) for v in value]
    return value


def as_refs(value: Any) -> Any:
    """``value`` with references read as references, not dicts."""
    return map_refs(value, lambda ref: ref)


class SpecId(BaseModel, frozen=True):
    """The identity of a spec: its name and interface version."""

    name: str
    version: int

    @classmethod
    def of(cls, spec: WorkflowSpec[Any] | SpecId | dict[str, Any]) -> SpecId:
        if isinstance(spec, SpecId):
            return spec
        if isinstance(spec, WorkflowSpec):
            return cls(name=spec.name, version=spec.version)
        return cls.model_validate(spec)

    def __str__(self) -> str:
        return f'{self.name}/v{self.version}'


class Request(BaseModel, frozen=True):
    """
    A spec and its parameter values, given as a params model or as a dict of
    its fields, each a group or a table as a model or a dict.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    spec: SpecId
    params: dict[str, Any] = Field(default_factory=dict)

    def __init__(
        self,
        spec: WorkflowSpec[Any] | SpecId,
        params: dict[str, Any] | BaseModel | None = None,
    ) -> None:
        values = {} if params is None else as_values(params)
        super().__init__(spec=SpecId.of(spec), params=values)

    def inputs(self) -> list[OutputRef]:
        """The outputs of other records this request reads."""
        return [ref for _, ref in walk_refs(self.params) if isinstance(ref, OutputRef)]

    def accumulators(self) -> list[AccumulatorRef]:
        """
        The outputs of accumulators this request reads; a record's request
        reads none, since submitting replaces each by an output of the record
        of the state it reads.
        """
        return [
            ref for _, ref in walk_refs(self.params) if isinstance(ref, AccumulatorRef)
        ]

    def datasets(self) -> list[DatasetRef]:
        """The datasets this request names directly."""
        return [ref for _, ref in walk_refs(self.params) if isinstance(ref, DatasetRef)]


class _Blank:
    def __repr__(self) -> str:
        return 'BLANK'


BLANK: Any = _Blank()
"""
A parameter left open in a template made by :meth:`Template.from_spec`; typed
``Any`` so that a type checker accepts it for any parameter.
"""


class _HasParams[**P](Protocol):
    """A spec as a type checker sees it, through its params model's signature."""

    @property
    def params(self) -> Callable[P, BaseModel]: ...


@dataclass(frozen=True)
class Template:
    """
    A spec, some values, and blanks that each use fills.

    :meth:`from_spec` writes a template with the params model's signature. The
    constructor makes one from values in hand: naming a field as a blank drops
    the value given for it, so a template can be made from any request's
    values or a whole params model, which it keeps as a dict. Change a template
    with ``dataclasses.replace``.
    """

    spec: SpecId
    params: dict[str, Any] = field(default_factory=dict)
    blanks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        values = as_values(self.params)
        kept = {k: v for k, v in values.items() if k not in self.blanks}
        object.__setattr__(self, 'spec', SpecId.of(self.spec))
        object.__setattr__(self, 'params', kept)

    @classmethod
    def from_spec[**P](
        cls, spec: _HasParams[P], /, *args: P.args, **kwargs: P.kwargs
    ) -> Template:
        """
        A template of ``spec`` from the parameters of its params model, given
        as the params model takes them; each parameter given as ``BLANK`` is a
        blank::

            Template.from_spec(IOFQ, sample=BLANK, reduction=Reduction(bins=100))

        So pyright reports a misspelt or missing parameter and a value
        of the wrong type, as it does for the params model. Every keyword is a
        parameter, so this method takes no option of its own. The values
        given are validated by their own fields (see :func:`model_without`); a
        required parameter may be a blank. Raises ``ValueError`` with what is
        wrong, and ``TypeError`` for a parameter given by position.
        """
        if not isinstance(spec, WorkflowSpec):
            raise TypeError(f'{spec!r}: not a WorkflowSpec')
        spec_id, model = SpecId.of(spec), spec.params
        if args:
            raise TypeError(f'{spec_id}: give the parameters by name')
        fields = model.model_fields
        blanks = tuple(name for name in fields if kwargs.get(name) is BLANK)
        given = {k: v for k, v in kwargs.items() if k not in blanks}
        problems = [f'{k}: not a parameter' for k in given if k not in fields]
        try:
            model_without(model, blanks).model_validate(given)
        except ValidationError as error:
            problems += validation_problems(error)
        if problems:
            raise ValueError(f'{spec_id}: {"; ".join(problems)}')
        return cls(spec_id, given, blanks)

    def fill(self, values: dict[str, Any] | BaseModel) -> Request:
        """The request with the blanks filled by ``values``."""
        values = as_values(values)
        if set(values) != set(self.blanks):
            raise ValueError(f'fill the blanks {self.blanks}, got {tuple(values)}')
        return Request(self.spec, {**self.params, **values})


class Status(StrEnum):
    PENDING = 'pending'
    COMPLETED = 'completed'
    FAILED = 'failed'
    CANCELLED = 'cancelled'


Row = dict[str, Any]
"""One row of a table of an accumulator, as a request over the table takes it."""


class Record(BaseModel, frozen=True):
    """
    A request as the backend accepted it.

    ``request`` has every value filled in: dataset names resolved, defaults
    filled, and each reference to an accumulator replaced by a reference to
    the record of the state it reads, a record of the plain request of that
    state.
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
