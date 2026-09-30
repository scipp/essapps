# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The backend checks requests, keeps records, and runs requests.

What the backend knows is its log (see ``log.py``): every change it accepts is
appended as one event and then applied to its views (see ``views.py``). A
change is checked before its event is appended, so the log holds only events
that apply. A backend given a log that already has events applies them first,
closes the sessions left open, and runs the records left pending, without
their stages; a snapshot left pending fails, since its accumulator is gone.

Output values, what stages and accumulators hold, and what waits for what are
not history. This backend keeps them in memory, so a backend started from an
existing log cannot read the outputs of the backend that wrote it.

A request waits until every record it references has completed; this is the
only scheduling there is.

A request runs through its spec's binding. A request through a stage in a
session runs through the callable the binding returned for the stage's
first call, so a binding that holds values computes only what depends on
the blanks. A stage lives until its session ends and the requests made
through it have run.

An accumulator takes only elements whose records have completed. A push
combines the element into the value the accumulator holds, and a snapshot's
record completes at submission with that value. A snapshot's record names the
accumulator and how many elements it covers, so it costs the same however
many elements that is.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ess.reduce.spec import DataField, DatasetRef, OutputRef, WorkflowSpec, data_fields
from pydantic import ValidationError

from .accumulators import AccumulatorSpec
from .bindings import (
    AccumulatorBinding,
    Binding,
    ElementAccumulator,
    Function,
    as_binding,
)
from .datasets import DatasetSource
from .log import (
    AccumulatorOpened,
    Event,
    Finished,
    Log,
    NewRecord,
    Pushed,
    SessionClosed,
    SessionOpened,
    StageOpened,
    Submitted,
)
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
from .views import AccumulatorView, Views


def _agree(output: DataField, param: DataField) -> bool:
    """Whether an output field fulfils a params field."""
    if output.format != param.format:
        return False
    return output.array is None or param.array is None or output.array == param.array


@dataclass(frozen=True)
class Entry:
    """
    A request of a submission or a snapshot of an accumulator, and its label and member.

    ``name`` says where the request came from in the call, a key or an index,
    and prefixes the reasons it is refused. A reference to the record ``@<i>``
    names the record the i-th entry becomes. ``stage`` names the stage the
    request goes through, if any. An entry with ``accumulator`` and no request
    is a snapshot of that accumulator over the elements pushed so far.
    """

    request: Request | None
    label: str | None = None
    member: str | None = None
    name: str | None = None
    stage: str | None = None
    accumulator: str | None = None

    def refused(self, error: SubmitError) -> SubmitError:
        return SubmitError(f'{self.name}: {error}' if self.name else str(error))


class _Stage:
    """
    The binding's callable for a stage, and the fixed values it was staged with.

    The first call stages the binding. A call with other fixed values, such as
    a dataset name that now resolves to another dataset, stages it again.
    """

    def __init__(self, blanks: tuple[str, ...]) -> None:
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
    What an accumulator holds, and the lock that orders its pushes.

    A push combines its element into ``accumulator``, the binding's, under
    ``lock`` and outside the backend's lock. ``value`` is the combined value of
    the elements in the accumulator's view; it changes under the backend's lock
    together with them, so a snapshot reads the two as one. ``stopped`` says
    why the accumulator takes no more pushes or snapshots.
    """

    def __init__(self, accumulator: ElementAccumulator) -> None:
        self.lock = threading.Lock()
        self.accumulator = accumulator
        self.value: Mapping[str, Any] | None = None
        self.stopped: str | None = None


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
        self._outputs: dict[tuple[str, str], Any] = {}
        self._waiting: dict[str, set[str]] = {}
        self._dependents: dict[str, set[str]] = {}
        self._staged: dict[str, _Stage] = {}  # the stages of open sessions
        self._stage_of: dict[str, _Stage] = {}  # unfinished record ID to its stage
        self._held: dict[str, _Held] = {}  # the accumulators of open sessions
        self._on_finished: dict[str, list[Callable[[Record], None]]] = {}
        with self._changed:
            for event in self._log:
                self._views.apply(event)
            for session in list(self._views.sessions):
                self._append(SessionClosed(session=session))
            for record in list(self._views.records.values()):
                if record.status.finished:
                    continue
                if isinstance(record.submitted, Snapshot):
                    self._finish(
                        record.id, Status.FAILED, 'the accumulator ended at a restart'
                    )
                else:
                    self._schedule(record.id)

    def close(self) -> None:
        self._executor.shutdown(wait=True)

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

    def submit(
        self, entries: list[Entry], *, proposal: str, submitter: str
    ) -> list[Record]:
        """
        Check every request, then create a record for each.

        If one request is refused, none is submitted. Dataset names are
        resolved before the backend's lock is taken. The records are pending,
        except a snapshot of an accumulator that holds its combined value,
        which completes with that value.
        """
        ids = [uuid.uuid4().hex for _ in entries]
        prepared = [
            None if e.accumulator is not None else self._prepare(e, ids, proposal)
            for e in entries
        ]
        with self._changed:
            submitted: list[Request | Snapshot] = [
                self._snapshot_of(e, proposal) if request is None else request
                for e, request in zip(entries, prepared, strict=True)
            ]
            specs = {i: r.spec for i, r in zip(ids, submitted, strict=True)}
            for entry, request in zip(entries, submitted, strict=True):
                if isinstance(request, Request):
                    self._check_reads(entry, request, specs, proposal)
                    self._check_stage(entry, request, proposal)
            self._append(
                Submitted(
                    time=self._clock(),
                    proposal=proposal,
                    submitter=submitter,
                    records=tuple(
                        NewRecord(
                            id=record_id,
                            submitted=request,
                            outputs=tuple(
                                self._specs[request.spec].outputs.model_fields
                            ),
                            label=entry.label,
                            member=entry.member,
                            stage=entry.stage,
                        )
                        for record_id, entry, request in zip(
                            ids, entries, submitted, strict=True
                        )
                    ),
                )
            )
            for record_id, entry in zip(ids, entries, strict=True):
                if entry.stage is not None:
                    self._stage_of[record_id] = self._staged[entry.stage]
            for record_id, entry in zip(ids, entries, strict=True):
                if entry.accumulator is None:
                    self._schedule(record_id)
                else:
                    value = self._held[entry.accumulator].value
                    assert value is not None  # noqa: S101 - an empty one was refused
                    self._complete(record_id, dict(value))
            return [self._views.records[i] for i in ids]

    def _prepare(self, entry: Entry, ids: list[str], proposal: str) -> Request:
        """The request with names resolved and defaults filled; needs no lock."""
        request = entry.request
        assert request is not None  # noqa: S101 - snapshots are made under the lock
        try:
            spec = self.spec(request.spec)
            unknown = set(request.params) - set(spec.params.model_fields)
            if unknown:
                raise SubmitError(
                    f'{sorted(unknown)}: not parameters of {request.spec}'
                )
            params = {
                field: map_refs(value, self._resolver(field, ids, proposal))
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
        values = {f: getattr(model, f) for f in type(model).model_fields}
        return Request(request.spec, values)

    def _resolver(
        self, field: str, ids: list[str], proposal: str
    ) -> Callable[[OutputRef | DatasetRef], OutputRef | DatasetRef]:
        def resolve(ref: OutputRef | DatasetRef) -> OutputRef | DatasetRef:
            if isinstance(ref, DatasetRef):
                try:
                    identity = self._datasets.resolve(ref)
                except KeyError:
                    raise SubmitError(f'{field}: unknown dataset {ref}') from None
                owner = self._datasets.metadata(identity).get('proposal', proposal)
                if owner != proposal:
                    raise SubmitError(
                        f'{field}: dataset {ref} belongs to proposal {owner}'
                    )
                return identity
            if ref.key is not None:
                raise SubmitError(f'{field}: {ref} names an element of an output')
            if not ref.record.startswith('@'):
                return ref
            index = ref.record[1:]
            if not index.isdigit() or int(index) >= len(ids):
                raise SubmitError(f'{field}: {ref} names a request not in this call')
            return OutputRef(record=ids[int(index)], output=ref.output)

        return resolve

    def _check_reads(
        self,
        entry: Entry,
        request: Request,
        submitted: Mapping[str, SpecId],
        proposal: str,
    ) -> None:
        """Check the outputs a request reads; lock held."""
        param_fields = data_fields(self._specs[request.spec].params)
        try:
            for field, value in request.params.items():
                for ref in output_refs(value):
                    spec_id = submitted.get(ref.record) or self._readable(
                        ref, field, proposal
                    )
                    outputs = self._output_fields[spec_id]
                    if ref.output not in outputs:
                        raise SubmitError(
                            f'{field}: {spec_id} has no output {ref.output!r}'
                        )
                    target = param_fields.get(field)
                    if target is not None and not _agree(outputs[ref.output], target):
                        raise SubmitError(f'{field}: {ref} does not fit the field')
        except SubmitError as error:
            raise entry.refused(error) from None

    def _check_stage(self, entry: Entry, request: Request, proposal: str) -> None:
        """Check the stage a request goes through; lock held."""
        if entry.stage is None:
            return
        stage = self._views.stages.get(entry.stage)
        if stage is None or stage.proposal != proposal:
            raise entry.refused(SubmitError('the stage has ended or is unknown'))
        if stage.spec != request.spec:
            raise entry.refused(
                SubmitError(f'the stage holds {stage.spec}, not {request.spec}')
            )

    def _snapshot_of(self, entry: Entry, proposal: str) -> Snapshot:
        """A snapshot of the entry's accumulator over the elements so far; lock held."""
        assert entry.accumulator is not None  # noqa: S101
        accumulator = self._open_accumulator(entry.accumulator, proposal)
        upto = len(accumulator.elements)
        if upto == 0:
            raise entry.refused(SubmitError('nothing has been pushed'))
        return Snapshot(spec=accumulator.spec, accumulator=entry.accumulator, upto=upto)

    def _readable(self, ref: OutputRef, field: str, proposal: str) -> SpecId:
        """The spec of a record a request may read; lock held."""
        record = self._views.records.get(ref.record)
        if record is None:
            raise SubmitError(f'{field}: unknown record {ref.record}')
        if record.proposal != proposal:
            raise SubmitError(
                f'{field}: record {ref.record} belongs to proposal {record.proposal}'
            )
        if record.status in (Status.FAILED, Status.CANCELLED):
            raise SubmitError(f'{field}: record {ref.record} {record.status}')
        return record.spec

    # Execution

    def _schedule(self, record_id: str) -> None:
        """Start the record, or let it wait for its unfinished inputs; lock held."""
        record = self._views.records[record_id]
        inputs = {ref.record for ref in record.request.refs()}
        waiting = {i for i in inputs if not self._views.records[i].status.finished}
        if not waiting:
            self._executor.submit(self._run, record_id)
            return
        self._waiting[record_id] = waiting
        for input_id in waiting:
            self._dependents.setdefault(input_id, set()).add(record_id)

    def _run(self, record_id: str) -> None:
        with self._changed:
            record = self._views.records[record_id]
            if record.status.finished:
                return
            stage = self._stage_of.get(record_id)
        try:
            outputs = dict(self._compute(record.request, stage))
        except Exception as error:  # any failure of a workflow is recorded
            with self._changed:
                self._finish(record_id, Status.FAILED, str(error) or repr(error))
            return
        with self._changed:
            self._complete(record_id, outputs)

    def _compute(self, request: Request, stage: _Stage | None) -> Mapping[str, Any]:
        """The outputs of a request, through its stage if it has one."""
        binding = self._bindings[request.spec]
        values = self._typed(request)
        if stage is None:
            return binding.stage(self._read(values), ())()
        fixed = {k: v for k, v in values.items() if k not in stage.blanks}
        call = stage.staged(
            fixed, lambda: binding.stage(self._read(fixed), stage.blanks)
        )
        return call(**self._read({k: values[k] for k in stage.blanks}))

    def _typed(self, request: Request) -> dict[str, Any]:
        """
        The request's values as its spec's params model gives them.

        A record holds its values as the log does, in JSON's types; a binding
        gets the types the params model declares, such as a tuple.
        """
        model = self._specs[request.spec].params.model_validate(request.params)
        return {f: getattr(model, f) for f in type(model).model_fields}

    def _read(self, values: dict[str, Any]) -> dict[str, Any]:
        """``values`` with the outputs and datasets they reference read."""
        with self._changed:
            values = map_refs(values, self._read_output)
        return map_refs(values, self._datasets.read)

    def _read_output(self, ref: OutputRef | DatasetRef) -> Any:
        """The value of an output; a dataset is read later, outside the lock."""
        if isinstance(ref, DatasetRef):
            return ref
        try:
            return self._outputs[(ref.record, ref.output)]
        except KeyError:
            raise LookupError(
                f'record {ref.record} has no output {ref.output}'
            ) from None

    def _complete(self, record_id: str, outputs: dict[str, Any]) -> None:
        """Complete a record with outputs, or fail it if they do not fit; lock held."""
        record = self._views.records[record_id]
        if record.status.finished:  # cancelled meanwhile
            return
        try:
            self._check_returned(record.spec, outputs)
        except ValueError as error:
            self._finish(record_id, Status.FAILED, str(error))
            return
        for name, value in outputs.items():
            self._outputs[(record_id, name)] = value
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
            if self._views.records[finished_id].status.finished:
                continue
            self._append(Finished(record=finished_id, status=finished, failure=message))
            for call in self._on_finished.pop(finished_id, ()):
                call(self._views.records[finished_id])
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

    # Sessions

    def open_session(self, proposal: str) -> str:
        session = uuid.uuid4().hex
        with self._changed:
            self._append(SessionOpened(session=session, proposal=proposal))
        return session

    def open_stage(
        self, session: str, spec_id: SpecId, blanks: tuple[str, ...], proposal: str
    ) -> str:
        """A stage in the session; its first call stages the binding."""
        unknown = set(blanks) - set(self.spec(spec_id).params.model_fields)
        if unknown:
            raise SubmitError(f'{sorted(unknown)}: not parameters of {spec_id}')
        stage_id = uuid.uuid4().hex
        with self._changed:
            self._check_session(session, proposal)
            self._append(
                StageOpened(
                    stage=stage_id, session=session, spec=spec_id, blanks=blanks
                )
            )
            self._staged[stage_id] = _Stage(blanks)
        return stage_id

    def open_accumulator(self, session: str, spec_id: SpecId, proposal: str) -> str:
        """
        An accumulator in the session, with nothing pushed.

        Its binding must make element accumulators; a plain request over a
        list works with any binding.
        """
        if not isinstance(self.spec(spec_id), AccumulatorSpec):
            raise SubmitError(f'{spec_id} is not an accumulator spec')
        binding = self._bindings[spec_id]
        if not isinstance(binding, AccumulatorBinding):
            raise SubmitError(
                f'{spec_id} is bound to code that cannot accumulate; '
                'submit the request over a list instead'
            )
        held = _Held(binding.accumulator())
        accumulator_id = uuid.uuid4().hex
        with self._changed:
            self._check_session(session, proposal)
            self._append(
                AccumulatorOpened(
                    accumulator=accumulator_id, session=session, spec=spec_id
                )
            )
            self._held[accumulator_id] = held
        return accumulator_id

    def _check_session(self, session: str, proposal: str) -> None:
        if self._views.sessions.get(session) != proposal:
            raise SubmitError('the session has ended or is unknown')

    def push(self, accumulator_id: str, element: Element, proposal: str) -> None:
        """
        Push an element, checked as a request over it alone would be.

        The records it references must have completed. The element is
        combined before its event is appended, under the accumulator's lock,
        so the pushes into one accumulator are logged in the order they were
        combined. If combining fails, the push is refused and the accumulator
        stops, since the binding may hold part of the element.
        """
        with self._changed:
            self._open_accumulator(accumulator_id, proposal)
            held = self._held[accumulator_id]
        with held.lock:
            with self._changed:
                position, values = self._pushable(accumulator_id, element, proposal)
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
                self._open_accumulator(accumulator_id, proposal)
                self._append(Pushed(accumulator=accumulator_id, element=element))
                held.value = value

    def _pushable(
        self, accumulator_id: str, element: Element, proposal: str
    ) -> tuple[int, dict[str, Any]]:
        """The position of a push and the element's values, once checked; lock held."""
        accumulator = self._open_accumulator(accumulator_id, proposal)
        spec = self._specs[accumulator.spec]
        assert isinstance(spec, AccumulatorSpec)  # noqa: S101
        if set(element) != set(spec.element.model_fields):
            raise SubmitError(
                f'an element has the fields {tuple(spec.element.model_fields)}'
            )
        position = len(accumulator.elements)
        request = Request(accumulator.spec, {f: [ref] for f, ref in element.items()})
        entry = Entry(request, name=f'element {position}')
        self._check_reads(entry, request, {}, proposal)
        for ref in element.values():
            if self._views.records[ref.record].status is Status.PENDING:
                raise entry.refused(
                    SubmitError(
                        f'record {ref.record} is still pending; '
                        'push it once it has completed'
                    )
                )
        try:
            return position, {f: self._read_output(ref) for f, ref in element.items()}
        except LookupError as error:  # its value is no longer kept
            raise entry.refused(SubmitError(str(error))) from None

    def _open_accumulator(self, accumulator_id: str, proposal: str) -> AccumulatorView:
        accumulator = self._views.accumulators.get(accumulator_id)
        if (
            accumulator is None
            or not accumulator.open
            or accumulator.proposal != proposal
        ):
            raise SubmitError('the accumulator has ended or is unknown')
        stopped = self._held[accumulator_id].stopped
        if stopped is not None:
            raise SubmitError(stopped)
        return accumulator

    def close_session(self, session: str) -> None:
        """
        End the session: its holders take no more requests.

        A stage is released once the requests made through it have run, and
        what an accumulator holds is released now.
        """
        with self._changed:
            if session not in self._views.sessions:
                return
            stages = [i for i, s in self._views.stages.items() if s.session == session]
            accumulators = [
                i for i, a in self._views.accumulators.items() if a.session == session
            ]
            self._append(SessionClosed(session=session))
            for stage_id in stages:
                del self._staged[stage_id]
            for accumulator_id in accumulators:
                del self._held[accumulator_id]

    # Queries and control, within one proposal

    def _mine(self, record_id: str, proposal: str) -> Record:
        record = self._views.records.get(record_id)
        if record is None or record.proposal != proposal:
            raise KeyError(f'no record {record_id} in proposal {proposal}')
        return record

    def wait(self, ids: Iterable[str], proposal: str) -> list[Record]:
        ids = list(ids)
        with self._changed:
            for record_id in ids:
                self._mine(record_id, proposal)
            records = self._views.records
            self._changed.wait_for(lambda: all(records[i].status.finished for i in ids))
            return [records[i] for i in ids]

    def when_finished(
        self, record_id: str, proposal: str, call: Callable[[Record], None]
    ) -> None:
        """
        Call ``call`` with the record once it has finished, or now if it has.

        ``call`` runs under the backend's lock, so it must return at once,
        raise nothing, and not call the backend, as ``queue.SimpleQueue.put``.
        """
        with self._changed:
            record = self._mine(record_id, proposal)
            if record.status.finished:
                call(record)
            else:
                self._on_finished.setdefault(record_id, []).append(call)

    def cancel(self, ids: Iterable[str], proposal: str) -> None:
        """Cancel the unfinished records; a running workflow's outputs are dropped."""
        with self._changed:
            for record_id in ids:
                self._mine(record_id, proposal)
                self._finish(record_id, Status.CANCELLED)

    def record(self, record_id: str, proposal: str) -> Record:
        with self._changed:
            return self._mine(record_id, proposal)

    def inputs(self, record_id: str, proposal: str) -> list[OutputRef]:
        """What a record read: a request's references, or a snapshot's elements."""
        with self._changed:
            record = self._mine(record_id, proposal)
            if isinstance(record.submitted, Request):
                return record.submitted.refs()
            snapshot = record.submitted
            elements = self._views.accumulators[snapshot.accumulator].elements
            return [ref for e in elements[: snapshot.upto] for ref in e.values()]

    def records(self, proposal: str, label: str | None = None) -> list[Record]:
        """The records of a proposal, oldest first, under ``label`` if given."""
        with self._changed:
            records = self._views.records
            if label is not None:
                return [
                    records[i] for i in self._views.labels.get((proposal, label), [])
                ]
            return [r for r in records.values() if r.proposal == proposal]

    def output(self, record_id: str, name: str, proposal: str) -> Any:
        with self._changed:
            self._mine(record_id, proposal)
            return self._read_output(OutputRef(record=record_id, output=name))
