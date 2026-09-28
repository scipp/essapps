# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The backend: validates requests, keeps the records, schedules, and owns the data.

``Backend`` is the transport boundary: the closed surface a client may call.
Every argument and return value on it is plain data, except ``output``, which
returns the value itself. ``LocalBackend`` implements it directly, holding the
record store, data store, registry, and launcher in this process; a remote
backend (``remote.py``) implements the same protocol by forwarding every call
to a server that holds a ``LocalBackend``.

Single writer to the record store. One scheduling primitive: a request whose
inputs are pending outputs waits until they complete, fails if any of them fails,
and is cancelled if any is cancelled. Recompute is explicit. Nothing here
knows the workflow graph: every request is a whole configured pipeline, checked
against the spec's params model, and a sum over runs is one request whose run
parameter is a list, which the workflow code sums.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable, Collection, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import UnionType
from typing import Any, Protocol, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError

from .binding import Registry
from .datastore import DataStore
from .launcher import Launcher
from .records import (
    Derivation,
    Failure,
    RunRecord,
    RunRequest,
    Status,
)
from .runner import Job
from .sources import Dataset, DatasetSource, find, merge
from .spec import (
    STAND_INS,
    DataField,
    DatasetRef,
    Format,
    OutputRef,
    Ref,
    SerializedWorkflowSpec,
    SpecId,
    WorkflowSpec,
    as_ref,
    data_field_at,
    data_fields,
    submodel,
    walk_refs,
)
from .store import RecordStore
from .views import ViewSpec, view

GROUP_PREFIX = '@'


class Publisher(Protocol):
    def publish(self, path: Path, snapshot: dict[str, Any]) -> str: ...


class Access(Protocol):
    """
    Which proposals a submitter may read beyond the one owning the request.

    A request may name datasets and outputs of records of any proposal its
    submitter may read, such as a facility vanadium run or a commissioning
    reference. At a facility the user office answers this.

    The check prevents mistakes, not misuse: submitters are not authenticated,
    and a request may read its own proposal without asking, so a submitter
    who names a proposal reads it. It becomes a security boundary once
    submitters are authenticated and their membership in the owning proposal
    is checked before anything else.
    """

    def may_read(self, submitter: str, proposal: str) -> bool: ...


class OwnProposalOnly:
    """The access without a user office: a request reads its own proposal only."""

    def may_read(self, submitter: str, proposal: str) -> bool:
        return False


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


class Backend(Protocol):
    """
    The closed surface a client may ask of a backend: the transport boundary.

    Every argument and return value here is plain data -- pydantic models,
    dataclasses, str, Path, dict -- except ``output``, which returns the value
    itself, held in memory or read from disk. ``LocalBackend`` does the work in
    this process; a remote backend forwards each call to a server that holds
    one.
    """

    def close(self) -> None:
        """Release what the backend holds open, such as the record store."""
        ...

    def reserve(self, label: str, rule: str) -> None:
        """Hold a label for a rule, so a person cannot land in its batch by hand."""
        ...

    def spec(self, spec_id: SpecId) -> SerializedWorkflowSpec:
        """The serialized spec this backend knows by id."""
        ...

    def specs(self) -> list[SerializedWorkflowSpec]:
        """Every spec this backend knows, serialized."""
        ...

    def datasets(self, proposals: Collection[str]) -> list[Dataset]:
        """Every dataset the backend's sources know for these proposals, by identity."""
        ...

    def validate(
        self, request: RunRequest, group: Mapping[str, RunRequest] | None = None
    ) -> ValidationReport:
        """Whether a request would be accepted, without submitting it."""
        ...

    def submit(
        self, group: Mapping[str, RunRequest], vary: Collection[str] = ()
    ) -> dict[str, RunRecord]:
        """
        Submit requests atomically; ``@name`` refs point at members of the group.

        ``vary`` names the parameters the caller varies from run to run, so that
        a session holds the stage cut at them. Like ``label``, it is a hint: it
        does not change the result, and it is not recorded.
        """
        ...

    def record(self, record_id: str) -> RunRecord:
        """A record by id."""
        ...

    def records(
        self,
        *,
        proposal: str | None = None,
        spec: SpecId | None = None,
        status: Status | None = None,
        label: str | None = None,
        member_key: str | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[RunRecord]:
        """Records matching every given filter, oldest first."""
        ...

    def latest(
        self, label: str, proposal: str, member_key: str | None = None
    ) -> RunRecord | None:
        """The record that supersedes the others under a label (a slot)."""
        ...

    def batch(self, label: str, proposal: str) -> list[RunRecord]:
        """The batch table under a label: the latest record per member key."""
        ...

    def members_to_retry(self, label: str, proposal: str) -> list[RunRecord]:
        """The failed or cancelled latest record of each member that never completed."""
        ...

    def wait(self, record_ids: list[str], *, timeout: float = 60.0) -> list[RunRecord]:
        """Block until every record is terminal, or raise past the timeout."""
        ...

    def cancel(self, record_id: str) -> None:
        """Cancel a record; a no-op once it is terminal."""
        ...

    def recompute(self, record_id: str, submitter: str) -> RunRecord:
        """
        Run a record's request again as a new record linked to the old one,
        submitted by ``submitter``.
        """
        ...

    def retry(self, record_id: str, submitter: str) -> RunRecord:
        """Like ``recompute``, marked as a retry rather than a deliberate rerun."""
        ...

    def output(self, ref: OutputRef) -> Any:
        """The value of an output: inline from the record, or from the data store."""
        ...

    def view(self, ref: OutputRef, spec: ViewSpec) -> dict[str, Any]:
        """A plain-data view of an output, shaped by ``spec``."""
        ...

    def write_out(self, ref: OutputRef, into: Path | None = None) -> Path:
        """
        The output as a file.

        Without ``into``, the data store's own copy, written on demand for an
        in-memory output, at a path on the backend's host. With ``into``, a
        copy named after the reference placed in that folder on the caller's
        side, which over HTTP is a download: how large data leaves the service.
        """
        ...

    def drop(self, ref: OutputRef) -> None:
        """Evict an output's disk copy; the record and its value elsewhere stay."""
        ...

    def publish(
        self, ref: OutputRef, publisher: str, *, allow_reused: bool = False
    ) -> str:
        """Publish an output through the named publisher; returns its PID."""
        ...

    def provenance(self, record_id: str) -> dict[str, Any]:
        """A self-contained snapshot: raw origins, params, spec, versions."""
        ...


class LocalBackend:
    def __init__(
        self,
        record_store: RecordStore,
        data: DataStore,
        registry: Registry,
        launcher: Launcher,
        sources: Iterable[DatasetSource] = (),
        publishers: Mapping[str, Publisher] = {},
        access: Access | None = None,
    ) -> None:
        self.record_store = record_store
        self.data = data
        self.registry = registry
        self.launcher = launcher
        self.sources = list(sources)
        self.publishers = dict(publishers)
        self.access = access or OwnProposalOnly()
        self._reserved: dict[str, str] = {}
        self._vary: dict[str, tuple[str, ...]] = {}

    def close(self) -> None:
        self.record_store.close()

    def reserve(self, label: str, rule: str) -> None:
        """
        Hold a label for a rule, so a person cannot land in its batch by hand.

        In memory only: the backend has no rule store, so a process that never
        runs a trigger loop reserves nothing.
        """
        self._reserved[label] = rule

    def spec(self, spec_id: SpecId) -> SerializedWorkflowSpec:
        return self.registry.spec(spec_id).serialize()

    def specs(self) -> list[SerializedWorkflowSpec]:
        return [s.serialize() for s in self.registry.specs()]

    def datasets(self, proposals: Collection[str]) -> list[Dataset]:
        """
        Every dataset the sources know for these proposals, by identity.

        Arrival may be repeated and out of order, and two sources may know
        one dataset by different identities, so datasets that share an
        identity are one, see :func:`ess.apps.sources.merge`; nothing is
        stored to make the list.
        """
        return merge(d for s in self.sources for d in s.datasets(proposals))

    def find(self, ref: DatasetRef) -> Dataset | None:
        """
        The dataset an identity or a stand-in names among those the sources
        know, or None; raises for a stand-in that names several.
        """
        return find(merge(filter(None, (s.find(ref) for s in self.sources))), ref)

    def locate(self, ref: DatasetRef) -> Path | None:
        """
        Where a dataset's bytes are now, or None if nothing has them.

        Identity is not location, so this is asked again at every dispatch and
        nothing is kept.
        """
        for source in self.sources:
            if (path := source.locate(ref)) is not None:
                return path
        return None

    # Validation

    def validate(
        self, request: RunRequest, group: Mapping[str, RunRequest] | None = None
    ) -> ValidationReport:
        """
        Three layers: shape and spec, the values, runnability.

        The request is checked as it would be recorded, the spec's defaults
        filled, against the whole params model: a request is a configured
        pipeline, whichever outputs it asks for, so a missing parameter is
        refused here and never found when the run starts.
        """
        return self._validate(request, group or {})[0]

    def _validate(
        self, request: RunRequest, group: Mapping[str, RunRequest]
    ) -> tuple[ValidationReport, RunRequest]:
        """The report, and the request as it would be recorded where it is valid."""
        if request.spec not in self.registry:
            return ValidationReport(
                layers=('schema',), errors=(f'unknown spec {request.spec}',)
            ), request
        spec = self.registry.spec(request.spec)
        request = self._with_defaults(request)
        errors = _shape_errors(spec, request)
        if errors:
            return ValidationReport(
                layers=('schema', 'params'), errors=tuple(errors)
            ), request
        found: dict[DatasetRef, Dataset | str] = {}
        request = self._as_recorded(request, found)
        for path, ref in walk_refs(request.params):
            errors += self._check_ref(
                ref, data_field_at(spec.params, path), request, group, found
            )
        if (rule := self._reserved.get(request.label)) is not None:
            submitted = (
                request.origin.rule.rsplit('/v', 1)[0]
                if request.origin.rule is not None
                else None
            )
            if submitted != rule:
                errors.append(
                    f'label {request.label!r} is reserved for rule {rule!r}; '
                    'apply the rule instead'
                )
        if not self.launcher.can_run(request.spec):
            errors.append(f'launcher cannot run {request.spec}')
        return ValidationReport(
            layers=('schema', 'params', 'runnability'), errors=tuple(errors)
        ), request

    def _check_ref(
        self,
        ref: Ref,
        consumer: DataField | None,
        request: RunRequest,
        group: Mapping[str, RunRequest],
        found: Mapping[DatasetRef, Dataset | str],
    ) -> list[str]:
        """
        Whether a reference may fill the field it is in; ``found`` is the
        dataset each dataset reference names, or why none.
        """
        if isinstance(ref, DatasetRef):
            return self._check_dataset(ref, consumer, request, found[ref])
        if ref.record.startswith(GROUP_PREFIX):
            name = ref.record[len(GROUP_PREFIX) :]
            if name not in group:
                return [f'{ref}: no group member named {name!r}']
            producer_request = group[name]
        else:
            if ref.record not in self.record_store:
                return [f'{ref}: no such record']
            producer_request = self.record_store.get(ref.record).request
        producer_spec = producer_request.spec
        if not self._may_read(request, [producer_request.proposal]):
            return [
                f'{ref}: belongs to proposal {producer_request.proposal}, which '
                f'{request.submitter} may not read'
            ]
        if producer_spec not in self.registry:
            return [f'{ref}: spec {producer_spec} is not known here']
        outputs = self.registry.spec(producer_spec).outputs
        if ref.output not in outputs.model_fields:
            return [f'{ref}: {producer_spec} has no output {ref.output!r}']
        produced = data_fields(outputs).get(ref.output)
        if consumer is None:
            return (
                [] if produced is None else [f'{ref}: data cannot fill a literal field']
            )
        if produced is None:
            return [f'{ref}: a literal output cannot fill a data field']
        if produced.format is not consumer.format:
            return [f'{ref}: {produced.format} output into {consumer.format} field']
        return []

    def _check_dataset(
        self,
        ref: DatasetRef,
        consumer: DataField | None,
        request: RunRequest,
        dataset: Dataset | str,
    ) -> list[str]:
        """
        Whether a source knows the dataset and the submitter may read it.

        A dataset's format is not known here, so only the field it fills is
        checked. Whether the bytes are there is found out at dispatch.
        """
        if consumer is None:
            return [f'{ref}: a dataset cannot fill a literal field']
        if isinstance(dataset, str):
            return [dataset]
        if not self._may_read(request, dataset.proposals):
            return [
                f'{ref}: belongs to proposals {dataset.proposals}, which '
                f'{request.submitter} may not read'
            ]
        return []

    def _may_read(self, request: RunRequest, proposals: Iterable[str]) -> bool:
        """Whether the request may name data that belongs to any of ``proposals``."""
        return any(
            p == request.proposal or self.access.may_read(request.submitter, p)
            for p in proposals
        )

    # Origin

    def submit(
        self, group: Mapping[str, RunRequest], vary: Collection[str] = ()
    ) -> dict[str, RunRecord]:
        """
        Submit requests atomically; ``@name`` refs point at members of the group.

        Every request is validated before any record exists; one refusal refuses
        the group. ``vary`` applies to every member, so each must have its
        names as parameters. It is kept in memory until dispatch: a record
        dispatched after a restart runs without it, which holds nothing and
        gives the same result.
        """
        validated = {name: self._validate(req, group) for name, req in group.items()}
        reports = {name: report for name, (report, _) in validated.items()}
        for name, req in group.items():
            if errors := _vary_errors(req, vary, self.registry):
                reports[name] = ValidationReport(
                    layers=reports[name].layers,
                    errors=(*reports[name].errors, *errors),
                )
        for name in _cycle(group):
            reports[name] = ValidationReport(
                layers=reports[name].layers,
                errors=(*reports[name].errors, 'part of a cycle within the group'),
            )
        if any(not r.ok for r in reports.values()):
            raise SubmitError({n: r for n, r in reports.items() if not r.ok})
        ids = {name: RunRecord(request=req).id for name, req in group.items()}
        records = {}
        latest: dict[tuple[str, str | None], str] = {}
        named = {GROUP_PREFIX + n: i for n, i in ids.items()}
        for name, req in group.items():
            record = RunRecord(
                id=ids[name],
                request=self._complete(validated[name][1], named),
                supersedes=self._supersedes(req, latest),
            )
            records[name] = record
            if req.label is not None:
                latest[(req.label, req.member_key)] = record.id
        self.record_store.add(*records.values())
        if vary:
            self._vary |= {r.id: tuple(vary) for r in records.values()}
        self._pump()
        return {name: self.record_store.get(r.id) for name, r in records.items()}

    def _complete(self, request: RunRequest, named: Mapping[str, str]) -> RunRequest:
        """
        The validated request as recorded: group references by ID, outputs named.

        A request without outputs asks for the spec's results, and its record
        names them, so that what a record computed never depends on the spec.
        """
        _check_identities(request)
        return request.model_copy(
            update={
                'params': _rewrite(request.params, lambda ref: _grouped(ref, named)),
                'outputs': request.outputs or self.registry.spec(request.spec).results,
            }
        )

    def _as_recorded(
        self, request: RunRequest, found: dict[DatasetRef, Dataset | str]
    ) -> RunRequest:
        """
        The request with defaults filled, every value in the form the params
        model gives it, and every dataset named by its identity, in the
        params and in the values the submitter pinned alike.

        So ``0`` and ``0.0`` given for one float field are one recorded value
        and one held stage, ``run:dream/1`` and the dataset's identity are one
        input, and a reprocess that carries a pinned ``run:dream/1`` names the
        dataset it named the first time. ``found`` collects, by each dataset
        reference the result holds, the dataset it names, or why none: a
        reference no source resolves is kept as given. Raises for a request
        whose values do not validate.
        """
        request = self._with_defaults(request)
        params = self.registry.spec(request.spec).params
        values = params.model_validate(request.params).model_dump(mode='json')

        def identity(ref: Ref) -> Ref:
            if not isinstance(ref, DatasetRef):
                return ref
            if ref not in found:
                try:
                    found[ref] = self.find(ref) or f'{ref}: no source knows it'
                except ValueError as e:
                    found[ref] = str(e)
            if isinstance(dataset := found[ref], str):
                return ref
            found[dataset.ref] = dataset
            return dataset.ref

        origin = request.origin
        return request.model_copy(
            update={
                'params': _rewrite(values, identity),
                'origin': origin.model_copy(
                    update={'pinned': _rewrite(origin.pinned, identity)}
                ),
            }
        )

    def _with_defaults(self, request: RunRequest) -> RunRequest:
        """
        The request with the spec's default in ``params`` for every parameter
        that has one and is not given.

        The record then says which value every parameter had, so that two
        requests that differ only in giving a default name the same pipeline,
        and a recompute runs with the values of the first run even when the
        spec's defaults have changed since. A required parameter without a
        default stays unset.
        """
        params = self.registry.spec(request.spec).params
        missing = [
            name
            for name, info in params.model_fields.items()
            if not info.is_required() and name not in request.params
        ]
        if not missing:
            return request
        defaults = submodel(params, missing, 'Defaults')().model_dump(mode='json')
        return request.model_copy(update={'params': {**request.params, **defaults}})

    def _supersedes(
        self, request: RunRequest, latest: Mapping[tuple[str, str | None], str]
    ) -> str | None:
        """
        The latest record this request's record supersedes, or None without a label.

        Within one group submitted together, a later request under the same
        label and member key supersedes the earlier one in the group, not the
        record that was latest before the group; ``latest`` holds the group's
        own as they are assigned while it is built.
        """
        if request.label is None:
            return None
        key = (request.label, request.member_key)
        if key in latest:
            return latest[key]
        record = self.record_store.latest(
            request.label, request.proposal, member_key=request.member_key
        )
        return None if record is None else record.id

    def recompute(self, record_id: str, submitter: str) -> RunRecord:
        """Run a record's request again as a new record linked to the old one."""
        return self._derive(record_id, submitter, 'recompute')

    def retry(self, record_id: str, submitter: str) -> RunRecord:
        return self._derive(record_id, submitter, 'retry')

    def _derive(self, record_id: str, submitter: str, reason: Any) -> RunRecord:
        """
        The old record's request, validated and recorded again as ``submitter``'s.
        """
        old = self.record_store.get(record_id)
        report, request = self._validate(
            old.request.model_copy(update={'submitter': submitter}), {}
        )
        if not report.ok:
            raise SubmitError({record_id: report})
        _check_identities(request)
        new = RunRecord(
            request=request,
            derives_from=Derivation(record=old.id, reason=reason),
            supersedes=self._supersedes(old.request, {}),
        )
        self.record_store.add(new)
        self._pump()
        return self.record_store.get(new.id)

    def record(self, record_id: str) -> RunRecord:
        return self.record_store.get(record_id)

    def records(
        self,
        *,
        proposal: str | None = None,
        spec: SpecId | None = None,
        status: Status | None = None,
        label: str | None = None,
        member_key: str | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[RunRecord]:
        return self.record_store.list(
            proposal=proposal,
            spec=spec,
            status=status,
            label=label,
            member_key=member_key,
            since=since,
            limit=limit,
        )

    def latest(
        self, label: str, proposal: str, member_key: str | None = None
    ) -> RunRecord | None:
        return self.record_store.latest(label, proposal, member_key=member_key)

    def batch(self, label: str, proposal: str) -> list[RunRecord]:
        return self.record_store.batch(label, proposal)

    def members_to_retry(self, label: str, proposal: str) -> list[RunRecord]:
        return self.record_store.members_to_retry(label, proposal)

    def cancel(self, record_id: str) -> None:
        record = self.record_store.get(record_id)
        if record.status.terminal:
            return
        self.launcher.cancel(record)
        self._finish(record, Status.CANCELLED)
        self._propagate(record)

    # Scheduling

    def poll(self) -> None:
        """Reconcile dispatched runs with the launcher, then dispatch what can run."""
        for record in list(
            self.record_store.by_status(Status.DISPATCHED, Status.RUNNING)
        ):
            updated = self.launcher.poll(record)
            if updated.status != record.status:
                self.record_store.update(updated)
                if updated.status.terminal:
                    self._propagate(updated)
        self._pump()

    def wait(
        self, record_ids: list[str], *, timeout: float = 60.0, interval: float = 0.05
    ) -> list[RunRecord]:
        deadline = time.monotonic() + timeout
        while True:
            self.poll()
            records = [self.record_store.get(i) for i in record_ids]
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
            for stale in list(
                self.record_store.by_status(Status.SUBMITTED, Status.WAITING)
            ):
                record = self.record_store.get(stale.id)
                if record.status.terminal:
                    continue
                producers = [
                    self.record_store.get(r.record) for r in record.request.refs()
                ]
                if all(p.status == Status.COMPLETED for p in producers):
                    self._dispatch(record)
                    progressed = True
                elif (dead := _dead(producers)) is not None:
                    # A request submitted after its producer failed never sees a
                    # status transition, so the failure is read here as well.
                    self._inherit(record, dead)
                    progressed = True
                elif record.status == Status.SUBMITTED:
                    record.status = Status.WAITING
                    self.record_store.update(record)

    def _propagate(self, record: RunRecord) -> None:
        """Failure and cancellation flow to every record waiting on this one."""
        if record.status == Status.COMPLETED:
            return
        for dependent_id in self.record_store.referencing(record.id):
            dependent = self.record_store.get(dependent_id)
            if not dependent.status.terminal:
                self._inherit(dependent, record)

    def _inherit(self, record: RunRecord, producer: RunRecord) -> None:
        """End a record because an input of it failed or was cancelled."""
        if producer.status == Status.CANCELLED:
            self._finish(record, Status.CANCELLED)
        else:
            self._finish(
                record,
                Status.FAILED,
                Failure(kind='upstream', message=f'input {producer.id} failed'),
            )
        self._propagate(record)

    def _finish(
        self, record: RunRecord, status: Status, failure: Failure | None = None
    ) -> None:
        record.status = status
        record.failure = failure
        record.finished = datetime.now(UTC)
        self.record_store.update(record)

    def _dispatch(self, record: RunRecord) -> None:
        params = self.registry.spec(record.spec).params
        locations: dict[Ref, Path] = {}
        literals: dict[str, Any] = {}
        named = [
            (ref, data_field_at(params, path) is not None)
            for path, ref in walk_refs(record.request.params)
        ]
        # A reference named by two parameters is one thing to resolve, and
        # locating a dataset means asking every source.
        for ref, into_data_field in dict.fromkeys(named):
            failure = self._resolve(ref, into_data_field, locations, literals)
            if failure is not None:
                self._finish(record, Status.FAILED, failure)
                self._propagate(record)
                return
        request = record.request
        job = Job(
            params=_inline(request.params, literals),
            vary=self._vary.pop(record.id, ()),
            outputs=request.outputs,
        )
        done = self.launcher.start(record, job, locations)
        self.record_store.update(done)
        if done.status.terminal:
            self._propagate(done)

    def _resolve(
        self,
        ref: Ref,
        into_data_field: bool,
        locations: dict[Ref, Path],
        literals: dict[str, Any],
    ) -> Failure | None:
        """
        What a reference stands for at dispatch, or why the run cannot have it.

        A dataset is located through the sources; an output of a record is a path
        when the launcher needs disk inputs, the value itself when it fills a
        literal field, and otherwise served from the data store by the runner.
        """
        if isinstance(ref, DatasetRef):
            located = self.locate(ref)
            if located is None:
                asked = ', '.join(repr(s) for s in self.sources) or 'no sources'
                return Failure(
                    kind='missing-dataset',
                    message=f'{ref}: no source has it; asked {asked}',
                )
            locations[ref] = located
            return None
        producer = self.record_store.get(ref.record)
        if ref.key is not None and ref.key not in (
            producer.output_keys(ref.output) or set()
        ):
            return Failure(kind='missing-key', message=f'{ref}: no such element')
        if not into_data_field:
            value = producer.outputs[ref.output]
            literals[str(ref)] = value[ref.key] if ref.key is not None else value
            return None
        if not self.data.available(ref):
            return Failure(
                kind='missing-copy', message=f'{ref}: no copy; recompute the producer'
            )
        if self.launcher.needs_disk_inputs:
            locations[ref] = self.data.path(ref)
        return None

    # Data

    def output(self, ref: OutputRef) -> Any:
        """The value of an output: inline from the record, or from the data store."""
        record = self.record_store.get(ref.record)
        if ref.output in record.outputs:
            value = record.outputs[ref.output]
            return value[ref.key] if ref.key is not None else value
        if ref not in record.stored_outputs:
            raise KeyError(
                f'{record.id} has no output {ref.output!r}'
                + (f' key {ref.key!r}' if ref.key else '')
            )
        outputs = data_fields(self.registry.spec(record.spec).outputs)
        if outputs[ref.output].format is Format.SCIPP:
            return self.data.array(ref)
        return self.data.path(ref)

    def view(self, ref: OutputRef, spec: ViewSpec) -> dict[str, Any]:
        return view(self.data.array(ref), spec)

    def write_out(self, ref: OutputRef, into: Path | None = None) -> Path:
        path = self.data.write_out(ref)
        if into is None:
            return path
        into.mkdir(parents=True, exist_ok=True)
        return Path(shutil.copy(path, into / f'{ref}{path.suffix}'))

    def drop(self, ref: OutputRef) -> None:
        self.data.drop(ref)

    # Publication

    def provenance(self, record_id: str) -> dict[str, Any]:
        """
        A self-contained snapshot: raw origins, params, spec, versions.

        Params keep their reference form; the value a literal output fed into
        a parameter is under ``literals`` of the input record it came from.
        """
        record = self.record_store.get(record_id)
        return {
            'record': record.id,
            'spec': str(record.spec),
            'outputs': list(record.request.outputs),
            'params': record.request.params,
            'literals': record.outputs,
            'package_versions': record.package_versions,
            'environment': record.environment,
            'binding': record.binding,
            'raw': [
                d.model_dump(mode='json', exclude_none=True)
                for d in record.request.datasets()
            ],
            'checksums': record.checksums,
            'inputs': [
                self.provenance(r)
                for r in dict.fromkeys(ref.record for ref in record.request.refs())
            ],
        }

    def publish(
        self, ref: OutputRef, publisher: str, *, allow_reused: bool = False
    ) -> str:
        """
        Publish an output through the named publisher.

        A publisher is server-side code; a client names it rather than passing
        it, so that publishing works the same over a remote backend. Idempotent,
        from a disk copy, with a provenance snapshot. A record whose result came
        out of a held stage is refused unless allowed, so that what is published
        was computed from the parameters alone.
        """
        try:
            target = self.publishers[publisher]
        except KeyError:
            raise KeyError(
                f'no publisher {publisher!r}; known: {sorted(self.publishers)}'
            ) from None
        record = self.record_store.get(ref.record)
        if ref.output in record.published:
            return record.published[ref.output]
        if record.status != Status.COMPLETED:
            raise ValueError(f'{record.id} is {record.status.value}')
        if record.reused and not allow_reused:
            raise ValueError(f'{record.id} reused a held stage; recompute it first')
        if record.binding == 'in_process' and not allow_reused:
            raise ValueError(f'{record.id} was bound in-process; not reproducible')
        path = self.data.path(ref)
        if ref.output not in record.publishing:
            record.publishing.append(ref.output)
            self.record_store.update(record)
        pid = target.publish(path, self.provenance(record.id))
        record.publishing.remove(ref.output)
        record.published[ref.output] = pid
        self.record_store.update(record)
        return pid


def _check_identities(request: RunRequest) -> None:
    """Raise for a dataset a request about to be recorded names by a stand-in."""
    values = {'params': request.params, 'pinned': request.origin.pinned}
    if stand_ins := [
        str(ref)
        for _, ref in walk_refs(values)
        if isinstance(ref, DatasetRef) and ref.dataset.partition(':')[0] in STAND_INS
    ]:
        raise RuntimeError(f'unresolved stand-ins in a request to record: {stand_ins}')


def _dead(producers: Iterable[RunRecord]) -> RunRecord | None:
    """The first producer that ended without completing, if there is one."""
    return next(
        (p for p in producers if p.status.terminal and p.status != Status.COMPLETED),
        None,
    )


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


def _grouped(ref: Ref, ids: Mapping[str, str]) -> Ref:
    """An ``@name`` reference pointed at the ID the group's member was given."""
    if isinstance(ref, OutputRef) and ref.record in ids:
        return ref.model_copy(update={'record': ids[ref.record]})
    return ref


def _rewrite(value: Any, fn: Callable[[Ref], Ref]) -> Any:
    """Every reference in ``value`` replaced by what ``fn`` makes of it."""
    if isinstance(value, dict):
        if (ref := as_ref(value)) is not None:
            new = fn(ref)
            return value if new == ref else new.model_dump()
        return {k: _rewrite(v, fn) for k, v in value.items()}
    if isinstance(value, list):
        return [_rewrite(v, fn) for v in value]
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


def _vary_errors(
    request: RunRequest, vary: Collection[str], registry: Registry
) -> list[str]:
    """A varied name that is not a parameter would cut the stage at nothing."""
    if request.spec not in registry:
        return []
    fields = registry.spec(request.spec).params.model_fields
    return [
        f'{name}: varied but not a parameter of {request.spec}'
        for name in vary
        if name not in fields
    ]


def _shape_errors(spec: WorkflowSpec, request: RunRequest) -> list[str]:
    """
    What is wrong with the names and values of a request, without workflow code.

    A params model ignores fields it does not declare unless its author forbids
    them, and a reduction parameter dropped in silence gives a wrong number
    without an error, so every name is checked against the spec, the names in a
    row or another nested model included. The values are checked against the
    whole params model.
    """
    fields = spec.params.model_fields
    errors = [
        f'{name}: not a parameter of {spec.id}'
        for name in sorted(set(request.params) - set(fields))
    ]
    errors += [
        error
        for name in sorted(set(request.params) & set(fields))
        for error in _unknown_names(fields[name].annotation, request.params[name], name)
    ]
    errors += [
        f'{name}: not an output of {spec.id}'
        for name in request.outputs
        if name not in spec.outputs.model_fields
    ]
    if errors:
        return errors
    try:
        spec.params.model_validate(request.params)
    except ValidationError as e:
        errors += [
            f'{".".join(map(str, err["loc"]))}: {err["msg"]}' for err in e.errors()
        ]
    return errors


def _unknown_names(annotation: Any, value: Any, path: str) -> list[str]:
    """
    The names in ``value`` that its model does not declare, at any depth: in a
    model, a list or dict of models, or an optional model.
    """
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if not isinstance(value, dict):
            return []
        fields = annotation.model_fields
        errors: list[str] = []
        for name, inner in value.items():
            if name in fields:
                errors += _unknown_names(
                    fields[name].annotation, inner, f'{path}.{name}'
                )
            else:
                errors.append(f'{path}.{name}: not a field of {annotation.__name__}')
        return errors
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is list and isinstance(value, list):
        return [
            error
            for i, item in enumerate(value)
            for error in _unknown_names(args[0], item, f'{path}[{i}]')
        ]
    if origin is dict and isinstance(value, dict):
        return [
            error
            for key, item in value.items()
            for error in _unknown_names(args[1], item, f'{path}[{key}]')
        ]
    options = [arg for arg in args if arg is not type(None)]
    if origin in (Union, UnionType) and len(options) == 1:
        return _unknown_names(options[0], value, path)
    return []
