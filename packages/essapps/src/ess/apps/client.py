# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The client: what a notebook, an application, or a driver calls.

A client talks to one backend for one proposal. Calls that take records or
requests accept one, a list, or a dict, and return the same shape.
"""

from __future__ import annotations

import queue
import threading
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import UTC, datetime
from typing import Any

from ess.reduce.spec import DatasetRef, OutputRef, WorkflowSpec
from pydantic import BaseModel

from .backend import Backend, Entry
from .bindings import Binding, Function
from .datasets import DatasetSource
from .records import (
    Record,
    Request,
    SpecId,
    Status,
    Submission,
    SubmitError,
    map_refs,
)
from .sessions import Accumulator, Session, Stage


def _items(what: Any) -> list[Any]:
    """The items of one item, a list, a tuple, or a dict."""
    if isinstance(what, Mapping):
        return list(what.values())
    if isinstance(what, list | tuple):
        return list(what)
    return [what]


def _reshape(what: Any, items: list[Any]) -> Any:
    """``items`` in the shape of ``what``."""
    if isinstance(what, Mapping):
        return dict(zip(what, items, strict=True))
    if isinstance(what, list | tuple):
        return type(what)(items)
    return items[0]


def _names(what: Any) -> list[str | None]:
    """How each item of ``what`` is named in messages: its key or its index."""
    if isinstance(what, Mapping):
        return [str(key) for key in what]
    if isinstance(what, list | tuple):
        return [f'[{i}]' for i in range(len(what))]
    return [None]


def _numbered(requests: list[Request]) -> list[Request]:
    """The requests with references to each other rewritten as ``@<index>``."""
    index = {r.placeholder: f'@{i}' for i, r in enumerate(requests)}
    if len(index) != len(requests):
        raise SubmitError('a request appears twice in one submission')

    def renumber(ref: Any) -> Any:
        if isinstance(ref, OutputRef) and ref.record in index:
            return OutputRef(record=index[ref.record], output=ref.output, key=ref.key)
        return ref

    return [Request(r.spec, map_refs(r.params, renumber)) for r in requests]


class Provenance(BaseModel, frozen=True):
    """
    Where a record came from: what it ran, the records it read, and their datasets.

    ``submitted`` is the record's request or snapshot. ``records`` lists every
    record read through all inputs, nearest first; it stops at datasets.
    """

    submitted: Submission
    upstream: tuple[Record, ...]

    def records(self) -> list[Record]:
        return list(self.upstream)

    def datasets(self) -> list[DatasetRef]:
        ran = (self.submitted, *(r.submitted for r in self.upstream))
        requests = [r for r in ran if isinstance(r, Request)]
        return list(dict.fromkeys(d for r in requests for d in r.datasets()))


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
        Submit a spec or a stage with values, an accumulator, or requests.

        Requests may be one, a list, or a dict. The records come back pending,
        in the shape given. Under a label, the keys of a dict become the
        members of their records.
        """
        stage = None
        if isinstance(what, WorkflowSpec | SpecId):
            what = Request(what, params)
        elif isinstance(what, Stage):
            stage, what = what.id, what.request(params or {})
        elif params is not None:
            raise TypeError('params go with a spec or a stage')
        elif isinstance(what, Accumulator):
            entry = Entry(None, label=label, member=member, accumulator=what.id)
            (record,) = self._backend.submit(
                [entry], proposal=self.proposal, submitter=self.submitter
            )
            return record
        if isinstance(what, Mapping):
            if member is not None:
                raise TypeError('the keys of a dict are the members')
            members = [str(k) if label is not None else None for k in what]
        else:
            members = [member] * len(_items(what))
        entries = [
            Entry(request, label=label, member=m, name=name, stage=stage)
            for request, m, name in zip(
                _numbered(_items(what)), members, _names(what), strict=True
            )
        ]
        records = self._backend.submit(
            entries, proposal=self.proposal, submitter=self.submitter
        )
        return _reshape(what, records)

    def compute(self, *args: Any, **kwargs: Any) -> Any:
        """Submit and wait."""
        return self.wait(self.submit(*args, **kwargs))

    def wait(self, records: Any) -> Any:
        """The records once finished; a failed record is returned, not raised."""
        ids = [r.id for r in _items(records)]
        return _reshape(records, self._backend.wait(ids, self.proposal))

    def as_completed(self, records: Iterable[Record]) -> Iterator[Record]:
        """
        The records as they finish, each once, in the order they finish.

        A thread consumes ``records``, so it may be a generator that blocks,
        such as one that submits a request per dataset as it arrives: records
        that finish meanwhile are yielded. An error it raises is raised here.
        Closing the iterator stops consuming ``records`` after the next one.
        """
        finished: queue.SimpleQueue[Record | Exception | int] = queue.SimpleQueue()
        stop = threading.Event()

        def consume() -> None:
            seen: set[str] = set()
            try:
                for record in records:
                    if record.id not in seen:
                        seen.add(record.id)
                        self._backend.when_finished(
                            record.id, self.proposal, finished.put
                        )
                    if stop.is_set():
                        return
            except Exception as error:  # raised in the caller's thread
                finished.put(error)
            else:
                finished.put(len(seen))

        threading.Thread(target=consume, daemon=True).start()
        yielded, total = 0, None
        try:
            while yielded != total:
                item = finished.get()
                if isinstance(item, Exception):
                    raise item
                if isinstance(item, int):
                    total = item
                else:
                    yielded += 1
                    yield item
        finally:
            stop.set()

    def cancel(self, records: Any) -> None:
        """End the unfinished records as cancelled."""
        self._backend.cancel([r.id for r in _items(records)], self.proposal)

    def session(self, where: str | None = None) -> Session:
        """
        A session for stages and accumulators, released when it ends.

        ``where`` places the session's process; only this process is supported.
        """
        if where not in (None, 'local'):
            raise NotImplementedError(f'sessions {where!r}')
        return Session(self._backend, self.proposal, where)

    # Reading

    def output(self, record: Record, name: str) -> Any:
        (record,) = self._backend.wait([record.id], self.proposal)
        if record.status is not Status.COMPLETED:
            raise RuntimeError(f'record {record.id} {record.status}: {record.failure}')
        return self._backend.output(record.id, name, self.proposal)

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
            for r in self._backend.records(self.proposal, label=label)
            if (spec_id is None or r.spec == spec_id)
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
        upstream: dict[str, Record] = {}
        todo = deque(self._inputs(record.id))
        while todo:
            record_id = todo.popleft()
            if record_id not in upstream:
                upstream[record_id] = self._backend.record(record_id, self.proposal)
                todo.extend(self._inputs(record_id))
        return Provenance(submitted=record.submitted, upstream=tuple(upstream.values()))

    def _inputs(self, record_id: str) -> list[str]:
        """The IDs of the records a record read."""
        return [ref.record for ref in self._backend.inputs(record_id, self.proposal)]


def local(
    *,
    proposal: str,
    datasets: DatasetSource,
    bind: Mapping[WorkflowSpec, Binding | Function],
    submitter: str = 'user',
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Client:
    """A client of a backend in this process, running the workflows bound here."""
    return Client(
        Backend(datasets, bind, clock=clock), proposal=proposal, submitter=submitter
    )
