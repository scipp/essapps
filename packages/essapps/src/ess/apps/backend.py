# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The backend of one proposal checks requests, keeps records, and runs requests.
It also answers queries about the files of its file source.

The rules that clients see are in :mod:`ess.apps.protocol`, and this backend
implements them in this process. This docstring explains how it does so and how
its pieces fit.

What the backend knows of history is its log (see ``log.py``): every submission,
finished record, opened accumulator, and push is appended as one event and then
applied to its views (see ``views.py``). A change is checked before its event is
appended, so the log holds only events that apply. A backend given a log that
already has events applies them first; a log holds the history of one proposal,
so a log with an event of another proposal is refused. Nothing the earlier
backend kept survives, so only persisted work goes on (see
:meth:`Backend._restart`). The log is the backend's only record of history, so
if an append fails, for example on a full disk, the backend no longer knows its
own state and stops: every call waiting in it, and every later call, raises
``RuntimeError`` from the append's error.

The backend keeps the outputs of records, and its stages and accumulators. A
client is a connection to the backend: it keeps nothing, and every client reads
and references the same values. Output values, stages, accumulators, and what
waits for what are not history. This backend keeps them in memory, so a backend
started from an existing log cannot read the outputs of the backend that wrote
it.

Keepers and readers
-------------------
The backend keeps the outputs of the records it submitted without persisting
them, until they are released (``_kept``). The pending records, steps of
accumulators, and stages that have yet to read a value are its readers
(``_readers``, see ``_Read``). A value is dropped when the backend does not keep
it and no reader is left (``_drop``). A pending record whose outputs nothing
keeps is cancelled, since they would be dropped the moment they are computed;
cancelling lets go of what it reads, so this passes along a chain. A workflow
already running is not interrupted: its record is cancelled at once, and its
outputs are dropped when it returns. Once a stage has staged, or a step is
done, what the binding holds is its own.

A persist request made with a submission or a freeze replaces the backend's
hold: the worker writes the outputs named when the workflow returns, and the
record finishes once they are written. The store may hold a value that no event
names, written for a record cancelled meanwhile or by a write that failed
part-way; nothing reads it.

:meth:`Backend._refusal` decides what may be read, in the request phase, from
the record's existence, whether the backend keeps it, and what it persists.
So whether a call is accepted does not depend on how far other work has run,
and a pending record is as readable as a completed one. A read takes the
outputs the backend keeps from memory, and persisted ones from the store, after
the write; a workflow reads the outputs it references from memory while the
backend holds them.

A request waits until every record it references has completed, the record
of an accumulator's state until the accumulator has reached that state, and
an accumulator's steps as described below. This is the only scheduling there
is.

Request and call phase
----------------------
A request runs through its spec's binding (see ``ess.spec.binding``): a plain
request through ``compute``, a stage through ``make_stage``, and an
accumulator through ``make_accumulator``.

In the *request phase*, at submission, ``checks.py`` checks what needs no data.
The backend adds, under its lock, what depends on history and on what it
keeps: each reference is one that may be read, and its data fit the type of its
field (see :data:`ess.spec.validation.OutputReads`).

In the *call phase*, on a worker, the backend reads the output each reference
names and validates each value, a row of a list on its own, by the params model
from the JSON form the log holds, with each reference replaced by the data read
and each file by its local path (see :func:`ess.spec.validation.validated`).
A group that holds a reference is validated only then, so its validators see
the data. A plain request makes the params instance from these values without
validating again, so the params model's model validators never run; a stage or
an accumulator gets the values by name. An output that the spec declares
optional and the binding returns as ``None`` is left out; any other output that
is not an instance of its type fails the record.

Stages
------
A stage keeps the outputs its template references until it has staged. The
first request to run through it has the binding make its stage with the
template's values by name, each read and validated by its own field, and every
request through the stage runs through the callable the binding returned, with
the values of the blanks. The record of such a request still names the
template's values, but these references are provenance only: they are not
checked against what the backend keeps, waited for, or read, except by a
restart, which runs the record as its plain request (see
:meth:`Backend._restart`).

Accumulators
------------
An accumulator keeps the rows logged, by JSON form, to refuse a repeated row, so
a push takes time in proportion to its rows. The driver gets each step's values
as the log holds them, so that it and the record of a state agree. Opening and
pushing return once the checks pass and the event is logged, as a submission
does; they do not wait for a record that is pending.

Each accumulator works through its steps one at a time, in the order logged, on
the workers. A step starts once the records it references have completed, and a
push once the readers of the state before it are done. Opening has the binding
make the driver. A push is one push into the driver per table it gives rows of,
in the order given, each row first read and validated by its table's element
type. If a step fails, or a record it references has not completed, the
accumulator stops: it takes no more pushes or readers, the steps after it are
dropped, and the requests that wait for a state it will not reach fail. Its
driver is dropped once no reader holds its current state, without waiting for
it to be released.

A read pins the accumulator's state after the pushes logged so far, without
waiting for a push to be added. A submission that references the accumulator
logs, before its requests, one record of the plain request of the pinned state,
as the accumulator and its number of pushes; the requests reference that
record's outputs. The backend does not keep this record, so only the requests
of that submission read it. The first read of a state checks the number of rows of each
blank table against its length constraints, outside the backend's lock.

The outputs of a state are computed outside the lock, all at once, the first time
a record of the state or a read of its outputs needs them, and kept until the next
push. The record of a state gets what the driver returns, not a copy, which may
share memory with the driver; a read of the outputs copies them, or only the part
that a selection names. So the next push waits until the readers of the state
before it are done, since the driver cannot go back to that state and no reader
may see its outputs change. The readers are the records of that state, until the
requests that read them have run (even those cancelled while they run), and the
reads in progress. Every wait is for work logged before the waiter: a request
waits for records submitted before it and for pushes logged before its
submission, and a push waits for the records of the state before it and the
requests of their submissions, all logged before the push. So no wait goes round
in a circle.

Releasing an accumulator and freezing it are described at
:meth:`~ess.apps.protocol.BackendProtocol.release` and
:meth:`~ess.apps.protocol.BackendProtocol.freeze`, and closing the backend at
:meth:`Backend.close`.
"""

from __future__ import annotations

import copy
import threading
import uuid
from collections import Counter, deque
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from ess.spec import (
    AccumulatorBinding,
    Binding,
    Driver,
    FileRef,
    OutputRef,
    Ref,
    StageBinding,
    StageCall,
    WorkflowSpec,
    field_types,
    validation,
)
from ess.spec.refs import Loc, ParamsError, iter_refs, map_refs
from ess.spec.validation import OutputReads

from .checks import Checks, check_fit, distinct
from .client import Client
from .errors import FieldError, NotReadable, RecordFailed, SubmitError
from .files import Selector, readable
from .log import (
    Event,
    Finished,
    Log,
    NewRecord,
    NewStateRecord,
    Opened,
    Pushed,
    Submitted,
)
from .protocol import (
    BackendProtocol,
    Entry,
    FileSource,
    Selection,
    Store,
)
from .records import (
    ArrayInfo,
    Changes,
    Record,
    RecordStatus,
    Request,
    SpecId,
    Status,
    Template,
)
from .views import Views

type Spec = WorkflowSpec[Any, Any]


class _Failure(Exception):
    """
    A failure in the call phase that the backend words itself, such as a
    workflow that returns no instance of its outputs model.
    """


def _reason(error: Exception) -> str:
    """
    Why a step failed, as a failure records it. An error the framework raises
    with a message for the user, such as a value refused in the call phase,
    is its message as it stands. Any other, raised by a binding, a driver, a
    file source, or a store, is its type and message, such as
    ``KeyError: 'counts'``, which ``'counts'`` alone would not tell.
    """
    if isinstance(error, _Failure | ParamsError | SubmitError | NotReadable):
        return str(error)
    text = str(error)
    return f'{type(error).__name__}: {text}' if text else type(error).__name__


def _outputs_of(
    spec_id: SpecId, model: type[BaseModel], returned: Any
) -> dict[str, Any]:
    """
    The outputs a binding returned, an instance of the outputs ``model``, by
    name; an output that the spec declares optional and the binding returned
    as ``None`` is left out. Raises for anything else.
    """
    if not isinstance(returned, model):
        raise _Failure(
            f'the workflow of {spec_id} returned {type(returned).__name__}, '
            f'not {model.__name__}'
        )
    outputs = {}
    for name, field in model.model_fields.items():
        if (value := getattr(returned, name)) is not None:
            outputs[name] = value
        elif not field_types.optional(field.annotation)[1]:
            raise _Failure(f'the workflow of {spec_id} returned None as {name}')
    return outputs


def _not_returned(where: str) -> NotReadable:
    return NotReadable(f'{where}: the workflow did not return it')


def _element(value: Any, ref: OutputRef) -> Any:
    """
    ``value``, the output ``ref`` names, or its element ``ref.key`` if given:
    by index for a list.
    """
    if (key := ref.key) is None:
        return value
    try:
        return value[int(key)] if isinstance(value, list) else value[key]
    except (IndexError, KeyError):
        raise _Failure(f'{ref}: the output has no element {key!r}') from None


def _selected(value: Any, select: Selection) -> Any:
    """
    A copy of the part of an array, or of a group, that ``select`` names;
    needs no lock.

    An array is a value with named dimensions that slices as
    ``value[dim, index]``, such as a scipp variable or data array. A
    dimension it lacks, or an index out of range, raises what the slice
    raises. Of a group, each dimension slices the array fields that have
    it, as ``sc.DataGroup`` slices its items, and every other field is
    copied whole, a group in it too. Unlike a ``sc.DataGroup``, a group
    refuses a dimension that no array field has, with ``ValueError``.
    """
    if not isinstance(value, BaseModel):
        for dim, index in select.items():
            value = value[dim, index]
        return value.copy()
    fields = dict(value)
    for dim, index in select.items():
        sliced = {
            n: v[dim, index] for n, v in fields.items() if dim in getattr(v, 'dims', ())
        }
        if not sliced:
            raise ValueError(
                f'{type(value).__name__} has no array field with dimension {dim!r}'
            )
        fields |= sliced
    # A deep copy of a slice copies the slice only.
    return value.model_copy(update=copy.deepcopy(fields))


def _info(value: Any) -> Any:
    """
    What a value is, with no copy of an array: an :class:`ArrayInfo` for an array
    of at least one dimension, wherever it is held, a dict of its fields for a
    group, and a copy of any other value; needs no lock.
    """
    if isinstance(value, BaseModel):
        return {n: _info(v) for n, v in value}
    if isinstance(value, Mapping):
        return {k: _info(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_info(v) for v in value]
    if getattr(value, 'dims', ()):
        return ArrayInfo.of(value)
    return copy.deepcopy(value)


def _output_reads(params: Mapping[str, Any]) -> list[tuple[str, str]]:
    """The outputs of records that ``params`` reference, by record ID and name."""
    return [
        (ref.record, ref.output)
        for _, ref in iter_refs(params)
        if isinstance(ref, OutputRef)
    ]


def _refused(index: int, error: SubmitError) -> SubmitError:
    """The reasons of ``error``, each naming the request at ``index``."""
    return SubmitError(replace(e, entry=index) for e in error.errors)


def _every[T, R](items: Sequence[T], check: Callable[[int, T], R]) -> list[R]:
    """
    ``check`` of each item, by its index. Raises one ``SubmitError`` with the
    reasons of all items, so that a caller learns of every refused request at once.
    """
    results: list[R] = []
    errors: list[FieldError] = []
    for i, item in enumerate(items):
        try:
            results.append(check(i, item))
        except SubmitError as error:  # noqa: PERF203
            errors += error.errors
    if errors:
        raise SubmitError(errors)
    return results


class _Stage:
    """
    A stage: its spec's binding, its template, and the binding's callable
    once it has staged.

    The template's values are as given, with file names resolved when the
    stage was made. ``given`` names the values a call gives: the blanks.
    ``reads`` are the outputs of records the
    template references, which the stage keeps as a reader until it has
    staged, has stopped, or is released; it is empty from then on. The first
    call to run stages the binding under ``staging``, so that concurrent calls
    stage it once. ``call``, the callable the binding returned, and
    ``stopped``, why staging failed, change under the backend's lock. A stage
    that has stopped takes no more calls.
    """

    def __init__(
        self, binding: StageBinding, template: Template, given: frozenset[str]
    ) -> None:
        self.spec = template.spec
        self.binding = binding
        self.template = template
        self.given = given
        self.reads = _output_reads(template.params)
        self.call: StageCall | None = None
        self.stopped: str | None = None
        self.staging = threading.Lock()

    def read_part(self, request: Request) -> Request:
        """
        The part of a call's ``request`` that the call reads: all of it until
        the stage has staged, then only the values of its blanks; backend's
        lock held.
        """
        if self.call is None:
            return request
        params = {k: v for k, v in request.params.items() if k in self.given}
        return request.model_copy(update={'params': params})


_Step = tuple[dict[str, Any], list[tuple[str, str]]]
"""
A step of an accumulator: its values by name, the template's fixed values
for the opening and the rows of each table for a push, and the outputs of
records it reads.
"""


class _Accumulator:
    """
    An accumulator: its template, its spec's binding, its driver once it has
    opened, and the steps logged but not yet done.

    ``added`` is the number of pushes in the driver, ``None`` until the
    driver is made. ``steps`` holds each step not yet done, in the order
    logged, as its values by name, as the log holds them: first the
    template's fixed values, until the driver is made, then the rows of each
    push by table. Each comes with the
    outputs of records it reads, which it holds as a reader. ``busy`` says a
    worker makes the driver or pushes into it, outside the backend's lock.
    ``seen`` holds the rows of each blank table logged so far, by JSON form,
    with their indexes (see :func:`.checks.distinct`).

    ``checked`` holds the number of pushes of the last state whose plain
    request was checked, and why it would be refused, or ``None``. Two
    reads that check different states at once may overwrite each other's,
    which costs only another check.
    ``stopped`` says why the accumulator takes no more pushes or readers, or
    is ``None``: it failed, or the record of its freeze completed. ``released``
    says it was released: it then keeps only the steps that a
    reader still needs. ``freezing`` says a freeze is being checked; pushes
    wait until it ends. ``frozen`` names the record of its last freeze, once
    logged: the accumulator then takes no more pushes.

    :meth:`outputs` computes every output of the current state the first time
    a reader reads it, and keeps them until :meth:`drop`, which the backend
    calls before a push adds. They are computed under a lock of their own,
    outside the backend's, so ``driver.outputs`` is called from one thread at a
    time and never while a push adds.
    """

    driver: Driver  # once the accumulator has opened

    def __init__(
        self, accumulator_id: str, template: Template, binding: AccumulatorBinding
    ) -> None:
        self.id = accumulator_id
        self.template = template
        self.binding = binding
        self.added: int | None = None
        self.steps: deque[_Step] = deque()
        self.seen: dict[str, dict[bytes, int]] = {i: {} for i in template.blanks}
        self.busy = False
        self.checked: tuple[int, str | None] | None = None
        self.stopped: str | None = None
        self.released = False
        self.freezing = False
        self.frozen: str | None = None
        self._computing = threading.Lock()
        self._computed: dict[str, Any] | None = None

    @property
    def step(self) -> str:
        """The next step, as a failure names it."""
        return 'opening' if self.added is None else f'push {self.added}'

    def outputs(self) -> dict[str, Any]:
        """
        Every output of the current state that the driver returns, for one of
        its readers; needs no lock.

        They are what the driver returns, not a copy (see the module
        docstring), computed once for the state.
        """
        with self._computing:
            if self._computed is None:
                spec = self.binding.spec
                returned = self.driver.outputs()
                self._computed = _outputs_of(self.template.spec, spec.outputs, returned)
            return self._computed

    def drop(self) -> None:
        """
        Drop the outputs computed for the current state; the backend's lock
        held, and no reader left, so none computes.
        """
        self._computed = None

    def end(self) -> None:
        """
        Drop the driver of an accumulator that takes no more readers, once
        the last reader of its current state is done; the backend's lock held.
        """
        self._computed = None
        if hasattr(self, 'driver'):  # an accumulator that failed to open has none
            del self.driver


def _state_key(accumulator_id: str, upto: int) -> str:
    """What the record of the state after ``upto`` pushes waits for."""
    return f'{accumulator_id}[:{upto}]'


_Pinned = tuple[_Accumulator, int, str | None]
"""
A state a submission pinned: the accumulator, its number of pushes, and why
the plain request of that state would be refused, or ``None``.
"""

_Read = tuple[str, str] | tuple[_Accumulator, int]
"""
A value that is read: an output by record ID and name, or an accumulator's
state by its number of pushes. The readers of an output are the pending
records, steps, and stages that have yet to read it. The readers of a state
are the records of the state, the reads of the accumulator's outputs in
progress, and the submissions and freezes being checked, from the pin until
the records of the states are logged and hold it themselves: otherwise a push
could move past the pinned state before its record holds it.
"""


class Backend(BackendProtocol):
    """
    Checks requests, keeps records, and runs requests of one ``proposal`` (see
    the module docstring).

    ``files`` is its file source, and ``bind`` lists a binding for each
    spec it runs; each binding names its spec. Workflows, the steps of
    accumulators, and writes share ``workers`` threads. A long step or
    workflow holds one of them, so other work waits only once every worker is
    busy; submissions are accepted meanwhile. ``log`` holds history, in
    memory if not given, and ``store`` is where persisted outputs are
    written. A backend started on an existing log needs the store the earlier
    backend wrote to: without one, the persisted work it goes on with fails,
    since it has no store. Raises ``ValueError`` for a log with an event of
    another proposal.
    """

    def __init__(
        self,
        *,
        proposal: str,
        files: FileSource,
        bind: Sequence[Binding],
        workers: int = 4,
        log: Log | None = None,
        store: Store | None = None,
    ) -> None:
        self._proposal = proposal
        self._files = files
        self._store = store
        self._bindings: dict[SpecId, Binding] = {}
        self._checks: dict[SpecId, Checks] = {}
        if isinstance(bind, Mapping):
            raise TypeError('bind is a list of bindings, each of which names its spec')
        for binding in bind:
            if not isinstance(binding, Binding):
                raise TypeError(
                    f'bind is a list of bindings, such as a PipelineBinding, '
                    f'a DriverBinding, or a function_binding, not {binding!r}'
                )
            if (spec_id := SpecId.of(binding.spec)) in self._bindings:
                raise ValueError(f'two specs are bound as {spec_id}')
            self._bindings[spec_id] = binding
            self._checks[spec_id] = Checks(binding.spec, files)
        self._executor = ThreadPoolExecutor(max_workers=workers)
        self._changed = threading.Condition()
        self._log = Log() if log is None else log
        for event in self._log:
            if isinstance(event, Submitted | Opened) and event.proposal != proposal:
                raise ValueError(
                    f'the log holds history of proposal {event.proposal}, '
                    f'not {proposal}'
                )
        self._views = Views()
        self._stopped: Exception | None = None  # why an append failed
        self._closed = False
        # Not history
        # the records whose outputs the backend keeps: those submitted without
        # persisting outputs, until released
        self._kept: set[str] = set()
        self._stages: dict[str, _Stage] = {}  # open, by ID
        self._accumulators: dict[str, _Accumulator] = {}  # open, by ID
        # by record ID and name
        self._outputs: dict[tuple[str, str], Any] = {}
        # a value, as (record ID, name) or (accumulator, number of pushes), to
        # the number of its readers (see _Read)
        self._readers: Counter[_Read] = Counter()
        # the reverse, for the records that have not started
        self._unread: dict[str, list[_Read]] = {}
        # a record that has not started to the records and states it waits for
        self._waiting: dict[str, set[str]] = {}
        self._dependents: dict[str, set[str]] = {}
        self._accumulating: set[_Accumulator] = set()  # with steps not yet done
        self._stage_of: dict[str, _Stage] = {}  # unfinished record ID to its stage
        # the record of a state to the state it reads, until it lets go of it:
        # in _finish if it fails or is cancelled before it runs, in _run if it
        # does not complete or is the record of a freeze, and in _drop once
        # its outputs are dropped
        self._state_of: dict[str, tuple[_Accumulator, int]] = {}
        # values whose last reader let go, for _let_go's loop to work through
        self._unread_values: deque[_Read] = deque()
        self._letting_go = False
        with self._changed:
            for event in self._log:
                self._views.apply(event)
            self._restart()

    def close(self) -> None:
        """
        Release everything the backend keeps, wait until no record is pending
        and no accumulator has a step to do, then let go of the workers and the
        log. Closing again does nothing more. A backend that has stopped lets go
        without waiting, drops the work not yet started, and raises.

        Releasing cancels the pending work that persists nothing (see
        :meth:`release`), so what is left to wait for is the work that writes
        to the store, and what it reads. From the start of closing, every call,
        also one waiting, raises ``RuntimeError``.
        """
        with self._changed:
            self._closed = True
            if self._stopped is None:  # a backend that stopped logs nothing more
                self._release([*self._kept, *self._stages, *self._accumulators])
            self._changed.wait_for(
                lambda: (
                    self._stopped is not None
                    or (
                        self._views.records.keys() <= self._views.finished.keys()
                        and not self._accumulating
                    )
                )
            )
        self._executor.shutdown(wait=True, cancel_futures=self._stopped is not None)
        self._log.close()
        self._check_stopped()

    def _restart(self) -> None:
        """
        Decide the records left pending in the log the backend started from;
        lock held. Nothing the earlier backend kept survived, so only persisted
        work goes on:

        1. A pending record is cancelled unless it persists outputs, or a
           pending record that persists outputs reads it, directly or through
           other pending records.
        2. Otherwise it runs if each value it reads is a file, is written,
           or is an output of a pending record that runs, and fails if not.
           A record of a state fails, since the driver it reads is gone.
           A call through a stage runs as its plain request, since the stage
           is gone, so it reads the values of the stage's template too.

        A record that had written its persisted outputs, but
        not finished, runs again, and its writes replace what the store holds.
        """
        views = self._views
        pending = [i for i in views.records if i not in views.finished]
        needed = {i for i in pending if i in views.persisted}
        for record_id in reversed(pending):  # a record follows what it reads
            if record_id in needed and record_id not in views.states:
                upstream = views.records[record_id].request.upstream()
                needed.update(ref.record for ref in upstream)
        runs: set[str] = set()
        for record_id in pending:
            if record_id not in needed:
                self._finish(record_id, Status.CANCELLED, 'the backend restarted')
            elif (reason := self._unreadable(record_id, runs)) is not None:
                self._finish(
                    record_id, Status.FAILED, f'the backend restarted: {reason}'
                )
            else:
                runs.add(record_id)
        for record_id in (i for i in pending if i in runs):
            self._schedule(record_id)

    def _unreadable(self, record_id: str, runs: Collection[str]) -> str | None:
        """
        Why a record pending at a restart cannot read what it references, or
        ``None``;
        ``runs`` are the pending records that run. Lock held.
        """
        views = self._views
        if record_id in views.states:
            return 'the driver it reads is gone'
        for ref in views.records[record_id].request.upstream():
            where = f'record {ref.record} output {ref.output}'
            if ref.record in runs:
                continue
            if (status := views.status(ref.record)) is not Status.COMPLETED:
                return f'record {ref.record} {status}'
            if views.omitted((ref.record, ref.output)):
                return f'{where}: the workflow did not return it'
            if not views.persists((ref.record, ref.output)):
                return f'{where} was not persisted'
        return None

    def specs(self) -> list[Spec]:
        self._check_running()
        return [binding.spec for binding in self._bindings.values()]

    def _spec(self, spec_id: SpecId) -> Spec:
        try:
            return self._bindings[spec_id].spec
        except KeyError:
            raise SubmitError.at((), f'unknown spec {spec_id}') from None

    def _append(self, event: Event) -> None:
        """
        Append an event and apply it to the views; lock held.

        The caller has checked that the event applies. What is applied is the
        event as the log holds it, as it would be when the log is read again.
        If the append fails, the backend stops (see the module docstring).
        Either way, it wakes what waits, such as :meth:`changes`.
        """
        try:
            stored = self._log.append(event)
        except Exception as error:
            self._stopped = error
            self._changed.notify_all()
            raise
        self._views.apply(stored)
        self._changed.notify_all()

    def _check_stopped(self) -> None:
        """Raise if the backend has stopped."""
        if self._stopped is not None:
            raise RuntimeError(
                f'the backend stopped: appending to its log failed: {self._stopped}'
            ) from self._stopped

    def _check_running(self) -> None:
        """Raise if the backend has stopped or is closed."""
        self._check_stopped()
        if self._closed:
            raise RuntimeError('the backend is closed')

    def _wait_for(self, predicate: Callable[[], bool]) -> None:
        """
        Wait until ``predicate`` holds; lock held. Raises if the backend stops
        or closes.
        """
        self._changed.wait_for(
            lambda: self._stopped is not None or self._closed or predicate()
        )
        self._check_running()

    # Submission

    def submit(
        self,
        entries: list[Entry],
        submitter: str,
        *,
        persist: bool | Sequence[str] = False,
    ) -> list[Record]:
        """
        File names are resolved before the backend's lock is taken. The state of
        each accumulator that the requests reference is pinned to the pushes
        logged so far, and its plain request is checked outside the lock (see
        :meth:`_check_state`). The records are pending, and the backend keeps
        their outputs, but not those of the records of states. A persist
        request is logged with the submission (see the module docstring).
        """
        self._check_running()
        prepared = _every(entries, self._prepare)
        requests = [request for request, _ in prepared]
        persisted = [self._persisted(r.spec, persist) for r in requests]
        referenced = {ref.record for r in requests for ref in r.upstream()}
        with self._changed:
            self._check_running()
            pinned = self._pin_states(referenced)
        try:
            states = {
                i: (acc, upto, self._check_state(acc, upto))
                for i, (acc, upto) in pinned.items()
            }
            with self._changed:
                self._check_running()
                _every(
                    list(zip(entries, prepared, strict=True)),
                    lambda i, item: self._check_entry(
                        i, item[0], item[1][0], item[1][1], states
                    ),
                )
                state_ids = {i: uuid.uuid4().hex for i in states}
                return self._create(
                    entries, requests, submitter, states, state_ids, persisted
                )
        finally:
            with self._changed:
                self._let_go(pinned.values())

    def _create(
        self,
        entries: list[Entry],
        requests: list[Request],
        submitter: str,
        states: Mapping[str, _Pinned],
        state_ids: Mapping[str, str],
        persisted: Sequence[tuple[str, ...]],
        persist_states: tuple[str, ...] = (),
    ) -> list[Record]:
        """
        Log and schedule the checked requests as records of ``submitter``; lock
        held.

        The record of each state in ``states``, by accumulator ID, is logged
        and scheduled first, with its ID in ``state_ids``, and the references
        to its accumulator name that record's outputs. A freeze logs the
        record of its state alone, with no requests. ``persisted`` names the
        outputs each request persists, and ``persist_states`` those the
        records of the states persist, as a freeze may; the backend keeps the
        requests' records that persist none.
        """
        ids = [uuid.uuid4().hex for _ in entries]

        def outputs(spec_id: SpecId) -> tuple[str, ...]:
            return tuple(self._bindings[spec_id].spec.outputs.model_fields)

        def state_record(ref: Ref) -> Ref:
            if isinstance(ref, OutputRef) and ref.record in state_ids:
                return ref.model_copy(update={'record': state_ids[ref.record]})
            return ref

        requests = [
            r.model_copy(update={'params': map_refs(r.params, state_record)})
            for r in requests
        ]
        self._append(
            Submitted(
                time=datetime.now(UTC),
                proposal=self._proposal,
                submitter=submitter,
                records=(
                    *(
                        NewStateRecord(
                            id=state_ids[i],
                            accumulator=i,
                            pushes=upto,
                            outputs=outputs(acc.template.spec),
                            persist=persist_states,
                        )
                        for i, (acc, upto, _) in states.items()
                    ),
                    *(
                        NewRecord(
                            id=record_id,
                            request=request,
                            outputs=outputs(request.spec),
                            label=e.label,
                            member=e.member,
                            persist=names,
                        )
                        for record_id, request, e, names in zip(
                            ids, requests, entries, persisted, strict=True
                        )
                    ),
                ),
            )
        )
        self._kept.update(
            i for i, names in zip(ids, persisted, strict=True) if not names
        )
        for i, (acc, upto, _) in states.items():
            self._state_of[state_ids[i]] = (acc, upto)
            self._schedule(state_ids[i])
        for record_id, entry in zip(ids, entries, strict=True):
            if entry.stage is not None:
                self._stage_of[record_id] = self._stages[entry.stage]
            self._schedule(record_id)
        return [self._views.records[i] for i in ids]

    def _persisted(
        self, spec_id: SpecId, persist: bool | Sequence[str]
    ) -> tuple[str, ...]:
        """
        The outputs of the spec that ``persist`` names: every one if
        ``True``, none if false. Refuses an output the spec lacks, and any
        persist without a store; needs no lock.
        """
        if not persist:
            return ()
        if self._store is None:
            raise SubmitError.at(
                (), 'persisting needs a store, which this backend lacks'
            )
        declared = tuple(self._spec(spec_id).outputs.model_fields)
        if persist is True:
            return declared
        names = (persist,) if isinstance(persist, str) else tuple(persist)
        if lacking := [n for n in names if n not in declared]:
            raise SubmitError.at((), f'{spec_id} has no outputs {lacking} to persist')
        return names

    def _prepare(self, index: int, entry: Entry) -> tuple[Request, OutputReads]:
        """
        The request as the record holds it, checked as a plain request in the
        request phase, but for the records it reads, and the references to
        outputs in it (see :meth:`_check_reads`); needs no lock.
        """
        try:
            self._spec(entry.request.spec)  # an unknown spec is refused as such
            checks = self._checks[entry.request.spec]
            return checks.request(entry.request, self._proposal)
        except SubmitError as error:
            raise _refused(index, error) from None

    def _pin_states(self, ids: Iterable[str]) -> dict[str, tuple[_Accumulator, int]]:
        """
        The current state of each open accumulator among ``ids``, by ID, as the
        accumulator and its number of pushes logged, each held as a reader
        holds it until the caller lets go of it; lock held.

        An accumulator that is released, unknown, or takes no readers is left
        out.
        """
        pinned = {}
        for i in ids:
            acc = self._accumulators.get(i)
            if acc is not None and acc.stopped is None:
                pinned[i] = (acc, len(self._views.pushes.get(i, ())))
        self._readers.update(pinned.values())
        return pinned

    def _check_entry(
        self,
        index: int,
        entry: Entry,
        request: Request,
        reads: OutputReads,
        states: Mapping[str, _Pinned],
    ) -> None:
        """
        Check the stage a request of a submission goes through, if any, and
        the ``reads`` of the part of the request that the call reads (see
        :meth:`_check_stage` and :meth:`_check_reads`); lock held.
        """
        try:
            stage = self._check_stage(entry, request)
            also: Collection[tuple[str, str]] = ()
            if stage is not None:
                read, also = stage.read_part(request).params, stage.reads
                reads = [r for r in reads if r[0][0] in read]
            self._check_reads(reads, states, also)
        except SubmitError as error:
            raise _refused(index, error) from None

    def _check_reads(
        self,
        reads: OutputReads,
        states: Mapping[str, _Pinned],
        also: Collection[tuple[str, str]] = (),
    ) -> None:
        """
        Check the outputs of records and accumulators that a request reads,
        ``reads``, each with its position and the type its data must fit;
        lock held. Each must be one that may be referenced (see
        :meth:`_source_spec`), declared by its spec, and fit its field (see
        :func:`.checks.check_fit`).

        Only a submission reads accumulators, from the ``states`` it pinned;
        it reads the records of these states, which it makes in
        :meth:`_create`, after these checks. A stage, an accumulator, or a
        push passes no states: a stage or an accumulator that held a state of
        an accumulator would hold back its every push for as long as it
        lives, and a value pushed into one would read another's while it
        adds. A request through a stage that has yet to stage also reads
        ``also``, what the stage keeps, which :meth:`_check_stage` has checked
        it for; one through a stage that has staged reads only its blanks.
        """
        for loc, ref, tp in reads:
            source = self._source_spec(ref, loc, states, also)
            check_fit(self._bindings[source].spec, ref, tp, loc)

    def _check_stage(self, entry: Entry, request: Request) -> _Stage | None:
        """
        The stage a request goes through, if any, once checked; lock held.

        The request must have the stage's values outside the blanks, as given
        or resolved when the stage was made.
        """
        if entry.stage is None:
            return None
        stage = self._stages.get(entry.stage)
        if stage is None:
            raise SubmitError.at((), 'the stage was released or is unknown')
        if stage.stopped is not None:
            raise SubmitError.at((), stage.stopped)
        if stage.spec != request.spec:
            raise SubmitError.at(
                (), f'the stage holds {stage.spec}, not {request.spec}'
            )
        raw, template = entry.request, stage.template
        given = {k: v for k, v in raw.params.items() if k not in stage.given}
        fixed = template.params
        differ = (given.keys() ^ fixed.keys()) | {
            k for k in given.keys() & fixed.keys() if given[k] != fixed[k]
        }
        if differ:
            raise SubmitError.at(
                (), f"{sorted(differ)}: differ from the stage's values"
            )
        return stage

    def _source_spec(
        self,
        ref: OutputRef,
        loc: Loc,
        states: Mapping[str, _Pinned],
        also: Collection[tuple[str, str]],
    ) -> SpecId:
        """
        The spec of a record or accumulator a request may read; lock held.

        An accumulator must be open, not stopped, and its state pinned in
        ``states`` readable. A record's output must be one that may be
        referenced (see :meth:`_refusal`).
        """
        if ref.record in self._views.accumulators:
            try:
                acc = self._accumulator(ref.record)
            except LookupError as error:  # not readable
                raise SubmitError.at(loc, error.args[0]) from None
            if ref.record not in states:
                raise SubmitError.at(loc, 'only a request may reference an accumulator')
            if (refused := states[ref.record][2]) is not None:
                raise SubmitError.at(loc, refused)
            return acc.template.spec
        record = self._views.records.get(ref.record)
        if record is None:
            raise SubmitError.at(loc, f'unknown record or accumulator {ref.record}')
        if (why := self._refusal((ref.record, ref.output), also)) is not None:
            raise SubmitError.at(loc, why)
        return record.spec

    def _refusal(
        self, key: tuple[str, str], also: Collection[tuple[str, str]] = ()
    ) -> str | None:
        """
        Why a request may not reference an output of a record now, or ``None``;
        lock held.

        This is the read rule. The record must not have failed or been
        cancelled, and a record that has completed must have returned the
        output. The backend must keep the record, or the request's stage the
        output, ``also``, and it is read from memory; or the record must
        persist the output, and it is read from the store. A pending record
        passes: whether a request is accepted does not depend on how far what
        it reads has run. So no request references the record of a state,
        which only the submission that made it reads (see the module
        docstring), unless a freeze persisted it.
        """
        record_id, name = key
        status = self._views.status(record_id)
        if status in (Status.FAILED, Status.CANCELLED):
            return f'record {record_id} {status}'
        held = record_id in self._kept or key in also
        if not held and not self._views.persists(key):
            return f'record {record_id} is not kept'
        if self._views.omitted(key):
            return f'record {record_id} output {name}: the workflow did not return it'
        return None

    # Execution

    def _schedule(self, record_id: str) -> None:
        """
        Start the record, or let it wait for what it reads to finish; lock held.

        A record of a state reads only the state, which it waits for; the
        pushes have read the outputs its rows reference. A record through a
        stage that has staged reads only the outputs its blanks reference. An
        output is written before its record finishes, so waiting for the record also
        waits for the write.
        """
        state = self._state_of.get(record_id)
        if state is None:
            request = self._views.records[record_id].request
            if (stage := self._stage_of.get(record_id)) is not None:
                request = stage.read_part(request)
            refs = request.upstream()
            self._unread[record_id] = [(ref.record, ref.output) for ref in refs]
            waiting = {ref.record for ref in refs} - self._views.finished.keys()
        else:
            acc, upto = state
            self._unread[record_id] = [state]
            waiting = {_state_key(acc.id, upto)} if acc.added != upto else set()
        self._readers.update(self._unread[record_id])
        if not waiting:
            self._executor.submit(self._run, record_id)
            return
        self._waiting[record_id] = waiting
        for input_id in waiting:
            self._dependents.setdefault(input_id, set()).add(record_id)

    def _run(self, record_id: str) -> None:
        """
        Run a record's workflow, holding the values it reads until it returns,
        and write the outputs persisted at its submission before it completes.

        It holds them even once the record is cancelled, since the workflow
        still reads them, and lets go of them before the write. A record of a
        state that completes holds the state until its outputs are dropped
        (see :meth:`_drop`), except the record of a freeze, after which the
        accumulator takes no readers.
        """
        with self._changed:
            if record_id in self._views.finished:
                return
            record = self._views.records[record_id]
            stage = self._stage_of.get(record_id)
            state = self._state_of.get(record_id)
            reads = self._unread.pop(record_id)
        try:
            outputs = self._compute(record, stage, state)
        except Exception as error:  # any failure of a workflow is recorded
            with self._changed:
                self._finish(record_id, Status.FAILED, _reason(error))
                self._let_go(reads)
            return
        with self._changed:
            if state is None:
                self._let_go(reads)
            names = self._views.persisted.get(record_id, ())
        failure = self._write(record_id, {n: outputs[n] for n in names if n in outputs})
        with self._changed:
            # A record of a state that completed lets go of the state in
            # _drop, once its outputs are dropped. The record of a freeze
            # holds the outputs of the accumulator's last state, which takes
            # no more pushes, so it lets go here, and the accumulator takes
            # no more readers: that record is read.
            frozen = state[0] if state and state[0].frozen == record_id else None
            if frozen is not None:
                self._state_of.pop(record_id, None)
            self._complete(record_id, outputs, failure)
            completed = self._views.status(record_id) is Status.COMPLETED
            if frozen is not None and completed:
                frozen.stopped = (
                    f'the accumulator is frozen: read record {record_id} instead'
                )
            if state is not None and (not completed or frozen is not None):
                self._let_go(reads)

    def _compute(
        self,
        record: Record,
        stage: _Stage | None,
        state: tuple[_Accumulator, int] | None,
    ) -> dict[str, Any]:
        """
        The outputs a record's workflow returns, or those of the state it
        reads, by name, an optional output returned as ``None`` left out; no
        lock. What the workflow read goes out of scope here, so once the
        caller lets go of the values read, nothing holds them.
        """
        if state is not None:
            return dict(state[0].outputs())
        request = record.request
        spec = self._bindings[request.spec].spec
        if stage is None:
            params = spec.params.model_construct(
                **self._typed(request.spec, request.params)
            )
            returned = self._bindings[request.spec].compute(params)
        else:
            fixed = {n: v for n, v in request.params.items() if n not in stage.given}
            call = self._staged(
                stage,
                lambda: stage.binding.make_stage(
                    self._typed(request.spec, fixed), stage.template.blanks
                ),
            )
            given = {n: v for n, v in request.params.items() if n in stage.given}
            returned = call(self._typed(request.spec, given))
        return _outputs_of(request.spec, spec.outputs, returned)

    def _typed(self, spec_id: SpecId, values: Mapping[str, Any]) -> dict[str, Any]:
        """
        ``values`` of a request by name as the binding takes them; no lock.
        The data each reference names is read, and each value is validated by
        the params model from what the log holds, a table without its length
        constraints, with each reference replaced by its data and each file
        by its local path (see
        :func:`ess.spec.validation.validated`). A group that holds a
        reference is validated only here, so its validators run now.
        A plain request makes the params instance from them, and a stage or a
        driver takes them by name.
        """
        spec = self._bindings[spec_id].spec
        paths = validation.local_paths(values, self._files.path)
        context = {'read': self._reader(values), 'paths': paths}
        return {
            n: validation.validated(spec.params, n, v, n in spec.tables, context)
            for n, v in values.items()
        }

    def _staged(self, stage: _Stage, make: Callable[[], StageCall]) -> StageCall:
        """
        The stage's callable; ``make`` stages the binding, outside the
        backend's lock, if no call has yet.

        Once staged, the binding holds what it needs, and the stage lets go of
        what its template references. If staging fails, the stage stops, and
        lets go of them too: the error is that call's failure, and the calls
        after it fail with the reason.
        """
        with stage.staging:
            with self._changed:
                if stage.stopped is not None:
                    raise _Failure(stage.stopped)
                if stage.call is not None:
                    return stage.call
            try:
                call = make()
            except Exception as error:
                with self._changed:
                    reason = _reason(error)
                    stage.stopped = f'the stage stopped: staging failed: {reason}'
                    self._let_go_of_template(stage)
                raise
            with self._changed:
                stage.call = call
                self._let_go_of_template(stage)
            return call

    def _let_go_of_template(self, stage: _Stage) -> None:
        """Let go of what the stage's template references, once; lock held."""
        reads, stage.reads = stage.reads, []
        self._let_go(reads)

    def _reader(self, params: Mapping[str, Any]) -> Callable[[OutputRef], Any]:
        """
        What reads the data each reference in ``params`` names: outputs held
        in memory, or one element of each, taken under the backend's lock
        now, and outside it, outputs written and no longer held from the
        store, each once, so that the references to one output give one
        object. The caller reads only what something keeps for it, so an
        output is held or written.
        """
        held: dict[Ref, Any] = {}
        with self._changed:
            for _, ref in iter_refs(params):
                if not isinstance(ref, OutputRef):
                    continue
                key = (ref.record, ref.output)
                if self._views.omitted(key):
                    raise _not_returned(f'record {ref.record} output {ref.output}')
                if key in self._outputs:
                    held[ref] = _element(self._outputs[key], ref)

        def read(ref: OutputRef) -> Any:
            if ref not in held:
                held[ref] = self._stored(ref)
            return held[ref]

        return read

    def _stored(self, ref: OutputRef) -> Any:
        """
        The written value of an output, or its element if ``ref`` has a key.
        Needs no lock.
        """
        if self._store is None:
            raise NotReadable(f'{ref}: the backend has no store')
        return _element(self._store.read(ref.record, ref.output), ref)

    def _let_go(self, keys: Iterable[_Read]) -> None:
        """
        Count one reader less of each value; lock held. A record's output that
        no reader is left for is dropped if the backend does not keep it (see
        :meth:`_drop`), and a state of an accumulator lets the accumulator
        move on.

        Dropping may cancel a record, which lets go of what it reads in turn.
        The values left unread are worked through in one loop, not by
        recursion, so a chain of any length is let go of at once; a call
        within the loop only adds to it. The outputs of an accumulator's state
        go at the next push. An accumulator that takes no more pushes drops
        them once the last reader of its state is done, and one that takes no
        more readers drops its driver too.
        """
        for key in keys:
            self._readers[key] -= 1
            if not self._readers[key]:
                del self._readers[key]
                self._unread_values.append(key)
        if not self._letting_go:
            self._letting_go = True
            try:
                while self._unread_values:
                    match self._unread_values.popleft():
                        case (_Accumulator() as acc, upto) if acc.stopped:
                            # readers of later states hold no outputs
                            if upto == acc.added:
                                acc.end()
                        case (_Accumulator() as acc, upto) if (
                            acc.frozen and not acc.steps and upto == acc.added
                        ):
                            acc.drop()  # no push comes to drop them
                        case (_Accumulator() as acc, _) if acc.released:
                            self._unneeded(acc)
                        case (_Accumulator() as acc, _):
                            self._advance(acc)
                        case (str() as record_id, _):
                            self._drop(record_id)
            finally:
                self._letting_go = False
        self._changed.notify_all()

    def _drop(self, record_id: str) -> None:
        """
        Drop from memory the record's outputs that nothing keeps: not the
        backend and no reader (see ``_readers``); lock held. A written output
        is read from the store.

        A pending record that nothing keeps and that persists nothing is
        cancelled, since its outputs would be dropped the moment they are
        computed; cancelling lets go of
        what it reads, so this passes along a chain. A record of a state that
        has completed lets go of the state once its outputs are dropped.
        """
        if record_id in self._kept:
            return
        names = self._views.records.outputs(record_id)
        kept = [n for n in names if self._readers[(record_id, n)]]
        if record_id not in self._views.finished:
            if not kept and record_id not in self._views.persisted:
                self._finish(record_id, Status.CANCELLED, 'nothing keeps its outputs')
            return
        for name in names:
            if name not in kept:
                self._outputs.pop((record_id, name), None)
        state = self._state_of.get(record_id)
        if state is not None and not any(
            (record_id, n) in self._outputs for n in names
        ):
            del self._state_of[record_id]
            self._let_go([state])

    def _complete(
        self, record_id: str, outputs: dict[str, Any], failure: str | None
    ) -> None:
        """
        Complete a record with the outputs its workflow returned, once those
        persisted at its submission were written, or fail it with ``failure``
        if their write failed; lock held. Then drop from memory the outputs
        that nothing keeps (see :meth:`_drop`): the backend does not keep a
        record persisting outputs, or one released while it was pending.

        Nothing is logged for a record cancelled meanwhile, also if its
        outputs were written.
        """
        if record_id in self._views.finished:  # cancelled meanwhile
            return
        if failure is not None:
            self._finish(record_id, Status.FAILED, f'the write failed: {failure}')
            return
        for name, value in outputs.items():
            self._outputs[(record_id, name)] = value
        names = self._views.records.outputs(record_id)
        omitted = tuple(n for n in names if n not in outputs)
        self._finish(record_id, Status.COMPLETED, omitted=omitted)
        self._drop(record_id)

    # Writing

    def _write(self, record_id: str, values: Mapping[str, Any]) -> str | None:
        """Write the values to the store; why it failed, or ``None``; no lock."""
        store = self._store
        if store is None:  # a log with persist requests, read without one
            return 'the backend has no store' if values else None
        try:
            for name, value in values.items():
                store.write(record_id, name, value)
        except Exception as error:  # any failure of a write is logged
            return _reason(error)
        return None

    def _finish(
        self,
        record_id: str,
        status: Status,
        failure: str | None = None,
        omitted: tuple[str, ...] = (),
    ) -> None:
        """
        Finish a record and start or fail what waits for it; lock held.
        ``omitted`` names the outputs a completed record's workflow did not
        return.
        """
        todo = [(record_id, status, failure, omitted)]
        while todo:
            finished_id, finished, message, left_out = todo.pop()
            if finished_id in self._views.finished:
                continue
            self._append(
                Finished(
                    record=finished_id,
                    status=finished,
                    failure=message,
                    omitted=left_out,
                )
            )
            self._let_go(self._unread.pop(finished_id, ()))  # if it never started
            for key in self._waiting.pop(finished_id, ()):
                if (dependents := self._dependents.get(key)) is not None:
                    dependents.discard(finished_id)
                    if not dependents:
                        del self._dependents[key]
            self._stage_of.pop(finished_id, None)
            if finished is not Status.COMPLETED:  # lets go of the state with its reads
                self._state_of.pop(finished_id, None)
            for dependent in self._dependents.pop(finished_id, set()):
                if finished is not Status.COMPLETED:
                    todo.append(
                        (
                            dependent,
                            Status.FAILED,
                            f'record {finished_id} {finished}',
                            (),
                        )
                    )
                    continue
                self._satisfied(dependent, finished_id)
        for acc in list(self._accumulating):  # a step may wait for these records
            self._advance(acc)
        self._changed.notify_all()

    def _satisfied(self, record_id: str, key: str) -> None:
        """Start the record once ``key`` was the last thing it waited for; lock held."""
        waiting = self._waiting[record_id]
        waiting.discard(key)
        if not waiting:
            del self._waiting[record_id]
            self._executor.submit(self._run, record_id)

    # What the backend keeps

    def release(self, ids: Sequence[str]) -> None:
        """
        A pending record that nothing else keeps is cancelled (see :meth:`_drop`).
        The steps of a released accumulator are dropped again each time a reader
        lets go (see :meth:`_unneeded`), and its driver once its readers are done.
        """
        with self._changed:
            self._check_running()
            self._release(ids)

    def _release(self, ids: Iterable[str]) -> None:
        """Let go of the records, stages, and accumulators ``ids``; lock held."""
        for i in ids:
            if (stage := self._stages.pop(i, None)) is not None:
                self._let_go_of_template(stage)
            if (acc := self._accumulators.pop(i, None)) is not None:
                acc.released = True
                self._unneeded(acc)
            if i in self._kept:
                self._kept.remove(i)
                self._drop(i)
        self._changed.notify_all()

    def _unneeded(self, acc: _Accumulator) -> None:
        """
        Drop the steps of a released accumulator that no reader needs, and
        let go of what they read; lock held. Called at release, and again
        whenever the last reader of one of its states lets go.

        A reader needs the steps up to the state it pinned. The step a worker
        is doing cannot be stopped, so it stays. The records of states that
        wait for a dropped step have all been cancelled, since a pending one
        is a reader.
        """
        pinned = [upto for held, upto in self._readers if held is acc]
        done = -1 if acc.added is None else acc.added  # the opening is step 0
        needed = max(pinned, default=done) - done
        dropped = []
        while len(acc.steps) > max(needed, int(acc.busy)):
            dropped.append(acc.steps.pop()[1])
        for reads in dropped:
            self._let_go(reads)
        self._advance(acc)

    def open_stage(self, template: Template) -> tuple[str, Template]:
        """
        A stage, and its template as the stage holds it.

        The spec's binding must make stages. The template is checked (see
        :meth:`.checks.Checks.template`), and the stage keeps its values; a
        request through it must have them. The stage keeps the outputs they
        reference until its first call to run has made the binding's stage,
        or it is released.
        """
        spec_id = template.spec
        self._check_running()
        self._spec(spec_id)  # an unknown spec is refused as such
        binding = self._bindings[spec_id]
        if not isinstance(binding, StageBinding):
            raise SubmitError.at((), f'{spec_id}: its binding makes no stages')
        stored, _, reads = self._checks[spec_id].template(template, self._proposal)
        stage_id = uuid.uuid4().hex
        with self._changed:
            self._check_running()
            self._check_reads(reads, {})
            given = frozenset(stored.blanks)
            stage = self._stages[stage_id] = _Stage(binding, stored, given)
            self._readers.update(stage.reads)
        return stage_id, stored

    def open_accumulator(self, template: Template) -> tuple[str, Template]:
        """
        An accumulator, and its template as history holds it.

        The spec's binding must make accumulators, and the template's blanks
        are tables, which the pushes fill. The
        template is checked as a stage's is (see :meth:`open_stage`), and
        the records its values reference are checked as a request's are. The
        accumulator then opens: its first step has the binding make the driver
        with these values, defaults filled in, by name (see :meth:`_advance`).
        """
        spec_id, blanks = template.spec, template.blanks
        self._check_running()
        spec = self._spec(spec_id)
        tables = spec.tables
        binding = self._bindings[spec_id]
        if not isinstance(binding, AccumulatorBinding):
            raise SubmitError.at((), f'{spec_id}: its binding makes no accumulators')
        if not blanks or not set(blanks) <= set(tables):
            raise SubmitError.at(
                (),
                f'{spec_id}: the blanks of an accumulator are tables, '
                f'{list(tables)}, not {list(blanks)}',
            )
        stored, fixed, reads = self._checks[spec_id].template(template, self._proposal)
        accumulator_id = uuid.uuid4().hex
        with self._changed:
            self._check_running()
            self._check_reads(reads, {})
            # The blank tables without rows, so that the pushes extend them
            # into the plain request of each state.
            empty: dict[str, list[Any]] = {b: [] for b in blanks}
            self._append(
                Opened(
                    accumulator=accumulator_id,
                    proposal=self._proposal,
                    template=stored,
                    params={**fixed, **empty},
                )
            )
            opened = self._views.accumulators[accumulator_id]
            acc = _Accumulator(accumulator_id, opened.template, binding)
            self._accumulators[accumulator_id] = acc
            self._queue(acc, {n: opened.params[n] for n in fixed})
        return accumulator_id, acc.template

    def push(self, accumulator_id: str, rows: Mapping[str, list[Any]]) -> None:
        """
        Log a push of rows of one or more tables, by name; they enter one
        state.

        The rows are checked (see :meth:`.checks.Checks.push`), and so are
        the records they reference, as a request's are, without waiting for
        those that are pending. A row its table holds is refused.
        The push is logged before it is added, so the pushes into one
        accumulator are added in the order they were logged (see
        :meth:`_advance`). A push waits while a freeze is being checked, and
        is refused if the freeze is logged.
        """
        with self._changed:
            self._check_running()
            acc = self._pushed_into(accumulator_id)
        spec_id = acc.template.spec
        checks = self._checks[spec_id]
        values, reads = checks.push(acc.template.blanks, rows, self._proposal)
        with self._changed:
            acc = self._settled(accumulator_id)
            if acc.frozen is not None:
                raise SubmitError.at(
                    (), f'the accumulator was frozen as record {acc.frozen}'
                )
            seen = {t: distinct(t, v, acc.seen[t]) for t, v in values.items()}
            self._check_reads(reads, {})
            self._append(Pushed(accumulator=accumulator_id, params=values))
            for table, rows_seen in seen.items():
                acc.seen[table].update(rows_seen)
            pushed = self._views.pushes[accumulator_id][-1]
            self._queue(acc, pushed.params)

    def freeze(
        self,
        accumulator_id: str,
        submitter: str,
        *,
        persist: bool | Sequence[str] = False,
    ) -> Record:
        """
        Freeze the accumulator: the record of the plain request of its state
        after the pushes logged so far, which the backend keeps; with
        ``persist``, the store keeps it instead, as for a submission (see
        :meth:`submit`).

        The freeze pins that state as a read does, and its plain request is
        checked outside the lock, while pushes wait. If it would be refused,
        so is the freeze, and the waiting pushes are logged. Otherwise the
        record is logged, and the accumulator takes no more pushes. The
        record's outputs are those of the driver, computed once and not
        copied; once it has completed, the accumulator takes no readers, and
        its driver is dropped when its last reader is done. If the record
        fails or is cancelled, the accumulator may be read and frozen again,
        but takes no pushes. Its computed outputs are then dropped once the
        last reader of its state is done, since no push comes to drop them.
        """
        with self._changed:
            acc = self._settled(accumulator_id)
            persisted = self._persisted(acc.template.spec, persist)
            if acc.frozen is not None and self._views.status(acc.frozen) is (
                Status.PENDING
            ):
                raise SubmitError.at(
                    (), f'the accumulator is frozen as record {acc.frozen}'
                )
            acc.freezing = True
            upto = len(self._views.pushes.get(accumulator_id, ()))
            self._readers[(acc, upto)] += 1
        try:
            if (refused := self._check_state(acc, upto)) is not None:
                raise SubmitError.at((), refused)
            with self._changed:
                self._check_running()
                self._pushed_into(accumulator_id)  # not released or stopped
                record_id = uuid.uuid4().hex
                pinned = {accumulator_id: (acc, upto, None)}
                self._create(
                    [],
                    [],
                    submitter,
                    pinned,
                    {accumulator_id: record_id},
                    [],
                    persist_states=persisted,
                )
                if not persisted:
                    self._kept.add(record_id)
                acc.frozen = record_id
                return self._views.records[record_id]
        finally:
            with self._changed:
                acc.freezing = False
                self._let_go([(acc, upto)])

    def _queue(self, acc: _Accumulator, values: dict[str, Any]) -> None:
        """
        Queue a step of the accumulator: opening its driver with the
        template's fixed values, first, or a push of rows by table; lock
        held.

        The step holds the outputs it reads until it is done.
        """
        reads = _output_reads(values)
        self._readers.update(reads)
        acc.steps.append((values, reads))
        self._accumulating.add(acc)
        self._advance(acc)

    def _advance(self, acc: _Accumulator) -> None:
        """
        Start the accumulator's next step on a worker once nothing holds it
        back, or stop the accumulator if the step cannot be done; lock held.

        A step waits for the records it references to finish, and a push for
        the readers of the state before it to be done. The step stops the
        accumulator if one of these records has not completed.
        """
        if acc.busy or acc.stopped is not None:
            return
        if not acc.steps:
            self._accumulating.discard(acc)
            return
        if acc.added is not None and self._readers[(acc, acc.added)]:
            return
        *_, reads = acc.steps[0]
        for record_id, _ in reads:
            status = self._views.status(record_id)
            if status is Status.PENDING:
                return
            if status is not Status.COMPLETED:
                self._stop(acc, f'{acc.step}: record {record_id} {status}')
                return
        acc.busy = True
        acc.drop()
        self._executor.submit(self._do_step, acc)

    def _do_step(self, acc: _Accumulator) -> None:
        """
        Make the accumulator's driver or push into it, as its next step
        says; on a worker, outside the backend's lock.

        The values are given by name, each read and validated by its own
        field (see :meth:`_typed`). If the step fails, the accumulator stops,
        since the driver may hold part of the rows.
        """
        # Read outside the lock: only this worker removes the step it does,
        # _unneeded keeps a busy step, and _stop runs only when no step is
        # busy, or on this worker.
        values, reads = acc.steps[0]
        try:
            typed = self._typed(acc.template.spec, values)
            if acc.added is None:  # the opening, which makes the driver
                acc.driver = acc.binding.make_accumulator(typed, acc.template.blanks)
            else:
                for table, rows in typed.items():
                    acc.driver.push(table, rows)
        except Exception as error:  # the binding refuses the values or rows
            with self._changed:
                self._stop(acc, f'{acc.step} failed: {_reason(error)}')
            return
        with self._changed:
            acc.steps.popleft()
            acc.added = 0 if acc.added is None else acc.added + 1
            acc.busy = False
            self._let_go(reads)
            for record_id in self._dependents.pop(_state_key(acc.id, acc.added), ()):
                self._satisfied(record_id, _state_key(acc.id, acc.added))
            self._advance(acc)

    def _stop(self, acc: _Accumulator, failure: str) -> None:
        """
        Stop the accumulator: it takes no more pushes or readers, its steps
        are dropped, and the records of its states that wait for them fail;
        lock held. The driver is dropped now if no reader holds its
        current state, and otherwise once the last one is done.
        """
        acc.stopped = f'the accumulator stopped: {failure}'
        acc.busy = False
        self._accumulating.discard(acc)
        steps = list(acc.steps)
        acc.steps.clear()
        for *_, reads in steps:
            self._let_go(reads)
        for upto in range(len(self._views.pushes.get(acc.id, ())) + 1):
            for record_id in self._dependents.pop(_state_key(acc.id, upto), ()):
                self._finish(record_id, Status.FAILED, acc.stopped)
        if not self._readers[(acc, acc.added)]:
            acc.end()
        self._changed.notify_all()

    def _check_state(self, acc: _Accumulator, upto: int) -> str | None:
        """
        Why the plain request of the accumulator's state after ``upto`` pushes
        would be refused, or ``None``; needs no lock.

        That request is the one its records list (see ``views.py``): the
        template's fixed values, with each blank table filled by the rows
        pushed, in push order. The number of rows of each blank table must
        meet its length constraints, so a table that needs a row must have
        one (see :meth:`.checks.Checks.state`).
        ``acc.checked`` keeps the answer for the last state checked.
        """
        checked = acc.checked
        if checked is not None and checked[0] == upto:
            return checked[1]
        with self._changed:
            plain = self._views.plain(acc.id, upto)
        refused = self._checks[plain.spec].state(plain, acc.template.blanks)
        acc.checked = (upto, refused)  # another check may overwrite it at once
        return refused

    def _accumulator(self, accumulator_id: str) -> _Accumulator:
        """
        The open accumulator, if it takes readers; lock held. Raises
        ``KeyError`` for one never opened, and ``NotReadable`` for one
        released or opened before a restart, or that takes no readers.
        """
        acc = self._accumulators.get(accumulator_id)
        if acc is None:
            if accumulator_id not in self._views.accumulators:
                raise KeyError(
                    f'no accumulator {accumulator_id} in proposal {self._proposal}'
                )
            raise NotReadable(f'accumulator {accumulator_id} is not open')
        if acc.stopped is not None:
            raise NotReadable(acc.stopped)
        return acc

    def _pushed_into(self, accumulator_id: str) -> _Accumulator:
        """
        The accumulator for a push or a freeze; lock held. Raises as
        :meth:`_accumulator` does, but ``SubmitError`` where a read raises
        ``NotReadable``: a push or a freeze is refused, not read.
        """
        try:
            return self._accumulator(accumulator_id)
        except NotReadable as error:
            raise SubmitError.at((), str(error)) from None

    def _settled(self, accumulator_id: str) -> _Accumulator:
        """
        The accumulator, for a push or a freeze, once no freeze of it is being
        checked; lock held, and released while it waits.
        """
        while True:
            self._check_running()
            acc = self._pushed_into(accumulator_id)
            if not acc.freezing:
                return acc
            self._changed.wait()

    def _current(
        self, accumulator_id: str, *, reader: bool
    ) -> tuple[_Accumulator, int]:
        """
        The accumulator and its number of pushes logged, its current state
        pinned as a read pins it; held as a reader holds it if ``reader``,
        until the caller lets go of it. Raises ``NotReadable`` if the state
        may not be read (see :meth:`_accumulator`).
        """
        with self._changed:
            self._check_running()
            acc = self._accumulator(accumulator_id)
            upto = len(self._views.pushes.get(accumulator_id, ()))
            if reader:
                self._readers[(acc, upto)] += 1
        try:
            if (refused := self._check_state(acc, upto)) is not None:
                raise NotReadable(refused)
        except BaseException:  # a validator may raise anything
            if reader:
                with self._changed:
                    self._let_go([(acc, upto)])
            raise
        return acc, upto

    # Queries and control

    def _record(self, record_id: str) -> Record:
        """The record; lock held. Raises ``KeyError`` for one unknown."""
        record = self._views.records.get(record_id)
        if record is None:
            raise KeyError(f'no record {record_id} in proposal {self._proposal}')
        return record

    def status(self, ids: Sequence[str]) -> list[RecordStatus]:
        with self._changed:
            self._check_running()
            for record_id in ids:
                self._record(record_id)
            finished = [self._views.finished.get(i) for i in ids]
            return [
                RecordStatus(Status.PENDING)
                if f is None
                else RecordStatus(f.status, f.failure)
                for f in finished
            ]

    def cancel(self, ids: Sequence[str]) -> None:
        with self._changed:
            self._check_running()
            for record_id in ids:
                self._record(record_id)
                self._finish(record_id, Status.CANCELLED)

    def readable(self, ids: Sequence[str]) -> list[tuple[str, ...]]:
        with self._changed:
            self._check_running()
            return [
                tuple(
                    name
                    for name in self._record(record_id).outputs
                    if self._refusal((record_id, name)) is None
                )
                for record_id in ids
            ]

    def plain_request(self, accumulator_id: str) -> Request:
        acc, upto = self._current(accumulator_id, reader=False)
        with self._changed:
            return self._views.plain(acc.id, upto)

    def records(
        self,
        *,
        label: str | None = None,
        ids: Sequence[str] | None = None,
    ) -> list[Record]:
        if label is not None and ids is not None:
            raise ValueError('give a label or ids, not both')
        with self._changed:
            self._check_running()
            records = self._views.records
            if ids is not None:
                return [self._record(i) for i in ids]
            if label is not None:
                return [records[i] for i in self._views.labels.get(label, [])]
            return list(records.values())

    def changes(self, after: int, *, timeout: float = 0.0) -> Changes:
        """
        Wakes once an event is appended, or the backend closes (see
        :meth:`_append`).
        """
        with self._changed:
            self._check_running()
            self._changed.wait_for(
                lambda: (
                    self._stopped is not None
                    or self._closed
                    or self._views.changes(after).records
                ),
                timeout,
            )
            self._check_running()
            return self._views.changes(after)

    def outputs(
        self,
        record_id: str,
        names: Sequence[str] | None,
        *,
        select: Selection | None = None,
    ) -> dict[str, Any]:
        values = self._read(record_id, names, select=select)
        if select is None:
            return values
        return {n: _selected(v, select) for n, v in values.items()}

    def describe(self, record_id: str, name: str) -> dict[str, Any]:
        value = self._read(record_id, [name], select=None)[name]
        info = _info(value)
        return info if isinstance(value, BaseModel) else {name: info}

    def _read(
        self,
        record_id: str,
        names: Sequence[str] | None,
        *,
        select: Selection | None,
    ) -> dict[str, Any]:
        """
        The values of :meth:`outputs`, not copied, in memory or read from the
        store; ``select`` is checked here and applied by the caller.
        """
        views = self._views
        with self._changed:
            self._check_running()
            record = self._record(record_id)
            if select is not None:
                self._check_select(record.spec, names)

            wanted = record.outputs if names is None else names
            for name in wanted:
                if name not in record.outputs:
                    raise KeyError(f'{record.spec} has no output {name!r}')

            def ready() -> bool:
                if record_id in views.finished:
                    return True
                if record_id in self._kept:
                    return False
                return not any(views.persists((record_id, n)) for n in wanted)

            self._wait_for(ready)
            kept = record_id in self._kept
            every = names is None
            if names is None:
                names = [
                    n for n in record.outputs if kept or views.persists((record_id, n))
                ]
                if not names:
                    raise NotReadable(f'record {record_id} is not kept')
            finished = views.finished.get(record_id)
            if finished is not None and finished.status is not Status.COMPLETED:
                raise RecordFailed(record_id, finished.status, finished.failure)
            if every:
                names = [n for n in names if not views.omitted((record_id, n))]
            for name in names:
                if (why := self._refusal((record_id, name))) is not None:
                    raise NotReadable(why)
            # from memory if kept, else from the store
            values = {n: self._outputs[(record_id, n)] for n in names if kept}
        if not kept:
            try:
                values = {n: self._stored(record.ref(n)) for n in names}
            except LookupError as error:  # the store no longer holds it
                raise NotReadable(
                    f'record {record_id}: the store no longer holds an output'
                ) from error
        return values

    def _check_select(self, spec_id: SpecId, names: Sequence[str] | None) -> None:
        """
        Refuse a selection unless it names outputs, each declared as an array
        or as a group with an array field; needs no lock. A selection names
        the dimensions of the outputs it reads, so it reads only outputs named.
        """
        if names is None:
            raise ValueError('select= needs the name of an output')
        declared = self._bindings[spec_id].spec.outputs.model_fields
        for name in (n for n in names if n in declared):  # others raise when read
            if not field_types.has_array(declared[name].annotation):
                raise ValueError(
                    f'{spec_id} output {name!r}: select= needs an array, or a group '
                    'with an array field'
                )

    def accumulator_outputs(
        self,
        accumulator_id: str,
        names: Sequence[str] | None,
        *,
        select: Selection | None = None,
    ) -> dict[str, Any]:
        """
        The read is one of the state's readers while the state's plain request is
        checked, its outputs are computed if no reader has yet, and the outputs or
        their parts are copied (see the module docstring).
        """
        acc, upto = self._current(accumulator_id, reader=True)
        try:
            if select is not None:
                self._check_select(acc.template.spec, names)
            with self._changed:
                self._wait_for(lambda: acc.added == upto or acc.stopped is not None)
                if acc.added != upto:
                    raise NotReadable(acc.stopped)
            declared = list(self._bindings[acc.template.spec].spec.outputs.model_fields)
            for name in names or ():
                if name not in declared:
                    raise KeyError(f'{acc.template.spec} has no output {name!r}')
            try:
                outputs = acc.outputs()
            except Exception as error:  # the driver raised or returned no outputs
                raise NotReadable(_reason(error)) from error
            for name in names or ():
                if name not in outputs:
                    raise _not_returned(f'{accumulator_id}[:{upto}].{name}')
            values = {n: outputs[n] for n in names or declared if n in outputs}
            if select is None:
                return copy.deepcopy(values)
            return {n: _selected(v, select) for n, v in values.items()}
        finally:
            with self._changed:
                self._let_go([(acc, upto)])

    # Files, within the backend's proposal

    def files(self, selector: Selector) -> list[FileRef]:
        self._check_running()
        return [
            ref
            for ref in self._files.list(selector)
            if readable(self._files.metadata(ref), self._proposal)
        ]

    def file_metadata(self, refs: Sequence[FileRef]) -> list[dict[str, Any]]:
        self._check_running()
        proposal = self._proposal
        found = [self._files.metadata(ref) for ref in refs]
        for ref, metadata in zip(refs, found, strict=True):
            if not readable(metadata, proposal):
                raise KeyError(f'no file {ref} in proposal {proposal}')
        return found


def local(
    *,
    proposal: str,
    files: FileSource,
    bind: Sequence[Binding],
    submitter: str = 'user',
    store: Store | None = None,
) -> Client:
    """
    A client of its own backend in this process, running the workflows bound here.

    ``bind`` lists a binding for each spec the backend runs, such as
    ``[PipelineBinding(IOFQ, ...), DriverBinding(IOFQ_MULTI, ...)]``. Since the
    code runs in this process, a binding may also be of code defined in a
    notebook, such as ``function_binding(IOFQ, iofq)``.

    Closing the client closes the backend (see :meth:`Backend.close`): it
    releases everything the backend keeps, which cancels the pending work that
    persists nothing, and waits until what is persisted is written. A client that
    is never closed ends with its process. Without a ``store``, persisting is
    refused. The backend keeps history in memory, so its records end with the
    process.
    """
    backend = Backend(proposal=proposal, files=files, bind=bind, store=store)
    return Client(backend, submitter=submitter, on_close=backend.close)
