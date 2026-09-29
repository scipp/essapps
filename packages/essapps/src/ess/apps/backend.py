# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The backend checks requests, keeps records, and runs requests.

This backend runs in the client's process and keeps records and outputs in
memory. A request waits until every record it references has completed; this
is the only scheduling there is.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from ess.reduce.spec import (
    DataField,
    DatasetRef,
    OutputRef,
    WorkflowSpec,
    as_ref,
    data_fields,
)
from pydantic import ValidationError

from .datasets import DatasetSource
from .records import Failure, Record, Request, SpecId, Status, SubmitError

Workflow = Callable[..., Mapping[str, Any]]
"""A bound workflow: takes parameter values, with data read, and returns outputs."""


def _map_refs(value: Any, fn: Callable[[Any], Any]) -> Any:
    """``value`` with every reference in it replaced by ``fn(reference)``."""
    if (ref := as_ref(value)) is not None:
        return fn(ref)
    if isinstance(value, dict):
        return {k: _map_refs(v, fn) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_map_refs(v, fn) for v in value]
    return value


def _agree(output: DataField, param: DataField) -> bool:
    """Whether an output field fulfils a params field."""
    if output.format != param.format:
        return False
    return output.array is None or param.array is None or output.array == param.array


class Backend:
    def __init__(
        self,
        datasets: DatasetSource,
        bind: Mapping[WorkflowSpec, Workflow],
        *,
        workers: int = 4,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._datasets = datasets
        self._specs = {SpecId.of(spec): spec for spec in bind}
        self._workflows = {SpecId.of(spec): fn for spec, fn in bind.items()}
        self._clock = clock
        self._executor = ThreadPoolExecutor(max_workers=workers)
        self._changed = threading.Condition()
        self._records: dict[str, Record] = {}
        self._outputs: dict[tuple[str, str], Any] = {}
        self._waiting: dict[str, set[str]] = {}
        self._started: set[str] = set()

    def close(self) -> None:
        self._executor.shutdown(wait=True)

    def spec(self, spec_id: SpecId) -> WorkflowSpec:
        try:
            return self._specs[spec_id]
        except KeyError:
            raise SubmitError(f'unknown spec {spec_id}') from None

    # Submission

    def submit(
        self,
        entries: list[tuple[Request, str | None, str | None]],
        *,
        proposal: str,
        submitter: str,
    ) -> list[Record]:
        """
        Check every request, then create a pending record for each.

        ``entries`` are requests with their label and member. If one request
        is refused, none is submitted.
        """
        with self._changed:
            ids = {req.placeholder: uuid.uuid4().hex[:12] for req, _, _ in entries}
            specs = {ids[req.placeholder]: self.spec(req.spec) for req, _, _ in entries}
            checked = [self._check(req, ids, specs, proposal) for req, _, _ in entries]
            records = []
            for (request, label, member), filled in zip(entries, checked, strict=True):
                record_id = ids[request.placeholder]
                records.append(
                    Record(
                        id=record_id,
                        request=filled,
                        proposal=proposal,
                        submitter=submitter,
                        created=self._clock(),
                        outputs=tuple(specs[record_id].outputs.model_fields),
                        label=label,
                        member=member,
                    )
                )
            self._records.update((r.id, r) for r in records)
            for record in records:
                self._schedule(record.id)
            return [self._records[r.id] for r in records]

    def _check(
        self,
        request: Request,
        ids: Mapping[str, str],
        specs: Mapping[str, WorkflowSpec],
        proposal: str,
    ) -> Request:
        """The request with references resolved and defaults filled."""
        spec = self.spec(request.spec)
        param_fields = data_fields(spec.params)

        def resolve(ref: OutputRef | DatasetRef) -> OutputRef | DatasetRef:
            if isinstance(ref, DatasetRef):
                try:
                    identity = self._datasets.resolve(ref)
                except KeyError:
                    raise SubmitError(f'unknown dataset {ref}') from None
                owner = self._datasets.metadata(identity).get('proposal', proposal)
                if owner != proposal:
                    raise SubmitError(f'dataset {ref} belongs to proposal {owner}')
                return identity
            record_id = ids.get(ref.record, ref.record)
            return OutputRef(record=record_id, output=ref.output, key=ref.key)

        unknown = set(request.params) - set(spec.params.model_fields)
        if unknown:
            raise SubmitError(f'{sorted(unknown)}: not parameters of {request.spec}')
        params = _map_refs(request.params, resolve)
        for name, value in params.items():
            for ref in _refs_in(value):
                if isinstance(ref, OutputRef):
                    self._check_output(ref, param_fields.get(name), specs, proposal)
        try:
            model = spec.params.model_validate(params)
        except ValidationError as error:
            problems = '; '.join(
                f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in error.errors()
            )
            raise SubmitError(f'{request.spec}: {problems}') from None
        values = {name: getattr(model, name) for name in type(model).model_fields}
        return Request(request.spec, values)

    def _check_output(
        self,
        ref: OutputRef,
        field: DataField | None,
        specs: Mapping[str, WorkflowSpec],
        proposal: str,
    ) -> None:
        if ref.record in specs:
            spec = specs[ref.record]
        elif (record := self._records.get(ref.record)) is not None:
            if record.proposal != proposal:
                raise SubmitError(
                    f'record {ref.record} belongs to proposal {record.proposal}'
                )
            spec = self.spec(record.request.spec)
        else:
            raise SubmitError(f'unknown record {ref.record}')
        outputs = data_fields(spec.outputs)
        if ref.output not in outputs:
            raise SubmitError(f'{SpecId.of(spec)} has no output {ref.output!r}')
        if field is not None and not _agree(outputs[ref.output], field):
            raise SubmitError(f'{ref} does not fit the field it fills')

    # Execution

    def _schedule(self, record_id: str) -> None:
        """Start the record if its inputs are done, or wait for them; lock held."""
        inputs = {ref.record for ref in self._records[record_id].request.refs()}
        for input_id in inputs:
            status = self._records[input_id].status
            if status.finished and status is not Status.COMPLETED:
                self._finish(record_id, Status.FAILED, f'input {input_id} {status}')
                return
        waiting = {i for i in inputs if not self._records[i].status.finished}
        if waiting:
            self._waiting[record_id] = waiting
        else:
            self._started.add(record_id)
            self._executor.submit(self._run, record_id)

    def _run(self, record_id: str) -> None:
        with self._changed:
            request = self._records[record_id].request
            if self._records[record_id].status.finished:
                return
        try:
            values = _map_refs(request.params, self._read)
            outputs = self._workflows[request.spec](**values)
        except Exception as error:  # any failure of a workflow is recorded
            with self._changed:
                self._finish(record_id, Status.FAILED, str(error))
            return
        with self._changed:
            for name, value in outputs.items():
                self._outputs[(record_id, name)] = value
            self._finish(record_id, Status.COMPLETED)

    def _read(self, ref: OutputRef | DatasetRef) -> Any:
        if isinstance(ref, DatasetRef):
            return self._datasets.read(ref)
        with self._changed:
            return self._outputs[(ref.record, ref.output)]

    def _finish(
        self, record_id: str, status: Status, failure: str | None = None
    ) -> None:
        """Finish a record and start or fail what waits for it; lock held."""
        record = self._records[record_id]
        if record.status.finished:
            return
        self._records[record_id] = record.model_copy(
            update={
                'status': status,
                'failure': None if failure is None else Failure(message=failure),
            }
        )
        self._changed.notify_all()
        dependents = [w for w, inputs in self._waiting.items() if record_id in inputs]
        for waiting_id in dependents:
            inputs = self._waiting.get(waiting_id)
            if inputs is None:  # failed meanwhile through another input
                continue
            if status is not Status.COMPLETED:
                del self._waiting[waiting_id]
                self._finish(waiting_id, Status.FAILED, f'input {record_id} {status}')
                continue
            inputs.discard(record_id)
            if not inputs:
                del self._waiting[waiting_id]
                self._started.add(waiting_id)
                self._executor.submit(self._run, waiting_id)

    # Queries and control

    def wait(self, ids: Iterable[str]) -> list[Record]:
        ids = list(ids)
        with self._changed:
            self._changed.wait_for(
                lambda: all(self._records[i].status.finished for i in ids)
            )
            return [self._records[i] for i in ids]

    def cancel(self, ids: Iterable[str]) -> None:
        """Cancel the records that have not started; those that have run to the end."""
        with self._changed:
            for record_id in ids:
                if record_id not in self._started:
                    self._waiting.pop(record_id, None)
                    self._finish(record_id, Status.CANCELLED)

    def record(self, record_id: str) -> Record:
        with self._changed:
            return self._records[record_id]

    def records(self, proposal: str) -> list[Record]:
        """Every record of a proposal, oldest first."""
        with self._changed:
            return [r for r in self._records.values() if r.proposal == proposal]

    def output(self, record_id: str, name: str) -> Any:
        with self._changed:
            return self._outputs[(record_id, name)]


def _refs_in(value: Any) -> list[OutputRef | DatasetRef]:
    found: list[OutputRef | DatasetRef] = []
    _map_refs(value, found.append)
    return found
