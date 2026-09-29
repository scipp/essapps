# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Requests and records: what a user asks for and what the backend made of it.

Both are plain data. A request names a spec and parameter values; a record is
the request with every value filled in, plus what happened. A record returned
to a client is a snapshot: a pending record is replaced by a newer snapshot as
it finishes, and a finished record never changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from ess.reduce.spec import DatasetRef, OutputRef, WorkflowSpec, walk_refs
from pydantic import BaseModel, ConfigDict, Field


class SubmitError(ValueError):
    """A request that cannot run, refused before any record exists."""


class SpecId(BaseModel, frozen=True):
    """The identity of a spec: its name and interface version."""

    name: str
    version: int

    @classmethod
    def of(cls, spec: WorkflowSpec | SpecId) -> SpecId:
        if isinstance(spec, SpecId):
            return spec
        return cls(name=spec.name, version=spec.version)

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
        self,
        spec: WorkflowSpec | SpecId,
        params: dict[str, Any] | None = None,
        **data: Any,
    ) -> None:
        super().__init__(spec=SpecId.of(spec), params=params or {}, **data)

    @property
    def placeholder(self) -> str:
        """The record name that :meth:`ref` uses before submission."""
        return f'@{id(self)}'

    def ref(self, output: str) -> OutputRef:
        return OutputRef(record=self.placeholder, output=output)

    def refs(self) -> list[OutputRef]:
        """The outputs of other records this request reads."""
        return [ref for _, ref in walk_refs(self.params) if isinstance(ref, OutputRef)]

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

    spec: WorkflowSpec | SpecId
    params: dict[str, Any] = field(default_factory=dict)
    blanks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        kept = {k: v for k, v in self.params.items() if k not in self.blanks}
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


class Record(BaseModel, frozen=True):
    """
    A request with every value filled in, and what happened to it.

    ``outputs`` lists the output names the spec declares; ``label`` and
    ``member`` are given at submission and do not change the result.
    """

    id: str
    request: Request
    proposal: str
    submitter: str
    created: datetime
    outputs: tuple[str, ...]
    status: Status = Status.PENDING
    label: str | None = None
    member: str | None = None
    failure: Failure | None = None

    def ref(self, output: str) -> OutputRef:
        if output not in self.outputs:
            raise KeyError(f'{self.request.spec} has no output {output!r}')
        return OutputRef(record=self.id, output=output)

    def refs(self) -> dict[str, OutputRef]:
        """A reference to every output, by name."""
        return {name: self.ref(name) for name in self.outputs}
