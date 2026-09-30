# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The backend checks requests, keeps records, and runs requests.

What the backend knows is its log (see ``log.py``): every change it accepts is
appended as one event and then applied to its views (see ``views.py``). A
change is checked before its event is appended, so the log holds only events
that apply. A backend given a log that already has events applies them first,
closes the sessions left open, and runs the records left pending, without
their stages.

Output values, what stages and accumulators hold, and what waits for what are
not history. This backend keeps them in memory, so a backend started from an
existing log cannot read the outputs of the backend that wrote it.

A request waits until every record it references has completed; this is the
only scheduling there is. A read of an accumulator waits until the records of
its elements have completed.

A request runs through its spec's binding. A request through a stage in a
session runs through the callable the binding returned for the stage's
first call, so a binding that holds values computes only what depends on
the blanks. A stage lives until its session ends and the requests made
through it have run.

An accumulator whose binding can accumulate keeps the combined value of its
elements, so a read combines only the elements pushed since the previous
read. A read's record names the accumulator and how many elements it covers,
so it costs the same however many elements that is.
"""

from __future__ import annotations

import threading
import uuid
from collections import deque
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
    Read,
    Record,
    Request,
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
    A request of a submission, or a read of an accumulator, with its label and member.

    ``name`` says where the request came from in the call, a key or an index,
    and prefixes the reasons it is refused. A reference to the record ``@<i>``
    names the record the i-th entry becomes. ``stage`` names the stage the
    request goes through, if any. An entry with ``accumulator`` and no request
    reads that accumulator over the elements pushed so far.
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


class _Reads:
    """
    The reads of one accumulator that wait or can run, and the value it holds.

    Reads wait in the order of ``upto``, which is the order of submission.
    Those that can run run one at a time and in order, since each moves the
    held value forward from where the previous one left it. Only the one
    running read touches the held value.
    """

    def __init__(self) -> None:
        self.waiting: deque[tuple[int, str]] = deque()
        self.runnable: deque[str] = deque()
        self.running = False
        self.held: ElementAccumulator | None = None
        self.held_upto = 0

    @property
    def idle(self) -> bool:
        return not (self.waiting or self.runnable or self.running)


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
        self._reads: dict[str, _Reads] = {}  # accumulator ID to its reads
        with self._changed:
            for event in self._log:
                self._views.apply(event)
            for session in list(self._views.sessions):
                self._append(SessionClosed(session=session))
            for record in list(self._views.records.values()):
                if not record.status.finished:
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
        Check every request, then create a pending record for each.

        If one request is refused, none is submitted. Dataset names are
        resolved before the backend's lock is taken.
        """
        ids = [uuid.uuid4().hex for _ in entries]
        prepared = [
            None if e.accumulator is not None else self._prepare(e, ids, proposal)
            for e in entries
        ]
        with self._changed:
            submitted: list[Request | Read] = [
                self._read_of(e, proposal) if request is None else request
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
            for record_id in ids:
                self._schedule(record_id)
            return [self._views.records[i] for i in ids]

    def _prepare(self, entry: Entry, ids: list[str], proposal: str) -> Request:
        """The request with names resolved and defaults filled; needs no lock."""
        request = entry.request
        assert request is not None  # noqa: S101 - reads are prepared under the lock
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

    def _read_of(self, entry: Entry, proposal: str) -> Read:
        """A read of the entry's accumulator over the elements so far; lock held."""
        assert entry.accumulator is not None  # noqa: S101
        accumulator = self._open_accumulator(entry.accumulator, proposal)
        upto = len(accumulator.elements)
        if upto == 0:
            raise entry.refused(SubmitError('nothing has been pushed'))
        if accumulator.failed is not None:
            raise entry.refused(
                SubmitError(f'element {accumulator.failed} did not complete')
            )
        return Read(spec=accumulator.spec, accumulator=entry.accumulator, upto=upto)

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
        if isinstance(record.submitted, Read):
            accumulator_id = record.submitted.accumulator
            reads = self._reads.setdefault(accumulator_id, _Reads())
            reads.waiting.append((record.submitted.upto, record_id))
            failed = self._views.accumulators[accumulator_id].failed
            for read_id in self._release(accumulator_id):
                self._finish(
                    read_id, Status.FAILED, f'element {failed} did not complete'
                )
            return
        inputs = {ref.record for ref in record.request.refs()}
        waiting = {i for i in inputs if not self._views.records[i].status.finished}
        if not waiting:
            self._executor.submit(self._run, record_id)
            return
        self._waiting[record_id] = waiting
        for input_id in waiting:
            self._dependents.setdefault(input_id, set()).add(record_id)

    def _release(self, accumulator_id: str) -> list[str]:
        """
        Start the reads of an accumulator that can run; lock held.

        Returns the waiting reads that never can, since an element they cover
        did not complete.
        """
        reads = self._reads.get(accumulator_id)
        if reads is None:
            return []
        accumulator = self._views.accumulators[accumulator_id]
        while reads.waiting and reads.waiting[0][0] <= accumulator.ready:
            reads.runnable.append(reads.waiting.popleft()[1])
        failing = []
        if accumulator.failed is not None:
            while reads.waiting and reads.waiting[-1][0] > accumulator.failed:
                failing.append(reads.waiting.pop()[1])
        if reads.runnable and not reads.running:
            reads.running = True
            self._executor.submit(self._run_reads, accumulator_id, reads)
        return failing

    def _run_reads(self, accumulator_id: str, reads: _Reads) -> None:
        """
        Run the runnable reads of an accumulator, one at a time and in order.

        This also runs the reads of a binding that cannot accumulate one at a
        time, which gains nothing for them but costs no more than their order.
        """
        try:
            while True:
                with self._changed:
                    if not reads.runnable:
                        reads.running = False
                        self._settle(accumulator_id)
                        return
                    read_id = reads.runnable.popleft()
                self._run(read_id)
        except BaseException:  # the log could not be written; a later release restarts
            with self._changed:
                reads.running = False
            raise

    def _settle(self, accumulator_id: str) -> None:
        """Drop what an ended accumulator holds once no read needs it; lock held."""
        reads = self._reads.get(accumulator_id)
        if (
            reads is not None
            and reads.idle
            and not self._views.accumulators[accumulator_id].open
        ):
            del self._reads[accumulator_id]

    def _run(self, record_id: str) -> None:
        try:
            with self._changed:
                record = self._views.records[record_id]
                if record.status.finished:
                    return
                stage = self._stage_of.get(record_id)
            if isinstance(record.submitted, Read):
                outputs = dict(self._combine(record, record.submitted))
            else:
                outputs = dict(self._compute(record.request, stage))
            self._check_returned(record.spec, outputs)
        except Exception as error:  # any failure of a workflow is recorded
            with self._changed:
                self._finish(record_id, Status.FAILED, str(error) or repr(error))
            return
        with self._changed:
            if self._views.records[record_id].status.finished:  # cancelled meanwhile
                return
            for name, value in outputs.items():
                self._outputs[(record_id, name)] = value
            self._finish(record_id, Status.COMPLETED)

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

    def _combine(self, record: Record, read: Read) -> Mapping[str, Any]:
        """
        The outputs of a read, from the value the accumulator holds.

        The held value moves forward over the elements pushed since the last
        read. A binding that cannot accumulate combines all elements. A read
        whose combining fails drops the held value, which may hold part of
        its elements.
        """
        binding = self._bindings[record.spec]
        if not isinstance(binding, AccumulatorBinding):
            return self._compute(record.request, None)
        reads = self._reads[read.accumulator]
        # The elements only grow, so reading them without the lock is safe.
        elements = self._views.accumulators[read.accumulator].elements
        if reads.held is None or reads.held_upto > read.upto:
            reads.held, reads.held_upto = binding.accumulator(), 0
        try:
            for element in elements[reads.held_upto : read.upto]:
                reads.held.push(self._read(dict(element)))
        except BaseException:
            reads.held = None
            raise
        reads.held_upto = read.upto
        return dict(reads.held.value)

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
            accumulators = [a for a, _ in self._views.elements_of.get(finished_id, ())]
            self._append(Finished(record=finished_id, status=finished, failure=message))
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
            for accumulator_id in accumulators:
                todo.extend(
                    (read_id, Status.FAILED, f'element input {finished_id} {finished}')
                    for read_id in self._release(accumulator_id)
                )
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
        """An accumulator in the session, with nothing pushed."""
        if not isinstance(self.spec(spec_id), AccumulatorSpec):
            raise SubmitError(f'{spec_id} is not an accumulator spec')
        accumulator_id = uuid.uuid4().hex
        with self._changed:
            self._check_session(session, proposal)
            self._append(
                AccumulatorOpened(
                    accumulator=accumulator_id, session=session, spec=spec_id
                )
            )
        return accumulator_id

    def _check_session(self, session: str, proposal: str) -> None:
        if self._views.sessions.get(session) != proposal:
            raise SubmitError('the session has ended or is unknown')

    def push(self, accumulator_id: str, element: Element, proposal: str) -> None:
        """
        Push an element, checked as a request over it alone would be.

        The records it references may still be pending.
        """
        with self._changed:
            accumulator = self._open_accumulator(accumulator_id, proposal)
            spec = self._specs[accumulator.spec]
            assert isinstance(spec, AccumulatorSpec)  # noqa: S101
            if set(element) != set(spec.element.model_fields):
                raise SubmitError(
                    f'an element has the fields {tuple(spec.element.model_fields)}'
                )
            request = Request(
                accumulator.spec, {f: [ref] for f, ref in element.items()}
            )
            entry = Entry(request, name=f'element {len(accumulator.elements)}')
            self._check_reads(entry, request, {}, proposal)
            self._append(Pushed(accumulator=accumulator_id, element=element))

    def _open_accumulator(self, accumulator_id: str, proposal: str) -> AccumulatorView:
        accumulator = self._views.accumulators.get(accumulator_id)
        if (
            accumulator is None
            or not accumulator.open
            or accumulator.proposal != proposal
        ):
            raise SubmitError('the accumulator has ended or is unknown')
        return accumulator

    def close_session(self, session: str) -> None:
        """
        End the session: its holders take no more requests.

        A stage, and the value an accumulator holds, are released once the
        requests made through them have run.
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
                self._settle(accumulator_id)

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

    def cancel(self, ids: Iterable[str], proposal: str) -> None:
        """Cancel the unfinished records; a running workflow's outputs are dropped."""
        with self._changed:
            for record_id in ids:
                self._mine(record_id, proposal)
                self._finish(record_id, Status.CANCELLED)

    def record(self, record_id: str, proposal: str) -> Record:
        with self._changed:
            return self._mine(record_id, proposal)

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
