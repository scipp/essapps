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
while a pending request that reads it has yet to run. Releasing and ending
stop no work.

A request waits until every record it references has completed; this is the
only scheduling there is.

A request runs through its spec's binding. A stage checks its template's
values as a request's and resolves dataset names when it is made, and a
request through it must have its values outside the blanks. Such a request
runs through the callable the binding returned for the stage's first call, so
a binding that holds values computes only what depends on the blanks. A stage
lives until its client releases it or ends, and the requests made through it
have run.

An accumulator opens from a template whose one blank is a table field. Its
binding makes an element accumulator for the other values, read when it
opens, and each push adds one row to the value it holds, in place if the
binding does so. A push takes only rows whose records have completed; it
waits for the records it references to finish. A reference to an accumulator
binds when a request is submitted, to the number of pushes so far, and the
request reads the accumulator's value itself, not a copy. So the next push
waits until the requests that hold the current state have run, even those
cancelled while they run, and a request that names an earlier state is
refused. No request waits for a push, so this wait ends. Releasing the
accumulator or ending its client waits for nothing; its value is dropped once
those requests have run.
"""

from __future__ import annotations

import contextlib
import threading
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
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
from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticSerializationError, to_json

from .bindings import (
    AccumulatorBinding,
    Binding,
    ElementAccumulator,
    Function,
    as_binding,
)
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


def _check_storable(field: str, value: Any) -> None:
    """Refuse a value that cannot be stored in the log, which holds JSON."""
    try:
        to_json(value)
    except PydanticSerializationError as error:
        raise SubmitError(f'{field}: cannot be stored as JSON: {error}') from None


def _problems(error: ValidationError, blanks: Sequence[str] = ()) -> list[str]:
    """What a validation error finds wrong, but for blanks that are missing."""
    return [
        f"{'.'.join(map(str, e['loc'])) or 'params'}: {e['msg']}"
        for e in error.errors()
        if not (e['type'] == 'missing' and len(e['loc']) == 1 and e['loc'][0] in blanks)
    ]


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


class _Held:
    """
    An accumulator: its template, the binding's element accumulator, and a lock.

    A push adds its row to ``accumulator`` under ``lock`` and outside the
    backend's lock. A submission binds its references to the accumulator under
    ``lock``, so it binds before or after a push, never during one. ``stopped``
    says why the accumulator takes no more pushes or references.

    The lock order is ``lock``, then the backend's; a submission that
    references several accumulators takes their locks in the order of their
    IDs.
    """

    def __init__(
        self, accumulator_id: str, template: Template, accumulator: ElementAccumulator
    ) -> None:
        self.id = accumulator_id
        self.template = template
        self.lock = threading.Lock()
        self.accumulator = accumulator
        self.stopped: str | None = None

    @property
    def table(self) -> str:
        """The table field that the pushes fill."""
        return self.template.blanks[0]


_Read = tuple[str, str] | _Held
"""A value a request reads: an output by record ID and name, or an accumulator's."""


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
        self.accumulators: dict[str, _Held] = {}


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
        self._outputs: dict[tuple[str, str], Any] = {}  # by record ID and name
        # a value, as (record ID, name) or the accumulator that holds it, to the
        # number of pending records that read it and have yet to run
        self._readers: Counter[_Read] = Counter()
        # the reverse, for the records that have not started
        self._unread: dict[str, list[_Read]] = {}
        self._waiting: dict[str, set[str]] = {}
        self._dependents: dict[str, set[str]] = {}
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
        Wait until no record is pending, then let go of the workers and the log.

        Closing stops no work: a request that waits for an input runs once the
        input has finished, and its dependents after it.
        """
        with self._changed:
            self._changed.wait_for(
                lambda: self._views.records.keys() <= self._views.finished.keys()
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
        client's accumulators are bound under their locks, so all references to
        one accumulator bind to the same state. The records are pending, and
        the client keeps their outputs.
        """
        ids = [uuid.uuid4().hex for _ in entries]
        proposal = self._client(client).proposal
        requests = [self._prepare(e, proposal) for e in entries]
        with self._changed:
            mine = self._client(client).accumulators
            referenced = {ref.accumulator for r in requests for ref in r.accumulators()}
            holding = [mine[i] for i in sorted(referenced) if i in mine]
        with contextlib.ExitStack() as locks:
            for held in holding:
                locks.enter_context(held.lock)
            locks.enter_context(self._changed)
            caller = self._client(client)
            requests = [self._bind(r) for r in requests]
            for entry, request in zip(entries, requests, strict=True):
                self._check_reads(entry, request, caller, binds=True)
                self._check_stage(entry, request, caller)
            self._append(
                Submitted(
                    time=datetime.now(UTC),
                    proposal=proposal,
                    submitter=caller.submitter,
                    records=tuple(
                        NewRecord(
                            id=record_id,
                            request=request,
                            outputs=tuple(
                                self._specs[request.spec].outputs.model_fields
                            ),
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
                self._schedule(record_id, caller.accumulators)
            return [self._views.records[i] for i in ids]

    def _prepare(self, entry: Entry, proposal: str) -> Request:
        """The request with names resolved and defaults filled; needs no lock."""
        request = entry.request
        try:
            params = self._resolve(request.spec, request.params, (), proposal)
            try:
                model = self.spec(request.spec).params.model_validate(params)
            except ValidationError as error:
                problems = '; '.join(_problems(error))
                raise SubmitError(f'{request.spec}: {problems}') from None
            values = _values(model)
            for field, value in values.items():
                _check_storable(field, value)
        except SubmitError as error:
            raise entry.refused(error) from None
        return Request(request.spec, values)

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

    def _bind(self, request: Request) -> Request:
        """
        The request with each reference to an accumulator bound to the
        accumulator's pushes so far; the accumulator's lock held.
        """

        def bind(ref: Ref) -> Ref:
            if isinstance(ref, AccumulatorRef) and ref.upto is None:
                pushed = len(self._views.pushes.get(ref.accumulator, ()))
                return ref.model_copy(update={'upto': pushed})
            return ref

        return Request(request.spec, map_refs(request.params, bind))

    def _check_reads(
        self, entry: Entry, request: Request, caller: _Client, *, binds: bool = False
    ) -> None:
        """
        Check the outputs of records and accumulators a request reads; lock held.

        Only a submission ``binds`` references to accumulators. A stage or an
        accumulator that held a state of an accumulator would hold back its
        every push for as long as it lives, and a row pushed into one would
        read another's value while it adds.
        """
        params = self._specs[request.spec].params
        try:
            for field, value in request.params.items():
                for where, ref, target in _reads(params, field, value):
                    if isinstance(ref, AccumulatorRef) and not binds:
                        raise SubmitError(
                            f'{where}: only a request may reference an accumulator'
                        )
                    spec_id = self._readable(ref, where, caller)
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
            k: map_refs(v, lambda ref: ref)
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
        self, ref: OutputRef | AccumulatorRef, field: str, caller: _Client
    ) -> SpecId:
        """
        The spec of a record or accumulator a request may read; lock held.

        An accumulator must be the client's own, and a reference to it must
        name its current state: an earlier one is gone, since the accumulator
        adds in place.
        """
        if isinstance(ref, AccumulatorRef):
            try:
                held = self._accumulator(caller, ref.accumulator)
            except SubmitError as error:
                raise SubmitError(f'{field}: {error}') from None
            pushed = len(self._views.pushes.get(ref.accumulator, ()))
            if ref.upto != pushed:
                raise SubmitError(
                    f'{field}: {ref}: the accumulator holds only its state '
                    f'after {pushed} pushes'
                )
            return held.template.spec
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

    def _schedule(self, record_id: str, accumulators: Mapping[str, _Held]) -> None:
        """
        Start the record, or let it wait for its unfinished inputs; lock held.

        ``accumulators`` holds the accumulators the record reads, by ID.
        """
        request = self._views.records[record_id].request
        refs = request.inputs()
        self._unread[record_id] = [(ref.record, ref.output) for ref in refs] + [
            accumulators[ref.accumulator] for ref in request.accumulators()
        ]
        self._readers.update(self._unread[record_id])
        waiting = {ref.record for ref in refs} - self._views.finished.keys()
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
        accumulators = {r.id: r for r in reads if isinstance(r, _Held)}
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
        self, request: Request, stage: _Stage | None, accumulators: Mapping[str, _Held]
    ) -> tuple[Function, dict[str, Any]]:
        """
        The callable that computes a request, through its stage if it has one,
        and the values of the blanks to call it with; the inputs are read, the
        accumulators from ``accumulators``.
        """
        binding = self._bindings[request.spec]
        values = self._typed(request)
        if stage is None:
            return binding.stage(self._read(values, accumulators), ()), {}
        fixed = {k: v for k, v in values.items() if k not in stage.blanks}
        call = stage.staged(
            lambda: binding.stage(self._read(fixed, accumulators), stage.blanks)
        )
        return call, self._read({k: values[k] for k in stage.blanks}, accumulators)

    def _typed(self, request: Request) -> dict[str, Any]:
        """
        The request's values as its spec's params model gives them.

        A record holds its values as the log does, in JSON's types; a binding
        gets the types the params model declares, such as a tuple.
        """
        return _values(self._specs[request.spec].params.model_validate(request.params))

    def _read(
        self, values: dict[str, Any], accumulators: Mapping[str, _Held]
    ) -> dict[str, Any]:
        """
        ``values`` with the outputs, accumulators, and datasets they reference
        read; ``accumulators`` holds the accumulators by ID.
        """
        with self._changed:
            values = map_refs(values, lambda ref: self._read_output(ref, accumulators))
        return map_refs(values, self._datasets.read)

    def _read_output(self, ref: Ref, accumulators: Mapping[str, _Held]) -> Any:
        """
        The value of an output or an accumulator's; a dataset is read later,
        outside the lock.

        An accumulator's value is what its element accumulator holds, not a
        copy (see the module docstring).
        """
        if isinstance(ref, DatasetRef):
            return ref
        if isinstance(ref, AccumulatorRef):
            held = accumulators[ref.accumulator]
            value = held.accumulator.value
            self._check_returned(held.template.spec, value, 'the accumulator')
            return value[ref.output]
        return self._value(ref)

    def _value(self, ref: OutputRef) -> Any:
        """The value of an output of a completed record, if kept; lock held."""
        key = (ref.record, ref.output)
        if key not in self._outputs:
            raise LookupError(
                f'record {ref.record} output {ref.output}: the value is not kept'
            )
        return self._outputs[key]

    def _let_go(self, keys: Iterable[_Read]) -> None:
        """
        Count one reader less of each value, and drop an output if unkept;
        lock held.

        An accumulator needs no drop: its value goes with the last reference
        to its ``_Held``, from its client or a pending record.
        """
        for key in keys:
            self._readers[key] -= 1
            if not self._readers[key]:
                del self._readers[key]
                if isinstance(key, tuple):
                    self._drop(*key)
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
        """Complete a record with outputs, or fail it if they do not fit; lock held."""
        if record_id in self._views.finished:  # cancelled meanwhile
            return
        spec_id = self._views.records[record_id].spec
        try:
            self._check_returned(spec_id, outputs, 'the workflow')
        except ValueError as error:
            self._finish(record_id, Status.FAILED, str(error))
            return
        for name, value in outputs.items():
            self._outputs[(record_id, name)] = value
        self._drop(record_id, *outputs)
        self._finish(record_id, Status.COMPLETED)

    def _check_returned(
        self, spec_id: SpecId, outputs: Mapping[str, Any], source: str
    ) -> None:
        fields = self._specs[spec_id].outputs.model_fields
        extra = set(outputs) - set(fields)
        missing = {n for n, f in fields.items() if f.is_required()} - set(outputs)
        if extra or missing:
            raise ValueError(
                f'{source} of {spec_id} returned {sorted(outputs)}: '
                f'missing {sorted(missing)}, not in the spec {sorted(extra)}'
            )

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
                inputs = self._waiting.get(dependent)
                if inputs is None:  # finished meanwhile
                    continue
                if finished is not Status.COMPLETED:
                    todo.append(
                        (dependent, Status.FAILED, f'input {finished_id} {finished}')
                    )
                    continue
                inputs.discard(finished_id)
                if not inputs:
                    del self._waiting[dependent]
                    self._executor.submit(self._run, dependent)
        self._changed.notify_all()

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
            caller = self._clients.pop(client, None)
            if caller is None:
                return
            for record_id in caller.kept:
                self._drop(record_id, *self._views.records[record_id].outputs)

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
        more pushes or references, and its value is dropped once the requests
        that read it have run. Releasing what the client does not keep does
        nothing.
        """
        with self._changed:
            caller = self._client(client)
            for i in ids:
                caller.stages.pop(i, None)
                caller.accumulators.pop(i, None)
                if i in caller.kept:
                    caller.kept.remove(i)
                    self._drop(i, *self._views.records[i].outputs)

    def open_stage(self, template: Template, client: str) -> tuple[str, Template]:
        """
        A stage of the client, and its template as the stage holds it.

        The template's values are checked as a request's are, but for the
        blanks, and dataset names are resolved now. The stage keeps the
        values; a request through it must have them. Its first call stages
        the binding.
        """
        spec_id, blanks = template.spec, template.blanks
        proposal = self._client(client).proposal
        fixed = self._resolve(spec_id, template.params, blanks, proposal)
        try:
            self.spec(spec_id).params.model_validate(fixed)
        except ValidationError as error:
            if problems := _problems(error, blanks):
                raise SubmitError(f'{spec_id}: {"; ".join(problems)}') from None
        stage_id = uuid.uuid4().hex
        with self._changed:
            caller = self._client(client)
            request = Request(spec_id, fixed)
            self._check_reads(Entry(request), request, caller)
            caller.stages[stage_id] = _Stage(spec_id, blanks, fixed)
        return stage_id, Template(spec_id, fixed, blanks)

    def open_accumulator(self, template: Template, client: str) -> tuple[str, Template]:
        """
        An accumulator of the client, and its template as history holds it.

        The template's one blank is a table field, which the pushes fill, and
        the spec's binding must make element accumulators; a plain request
        over a table works with any binding. The other values are checked as
        the request over an empty table, and come back as that request holds
        them: names resolved and defaults filled in. Opening waits for the
        records they reference to finish, and refuses them unless they have
        completed. The binding then makes the element accumulator with those
        values read, so what depends only on them is computed once.
        """
        spec_id = template.spec
        caller = self._client(client)
        tables = table_fields(self.spec(spec_id).params)
        if len(template.blanks) != 1 or template.blanks[0] not in tables:
            raise SubmitError(
                f'{spec_id}: an accumulator has one blank, a table field of '
                f'{sorted(tables)}, not {list(template.blanks)}'
            )
        binding = self._bindings[spec_id]
        if not isinstance(binding, AccumulatorBinding):
            raise SubmitError(
                f'{spec_id} is bound to code that cannot accumulate; '
                'submit the request over a table instead'
            )
        (table,) = template.blanks
        request = self._prepare(Entry(template.fill({table: []})), caller.proposal)
        fixed = {k: v for k, v in request.params.items() if k != table}
        values = self._read_checked(Request(spec_id, fixed), caller)
        try:
            accumulator = binding.accumulator(values)
        except Exception as error:  # the binding refuses the values
            reason = str(error) or repr(error)
            raise SubmitError(
                f'{spec_id}: the accumulator failed to open: {reason}'
            ) from error
        accumulator_id = uuid.uuid4().hex
        with self._changed:
            caller = self._client(client)
            self._append(
                Opened(
                    accumulator=accumulator_id,
                    proposal=caller.proposal,
                    template=Template(spec_id, fixed, (table,)),
                )
            )
            opened = self._views.accumulators[accumulator_id]
            held = _Held(accumulator_id, opened.template, accumulator)
            caller.accumulators[accumulator_id] = held
        return accumulator_id, held.template

    def push(self, accumulator_id: str, row: Row, client: str) -> None:
        """
        Push a row, checked as the request over it alone would be.

        The push waits for the records the row references to finish, so
        whether it is refused does not depend on timing; they must have
        completed. It then waits for the requests that read the accumulator
        (see the module docstring). The row is added before its event is
        appended, under the accumulator's lock, so the pushes into one
        accumulator are logged in the order they were added. If adding fails,
        the push is refused and the accumulator stops, since the binding may
        hold part of the row.
        """
        held, filled, values = self._pushable(accumulator_id, row, client)
        with held.lock:
            with self._changed:
                self._changed.wait_for(lambda: held not in self._readers)
                self._accumulator(self._client(client), accumulator_id)
                position = len(self._views.pushes.get(accumulator_id, ()))
            try:
                held.accumulator.push(values)
            except Exception as error:  # the binding may hold part of the row
                reason = str(error) or repr(error)
                failure = f'row {position} failed to add: {reason}'
                with self._changed:
                    held.stopped = f'the accumulator stopped: {failure}'
                raise SubmitError(failure) from error
            with self._changed:
                self._append(Pushed(accumulator=accumulator_id, row=filled))

    def _pushable(
        self, accumulator_id: str, row: Row, client: str
    ) -> tuple[_Held, Row, dict[str, Any]]:
        """
        The accumulator, the row, and its values read, once checked.

        The row is checked as in the request over it alone with the
        accumulator's other values, once the records it references have
        finished, and comes back as that request holds it: names resolved and
        defaults filled in. Only the row's references are checked and read:
        the other values were read when the accumulator opened. A row that
        references an accumulator is refused; accumulators meet in a request.
        """
        with self._changed:
            caller = self._client(client)
            held = self._accumulator(caller, accumulator_id)
        table = held.table
        entry = Entry(held.template.fill({table: [row]}))
        rows = self._prepare(entry, caller.proposal).params[table]
        read = self._read_checked(Request(held.template.spec, {table: rows}), caller)
        (filled,) = rows
        (values,) = read[table]
        return held, filled, values

    def _read_checked(self, request: Request, caller: _Client) -> dict[str, Any]:
        """
        The request's values read, once the records it references have
        finished and its references are checked as a request's; it may not
        reference an accumulator. Waiting first makes whether it is refused
        independent of timing.
        """
        ids = [ref.record for ref in request.inputs()]
        with self._changed:
            self._changed.wait_for(lambda: not self._pending(ids, caller.proposal))
            self._check_reads(Entry(request), request, caller)
        try:
            return self._read(request.params, {})
        except LookupError as error:  # an output released since the check
            raise SubmitError(str(error)) from None

    def _accumulator(self, caller: _Client, accumulator_id: str) -> _Held:
        held = caller.accumulators.get(accumulator_id)
        if held is None:
            raise SubmitError('the accumulator was released or is unknown')
        if held.stopped is not None:
            raise SubmitError(held.stopped)
        return held

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

    def accumulated(self, state: AccumulatorRef, client: str) -> Request:
        """
        The plain request whose outputs a bound reference reads: the
        accumulator's template with the table filled by the first ``upto``
        rows pushed.
        """
        with self._changed:
            proposal = self._client(client).proposal
            opened = self._views.accumulators.get(state.accumulator)
            if opened is None or opened.proposal != proposal:
                raise KeyError(
                    f'no accumulator {state.accumulator} in proposal {proposal}'
                )
            rows = self._views.pushes.get(state.accumulator, [])[: state.upto]
            if len(rows) != state.upto:
                raise LookupError(f'the log lacks rows that {state} reads')
            return opened.template.fill({opened.template.blanks[0]: rows})

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

    def output(self, record_id: str, name: str, client: str) -> Any:
        """The value of an output of a completed record, if it is still kept."""
        with self._changed:
            record = self._mine(record_id, self._client(client).proposal)
            return self._value(record.ref(name))

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
