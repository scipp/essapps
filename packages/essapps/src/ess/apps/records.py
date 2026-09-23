# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Run requests and run records: what ran.

A run request is one run of a workflow, every parameter value set, with any
intermediates supplied in place of what computes them and the outputs to
compute named; a run record is the request plus what happened to it.

See docs/developer/records.md.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, Field
from pydantic_core import to_jsonable_python

from .spec import DatasetRef, OutputRef, SpecId, dataset_refs, walk_refs


def _plain(value: Any) -> Any:
    return to_jsonable_python(value)


Plain = Annotated[Any, AfterValidator(_plain)]
"""
A parameter value in its plain JSON form, the one form stored data has.

A model or a reference given as a Python object is dumped when the holder is
validated, so that a value compares and displays alike whether it was just made
or read back from the record store.
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


class Origin(BaseModel, frozen=True):
    """
    Where a request's values came from: explanation, not provenance.

    Provenance is the data a run read, reached through the resolved request,
    which alone reproduces the run. This says which template version, rule
    version, lookup version and lookup entry filled the request, and which
    values were pinned beyond them, so that a reprocess under a new template or
    lookup version carries what was pinned and fills the rest again.
    """

    template: str | None = None
    rule: str | None = None
    lookup: str | None = None
    entry: str | None = Field(
        default=None, description="Name of the entry of ``lookup`` that matched."
    )
    pinned: dict[str, Plain] = Field(
        default_factory=dict,
        description="Values the submitter supplied beyond template and lookup.",
    )


class Accumulate(BaseModel, frozen=True):
    """
    A supplied intermediate that is the accumulation of several outputs.

    The binding accumulates them with the accumulator its author gave for the
    input, in the order listed; the framework never combines values itself.
    Every accumulator is associative, so an element may itself be an
    accumulated value that a finalize run passed through as an output.
    """

    accumulate: list[OutputRef]


class RunRequest(BaseModel, frozen=True):
    """
    One run of a workflow: everything needed to run it.

    ``params`` holds every parameter value, the ones a caller varies included;
    as recorded, the spec's default is filled in for each parameter not given.
    ``supplied`` holds intermediates the spec exposes, each a reference or an
    :class:`Accumulate`, supplied in place of what computes them. Only a
    required parameter without a default may stay unset, and only when the
    request supplies an intermediate in place of what needs it. ``outputs`` are
    the outputs to compute, empty for the spec's results.

    ``vary`` names the parameters a caller varies from run to run, so that a
    session holds the stage cut at them. Like ``label``, it is a hint: it does
    not change the result, it is not part of the workflow ID, and provenance
    does not rely on it.

    A request is complete: it never names a session or a process, and the only
    path it may name is the identity of a local file that carries no run
    identity.
    """

    spec: SpecId
    params: dict[str, Plain] = Field(default_factory=dict)
    supplied: dict[str, Plain] = Field(default_factory=dict)
    outputs: tuple[str, ...] = ()
    vary: tuple[str, ...] = Field(
        default=(),
        description="Parameters a caller varies; a hint for the session, not "
        "part of the workflow ID.",
    )
    instrument: str = Field(min_length=1)
    proposal: str = Field(min_length=1)
    submitter: str = Field(min_length=1)
    label: str | None = Field(
        default=None,
        description="Records under one label supersede each other.",
    )
    member_key: str | None = Field(
        default=None,
        description="Which member of the batch under the label this is.",
    )
    origin: Origin = Field(
        default_factory=Origin,
        description="How the request was made: template, rule, lookup entry, "
        "and the values the submitter pinned.",
    )

    @property
    def fixed(self) -> dict[str, Any]:
        """The parameters the request does not vary."""
        return {k: v for k, v in self.params.items() if k not in self.vary}

    @property
    def workflow_id(self) -> str:
        """
        A hash of spec, the parameters not varied, instrument, and proposal:
        the pipeline from which a session cuts the stage it holds.

        Two requests that set the same values name the same pipeline, whatever
        they vary or supply. Keys are sorted so that the order in which values
        were given does not change the identity.
        """
        content = json.dumps(
            {
                'spec': self.spec.model_dump(mode='json'),
                'params': self.fixed,
                'instrument': self.instrument,
                'proposal': self.proposal,
            },
            sort_keys=True,
        )
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def values(self) -> dict[str, Any]:
        """Parameters and supplied intermediates together, as plain data."""
        return {**self.params, **self.supplied}

    def refs(self) -> list[OutputRef]:
        """References to outputs of records: the edges the scheduler waits on."""
        return [r for _, r in walk_refs(self.values()) if isinstance(r, OutputRef)]

    def datasets(self) -> list[DatasetRef]:
        """Distinct references to data the framework did not compute."""
        return dataset_refs(self.values())


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
    stored_outputs: list[OutputRef] = Field(default_factory=list)
    package_versions: dict[str, str] = Field(default_factory=dict)
    environment: str | None = None
    binding: Literal['entry_point', 'in_process'] | None = None
    reused: bool = Field(
        default=False,
        description="Whether the result came out of a stage the session was "
        "already holding, which publication reads before publishing.",
    )
    checksums: dict[str, str] = Field(default_factory=dict)
    failure: Failure | None = None


class RunRecord(BaseModel):
    """
    A run request plus what happened to it.

    Immutable once the run completes, except for status, and never deleted on its
    own. Small output values live in ``outputs``; data-reference outputs are listed
    in ``stored_outputs`` and their bytes live in the data store. ``checksums``
    holds the checksum of each local file the run read, keyed by the string form
    of the dataset reference that named it, so that a recompute can tell whether
    it read the same bytes.
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    request: RunRequest
    status: Status = Status.SUBMITTED
    created: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started: datetime | None = None
    finished: datetime | None = None
    resolved_params: dict[str, Any] | None = None
    outputs: dict[str, Any] = Field(default_factory=dict)
    stored_outputs: list[OutputRef] = Field(default_factory=list)
    package_versions: dict[str, str] = Field(default_factory=dict)
    environment: str | None = None
    binding: Literal['entry_point', 'in_process'] | None = None
    reused: bool = Field(
        default=False,
        description="Whether the result came out of a stage the session was "
        "already holding.",
    )
    checksums: dict[str, str] = Field(default_factory=dict)
    derives_from: Derivation | None = None
    supersedes: str | None = Field(
        default=None,
        description="The latest record under this label and member key when this "
        "record was submitted: the record nothing supersedes. The order under a label "
        "never depends on a clock, which matters once several writers, a rule, "
        "a retry, and a person's correction submit under one label from "
        "different hosts. ``derives_from`` says why a request was made; this "
        "says where the record sits in the label's history.",
    )
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

    def ref(self, output: str | None = None, key: str | None = None) -> OutputRef:
        """A reference to an output; the output name may be omitted if there is one."""
        if output is None:
            names = sorted(self.output_names())
            if len(names) != 1:
                what = (
                    f'is {self.status.value}' if not names else f'has outputs {names}'
                )
                raise ValueError(f'{self.id} {what}; name the output')
            output = names[0]
        return OutputRef(record=self.id, output=output, key=key)

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
