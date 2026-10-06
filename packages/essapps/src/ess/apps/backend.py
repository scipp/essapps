# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The backend checks requests, keeps records, and runs requests. It also
answers its clients' queries about the datasets of its dataset source.

What the backend knows of history is its log (see ``log.py``): every
submission, finished record, opened accumulator, and push is appended as one
event and then applied to its views (see ``views.py``). A change is checked
before its event is appended, so the log holds only events that apply. A
backend given a log that already has events applies them first and runs the
records left pending, without their stages; a record left pending that reads
an accumulator fails, since the accumulator is gone.

A client is one entry in the backend, from :meth:`Backend.open_client` to
:meth:`Backend.close_client`: its proposal, the records whose outputs it keeps,
and its stages and accumulators. Every call names its client, and a call of a
client that has ended raises :class:`ClientEnded`. Clients, output values, and
what waits for what are not history. This backend keeps them in memory, so a
backend started from an existing log has no clients and cannot read the
outputs of the backend that wrote it.

An output value is kept while the client that made its record keeps it, or
while a pending request or a step of an accumulator that reads it has yet to
run. Releasing and ending stop no work.

A request waits until every record it references has completed, and the
state of an accumulator it reads has been reached; an accumulator's steps
wait as described below. This is the only scheduling there is.

A request runs through its spec's binding. A stage checks its template's
values as a request's and resolves dataset names when it is made, and a
request through it must have its values outside the blanks. Such a request
runs through the callable the binding returned for the stage's first call, so
a binding that holds values computes only what depends on the blanks. A stage
lives until its client releases it or ends, and the requests made through it
have run.

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
that reads a state the accumulator has yet to reach waits for it as for an
input, and a read of its outputs waits for it as ``wait`` waits for a record.
A request reads at most one accumulator, and a row or a template reads none.
A state may be read only if its plain request would be accepted. The first
read of a state validates that request, outside the backend's lock, and a
read of a state whose request would be refused is refused with that
request's reason. So rules on a whole table, such as its length, and the
params model's own validators apply at reads, not at pushes. Validators that
read more than one value, such as the params model's own or those of a table
field, must not change a value: the held state got the values typed one by
one, and nothing checks that the plain request holds the same.

The outputs of a state are computed outside the backend's lock, all at once,
the first time a request or a read of its outputs reads the state, and kept
until the next push. A request reads them as the binding returns them, not a
copy, and a read of the outputs copies them. So the next push waits until the
readers of the state before it are done: the requests that reference it,
even those cancelled while they run, and the reads of its outputs in
progress. A request that names an earlier state is refused. A reader of a
state waits only for pushes up to that state and for records submitted
before it, so no wait goes round in a circle. Releasing the accumulator or
ending its client stops no work: the pushes logged are added, and the held
state is dropped once its readers are done.
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

from ess.reduce.spec import (
    AccumulatorRef,
    DataField,
    DatasetRef,
    OutputRef,
    Ref,
    WorkflowSpec,
    data_fields,
    table_fields,
    walk_refs,
)
from pydantic import BaseModel, ValidationError, create_model, field_validator
from pydantic_core import (
    PydanticSerializationError,
    from_json,
    to_json,
    to_jsonable_python,
)

from .bindings import Binding, Function, HeldState, as_binding, open_held_state
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
    A stage: its spec, its blanks, its fixed values, and the binding's callable.

    The fixed values are the template's, with dataset names resolved when the
    stage was made. The first call stages the binding, and so does the call
    after one that failed to stage. What the callable holds is a cache: the
    backend may drop it at any time, and the next call stages the binding
    again and makes the same record.
    """

    def __init__(
        self, spec: SpecId, blanks: tuple[str, ...], fixed: dict[str, Any]
    ) -> None:
        self.spec = spec
        self.blanks = blanks
        self.fixed = fixed
        self._lock = threading.Lock()
        self._call: Function | None = None

    def staged(self, stage: Callable[[], Function]) -> Function:
        """The binding's callable; ``stage`` makes it if there is none."""
        with self._lock:
            if self._call is None:
                self._call = stage()
            return self._call


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
    request was validated, and why that request would be refused, or ``None``
    if it would be accepted. Two reads that validate different states at once
    may overwrite each other's verdict, which costs only another validation.
    ``stopped`` says why the accumulator takes no more pushes or readers, or
    is ``None``.

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
        self.checked: tuple[int, str | None] | None = None
        self.stopped: str | None = None
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
    """What a request waits for that reads the state after ``upto`` pushes."""
    return f'{accumulator_id}[:{upto}]'


_Read = tuple[str, str] | tuple[_Accumulator, int]
"""
A value a request reads: an output by record ID and name, or an accumulator's
state by its number of pushes.
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
        # the number of pending records, steps, and reads that have yet to read it
        self._readers: Counter[_Read] = Counter()
        # the reverse, for the records that have not started
        self._unread: dict[str, list[_Read]] = {}
        # a record that has not started to the records and states it waits for
        self._waiting: dict[str, set[str]] = {}
        self._dependents: dict[str, set[str]] = {}
        self._accumulating: set[_Accumulator] = set()  # with steps not yet done
        self._stage_of: dict[str, _Stage] = {}  # unfinished record ID to its stage
        self._on_finished: dict[str, list[Callable[[Record], None]]] = {}
        with self._changed:
            for event in self._log:
                self._views.apply(event)
            for record in list(self._views.records.values()):
                if record.id in self._views.finished:
                    continue
                if record.request.accumulators():
                    self._finish(
                        record.id, Status.FAILED, 'the accumulator ended at a restart'
                    )
                else:
                    self._schedule(record.id, {})

    def close(self) -> None:
        """
        Wait until no record is pending and no accumulator has a step to do,
        then let go of the workers and the log.

        Closing stops no work: a request that waits for an input runs once the
        input has finished, and its dependents after it.
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
        resolved before the backend's lock is taken. References to the
        client's accumulators are pinned to the pushes logged so far, so all
        references to one accumulator pin the same state, and that state's
        plain request is checked outside the lock (see :meth:`_check_state`).
        The records are pending, and the client keeps their outputs.
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
                requests = [self._pin(r, pinned) for r in requests]
                for entry, request in zip(entries, requests, strict=True):
                    self._check_reads(entry, request, caller, states=states)
                    self._check_stage(entry, request, caller)
                return self._create(entries, requests, caller, pinned)
        finally:
            with self._changed:
                self._let_go(pinned.values())

    def _create(
        self,
        entries: list[Entry],
        requests: list[Request],
        caller: _Client,
        pinned: Mapping[str, tuple[_Accumulator, int]],
    ) -> list[Record]:
        """Log and schedule the checked requests as the client's records; lock held."""
        ids = [uuid.uuid4().hex for _ in entries]
        self._append(
            Submitted(
                time=datetime.now(UTC),
                proposal=caller.proposal,
                submitter=caller.submitter,
                records=tuple(
                    NewRecord(
                        id=record_id,
                        request=request,
                        outputs=tuple(self._specs[request.spec].outputs.model_fields),
                        label=entry.label,
                        member=entry.member,
                    )
                    for record_id, entry, request in zip(
                        ids, entries, requests, strict=True
                    )
                ),
            )
        )
        caller.kept.update(ids)
        for record_id, entry in zip(ids, entries, strict=True):
            if entry.stage is not None:
                self._stage_of[record_id] = caller.stages[entry.stage]
            self._schedule(record_id, pinned)
        return [self._views.records[i] for i in ids]

    def _prepare(self, entry: Entry, proposal: str) -> Request:
        """
        The request with names resolved and defaults filled; needs no lock.

        A request reads at most one accumulator: on the service each
        accumulator is a job of its own, and the request runs in it.
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

    def _pin(
        self, request: Request, pinned: Mapping[str, tuple[_Accumulator, int]]
    ) -> Request:
        """
        The request with each reference to an accumulator in ``pinned``
        pinned to that state, unless it names one; needs no lock.
        """

        def pin(ref: Ref) -> Ref:
            if (
                isinstance(ref, AccumulatorRef)
                and ref.upto is None
                and ref.accumulator in pinned
            ):
                return ref.model_copy(update={'upto': pinned[ref.accumulator][1]})
            return ref

        return Request(request.spec, map_refs(request.params, pin))

    def _check_reads(
        self,
        entry: Entry,
        request: Request,
        caller: _Client,
        *,
        states: Mapping[str, tuple[_Accumulator, int, str | None]] | None = None,
    ) -> None:
        """
        Check the outputs of records and accumulators a request reads; lock held.

        Only a submission reads accumulators, from the ``states`` it pinned,
        each with why its plain request would be refused, or ``None``. A stage
        or an accumulator that held a state of an accumulator would hold back
        its every push for as long as it lives, and a row pushed into one would
        read another's value while it adds.
        """
        params = self._specs[request.spec].params
        try:
            for field, value in request.params.items():
                for where, ref, target in _reads(params, field, value):
                    if isinstance(ref, AccumulatorRef) and states is None:
                        raise SubmitError(
                            f'{where}: only a request may reference an accumulator'
                        )
                    spec_id = self._readable(ref, where, caller, states or {})
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
        states: Mapping[str, tuple[_Accumulator, int, str | None]],
    ) -> SpecId:
        """
        The spec of a record or accumulator a request may read; lock held.

        An accumulator must be the client's own, not stopped, and its state
        pinned in ``states`` readable, and a reference to it must name that
        state: an earlier one is gone, since the accumulator keeps one held
        state.
        """
        if isinstance(ref, AccumulatorRef):
            try:
                acc = self._accumulator(caller, ref.accumulator)
            except SubmitError as error:
                raise SubmitError(f'{field}: {error}') from None
            _, pushed, refused = states[ref.accumulator]
            if refused is not None:
                raise SubmitError(f'{field}: {refused}')
            if ref.upto != pushed:
                raise SubmitError(
                    f'{field}: {ref}: the accumulator holds only its state '
                    f'after {pushed} pushes'
                )
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
        return record.spec

    # Execution

    def _schedule(
        self, record_id: str, pinned: Mapping[str, tuple[_Accumulator, int]]
    ) -> None:
        """
        Start the record, or let it wait for its unfinished inputs and the
        state of the accumulator it reads; lock held.

        ``pinned`` holds the states the record's submission pinned, by ID.
        """
        request = self._views.records[record_id].request
        refs = request.inputs()
        states = {pinned[ref.accumulator] for ref in request.accumulators()}
        self._unread[record_id] = [(ref.record, ref.output) for ref in refs]
        self._unread[record_id].extend(states)
        self._readers.update(self._unread[record_id])
        waiting = {ref.record for ref in refs} - self._views.finished.keys()
        waiting.update(
            _state_key(acc.id, upto) for acc, upto in states if acc.added != upto
        )
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
        still reads them.
        """
        with self._changed:
            if record_id in self._views.finished:
                return
            record = self._views.records[record_id]
            stage = self._stage_of.get(record_id)
            reads = self._unread.pop(record_id)
        accumulators = {r.id: r for r, _ in reads if isinstance(r, _Accumulator)}
        try:
            call, blanks = self._call(record.request, stage, accumulators)
            outputs = dict(call(**blanks))
        except Exception as error:  # any failure of a workflow is recorded
            with self._changed:
                self._finish(record_id, Status.FAILED, str(error) or repr(error))
                self._let_go(reads)
            return
        with self._changed:
            self._complete(record_id, outputs)
            self._let_go(reads)

    def _call(
        self,
        request: Request,
        stage: _Stage | None,
        accumulators: Mapping[str, _Accumulator],
    ) -> tuple[Function, dict[str, Any]]:
        """
        The callable that computes a request, through its stage if it has one,
        and the values of the blanks to call it with; the inputs are read, the
        accumulators from ``accumulators``.
        """
        binding = self._bindings[request.spec]
        values = self._typed(request.spec, request.params)
        if stage is None:
            return binding.stage(self._read(values, accumulators), ()), {}
        fixed = {k: v for k, v in values.items() if k not in stage.blanks}
        call = stage.staged(
            lambda: binding.stage(self._read(fixed, accumulators), stage.blanks)
        )
        return call, self._read({k: values[k] for k in stage.blanks}, accumulators)

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

    def _read(
        self, values: dict[str, Any], accumulators: Mapping[str, _Accumulator]
    ) -> dict[str, Any]:
        """
        ``values`` with the outputs, accumulators' outputs, and datasets they
        reference read; ``accumulators`` holds the accumulators by ID.
        """
        ids = {
            r.accumulator for _, r in walk_refs(values) if isinstance(r, AccumulatorRef)
        }
        states = {i: accumulators[i].outputs() for i in ids}
        with self._changed:
            values = map_refs(values, lambda ref: self._read_output(ref, states))
        return map_refs(values, self._datasets.read)

    def _read_output(self, ref: Ref, states: Mapping[str, Mapping[str, Any]]) -> Any:
        """
        The value of an output, or of an accumulator's from ``states``, its
        computed outputs by accumulator ID; a dataset is read later, outside
        the lock.
        """
        if isinstance(ref, DatasetRef):
            return ref
        if isinstance(ref, AccumulatorRef):
            return _returned(states[ref.accumulator][ref.output], str(ref))
        return self._value(ref)

    def _value(self, ref: OutputRef) -> Any:
        """
        The value of an output of a completed record, if kept and returned;
        lock held.
        """
        where = f'record {ref.record} output {ref.output}'
        if (ref.record, ref.output) not in self._outputs:
            raise LookupError(f'{where}: the value is not kept')
        return _returned(self._outputs[(ref.record, ref.output)], where)

    def _let_go(self, keys: Iterable[_Read]) -> None:
        """
        Count one reader less of each value, and drop an output once it is
        unkept and unread; lock held.

        An accumulator's state needs no drop: its outputs go at the next push,
        which may start once the state is unread, and the accumulator goes
        with the last reference to its ``_Accumulator``.
        """
        for key in keys:
            self._readers[key] -= 1
            if not self._readers[key]:
                del self._readers[key]
                match key:
                    case (_Accumulator() as acc, _):
                        self._advance(acc)
                    case (str() as record_id, str() as name):
                        self._drop(record_id, name)
        self._changed.notify_all()

    def _drop(self, record_id: str, *names: str) -> None:
        """
        Drop the record's outputs that no client keeps and no pending record
        that reads them has yet to run; lock held.
        """
        if any(record_id in c.kept for c in self._clients.values()):
            return
        for name in names:
            if not self._readers[(record_id, name)]:
                self._outputs.pop((record_id, name), None)

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
        self._drop(record_id, *fields)
        self._finish(record_id, Status.COMPLETED)

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
            self._waiting.pop(finished_id, None)
            self._stage_of.pop(finished_id, None)
            for dependent in self._dependents.pop(finished_id, set()):
                if dependent not in self._waiting:  # finished meanwhile
                    continue
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
        End the client and release everything it keeps; stops no work.

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
        Release records, stages, and accumulators the client keeps; stops no work.

        A released record's outputs are dropped once the pending records that
        read them have run. A released stage takes no more requests, and those
        made through it still run through it. A released accumulator takes no
        more pushes or readers; the pushes logged are still added, and its
        held state is dropped once its readers are done. Releasing what the
        client does not keep does nothing.
        """
        with self._changed:
            caller = self._client(client)
            for i in ids:
                caller.stages.pop(i, None)
                caller.accumulators.pop(i, None)
                if i in caller.kept:
                    caller.kept.remove(i)
                    self._drop(i, *self._views.records[i].outputs)
            self._changed.notify_all()

    def open_stage(self, template: Template, client: str) -> tuple[str, Template]:
        """
        A stage of the client, and its template as the stage holds it.

        The template is checked (see :meth:`_check_template`), and the stage
        keeps its values; a request through it must have them. Its first call
        stages the binding.
        """
        spec_id, blanks = template.spec, template.blanks
        fixed = self._check_template(template, self._client(client).proposal)
        stage_id = uuid.uuid4().hex
        with self._changed:
            caller = self._client(client)
            request = Request(spec_id, fixed)
            self._check_reads(Entry(request), request, caller)
            caller.stages[stage_id] = _Stage(spec_id, blanks, fixed)
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
            self._check_reads(Entry(request), request, caller)
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
            self._check_reads(Entry(request), request, caller)
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
        reads = [
            (ref.record, ref.output)
            for _, ref in walk_refs(values)
            if isinstance(ref, OutputRef)
        ]
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
        values, reads = acc.steps[0]
        try:
            read = self._read(values, {})
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
                if record_id in self._waiting:  # not cancelled meanwhile
                    self._satisfied(record_id, _state_key(acc.id, acc.added))
            self._advance(acc)

    def _stop(self, acc: _Accumulator, failure: str) -> None:
        """
        Stop the accumulator: it takes no more pushes or readers, its steps
        are dropped, and the requests that wait for its states fail; lock held.
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

    def _check_state(self, acc: _Accumulator, upto: int) -> str | None:
        """
        Why the plain request of the accumulator's state after ``upto`` pushes
        would be refused, or ``None``; needs no lock.

        ``acc.checked`` keeps the verdict for the last state validated. The
        validation takes time in proportion to the rows, so it runs outside
        the backend's lock, over the rows that the log holds.
        """
        checked = acc.checked
        if checked is not None and checked[0] == upto:
            return checked[1]
        with self._changed:
            pushed = self._views.pushes.get(acc.id, [])[:upto]
        request = _filled(acc.template, [p.rows for p in pushed])
        try:
            self._typed(request.spec, request.params)
        except SubmitError as error:
            acc.checked = (upto, str(error))
        else:
            acc.checked = (upto, None)
        return acc.checked[1]

    def _accumulator(self, caller: _Client, accumulator_id: str) -> _Accumulator:
        acc = caller.accumulators.get(accumulator_id)
        if acc is None:
            raise SubmitError('the accumulator was released or is unknown')
        if acc.stopped is not None:
            raise SubmitError(acc.stopped)
        return acc

    def _current(
        self, accumulator_id: str, client: str, *, reader: bool
    ) -> tuple[_Accumulator, int]:
        """
        The accumulator and its number of pushes logged, its current state
        pinned as a read pins it; held as a reader holds it if ``reader``,
        until the caller lets go of it. Raises ``LookupError`` if the state
        may not be read.
        """
        try:
            with self._changed:
                acc = self._accumulator(self._client(client), accumulator_id)
                upto = len(self._views.pushes.get(accumulator_id, ()))
                if reader:
                    self._readers[(acc, upto)] += 1
        except SubmitError as error:
            raise LookupError(str(error)) from None
        if (refused := self._check_state(acc, upto)) is not None:
            if reader:
                with self._changed:
                    self._let_go([(acc, upto)])
            raise LookupError(refused)
        return acc, upto

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
        """Why a record failed; ``None`` unless it has failed."""
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
        """Cancel the unfinished records; a running workflow's outputs are dropped."""
        with self._changed:
            proposal = self._client(client).proposal
            for record_id in ids:
                self._mine(record_id, proposal)
                self._finish(record_id, Status.CANCELLED)

    def record(self, record_id: str, client: str) -> Record:
        with self._changed:
            return self._mine(record_id, self._client(client).proposal)

    def accumulated(
        self, accumulator_id: str, upto: int | None, client: str
    ) -> Request:
        """
        The plain request of the accumulator's state after ``upto`` pushes, as
        a reference in a record names it, or of its current state, pinned as
        a read pins it, if ``upto`` is ``None``. Raises ``LookupError`` if the
        log lacks these pushes, or the current state may not be read.

        It is the accumulator's template with each table filled by the rows
        pushed into it, in push order, and defaults filled in, as the record
        of that request holds it.
        """
        if upto is None:
            upto = self._current(accumulator_id, client, reader=False)[1]
        with self._changed:
            proposal = self._client(client).proposal
            opened = self._views.accumulators.get(accumulator_id)
            if opened is None or opened.proposal != proposal:
                raise KeyError(
                    f'no accumulator {accumulator_id} in proposal {proposal}'
                )
            pushed = self._views.pushes.get(accumulator_id, [])[:upto]
        if len(pushed) != upto:
            raise LookupError(f'the log lacks pushes of {accumulator_id}[:{upto}]')
        request = _filled(opened.template, [p.rows for p in pushed])
        values = to_jsonable_python(self._typed(request.spec, request.params))
        return Request(request.spec, as_refs(values))

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
        Values of a completed record's outputs, if still kept: of ``names``,
        or of every output the workflow returned if ``names`` is ``None``.
        """
        with self._changed:
            record = self._mine(record_id, self._client(client).proposal)
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
        acc, upto = self._current(accumulator_id, client, reader=True)
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
