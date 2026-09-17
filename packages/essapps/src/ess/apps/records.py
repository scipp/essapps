# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Run requests and run records: the one way the framework names data (D1)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from .spec import DatasetRef, Ref, SpecId, walk_refs

RunStage = Literal['run', 'contribute', 'combine']
"""
Which entry points a request runs (D15).

``run`` is the whole workflow, contribute then finalize, and the only stage a
spec without a contribution has. ``contribute`` is a member run of a series: it
produces the contribution and none of the other outputs. ``combine`` combines
the referenced contributions and finalizes the result; it carries the finalize
parameters and no others.
"""


class Status(StrEnum):
    SUBMITTED = 'submitted'
    WAITING = 'waiting'
    DISPATCHED = 'dispatched'
    RUNNING = 'running'
    COMPLETED = 'completed'
    FAILED = 'failed'
    CANCELLED = 'cancelled'

    @property
    def terminal(self) -> bool:
        return self in (Status.COMPLETED, Status.FAILED, Status.CANCELLED)


class Submission(BaseModel, frozen=True):
    """
    How a request was made (D14): explanation, not provenance.

    The resolved request alone reproduces the run; this says which template
    version, rule version and lookup entry filled it, and which values the
    submitter typed beyond them, so that a reprocess under a new template or
    lookup version carries what was typed and recomputes what was filled.
    """

    template: str | None = None
    rule: str | None = None
    entry: str | None = Field(
        default=None, description="Name of the lookup entry that matched."
    )
    typed: dict[str, Any] = Field(
        default_factory=dict,
        description="Values the submitter supplied beyond template and lookup.",
    )


class RunRequest(BaseModel, frozen=True):
    """
    Everything needed to execute a workflow once.

    ``params`` is the plain JSON form of the spec's params model, with data
    reference fields holding a :class:`Ref` or a :class:`DatasetRef`. A request is
    complete: it never names a session or a process, and the only path it may
    name is the identity of a local file that carries no run identity.

    ``stage`` says which entry points run. A combine request carries the finalize
    parameters in ``params`` and its members in ``contributions``, which are
    references like any other and are what the scheduler waits on.
    """

    spec: SpecId
    params: dict[str, Any] = Field(default_factory=dict)
    stage: RunStage = Field(
        default='run',
        description="Which entry points of the workflow this request runs (D15).",
    )
    contributions: list[Ref] = Field(
        default_factory=list,
        description="What a combine request combines: contribution outputs of "
        "member records and of the previous combine.",
    )
    instrument: str = Field(min_length=1)
    proposal: str = Field(min_length=1)
    submitter: str = Field(min_length=1)
    label: str | None = Field(
        default=None,
        description="Records under one label supersede each other (D14, D10).",
    )
    member_key: str | None = Field(
        default=None,
        description="Which member of the batch under the label this is.",
    )
    submission: Submission = Field(
        default_factory=Submission,
        description="How the request was made: template, rule, lookup entry, "
        "and the values the submitter typed (D14).",
    )

    def refs(self) -> list[Ref]:
        """References to outputs of records: the edges the scheduler waits on."""
        params = [r for _, r in walk_refs(self.params) if isinstance(r, Ref)]
        return params + list(self.contributions)

    def datasets(self) -> list[DatasetRef]:
        """References to data the framework did not compute; never pending."""
        return [r for _, r in walk_refs(self.params) if isinstance(r, DatasetRef)]


class Derivation(BaseModel, frozen=True):
    record: str
    reason: Literal['retry', 'recompute', 'copy']


class Failure(BaseModel, frozen=True):
    """Structured reason a run failed, so a user sees why without reading logs."""

    kind: str
    message: str
    traceback: str | None = None


class RunResult(BaseModel):
    """What a runner reports back; the backend applies it to its own record."""

    status: Status
    started: datetime
    finished: datetime
    resolved_params: dict[str, Any] | None = None
    outputs: dict[str, Any] = Field(default_factory=dict)
    stored_outputs: list[Ref] = Field(default_factory=list)
    package_versions: dict[str, str] = Field(default_factory=dict)
    environment: str | None = None
    binding: Literal['entry_point', 'in_process'] | None = None
    reused: bool = False
    checksums: dict[str, str] = Field(default_factory=dict)
    failure: Failure | None = None


class RunRecord(BaseModel):
    """
    A run request plus what happened to it.

    Immutable once the run completes, except for status, and never deleted on its
    own. Small output values live in ``outputs``; data-reference outputs are listed
    in ``stored_outputs`` and their bytes live in the data store. ``checksums``
    holds the checksum of each local file the run read, by parameter path, so
    that a recompute can tell whether it read the same bytes.
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    request: RunRequest
    status: Status = Status.SUBMITTED
    created: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started: datetime | None = None
    finished: datetime | None = None
    resolved_params: dict[str, Any] | None = None
    outputs: dict[str, Any] = Field(default_factory=dict)
    stored_outputs: list[Ref] = Field(default_factory=list)
    package_versions: dict[str, str] = Field(default_factory=dict)
    environment: str | None = None
    binding: Literal['entry_point', 'in_process'] | None = None
    reused: bool = False
    checksums: dict[str, str] = Field(default_factory=dict)
    derives_from: Derivation | None = None
    failure: Failure | None = None
    launcher_job: str | None = None
    publishing: list[str] = Field(
        default_factory=list, description="Outputs whose publication was begun."
    )
    published: dict[str, str] = Field(
        default_factory=dict, description="PID per published output."
    )

    def apply(self, result: RunResult) -> None:
        for name, value in result:
            setattr(self, name, value)

    def ref(self, output: str | None = None, key: str | None = None) -> Ref:
        """A reference to an output; the output name may be omitted if there is one."""
        if output is None:
            names = sorted(self.output_names())
            if len(names) != 1:
                what = (
                    f'is {self.status.value}' if not names else f'has outputs {names}'
                )
                raise ValueError(f'{self.id} {what}; name the output')
            output = names[0]
        return Ref(record=self.id, output=output, key=key)

    @property
    def spec(self) -> SpecId:
        return self.request.spec

    def output_names(self) -> set[str]:
        return set(self.outputs) | {ref.output for ref in self.stored_outputs}

    def output_keys(self, output: str) -> set[str] | None:
        """Keys of a collection output, or None if it is not a collection."""
        keys = {r.key for r in self.stored_outputs if r.output == output}
        if keys and None not in keys:
            return {k for k in keys if k is not None}
        value = self.outputs.get(output)
        if isinstance(value, dict):
            return set(value)
        if isinstance(value, list):
            return {str(i) for i in range(len(value))}
        return None
