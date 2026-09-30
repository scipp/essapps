# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Requests and records: what a user asks for and what the backend made of it.

Both are plain data. A request names a spec and parameter values; a record is
the request with every value filled in, plus what happened. A record returned
to a client is a copy as of the call: a pending record is replaced by a newer
copy as it finishes, and a finished record never changes.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from ess.reduce.spec import DatasetRef, OutputRef, WorkflowSpec, as_ref, walk_refs
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr


class SubmitError(ValueError):
    """A request that cannot run, refused before any record exists."""


def map_refs(value: Any, fn: Callable[[Any], Any]) -> Any:
    """``value`` with each reference in dicts and lists replaced by ``fn(ref)``."""
    if (ref := as_ref(value)) is not None:
        return fn(ref)
    if isinstance(value, dict):
        return {k: map_refs(v, fn) for k, v in value.items()}
    if isinstance(value, list):
        return [map_refs(v, fn) for v in value]
    return value


def output_refs(value: Any) -> list[OutputRef]:
    """The references to outputs in ``value``."""
    return [ref for _, ref in walk_refs(value) if isinstance(ref, OutputRef)]


class SpecId(BaseModel, frozen=True):
    """The identity of a spec: its name and interface version."""

    name: str
    version: int

    @classmethod
    def of(cls, spec: WorkflowSpec | SpecId | dict[str, Any]) -> SpecId:
        if isinstance(spec, SpecId):
            return spec
        if isinstance(spec, WorkflowSpec):
            return cls(name=spec.name, version=spec.version)
        return cls.model_validate(spec)

    def __str__(self) -> str:
        return f'{self.name}/v{self.version}'


class Request(BaseModel, frozen=True):
    """
    A spec and its parameter values.

    A request that is not yet submitted can be referenced by other requests in
    the same submission; :meth:`ref` gives a placeholder that the backend
    replaces by a reference to the record the request becomes.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    spec: SpecId
    params: dict[str, Any] = Field(default_factory=dict)

    def __init__(
        self, spec: WorkflowSpec | SpecId, params: dict[str, Any] | None = None
    ) -> None:
        super().__init__(spec=SpecId.of(spec), params=params or {})

    @property
    def placeholder(self) -> str:
        """The record name that :meth:`ref` uses before submission."""
        return f'@{id(self)}'

    def ref(self, output: str) -> OutputRef:
        return OutputRef(record=self.placeholder, output=output)

    def refs(self) -> list[OutputRef]:
        """The outputs of other records this request reads."""
        return output_refs(self.params)

    def datasets(self) -> list[DatasetRef]:
        """The datasets this request names directly."""
        return [ref for _, ref in walk_refs(self.params) if isinstance(ref, DatasetRef)]


@dataclass(frozen=True)
class Template:
    """
    A spec, some values, and blanks that each use fills.

    Naming a field as a blank drops the value given for it, so a template can
    be made from any request's values. Change a template with
    ``dataclasses.replace``.
    """

    spec: SpecId
    params: dict[str, Any] = field(default_factory=dict)
    blanks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        kept = {k: v for k, v in self.params.items() if k not in self.blanks}
        object.__setattr__(self, 'spec', SpecId.of(self.spec))
        object.__setattr__(self, 'params', kept)

    def fill(self, values: dict[str, Any]) -> Request:
        """The request with the blanks filled by ``values``."""
        if set(values) != set(self.blanks):
            raise ValueError(f'fill the blanks {self.blanks}, got {tuple(values)}')
        return Request(self.spec, {**self.params, **values})


class Status(StrEnum):
    PENDING = 'pending'
    COMPLETED = 'completed'
    FAILED = 'failed'
    CANCELLED = 'cancelled'

    @property
    def finished(self) -> bool:
        return self is not Status.PENDING


class Failure(BaseModel, frozen=True):
    message: str


class Snapshot(BaseModel, frozen=True):
    """
    What submitting an accumulator makes: its spec over the first elements.

    ``upto`` counts the elements pushed before the submission. The request
    this stands for, the accumulator spec over those elements, is built only
    when someone asks for it, so a snapshot costs the same however many
    elements it covers.
    """

    spec: SpecId
    accumulator: str
    upto: int = Field(ge=1)


Element = dict[str, OutputRef]
"""One element of an accumulator: a reference for each field of the element model."""

Submission = Annotated[Snapshot | Request, Field(union_mode='left_to_right')]
"""
How a record stores its request. Plain data is tried as a snapshot first, since
a request takes any values.
"""


class Record(BaseModel, frozen=True):
    """
    A request with every value filled in, and what happened to it.

    ``submitted`` is what the backend's log holds: the request, or for a
    snapshot of an accumulator a :class:`Snapshot`; :attr:`request` is the
    request either way. ``outputs`` lists the output names the spec declares;
    ``label`` and ``member`` are given at submission and do not change the
    result.
    """

    id: str
    submitted: Submission
    proposal: str
    submitter: str
    created: datetime
    outputs: tuple[str, ...]
    status: Status = Status.PENDING
    label: str | None = None
    member: str | None = None
    failure: Failure | None = None
    _elements: Sequence[Element] = PrivateAttr(default=())

    def with_elements(self, elements: Sequence[Element]) -> Record:
        """
        This record, building the request of a snapshot from ``elements``.

        ``elements`` are the accumulator's elements in push order; they may
        grow, since a snapshot uses only the first ``upto``.
        """
        record = self.model_copy()
        record._elements = elements
        return record

    def __eq__(self, other: object) -> bool:
        # Where a snapshot finds its elements is not part of what a record says.
        if not isinstance(other, Record):
            return NotImplemented
        return self.__dict__ == other.__dict__

    __hash__ = None  # type: ignore[assignment]

    @property
    def spec(self) -> SpecId:
        return self.submitted.spec

    @property
    def request(self) -> Request:
        """The request with every value filled in; a snapshot's, over its elements."""
        if isinstance(self.submitted, Request):
            return self.submitted
        if len(self._elements) < self.submitted.upto:
            raise LookupError(
                f'record {self.id} is a snapshot of accumulator '
                f'{self.submitted.accumulator}, whose elements are not known here'
            )
        elements = self._elements[: self.submitted.upto]
        return Request(self.spec, {f: [e[f] for e in elements] for f in elements[0]})

    def ref(self, output: str) -> OutputRef:
        if output not in self.outputs:
            raise KeyError(f'{self.spec} has no output {output!r}')
        return OutputRef(record=self.id, output=output)

    def refs(self) -> dict[str, OutputRef]:
        """A reference to every output, by name."""
        return {name: self.ref(name) for name in self.outputs}
