# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The workspace: it checks requests, logs records, runs them on worker threads,
and holds their output values in memory.

:mod:`ess.apps.protocol` states the rules that a client relies on. This
docstring states what spans the functions of this module.

Threads
-------
One lock, the condition ``_changed``, guards the workspace and its ``_Stage``
and ``_Accumulator`` objects. A method whose name ends in ``_locked`` must be
called with the lock held. Any other method takes the lock where it needs it.

Calls that may be slow run without the lock: the file source, the store,
bindings, drivers, and validation. So a method that checks a request does what
needs no history before it takes the lock, and the rest after.

Three exceptions to the lock:

- A stage's ``staging`` lock makes concurrent calls stage once. It is taken
  before ``_changed``, never while ``_changed`` is held.
- An accumulator's ``_computing`` lock guards the outputs of its state. It is
  never taken while ``_changed`` is held.
- While an accumulator is ``busy``, its worker uses the driver and the first
  step without ``_changed``.

History
-------
The log (``log.py``) is the workspace's only record of history. A change is
checked, then appended to the log as an event, then applied to ``_views``
(``_append_locked``). The log thus holds only events that apply. The views
apply the event in the form the log stores, as a later read of the log would.
If an append fails, for example on a full disk, the workspace no longer knows
its own state and stops: every waiting and later call raises ``Stopped``, from
the error of the append.

Output values, stages, accumulators, readers, and waits are not history. They
are held in memory and end with the process. The workspace logs only records of
a bound spec. So a record of an unbound spec comes from the log the workspace
started from, and none of its values is in memory.

Restart
-------
A record's start is logged before its workflow reads anything. Its persisted
outputs are written before its finish is logged. At a restart
(``_restart_locked``), only persisted work that had not started goes on. Each
record pending in the log:

1. fails if it started, since it may have ended the process, as an
   out-of-memory kill does;
2. otherwise is cancelled, unless it persists outputs, or a pending record
   that persists outputs reads it, directly or through other pending records;
3. otherwise runs, if its spec is bound and each output it reads is of a bound
   spec and either persisted or of a record that runs. Otherwise it fails.
   The record of a freeze fails, since its driver is gone. A call through a
   stage runs as its plain request, since the stage is gone.

So a record that runs at a restart had not started, and has written nothing.

Values and readers
------------------
The workspace holds in memory the outputs of the records in ``_in_memory``:
those that a submission or a freeze made without persisting outputs, until they
are released. Any other holder of a value is a *reader*, counted in
``_readers``. The readers of an output are the pending records, the steps of
accumulators, and the stages not yet staged that reference it. The readers of
an accumulator's state are the reads of it in progress, a freeze while it is
checked, and the record of a freeze until it has finished and its workflow, if
it ran, has returned.

When the last reader of an output lets go (``_let_go_locked``), the output is
dropped, unless it is in ``_in_memory``. A pending record that persists nothing,
and whose outputs nothing would hold, is cancelled, since its outputs would be
dropped once computed. Cancelling lets go of what the record reads, so this
passes along a chain.

A workflow reads an output from memory if the workspace holds it there, and
from the store otherwise. Persisted outputs are written once the workflow has
returned, and the record finishes after the write.

Waits
-----
A record waits for the records it references (``_Plan.depends_on``), and so for
the write of their persisted outputs. The record of a freeze waits for the
accumulator to reach its state. An accumulator does its steps one at a time, in
the order logged, on the workers (``_advance_locked``). A step waits for the
records it references, and a push also for the readers of the state before it.
If a step fails, or a record it references does not complete, the accumulator
stops.

Every wait is for work that came before the waiter, so no wait goes round in a
circle. A record waits for records logged before it. A read, or the record of a
freeze, waits for pushes logged before it took its state. A push waits for
readers that took the state before the push was logged.

Accumulator outputs
-------------------
The outputs of a state are computed once, without ``_changed``, when the first
reader needs them, and held until the next push. The record of a freeze gets
them without a copy, so they may share memory with the driver. A read copies
them. A driver cannot go back to an earlier state, so the next push waits until
the readers of the current state are done. This way no reader sees a value
change.

Stages
------
Once a stage has staged, a call through it reads only the values of the blanks.
The template's references in the call's record are then provenance only. They
are not checked, waited for, or read, except at a restart, which runs the record
as its plain request.
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
    """A failure in the call phase whose message the workspace writes."""


def _reason(error: Exception) -> str:
    """
    The failure text of an error: its message if it was written for a person,
    else its type and message, such as ``KeyError: 'counts'``.
    """
    if isinstance(error, _Failure | ParamsError | SubmitError | NotReadable):
        return str(error)
    text = str(error)
    return f'{type(error).__name__}: {text}' if text else type(error).__name__


def _outputs_of(
    spec_id: SpecId, model: type[BaseModel], returned: Any
) -> dict[str, Any]:
    """The outputs a binding returned, by name, without those returned as ``None``."""
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
    """``value``, or its element ``ref.key``, by index for a list."""
    if (key := ref.key) is None:
        return value
    try:
        return value[int(key)] if isinstance(value, list) else value[key]
    except (IndexError, KeyError):
        raise _Failure(f'{ref}: the output has no element {key!r}') from None


def _info(value: Any) -> Any:
    """``value`` as plain data, each array of at least one dimension as its info."""
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
    """An output of a record."""

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
    """A bound spec: its binding, request-phase checks, and digest."""

    binding: Binding
    checks: Checks
    digest: str  # held, since ``WorkflowSpec.digest`` computes it on each access

    @property
    def spec(self) -> Spec:
        return self.binding.spec


class _Stage:
    """An open stage: its binding, its template, and its callable once staged."""

    def __init__(self, binding: StageBinding, template: Template) -> None:
        self.spec = template.spec
        self.binding = binding
        self.template = template
        self.blanks = frozenset(template.blanks)
        # the stage reads these until it has staged, stopped, or is released
        self.reads = _output_reads(template.params)
        self.call: StageCall | None = None
        self.stopped: str | None = None  # why staging failed
        self.staging = threading.Lock()  # so that concurrent calls stage once

    def read_part_locked(self, request: Request) -> Request:
        """The part of a call's ``request`` that the call reads."""
        if self.call is None:
            return request
        params = {k: v for k, v in request.params.items() if k in self.blanks}
        return request.model_copy(update={'params': params})


class _DriverInput(NamedTuple):
    """
    A step of an accumulator: the template's fixed values for the opening, or
    the rows of each table for a push, and the outputs they reference. The
    values are as the log holds them, so that the driver and the record of a
    freeze agree.
    """

    values: dict[str, Any]
    reads: list[_OutputKey]


class _Accumulator:
    """An open accumulator: its template, binding, driver, and the steps to do."""

    driver: Driver  # once the accumulator has opened

    def __init__(
        self, accumulator_id: str, template: Template, binding: AccumulatorBinding
    ) -> None:
        self.id = accumulator_id
        self.template = template
        self.binding = binding
        self.added: int | None = None  # pushes in the driver; None before it is made
        self.steps: deque[_DriverInput] = deque()  # in the order logged
        # the rows of each blank table logged so far, by JSON form, with their index
        self.seen: dict[str, dict[bytes, int]] = {i: {} for i in template.blanks}
        self.busy = False  # a worker makes the driver or pushes into it
        # the pushes of the state checked last, and its problem
        self.checked: tuple[int, str | None] | None = None
        self.stopped: str | None = None  # why it takes no more pushes or readers
        self.released = False
        self.freezing = False  # a freeze is being checked
        self.frozen: str | None = None  # the record of its last freeze
        self._computing = threading.Lock()
        self._computed: dict[str, Any] | None = None

    @property
    def step(self) -> str:
        """The next step, as a failure names it."""
        return 'opening' if self.added is None else f'push {self.added}'

    def outputs(self) -> dict[str, Any]:
        """Every output of the state the driver is in, computed once per state."""
        with self._computing:
            if self._computed is None:
                spec = self.binding.spec
                returned = self.driver.outputs()
                self._computed = _outputs_of(self.template.spec, spec.outputs, returned)
            return self._computed

    def drop_locked(self) -> None:
        """Drop the outputs computed for the state the driver is in."""
        self._computed = None

    def end_locked(self) -> None:
        """Drop the driver and its outputs."""
        self._computed = None
        if hasattr(self, 'driver'):  # an accumulator that failed to open has none
            del self.driver


def _freeze_wait_key(accumulator_id: str, upto: int) -> str:
    """What the record of a freeze of the state after ``upto`` pushes waits for."""
    return f'{accumulator_id}[:{upto}]'


class _StateKey(NamedTuple):
    """A state of an accumulator, by its number of pushes."""

    acc: _Accumulator
    pushes: int


_Read = _OutputKey | _StateKey
"""A value that has readers (see the module docstring)."""


@dataclass
class _Plan:
    """How a record that has not finished runs."""

    reads: list[_Read]  # held by the plan until the record starts, then by _run
    depends_on: set[str]  # records, and states (see _freeze_wait_key)
    compute: Callable[[], dict[str, Any]]  # runs on a worker
    state: _StateKey | None = None  # the state the record of a freeze reads


class Workspace(WorkspaceProtocol):
    """
    The workspace of one proposal, in this process (see the module docstring).

    Parameters
    ----------
    bind:
        A binding for each spec the workspace runs. Each binding names its spec.
    workers:
        The number of threads that run workflows, accumulator steps, and writes.
        Submissions are accepted while every worker is busy. The default is one:
        a workflow that is parallel inside gains nothing from more, and the peak
        memory grows with each request run at once.
    log:
        The history, held in memory if not given. The workspace owns the log,
        and closes it in :meth:`close`.
    store:
        Where persisted outputs are written. A workspace started from an
        existing log needs the store of the workspace that wrote the log.
        Without it, the persisted work that goes on fails.

    Raises
    ------
    ValueError
        If two bindings have the same spec, or the log holds an event of
        another proposal. In the second case the log is closed.
    TypeError
        If ``bind`` is not a list of bindings.
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
        # output values held in memory, by record ID and name
        self._outputs: dict[tuple[str, str], Any] = {}
        # a value to the number of its readers
        self._readers: Counter[_Read] = Counter()
        self._plans: dict[str, _Plan] = {}  # of the records not yet finished
        # a record or a state (see _freeze_wait_key) to the records that wait for it
        self._dependents: dict[str, set[str]] = {}
        self._accumulating: set[_Accumulator] = set()  # with steps not yet done
        # values whose last reader let go, for _let_go_locked's loop to work through
        self._unread_values: deque[_Read] = deque()
        self._letting_go = False
        with self._changed:
            self._restart_locked()

    def close(self) -> None:
        """
        Release everything, wait until no record is pending and no accumulator
        has a step to do, then shut down the workers and close the log.

        Releasing cancels the pending work that persists nothing. So closing
        waits only for work that writes to the store, and for what that work
        reads. A call under way that does not wait returns or raises, depending
        on how far it has got. If an append fails while releasing, the
        workspace stops. A workspace that has stopped does not wait: it drops
        the work not yet started and raises ``Stopped``. Closing again does
        nothing more.
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
        """Finish or run the records left pending in the log."""
        views = self._views
        pending = [i for i in views.records if i not in views.finished]
        needed = {i for i in pending if i in views.persisted}
        for record_id in reversed(pending):  # a record follows what it reads
            # the record of a freeze fails, so it needs nothing
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
        Why a record pending at a restart cannot run, or ``None``. ``runs``
        holds the pending records that run.
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
        """Why a record is of an unbound spec, or ``None``."""
        spec_id = self._views.records.spec(record_id)
        if (bound := self._bound.get(spec_id)) is None:
            return f'{spec_id} has no binding'
        if bound.digest != self._views.digests[record_id]:
            return f"{spec_id} is bound with other models than the record's"
        return None

    def _known(self, spec_id: SpecId) -> _Bound:
        """The bound spec ``spec_id``; raises ``SubmitError`` if there is none."""
        try:
            return self._bound[spec_id]
        except KeyError:
            raise SubmitError.at((), f'unknown spec {spec_id}') from None

    def _append_locked(self, event: Event) -> None:
        """Append an event to the log and apply it to the views."""
        try:
            stored = self._log.append(event)
        except Exception as error:
            self._stopped = error
            self._changed.notify_all()
            raise
        self._views.apply(stored)
        self._changed.notify_all()

    def _check_stopped(self) -> None:
        """Raise ``Stopped`` if the workspace has stopped."""
        if self._stopped is not None:
            raise Stopped(
                f'the workspace stopped: appending to its log failed: {self._stopped}'
            ) from self._stopped

    def _check_running(self) -> None:
        """Raise ``Stopped`` if the workspace has stopped or is closed."""
        self._check_stopped()
        if self._closed:
            raise Stopped('the workspace is closed')

    def _wait_for_locked(self, predicate: Callable[[], bool]) -> None:
        """Wait until ``predicate`` holds; raise if the workspace stops or closes."""
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
        """Log a submission or a freeze, and hold the outputs of what persists none."""
        new_digests = self._views.new_digests(
            {s: self._bound[s].digest for s in spec_ids}
        )
        self._append_locked(
            Submitted(
                time=datetime.now(UTC),
                proposal=self._proposal,
                submitter=submitter,
                specs=new_digests,
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
        """The outputs of the spec that ``persist`` names."""
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
        self._check_running()
        return _each(items, self._resolved)

    def _resolved(self, index: int, item: Request | Template) -> Request | Template:
        """``item`` resolved; a refusal names it by ``index``."""
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
        The request as its record holds it, and the outputs it references,
        checked except for what needs history. A refusal names it by ``index``.
        """
        try:
            return self._known(request.spec).checks.request(request, self._proposal)
        except SubmitError as error:
            raise _refused(index, error) from None

    def _check_entry_locked(
        self, index: int, entry: Entry, request: Request, reads: OutputReads
    ) -> None:
        """Check the stage of a request, if any, and the outputs its call reads."""
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
        """Check that each output in ``reads`` may be read and fits its field."""
        for loc, ref, tp in reads:
            source = self._source_spec_locked(ref, loc, stage_reads)
            check_fit(self._bound[source].spec, ref, tp, loc)

    def _source_spec_locked(
        self, ref: OutputRef, loc: Loc, stage_reads: Collection[_OutputKey]
    ) -> SpecId:
        """The spec of the record whose output ``ref`` refers to, once checked."""
        # A reader of an accumulator's state would hold back its next push until
        # the reader's workflow returned, so a request reads a freeze instead.
        if ref.record in self._views.accumulators:
            frozen = [
                record_id
                for record_id, (acc_id, _) in self._views.states.items()
                if acc_id == ref.record
                and self._views.status(record_id) in (Status.PENDING, Status.COMPLETED)
            ]
            # at most one: a freeze is refused while one is pending or completed
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
        The answer does not depend on how far other work has run.
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
        """The plan of a record that reads the outputs ``request`` references."""
        refs = request.upstream()
        return _Plan(
            reads=[_OutputKey(ref.record, ref.output) for ref in refs],
            depends_on={ref.record for ref in refs} - self._views.finished.keys(),
            compute=compute,
        )

    def _schedule_locked(self, record_id: str, plan: _Plan) -> None:
        """Make the record a reader of what it reads, and start it or let it wait."""
        self._plans[record_id] = plan
        self._readers.update(plan.reads)
        if not plan.depends_on:
            self._executor.submit(self._run, record_id)
            return
        for key in plan.depends_on:
            self._dependents.setdefault(key, set()).add(record_id)

    def _run(self, record_id: str) -> None:
        """Run a record on a worker: log its start, compute, write, and finish it."""
        with self._changed:
            if record_id in self._views.finished:
                return
            self._append_locked(Started(record=record_id))
            plan = self._plans[record_id]
            # held here, even if the record is cancelled meanwhile, since the
            # workflow reads them
            reads, plan.reads = plan.reads, []
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
                    # the accumulator is read through this record from now on
                    acc.stopped = (
                        f'the accumulator is frozen: read record {record_id} instead'
                    )
                self._let_go_locked(reads)

    def _compute(self, request: Request) -> dict[str, Any]:
        """
        The outputs of a plain request. The values read go out of scope on
        return, so that nothing holds them once the caller lets go of them.
        """
        bound = self._bound[request.spec]
        # No validation here: _typed validated each value by its field, and the
        # params model has no validators of its own, which WorkflowSpec refuses.
        params = bound.spec.params.model_construct(
            **self._typed(request.spec, request.params)
        )
        returned = bound.binding.compute(params)
        return _outputs_of(request.spec, bound.spec.outputs, returned)

    def _typed(self, spec_id: SpecId, values: Mapping[str, Any]) -> dict[str, Any]:
        """
        ``values`` as the log holds them, each validated by its field, with each
        reference replaced by its data and each file by its local path.
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
        The local path of a file. A file the source no longer knows, moved or
        given a ``pid`` since the record was made, fails the record.
        """
        try:
            return self._files.path(ref)
        except KeyError:
            raise _Failure(f'the file source does not know the file {ref}') from None

    def _reader(self, params: Mapping[str, Any]) -> Callable[[OutputRef], Any]:
        """
        A function that returns the data a reference in ``params`` refers to.
        Outputs in memory are taken now. Others are read from the store once
        per reference, so that equal references return one object.
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
        """The output or element ``ref`` refers to, read from the store."""
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
        """Count one reader less of each value, and act on each left without."""
        for key in keys:
            self._readers[key] -= 1
            if not self._readers[key]:
                del self._readers[key]
                self._unread_values.append(key)
        # A loop, not recursion, since dropping may cancel a record, which lets
        # go of what it reads, along a chain of any length. A nested call only
        # appends to the queue.
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
        Drop the record's outputs that nothing holds, or cancel the record if
        it is pending, persists nothing, and nothing would hold its outputs.
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
        """Complete a record with its outputs, or fail it if their write failed."""
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
        Log the end of a record, and fail or start the records that wait for it.
        ``omitted`` names the outputs the workflow did not return.
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
                for key in plan.depends_on:
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
        depends_on = self._plans[record_id].depends_on
        depends_on.remove(key)  # raises for a record that no longer waits for it
        if not depends_on:
            self._executor.submit(self._run, record_id)

    # Releasing

    def release(self, ids: Sequence[str]) -> None:
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
        """The record; raises ``KeyError`` if it is unknown."""
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
        """The values of outputs, not copied; the caller applies ``select``."""
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
        """Whether a read of the outputs ``wanted`` stops waiting."""
        if record_id in self._views.finished:
            return True
        if record_id in self._in_memory:
            return False
        return not any(self._views.persists((record_id, n)) for n in wanted)

    def _read_names_locked(
        self, record: Record, names: Sequence[str] | None
    ) -> list[str]:
        """
        The outputs a read returns, once it has waited. Raises ``RecordFailed``
        for a record that failed or was cancelled, and ``NotReadable`` for an
        output that may not be read.
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
        """The stage a request goes through, if any, once checked."""
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
        """The plan of a record of a request through the stage."""
        request = self._views.records[record_id].request
        return self._plan_locked(
            stage.read_part_locked(request), lambda: self._call(stage, request)
        )

    def _call(self, stage: _Stage, request: Request) -> dict[str, Any]:
        """The outputs of a request through the stage."""
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
        """The stage's callable, made by ``make`` on the first call."""
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
        """Let go of the outputs the stage's template references."""
        reads, stage.reads = stage.reads, []
        self._let_go_locked(reads)

    def open_accumulator(self, template: Template) -> tuple[str, Template]:
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
        It reads only the state: the pushes have read what their rows reference.
        """
        state = _StateKey(acc, upto)
        return _Plan(
            reads=[state],
            depends_on={_freeze_wait_key(acc.id, upto)} if acc.added != upto else set(),
            compute=lambda: dict(acc.outputs()),
            state=state,
        )

    def _queue_locked(self, acc: _Accumulator, values: dict[str, Any]) -> None:
        """Queue a step, a reader of the outputs it references until it is done."""
        reads = _output_reads(values)
        self._readers.update(reads)
        acc.steps.append(_DriverInput(values, reads))
        self._accumulating.add(acc)
        self._advance_locked(acc)

    def _advance_locked(self, acc: _Accumulator) -> None:
        """Start the accumulator's next step on a worker once nothing holds it back."""
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
        Make the driver or push into it, on a worker. A failure stops the
        accumulator, since the driver may hold part of the rows.
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
            for record_id in self._dependents.pop(
                _freeze_wait_key(acc.id, acc.added), ()
            ):
                self._satisfied_locked(record_id, _freeze_wait_key(acc.id, acc.added))
            self._advance_locked(acc)

    def _stop_locked(self, acc: _Accumulator, failure: str) -> None:
        """Stop the accumulator, drop its steps, and fail the freezes that wait."""
        acc.stopped = f'the accumulator stopped: {failure}'
        acc.busy = False
        self._accumulating.discard(acc)
        steps = list(acc.steps)
        acc.steps.clear()
        for step in steps:
            self._let_go_locked(step.reads)
        for upto in range(len(self._views.pushes.get(acc.id, ())) + 1):
            for record_id in self._dependents.pop(_freeze_wait_key(acc.id, upto), ()):
                self._finish_locked(record_id, Status.FAILED, acc.stopped)
        if not self._readers[_StateKey(acc, acc.added)]:
            acc.end_locked()
        self._changed.notify_all()

    def _unneeded_locked(self, acc: _Accumulator) -> None:
        """
        Drop the steps of a released accumulator that no reader needs. A reader
        needs the steps up to its state, and the step a worker does stays. No
        record of a freeze waits for a dropped step, since a pending one is a
        reader.
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
        Why the plain request of the state after ``upto`` pushes would be
        refused, or ``None``.
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
        The open accumulator, unless it stopped. Raises ``KeyError`` for one
        never opened, and ``NotReadable`` for one not open or stopped.
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
        The accumulator for a push or a freeze, as :meth:`_accumulator_locked`
        returns it, raising ``SubmitError`` in place of ``NotReadable``.
        """
        try:
            return self._accumulator_locked(accumulator_id)
        except NotReadable as error:
            raise SubmitError.at((), str(error)) from None

    def _settled_locked(self, accumulator_id: str) -> _Accumulator:
        """The accumulator for a push or a freeze, once no freeze is being checked."""
        while True:
            self._check_running()
            acc = self._pushed_into_locked(accumulator_id)
            if not acc.freezing:
                return acc
            self._changed.wait()

    def _logged_state_locked(self, accumulator_id: str) -> tuple[_Accumulator, int]:
        """The accumulator and its number of pushes logged, its logged state."""
        self._check_running()
        acc = self._accumulator_locked(accumulator_id)
        return acc, len(self._views.pushes.get(accumulator_id, ()))

    @contextmanager
    def _reading(self, accumulator_id: str) -> Iterator[tuple[_Accumulator, int]]:
        """
        The accumulator and its logged state, held as a reader for the block.
        Raises ``NotReadable`` if the state's plain request would be refused.
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
    A client of a new workspace in this process, which runs the bindings given.

    The workspace holds history in memory, so its records end with the process.
    Closing the client closes the workspace (see :meth:`Workspace.close`), which
    waits until what is persisted is written.

    Parameters
    ----------
    bind:
        A binding for each spec the workspace runs, such as
        ``[PipelineBinding(IOFQ, ...), DriverBinding(IOFQ_MULTI, ...)]``. Since
        the code runs in this process, a binding may be of code defined in a
        notebook, such as ``function_binding(IOFQ, iofq)``.
    store:
        Where persisted outputs are written. Without one, a call with
        ``persist`` raises ``SubmitError``.
    workers:
        See :class:`Workspace`.
    """
    workspace = Workspace(
        proposal=proposal, files=files, bind=bind, workers=workers, store=store
    )
    return Client(workspace, submitter=submitter, on_close=workspace.close)
