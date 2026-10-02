# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The backend checks requests, keeps records, and runs requests. It also
answers its clients' queries about the datasets of its dataset source.

What the backend knows of history is its log (see ``log.py``): every
submission, finished record, and push is appended as one event and then
applied to its views (see ``views.py``). A change is checked before its event
is appended, so the log holds only events that apply. A backend given a log
that already has events applies them first and runs the records left pending,
without their stages; a snapshot left pending fails, since its accumulator is
gone.

A client is one entry in the backend, from :meth:`Backend.open_client` to
:meth:`Backend.close_client`: its proposal, the records whose outputs it keeps,
and its stages and accumulators. Every call names its client, and a call of a
client that has ended raises :class:`ClientEnded`. Clients, output values, and
what waits for what are not history. This backend keeps them in memory, so a
backend started from an existing log has no clients and cannot read the
outputs of the backend that wrote it.

An output value is kept while the client that made its record keeps it, or
while a pending request has yet to read it. Releasing and ending stop no work.

A request waits until every record it references has completed; this is the
only scheduling there is.

A request runs through its spec's binding. A request through a stage runs
through the callable the binding returned for the stage's first call, so a
binding that holds values computes only what depends on the blanks. A stage
lives until its client releases it or ends, and the requests made through it
have run.

An accumulator takes only elements whose records have completed; a push
waits for the records it references to finish. A push then combines the
element into the value the accumulator holds, and a snapshot's record
completes at submission with that value. A snapshot's record names the
accumulator and how many elements it covers, so it costs the same however
many elements that is.
"""

from __future__ import annotations

import threading
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ess.reduce.spec import (
    DataField,
    DatasetRef,
    OutputRef,
    WorkflowSpec,
    data_fields,
    table_fields,
)
from pydantic import BaseModel, ValidationError

from .accumulators import element_table
from .bindings import (
    AccumulatorBinding,
    Binding,
    ElementAccumulator,
    Function,
    as_binding,
)
from .datasets import DatasetSource, Selector, readable
from .log import Event, Finished, Log, NewRecord, Pushed, Submitted
from .records import (
    Element,
    Record,
    Request,
    Snapshot,
    SpecId,
    Status,
    SubmitError,
    map_refs,
    output_refs,
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


def _reads(
    params: type[BaseModel], field: str, value: Any
) -> Iterator[tuple[str, OutputRef, DataField | None]]:
    """Each output a param's value references, with its place and the field it fills."""
    row = table_fields(params).get(field)
    if row is None:
        target = data_fields(params).get(field)
        yield from ((field, ref, target) for ref in output_refs(value))
        return
    cells = data_fields(row)
    for i, cell_values in enumerate(value):
        for cell, cell_value in cell_values.items():
            for ref in output_refs(cell_value):
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
    A stage: the binding's callable, and the fixed values it was staged with.

    The first call stages the binding. A call with other fixed values, such as
    a dataset name that now resolves to another dataset, stages it again.
    """

    def __init__(self, spec: SpecId, blanks: tuple[str, ...]) -> None:
        self.spec = spec
        self.blanks = blanks
        self._lock = threading.Lock()
        self._fixed: dict[str, Any] | None = None
        self._call: Function | None = None

    def staged(self, fixed: dict[str, Any], stage: Callable[[], Function]) -> Function:
        """The binding's callable for ``fixed``; ``stage`` makes it if needed."""
        with self._lock:
            if self._call is None or fixed != self._fixed:
                self._call, self._fixed = stage(), fixed
            return self._call


class _Held:
    """
    An accumulator: what it holds, and the lock that orders its pushes.

    A push combines its element into ``accumulator``, the binding's, under
    ``lock`` and outside the backend's lock. ``value`` is the combined value of
    the accumulator's elements in the views; it changes under the backend's
    lock together with them, so a snapshot reads the two as one. ``stopped``
    says why the accumulator takes no more pushes or snapshots.
    """

    def __init__(self, spec: SpecId, accumulator: ElementAccumulator) -> None:
        self.spec = spec
        self.lock = threading.Lock()
        self.accumulator = accumulator
        self.value: Mapping[str, Any] | None = None
        self.stopped: str | None = None


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
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
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
        self._clock = clock
        self._executor = ThreadPoolExecutor(max_workers=workers)
        self._changed = threading.Condition()
        self._log = Log() if log is None else log
        self._views = Views()
        # Not history
        self._clients: dict[str, _Client] = {}  # by client ID, until it ends
        self._outputs: dict[tuple[str, str], Any] = {}  # by record ID and name
        # (record ID, name) to the number of pending records yet to read it
        self._readers: Counter[tuple[str, str]] = Counter()
        self._unread: dict[str, list[tuple[str, str]]] = {}  # the reverse
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
                if isinstance(record.submitted, Snapshot):
                    self._finish(
                        record.id, Status.FAILED, 'the accumulator ended at a restart'
                    )
                else:
                    self._schedule(record.id)

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
        resolved before the backend's lock is taken. The records are pending,
        and the client keeps their outputs.
        """
        ids = [uuid.uuid4().hex for _ in entries]
        proposal = self._client(client).proposal
        requests = [self._prepare(e, proposal) for e in entries]
        with self._changed:
            caller = self._client(client)
            for entry, request in zip(entries, requests, strict=True):
                self._check_reads(entry, request, proposal)
                self._check_stage(entry, request, caller)
            self._append(
                Submitted(
                    time=self._clock(),
                    proposal=proposal,
                    submitter=caller.submitter,
                    records=tuple(
                        NewRecord(
                            id=record_id,
                            submitted=request,
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
                self._schedule(record_id)
            return [self._views.records[i] for i in ids]

    def snapshot(self, accumulator_id: str, *, client: str) -> Record:
        """
        A record of the accumulator's combined value, completed at once.

        It covers the elements pushed so far, and names the accumulator and how
        many elements that is. The client keeps its outputs.
        """
        record_id = uuid.uuid4().hex
        with self._changed:
            caller = self._client(client)
            held = self._accumulator(caller, accumulator_id)
            value = held.value
            if value is None:
                raise SubmitError('nothing has been pushed')
            snapshot = Snapshot(
                spec=held.spec,
                accumulator=accumulator_id,
                upto=len(self._views.elements[accumulator_id]),
            )
            outputs = tuple(self._specs[held.spec].outputs.model_fields)
            self._append(
                Submitted(
                    time=self._clock(),
                    proposal=caller.proposal,
                    submitter=caller.submitter,
                    records=(
                        NewRecord(id=record_id, submitted=snapshot, outputs=outputs),
                    ),
                )
            )
            caller.kept.add(record_id)
            self._complete(record_id, dict(value))
            return self._views.records[record_id]

    def _prepare(self, entry: Entry, proposal: str) -> Request:
        """The request with names resolved and defaults filled; needs no lock."""
        request = entry.request
        try:
            spec = self.spec(request.spec)
            unknown = set(request.params) - set(spec.params.model_fields)
            if unknown:
                raise SubmitError(
                    f'{sorted(unknown)}: not parameters of {request.spec}'
                )
            _check_cells(spec.params, request.params)
            params = {
                field: map_refs(value, self._resolver(field, proposal))
                for field, value in request.params.items()
            }
            try:
                model = spec.params.model_validate(params)
            except ValidationError as error:
                problems = '; '.join(
                    f"{'.'.join(map(str, e['loc'])) or 'params'}: {e['msg']}"
                    for e in error.errors()
                )
                raise SubmitError(f'{request.spec}: {problems}') from None
        except SubmitError as error:
            raise entry.refused(error) from None
        return Request(request.spec, _values(model))

    def _resolver(
        self, field: str, proposal: str
    ) -> Callable[[OutputRef | DatasetRef], OutputRef | DatasetRef]:
        def resolve(ref: OutputRef | DatasetRef) -> OutputRef | DatasetRef:
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
            if ref.key is not None:
                raise SubmitError(f'{field}: {ref} names an element of an output')
            return ref

        return resolve

    def _check_reads(self, entry: Entry, request: Request, proposal: str) -> None:
        """Check the outputs a request reads; lock held."""
        params = self._specs[request.spec].params
        try:
            for field, value in request.params.items():
                for where, ref, target in _reads(params, field, value):
                    spec_id = self._readable(ref, where, proposal)
                    outputs = self._output_fields[spec_id]
                    if ref.output not in outputs:
                        raise SubmitError(
                            f'{where}: {spec_id} has no output {ref.output!r}'
                        )
                    if target is not None and not _agree(outputs[ref.output], target):
                        raise SubmitError(f'{where}: {ref} does not fit the field')
                    if self._views.status(ref.record) is Status.COMPLETED:
                        try:
                            self._value(ref)
                        except LookupError as error:
                            raise SubmitError(f'{where}: {error}') from None
        except SubmitError as error:
            raise entry.refused(error) from None

    def _check_stage(self, entry: Entry, request: Request, caller: _Client) -> None:
        """Check the stage a request goes through; lock held."""
        if entry.stage is None:
            return
        stage = caller.stages.get(entry.stage)
        if stage is None:
            raise entry.refused(SubmitError('the stage was released or is unknown'))
        if stage.spec != request.spec:
            raise entry.refused(
                SubmitError(f'the stage holds {stage.spec}, not {request.spec}')
            )

    def _readable(self, ref: OutputRef, field: str, proposal: str) -> SpecId:
        """The spec of a record a request may read; lock held."""
        record = self._views.records.get(ref.record)
        if record is None:
            raise SubmitError(f'{field}: unknown record {ref.record}')
        if record.proposal != proposal:
            raise SubmitError(
                f'{field}: record {ref.record} belongs to proposal {record.proposal}'
            )
        status = self._views.status(ref.record)
        if status in (Status.FAILED, Status.CANCELLED):
            raise SubmitError(f'{field}: record {ref.record} {status}')
        return record.spec

    # Execution

    def _schedule(self, record_id: str) -> None:
        """Start the record, or let it wait for its unfinished inputs; lock held."""
        refs = self._views.records[record_id].request.inputs()
        self._unread[record_id] = [(ref.record, ref.output) for ref in refs]
        self._readers.update(self._unread[record_id])
        waiting = {ref.record for ref in refs} - self._views.finished.keys()
        if not waiting:
            self._executor.submit(self._run, record_id)
            return
        self._waiting[record_id] = waiting
        for input_id in waiting:
            self._dependents.setdefault(input_id, set()).add(record_id)

    def _run(self, record_id: str) -> None:
        with self._changed:
            if record_id in self._views.finished:
                return
            record = self._views.records[record_id]
            stage = self._stage_of.get(record_id)
        try:
            call, blanks = self._call(record.request, stage)
            with self._changed:
                self._has_read(record_id)
            outputs = dict(call(**blanks))
        except Exception as error:  # any failure of a workflow is recorded
            with self._changed:
                self._finish(record_id, Status.FAILED, str(error) or repr(error))
            return
        with self._changed:
            self._complete(record_id, outputs)

    def _call(
        self, request: Request, stage: _Stage | None
    ) -> tuple[Function, dict[str, Any]]:
        """
        The callable that computes a request, through its stage if it has one,
        and the values of the blanks to call it with; the inputs are read.
        """
        binding = self._bindings[request.spec]
        values = self._typed(request)
        if stage is None:
            return binding.stage(self._read(values), ()), {}
        fixed = {k: v for k, v in values.items() if k not in stage.blanks}
        call = stage.staged(
            fixed, lambda: binding.stage(self._read(fixed), stage.blanks)
        )
        return call, self._read({k: values[k] for k in stage.blanks})

    def _typed(self, request: Request) -> dict[str, Any]:
        """
        The request's values as its spec's params model gives them.

        A record holds its values as the log does, in JSON's types; a binding
        gets the types the params model declares, such as a tuple.
        """
        return _values(self._specs[request.spec].params.model_validate(request.params))

    def _read(self, values: dict[str, Any]) -> dict[str, Any]:
        """``values`` with the outputs and datasets they reference read."""
        with self._changed:
            values = map_refs(values, self._read_output)
        return map_refs(values, self._datasets.read)

    def _read_output(self, ref: OutputRef | DatasetRef) -> Any:
        """The value of an output; a dataset is read later, outside the lock."""
        return ref if isinstance(ref, DatasetRef) else self._value(ref)

    def _value(self, ref: OutputRef) -> Any:
        """The value of an output of a completed record, if kept; lock held."""
        try:
            return self._outputs[(ref.record, ref.output)]
        except KeyError:
            raise LookupError(
                f'record {ref.record} output {ref.output}: the value is not kept'
            ) from None

    def _has_read(self, record_id: str) -> None:
        """Let go of the values a record was yet to read; lock held."""
        for key in self._unread.pop(record_id, ()):
            self._readers[key] -= 1
            if not self._readers[key]:
                del self._readers[key]
                self._drop(*key)

    def _drop(self, record_id: str, *names: str) -> None:
        """
        Drop the record's outputs that no client keeps and no pending record
        is yet to read; lock held.
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
        try:
            self._check_returned(self._views.records[record_id].spec, outputs)
        except ValueError as error:
            self._finish(record_id, Status.FAILED, str(error))
            return
        for name, value in outputs.items():
            self._outputs[(record_id, name)] = value
        self._drop(record_id, *outputs)
        self._finish(record_id, Status.COMPLETED)

    def _check_returned(self, spec_id: SpecId, outputs: dict[str, Any]) -> None:
        fields = self._specs[spec_id].outputs.model_fields
        extra = set(outputs) - set(fields)
        missing = {n for n, f in fields.items() if f.is_required()} - set(outputs)
        if extra or missing:
            raise ValueError(
                f'the workflow of {spec_id} returned {sorted(outputs)}: '
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
            self._has_read(finished_id)
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
            for record_id in () if caller is None else caller.kept:
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

        A released record's outputs are dropped once no pending record is yet
        to read them. A released stage takes no more requests, and those made
        through it still run through it. A released accumulator takes no more
        pushes or snapshots, and its combined value is dropped. Releasing what
        the client does not keep does nothing.
        """
        with self._changed:
            caller = self._client(client)
            for i in ids:
                caller.stages.pop(i, None)
                caller.accumulators.pop(i, None)
                if i in caller.kept:
                    caller.kept.remove(i)
                    self._drop(i, *self._views.records[i].outputs)

    def open_stage(self, spec_id: SpecId, blanks: tuple[str, ...], client: str) -> str:
        """A stage of the client; its first call stages the binding."""
        stage_id = uuid.uuid4().hex
        with self._changed:
            caller = self._client(client)
            unknown = set(blanks) - set(self.spec(spec_id).params.model_fields)
            if unknown:
                raise SubmitError(f'{sorted(unknown)}: not parameters of {spec_id}')
            caller.stages[stage_id] = _Stage(spec_id, blanks)
        return stage_id

    def open_accumulator(self, spec_id: SpecId, client: str) -> str:
        """
        An accumulator of the client, with nothing pushed.

        Its binding must make element accumulators; a plain request over a
        table works with any binding.
        """
        accumulator_id = uuid.uuid4().hex
        with self._changed:
            caller = self._client(client)
            if element_table(self.spec(spec_id)) is None:
                raise SubmitError(f'{spec_id} does not take one table')
            binding = self._bindings[spec_id]
            if not isinstance(binding, AccumulatorBinding):
                raise SubmitError(
                    f'{spec_id} is bound to code that cannot accumulate; '
                    'submit the request over a table instead'
                )
            caller.accumulators[accumulator_id] = _Held(spec_id, binding.accumulator())
        return accumulator_id

    def push(self, accumulator_id: str, element: Element, client: str) -> None:
        """
        Push an element, checked as a request over it alone would be.

        The push waits for the records the element references to finish, so
        whether it is refused does not depend on timing; they must have
        completed. The element is
        combined before its event is appended, under the accumulator's lock,
        so the pushes into one accumulator are logged in the order they were
        combined. If combining fails, the push is refused and the accumulator
        stops, since the binding may hold part of the element.
        """
        held, filled, values = self._pushable(accumulator_id, element, client)
        with held.lock:
            with self._changed:
                self._accumulator(self._client(client), accumulator_id)
                position = len(self._views.elements.get(accumulator_id, ()))
            try:
                held.accumulator.push(values)
                value = held.accumulator.value
            except Exception as error:  # the binding may hold part of the element
                reason = str(error) or repr(error)
                failure = f'element {position} failed to combine: {reason}'
                with self._changed:
                    held.stopped = f'the accumulator stopped: {failure}'
                raise SubmitError(failure) from error
            with self._changed:
                self._accumulator(self._client(client), accumulator_id)
                self._append(Pushed(accumulator=accumulator_id, element=filled))
                held.value = value

    def _pushable(
        self, accumulator_id: str, element: Element, client: str
    ) -> tuple[_Held, Element, dict[str, Any]]:
        """
        The accumulator, the element, and its values read, once checked.

        The element is checked and read as the request over it alone is, once
        the records it references have finished, and comes back as that
        request holds it: names resolved and defaults filled in.
        """
        ids = [ref.record for ref in output_refs(element)]
        with self._changed:
            caller = self._client(client)
            held = self._accumulator(caller, accumulator_id)
            self._changed.wait_for(lambda: not self._pending(ids, caller.proposal))
        table = element_table(self._specs[held.spec])
        assert table is not None  # noqa: S101
        name, _ = table
        entry = Entry(Request(held.spec, {name: [element]}))
        request = self._prepare(entry, caller.proposal)
        with self._changed:
            self._check_reads(entry, request, caller.proposal)
        try:
            values = self._read(request.params)
        except LookupError as error:  # an output released since the check
            raise SubmitError(str(error)) from None
        ((filled,), (read,)) = request.params[name], values[name]
        return held, filled, read

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

    def inputs(self, record_id: str, client: str) -> list[OutputRef]:
        """What a record read: a request's references, or a snapshot's elements."""
        with self._changed:
            record = self._mine(record_id, self._client(client).proposal)
            if isinstance(record.submitted, Request):
                return record.submitted.inputs()
            snapshot = record.submitted
            elements = self._views.elements.get(snapshot.accumulator, [])
            if len(elements) < snapshot.upto:
                raise LookupError(
                    f'the log lacks elements of accumulator {snapshot.accumulator} '
                    f'that record {record_id} covers'
                )
            return [ref for e in elements[: snapshot.upto] for ref in output_refs(e)]

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
