# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The client: what a notebook, an application, or a driver calls.

A client talks to one backend for one proposal. Calls that take records or
requests accept one, a list, or a dict, and return the same shape.

A record never changes; its status changes once, from pending to finished,
and is asked with :meth:`Client.status` or :meth:`Client.wait`.

A client is the lifetime of what it keeps: the outputs of the records it
makes, its stages, and its accumulators. It keeps each until it releases it or
ends, but a snapshot's value only until the next push into its accumulator.
Releasing and ending stop no work: a pending request still runs, and keeps the
values it reads until it has run.
"""

from __future__ import annotations

import functools
import queue
import threading
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Self

from ess.reduce.spec import DatasetRef, WorkflowSpec
from pydantic import BaseModel

from .accumulators import element_table
from .backend import Backend, Entry
from .bindings import Binding, Function
from .datasets import DatasetSource, Selector
from .records import (
    Element,
    Record,
    Request,
    SpecId,
    Status,
    Submission,
    Template,
)


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


class Datasets:
    """
    The datasets of the backend's dataset source that the proposal may read.

    Forms and drivers find datasets here: they list them, wait for new ones,
    and read their metadata. A request names a dataset with ``dataset(...)``.
    """

    def __init__(self, backend: Backend, client: str) -> None:
        self._backend = backend
        self._client = client

    def list(self, selector: Selector) -> list[DatasetRef]:
        """The datasets the selector matches, in the order they were measured."""
        return self._backend.datasets(selector, self._client)

    def watch(self, selector: Selector) -> Iterator[DatasetRef]:
        """Matching datasets: the existing ones first, then new ones, each once."""
        return self._backend.watch_datasets(selector, self._client)

    def metadata(self, ref: DatasetRef) -> dict[str, Any]:
        """The dataset's current metadata; raises ``KeyError`` if not readable."""
        return self._backend.dataset_metadata(ref, self._client)


@dataclass(frozen=True)
class Stage:
    """
    A template whose fixed part the backend computes once; a call fills its blanks.

    A stage never changes what a record says: a call through it makes the
    record of the filled template. ``id`` names the stage in the backend.
    """

    template: Template
    id: str


class Accumulator:
    """
    The output of its spec over the elements pushed into it so far.

    ``id`` names the accumulator in the backend; submitting it makes a
    snapshot, a record of the combined value of the elements pushed so far.
    A snapshot's value is kept until the next push or the accumulator's
    release. To keep an earlier state, submit a request that reduces or
    copies the snapshot; the next push waits until it has run.
    """

    def __init__(
        self, spec: WorkflowSpec, accumulator_id: str, push: Callable[[Element], None]
    ) -> None:
        self.spec = spec
        self.id = accumulator_id
        self._push = push

    def push(self, element: Element) -> None:
        """
        Push a row of the spec's table, as a request over the table takes it.

        Select a record's outputs with ``record.refs('numerator', ...)``.
        The push waits for the records to finish, and refuses them unless they
        have completed. A driver that pushes records as they finish, as
        ``client.as_completed`` yields them, never waits for them here.

        The push ends the values of the snapshots taken since the last push:
        it refuses new requests that reference them and waits until the
        requests that read them have run.
        """
        self._push(element)


class Client:
    """
    A connection to a backend, for one proposal.

    ``with client:`` closes the client at the end of the block. A client that
    owns its backend, as :func:`local` makes one, also closes the backend.
    Any call of a client that has ended raises ``ClientEnded``.
    """

    def __init__(
        self,
        backend: Backend,
        *,
        proposal: str,
        submitter: str,
        owns_backend: bool = False,
    ) -> None:
        self._backend = backend
        self._owns_backend = owns_backend
        self._id = backend.open_client(proposal, submitter)
        self.proposal = proposal
        self.submitter = submitter
        self.datasets = Datasets(backend, self._id)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """
        End the client: release everything it keeps, and stop no work.

        A client that owns its backend then closes it, which waits until no
        record is pending.
        """
        self._backend.close_client(self._id)
        if self._owns_backend:
            self._backend.close()

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
        members of their records. An accumulator makes a snapshot, which is
        completed at once and takes no label or member.
        """
        stage = None
        if isinstance(what, WorkflowSpec | SpecId):
            what = Request(what, params)
        elif isinstance(what, Stage):
            stage, what = what.id, what.template.fill(params or {})
        elif params is not None:
            raise TypeError('params go with a spec or a stage')
        elif isinstance(what, Accumulator):
            if label is not None or member is not None:
                raise TypeError('a snapshot takes no label or member')
            return self._backend.snapshot(what.id, client=self._id)
        if isinstance(what, Mapping):
            if member is not None:
                raise TypeError('the keys of a dict are the members')
            members = [str(k) if label is not None else None for k in what]
        else:
            members = [member] * len(_items(what))
        entries = [
            Entry(request, label=label, member=m, name=name, stage=stage)
            for request, m, name in zip(
                _items(what), members, _names(what), strict=True
            )
        ]
        records = self._backend.submit(entries, client=self._id)
        return _reshape(what, records)

    def compute(self, *args: Any, **kwargs: Any) -> Any:
        """Submit, wait, and return the records."""
        records = self.submit(*args, **kwargs)
        self.wait(records)
        return records

    def status(self, records: Any) -> Any:
        """The status of each record now."""
        ids = [r.id for r in _items(records)]
        return _reshape(records, self._backend.status(ids, self._id))

    def wait(self, records: Any) -> Any:
        """The status of each record once all have finished; a failure is not raised."""
        ids = [r.id for r in _items(records)]
        return _reshape(records, self._backend.wait(ids, self._id))

    def failure(self, records: Any) -> Any:
        """Why each record failed; ``None`` for a record that has not failed."""
        failures = [self._backend.failure(r.id, self._id) for r in _items(records)]
        return _reshape(records, failures)

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
                        self._backend.when_finished(record.id, self._id, finished.put)
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
        self._backend.cancel([r.id for r in _items(records)], self._id)

    # Stages, accumulators, and what the client keeps

    def stage(self, template: Template) -> Stage:
        """
        A stage of the template; the client keeps it until it releases it.

        A template a request would refuse is refused here. The stage's
        template holds the values as the backend resolved them, so a dataset
        name stays the dataset it named when the stage was made.
        """
        stage_id, resolved = self._backend.open_stage(template, client=self._id)
        return Stage(resolved, stage_id)

    def accumulator(self, spec: WorkflowSpec) -> Accumulator:
        """
        An accumulator of ``spec``, whose only param must be a table.

        The client keeps it until it releases it.
        """
        if element_table(spec) is None:
            raise TypeError(f'{spec.name} does not take one table')
        accumulator_id = self._backend.open_accumulator(
            SpecId.of(spec), client=self._id
        )
        push = functools.partial(self._backend.push, accumulator_id, client=self._id)
        return Accumulator(spec, accumulator_id, push)

    def release(self, what: Any) -> None:
        """
        Release records, stages, or accumulators: one, a list, or a dict.

        A released record's outputs are dropped once the pending requests that
        read them have run; the record stays. A released stage takes no more
        calls. A released accumulator takes no more pushes or snapshots, and its
        snapshots' values end as at a push. Releasing stops no work.
        """
        self._backend.release([x.id for x in _items(what)], client=self._id)

    # Reading

    def output(self, record: Record, name: str) -> Any:
        """
        The value of an output, once the record has completed.

        A snapshot's value is a copy, so a later push does not change it.
        """
        (status,) = self._backend.wait([record.id], self._id)
        if status is not Status.COMPLETED:
            failure = self._backend.failure(record.id, self._id)
            raise RuntimeError(f'record {record.id} {status}: {failure}')
        return self._backend.output(record.id, name, self._id)

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
            for r in self._backend.records(self._id, label=label)
            if (spec_id is None or r.spec == spec_id)
            and (since is None or r.created >= since)
            and (until is None or r.created < until)
        ]

    def latest(self, label: str, member: str | None = None) -> Record:
        """The newest record under a label, of ``member`` if given, pending or not."""
        matching = [
            r for r in self.records(label=label) if member is None or r.member == member
        ]
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
                upstream[record_id] = self._backend.record(record_id, self._id)
                todo.extend(self._inputs(record_id))
        return Provenance(submitted=record.submitted, upstream=tuple(upstream.values()))

    def _inputs(self, record_id: str) -> list[str]:
        """The IDs of the records a record read."""
        return [ref.record for ref in self._backend.inputs(record_id, self._id)]


def local(
    *,
    proposal: str,
    datasets: DatasetSource,
    bind: Mapping[WorkflowSpec, Binding | Function],
    submitter: str = 'user',
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Client:
    """
    A client of its own backend in this process, running the workflows bound here.

    Closing the client closes the backend.
    """
    return Client(
        Backend(datasets, bind, clock=clock),
        proposal=proposal,
        submitter=submitter,
        owns_backend=True,
    )
