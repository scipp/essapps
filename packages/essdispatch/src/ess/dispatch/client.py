# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The client: what a notebook, an application, or a driver calls.

A client talks to one backend for one proposal. Calls that take records or
requests accept one, a list, or a dict, and return the same shape.

A record never changes; its status changes once, from pending to finished,
and is asked with :meth:`Client.status` or :meth:`Client.wait`.

A call that starts work returns once the backend has checked and logged it:
:meth:`Client.submit`, :meth:`Client.accumulator`, :meth:`Accumulator.push`,
and :meth:`Client.freeze`. It checks models and the references it names
against what the backend knows now, and leaves reading data and computing to
the backend's workers. Only the calls that read results wait for that work:
:meth:`Client.wait`, :meth:`Client.compute`, :meth:`Client.output`, and
:meth:`Client.as_completed`; and :meth:`Datasets.watch` waits for new
datasets. So a GUI application can make every other call from its UI thread.

A client is the lifetime of what it keeps: the outputs of the records it
makes, its stages, and its accumulators. It keeps each until it releases it or
ends. A pending request keeps the values it reads until it has run, and the
store keeps the outputs a client persists. Work that nothing keeps any more is
cancelled: a pending record whose outputs neither its client, nor a pending
request, nor a persist request keeps. A client reads, and its requests
reference, only the outputs it keeps and those persisted. It reads the outputs
it keeps from memory and the others from the store, so it never gets the
value another client holds.
"""

from __future__ import annotations

import functools
import queue
import threading
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Self

from pydantic import BaseModel

from ess.spec import AccumulatorRef, Binding, DatasetRef, Function, WorkflowSpec

from .backend import Backend, Entry, Selection
from .datasets import DatasetSource, Selector
from .records import Record, Request, Row, SpecId, Template
from .store import Store


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
    Where a record came from: what it ran, what it read, and their datasets.

    ``request`` is the record's request, or for the state of an accumulator,
    the plain request over the rows pushed so far. ``records`` lists every
    record read through all inputs, nearest first, the records of the states
    of accumulators among them; it stops at datasets.
    """

    request: Request
    upstream: tuple[Record, ...]

    def records(self) -> list[Record]:
        return list(self.upstream)

    def datasets(self) -> list[DatasetRef]:
        ran = (self.request, *(r.request for r in self.upstream))
        return list(dict.fromkeys(d for r in ran for d in r.datasets()))


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
    A template whose blanks, tables, are filled one push at a time.

    Its outputs are those of the plain request over the rows pushed so far,
    read by the client that opened it as a record's are: with
    ``client.output``, or by a request through :meth:`ref` and :meth:`refs`.
    A binding with a held state of its own adds each row to it, so earlier
    rows are not computed again; for any other binding, the backend keeps the
    rows and computes the plain request over all of them for each state read.
    The outputs of a state are computed all at once, at the first pin that
    needs them, and kept until the next push is added; no output is computed
    for a state that nobody pins. The first pin of a state also validates its
    plain request, which takes time in proportion to the rows.

    A push, and a submission that pins, return once logged, so a driver does
    not slow down when the backend falls behind. A driver that should skip
    pins meanwhile checks whether the record of its previous pin has finished.

    ``template`` holds the other values as the backend accepted them,
    ``outputs`` the output names the spec declares, and ``id`` names the
    accumulator in the backend.
    """

    def __init__(
        self,
        template: Template,
        accumulator_id: str,
        outputs: tuple[str, ...],
        push: Callable[[Mapping[str, Row]], None],
    ) -> None:
        self.template = template
        self.id = accumulator_id
        self.outputs = outputs
        self._push = push

    def push(self, rows: Mapping[str, Row]) -> None:
        """
        Add one row to each named table of the template, as a request over the
        table takes it; the rows enter one state. A name that is not one of
        the template's blanks is refused.

        A row names datasets or references outputs of records, never an
        accumulator. It is checked by its table's row model; rules on a whole
        table, such as its length, are checked when a state is pinned. So a
        table with a maximum length cannot be pinned again once a push
        exceeds it, since no row can be removed. A record that has failed or
        was cancelled is refused, and one that is pending is not waited for.
        The push returns once it is logged. The backend adds the pushes one at
        a time, in the order logged, each once the records its rows reference
        have completed and the readers of the state before it are done (see
        :mod:`ess.dispatch.backend`).

        If adding fails, or a record has not completed, the accumulator stops
        for good: the records of states it did not reach fail, and so do the
        requests that read them, and every later push and pin is refused. The
        reason names the step, such as ``push 0`` for the first push. A driver
        that should skip a run whose reduction failed waits for that record
        before it pushes.
        """
        self._push(rows)

    def ref(self, output: str) -> AccumulatorRef:
        """
        A reference to an output of the accumulator, for a request.

        Submitting the request pins the state after the rows pushed so far,
        makes a record of that state's plain request, and replaces the
        reference by the same output of that record. No client keeps that
        record, so only the requests of that submission read it. The request
        waits until the accumulator has reached the state, as it waits for a
        record it references, and is refused if the state's plain request
        would be refused. A request references at most one accumulator; a row
        or a template references none.
        """
        if output not in self.outputs:
            raise KeyError(f'{self.template.spec} has no output {output!r}')
        return AccumulatorRef(accumulator=self.id, output=output)

    def refs(self, *outputs: str) -> dict[str, AccumulatorRef]:
        """A reference to each of ``outputs`` by name; to every output if none."""
        return {name: self.ref(name) for name in outputs or self.outputs}


class Client:
    """
    A connection to a backend, for one proposal.

    ``with client:`` closes the client at the end of the block. A client that
    owns its backend, as :func:`local` makes one, also closes the backend.
    Any call of a client that has ended raises ``ClientEnded``, except
    :meth:`close`, which then does nothing.
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
        End the client: release everything it keeps, which cancels the
        work that nothing else keeps.

        It first waits until the outputs it asked to persist with
        :meth:`persist` are written, and raises ``RuntimeError`` naming each
        whose write failed, once it has ended. A client that owns its backend
        then closes it, which waits for the work that still runs: workflows
        already running, the pushes that readers of a released accumulator
        still need, and the records and writes that persist requests wait
        for. A client that is never closed ends with its process.
        """
        try:
            self._backend.close_client(self._id)
        finally:
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
        persist: bool | Sequence[str] = False,
    ) -> Any:
        """
        Submit a spec or a stage with values, or requests.

        Requests may be one, a list, or a dict. The records come back pending,
        in the shape given. Under a label, the keys of a dict become the
        members of their records. An accumulator is not submitted: a request
        reads it through :meth:`Accumulator.ref`.

        The backend checks the values against its own spec, so a ``SpecId``
        in place of the spec gets the same checks. A request that cannot run
        raises ``SubmitError`` naming the field at fault, after the request's
        key or index if several are given: an invalid value, an unknown spec
        or dataset, a reference the client may not read, or an output that
        does not fit its field. Then no request of the call is submitted; the
        records of earlier calls stay.

        With ``persist=True``, or the names of outputs, the client does not
        keep these records: the store keeps the outputs named, and the others
        are dropped once a record completes. Each record completes once they
        are written, and fails if the write fails. Unattended drivers submit
        this way.
        """
        stage = None
        if isinstance(what, WorkflowSpec | SpecId):
            what = Request(what, params)
        elif isinstance(what, Stage):
            stage, what = what.id, what.template.fill(params or {})
        elif isinstance(what, Accumulator):
            raise TypeError(
                'an accumulator is read by referencing it in a request: '
                'accumulator.ref(output)'
            )
        elif params is not None:
            raise TypeError('params go with a spec or a stage')
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
        records = self._backend.submit(entries, client=self._id, persist=persist)
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
        The stage keeps the outputs its template references until its first
        call has staged the binding, so the records they belong to may be
        released. If staging fails, the stage stops: the calls through it
        fail, and later ones are refused.

        A template a request would refuse is refused here. The stage's
        template holds the values as the backend resolved them, so a dataset
        name stays the dataset it named when the stage was made.
        """
        stage_id, resolved = self._backend.open_stage(template, client=self._id)
        return Stage(resolved, stage_id)

    def accumulator(self, template: Template) -> Accumulator:
        """
        An accumulator of the template; the client keeps it until it releases it.

        The template's blanks are one or more of the spec's table fields. A
        template a request would refuse is refused here, with the blanks left
        out, and so is one that references an accumulator. The held state gets
        each value typed by its own field and that field's validators; the
        accumulator's template holds the values as the backend resolved them.
        A record the template references that has failed or was cancelled is
        refused, and one that is pending is not waited for. Opening returns
        once the template is checked; the backend reads its values once, after
        the records they reference have completed, and the accumulator stops
        if the binding then fails to open.
        """
        accumulator_id, resolved = self._backend.open_accumulator(
            template, client=self._id
        )
        outputs = tuple(self._backend.spec(resolved.spec).outputs.model_fields)
        push = functools.partial(self._backend.push, accumulator_id, client=self._id)
        return Accumulator(resolved, accumulator_id, outputs, push)

    def freeze(
        self, accumulator: Accumulator, *, persist: bool | Sequence[str] = False
    ) -> Record:
        """
        The record of the accumulator's last state, the state after the pushes
        so far; the client keeps it, and the accumulator takes no more pushes.
        With ``persist``, the store keeps it instead, as with ``submit``.

        The record's outputs are what the held state returns, computed once
        and not copied. Once the record has completed, the accumulator is
        read through it: a read of the accumulator is refused and names the
        record, and the held state is dropped once its readers are done. A
        freeze is refused, and the accumulator takes pushes again, if the
        plain request of the state would be refused; a push made meanwhile
        waits for that answer. If the record fails or is cancelled, the
        accumulator can be read and frozen again. A template or a row, which
        may not reference an accumulator, references this record instead.
        """
        return self._backend.freeze(accumulator.id, client=self._id, persist=persist)

    def persist(self, records: Any, *outputs: str) -> None:
        """
        Write the named outputs, or every output, of records the client keeps
        to the store, each once it has completed; returns at once. It raises
        ``SubmitError``, and persists nothing, for a record the client does
        not keep, a failed or cancelled one, or an output a record lacks.

        The store then keeps them, and every client of the proposal reads and
        references them. This client keeps its hold until it releases a
        record, so it reads them from memory until then. If a write fails,
        the record stays as it is; a read by another client raises with the
        reason, this call may be made again, and :meth:`close` raises if it
        still failed. Persisting what is persisted does nothing.
        """
        ids = [r.id for r in _items(records)]
        self._backend.persist(ids, outputs, client=self._id)

    def release(self, what: Any) -> None:
        """
        Release records, stages, or accumulators: one, a list, or a dict.

        A released record's outputs are dropped once nothing else keeps them,
        such as a pending request that reads them; the record stays. A
        pending record that nothing else keeps is cancelled. A released stage
        takes no more calls, and lets go of what its template references if
        it has not staged. A
        released accumulator takes no more pushes or readers; the pushes up
        to the last state a pending read still holds are added, the later
        ones are dropped, and its state is dropped once its readers are done.
        """
        self._backend.release([x.id for x in _items(what)], client=self._id)

    # Reading

    def output(
        self,
        what: Record | Accumulator,
        name: str | None = None,
        *,
        select: Selection | None = None,
    ) -> Any:
        """
        The value of an output, or every output returned, by name, if ``name``
        is None; an optional output that was not returned is left out.

        A record's, once it has completed, if the client keeps it, or once
        written, if persisted; raises ``LookupError`` for a record it neither
        keeps nor persisted, whose write failed, or whose value the store no
        longer holds, and ``RuntimeError`` for one that fails or is cancelled.
        An accumulator's are those of its state after the pushes so far, once
        they are added, copied, so that like a record's they do not change.
        The read makes no record. It raises ``LookupError`` if the state's
        plain request would be refused, with its reason, also for a state
        with nothing pushed.

        ``select`` picks part of the named array output by dimension name, an
        index or a slice for each dimension it names, such as
        ``{'q': slice(0, 10)}``, and copies only that part. It is refused
        without ``name``, and for an output not declared as an array; a
        dimension the output lacks, or an index out of range, raises what the
        slice raises. A read of an accumulator holds back its next push while
        the state is checked, its outputs are computed if no reader has yet,
        and the part is copied.
        """
        names = None if name is None else [name]
        if isinstance(what, Accumulator):
            values = self._backend.accumulator_outputs(
                what.id, names, self._id, select=select
            )
        else:
            values = self._backend.outputs(what.id, names, self._id, select=select)
        return values if name is None else values[name]

    def records(self, *, label: str | None = None) -> list[Record]:
        """The proposal's records, oldest first, under ``label`` if given."""
        return self._backend.records(self._id, label=label)

    def latest(self, label: str, member: str | None = None) -> Record:
        """The newest record under a label, of ``member`` if given, pending or not."""
        matching = [
            r for r in self.records(label=label) if member is None or r.member == member
        ]
        if not matching:
            raise KeyError(f'no record under {label!r}, member {member!r}')
        return matching[-1]

    def provenance(self, what: Record | Accumulator) -> Provenance:
        """
        Where a record, or the current state of an accumulator, came from.

        An accumulator's state is the plain request over the rows pushed so
        far, so its provenance is that of the record of this request. It pins
        the state, but returns at once, without waiting for the pushes to be
        added.
        """
        if isinstance(what, Accumulator):
            request = self._backend.plain_request(what.id, self._id)
        else:
            request = what.request
        upstream: dict[str, Record] = {}
        todo = deque([request])
        while todo:
            for ref in todo.popleft().inputs():
                if ref.record not in upstream:
                    upstream[ref.record] = self._backend.record(ref.record, self._id)
                    todo.append(upstream[ref.record].request)
        return Provenance(request=request, upstream=tuple(upstream.values()))


def local(
    *,
    proposal: str,
    datasets: DatasetSource,
    bind: Mapping[WorkflowSpec, Binding | Function],
    submitter: str = 'user',
    store: Store | None = None,
) -> Client:
    """
    A client of its own backend in this process, running the workflows bound here.

    Closing the client closes the backend, which waits until what is persisted
    is written. Without a ``store``, persisting is refused. The backend keeps
    history in memory, so its records end with the process.
    """
    return Client(
        Backend(datasets, bind, store=store),
        proposal=proposal,
        submitter=submitter,
        owns_backend=True,
    )
