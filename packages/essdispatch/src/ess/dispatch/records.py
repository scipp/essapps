# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Requests and records: what a user asks for and what the backend made of it.

Both are plain data. A request names a spec and its params by name, each an
inline value or a reference; a record is the request with every value filled
in, as the backend accepted it. A record never changes. Its status, which
changes once from pending to finished, is asked of the client.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, with_config

from ess.spec import AccumulatorRef, DatasetRef, OutputRef, Ref, WorkflowSpec
from ess.spec.refs import ParamsError, iter_refs, map_refs

from .errors import FieldError, SubmitError


def as_values(value: Any) -> Any:
    """
    ``value`` with each model in it, but a reference, as a dict of the fields
    that were given.

    So a request, a template, and a push take a params model or a group as a
    model or as a dict alike. Defaults are left out, as they are of a dict, so
    that the backend fills in those of its own spec; a reference is kept
    whole, since its kind is a default.
    """
    if isinstance(value, BaseModel) and not isinstance(value, Ref):
        return value.model_dump(exclude_unset=True)
    if isinstance(value, dict):
        return {k: as_values(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [as_values(v) for v in value]
    return value


def read_refs(params: Any) -> Any:
    """
    Params by name with each reference in its plain (JSON) form read as a
    :data:`~ess.spec.Ref`, where a request may hold one (see
    :func:`ess.spec.refs.iter_refs`); anything but a mapping is left for
    validation to refuse. Raises ``SubmitError`` for a malformed reference.
    """
    if not isinstance(params, Mapping):
        return params
    try:
        return map_refs(params, lambda ref: ref)
    except ParamsError as error:
        raise SubmitError.of(error) from None


Params = Annotated[dict[str, Any], BeforeValidator(read_refs)]
"""
The params of a request by name, each an inline value, a reference, ``None``,
or a list of these; so every way of making one, the log's JSON among them,
holds references as :data:`~ess.spec.Ref`.
"""


def _given(values: Mapping[str, Any] | BaseModel | None) -> dict[str, Any]:
    """Params by name, as :func:`as_values` gives them, references read."""
    return {} if values is None else read_refs(as_values(values))


class SpecId(BaseModel, frozen=True):
    """The identity of a spec: its name and interface version."""

    name: str
    version: int

    @classmethod
    def of(cls, spec: WorkflowSpec[Any, Any] | SpecId | dict[str, Any]) -> SpecId:
        if isinstance(spec, SpecId):
            return spec
        if isinstance(spec, WorkflowSpec):
            return cls(name=spec.name, version=spec.version)
        return cls.model_validate(spec)

    def __str__(self) -> str:
        return f'{self.name}/v{self.version}'


class Request(BaseModel, frozen=True):
    """
    A spec and its params, by name.

    ``params`` is an instance of the params model, if every value is given
    inline, or a dict by name: an inline value, such as a group as a model or
    a dict, or a reference, as the parameter's type allows; a list of these
    for a list parameter.
    A file is always given by reference, such as ``dataset(path=...)``. A
    reference is found at the top of a value or of an element of a list, so
    the references of a request are found without its spec.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra='forbid')

    spec: SpecId
    params: Params = Field(default_factory=dict)

    def __init__(
        self,
        spec: WorkflowSpec[Any, Any] | SpecId,
        *,
        params: Mapping[str, Any] | BaseModel | None = None,
    ) -> None:
        super().__init__(spec=SpecId.of(spec), params=_given(params))

    def upstream(self) -> list[OutputRef]:
        """The outputs of other records this request reads."""
        return [ref for _, ref in iter_refs(self.params) if isinstance(ref, OutputRef)]

    def accumulators(self) -> list[AccumulatorRef]:
        """
        The outputs of accumulators this request reads; a record's request
        reads none, since submitting replaces each by an output of the record
        of the state it reads.
        """
        return [
            ref for _, ref in iter_refs(self.params) if isinstance(ref, AccumulatorRef)
        ]

    def datasets(self) -> list[DatasetRef]:
        """The datasets this request names directly."""
        return [ref for _, ref in iter_refs(self.params) if isinstance(ref, DatasetRef)]


@dataclass(frozen=True, init=False)
@with_config(extra='forbid')
class Template:
    """
    A spec, some values, and blanks that each use fills.

    Values are given as a request's are. A blank is a parameter, a table
    among them. Naming a field as a blank drops the value given for it, so a
    template can be made from any request's values or a whole params model,
    which it keeps as a dict. Change a template with ``dataclasses.replace``.
    """

    spec: SpecId
    params: Params
    blanks: tuple[str, ...]

    def __init__(
        self,
        spec: WorkflowSpec[Any, Any] | SpecId,
        *,
        params: Mapping[str, Any] | BaseModel | None = None,
        blanks: Collection[str] = (),
    ) -> None:
        blanks = tuple(blanks)
        kept = {k: v for k, v in _given(params).items() if k not in blanks}
        object.__setattr__(self, 'spec', SpecId.of(spec))
        object.__setattr__(self, 'params', kept)
        object.__setattr__(self, 'blanks', blanks)

    def fill(self, *, params: Mapping[str, Any] | BaseModel | None = None) -> Request:
        """
        The request with the blanks filled by ``params``, a blank table by
        its rows. Raises ``SubmitError`` for each blank not filled, and each
        value the template fixes.
        """
        params = _given(params)
        missing = [b for b in self.blanks if b not in params]
        fixed = sorted(params.keys() & self.params.keys())
        if missing or fixed:
            raise SubmitError(
                [FieldError(None, (b,), 'a blank, not filled') for b in missing]
                + [FieldError(None, (f,), 'fixed by the template') for f in fixed]
            )
        return Request(self.spec, params={**self.params, **params})


class Status(StrEnum):
    PENDING = 'pending'
    COMPLETED = 'completed'
    FAILED = 'failed'
    CANCELLED = 'cancelled'


class Record(BaseModel, frozen=True):
    """
    A request as the backend accepted it.

    ``request`` has every value filled in, inline values in their JSON form
    and references as :data:`~ess.spec.Ref`: dataset names resolved, defaults
    filled, and each reference to an
    accumulator replaced by a reference to the record of the state it reads, a
    record of the plain request of that state.
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

    def ref(self, output: str, key: str | None = None) -> OutputRef:
        """
        A reference to ``output``, or to its element ``key`` if it is a list
        or a dict; the key of an element of a list is its index, such as
        ``'0'``.
        """
        if output not in self.outputs:
            raise KeyError(f'{self.spec} has no output {output!r}')
        return OutputRef(record=self.id, output=output, key=key)

    def refs(self, *outputs: str) -> dict[str, OutputRef]:
        """A reference to each of ``outputs`` by name; to every output if none."""
        return {name: self.ref(name) for name in outputs or self.outputs}
