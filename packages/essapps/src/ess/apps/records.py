# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Templates, run requests, and run records: what to run, and what ran.

A run request is one run of a workflow, every parameter value set, with any
intermediates supplied in place of what computes them and the outputs to
compute named; a run record is the request plus what happened to it. A
template is a partial request: the values set and the blanks a use fills.

See docs/developer/records.md.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, Field, field_validator
from pydantic_core import to_jsonable_python

from .spec import (
    DatasetRef,
    OutputRef,
    SpecId,
    WorkflowSpec,
    data_fields,
    dataset_refs,
    walk_refs,
)


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


class Template(BaseModel, frozen=True):
    """
    A partial request: a spec, the values set, and the blanks a use fills.

    A blank is a parameter the use varies from request to request, or an
    intermediate it supplies in place of what computes it: the stage's inputs.
    Every request made from one template names the same stage, which a session
    holds. Records made from a template carry its name as their label. The
    templates of batches and rules are stored and versioned; a slider's
    template lives on the client for as long as the slider does.

    ``params`` holds the plain JSON form a request's params have, whatever
    objects the author passed, so a template read back from storage equals the
    one that was stored. ``outputs`` names the outputs to compute, empty for
    the spec's results. ``dataset_field`` is the blank a dataset fills when a
    rule or :func:`ess.apps.batch.apply` supplies one, which is the sole blank
    unless a template has several.
    """

    spec: SpecId
    params: dict[str, Plain] = Field(default_factory=dict)
    blanks: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    name: str | None = None
    version: int = 1
    dataset_field: str | None = None
    derived_from: str | None = None

    @field_validator('spec', mode='before')
    @classmethod
    def _spec_id(cls, value: Any) -> Any:
        return value.id if isinstance(value, WorkflowSpec) else value

    @classmethod
    def from_request(
        cls,
        name: str,
        request: RunRequest,
        spec: WorkflowSpec,
        blank: Iterable[str] = (),
    ) -> Template:
        """
        Save a request as a template: its data-reference fields, what it varied,
        and what it supplied are the blanks.
        """
        blanks = tuple(
            sorted(
                set(data_fields(spec.params))
                | set(blank)
                | set(request.vary)
                | set(request.supplied)
            )
        )
        params = {k: v for k, v in request.params.items() if k not in blanks}
        return cls(
            name=name,
            spec=request.spec,
            params=params,
            blanks=blanks,
            outputs=request.outputs,
        )

    @property
    def id(self) -> str | None:
        """Name and version; a template without a name has no identity."""
        return None if self.name is None else f'{self.name}/v{self.version}'

    def revise(self, **changes: Any) -> Template:
        """A new version by copy; the old one stays."""
        return Template(
            **dict(self)
            | {
                'version': self.version + 1,
                'params': self.params | changes,
                'derived_from': self.id,
            }
        )

    def cut(
        self,
        *,
        blanks: Iterable[str] | None = None,
        outputs: Iterable[str] | None = None,
        name: str | None = None,
    ) -> Template:
        """The stage from ``blanks`` to ``outputs`` over the same spec and values."""
        return self.model_copy(
            update={
                'blanks': self.blanks if blanks is None else tuple(blanks),
                'outputs': self.outputs if outputs is None else tuple(outputs),
                'name': self.name if name is None else name,
            }
        )

    def fill(self, **values: Any) -> dict[str, Any]:
        """The template's values with every blank filled; other values override."""
        missing = set(self.blanks) - values.keys()
        if missing:
            raise ValueError(f'{self} needs {sorted(missing)}')
        return self.params | values

    def __str__(self) -> str:
        return f'template {self.id or f"over {self.spec}"}'

    def field_for_dataset(self) -> str:
        """The blank a dataset fills."""
        if self.dataset_field is not None:
            return self.dataset_field
        if len(self.blanks) == 1:
            return self.blanks[0]
        raise ValueError(
            f'{self} has blanks {list(self.blanks)}; name the one a dataset '
            'fills in dataset_field'
        )


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
    own. The request is recorded with every value in the form its params model
    gives it, so what a run read is the request's ``params``. Small output values
    live in ``outputs``; data-reference outputs are listed in ``stored_outputs``
    and their bytes live in the data store. ``checksums`` holds the checksum of
    each local file the run read, keyed by the string form of the dataset
    reference that named it, so that a recompute can tell whether it read the
    same bytes.
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    request: RunRequest
    status: Status = Status.SUBMITTED
    created: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started: datetime | None = None
    finished: datetime | None = None
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
