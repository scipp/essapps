# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The backend checks requests, keeps records, and runs requests.

This backend runs in the client's process and keeps records and outputs in
memory. A request waits until every record it references has completed; this
is the only scheduling there is.

A request runs through its spec's binding. A request through a stage in a
session runs through the callable the binding returned for the stage's
first call, so it computes only what depends on the blanks.
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

from .bindings import Binding, Function, as_binding
from .datasets import DatasetSource
from .records import (
    Failure,
    Record,
    Request,
    SpecId,
    Status,
    SubmitError,
    map_refs,
    output_refs,
)


def _agree(output: DataField, param: DataField) -> bool:
    """Whether an output field fulfils a params field."""
    if output.format != param.format:
        return False
    return output.array is None or param.array is None or output.array == param.array


@dataclass(frozen=True)
class Entry:
    """
    A request of a submission, with its label and member.

    ``name`` says where the request came from in the call, a key or an index,
    and prefixes the reasons it is refused. A reference to the record ``@<i>``
    names the record the i-th entry becomes. ``holder`` names the held stage
    the request goes through, if any.
    """

    request: Request
    label: str | None = None
    member: str | None = None
    name: str | None = None
    holder: str | None = None

    def refused(self, error: SubmitError) -> SubmitError:
        return SubmitError(f'{self.name}: {error}' if self.name else str(error))


class _Held:
    """
    A stage held in a session: the binding's callable and the fixed values it has.

    The first call stages it. A call with other fixed values, such as a dataset
    name that now resolves to another dataset, stages it again.
    """

    def __init__(self, blanks: tuple[str, ...]) -> None:
        self.blanks = blanks
        self._lock = threading.Lock()
        self._fixed: dict[str, Any] | None = None
        self._call: Function | None = None

    def call(self, fixed: dict[str, Any], stage: Callable[[], Function]) -> Function:
        with self._lock:
            if self._call is None or fixed != self._fixed:
                self._call, self._fixed = stage(), fixed
            return self._call


class Backend:
    def __init__(
        self,
        datasets: DatasetSource,
        bind: Mapping[WorkflowSpec, Binding | Function],
        *,
        workers: int = 4,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
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
        self._records: dict[str, Record] = {}
        self._outputs: dict[tuple[str, str], Any] = {}
        self._waiting: dict[str, set[str]] = {}
        self._dependents: dict[str, set[str]] = {}
        self._sessions: dict[str, set[str]] = {}
        self._held: dict[str, _Held] = {}
        self._through: dict[str, str] = {}  # record ID to holder ID

    def close(self) -> None:
        self._executor.shutdown(wait=True)

    def spec(self, spec_id: SpecId) -> WorkflowSpec:
        try:
            return self._specs[spec_id]
        except KeyError:
            raise SubmitError(f'unknown spec {spec_id}') from None

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
        prepared = [self._prepare(e, ids, proposal) for e in entries]
        submitted = {i: r.spec for i, r in zip(ids, prepared, strict=True)}
        with self._changed:
            for entry, request in zip(entries, prepared, strict=True):
                self._check_reads(entry, request, submitted, proposal)
            records = [
                Record(
                    id=record_id,
                    request=request,
                    proposal=proposal,
                    submitter=submitter,
                    created=self._clock(),
                    outputs=tuple(self._specs[request.spec].outputs.model_fields),
                    label=entry.label,
                    member=entry.member,
                )
                for record_id, entry, request in zip(
                    ids, entries, prepared, strict=True
                )
            ]
            self._records.update((r.id, r) for r in records)
            self._through.update(
                (r.id, e.holder)
                for r, e in zip(records, entries, strict=True)
                if e.holder is not None
            )
            for record in records:
                self._schedule(record.id)
            return [self._records[r.id] for r in records]

    def _prepare(self, entry: Entry, ids: list[str], proposal: str) -> Request:
        """The request with names resolved and defaults filled; needs no lock."""
        request = entry.request
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

    def _readable(self, ref: OutputRef, field: str, proposal: str) -> SpecId:
        """The spec of a record a request may read; lock held."""
        record = self._records.get(ref.record)
        if record is None:
            raise SubmitError(f'{field}: unknown record {ref.record}')
        if record.proposal != proposal:
            raise SubmitError(
                f'{field}: record {ref.record} belongs to proposal {record.proposal}'
            )
        if record.status in (Status.FAILED, Status.CANCELLED):
            raise SubmitError(f'{field}: record {ref.record} {record.status}')
        return record.request.spec

    # Execution

    def _schedule(self, record_id: str) -> None:
        """Start the record, or let it wait for its unfinished inputs; lock held."""
        inputs = {ref.record for ref in self._records[record_id].request.refs()}
        waiting = {i for i in inputs if not self._records[i].status.finished}
        if not waiting:
            self._executor.submit(self._run, record_id)
            return
        self._waiting[record_id] = waiting
        for input_id in waiting:
            self._dependents.setdefault(input_id, set()).add(record_id)

    def _run(self, record_id: str) -> None:
        try:
            with self._changed:
                request = self._records[record_id].request
                if self._records[record_id].status.finished:
                    return
                holder = self._through.get(record_id)
                held = None if holder is None else self._held.get(holder)
            outputs = dict(self._compute(request, held))
            self._check_returned(request.spec, outputs)
        except Exception as error:  # any failure of a workflow is recorded
            with self._changed:
                self._finish(record_id, Status.FAILED, str(error) or repr(error))
            return
        with self._changed:
            if self._records[record_id].status.finished:  # cancelled while running
                return
            for name, value in outputs.items():
                self._outputs[(record_id, name)] = value
            self._finish(record_id, Status.COMPLETED)

    def _compute(self, request: Request, held: _Held | None) -> Mapping[str, Any]:
        """The outputs of a request, through a held stage if there is one."""
        binding = self._bindings[request.spec]
        if held is None:  # also a request whose session has ended
            return binding.stage(self._read(request.params), ())()
        fixed = {k: v for k, v in request.params.items() if k not in held.blanks}
        call = held.call(fixed, lambda: binding.stage(self._read(fixed), held.blanks))
        return call(**self._read({k: request.params[k] for k in held.blanks}))

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
            finished_id, finished, reason = todo.pop()
            record = self._records[finished_id]
            if record.status.finished:
                continue
            self._records[finished_id] = record.model_copy(
                update={
                    'status': finished,
                    'failure': None if reason is None else Failure(message=reason),
                }
            )
            self._waiting.pop(finished_id, None)
            self._through.pop(finished_id, None)
            for dependent in self._dependents.pop(finished_id, set()):
                inputs = self._waiting.get(dependent)
                if inputs is None:  # finished meanwhile
                    continue
                if finished is not Status.COMPLETED:
                    reason = f'input {finished_id} {finished}'
                    todo.append((dependent, Status.FAILED, reason))
                    continue
                inputs.discard(finished_id)
                if not inputs:
                    del self._waiting[dependent]
                    self._executor.submit(self._run, dependent)
        self._changed.notify_all()

    # Sessions

    def open_session(self) -> str:
        session = uuid.uuid4().hex
        with self._changed:
            self._sessions[session] = set()
        return session

    def hold(self, session: str, blanks: tuple[str, ...]) -> str:
        """Hold a stage with ``blanks`` in the session; its first call stages it."""
        holder = uuid.uuid4().hex
        with self._changed:
            self._sessions[session].add(holder)
            self._held[holder] = _Held(blanks)
        return holder

    def close_session(self, session: str) -> None:
        """Release the session's holders; requests through them run in full."""
        with self._changed:
            for holder in self._sessions.pop(session):
                del self._held[holder]

    # Queries and control, within one proposal

    def _mine(self, record_id: str, proposal: str) -> Record:
        record = self._records.get(record_id)
        if record is None or record.proposal != proposal:
            raise KeyError(f'no record {record_id} in proposal {proposal}')
        return record

    def wait(self, ids: Iterable[str], proposal: str) -> list[Record]:
        ids = list(ids)
        with self._changed:
            for record_id in ids:
                self._mine(record_id, proposal)
            self._changed.wait_for(
                lambda: all(self._records[i].status.finished for i in ids)
            )
            return [self._records[i] for i in ids]

    def cancel(self, ids: Iterable[str], proposal: str) -> None:
        """Cancel the unfinished records; a running workflow's outputs are dropped."""
        with self._changed:
            for record_id in ids:
                self._mine(record_id, proposal)
                self._finish(record_id, Status.CANCELLED)

    def record(self, record_id: str, proposal: str) -> Record:
        with self._changed:
            return self._mine(record_id, proposal)

    def records(self, proposal: str) -> list[Record]:
        """Every record of a proposal, oldest first."""
        with self._changed:
            return [r for r in self._records.values() if r.proposal == proposal]

    def output(self, record_id: str, name: str, proposal: str) -> Any:
        with self._changed:
            self._mine(record_id, proposal)
            return self._read_output(OutputRef(record=record_id, output=name))
