# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The backend: validates requests, keeps the records, schedules, and owns the data.

Single writer to the record store. One scheduling primitive: a request whose
inputs are pending outputs waits until they complete, fails if any of them fails,
and is cancelled if any is cancelled (D6). Recompute is explicit (D1).
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from .binding import Registry
from .datastore import DataStore
from .launcher import Launcher
from .records import Derivation, Failure, RunRecord, RunRequest, Status
from .spec import (
    FILE_SPEC,
    DataRef,
    Kind,
    LocalOrigin,
    PidOrigin,
    Ref,
    as_ref,
    data_ref_fields,
    walk_refs,
)
from .store import RecordStore
from .views import ViewSpec, view

GROUP_PREFIX = '@'


class Publisher(Protocol):
    def publish(self, path: Path, snapshot: dict[str, Any]) -> str: ...


class ValidationReport(BaseModel, frozen=True):
    """Which layers ran and what they found; empty errors means valid."""

    layers: tuple[str, ...]
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


class SubmitError(ValueError):
    def __init__(self, reports: Mapping[str, ValidationReport]) -> None:
        self.reports = dict(reports)
        lines = [f'{name}: {e}' for name, r in reports.items() for e in r.errors]
        super().__init__('Refused:\n  ' + '\n  '.join(lines))


class Backend:
    def __init__(
        self,
        records: RecordStore,
        data: DataStore,
        registry: Registry,
        launcher: Launcher,
    ) -> None:
        self.records = records
        self.data = data
        self.registry = registry
        self.launcher = launcher

    def close(self) -> None:
        self.records.close()

    # Stand-ins

    def file_record(
        self, path: Path, *, instrument: str, proposal: str, submitter: str
    ) -> RunRecord:
        """The file record for a local path, created on first reference."""
        path = Path(path).resolve()
        origin = LocalOrigin(path=path)
        return self._file_record(origin, path, instrument, proposal, submitter)

    def dataset_record(
        self, pid: str, path: Path, *, instrument: str, proposal: str, submitter: str
    ) -> RunRecord:
        """The file record for a catalogue dataset, created on first reference."""
        return self._file_record(
            PidOrigin(pid=pid), path, instrument, proposal, submitter
        )

    def _file_record(
        self,
        origin: LocalOrigin | PidOrigin,
        path: Path,
        instrument: str,
        proposal: str,
        submitter: str,
    ) -> RunRecord:
        params = {'origin': origin.model_dump(mode='json')}
        for existing in self.records.list(proposal=proposal, spec=FILE_SPEC.id):
            if existing.request.params == params:
                return existing
        if not Path(path).is_file():
            raise FileNotFoundError(path)
        request = RunRequest(
            spec=FILE_SPEC.id,
            params=params,
            instrument=instrument,
            proposal=proposal,
            submitter=submitter,
        )
        record = RunRecord(request=request, status=Status.COMPLETED, binding='file')
        record.finished = record.created
        record.stored_outputs = [Ref(record=record.id, output='file')]
        self.records.add(record)
        self.data.adopt(record.stored_outputs[0], Path(path), store_owned=False)
        return record

    # Validation

    def validate(
        self, request: RunRequest, group: Mapping[str, RunRequest] | None = None
    ) -> ValidationReport:
        """Three layers: shape and spec, the params model, runnability."""
        errors: list[str] = []
        if request.spec not in self.registry:
            return ValidationReport(
                layers=('schema',), errors=(f'unknown spec {request.spec}',)
            )
        spec = self.registry.spec(request.spec)
        try:
            spec.params.model_validate(request.params)
        except ValidationError as e:
            errors += [
                f'{".".join(map(str, err["loc"]))}: {err["msg"]}' for err in e.errors()
            ]
            return ValidationReport(layers=('schema', 'params'), errors=tuple(errors))
        refs = data_ref_fields(spec.params)
        for path, ref in walk_refs(request.params):
            field = path.split('.')[0].split('[')[0]
            errors += self._check_ref(ref, refs.get(field), request, group or {})
        if not self.launcher.can_run(request.spec):
            errors.append(f'launcher cannot run {request.spec}')
        return ValidationReport(
            layers=('schema', 'params', 'runnability'), errors=tuple(errors)
        )

    def _check_ref(
        self,
        ref: Ref,
        consumer: DataRef | None,
        request: RunRequest,
        group: Mapping[str, RunRequest],
    ) -> list[str]:
        if ref.record.startswith(GROUP_PREFIX):
            name = ref.record[len(GROUP_PREFIX) :]
            if name not in group:
                return [f'{ref}: no group member named {name!r}']
            producer_spec, producer_proposal = group[name].spec, group[name].proposal
        else:
            if ref.record not in self.records:
                return [f'{ref}: no such record']
            producer = self.records.get(ref.record)
            producer_spec, producer_proposal = producer.spec, producer.request.proposal
        if producer_proposal != request.proposal:
            return [f'{ref}: belongs to proposal {producer_proposal}']
        if producer_spec not in self.registry:
            return [f'{ref}: spec {producer_spec} is not known here']
        outputs = self.registry.spec(producer_spec).outputs
        if ref.output not in outputs.model_fields:
            return [f'{ref}: {producer_spec} has no output {ref.output!r}']
        produced = data_ref_fields(outputs).get(ref.output)
        if producer_spec == FILE_SPEC.id:
            return (
                []
                if consumer is not None
                else [f'{ref}: a file cannot fill a literal field']
            )
        if consumer is None:
            return (
                [] if produced is None else [f'{ref}: data cannot fill a literal field']
            )
        if produced is None:
            return [f'{ref}: a literal output cannot fill a data field']
        if (consumer.kind is Kind.ARRAY) != (produced.kind is Kind.ARRAY):
            return [f'{ref}: {produced.kind} output into {consumer.kind} field']
        return []

    # Submission

    def submit(self, group: Mapping[str, RunRequest]) -> dict[str, RunRecord]:
        """
        Submit requests atomically; ``@name`` refs point at members of the group.

        Every request is validated before any record exists; one refusal refuses
        the group.
        """
        reports = {name: self.validate(req, group) for name, req in group.items()}
        for name in _cycle(group):
            reports[name] = ValidationReport(
                layers=reports[name].layers,
                errors=(*reports[name].errors, 'part of a cycle within the group'),
            )
        if any(not r.ok for r in reports.values()):
            raise SubmitError({n: r for n, r in reports.items() if not r.ok})
        ids = {name: RunRecord(request=req).id for name, req in group.items()}
        records = {}
        for name, req in group.items():
            params = _rewrite(req.params, {GROUP_PREFIX + n: i for n, i in ids.items()})
            records[name] = RunRecord(
                id=ids[name], request=req.model_copy(update={'params': params})
            )
        self.records.add(*records.values())
        self._pump()
        return {name: self.records.get(r.id) for name, r in records.items()}

    def submit_one(self, request: RunRequest) -> RunRecord:
        return self.submit({'request': request})['request']

    def recompute(self, record_id: str) -> RunRecord:
        """Run a record's request again as a new record linked to the old one."""
        return self._derive(record_id, 'recompute')

    def retry(self, record_id: str) -> RunRecord:
        return self._derive(record_id, 'retry')

    def _derive(self, record_id: str, reason: Any) -> RunRecord:
        old = self.records.get(record_id)
        report = self.validate(old.request)
        if not report.ok:
            raise SubmitError({record_id: report})
        new = RunRecord(
            request=old.request, derives_from=Derivation(record=old.id, reason=reason)
        )
        self.records.add(new)
        self._pump()
        return self.records.get(new.id)

    def cancel(self, record_id: str) -> None:
        record = self.records.get(record_id)
        if record.status.terminal:
            return
        self.launcher.cancel(record)
        self._finish(record, Status.CANCELLED)
        self._propagate(record)

    # Scheduling

    def poll(self) -> None:
        """Reconcile dispatched runs with the launcher, then dispatch what can run."""
        for record in list(self.records.by_status(Status.DISPATCHED, Status.RUNNING)):
            updated = self.launcher.poll(record)
            if updated.status != record.status:
                self.records.update(updated)
                if updated.status.terminal:
                    self._propagate(updated)
        self._pump()

    def wait(
        self, record_ids: list[str], *, timeout: float = 60.0, interval: float = 0.05
    ) -> list[RunRecord]:
        deadline = time.monotonic() + timeout
        while True:
            self.poll()
            records = [self.records.get(i) for i in record_ids]
            if all(r.status.terminal for r in records):
                return records
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f'{[r.id for r in records if not r.status.terminal]}'
                )
            time.sleep(interval)

    def _pump(self) -> None:
        progressed = True
        while progressed:
            progressed = False
            for stale in list(self.records.by_status(Status.SUBMITTED, Status.WAITING)):
                record = self.records.get(stale.id)
                if record.status.terminal:
                    continue
                producers = [self.records.get(r.record) for r in record.request.refs()]
                if all(p.status == Status.COMPLETED for p in producers):
                    self._dispatch(record)
                    progressed = True
                elif record.status == Status.SUBMITTED:
                    record.status = Status.WAITING
                    self.records.update(record)

    def _propagate(self, record: RunRecord) -> None:
        """Failure and cancellation flow to every record waiting on this one."""
        if record.status == Status.COMPLETED:
            return
        for dependent_id in self.records.referencing(record.id):
            dependent = self.records.get(dependent_id)
            if dependent.status.terminal:
                continue
            if record.status == Status.CANCELLED:
                self._finish(dependent, Status.CANCELLED)
            else:
                self._finish(
                    dependent,
                    Status.FAILED,
                    Failure(kind='upstream', message=f'input {record.id} failed'),
                )
            self._propagate(dependent)

    def _finish(
        self, record: RunRecord, status: Status, failure: Failure | None = None
    ) -> None:
        record.status = status
        record.failure = failure
        record.finished = datetime.now(UTC)
        self.records.update(record)

    def _dispatch(self, record: RunRecord) -> None:
        data_fields = data_ref_fields(self.registry.spec(record.spec).params)
        locations: dict[Ref, Path] = {}
        literals: dict[str, Any] = {}
        for path, ref in walk_refs(record.request.params):
            producer = self.records.get(ref.record)
            failure = None
            if ref.key is not None and ref.key not in (
                producer.output_keys(ref.output) or set()
            ):
                failure = Failure(kind='missing-key', message=f'{ref}: no such element')
            elif path.split('.')[0].split('[')[0] in data_fields:
                if not self.data.available(ref):
                    failure = Failure(
                        kind='missing-copy',
                        message=f'{ref}: no copy; recompute the producer',
                    )
                elif self.launcher.needs_disk_inputs:
                    locations[ref] = self.data.get(ref, Kind.OPAQUE)
            else:
                value = producer.outputs[ref.output]
                literals[str(ref)] = value[ref.key] if ref.key is not None else value
            if failure is not None:
                self._finish(record, Status.FAILED, failure)
                self._propagate(record)
                return
        params = _inline(record.request.params, literals)
        done = self.launcher.start(record, params, locations)
        self.records.update(done)
        if done.status.terminal:
            self._propagate(done)

    # Data

    def output(self, ref: Ref) -> Any:
        """The value of an output: inline from the record, or from the data store."""
        record = self.records.get(ref.record)
        if ref.output in record.outputs:
            value = record.outputs[ref.output]
            return value[ref.key] if ref.key is not None else value
        if ref not in record.stored_outputs:
            raise KeyError(
                f'{record.id} has no output {ref.output!r}'
                + (f' key {ref.key!r}' if ref.key else '')
            )
        kind = data_ref_fields(self.registry.spec(record.spec).outputs)[ref.output].kind
        return self.data.get(ref, kind)

    def view(self, ref: Ref, spec: ViewSpec) -> dict[str, Any]:
        return view(self.data.get(ref, Kind.ARRAY), spec)

    # Publication (D11)

    def provenance(self, record_id: str) -> dict[str, Any]:
        """A self-contained snapshot: raw origins, resolved params, spec, versions."""
        record = self.records.get(record_id)
        raw: list[dict[str, Any]] = []
        inputs: list[dict[str, Any]] = []
        for ref in record.request.refs():
            producer = self.records.get(ref.record)
            if producer.spec == FILE_SPEC.id:
                raw.append(producer.request.params['origin'])
            else:
                inputs.append(self.provenance(producer.id))
        return {
            'record': record.id,
            'spec': str(record.spec),
            'params': record.resolved_params,
            'package_versions': record.package_versions,
            'environment': record.environment,
            'binding': record.binding,
            'raw': raw,
            'inputs': inputs,
        }

    def publish(
        self, ref: Ref, publisher: Publisher, *, allow_reused: bool = False
    ) -> str:
        """
        Publish an output: idempotent, from a disk copy, with a provenance snapshot.

        A record whose workflow object was reused is refused unless allowed, so
        that what is published was computed cold.
        """
        record = self.records.get(ref.record)
        if ref.output in record.published:
            return record.published[ref.output]
        if record.status != Status.COMPLETED:
            raise ValueError(f'{record.id} is {record.status.value}')
        if record.reused and not allow_reused:
            raise ValueError(f'{record.id} reused a warm workflow; recompute it first')
        if record.binding == 'in_process' and not allow_reused:
            raise ValueError(f'{record.id} was bound in-process; not reproducible')
        path = self.data.get(ref, Kind.OPAQUE)
        if ref.output not in record.publishing:
            record.publishing.append(ref.output)
            self.records.update(record)
        pid = publisher.publish(path, self.provenance(record.id))
        record.publishing.remove(ref.output)
        record.published[ref.output] = pid
        self.records.update(record)
        return pid


def _cycle(group: Mapping[str, RunRequest]) -> set[str]:
    """Members of the group that lie on a cycle of ``@name`` references."""
    edges = {
        name: {
            r.record[len(GROUP_PREFIX) :]
            for r in req.refs()
            if r.record.startswith(GROUP_PREFIX)
        }
        for name, req in group.items()
    }
    on_cycle: set[str] = set()
    for start in edges:
        stack, seen = [start], set()
        while stack:
            for nxt in edges.get(stack.pop(), ()):
                if nxt == start:
                    on_cycle.add(start)
                elif nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
    return on_cycle


def _rewrite(value: Any, ids: Mapping[str, str]) -> Any:
    if isinstance(value, Ref):
        value = value.model_dump()
    if isinstance(value, dict):
        if (ref := as_ref(value)) is not None:
            return ref.model_dump() | {'record': ids.get(ref.record, ref.record)}
        return {k: _rewrite(v, ids) for k, v in value.items()}
    if isinstance(value, list):
        return [_rewrite(v, ids) for v in value]
    return value


def _inline(value: Any, literals: Mapping[str, Any]) -> Any:
    """Replace refs into literal outputs by the values themselves."""
    if isinstance(value, dict):
        if (ref := as_ref(value)) is not None:
            return literals.get(str(ref), value)
        return {k: _inline(v, literals) for k, v in value.items()}
    if isinstance(value, list):
        return [_inline(v, literals) for v in value]
    return value
