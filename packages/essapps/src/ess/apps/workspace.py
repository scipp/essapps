# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A workspace holds the history and the values of one proposal: it checks
requests, keeps records, and runs requests. It also answers queries about the
files of its file source.

The rules that clients see are in :mod:`ess.apps.protocol`, and this workspace
implements them in this process. This docstring explains how it does so and how
its pieces fit.

A method whose name ends in ``_locked`` must be called with the workspace's lock
(``_changed``) held. Any other method may be called without it, and takes the
lock where it needs it.

What the workspace knows of history is its log (see ``log.py``): every
submission, started and finished record, opened accumulator, and push is
appended as one event and then applied to its views (see ``views.py``). A change
is checked before its event is appended, so the log holds only events that
apply. A workspace given a log that already has events applies them first; a log
holds the history of one proposal, so a log with an event of another proposal is
refused. Nothing the earlier workspace kept survives, so only persisted work
that had not started goes on (see :meth:`Workspace._restart_locked`). The log is
the workspace's only record of history, so if an append fails, for example on a
full disk, the workspace no longer knows its own state and stops: every call
waiting in it, and every later call, raises ``RuntimeError`` from the append's
error.

The workspace keeps the outputs of records, and its stages and accumulators. A
client is a connection to the workspace: it keeps nothing, and every client reads
and references the same values. Output values, stages, accumulators, and what
waits for what are not history. This workspace keeps them in memory, so a workspace
started from an existing log cannot read the outputs of the workspace that wrote
it.

Keepers and readers
-------------------
The workspace holds in memory the outputs of the records that a submission or a
freeze made without persisting them, until they are released (``_in_memory``).
The pending records, steps of accumulators, and stages that have yet to read a
value are its readers (``_readers``, see ``_Read``). A value is dropped when the
workspace does not hold it in memory and no reader is left (``_drop_locked``). A
pending record whose outputs nothing keeps is cancelled, since they would be
dropped the moment they are computed; cancelling lets go of what it reads, so
this passes along a chain. A workflow already running is not interrupted: its
record is cancelled at once, and its outputs are dropped when it returns. Once a
stage has staged, or a step is done, what the binding holds is its own.

A persist request made with a submission or a freeze replaces the workspace's
hold: the worker writes the outputs named when the workflow returns, and the
record finishes once they are written. The store may hold a value that no event
names, written for a record cancelled meanwhile or by a write that failed
part-way; nothing reads it.

:meth:`Workspace._read_problem_locked` decides what may be read, in the request
phase, from the record's existence, whether the workspace holds it in memory,
and what it persists. So whether a call is accepted does not depend on how far
other work has run, and a pending record is as readable as a completed one. A
read takes the outputs the workspace holds in memory from there, and persisted
ones from the store, after the write; a workflow reads the outputs it references
from memory while the workspace holds them.

A request waits until every record it references has completed, the record
of a freeze until the accumulator has reached its state, and an accumulator's
steps as described below. This is the only scheduling there is.

Records of an unbound spec
--------------------------
The log holds the digest of each record's spec (see ``log.py``). A record is
of the bound spec if a binding has its spec's name, version, and digest, and
otherwise of an unbound spec (:meth:`Workspace._unbound_spec_problem`, which
:meth:`Workspace.bound` answers to a client). Every record that this
workspace logs is of the bound spec, so a record of an unbound spec comes from
the log it started from, and none of its values is in memory. Its persisted
outputs are read from the store as stored, without a type, so ``select=`` and
``describe`` take them as plain scipp data. Its outputs are not readable to a
request: a reference to one is refused, since its type cannot be checked, and
at a restart a pending record of an unbound spec fails, as does one that reads
an output of one.

Request and call phase
----------------------
A request runs through its spec's binding (see ``ess.spec.binding``): a plain
request through ``compute``, a stage through ``make_stage``, and an
accumulator through ``make_accumulator``.

In the *request phase*, at submission, ``checks.py`` checks what needs no data.
The workspace adds, under its lock, what depends on history and on what it
keeps: each reference is one that may be read, and its data fit the type of its
field (see :data:`ess.spec.validation.OutputReads`). :meth:`Workspace.resolve`
gives the first part alone, the request or template as a record would hold its
values, and logs nothing.

In the *call phase*, on a worker, the workspace reads the output each reference
names and validates each value, a row of a list on its own, by the params model
from the JSON form the log holds, with each reference replaced by the data read
and each file by its local path (see :func:`ess.spec.validation.validated`).
A group that holds a reference is validated only then, so its validators see
the data. A plain request makes the params instance from these values without
validating again, since the params model has no validators of its own, which
:class:`~ess.spec.WorkflowSpec` refuses; a stage or an accumulator gets the
values by name. An output that the spec declares
optional and the binding returns as ``None`` is left out; any other output that
is not an instance of its type fails the record.

Stages
------
A stage keeps the outputs its template references until it has staged. The first
request to run through it has the binding make its stage with the template's
values by name, each read and validated by its own field, and every request
through the stage runs through the callable the binding returned, with the
values of the blanks. The record of such a request still names the template's
values, but these references are provenance only: they are not checked against
what the workspace keeps, waited for, or read, except by a restart, which runs
the record as its plain request (see :meth:`Workspace._restart_locked`).

Accumulators
------------
An accumulator keeps the rows logged, by JSON form, to refuse a repeated row, so
a push takes time in proportion to its rows. The driver gets each step's values
as the log holds them, so that it and the record of a freeze agree. Opening and
pushing return once the checks pass and the event is logged, as a submission
does; they do not wait for a record that is pending.

Each accumulator works through its steps one at a time, in the order logged, on
the workers. A step starts once the records it references have completed, and a
push once the readers of the state before it are done. Opening has the binding
make the driver. A push is one push into the driver per table it gives rows of,
in the order given, each row first read and validated by its table's element
type. If a step fails, or a record it references has not completed, the
accumulator stops: it takes no more pushes or readers, the steps after it are
dropped, and the record of a freeze of a state it will not reach fails. Its
driver is dropped once no reader holds the state the driver is in, without
waiting for it to be released.

An accumulator's *logged state* is state n for the n pushes logged so far,
whether or not they have been added. A read is of the logged state when it is
called, and a freeze of the logged state once no other freeze of the
accumulator is being checked. A request never references an accumulator, only
the record of its freeze, so no request holds back a push.
A freeze logs one record of the plain request of that state, as the
accumulator and its number of pushes. The first read of a state checks the
number of rows of each blank table against its length constraints, outside the
workspace's lock.

The outputs of a state are computed outside the lock, all at once, the first time
the record of a freeze or a read of its outputs needs them, and kept until the
next push. The record of a freeze gets what the driver returns, not a copy, which
may share memory with the driver; a read of the outputs copies them, or only the
part that a selection names. So the next push waits until the readers of the
state before it are done, since the driver cannot go back to that state and no
reader may see its outputs change. The readers are the reads in progress, and a
freeze, while it is checked, and then its record, until it has finished and its
workflow, if it ran, has returned (see ``_Read``). Every wait is for work that
came before the waiter: a request waits for records submitted before it, a read
and the record of a freeze for the pushes logged before the read or the freeze
took its state, and a push for the readers of the state before it, which took
that state before the push was logged. So no wait goes round in a circle.

Releasing an accumulator and freezing it are described at
:meth:`~ess.apps.protocol.WorkspaceProtocol.release` and
:meth:`~ess.apps.protocol.WorkspaceProtocol.freeze`, and closing the workspace at
:meth:`Workspace.close`.
"""

from __future__ import annotations

import copy
import threading
import uuid
from collections import Counter, deque
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

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
from ess.spec.refs import Loc, ParamsError, iter_refs
from ess.spec.validation import OutputReads

from .checks import Checks, check_fit, distinct
from .client import Client
from .errors import NotReadable, RecordFailed, Stopped, SubmitError
from .files import Selector, readable
from .log import (
    Event,
    Finished,
    Log,
    NewRecord,
    NewStateRecord,
    Opened,
    Pushed,
    Started,
    Submitted,
)
from .protocol import (
    Entry,
    FileSource,
    Selection,
    Store,
    WorkspaceProtocol,
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
from .selection import check_select, selected
from .views import Views

type Spec = WorkflowSpec[Any, Any]


class _Failure(Exception):
    """
    A failure in the call phase that the workspace words itself, such as a
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


def _info(value: Any) -> Any:
    """
    What a value is, with no copy of an array: an :class:`ArrayInfo` for an
    array of at least one dimension, wherever it is held, a dict of its fields
    for a group, and a copy of any other value.
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


class _OutputKey(NamedTuple):
    """An output of a record that is read."""

    record: str
    output: str


def _output_reads(params: Mapping[str, Any]) -> list[_OutputKey]:
    """The outputs of records that ``params`` reference."""
    return [
        _OutputKey(ref.record, ref.output)
        for _, ref in iter_refs(params)
        if isinstance(ref, OutputRef)
    ]


def _refused(index: int, error: SubmitError) -> SubmitError:
    """The reasons of ``error``, each naming the request at ``index``."""
    return SubmitError(replace(e, request_key=index) for e in error.errors)


def _each[T, R](
    items: Sequence[T], check: Callable[[int, T], R]
) -> list[R | SubmitError]:
    """``check`` of each item, by its index, or the ``SubmitError`` it raised."""
    results: list[R | SubmitError] = []
    for i, item in enumerate(items):
        try:
            results.append(check(i, item))
        except SubmitError as error:  # noqa: PERF203
            results.append(error)
    return results


def _every[T, R](items: Sequence[T], check: Callable[[int, T], R]) -> list[R]:
    """
    ``check`` of each item, by its index. Raises one ``SubmitError`` with the
    reasons of all items, so that a caller learns of every refused request at once.
    """
    results = _each(items, check)
    if refused := [r for r in results if isinstance(r, SubmitError)]:
        raise SubmitError(e for r in refused for e in r.errors)
    return [r for r in results if not isinstance(r, SubmitError)]


@dataclass(frozen=True)
class _Bound:
    """
    What the workspace holds for a bound spec: its binding, its request-phase
    checks, and its digest, computed once since :attr:`~ess.spec.WorkflowSpec.digest`
    is computed on each access.
    """

    binding: Binding
    checks: Checks
    digest: str

    @property
    def spec(self) -> Spec:
        return self.binding.spec


class _Stage:
    """
    A stage: its spec's binding, its template, and the binding's callable
    once it has staged.

    The template's values are as given, with file names resolved when the
    stage was made. ``blanks`` names the values a call gives.
    ``reads`` are the outputs of records the
    template references, which the stage keeps as a reader until it has
    staged, has stopped, or is released; it is empty from then on. The first
    call to run stages the binding under ``staging``, so that concurrent calls
    stage it once. ``call``, the callable the binding returned, and
    ``stopped``, why staging failed, change under the workspace's lock. A stage
    that has stopped takes no more calls.
    """

    def __init__(self, binding: StageBinding, template: Template) -> None:
        self.spec = template.spec
        self.binding = binding
        self.template = template
        self.blanks = frozenset(template.blanks)
        self.reads = _output_reads(template.params)
        self.call: StageCall | None = None
        self.stopped: str | None = None
        self.staging = threading.Lock()

    def read_part_locked(self, request: Request) -> Request:
        """
        The part of a call's ``request`` that the call reads: all of it until
        the stage has staged, then only the values of its blanks.
        """
        if self.call is None:
            return request
        params = {k: v for k, v in request.params.items() if k in self.blanks}
        return request.model_copy(update={'params': params})


class _DriverInput(NamedTuple):
    """
    A step of an accumulator: its values by name, the template's fixed values
    for the opening and the rows of each table for a push, and the outputs of
    records it reads.
    """

    values: dict[str, Any]
    reads: list[_OutputKey]


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
    worker makes the driver or pushes into it, outside the workspace's lock.
    ``seen`` holds the rows of each blank table logged so far, by JSON form,
    with their indexes (see :func:`.checks.distinct`).

    ``checked`` holds the number of pushes of the state checked last, and
    why its plain request would be refused, or ``None``. Two
    reads that check different states at once may overwrite each other's,
    which costs only another check.
    ``stopped`` says why the accumulator takes no more pushes or readers, or
    is ``None``: it failed, or the record of its freeze completed. ``released``
    says it was released: it then keeps only the steps that a
    reader still needs. ``freezing`` says a freeze is being checked; pushes
    wait until it ends. ``frozen`` names the record of its last freeze, once
    logged: the accumulator then takes no more pushes.

    :meth:`outputs` computes every output of the state the driver is in the
    first time a reader reads it, and keeps them until :meth:`drop_locked`,
    which the workspace calls before a push adds. They are computed under a lock
    of their own, outside the workspace's, so ``driver.outputs`` is called from
    one thread at a time and never while a push adds.
    """

    driver: Driver  # once the accumulator has opened

    def __init__(
        self, accumulator_id: str, template: Template, binding: AccumulatorBinding
    ) -> None:
        self.id = accumulator_id
        self.template = template
        self.binding = binding
        self.added: int | None = None
        self.steps: deque[_DriverInput] = deque()
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
        Every output of the state the driver is in, for one of its readers.

        They are what the driver returns, not a copy (see the module
        docstring), computed once for the state.
        """
        with self._computing:
            if self._computed is None:
                spec = self.binding.spec
                returned = self.driver.outputs()
                self._computed = _outputs_of(self.template.spec, spec.outputs, returned)
            return self._computed

    def drop_locked(self) -> None:
        """
        Drop the outputs computed for the state the driver is in; no reader is
        left, so none computes.
        """
        self._computed = None

    def end_locked(self) -> None:
        """
        Drop the driver of an accumulator that takes no more readers, once the
        last reader of the state the driver is in is done.
        """
        self._computed = None
        if hasattr(self, 'driver'):  # an accumulator that failed to open has none
            del self.driver


def _state_key(accumulator_id: str, upto: int) -> str:
    """What the record of a freeze of the state after ``upto`` pushes waits for."""
    return f'{accumulator_id}[:{upto}]'


class _StateKey(NamedTuple):
    """A state of an accumulator that is read, by its number of pushes."""

    acc: _Accumulator
    pushes: int


_Read = _OutputKey | _StateKey
"""
A value that is read: an output of a record, or a state of an accumulator. The
readers of an output are the pending records, steps, and stages that have yet to
read it. The readers of a state are the reads of the accumulator's outputs in
progress, a freeze being checked, from its call until its record is logged and
holds the state itself, and the record of the freeze, until it has finished and
its workflow, if it ran, has returned.
"""


@dataclass
class _Plan:
    """
    How a record that has not finished runs, made when it is scheduled.

    ``reads`` are the values it holds as a reader until it starts, which then
    holds them itself, or finishes without starting. ``waits`` are the records
    and the states (see :func:`_state_key`) it waits for before it starts.
    ``compute`` returns its outputs by name, on a worker. ``state`` is the
    state that the record of a freeze reads, which it holds until it has
    finished, or ``None``.
    """

    reads: list[_Read]
    waits: set[str]
    compute: Callable[[], dict[str, Any]]
    state: _StateKey | None = None


class Workspace(WorkspaceProtocol):
    """
    Checks requests, keeps records, and runs requests of one ``proposal`` (see
    the module docstring).

    ``files`` is its file source, and ``bind`` lists a binding for each
    spec it runs; each binding names its spec. Workflows, the steps of
    accumulators, and writes share ``workers`` threads. A long step or
    workflow holds one of them, so other work waits only once every worker is
    busy; submissions are accepted meanwhile. The default is one thread: a
    workflow that is parallel inside gains nothing from more, and the peak
    memory grows with each request run at once. The best number depends on the
    instrument, so an application may set it. ``log`` holds history, in
    memory if not given, and ``store`` is where persisted outputs are
    written. A workspace started on an existing log needs the store the earlier
    workspace wrote to: without one, the persisted work it goes on with fails,
    since it has no store. The workspace owns its log and closes it in
    :meth:`close`. Raises ``ValueError`` for a log with an event of another
    proposal, and closes the log.
    """

    def __init__(
        self,
        *,
        proposal: str,
        files: FileSource,
        bind: Sequence[Binding],
        workers: int = 1,
        log: Log | None = None,
        store: Store | None = None,
    ) -> None:
        self._proposal = proposal
        self._files = files
        self._store = store
        self._bound: dict[SpecId, _Bound] = {}
        if isinstance(bind, Mapping):
            raise TypeError('bind is a list of bindings, each of which names its spec')
        for binding in bind:
            if not isinstance(binding, Binding):
                raise TypeError(
                    f'bind is a list of bindings, such as a PipelineBinding, '
                    f'a DriverBinding, or a function_binding, not {binding!r}'
                )
            if (spec_id := SpecId.of(binding.spec)) in self._bound:
                raise ValueError(f'two specs are bound as {spec_id}')
            spec = binding.spec
            self._bound[spec_id] = _Bound(binding, Checks(spec, files), spec.digest)
        self._log = Log() if log is None else log
        self._views = Views()
        try:
            for event in self._log:
                if isinstance(event, Submitted | Opened) and event.proposal != proposal:
                    raise ValueError(
                        f'the log holds history of proposal {event.proposal}, '
                        f'not {proposal}'
                    )
                self._views.apply(event)
        except ValueError:
            self._log.close()  # the workspace owns its log
            raise
        self._executor = ThreadPoolExecutor(max_workers=workers)
        self._changed = threading.Condition()
        self._stopped: Exception | None = None  # why an append failed
        self._closed = False
        # Not history
        # the records whose outputs the workspace holds in memory: those a
        # submission or a freeze made without persisting outputs, until released
        self._in_memory: set[str] = set()
        self._stages: dict[str, _Stage] = {}  # open, by ID
        self._accumulators: dict[str, _Accumulator] = {}  # open, by ID
        # by record ID and name
        self._outputs: dict[tuple[str, str], Any] = {}
        # a value to the number of its readers (see _Read)
        self._readers: Counter[_Read] = Counter()
        self._plans: dict[str, _Plan] = {}  # of the records not yet finished
        # a record or a state (see _state_key) to the records that wait for it
        self._dependents: dict[str, set[str]] = {}
        self._accumulating: set[_Accumulator] = set()  # with steps not yet done
        # values whose last reader let go, for _let_go_locked's loop to work through
        self._unread_values: deque[_Read] = deque()
        self._letting_go = False
        with self._changed:
            self._restart_locked()

    def close(self) -> None:
        """
        Release everything the workspace keeps, wait until no record is pending
        and no accumulator has a step to do, then let go of the workers and the
        log. Closing again does nothing more. A workspace that has stopped lets go
        without waiting, drops the work not yet started, and raises.

        Releasing cancels the pending work that persists nothing (see
        :meth:`release`), so what is left to wait for is the work that writes
        to the store, and what it reads. Once closing has started, every later
        call raises ``RuntimeError``, and so does a call waiting in the
        workspace. A call under way that does not wait returns or raises, by
        how far it has got. If an append fails while releasing, the workspace
        stops.
        """
        try:
            with self._changed:
                self._closed = True
                if self._stopped is None:  # a workspace that stopped logs nothing more
                    try:
                        self._release_locked(
                            [*self._in_memory, *self._stages, *self._accumulators]
                        )
                    except Exception as error:
                        if error is not self._stopped:  # not a failed append
                            raise
                self._changed.wait_for(
                    lambda: (
                        self._stopped is not None
                        or (
                            self._views.records.keys() <= self._views.finished.keys()
                            and not self._accumulating
                        )
                    )
                )
        finally:
            self._executor.shutdown(wait=True, cancel_futures=self._stopped is not None)
            self._log.close()
        self._check_stopped()

    def _restart_locked(self) -> None:
        """
        Decide the records left pending in the log the workspace started from.
        Nothing the earlier workspace kept survived, so only persisted work goes
        on:

        1. A pending record fails if it started: the workspace ended while it
           ran, perhaps because of it, as an out-of-memory kill does, so
           running it again could end the process again. A person submits its
           request again to run it.
        2. Otherwise it is cancelled unless it persists outputs, or a pending
           record that persists outputs reads it, directly or through other
           pending records.
        3. Otherwise it runs if it is of the bound spec, and each value it
           reads is a file, is written, or is an output of a pending record
           that runs, and of the bound spec; it fails if not.
           The record of a freeze fails, since the driver it reads is gone.
           A call through a stage runs as its plain request, since the stage
           is gone, so it reads the values of the stage's template too.

        So a record that runs at a restart had not started, and has written
        nothing.
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
            if record_id in views.started:
                self._finish_locked(
                    record_id, Status.FAILED, 'the workspace ended while it ran'
                )
            elif record_id not in needed:
                self._finish_locked(
                    record_id, Status.CANCELLED, 'the workspace restarted'
                )
            elif (reason := self._restart_problem(record_id, runs)) is not None:
                self._finish_locked(
                    record_id, Status.FAILED, f'the workspace restarted: {reason}'
                )
            else:
                runs.add(record_id)
        for record_id in (i for i in pending if i in runs):
            self._schedule_locked(record_id, self._request_plan_locked(record_id))

    def _restart_problem(self, record_id: str, runs: Collection[str]) -> str | None:
        """
        Why a record pending at a restart cannot read what it references, or
        ``None``; ``runs`` are the pending records that run.
        """
        views = self._views
        if record_id in views.states:
            return 'the driver it reads is gone'
        if (other := self._unbound_spec_problem(record_id)) is not None:
            return f'its spec is not bound: {other}'
        for ref in views.records[record_id].request.upstream():
            where = f'record {ref.record} output {ref.output}'
            if (other := self._unbound_spec_problem(ref.record)) is not None:
                return f'{where}: the spec of its record is not bound: {other}'
            if ref.record in runs:
                continue
            if (status := views.status(ref.record)) is not Status.COMPLETED:
                return f'record {ref.record} {status}'
            if views.omitted((ref.record, ref.output)):
                return f'{where}: the workflow did not return it'
            if not views.persists((ref.record, ref.output)):
                return f'{where} was not persisted'
        return None

    @property
    def proposal(self) -> str:
        return self._proposal

    def specs(self) -> list[Spec]:
        self._check_running()
        return [bound.spec for bound in self._bound.values()]

    def _unbound_spec_problem(self, record_id: str) -> str | None:
        """
        Why a record is of an unbound spec, or ``None`` if it is of the bound
        spec: one with the name, version, and digest of the record's (see the
        module docstring).
        """
        spec_id = self._views.records.spec(record_id)
        if (bound := self._bound.get(spec_id)) is None:
            return f'{spec_id} has no binding'
        if bound.digest != self._views.digests[record_id]:
            return f"{spec_id} is bound with other models than the record's"
        return None

    def _known(self, spec_id: SpecId) -> _Bound:
        """What is bound to a spec a call names; refuses an unknown spec."""
        try:
            return self._bound[spec_id]
        except KeyError:
            raise SubmitError.at((), f'unknown spec {spec_id}') from None

    def _append_locked(self, event: Event) -> None:
        """
        Append an event and apply it to the views.

        The caller has checked that the event applies. What is applied is the
        event as the log holds it, as it would be when the log is read again.
        If the append fails, the workspace stops (see the module docstring).
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
        """Raise if the workspace has stopped."""
        if self._stopped is not None:
            raise Stopped(
                f'the workspace stopped: appending to its log failed: {self._stopped}'
            ) from self._stopped

    def _check_running(self) -> None:
        """Raise if the workspace has stopped or is closed."""
        self._check_stopped()
        if self._closed:
            raise Stopped('the workspace is closed')

    def _wait_for_locked(self, predicate: Callable[[], bool]) -> None:
        """
        Wait until ``predicate`` holds. Raises if the workspace stops or closes.
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
        File names are resolved before the workspace's lock is taken. The records
        are pending, and the workspace holds in memory the outputs of those that
        persist none. A persist request is logged with the submission (see the module
        docstring).
        """
        self._check_running()
        prepared = _every([e.request for e in entries], self._prepare)
        requests = [request for request, _ in prepared]
        persisted = [self._persisted(r.spec, persist) for r in requests]
        with self._changed:
            self._check_running()
            _every(
                list(zip(entries, prepared, strict=True)),
                lambda i, item: self._check_entry_locked(
                    i, item[0], item[1][0], item[1][1]
                ),
            )
            ids = [uuid.uuid4().hex for _ in entries]
            self._log_submission_locked(
                submitter,
                [r.spec for r in requests],
                [
                    NewRecord(
                        id=record_id,
                        request=request,
                        outputs=self._output_names(request.spec),
                        label=e.label,
                        member=e.member,
                        persist=names,
                    )
                    for record_id, request, e, names in zip(
                        ids, requests, entries, persisted, strict=True
                    )
                ],
            )
            for record_id, entry in zip(ids, entries, strict=True):
                if entry.stage is None:
                    plan = self._request_plan_locked(record_id)
                else:
                    plan = self._stage_plan_locked(record_id, self._stages[entry.stage])
                self._schedule_locked(record_id, plan)
            return [self._views.records[i] for i in ids]

    def _log_submission_locked(
        self,
        submitter: str,
        spec_ids: Iterable[SpecId],
        records: Sequence[NewRecord | NewStateRecord],
    ) -> None:
        """
        Log a submission or a freeze of ``records`` of the bound ``spec_ids``,
        and hold in memory the outputs of those that persist none.
        """
        unlogged = self._views.unlogged({s: self._bound[s].digest for s in spec_ids})
        self._append_locked(
            Submitted(
                time=datetime.now(UTC),
                proposal=self._proposal,
                submitter=submitter,
                specs=unlogged,
                records=tuple(records),
            )
        )
        self._in_memory.update(r.id for r in records if not r.persist)

    def _output_names(self, spec_id: SpecId) -> tuple[str, ...]:
        """The names of the outputs the spec declares."""
        return tuple(self._bound[spec_id].spec.outputs.model_fields)

    def _persisted(
        self, spec_id: SpecId, persist: bool | Sequence[str]
    ) -> tuple[str, ...]:
        """
        The outputs of the spec that ``persist`` names: every one if ``True``,
        none if false. Refuses an output the spec lacks, and any persist without
        a store.
        """
        if not persist:
            return ()
        if self._store is None:
            raise SubmitError.at(
                (), 'persisting needs a store, which this workspace lacks'
            )
        declared = tuple(self._known(spec_id).spec.outputs.model_fields)
        if persist is True:
            return declared
        names = (persist,) if isinstance(persist, str) else tuple(persist)
        if lacking := [n for n in names if n not in declared]:
            raise SubmitError.at((), f'{spec_id} has no outputs {lacking} to persist')
        return names

    def resolve(
        self, items: list[Request | Template]
    ) -> list[Request | Template | SubmitError]:
        """
        Each request or template resolved (see :meth:`_resolved`), the part of
        :meth:`submit`'s checks that reads neither history nor what the
        workspace keeps.
        """
        self._check_running()
        return _each(items, self._resolved)

    def _resolved(self, index: int, item: Request | Template) -> Request | Template:
        """
        A request as :meth:`_prepare` gives it, or a template with every value
        but the blanks as :meth:`.checks.Checks.template` fixes it, defaults
        included. A refusal names the item by ``index``.
        """
        if isinstance(item, Request):
            return self._prepare(index, item)[0]
        try:
            checks = self._known(item.spec).checks
            _, fixed, _ = checks.template(item, self._proposal)
        except SubmitError as error:
            raise _refused(index, error) from None
        return Template(item.spec, params=fixed, blanks=item.blanks)

    def _prepare(self, index: int, request: Request) -> tuple[Request, OutputReads]:
        """
        The request as the record holds it, checked as a plain request in the
        request phase, but for the records it reads, and the references to
        outputs in it (see :meth:`_check_reads_locked`). A refusal names the
        request by ``index``.
        """
        try:
            return self._known(request.spec).checks.request(request, self._proposal)
        except SubmitError as error:
            raise _refused(index, error) from None

    def _check_entry_locked(
        self, index: int, entry: Entry, request: Request, reads: OutputReads
    ) -> None:
        """
        Check the stage a request of a submission goes through, if any, and the
        ``reads`` of the part of the request that the call reads (see
        :meth:`_check_stage_locked` and :meth:`_check_reads_locked`).
        """
        try:
            stage = self._check_stage_locked(entry, request)
            stage_reads: Collection[_OutputKey] = ()
            if stage is not None:
                read, stage_reads = stage.read_part_locked(request).params, stage.reads
                reads = [r for r in reads if r[0][0] in read]
            self._check_reads_locked(reads, stage_reads)
        except SubmitError as error:
            raise _refused(index, error) from None

    def _check_reads_locked(
        self, reads: OutputReads, stage_reads: Collection[_OutputKey] = ()
    ) -> None:
        """
        Check the outputs of records that a request, a template, or a push
        reads, ``reads``, each with its position and the type its data must fit.
        Each must be one that may be referenced (see
        :meth:`_source_spec_locked`), declared by its spec, and fit its field
        (see :func:`.checks.check_fit`).
        """
        for loc, ref, tp in reads:
            source = self._source_spec_locked(ref, loc, stage_reads)
            check_fit(self._bound[source].spec, ref, tp, loc)

    def _source_spec_locked(
        self, ref: OutputRef, loc: Loc, stage_reads: Collection[_OutputKey]
    ) -> SpecId:
        """
        The spec of a record a request may read. The record's output must be one
        that may be referenced (see :meth:`_read_problem_locked`), and the
        record of the bound spec, by which the output's type is checked.

        An accumulator is refused: a reader of its state would hold back its
        next push until the reader's workflow has returned, so a request reads
        the record of its freeze instead. The refusal names that record if the
        accumulator has one that is pending or has completed; at most one is,
        since a freeze is refused while one is pending and after one has
        completed.
        """
        if ref.record in self._views.accumulators:
            frozen = [
                record_id
                for record_id, (acc_id, _) in self._views.states.items()
                if acc_id == ref.record
                and self._views.status(record_id) in (Status.PENDING, Status.COMPLETED)
            ]
            which = f'its freeze, {frozen[0]}' if frozen else 'a freeze'
            raise SubmitError.at(
                loc,
                f'accumulator {ref.record} cannot be referenced; '
                f'a request references the record of {which}',
            )
        record = self._views.records.get(ref.record)
        if record is None:
            raise SubmitError.at(loc, f'unknown record or accumulator {ref.record}')
        if (
            why := self._read_problem_locked((ref.record, ref.output), stage_reads)
        ) is not None:
            raise SubmitError.at(loc, why)
        if (other := self._unbound_spec_problem(ref.record)) is not None:
            raise SubmitError.at(
                loc,
                f"the output's type cannot be checked: record {ref.record} is of "
                f'an unbound spec: {other}',
            )
        return record.spec

    def _read_problem_locked(
        self, key: tuple[str, str], stage_reads: Collection[_OutputKey] = ()
    ) -> str | None:
        """
        Why a request may not reference an output of a record now, or ``None``.

        This is the read rule. The record must not have failed or been
        cancelled, and a record that has completed must have returned the
        output. The workspace must hold the record in memory, or the request's
        stage the output, ``stage_reads``, and it is read from memory; or the
        record must persist the output, and it is read from the store. A pending
        record passes: whether a request is accepted does not depend on how far
        what it reads has run.
        """
        record_id, name = key
        status = self._views.status(record_id)
        if status in (Status.FAILED, Status.CANCELLED):
            return f'record {record_id} {status}'
        held = record_id in self._in_memory or key in stage_reads
        if not held and not self._views.persists(key):
            return f'record {record_id} is neither in memory nor persisted'
        if self._views.omitted(key):
            return f'record {record_id} output {name}: the workflow did not return it'
        return None

    # Execution

    def _request_plan_locked(self, record_id: str) -> _Plan:
        """The plan of a record of a plain request."""
        request = self._views.records[record_id].request
        return self._plan_locked(request, lambda: self._compute(request))

    def _plan_locked(
        self, request: Request, compute: Callable[[], dict[str, Any]]
    ) -> _Plan:
        """
        The plan of a record that reads the outputs ``request`` references, and
        waits for their records to finish. An output is written before its
        record finishes, so waiting for the record also waits for the write.
        """
        refs = request.upstream()
        return _Plan(
            reads=[_OutputKey(ref.record, ref.output) for ref in refs],
            waits={ref.record for ref in refs} - self._views.finished.keys(),
            compute=compute,
        )

    def _schedule_locked(self, record_id: str, plan: _Plan) -> None:
        """
        Start the record, or let it wait for what its plan waits for. The record
        holds what the plan reads as a reader.
        """
        self._plans[record_id] = plan
        self._readers.update(plan.reads)
        if not plan.waits:
            self._executor.submit(self._run, record_id)
            return
        for key in plan.waits:
            self._dependents.setdefault(key, set()).add(record_id)

    def _run(self, record_id: str) -> None:
        """
        Run a record's workflow, holding the values it reads until it returns,
        and write the outputs persisted at its submission before it completes.
        The record's start is logged before anything is read, so that a
        restart fails a record that was running when the workspace ended.

        It holds them even once the record is cancelled, since the workflow
        still reads them, and lets go of them before the write. The record of
        a freeze holds the state until it has finished; once it has completed,
        the accumulator takes no readers: that record is read.
        """
        with self._changed:
            if record_id in self._views.finished:
                return
            self._append_locked(Started(record=record_id))
            plan = self._plans[record_id]
            reads, plan.reads = plan.reads, []  # held here from now on
        try:
            outputs = plan.compute()
        except Exception as error:  # any failure of a workflow is recorded
            with self._changed:
                self._finish_locked(record_id, Status.FAILED, _reason(error))
                self._let_go_locked(reads)
            return
        with self._changed:
            if plan.state is None:
                self._let_go_locked(reads)
            names = self._views.persisted.get(record_id, ())
        failure = self._write(record_id, {n: outputs[n] for n in names if n in outputs})
        with self._changed:
            self._complete_locked(record_id, outputs, failure)
            if plan.state is not None:
                acc, _ = plan.state
                if self._views.status(record_id) is Status.COMPLETED:
                    acc.stopped = (
                        f'the accumulator is frozen: read record {record_id} instead'
                    )
                self._let_go_locked(reads)

    def _compute(self, request: Request) -> dict[str, Any]:
        """
        The outputs the workflow of a plain request returns, by name, an
        optional output returned as ``None`` left out. What the workflow read
        goes out of scope here, so once the caller lets go of the values read,
        nothing holds them.
        """
        bound = self._bound[request.spec]
        params = bound.spec.params.model_construct(
            **self._typed(request.spec, request.params)
        )
        returned = bound.binding.compute(params)
        return _outputs_of(request.spec, bound.spec.outputs, returned)

    def _typed(self, spec_id: SpecId, values: Mapping[str, Any]) -> dict[str, Any]:
        """
        ``values`` of a request by name as the binding takes them. The data each
        reference names is read, and each value is validated by the params model
        from what the log holds, a table without its length constraints, with
        each reference replaced by its data and each file by its local path (see
        :func:`ess.spec.validation.validated`). A group that holds a reference
        is validated only here, so its validators run now. A plain request makes
        the params instance from them, and a stage or a driver takes them by
        name.
        """
        spec = self._bound[spec_id].spec
        paths = validation.local_paths(values, self._path)
        context = {'read': self._reader(values), 'paths': paths}
        return {
            n: validation.validated(spec.params, n, v, n in spec.tables, context)
            for n, v in values.items()
        }

    def _path(self, ref: FileRef) -> Path:
        """
        The local path of a file. A record may hold the identity of a file the
        source no longer knows, moved or given a ``pid`` since the record was
        made, which fails the record.
        """
        try:
            return self._files.path(ref)
        except KeyError:
            raise _Failure(f'the file source does not know the file {ref}') from None

    def _reader(self, params: Mapping[str, Any]) -> Callable[[OutputRef], Any]:
        """
        What reads the data each reference in ``params`` names: outputs held
        in memory, or one element of each, taken under the workspace's lock
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
        The written value of an output, or its element if ``ref`` has a key: of
        the type the bound spec declares, or as stored for a record of an
        unbound spec.
        """
        if self._store is None:
            raise NotReadable(f'{ref}: the workspace has no store')
        tp = None
        if self._unbound_spec_problem(ref.record) is None:
            spec = self._bound[self._views.records.spec(ref.record)].spec
            tp = spec.outputs.model_fields[ref.output].annotation
        try:
            value = self._store.read(ref.record, ref.output, tp)
        except LookupError as error:
            raise NotReadable(
                f'{ref}: the store no longer holds it: {error}'
            ) from error
        return _element(value, ref)

    def _let_go_locked(self, keys: Iterable[_Read]) -> None:
        """
        Count one reader less of each value. A record's output that no reader is
        left for is dropped if the workspace does not hold it in memory (see
        :meth:`_drop_locked`), and a state of an accumulator lets the
        accumulator move on.

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
                        case _StateKey(acc, upto) if acc.stopped:
                            # readers of later states hold no outputs
                            if upto == acc.added:
                                acc.end_locked()
                        case _StateKey(acc, upto) if (
                            acc.frozen and not acc.steps and upto == acc.added
                        ):
                            acc.drop_locked()  # no push comes to drop them
                        case _StateKey(acc) if acc.released:
                            self._unneeded_locked(acc)
                        case _StateKey(acc):
                            self._advance_locked(acc)
                        case _OutputKey(record_id):
                            self._drop_locked(record_id)
            finally:
                self._letting_go = False
        self._changed.notify_all()

    def _drop_locked(self, record_id: str) -> None:
        """
        Drop from memory the record's outputs that nothing keeps: not the
        workspace and no reader (see ``_readers``). A written output is read
        from the store.

        A pending record that nothing keeps and that persists nothing is
        cancelled, since its outputs would be dropped the moment they are
        computed; cancelling lets go of
        what it reads, so this passes along a chain.
        """
        if record_id in self._in_memory:
            return
        names = self._views.records.outputs(record_id)
        kept = [n for n in names if self._readers[_OutputKey(record_id, n)]]
        if record_id not in self._views.finished:
            if not kept and record_id not in self._views.persisted:
                self._finish_locked(
                    record_id, Status.CANCELLED, 'nothing keeps its outputs'
                )
            return
        for name in names:
            if name not in kept:
                self._outputs.pop((record_id, name), None)

    def _complete_locked(
        self, record_id: str, outputs: dict[str, Any], failure: str | None
    ) -> None:
        """
        Complete a record with the outputs its workflow returned, once those
        persisted at its submission were written, or fail it with ``failure`` if
        their write failed. Then drop from memory the outputs that nothing keeps
        (see :meth:`_drop_locked`): the workspace does not keep a record
        persisting outputs, or one released while it was pending.

        Nothing is logged for a record cancelled meanwhile, also if its
        outputs were written.
        """
        if record_id in self._views.finished:  # cancelled meanwhile
            return
        if failure is not None:
            self._finish_locked(
                record_id, Status.FAILED, f'the write failed: {failure}'
            )
            return
        for name, value in outputs.items():
            self._outputs[(record_id, name)] = value
        names = self._views.records.outputs(record_id)
        omitted = tuple(n for n in names if n not in outputs)
        self._finish_locked(record_id, Status.COMPLETED, omitted=omitted)
        self._drop_locked(record_id)

    # Writing

    def _write(self, record_id: str, values: Mapping[str, Any]) -> str | None:
        """Write the values to the store; why it failed, or ``None``."""
        store = self._store
        if store is None:  # a log with persist requests, read without one
            return 'the workspace has no store' if values else None
        try:
            for name, value in values.items():
                store.write(record_id, name, value)
        except Exception as error:  # any failure of a write is logged
            return _reason(error)
        return None

    def _finish_locked(
        self,
        record_id: str,
        status: Status,
        failure: str | None = None,
        omitted: tuple[str, ...] = (),
    ) -> None:
        """
        Finish a record and start or fail what waits for it. ``omitted`` names
        the outputs a completed record's workflow did not return.
        """
        todo = [(record_id, status, failure, omitted)]
        while todo:
            finished_id, finished, message, left_out = todo.pop()
            if finished_id in self._views.finished:
                continue
            self._append_locked(
                Finished(
                    record=finished_id,
                    status=finished,
                    failure=message,
                    omitted=left_out,
                )
            )
            if (plan := self._plans.pop(finished_id, None)) is not None:
                self._let_go_locked(plan.reads)  # if it never started
                for key in plan.waits:
                    if (dependents := self._dependents.get(key)) is not None:
                        dependents.discard(finished_id)
                        if not dependents:
                            del self._dependents[key]
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
                self._satisfied_locked(dependent, finished_id)
        for acc in list(self._accumulating):  # a step may wait for these records
            self._advance_locked(acc)
        self._changed.notify_all()

    def _satisfied_locked(self, record_id: str, key: str) -> None:
        """Start the record once ``key`` was the last thing it waited for."""
        waits = self._plans[record_id].waits
        waits.remove(key)  # raises for a record that no longer waits for it
        if not waits:
            self._executor.submit(self._run, record_id)

    # What the workspace keeps

    def release(self, ids: Sequence[str]) -> None:
        """
        A pending record that nothing else keeps is cancelled (see
        :meth:`_drop_locked`). The steps of a released accumulator are dropped
        again each time a reader lets go (see :meth:`_unneeded_locked`), and its
        driver once its readers are done.
        """
        with self._changed:
            self._check_running()
            self._release_locked(ids)

    def _release_locked(self, ids: Iterable[str]) -> None:
        """Let go of the records, stages, and accumulators ``ids``."""
        for i in ids:
            if (stage := self._stages.pop(i, None)) is not None:
                self._let_go_of_template_locked(stage)
            if (acc := self._accumulators.pop(i, None)) is not None:
                acc.released = True
                self._unneeded_locked(acc)
            if i in self._in_memory:
                self._in_memory.remove(i)
                self._drop_locked(i)
        self._changed.notify_all()

    # Queries and control

    def _record_locked(self, record_id: str) -> Record:
        """The record. Raises ``KeyError`` for one unknown."""
        record = self._views.records.get(record_id)
        if record is None:
            raise KeyError(f'no record {record_id} in proposal {self._proposal}')
        return record

    def status(self, ids: Sequence[str]) -> list[RecordStatus]:
        with self._changed:
            self._check_running()
            for record_id in ids:
                self._record_locked(record_id)
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
                self._record_locked(record_id)
                self._finish_locked(record_id, Status.CANCELLED)

    def bound(self, ids: Sequence[str]) -> list[bool]:
        with self._changed:
            self._check_running()
            return [
                self._unbound_spec_problem(self._record_locked(i).id) is None
                for i in ids
            ]

    def readable(self, ids: Sequence[str]) -> list[tuple[str, ...]]:
        with self._changed:
            self._check_running()
            return [
                tuple(
                    name
                    for name in self._record_locked(record_id).outputs
                    if self._read_problem_locked((record_id, name)) is None
                )
                for record_id in ids
            ]

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
                return [self._record_locked(i) for i in ids]
            if label is not None:
                return [records[i] for i in self._views.labels.get(label, [])]
            return list(records.values())

    def changes(self, after: int, *, timeout: float = 0.0) -> Changes:
        """
        Wakes once an event is appended, or the workspace closes (see
        :meth:`_append_locked`).
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
        return {n: selected(v, select) for n, v in values.items()}

    def describe(self, record_id: str, name: str) -> dict[str, Any]:
        value = self._read(record_id, [name], select=None)[name]
        info = _info(value)
        # read as stored, any output but an array is a DataGroup: a mapping
        stored = self._unbound_spec_problem(record_id) is not None
        group = isinstance(value, BaseModel) or (stored and isinstance(value, Mapping))
        return info if group else {name: info}

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
        with self._changed:
            self._check_running()
            record = self._record_locked(record_id)
            if select is not None:
                typed = self._unbound_spec_problem(record_id) is None
                check_select(self._bound[record.spec].spec if typed else None, names)
            wanted = record.outputs if names is None else names
            for name in wanted:
                if name not in record.outputs:
                    raise KeyError(f'{record.spec} has no output {name!r}')
            self._wait_for_locked(lambda: self._read_ready_locked(record_id, wanted))
            in_memory = record_id in self._in_memory
            chosen = self._read_names_locked(record, names)
            # from memory if held there, else from the store
            values = {n: self._outputs[(record_id, n)] for n in chosen if in_memory}
        if not in_memory:
            values = {n: self._stored(record.ref(n)) for n in chosen}
        return values

    def _read_ready_locked(self, record_id: str, wanted: Sequence[str]) -> bool:
        """
        Whether a read of the outputs ``wanted`` stops waiting: once the record
        has finished, or at once if the workspace does not hold it in memory and
        it persists none of them, since there is nothing to wait for.
        """
        if record_id in self._views.finished:
            return True
        if record_id in self._in_memory:
            return False
        return not any(self._views.persists((record_id, n)) for n in wanted)

    def _read_names_locked(
        self, record: Record, names: Sequence[str] | None
    ) -> list[str]:
        """
        The outputs a read takes, once it has waited: ``names``, or if ``None``
        every output that the workspace holds in memory or the record persists
        and that the workflow returned. Raises ``RecordFailed`` for a record
        that failed or was cancelled, and ``NotReadable`` for an output that may
        not be read (see :meth:`_read_problem_locked`).
        """
        views, record_id = self._views, record.id
        if names is None:
            in_memory = record_id in self._in_memory
            held = [
                n for n in record.outputs if in_memory or views.persists((record_id, n))
            ]
            if not held:
                raise NotReadable(
                    f'record {record_id} is neither in memory nor persisted'
                )
            # only a completed record omits outputs, so a failed one raises below
            names = [n for n in held if not views.omitted((record_id, n))]
        finished = views.finished.get(record_id)
        if finished is not None and finished.status is not Status.COMPLETED:
            raise RecordFailed(record_id, finished.status, finished.failure)
        for name in names:
            if (why := self._read_problem_locked((record_id, name))) is not None:
                raise NotReadable(why)
        return list(names)

    # Files, within the workspace's proposal

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

    # Stages and accumulators

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
        bound = self._known(spec_id)
        binding = bound.binding
        if not isinstance(binding, StageBinding):
            raise SubmitError.at((), f'{spec_id}: its binding makes no stages')
        stored, _, reads = bound.checks.template(template, self._proposal)
        stage_id = uuid.uuid4().hex
        with self._changed:
            self._check_running()
            self._check_reads_locked(reads)
            stage = self._stages[stage_id] = _Stage(binding, stored)
            self._readers.update(stage.reads)
        return stage_id, stored

    def _check_stage_locked(self, entry: Entry, request: Request) -> _Stage | None:
        """
        The stage a request goes through, if any, once checked.

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
        fixed = {k: v for k, v in entry.request.params.items() if k not in stage.blanks}
        held = stage.template.params
        differ = (fixed.keys() ^ held.keys()) | {
            k for k in fixed.keys() & held.keys() if fixed[k] != held[k]
        }
        if differ:
            raise SubmitError.at(
                (), f"{sorted(differ)}: differ from the stage's values"
            )
        return stage

    def _stage_plan_locked(self, record_id: str, stage: _Stage) -> _Plan:
        """
        The plan of a record of a request through the stage. A record through a
        stage that has staged reads only the outputs its blanks reference.
        """
        request = self._views.records[record_id].request
        return self._plan_locked(
            stage.read_part_locked(request), lambda: self._call(stage, request)
        )

    def _call(self, stage: _Stage, request: Request) -> dict[str, Any]:
        """
        The outputs the stage's callable returns for a request through it, as
        :meth:`_compute` gives those of a plain request.
        """
        fixed = {n: v for n, v in request.params.items() if n not in stage.blanks}
        call = self._staged(
            stage,
            lambda: stage.binding.make_stage(
                self._typed(request.spec, fixed), stage.template.blanks
            ),
        )
        filled = {n: v for n, v in request.params.items() if n in stage.blanks}
        returned = call(self._typed(request.spec, filled))
        return _outputs_of(request.spec, stage.binding.spec.outputs, returned)

    def _staged(self, stage: _Stage, make: Callable[[], StageCall]) -> StageCall:
        """
        The stage's callable; ``make`` stages the binding, outside the
        workspace's lock, if no call has yet.

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
                    self._let_go_of_template_locked(stage)
                raise
            with self._changed:
                stage.call = call
                self._let_go_of_template_locked(stage)
            return call

    def _let_go_of_template_locked(self, stage: _Stage) -> None:
        """Let go of what the stage's template references, once."""
        reads, stage.reads = stage.reads, []
        self._let_go_locked(reads)

    def open_accumulator(self, template: Template) -> tuple[str, Template]:
        """
        An accumulator, and its template as history holds it.

        The spec's binding must make accumulators, and the template's blanks are
        tables, which the pushes fill. The template is checked as a stage's is
        (see :meth:`open_stage`), and the records its values reference are
        checked as a request's are. The accumulator then opens: its first step
        has the binding make the driver with these values, defaults filled in,
        by name (see :meth:`_advance_locked`).
        """
        spec_id, blanks = template.spec, template.blanks
        self._check_running()
        bound = self._known(spec_id)
        tables = bound.spec.tables
        binding = bound.binding
        if not isinstance(binding, AccumulatorBinding):
            raise SubmitError.at((), f'{spec_id}: its binding makes no accumulators')
        if not blanks or not set(blanks) <= set(tables):
            raise SubmitError.at(
                (),
                f'{spec_id}: the blanks of an accumulator are tables, '
                f'{list(tables)}, not {list(blanks)}',
            )
        stored, fixed, reads = bound.checks.template(template, self._proposal)
        accumulator_id = uuid.uuid4().hex
        with self._changed:
            self._check_running()
            self._check_reads_locked(reads)
            # The blank tables without rows, so that the pushes extend them
            # into the plain request of each state.
            empty: dict[str, list[Any]] = {b: [] for b in blanks}
            self._append_locked(
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
            self._queue_locked(acc, {n: opened.params[n] for n in fixed})
        return accumulator_id, acc.template

    def push(self, accumulator_id: str, rows: Mapping[str, list[Any]]) -> None:
        """
        Log a push of rows of one or more tables, by name; they enter one
        state.

        The rows are checked (see :meth:`.checks.Checks.push`), and so are the
        records they reference, as a request's are, without waiting for those
        that are pending. A row its table holds is refused. The push is logged
        before it is added, so the pushes into one accumulator are added in the
        order they were logged (see :meth:`_advance_locked`). A push waits while
        a freeze is being checked, and is refused if the freeze is logged.
        """
        with self._changed:
            self._check_running()
            acc = self._pushed_into_locked(accumulator_id)
        checks = self._bound[acc.template.spec].checks
        values, reads = checks.push(acc.template.blanks, rows, self._proposal)
        with self._changed:
            acc = self._settled_locked(accumulator_id)
            if acc.frozen is not None:
                raise SubmitError.at(
                    (), f'the accumulator was frozen as record {acc.frozen}'
                )
            seen = {t: distinct(t, v, acc.seen[t]) for t, v in values.items()}
            self._check_reads_locked(reads)
            self._append_locked(Pushed(accumulator=accumulator_id, params=values))
            for table, rows_seen in seen.items():
                acc.seen[table].update(rows_seen)
            pushed = self._views.pushes[accumulator_id][-1]
            self._queue_locked(acc, pushed.params)

    def freeze(
        self,
        accumulator_id: str,
        submitter: str,
        *,
        persist: bool | Sequence[str] = False,
    ) -> Record:
        """
        Freeze the accumulator: the record of the plain request of its logged
        state (see the module docstring), which the workspace keeps; with
        ``persist``, the store keeps it instead, as for a submission (see
        :meth:`submit`).

        The freeze is a reader of that state, as a read is, and its plain
        request is checked outside the lock, while pushes wait. If it would be refused,
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
            acc = self._settled_locked(accumulator_id)
            persisted = self._persisted(acc.template.spec, persist)
            if acc.frozen is not None and self._views.status(acc.frozen) is (
                Status.PENDING
            ):
                raise SubmitError.at(
                    (), f'the accumulator is frozen as record {acc.frozen}'
                )
            acc.freezing = True
            upto = len(self._views.pushes.get(accumulator_id, ()))
            self._readers[_StateKey(acc, upto)] += 1
        try:
            if (refused := self._state_problem(acc, upto)) is not None:
                raise SubmitError.at((), refused)
            with self._changed:
                self._check_running()
                self._pushed_into_locked(accumulator_id)  # not released or stopped
                record_id = uuid.uuid4().hex
                spec_id = acc.template.spec
                self._log_submission_locked(
                    submitter,
                    [spec_id],
                    [
                        NewStateRecord(
                            id=record_id,
                            accumulator=accumulator_id,
                            pushes=upto,
                            outputs=self._output_names(spec_id),
                            persist=persisted,
                        )
                    ],
                )
                acc.frozen = record_id
                self._schedule_locked(record_id, self._state_plan_locked(acc, upto))
                return self._views.records[record_id]
        finally:
            with self._changed:
                acc.freezing = False
                self._let_go_locked([_StateKey(acc, upto)])

    def _state_plan_locked(self, acc: _Accumulator, upto: int) -> _Plan:
        """
        The plan of the record of a freeze of the state after ``upto`` pushes.
        It reads only the state, which it waits for; the pushes have read the
        outputs its rows reference.
        """
        state = _StateKey(acc, upto)
        return _Plan(
            reads=[state],
            waits={_state_key(acc.id, upto)} if acc.added != upto else set(),
            compute=lambda: dict(acc.outputs()),
            state=state,
        )

    def _queue_locked(self, acc: _Accumulator, values: dict[str, Any]) -> None:
        """
        Queue a step of the accumulator: opening its driver with the template's
        fixed values, first, or a push of rows by table.

        The step holds the outputs it reads until it is done.
        """
        reads = _output_reads(values)
        self._readers.update(reads)
        acc.steps.append(_DriverInput(values, reads))
        self._accumulating.add(acc)
        self._advance_locked(acc)

    def _advance_locked(self, acc: _Accumulator) -> None:
        """
        Start the accumulator's next step on a worker once nothing holds it
        back, or stop the accumulator if the step cannot be done.

        A step waits for the records it references to finish, and a push for
        the readers of the state before it to be done. The step stops the
        accumulator if one of these records has not completed.
        """
        if acc.busy or acc.stopped is not None:
            return
        if not acc.steps:
            self._accumulating.discard(acc)
            return
        if acc.added is not None and self._readers[_StateKey(acc, acc.added)]:
            return
        for record_id, _ in acc.steps[0].reads:
            status = self._views.status(record_id)
            if status is Status.PENDING:
                return
            if status is not Status.COMPLETED:
                self._stop_locked(acc, f'{acc.step}: record {record_id} {status}')
                return
        acc.busy = True
        acc.drop_locked()
        self._executor.submit(self._do_step, acc)

    def _do_step(self, acc: _Accumulator) -> None:
        """
        Make the accumulator's driver or push into it, as its next step
        says; on a worker, outside the workspace's lock.

        The values are given by name, each read and validated by its own
        field (see :meth:`_typed`). If the step fails, the accumulator stops,
        since the driver may hold part of the rows.
        """
        # Read outside the lock: only this worker removes the step it does,
        # _unneeded_locked keeps a busy step, and _stop_locked runs only when no
        # step is busy, or on this worker.
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
                self._stop_locked(acc, f'{acc.step} failed: {_reason(error)}')
            return
        with self._changed:
            acc.steps.popleft()
            acc.added = 0 if acc.added is None else acc.added + 1
            acc.busy = False
            self._let_go_locked(reads)
            for record_id in self._dependents.pop(_state_key(acc.id, acc.added), ()):
                self._satisfied_locked(record_id, _state_key(acc.id, acc.added))
            self._advance_locked(acc)

    def _stop_locked(self, acc: _Accumulator, failure: str) -> None:
        """
        Stop the accumulator: it takes no more pushes or readers, its steps are
        dropped, and the record of a freeze that waits for them fails. The
        driver is dropped now if no reader holds the state it is in, and
        otherwise once the last one is done.
        """
        acc.stopped = f'the accumulator stopped: {failure}'
        acc.busy = False
        self._accumulating.discard(acc)
        steps = list(acc.steps)
        acc.steps.clear()
        for step in steps:
            self._let_go_locked(step.reads)
        for upto in range(len(self._views.pushes.get(acc.id, ())) + 1):
            for record_id in self._dependents.pop(_state_key(acc.id, upto), ()):
                self._finish_locked(record_id, Status.FAILED, acc.stopped)
        if not self._readers[_StateKey(acc, acc.added)]:
            acc.end_locked()
        self._changed.notify_all()

    def _unneeded_locked(self, acc: _Accumulator) -> None:
        """
        Drop the steps of a released accumulator that no reader needs, and let
        go of what they read. Called at release, and again whenever the last
        reader of one of its states lets go.

        A reader needs the steps up to the state it reads. The step a worker
        is doing cannot be stopped, so it stays. No record of a freeze waits
        for a dropped step, since a pending one is a reader.
        """
        read = [upto for held, upto in self._readers if held is acc]
        done = -1 if acc.added is None else acc.added  # the opening is step 0
        needed = max(read, default=done) - done
        dropped = []
        while len(acc.steps) > max(needed, int(acc.busy)):
            dropped.append(acc.steps.pop().reads)
        for reads in dropped:
            self._let_go_locked(reads)
        self._advance_locked(acc)

    def _state_problem(self, acc: _Accumulator, upto: int) -> str | None:
        """
        Why the plain request of the accumulator's state after ``upto`` pushes
        would be refused, or ``None``.

        That request is the one its records list (see ``views.py``): the
        template's fixed values, with each blank table filled by the rows
        pushed, in push order. The number of rows of each blank table must
        meet its length constraints, so a table that needs a row must have
        one (see :meth:`.checks.Checks.state_problem`).
        ``acc.checked`` keeps the answer for the state checked last.
        """
        checked = acc.checked
        if checked is not None and checked[0] == upto:
            return checked[1]
        with self._changed:
            plain = self._views.plain(acc.id, upto)
        refused = self._bound[plain.spec].checks.state_problem(
            plain, acc.template.blanks
        )
        acc.checked = (upto, refused)  # another check may overwrite it at once
        return refused

    def _accumulator_locked(self, accumulator_id: str) -> _Accumulator:
        """
        The open accumulator, if it takes readers. Raises ``KeyError`` for one
        never opened, and ``NotReadable`` for one released or opened before a
        restart, or that takes no readers.
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

    def _pushed_into_locked(self, accumulator_id: str) -> _Accumulator:
        """
        The accumulator for a push or a freeze. Raises as
        :meth:`_accumulator_locked` does, but ``SubmitError`` where a read
        raises ``NotReadable``: a push or a freeze is refused, not read.
        """
        try:
            return self._accumulator_locked(accumulator_id)
        except NotReadable as error:
            raise SubmitError.at((), str(error)) from None

    def _settled_locked(self, accumulator_id: str) -> _Accumulator:
        """
        The accumulator, for a push or a freeze, once no freeze of it is being
        checked. The lock is released while it waits.
        """
        while True:
            self._check_running()
            acc = self._pushed_into_locked(accumulator_id)
            if not acc.freezing:
                return acc
            self._changed.wait()

    def _logged_state_locked(self, accumulator_id: str) -> tuple[_Accumulator, int]:
        """
        The accumulator and its number of pushes logged, which name its logged
        state, the state a read is of. Raises ``NotReadable`` if the accumulator
        takes no readers (see :meth:`_accumulator_locked`).
        """
        self._check_running()
        acc = self._accumulator_locked(accumulator_id)
        return acc, len(self._views.pushes.get(accumulator_id, ()))

    @contextmanager
    def _reading(self, accumulator_id: str) -> Iterator[tuple[_Accumulator, int]]:
        """
        The accumulator and its number of pushes logged (see
        :meth:`_logged_state_locked`), the state held as a reader holds it for
        the block. Raises ``NotReadable`` if the state may not be read (see
        :meth:`_state_problem`).
        """
        with self._changed:
            acc, upto = self._logged_state_locked(accumulator_id)
            self._readers[_StateKey(acc, upto)] += 1
        try:  # a validator may raise anything
            if (refused := self._state_problem(acc, upto)) is not None:
                raise NotReadable(refused)
            yield acc, upto
        finally:
            with self._changed:
                self._let_go_locked([_StateKey(acc, upto)])

    def plain_request(self, accumulator_id: str) -> Request:
        with self._changed:
            acc, upto = self._logged_state_locked(accumulator_id)
        if (refused := self._state_problem(acc, upto)) is not None:
            raise NotReadable(refused)
        with self._changed:
            return self._views.plain(acc.id, upto)

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
        with self._reading(accumulator_id) as (acc, upto):
            if select is not None:
                check_select(self._bound[acc.template.spec].spec, names)
            with self._changed:
                self._wait_for_locked(
                    lambda: acc.added == upto or acc.stopped is not None
                )
                if acc.added != upto:
                    raise NotReadable(acc.stopped)
            declared = list(self._bound[acc.template.spec].spec.outputs.model_fields)
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
            return {n: selected(v, select) for n, v in values.items()}


def local(
    *,
    proposal: str,
    files: FileSource,
    bind: Sequence[Binding],
    submitter: str = 'user',
    store: Store | None = None,
    workers: int = 1,
) -> Client:
    """
    A client of its own workspace in this process, running the workflows bound here.

    ``bind`` lists a binding for each spec the workspace runs, such as
    ``[PipelineBinding(IOFQ, ...), DriverBinding(IOFQ_MULTI, ...)]``. Since the
    code runs in this process, a binding may also be of code defined in a
    notebook, such as ``function_binding(IOFQ, iofq)``.

    Closing the client closes the workspace (see :meth:`Workspace.close`): it
    releases everything the workspace keeps, which cancels the pending work that
    persists nothing, and waits until what is persisted is written. A client that
    is never closed ends with its process. Without a ``store``, persisting is
    refused. The workspace keeps history in memory, so its records end with the
    process. ``workers`` is the number of threads that run requests (see
    :class:`Workspace`).
    """
    workspace = Workspace(
        proposal=proposal, files=files, bind=bind, workers=workers, store=store
    )
    return Client(workspace, submitter=submitter, on_close=workspace.close)
