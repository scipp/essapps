# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The client: what a notebook, an application, or a driver calls.

A client talks to one backend for one proposal. Calls that take records or
requests accept one, a list, or a dict, and return the same shape.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, TypeVar

from ess.reduce.spec import DatasetRef, WorkflowSpec
from pydantic import BaseModel

from .backend import Backend, Workflow
from .datasets import DatasetSource
from .records import Record, Request, SpecId, Status

T = TypeVar('T')
U = TypeVar('U')


def _each(what: Any, fn: Callable[[list[T]], list[U]]) -> Any:
    """Apply ``fn`` to the items of a single item, a list, or a dict; keep the shape."""
    if isinstance(what, Mapping):
        return dict(zip(what, fn(list(what.values())), strict=True))
    if isinstance(what, list | tuple):
        return fn(list(what))
    return fn([what])[0]


class Provenance(BaseModel, frozen=True):
    """
    Where a record came from: its request, the records it read, and their datasets.

    ``records`` lists every record read through all inputs, nearest first; it
    stops at datasets.
    """

    request: Request
    upstream: tuple[Record, ...]

    def records(self) -> list[Record]:
        return list(self.upstream)

    def datasets(self) -> list[DatasetRef]:
        found: list[DatasetRef] = []
        for request in (self.request, *(r.request for r in self.upstream)):
            found.extend(d for d in request.datasets() if d not in found)
        return found


class Client:
    def __init__(self, backend: Backend, *, proposal: str, submitter: str) -> None:
        self._backend = backend
        self.proposal = proposal
        self.submitter = submitter

    # Submitting

    def submit(
        self,
        what: Any,
        params: dict[str, Any] | None = None,
        *,
        label: str | None = None,
        member: str | None = None,
    ) -> Any:
        """
        Submit a spec with its values, a request, a list, or a dict of requests.

        The records come back pending, in the shape given. Under a label, the
        keys of a dict become the members of their records.
        """
        if isinstance(what, WorkflowSpec | SpecId):
            what = Request(what, params)
        elif params is not None:
            raise TypeError('params go with a spec, not with requests')
        if isinstance(what, Mapping):
            if member is not None:
                raise TypeError('the keys of a dict are the members')
            entries = [(r, label, key if label else None) for key, r in what.items()]
        else:
            requests = what if isinstance(what, list | tuple) else [what]
            entries = [(r, label, member) for r in requests]
        records = self._backend.submit(
            entries, proposal=self.proposal, submitter=self.submitter
        )
        return _each(what, lambda _: records)

    def compute(self, *args: Any, **kwargs: Any) -> Any:
        """Submit and wait."""
        return self.wait(self.submit(*args, **kwargs))

    def wait(self, records: Any) -> Any:
        """The records once finished; a failed record is returned, not raised."""
        return _each(records, lambda rs: self._backend.wait(r.id for r in rs))

    def cancel(self, records: Any) -> None:
        """Cancel the records that have not started."""
        if isinstance(records, Mapping):
            records = list(records.values())
        elif not isinstance(records, list | tuple):
            records = [records]
        self._backend.cancel(r.id for r in records)

    # Reading

    def output(self, record: Record, name: str) -> Any:
        (record,) = self._backend.wait([record.id])
        if record.status is not Status.COMPLETED:
            raise RuntimeError(f'record {record.id} {record.status}: {record.failure}')
        return self._backend.output(record.id, name)

    def records(
        self,
        *,
        label: str | None = None,
        spec: WorkflowSpec | SpecId | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[Record]:
        """The proposal's records, oldest first, filtered by what is given."""
        spec_id = None if spec is None else SpecId.of(spec)
        return [
            r
            for r in self._backend.records(self.proposal)
            if (label is None or r.label == label)
            and (spec_id is None or r.request.spec == spec_id)
            and (since is None or r.created >= since)
            and (until is None or r.created < until)
        ]

    def latest(self, label: str, member: str | None = None) -> Record:
        """The latest record under a label and member, pending or not."""
        matching = [r for r in self.records(label=label) if r.member == member]
        if not matching:
            raise KeyError(f'no record under {label!r}, member {member!r}')
        return matching[-1]

    def members(self, label: str) -> dict[str | None, Record]:
        """The latest record of each member of a label."""
        return {r.member: r for r in self.records(label=label)}

    def labels(self) -> list[str]:
        return sorted({r.label for r in self.records() if r.label is not None})

    def provenance(self, record: Record) -> Provenance:
        upstream: list[Record] = []
        todo = [ref.record for ref in record.request.refs()]
        while todo:
            read = self._backend.record(todo.pop(0))
            if read not in upstream:
                upstream.append(read)
                todo.extend(ref.record for ref in read.request.refs())
        return Provenance(request=record.request, upstream=tuple(upstream))


def local(
    *,
    proposal: str,
    datasets: DatasetSource,
    bind: Mapping[WorkflowSpec, Workflow],
    submitter: str = 'user',
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Client:
    """A client of a backend in this process, running the workflows bound here."""
    return Client(
        Backend(datasets, bind, clock=clock), proposal=proposal, submitter=submitter
    )
