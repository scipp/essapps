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

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ess.spec import (
    AccumulatorRef,
    DatasetRef,
    OutputRef,
    WorkflowSpec,
    as_ref,
    walk_refs,
)


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


def as_refs(value: Any) -> Any:
    """``value`` with references read as references, not dicts."""
    return map_refs(value, lambda ref: ref)


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
    """A spec and its parameter values."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    spec: SpecId
    params: dict[str, Any] = Field(default_factory=dict)

    def __init__(
        self, spec: WorkflowSpec | SpecId, params: dict[str, Any] | None = None
    ) -> None:
        super().__init__(spec=SpecId.of(spec), params=params or {})

    def inputs(self) -> list[OutputRef]:
        """The outputs of other records this request reads."""
        return [ref for _, ref in walk_refs(self.params) if isinstance(ref, OutputRef)]

    def accumulators(self) -> list[AccumulatorRef]:
        """The states of accumulators this request reads."""
        return [
            ref for _, ref in walk_refs(self.params) if isinstance(ref, AccumulatorRef)
        ]

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


Row = dict[str, Any]
"""One row of a table of an accumulator, as a request over the table takes it."""


class Record(BaseModel, frozen=True):
    """
    A request as the backend accepted it.

    ``request`` has every value filled in: dataset names resolved, defaults
    filled, and references to accumulators pinned to the state they read.
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
