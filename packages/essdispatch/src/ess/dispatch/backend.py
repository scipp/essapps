# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The backend checks requests, keeps records, and runs requests. It also
answers its clients' queries about the datasets of its dataset source.

What the backend knows of history is its log (see ``log.py``): every
submission, finished record, opened accumulator, and push is appended as one
event and then applied to its views (see ``views.py``). A change is checked
before its event is appended, so the log holds only events that apply. A
backend given a log that already has events applies them first. It has no
clients, so nothing keeps the records left pending, and they are cancelled.

A client is one entry in the backend, from :meth:`Backend.open_client` to
:meth:`Backend.close_client`: its proposal, the records whose outputs it keeps,
and its stages and accumulators. Every call names its client, and a call of a
client that has ended raises :class:`ClientEnded`. Clients, output values, and
what waits for what are not history. This backend keeps them in memory, so a
backend started from an existing log has no clients and cannot read the
outputs of the backend that wrote it.

An output value is kept while the client that made its record keeps it, while
a stage whose template references it has yet to stage, or while a pending
request or a step of an accumulator that reads it has yet to run. Once a stage
has staged, or a step is done, what the binding holds is its own. A pending
record whose outputs nothing keeps is cancelled, since they would be dropped
the moment they are computed; cancelling lets go of what it reads, so this
passes along a chain. A workflow already running is not interrupted: its
record is cancelled at once, and its outputs are dropped when it returns.

A client reads, and its requests, stages, and accumulators reference, only the
outputs of records it keeps, whether they are pending or completed; a request
through a stage that has yet to stage also those the stage keeps, and one
through a stage that has staged reads only the values of its blanks. So whether
a call is accepted does not depend on how far other work has run, and a value
kept only for another client or for a pending request is refused.

A request waits until every record it references has completed, the record
of an accumulator's state until the accumulator has reached that state, and
an accumulator's steps as described below. This is the only scheduling there
is.

A request runs through its spec's binding. A stage checks its template's
values as a request's and resolves dataset names when it is made, and keeps
the outputs they reference until it has staged. A request through it must
have its values outside the blanks. The first such request to run stages the
binding with the template's values, and every request through the stage runs
through the callable the binding returned, so a binding that holds values
computes only what depends on the blanks. If staging fails, the stage stops:
the requests through it fail, and later ones are refused. A stage lives until
its client releases it or ends, and the requests made through it have run.

An accumulator opens from a template whose blanks are table fields, checked
as a stage's template is. Each other value is typed by its own field. Each
push names one row for each of one or more tables; a row is checked by its
table's row model alone. Opening and pushing return once these checks pass
and the event is logged, as a submission does: they check the records they
reference against what the backend knows now, and refuse one that has
failed, but do not wait for one that is pending.

Each accumulator then works through its steps one at a time, in the order
they were logged, on the workers: opening makes the held state from the
template's values, by the binding or, for a binding that makes none, by
keeping the rows (see ``bindings.py``), and each push adds its rows to it, in
place if the binding does so. A step starts once the records it references
have completed, and a push once the readers of the state before it are done.
If a step fails, or a record it references has not completed, the
accumulator stops: it takes no more pushes or readers, the steps after it
are dropped, and the requests that wait for a state it will not reach fail.

A read pins the accumulator's state after the pushes logged so far: a request
that references the accumulator, when it is submitted; a read of its outputs;
and its provenance. Pinning never waits for a push to be added. A request
references an accumulator's output by a placeholder (``AccumulatorRef``). The
submission makes one record of the plain request of the pinned state for each
accumulator its requests reference, logs it before them, and the records of
the requests reference that record's outputs. No client keeps the record of a
state, so only the requests of that submission read it. A request reads at
most one accumulator, and a row or a template reads none. A state may be
read only if its plain request would be accepted. The first read of a state
validates that request, outside the backend's lock, and a read of a state
whose request would be refused is refused with that request's reason. So
rules on a whole table, such as its length, and the params model's own
validators apply at reads, not at pushes. Validators that read more than one
value, such as the params model's own or those of a table field, must not
change a value: the held state got the values typed one by one, and nothing
checks that the plain request holds the same.

The outputs of a state are computed outside the backend's lock, all at once,
the first time a record of the state or a read of its outputs reads it, and
kept until the next push. The record of a state is computed from the held
state, as a call through a stage is from what the stage holds: its outputs
are what the binding returns, not a copy, and may share memory with the held
state, which the next push may change in place. They are kept until the
requests that read them have run, and the record holds the state as its
reader until then. Reading the outputs of the accumulator copies them. So the
next push waits until the readers of the state before it are done, since the
held state cannot go back to that state and no reader may see its outputs
change: the records of that state, until the requests that read them have
run, even those cancelled while they run, and the reads of its outputs in
progress. Every wait is for work logged before the waiter: a request waits
for records submitted before it and for pushes logged before its submission,
and a push waits for the records of the state before it and the requests of
their submissions, all logged before the push, since only the submission that
made a record of a state reads it.
So no wait goes round in a circle.
Releasing the accumulator or ending its client keeps the pushes up to the last
state a reader still holds, which are still added, and drops the later ones,
again each time a reader lets go; the held state is dropped once its readers
are done.
"""

from __future__ import annotations

import copy
import inspect
import threading
import uuid
from collections import Counter, deque
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ValidationError, create_model, field_validator
from pydantic_core import (
    PydanticSerializationError,
    from_json,
    to_json,
)

from ess.spec import (
    AccumulatorRef,
    Binding,
    DataField,
    DatasetRef,
    Function,
    HeldState,
    OutputRef,
    Ref,
    WorkflowSpec,
    data_fields,
    table_fields,
    walk_refs,
)

from .bindings import as_binding, open_held_state
from .datasets import DatasetSource, Selector, readable
from .log import Event, Finished, Log, NewRecord, Opened, Pushed, Submitted
from .records import (
    Record,
    Request,
    Row,
    SpecId,
    Status,
    SubmitError,
    Template,
    as_refs,
    map_refs,
)
from .views import Views


class ClientEnded(RuntimeError):
    """A call of a client that has ended."""


def _agree(output: DataField, param: DataField) -> bool:
    """Whether an output field fulfils a params field."""
    if output.format != param.format:
        return False
    return output.array is None or param.array is None or output.array == param.array


def _values(model: BaseModel) -> dict[str, Any]:
    """The values of a params model by field, the rows of a table as dicts."""
    tables = table_fields(type(model))
    values = {f: getattr(model, f) for f in type(model).model_fields}
    return {
        f: [dict(row) for row in v] if f in tables and v is not None else v
        for f, v in values.items()
    }


def _required(outputs: type[BaseModel]) -> frozenset[str]:
    """The names of the outputs a workflow must return."""
    return frozenset(n for n, f in outputs.model_fields.items() if f.is_required())


def _check_storable(field: str, value: Any) -> Any:
    """
    Refuse a value that cannot be stored in the log, which holds JSON; return
    it as the log holds it, read back.
    """
    try:
        return as_refs(from_json(to_json(value)))
    except PydanticSerializationError as error:
        raise SubmitError(f'{field}: cannot be stored as JSON: {error}') from None


def _problems(
    error: ValidationError, blanks: Sequence[str] = (), at: tuple[str | int, ...] = ()
) -> list[str]:
    """
    What a validation error finds wrong, but for blanks that are missing; the
    places are prefixed by ``at``.
    """
    return [
        f"{'.'.join(map(str, (*at, *e['loc']))) or 'params'}: {e['msg']}"
        for e in error.errors()
        if not (e['type'] == 'missing' and len(e['loc']) == 1 and e['loc'][0] in blanks)
    ]


def _filled(template: Template, pushes: Sequence[Mapping[str, Row]]) -> Request:
    """The template with each table filled by the rows pushed into it, in push order."""
    return template.fill(
        {t: [rows[t] for rows in pushes if t in rows] for t in template.blanks}
    )


def _field_validators(
    model: type[BaseModel], fields: Collection[str]
) -> dict[str, Any]:
    """
    The field validators of ``model`` that validate any of ``fields``, for a
    model of these fields alone; ``create_model`` takes them as validators.
    """
    validators = {}
    for name, decorator in model.__pydantic_decorators__.field_validators.items():
        info = decorator.info
        names = ['*'] if '*' in info.fields else [f for f in info.fields if f in fields]
        if names:
            func = decorator.func  # bound to ``model`` if a classmethod
            unbound = classmethod(func.__func__) if inspect.ismethod(func) else func
            validators[name] = field_validator(*names, mode=info.mode)(unbound)
    return validators


_OMITTED = object()
"""The value of an optional output that a workflow or a held state left out."""


def _returned(value: Any, where: str) -> Any:
    """``value``, unless it stands for an output that was left out."""
    if value is _OMITTED:
        raise LookupError(f'{where}: the workflow did not return it')
    return value


def _check_cells(params: type[BaseModel], values: Mapping[str, Any]) -> None:
    """
    Refuse a row of a table with a field its row model lacks.

    Validating the row would drop such a field silently; it is refused
    instead, as a parameter the spec lacks is.
    """
    for field, row in table_fields(params).items():
        rows = values.get(field)
        for i, cells in enumerate(rows if isinstance(rows, list) else []):
            if isinstance(cells, dict) and (
                extra := set(cells) - set(row.model_fields)
            ):
                raise SubmitError(
                    f'{field}[{i}]: {sorted(extra)}: not fields of {row.__name__}'
                )


def _reading(value: Any) -> list[OutputRef | AccumulatorRef]:
    """The references in ``value`` that read a value: of a record or an accumulator."""
    return [ref for _, ref in walk_refs(value) if not isinstance(ref, DatasetRef)]


def _output_reads(values: Any) -> list[tuple[str, str]]:
    """The outputs of records that ``values`` reference, by record ID and name."""
    return [
        (ref.record, ref.output)
        for _, ref in walk_refs(values)
        if isinstance(ref, OutputRef)
    ]


def _reads(
    params: type[BaseModel], field: str, value: Any
) -> Iterator[tuple[str, OutputRef | AccumulatorRef, DataField | None]]:
    """Each value a param's value reads, with its place and the field it fills."""
    row = table_fields(params).get(field)
    if row is None:
        target = data_fields(params).get(field)
        yield from ((field, ref, target) for ref in _reading(value))
        return
    cells = data_fields(row)
    for i, cell_values in enumerate(value):
        for cell, cell_value in cell_values.items():
            for ref in _reading(cell_value):
                yield f'{field}[{i}].{cell}', ref, cells.get(cell)


@dataclass(frozen=True)
class Entry:
    """
    A request of a submission, and its label and member.

    ``name`` says where the request came from in the call, a key or an index,
    and prefixes the reasons it is refused. ``stage`` names the stage the
    request goes through, if any.
    """

    request: Request
    label: str | None = None
    member: str | None = None
    name: str | None = None
    stage: str | None = None

    def refused(self, error: SubmitError) -> SubmitError:
        return SubmitError(f'{self.name}: {error}' if self.name else str(error))


class _Stage:
    """
    A stage: its spec, its blanks, its fixed values, and the binding's callable
    once it has staged.

    The fixed values are the template's, with dataset names resolved when the
    stage was made. ``reads`` are the outputs of records they reference, which
    the stage keeps as a reader until it has staged, has stopped, or is
    released; it is empty from then on. The first call to run stages the
    binding under ``staging``, so that concurrent calls stage it once.
    ``call``, the callable the binding returned, and ``stopped``, why staging
    failed, change under the backend's lock. A stage that has stopped takes
    no more calls.
    """

    def __init__(
        self, spec: SpecId, blanks: tuple[str, ...], fixed: dict[str, Any]
    ) -> None:
        self.spec = spec
        self.blanks = blanks
        self.fixed = fixed
        self.reads = _output_reads(fixed)
        self.call: Function | None = None
        self.stopped: str | None = None
        self.staging = threading.Lock()


class _Accumulator:
    """
    An accumulator: its template, the binding's held state once it has
    opened, and the steps logged but not yet done.

    ``added`` is the number of pushes in the held state, ``None`` until the
    held state has opened. ``steps`` holds the values of each step not yet
    done, in the order logged: first the template's values, until the held
    state has opened, then the rows of each push. Each comes with the outputs
    of records it reads, which it holds as a reader. ``busy`` says a worker
    opens the held state or adds to it, outside the backend's lock.

    ``checked`` holds the number of pushes of the last state whose plain
    request was validated, and that request as a record holds it, or why it
    would be refused. Two reads that validate different states at once may
    overwrite each other's, which costs only another validation.
    ``stopped`` says why the accumulator takes no more pushes or readers, or
    is ``None``. ``released`` says its client released it or ended: it then
    keeps only the steps that a reader still needs.

    :meth:`outputs` computes every output of the current state the first time
    a reader reads it, and keeps them until :meth:`drop`, which the backend
    calls before a push adds. They are computed under a lock of their own,
    outside the backend's, so ``state.outputs`` is called from one thread at a
    time and never while a push adds.
    """

    state: HeldState  # once the held state has opened

    def __init__(
        self, accumulator_id: str, template: Template, outputs: type[BaseModel]
    ) -> None:
        self.id = accumulator_id
        self.template = template
        self.added: int | None = None
        self.steps: deque[tuple[dict[str, Any], list[tuple[str, str]]]] = deque()
        self.busy = False
        self.checked: tuple[int, Request | str] | None = None
        self.stopped: str | None = None
        self.released = False
        self._declared = tuple(outputs.model_fields)
        self._required = _required(outputs)
        self._computing = threading.Lock()
        self._computed: dict[str, Any] | None = None

    @property
    def step(self) -> str:
        """The next step, as a failure names it."""
        return 'opening' if self.added is None else f'push {self.added}'

    def outputs(self) -> dict[str, Any]:
        """
        Every output of the current state, for one of its readers; needs no
        lock. An optional output that the held state leaves out is
        ``_OMITTED``.

        They are what the held state returns, not a copy (see the module
        docstring), computed once for the state.
        """
        with self._computing:
            if self._computed is None:
                outputs = self.state.outputs()
                if lacking := self._required - outputs.keys():
                    raise ValueError(
                        f'the held state of {self.template.spec} returned '
                        f'{sorted(outputs)}: missing {sorted(lacking)}'
                    )
                self._computed = {**dict.fromkeys(self._declared, _OMITTED), **outputs}
            return self._computed

    def drop(self) -> None:
        """
        Drop the outputs computed for the current state; the backend's lock
        held, and no reader left, so none computes.
        """
        self._computed = None


def _state_key(accumulator_id: str, upto: int) -> str:
    """What the record of the state after ``upto`` pushes waits for."""
    return f'{accumulator_id}[:{upto}]'


_Pinned = tuple[_Accumulator, int, Request | str]
"""
A state a submission pinned: the accumulator, its number of pushes, and the
plain request of that state, or why that request would be refused.
"""

_Read = tuple[str, str] | tuple[_Accumulator, int]
"""
A value that is read: an output by record ID and name, or an accumulator's
state by its number of pushes. The readers of a state are the records of the
state and reads of the accumulator's outputs in progress.
"""


class _Client:
    """
    A client of the backend, until it ends.

    ``kept`` holds the IDs of the records whose outputs the client keeps: the
    records it made and has not released.
    """

    def __init__(self, proposal: str, submitter: str) -> None:
        self.proposal = proposal
        self.submitter = submitter
        self.kept: set[str] = set()
        self.stages: dict[str, _Stage] = {}
        self.accumulators: dict[str, _Accumulator] = {}


class Backend:
    def __init__(
        self,
        datasets: DatasetSource,
        bind: Mapping[WorkflowSpec, Binding | Function],
        *,
        workers: int = 4,
        log: Log | None = None,
    ) -> None:
        self._datasets = datasets
        self._specs: dict[SpecId, WorkflowSpec] = {}
        for spec in bind:
            if (spec_id := SpecId.of(spec)) in self._specs:
                raise ValueError(f'two specs are bound as {spec_id}')
            self._specs[spec_id] = spec
        self._bindings = {SpecId.of(spec): as_binding(b) for spec, b in bind.items()}
        self._output_fields = {
            spec_id: data_fields(spec.outputs) for spec_id, spec in self._specs.items()
        }
        self._executor = ThreadPoolExecutor(max_workers=workers)
        self._changed = threading.Condition()
        self._log = Log() if log is None else log
        self._views = Views()
        # Not history
        self._clients: dict[str, _Client] = {}  # by client ID, until it ends
        # by record ID and name; _OMITTED for an optional output left out
        self._outputs: dict[tuple[str, str], Any] = {}
        # a value, as (record ID, name) or (accumulator, number of pushes), to
        # the number of pending records, steps, and reads that have yet to read
        # it, and of records of states that hold it
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
        # does not complete, and in _drop once its outputs are dropped
        self._state_of: dict[str, tuple[_Accumulator, int]] = {}
        # values whose last reader let go, for _let_go's loop to work through
        self._unread_values: deque[_Read] = deque()
        self._letting_go = False
        self._on_finished: dict[str, list[Callable[[Record], None]]] = {}
        with self._changed:
            for event in self._log:
                self._views.apply(event)
            for record_id in list(self._views.records):
                if record_id not in self._views.finished:
                    self._finish(record_id, Status.CANCELLED, 'the backend restarted')

    def close(self) -> None:
        """
        Wait until no record is pending and no accumulator has a step to do,
        then let go of the workers and the log.

        Closing the backend cancels nothing itself: what its clients still
        keep runs to its end. Ending the clients first (see
        :meth:`close_client`) cancels the work that nothing else keeps.
        """
        with self._changed:
            self._changed.wait_for(
                lambda: (
                    self._views.records.keys() <= self._views.finished.keys()
                    and not self._accumulating
                )
            )
        self._executor.shutdown(wait=True)
        self._log.close()

    def spec(self, spec_id: SpecId) -> WorkflowSpec:
        try:
            return self._specs[spec_id]
        except KeyError:
            raise SubmitError(f'unknown spec {spec_id}') from None

    def _append(self, event: Event) -> None:
        """
        Append an event and apply it to the views; lock held.

        The caller has checked that the event applies. What is applied is the
        event as the log holds it, as it would be when the log is read again.
        """
        self._views.apply(self._log.append(event))

    # Submission

    def submit(self, entries: list[Entry], *, client: str) -> list[Record]:
        """
        Check every request, then create a record for each.

        If one request is refused, none is submitted. Dataset names are
        resolved before the backend's lock is taken. The state of each of the
        client's accumulators that the requests reference is pinned to the
        pushes logged so far, and its plain request is checked outside the
        lock (see :meth:`_check_state`). The records are pending, and the
        client keeps their outputs, but not those of the records of states.
        """
        proposal = self._client(client).proposal
        requests = [self._prepare(e, proposal) for e in entries]
        referenced = {ref.accumulator for r in requests for ref in r.accumulators()}
        with self._changed:
            pinned = self._pin_states(self._client(client), referenced)
        try:
            states = {
                i: (acc, upto, self._check_state(acc, upto))
                for i, (acc, upto) in pinned.items()
            }
            with self._changed:
                caller = self._client(client)
                for entry, request in zip(entries, requests, strict=True):
                    self._check_stage(entry, request, caller)
                    self._check_reads(entry, request, caller, states)
                return self._create(entries, requests, caller, states)
        finally:
            with self._changed:
                self._let_go(pinned.values())

    def _create(
        self,
        entries: list[Entry],
        requests: list[Request],
        caller: _Client,
        states: Mapping[str, _Pinned],
    ) -> list[Record]:
        """
        Log and schedule the checked requests as the client's records; lock held.

        The record of each state in ``states`` is logged and scheduled first,
        and the references to its accumulator name that record's outputs.
        """
        state_ids = {i: uuid.uuid4().hex for i in states}
        ids = [uuid.uuid4().hex for _ in entries]

        def new(record_id: str, request: Request, **given: str | None) -> NewRecord:
            outputs = tuple(self._specs[request.spec].outputs.model_fields)
            return NewRecord(id=record_id, request=request, outputs=outputs, **given)

        def state_record(ref: Ref) -> Ref:
            if isinstance(ref, AccumulatorRef):
                return OutputRef(record=state_ids[ref.accumulator], output=ref.output)
            return ref

        requests = [Request(r.spec, map_refs(r.params, state_record)) for r in requests]
        self._append(
            Submitted(
                time=datetime.now(UTC),
                proposal=caller.proposal,
                submitter=caller.submitter,
                records=(
                    *(new(state_ids[i], plain) for i, (_, _, plain) in states.items()),
                    *(
                        new(record_id, request, label=e.label, member=e.member)
                        for record_id, request, e in zip(
                            ids, requests, entries, strict=True
                        )
                    ),
                ),
            )
        )
        caller.kept.update(ids)
        for i, (acc, upto, _) in states.items():
            self._state_of[state_ids[i]] = (acc, upto)
            self._schedule(state_ids[i])
        for record_id, entry in zip(ids, entries, strict=True):
            if entry.stage is not None:
                self._stage_of[record_id] = caller.stages[entry.stage]
            self._schedule(record_id)
        return [self._views.records[i] for i in ids]

    def _prepare(self, entry: Entry, proposal: str) -> Request:
        """
        The request with names resolved and defaults filled; needs no lock.

        A request reads at most one accumulator: it reads a held state in
        place, so it runs where that held state is (ADR 0003).
        """
        request = entry.request
        try:
            params = self._resolve(request.spec, request.params, (), proposal)
            values = self._typed(request.spec, params)
            for field, value in values.items():
                _check_storable(field, value)
            prepared = Request(request.spec, values)
            if len(read := {ref.accumulator for ref in prepared.accumulators()}) > 1:
                raise SubmitError(
                    f'{request.spec}: a request reads at most one accumulator, '
                    f'not {len(read)}'
                )
        except SubmitError as error:
            raise entry.refused(error) from None
        return prepared

    def _resolve(
        self,
        spec_id: SpecId,
        params: Mapping[str, Any],
        blanks: tuple[str, ...],
        proposal: str,
    ) -> dict[str, Any]:
        """
        ``params`` with dataset names resolved; needs no lock.

        Refuses a param or blank the spec lacks, and a row of a table with a
        field its row model lacks.
        """
        spec = self.spec(spec_id)
        unknown = (set(params) | set(blanks)) - set(spec.params.model_fields)
        if unknown:
            raise SubmitError(f'{sorted(unknown)}: not parameters of {spec_id}')
        _check_cells(spec.params, params)
        return {
            field: map_refs(value, self._resolver(field, proposal))
            for field, value in params.items()
        }

    def _resolver(self, field: str, proposal: str) -> Callable[[Ref], Ref]:
        def resolve(ref: Ref) -> Ref:
            if isinstance(ref, DatasetRef):
                try:
                    identity = self._datasets.resolve(ref)
                except KeyError:
                    raise SubmitError(f'{field}: unknown dataset {ref}') from None
                metadata = self._datasets.metadata(identity)
                if not readable(metadata, proposal):
                    raise SubmitError(
                        f'{field}: dataset {ref} belongs to proposal '
                        f'{metadata["proposal"]}'
                    )
                return identity
            if isinstance(ref, OutputRef) and ref.key is not None:
                raise SubmitError(f'{field}: {ref} names an element of an output')
            return ref

        return resolve

    def _pin_states(
        self, caller: _Client, ids: Iterable[str]
    ) -> dict[str, tuple[_Accumulator, int]]:
        """
        The current state of each of the client's accumulators among ``ids``,
        by ID, as the accumulator and its number of pushes logged, each held
        as a reader holds it until the caller lets go of it; lock held.

        An accumulator that is released, unknown, or stopped is left out.
        """
        pinned = {}
        for i in ids:
            acc = caller.accumulators.get(i)
            if acc is not None and acc.stopped is None:
                pinned[i] = (acc, len(self._views.pushes.get(i, ())))
        self._readers.update(pinned.values())
        return pinned

    def _check_reads(
        self,
        entry: Entry,
        request: Request,
        caller: _Client,
        states: Mapping[str, _Pinned],
    ) -> None:
        """
        Check the outputs of records and accumulators a request reads; lock held.

        Only a submission reads accumulators, from the ``states`` it pinned;
        it reads the records of these states, which it makes in
        :meth:`_create`, after these checks. A stage, an accumulator, or a
        push passes no states: a stage or an accumulator that held a state of
        an accumulator would hold back its every push for as long as it
        lives, and a row pushed into one would read another's value while it
        adds. A request through a stage that has yet to stage also reads
        what the stage keeps, which :meth:`_check_stage` has checked it for;
        one through a stage that has staged reads only its blanks.
        """
        params = self._specs[request.spec].params
        stage = caller.stages[entry.stage] if entry.stage is not None else None
        also = set(stage.reads) if stage is not None else set()
        try:
            for field, value in request.params.items():
                if stage is not None and stage.call is not None:
                    if field not in stage.blanks:
                        continue
                for where, ref, target in _reads(params, field, value):
                    spec_id = self._readable(ref, where, caller, states, also)
                    outputs = self._output_fields[spec_id]
                    if ref.output not in outputs:
                        raise SubmitError(
                            f'{where}: {spec_id} has no output {ref.output!r}'
                        )
                    if target is not None and not _agree(outputs[ref.output], target):
                        raise SubmitError(f'{where}: {ref} does not fit the field')
                    if (
                        isinstance(ref, OutputRef)
                        and self._views.status(ref.record) is Status.COMPLETED
                    ):
                        try:
                            self._value(ref)
                        except LookupError as error:
                            raise SubmitError(f'{where}: {error}') from None
        except SubmitError as error:
            raise entry.refused(error) from None

    def _check_stage(self, entry: Entry, request: Request, caller: _Client) -> None:
        """
        Check the stage a request goes through; lock held.

        The request must have the stage's values outside the blanks, as given
        or resolved when the stage was made.
        """
        if entry.stage is None:
            return
        stage = caller.stages.get(entry.stage)
        if stage is None:
            raise entry.refused(SubmitError('the stage was released or is unknown'))
        if stage.stopped is not None:
            raise entry.refused(SubmitError(stage.stopped))
        if stage.spec != request.spec:
            raise entry.refused(
                SubmitError(f'the stage holds {stage.spec}, not {request.spec}')
            )
        given = {
            k: as_refs(v)
            for k, v in entry.request.params.items()
            if k not in stage.blanks
        }
        differ = (given.keys() ^ stage.fixed.keys()) | {
            k for k in given.keys() & stage.fixed.keys() if given[k] != stage.fixed[k]
        }
        if differ:
            raise entry.refused(
                SubmitError(f"{sorted(differ)}: differ from the stage's values")
            )

    def _readable(
        self,
        ref: OutputRef | AccumulatorRef,
        field: str,
        caller: _Client,
        states: Mapping[str, _Pinned],
        also: Collection[tuple[str, str]],
    ) -> SpecId:
        """
        The spec of a record or accumulator a request may read; lock held.

        An accumulator must be the client's own, not stopped, and its state
        pinned in ``states`` readable. A record must be one whose outputs the
        client keeps, pending or completed, or its output must be among
        ``also``, which the request's stage keeps: whether a request is
        accepted does not depend on how far its inputs have run. So no client
        reads the record of a state, which only the submission that made it
        keeps (see the module docstring).
        """
        if isinstance(ref, AccumulatorRef):
            try:
                acc = self._accumulator(caller, ref.accumulator)
            except SubmitError as error:
                raise SubmitError(f'{field}: {error}') from None
            if ref.accumulator not in states:
                raise SubmitError(
                    f'{field}: only a request may reference an accumulator'
                )
            _, _, plain = states[ref.accumulator]
            if isinstance(plain, str):
                raise SubmitError(f'{field}: {plain}')
            return acc.template.spec
        record = self._views.records.get(ref.record)
        if record is None:
            raise SubmitError(f'{field}: unknown record {ref.record}')
        if record.proposal != caller.proposal:
            raise SubmitError(
                f'{field}: record {ref.record} belongs to proposal {record.proposal}'
            )
        status = self._views.status(ref.record)
        if status in (Status.FAILED, Status.CANCELLED):
            raise SubmitError(f'{field}: record {ref.record} {status}')
        if ref.record not in caller.kept and (ref.record, ref.output) not in also:
            raise SubmitError(
                f'{field}: record {ref.record} is not kept by this client'
            )
        return record.spec

    # Execution

    def _schedule(self, record_id: str) -> None:
        """
        Start the record, or let it wait for its unfinished inputs; lock held.

        A record of a state reads only the state, which it waits for; the
        pushes have read the outputs its rows reference. A record through a
        stage that has staged reads only the outputs its blanks reference.
        """
        state = self._state_of.get(record_id)
        if state is None:
            request = self._views.records[record_id].request
            stage = self._stage_of.get(record_id)
            if stage is not None and stage.call is not None:
                blanks = {k: v for k, v in request.params.items() if k in stage.blanks}
                request = Request(request.spec, blanks)
            refs = request.inputs()
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
        Run a record's workflow, holding the values it reads until it returns.

        It holds them even once the record is cancelled, since the workflow
        still reads them. A record of a state that completes holds the state
        until its outputs are dropped (see :meth:`_drop`).
        """
        with self._changed:
            if record_id in self._views.finished:
                return
            record = self._views.records[record_id]
            stage = self._stage_of.get(record_id)
            state = self._state_of.get(record_id)
            reads = self._unread.pop(record_id)
        try:
            if state is None:
                call, blanks = self._call(record.request, stage)
                outputs = dict(call(**blanks))
            else:
                outputs = state[0].outputs()
        except Exception as error:  # any failure of a workflow is recorded
            with self._changed:
                self._finish(record_id, Status.FAILED, str(error) or repr(error))
                self._let_go(reads)
            return
        with self._changed:
            self._complete(record_id, outputs)
            # A record of a state that completed lets go of the state in
            # _drop, once its outputs are dropped; any other record here.
            if state is None or self._views.status(record_id) is not Status.COMPLETED:
                self._let_go(reads)

    def _call(
        self, request: Request, stage: _Stage | None
    ) -> tuple[Function, dict[str, Any]]:
        """
        The callable that computes a request, through its stage if it has one,
        and the values of the blanks to call it with; the inputs are read.
        """
        binding = self._bindings[request.spec]
        values = self._typed(request.spec, request.params)
        if stage is None:
            return binding.stage(self._read(values), ()), {}
        fixed = {k: v for k, v in values.items() if k not in stage.blanks}
        call = self._staged(
            stage, lambda: binding.stage(self._read(fixed), stage.blanks)
        )
        return call, self._read({k: values[k] for k in stage.blanks})

    def _staged(self, stage: _Stage, make: Callable[[], Function]) -> Function:
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
                    raise RuntimeError(stage.stopped)
                if stage.call is not None:
                    return stage.call
            try:
                call = make()
            except Exception as error:
                with self._changed:
                    reason = str(error) or repr(error)
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

    def _typed(
        self,
        spec_id: SpecId,
        params: Mapping[str, Any],
        model: type[BaseModel] | None = None,
    ) -> dict[str, Any]:
        """
        ``params`` as the spec's params model gives them, or ``model`` if
        given, defaults filled in. Raises ``SubmitError`` with what the model
        finds wrong.

        A record holds its values as the log does, in JSON's types; a binding
        gets the types the params model declares, such as a tuple.
        """
        model = model or self._specs[spec_id].params
        try:
            return _values(model.model_validate(params))
        except ValidationError as error:
            problems = '; '.join(_problems(error))
            raise SubmitError(f'{spec_id}: {problems}') from None

    def _fixed(
        self, spec_id: SpecId, params: Mapping[str, Any], tables: Sequence[str]
    ) -> dict[str, Any]:
        """
        The values of an accumulator's template that fills ``tables``, each
        typed by its own field and that field's validators, defaults filled
        in.

        The params model's own validators read whole requests, so they run
        on the plain request of a state when it is read (see
        :meth:`_check_state`), not here.
        """
        model = self._specs[spec_id].params
        fields: dict[str, Any] = {
            f: (info.annotation, info)
            for f, info in model.model_fields.items()
            if f not in tables
        }
        alone = create_model(
            model.__name__,
            __config__=model.model_config,
            __validators__=_field_validators(model, fields),
            **fields,
        )
        return self._typed(spec_id, params, alone)

    def _read(self, values: dict[str, Any]) -> dict[str, Any]:
        """
        ``values`` with the outputs and datasets they reference read, the
        datasets outside the backend's lock.
        """
        with self._changed:
            values = map_refs(
                values,
                lambda ref: ref if isinstance(ref, DatasetRef) else self._value(ref),
            )
        return map_refs(values, self._datasets.read)

    def _value(self, ref: OutputRef) -> Any:
        """
        The value of an output of a completed record, if returned; lock held.

        The caller reads only what something keeps for it, so the value is
        there.
        """
        where = f'record {ref.record} output {ref.output}'
        return _returned(self._outputs[(ref.record, ref.output)], where)

    def _let_go(self, keys: Iterable[_Read]) -> None:
        """
        Count one reader less of each value; lock held. A record's output that
        no reader is left for is dropped if no client keeps it (see
        :meth:`_drop`), and a state of an accumulator lets the accumulator
        move on.

        Dropping may cancel a record, which lets go of what it reads in turn.
        The values left unread are worked through in one loop, not by
        recursion, so a chain of any length is let go of at once; a call
        within the loop only adds to it. An accumulator's state needs no
        drop: its outputs go at the next push, and the accumulator goes with
        the last reference to its ``_Accumulator``.
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
        Drop the record's outputs that nothing keeps: no client, and no
        reader (see ``_readers``); lock held.

        A pending record that nothing keeps is cancelled, since its outputs
        would be dropped the moment they are computed; cancelling lets go of
        what it reads, so this passes along a chain. A record of a state that
        has completed lets go of the state once its outputs are dropped.
        """
        if any(record_id in c.kept for c in self._clients.values()):
            return
        record = self._views.records[record_id]
        if record_id not in self._views.finished:
            if not any(self._readers[(record_id, n)] for n in record.outputs):
                self._finish(record_id, Status.CANCELLED, 'nothing keeps its outputs')
            return
        for name in record.outputs:
            if not self._readers[(record_id, name)]:
                self._outputs.pop((record_id, name), None)
        state = self._state_of.get(record_id)
        if state is not None and not any(
            (record_id, n) in self._outputs for n in record.outputs
        ):
            del self._state_of[record_id]
            self._let_go([state])

    def _complete(self, record_id: str, outputs: dict[str, Any]) -> None:
        """
        Complete a record with outputs, or fail it if they do not fit; lock
        held. An optional output the workflow left out is kept as ``_OMITTED``.
        """
        if record_id in self._views.finished:  # cancelled meanwhile
            return
        spec_id = self._views.records[record_id].spec
        fields = self._specs[spec_id].outputs.model_fields
        extra = set(outputs) - set(fields)
        missing = _required(self._specs[spec_id].outputs) - set(outputs)
        if extra or missing:
            self._finish(
                record_id,
                Status.FAILED,
                f'the workflow of {spec_id} returned {sorted(outputs)}: '
                f'missing {sorted(missing)}, not in the spec {sorted(extra)}',
            )
            return
        for name in fields:
            self._outputs[(record_id, name)] = outputs.get(name, _OMITTED)
        self._finish(record_id, Status.COMPLETED)
        self._drop(record_id)

    def _finish(
        self, record_id: str, status: Status, failure: str | None = None
    ) -> None:
        """Finish a record and start or fail what waits for it; lock held."""
        todo = [(record_id, status, failure)]
        while todo:
            finished_id, finished, message = todo.pop()
            if finished_id in self._views.finished:
                continue
            self._append(Finished(record=finished_id, status=finished, failure=message))
            for call in self._on_finished.pop(finished_id, ()):
                call(self._views.records[finished_id])
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
                        (dependent, Status.FAILED, f'input {finished_id} {finished}')
                    )
                    continue
                self._satisfied(dependent, finished_id)
        for acc in list(self._accumulating):  # a step may wait for these records
            self._advance(acc)
        self._changed.notify_all()

    def _satisfied(self, record_id: str, key: str) -> None:
        """Start the record once ``key`` was the last thing it waited for; lock held."""
        inputs = self._waiting[record_id]
        inputs.discard(key)
        if not inputs:
            del self._waiting[record_id]
            self._executor.submit(self._run, record_id)

    # Clients and what they keep

    def open_client(self, proposal: str, submitter: str) -> str:
        """A new client of the proposal; it keeps nothing yet."""
        client = uuid.uuid4().hex
        with self._changed:
            self._clients[client] = _Client(proposal, submitter)
        return client

    def close_client(self, client: str) -> None:
        """
        End the client and release everything it keeps, which cancels the
        work that nothing else keeps (see :meth:`release`).

        A client that has ended is ended again without effect.
        """
        with self._changed:
            caller = self._clients.get(client)
            if caller is None:
                return
            self.release([*caller.kept, *caller.stages, *caller.accumulators], client)
            del self._clients[client]

    def _client(self, client: str) -> _Client:
        """The client, unless it has ended."""
        with self._changed:
            caller = self._clients.get(client)
        if caller is None:
            raise ClientEnded(f'client {client} has ended')
        return caller

    def release(self, ids: Iterable[str], client: str) -> None:
        """
        Release records, stages, and accumulators the client keeps.

        A released record's outputs are dropped once nothing else keeps
        them, and a pending record that nothing else keeps is cancelled (see
        :meth:`_drop`). A released stage takes no more requests, those made
        through it still run through it, and it lets go of what its template
        references, if it has not let go already. A released accumulator
        takes no more pushes or readers; the pushes up to the last state that
        a reader still holds are added, the later ones are dropped, and its
        held state is dropped once its readers are done. Releasing what the
        client does not keep does nothing.
        """
        with self._changed:
            caller = self._client(client)
            for i in ids:
                if (stage := caller.stages.pop(i, None)) is not None:
                    self._let_go_of_template(stage)
                if (acc := caller.accumulators.pop(i, None)) is not None:
                    acc.released = True
                    self._unneeded(acc)
                if i in caller.kept:
                    caller.kept.remove(i)
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

    def open_stage(self, template: Template, client: str) -> tuple[str, Template]:
        """
        A stage of the client, and its template as the stage holds it.

        The template is checked (see :meth:`_check_template`), and the stage
        keeps its values; a request through it must have them. The stage
        keeps the outputs they reference until its first call to run has
        staged the binding, or it is released.
        """
        spec_id, blanks = template.spec, template.blanks
        fixed = self._check_template(template, self._client(client).proposal)
        stage_id = uuid.uuid4().hex
        with self._changed:
            caller = self._client(client)
            request = Request(spec_id, fixed)
            self._check_reads(Entry(request), request, caller, {})
            stage = caller.stages[stage_id] = _Stage(spec_id, blanks, fixed)
            self._readers.update(stage.reads)
        return stage_id, Template(spec_id, fixed, blanks)

    def _check_template(self, template: Template, proposal: str) -> dict[str, Any]:
        """
        The template's values with dataset names resolved, as the log holds
        them; needs no lock.

        They are checked as a request's are, but for the blanks, which are
        left out. Defaults are not filled in.
        """
        spec_id, blanks = template.spec, template.blanks
        fixed = self._resolve(spec_id, template.params, blanks, proposal)
        try:
            self.spec(spec_id).params.model_validate(fixed)
        except ValidationError as error:
            if problems := _problems(error, blanks):
                raise SubmitError(f'{spec_id}: {"; ".join(problems)}') from None
        return {field: _check_storable(field, value) for field, value in fixed.items()}

    def open_accumulator(self, template: Template, client: str) -> tuple[str, Template]:
        """
        An accumulator of the client, and its template as history holds it.

        The template's blanks are table fields, which the pushes fill. The
        template is checked as a stage's is (see :meth:`_check_template`), its
        values are typed (see :meth:`_fixed`), and the records they reference
        are checked as a request's are. The accumulator then opens: its first
        step makes the held state with these values read and defaults filled
        in, so what depends only on them is computed once (see
        :func:`~.bindings.open_held_state` and :meth:`_advance`).
        """
        spec_id, blanks = template.spec, template.blanks
        caller = self._client(client)
        tables = table_fields(self.spec(spec_id).params)
        if not blanks or not set(blanks) <= tables.keys():
            raise SubmitError(
                f'{spec_id}: the blanks of an accumulator are table fields of '
                f'{sorted(tables)}, not {list(blanks)}'
            )
        stored = Template(
            spec_id, self._check_template(template, caller.proposal), blanks
        )
        fixed = self._fixed(spec_id, stored.params, blanks)
        accumulator_id = uuid.uuid4().hex
        with self._changed:
            caller = self._client(client)
            request = Request(spec_id, fixed)
            self._check_reads(Entry(request), request, caller, {})
            self._append(
                Opened(
                    accumulator=accumulator_id,
                    proposal=caller.proposal,
                    template=stored,
                )
            )
            acc = _Accumulator(
                accumulator_id,
                self._views.accumulators[accumulator_id].template,
                self._specs[spec_id].outputs,
            )
            caller.accumulators[accumulator_id] = acc
            self._queue(acc, fixed)
        return accumulator_id, acc.template

    def push(self, accumulator_id: str, rows: Mapping[str, Row], client: str) -> None:
        """
        Log a push of one row into each named table; the rows enter one state.

        The rows are checked (see :meth:`_pushable`), and so are the records
        they reference, as a request's are, without waiting for those that
        are pending. The push is logged before it is added, so the pushes
        into one accumulator are added in the order they were logged (see
        :meth:`_advance`).
        """
        filled = self._pushable(accumulator_id, rows, client)
        with self._changed:
            caller = self._client(client)
            acc = self._accumulator(caller, accumulator_id)
            request = Request(acc.template.spec, {t: [r] for t, r in filled.items()})
            self._check_reads(Entry(request), request, caller, {})
            self._append(Pushed(accumulator=accumulator_id, rows=filled))
            self._queue(acc, filled)

    def _pushable(
        self, accumulator_id: str, rows: Mapping[str, Row], client: str
    ) -> dict[str, Row]:
        """
        The rows, once checked; needs no lock.

        Each row is checked by its table's row model alone, and refused as the
        request over that one row would refuse it. Rules on the whole table,
        such as its length, and the params model's own validators apply when
        a state is read (see :meth:`_check_state`). A row comes back as that
        request holds it: names resolved and defaults filled in.
        """
        with self._changed:
            caller = self._client(client)
            acc = self._accumulator(caller, accumulator_id)
        spec_id, blanks = acc.template.spec, acc.template.blanks
        if not rows or not rows.keys() <= set(blanks):
            raise SubmitError(
                f'a push names tables of the accumulator, {list(blanks)}, '
                f'not {sorted(rows)}'
            )
        alone = {table: [row] for table, row in rows.items()}
        resolved = self._resolve(spec_id, alone, (), caller.proposal)
        row_models = table_fields(self._specs[spec_id].params)
        filled = {}
        for table, (row,) in resolved.items():
            try:
                filled[table] = dict(row_models[table].model_validate(row))
            except ValidationError as error:
                problems = '; '.join(_problems(error, at=(table, 0)))
                raise SubmitError(f'{spec_id}: {problems}') from None
            _check_storable(table, [filled[table]])
        return filled

    def _queue(self, acc: _Accumulator, values: dict[str, Any]) -> None:
        """
        Queue a step of the accumulator: opening its held state with the
        template's values, or adding the rows of a push; lock held.

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
        _, reads = acc.steps[0]
        for record_id, _ in reads:
            status = self._views.status(record_id)
            if status is Status.PENDING:
                return
            if status is not Status.COMPLETED:
                self._stop(acc, f'{acc.step}: input {record_id} {status}')
                return
        acc.busy = True
        acc.drop()
        self._executor.submit(self._do_step, acc)

    def _do_step(self, acc: _Accumulator) -> None:
        """
        Open the accumulator's held state or add a push to it, as its next step
        says; on a worker, outside the backend's lock.

        If the step fails, the accumulator stops, since the binding may hold
        part of the rows.
        """
        # Read outside the lock: only this worker removes the step it does,
        # _unneeded keeps a busy step, and _stop runs only when no step is
        # busy, or on this worker.
        values, reads = acc.steps[0]
        try:
            read = self._read(values)
            if acc.added is None:
                binding = self._bindings[acc.template.spec]
                acc.state = open_held_state(binding, read, acc.template.blanks)
            else:
                acc.state.push(read)
        except Exception as error:  # the binding refuses the values or rows
            with self._changed:
                self._stop(acc, f'{acc.step} failed: {str(error) or repr(error)}')
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
        lock held.
        """
        acc.stopped = f'the accumulator stopped: {failure}'
        acc.busy = False
        self._accumulating.discard(acc)
        steps = list(acc.steps)
        acc.steps.clear()
        for _, reads in steps:
            self._let_go(reads)
        for upto in range(len(self._views.pushes.get(acc.id, ())) + 1):
            for record_id in self._dependents.pop(_state_key(acc.id, upto), ()):
                self._finish(record_id, Status.FAILED, acc.stopped)
        self._changed.notify_all()

    def _check_state(self, acc: _Accumulator, upto: int) -> Request | str:
        """
        The plain request of the accumulator's state after ``upto`` pushes, as
        its record holds it, or why it would be refused; needs no lock.

        It is the accumulator's template with each table filled by the rows
        pushed into it, in push order, and defaults filled in. ``acc.checked``
        keeps it for the last state validated. The validation takes time in
        proportion to the rows, so it runs outside the backend's lock, over
        the rows that the log holds.
        """
        checked = acc.checked
        if checked is not None and checked[0] == upto:
            return checked[1]
        with self._changed:
            pushed = self._views.pushes.get(acc.id, [])[:upto]
        request = _filled(acc.template, [p.rows for p in pushed])
        result: Request | str
        try:
            values = self._typed(request.spec, request.params)
            stored = {f: _check_storable(f, v) for f, v in values.items()}
        except SubmitError as error:
            result = str(error)
        else:
            result = Request(request.spec, stored)
        acc.checked = (upto, result)  # another check may overwrite it at once
        return result

    def _accumulator(self, caller: _Client, accumulator_id: str) -> _Accumulator:
        acc = caller.accumulators.get(accumulator_id)
        if acc is None:
            raise SubmitError('the accumulator was released or is unknown')
        if acc.stopped is not None:
            raise SubmitError(acc.stopped)
        return acc

    def _current(
        self, accumulator_id: str, client: str, *, reader: bool
    ) -> tuple[_Accumulator, int, Request]:
        """
        The accumulator, its number of pushes logged, and the plain request of
        that state, its current state pinned as a read pins it; held as a
        reader holds it if ``reader``, until the caller lets go of it. Raises
        ``LookupError`` if the state may not be read.
        """
        try:
            with self._changed:
                acc = self._accumulator(self._client(client), accumulator_id)
                upto = len(self._views.pushes.get(accumulator_id, ()))
                if reader:
                    self._readers[(acc, upto)] += 1
        except SubmitError as error:
            raise LookupError(str(error)) from None
        try:
            if isinstance(plain := self._check_state(acc, upto), str):
                raise LookupError(plain)
        except BaseException:  # a validator may raise anything
            if reader:
                with self._changed:
                    self._let_go([(acc, upto)])
            raise
        return acc, upto, plain

    # Queries and control, within the client's proposal

    def _pending(self, ids: Iterable[str], proposal: str) -> bool:
        """Whether a record of the proposal among ``ids`` is pending; lock held."""
        records = self._views.records
        return any(
            i in records
            and records[i].proposal == proposal
            and i not in self._views.finished
            for i in ids
        )

    def _mine(self, record_id: str, proposal: str) -> Record:
        record = self._views.records.get(record_id)
        if record is None or record.proposal != proposal:
            raise KeyError(f'no record {record_id} in proposal {proposal}')
        return record

    def status(self, ids: Iterable[str], client: str) -> list[Status]:
        with self._changed:
            proposal = self._client(client).proposal
            return [self._status(i, proposal) for i in ids]

    def wait(self, ids: Iterable[str], client: str) -> list[Status]:
        """The status of each record once all have finished."""
        ids = list(ids)
        with self._changed:
            proposal = self._client(client).proposal
            for record_id in ids:
                self._mine(record_id, proposal)
            self._changed.wait_for(lambda: not self._pending(ids, proposal))
            return [self._status(i, proposal) for i in ids]

    def failure(self, record_id: str, client: str) -> str | None:
        """
        Why a record failed, or why the backend cancelled it; ``None`` for a
        record pending, completed, or cancelled by a client.
        """
        with self._changed:
            self._mine(record_id, self._client(client).proposal)
            finished = self._views.finished.get(record_id)
            return None if finished is None else finished.failure

    def _status(self, record_id: str, proposal: str) -> Status:
        self._mine(record_id, proposal)
        return self._views.status(record_id)

    def when_finished(
        self, record_id: str, client: str, call: Callable[[Record], None]
    ) -> None:
        """
        Call ``call`` with the record once it has finished, or now if it has.

        ``call`` runs under the backend's lock, so it must return at once,
        raise nothing, and not call the backend, as ``queue.SimpleQueue.put``.
        """
        with self._changed:
            record = self._mine(record_id, self._client(client).proposal)
            if record_id in self._views.finished:
                call(record)
            else:
                self._on_finished.setdefault(record_id, []).append(call)

    def cancel(self, ids: Iterable[str], client: str) -> None:
        """
        Cancel the unfinished records; a running workflow's outputs are dropped.

        A client may cancel any record of its proposal, not only those it
        keeps: a batch that persists at submission keeps nothing, and another
        client may stop it.
        """
        with self._changed:
            proposal = self._client(client).proposal
            for record_id in ids:
                self._mine(record_id, proposal)
                self._finish(record_id, Status.CANCELLED)

    def record(self, record_id: str, client: str) -> Record:
        with self._changed:
            return self._mine(record_id, self._client(client).proposal)

    def plain_request(self, accumulator_id: str, client: str) -> Request:
        """
        The plain request of the accumulator's current state, pinned as a
        read pins it. Raises ``LookupError`` if the state may not be read.
        """
        return self._current(accumulator_id, client, reader=False)[2]

    def records(self, client: str, label: str | None = None) -> list[Record]:
        """The records of the proposal, oldest first, under ``label`` if given."""
        with self._changed:
            proposal = self._client(client).proposal
            records = self._views.records
            if label is not None:
                return [
                    records[i] for i in self._views.labels.get((proposal, label), [])
                ]
            return [r for r in records.values() if r.proposal == proposal]

    def outputs(
        self, record_id: str, names: Sequence[str] | None, client: str
    ) -> dict[str, Any]:
        """
        Values of a record's outputs, once it has completed: of ``names``, or
        of every output the workflow returned if ``names`` is ``None``.

        Only a record whose outputs the client keeps is read. Raises
        ``LookupError`` for any other, also once the client releases a record
        whose outputs this waits for, and ``RuntimeError`` if the record fails
        or is cancelled.
        """
        with self._changed:
            caller = self._client(client)
            record = self._mine(record_id, caller.proposal)
            self._changed.wait_for(
                lambda: (
                    record_id not in caller.kept or record_id in self._views.finished
                )
            )
            if record_id not in caller.kept:
                raise LookupError(f'record {record_id} is not kept by this client')
            finished = self._views.finished[record_id]
            if finished.status is not Status.COMPLETED:
                raise RuntimeError(
                    f'record {record_id} {finished.status}: {finished.failure}'
                )
            if names is None:
                names = [
                    n
                    for n in record.outputs
                    if self._outputs.get((record_id, n)) is not _OMITTED
                ]
            return {n: self._value(record.ref(n)) for n in names}

    def accumulator_outputs(
        self, accumulator_id: str, names: Sequence[str] | None, client: str
    ) -> dict[str, Any]:
        """
        Copies of outputs of the accumulator's current state: of ``names``, or
        of every output its held state returns if ``names`` is ``None``.

        The read pins the state after the pushes logged so far, waits until
        the accumulator has reached it, and is one of the state's readers
        until it has copied the outputs (see the module docstring). An
        accumulator that cannot be read, or stops before it reaches the state,
        raises ``LookupError``.
        """
        acc, upto, _ = self._current(accumulator_id, client, reader=True)
        try:
            with self._changed:
                self._changed.wait_for(
                    lambda: acc.added == upto or acc.stopped is not None
                )
                if acc.added != upto:
                    raise LookupError(acc.stopped)
            declared = list(self._specs[acc.template.spec].outputs.model_fields)
            for name in names or ():
                if name not in declared:
                    raise KeyError(f'{acc.template.spec} has no output {name!r}')
            outputs = acc.outputs()
            return copy.deepcopy(
                {
                    n: _returned(outputs[n], f'{accumulator_id}[:{upto}].{n}')
                    for n in (declared if names is None else names)
                    if names is not None or outputs[n] is not _OMITTED
                }
            )
        finally:
            with self._changed:
                self._let_go([(acc, upto)])

    # Datasets, within the client's proposal

    def datasets(self, selector: Selector, client: str) -> list[DatasetRef]:
        """The matching datasets the proposal may read, in the order measured."""
        proposal = self._client(client).proposal
        return [
            ref
            for ref in self._datasets.list(selector)
            if readable(self._datasets.metadata(ref), proposal)
        ]

    def watch_datasets(self, selector: Selector, client: str) -> Iterator[DatasetRef]:
        """Matching datasets the proposal may read: existing ones, then new ones."""
        proposal = self._client(client).proposal
        return (
            ref
            for ref in self._datasets.watch(selector)
            if readable(self._datasets.metadata(ref), proposal)
        )

    def dataset_metadata(self, ref: DatasetRef, client: str) -> dict[str, Any]:
        proposal = self._client(client).proposal
        metadata = self._datasets.metadata(ref)
        if not readable(metadata, proposal):
            raise KeyError(f'no dataset {ref} in proposal {proposal}')
        return metadata
