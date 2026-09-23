# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The client interface: the backend's Python interface, which is the API.

See docs/developer/operations.md.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .backend import Backend, LocalBackend, Publisher, ValidationReport
from .binding import ENTRY_POINT_REGISTRY, Registry, import_object
from .datastore import DataStore
from .launcher import Launcher, SessionLauncher, SubprocessLauncher
from .records import Origin, StageRecord, StageRequest, Status
from .sources import Dataset, DatasetSource
from .spec import (
    Format,
    OutputRef,
    Ref,
    SerializedWorkflowSpec,
    SpecId,
    WorkflowSpec,
    schema_data_fields,
)
from .store import RecordStore
from .views import ViewSpec


class Candidate(BaseModel, frozen=True):
    """A row of the picker: what may fill a data-reference field, and how to show it."""

    ref: Ref
    format: Format | None = Field(
        default=None, description="None for a dataset, whose format is not known."
    )
    display: dict[str, Any] = Field(default_factory=dict)


class Client:
    """One user's handle on a backend, with the instrument and proposal fixed."""

    def __init__(
        self, backend: Backend, *, instrument: str, proposal: str, submitter: str
    ) -> None:
        self.backend = backend
        self.instrument = instrument
        self.proposal = proposal
        self.submitter = submitter

    def close(self) -> None:
        self.backend.close()

    def spec(self, spec_id: SpecId) -> SerializedWorkflowSpec:
        return self.backend.spec(spec_id)

    def specs(self) -> list[SerializedWorkflowSpec]:
        return self.backend.specs()

    def workflow(
        self,
        spec: WorkflowSpec | SpecId,
        params: BaseModel | Mapping[str, Any] | None = None,
    ) -> WorkflowHandle:
        """
        A pipeline with parameters set, for this instrument and proposal.

        Nothing runs and nothing is stored: the handle only makes stage
        requests, each of which carries ``spec`` and ``params``.
        """
        spec_id = spec.id if isinstance(spec, WorkflowSpec) else spec
        if isinstance(params, BaseModel):
            params = params.model_dump(mode='json', exclude_unset=True)
        return WorkflowHandle(self, spec_id, dict(params or {}))

    def request(
        self,
        spec: WorkflowSpec | SpecId,
        params: BaseModel | Mapping[str, Any] | None = None,
        *,
        inputs: Mapping[str, Any] | None = None,
        outputs: Iterable[str] = (),
        label: str | None = None,
        member_key: str | None = None,
        origin: Origin | None = None,
    ) -> StageRequest:
        """A stage request cut from the pipeline of ``spec`` and ``params``."""
        return self.workflow(spec, params).request(
            inputs,
            outputs=outputs,
            label=label,
            member_key=member_key,
            origin=origin,
        )

    def validate(self, request: StageRequest) -> ValidationReport:
        return self.backend.validate(request)

    def submit(self, request: StageRequest) -> StageRecord:
        return self.backend.submit({'request': request})['request']

    def submit_group(self, group: Mapping[str, StageRequest]) -> dict[str, StageRecord]:
        """Submit together; ``@name`` in a reference names a member of the group."""
        return self.backend.submit(group)

    def run(
        self, spec: WorkflowSpec | SpecId, params: Any = None, **kwargs: Any
    ) -> StageRecord:
        """A plain run: the stage with no inputs, computing the spec's results."""
        return self.submit(self.request(spec, params, **kwargs))

    def datasets(self) -> list[Dataset]:
        """Every dataset the backend's sources know for this proposal, by identity."""
        return self.backend.datasets(self.proposal)

    def pick(self, format: Format | None = None) -> list[Candidate]:
        """
        The candidates for a data-reference field of this format: the picker.

        Completed outputs from the record store and datasets from every source.
        Nothing is stored to make the list, and a further place to pick from is
        another dataset source, not a change here.
        """
        rows = [*self._picked_outputs(), *self._picked_datasets()]
        return [row for row in rows if format is None or row.format in (None, format)]

    def _picked_outputs(self) -> Iterator[Candidate]:
        for record in self.records(status=Status.COMPLETED):
            try:
                spec = self.spec(record.spec)
            except KeyError:
                continue
            outputs = schema_data_fields(spec.outputs_schema)
            for ref in record.stored_outputs:
                if (data := outputs.get(ref.output)) is not None:
                    yield Candidate(
                        ref=ref,
                        format=data.format,
                        display={
                            'name': f'{record.spec} {ref.output}',
                            'created': record.created.isoformat(),
                        },
                    )

    def _picked_datasets(self) -> Iterator[Candidate]:
        for dataset in self.datasets():
            yield Candidate(
                ref=dataset.ref,
                display={'name': dataset.path.name} | dataset.metadata,
            )

    def record(self, record_id: str) -> StageRecord:
        return self.backend.record(record_id)

    def records(self, **filters: Any) -> list[StageRecord]:
        return self.backend.records(proposal=self.proposal, **filters)

    def latest(self, label: str, member_key: str | None = None) -> StageRecord | None:
        """The record that supersedes the others under a label (a slot)."""
        return self.backend.latest(label, self.proposal, member_key=member_key)

    def batch(self, label: str) -> list[StageRecord]:
        """The batch table under a label: the latest record per member key."""
        return self.backend.batch(label, self.proposal)

    def members_to_retry(self, label: str) -> list[StageRecord]:
        """The failed or cancelled latest record of each member that never completed."""
        return self.backend.members_to_retry(label, self.proposal)

    def wait(
        self, records: Iterable[StageRecord | str], timeout: float = 60.0
    ) -> list[StageRecord]:
        ids = [r if isinstance(r, str) else r.id for r in records]
        return self.backend.wait(ids, timeout=timeout)

    def cancel(self, record: StageRecord | str) -> None:
        self.backend.cancel(record if isinstance(record, str) else record.id)

    def recompute(self, record: StageRecord | str) -> StageRecord:
        return self.backend.recompute(record if isinstance(record, str) else record.id)

    def output(
        self,
        ref: OutputRef | StageRecord,
        output: str | None = None,
        key: str | None = None,
    ) -> Any:
        if isinstance(ref, StageRecord):
            ref = ref.ref(output, key)
        return self.backend.output(ref)

    def view(self, ref: OutputRef, **spec: Any) -> dict[str, Any]:
        return self.backend.view(ref, ViewSpec(**spec))

    def write_out(self, ref: OutputRef, into: Path | None = None) -> Path:
        return self.backend.write_out(ref, into)

    def drop(self, ref: OutputRef) -> None:
        self.backend.drop(ref)

    def publish(self, ref: OutputRef, publisher: str, **kwargs: Any) -> str:
        return self.backend.publish(ref, publisher, **kwargs)

    def provenance(self, record: StageRecord | str) -> dict[str, Any]:
        return self.backend.provenance(record if isinstance(record, str) else record.id)


class WorkflowHandle:
    """
    A pipeline with parameters set, and the client that cuts stages from it.

    Held on the client side only, like :func:`functools.partial`: every request it
    makes carries ``spec`` and ``params``, and the backend records nothing else
    of it. ``params`` are the values given; the backend fills the spec's defaults
    when it accepts a request.
    """

    def __init__(self, client: Client, spec: SpecId, params: dict[str, Any]) -> None:
        self.client = client
        self.spec = spec
        self.params = params

    def with_params(self, **params: Any) -> WorkflowHandle:
        """Another pipeline: these values changed or added."""
        return WorkflowHandle(self.client, self.spec, {**self.params, **params})

    def request(
        self,
        inputs: Mapping[str, Any] | None = None,
        *,
        outputs: Iterable[str] = (),
        label: str | None = None,
        member_key: str | None = None,
        origin: Origin | None = None,
    ) -> StageRequest:
        return StageRequest(
            spec=self.spec,
            params=self.params,
            inputs=dict(inputs or {}),
            outputs=tuple(outputs),
            instrument=self.client.instrument,
            proposal=self.client.proposal,
            submitter=self.client.submitter,
            label=label,
            member_key=member_key,
            origin=origin or Origin(),
        )

    def stage(
        self,
        *,
        inputs: Iterable[str] = (),
        outputs: Iterable[str] = (),
        label: str | None = None,
    ) -> StageHandle:
        """
        The part of the pipeline from ``inputs`` to ``outputs``.

        Inputs are parameters this handle leaves unset and intermediates the
        spec exposes; without ``outputs``, the spec's results. A session holds
        the stage from the first call on.
        """
        return StageHandle(self, tuple(inputs), tuple(outputs), label)


class StageHandle:
    """A stage cut from a pipeline with parameters set; a call is a stage record."""

    def __init__(
        self,
        workflow: WorkflowHandle,
        inputs: tuple[str, ...],
        outputs: tuple[str, ...],
        label: str | None,
    ) -> None:
        self.workflow = workflow
        self.inputs = inputs
        self.outputs = outputs
        self.label = label

    def request(
        self,
        values: Mapping[str, Any],
        *,
        member_key: str | None = None,
        origin: Origin | None = None,
    ) -> StageRequest:
        if set(values) != set(self.inputs):
            raise ValueError(
                f'the stage takes {sorted(self.inputs)}, given {sorted(values)}'
            )
        return self.workflow.request(
            values,
            outputs=self.outputs,
            label=self.label,
            member_key=member_key,
            origin=origin,
        )

    def compute(
        self, values: Mapping[str, Any] | None = None, **kwargs: Any
    ) -> StageRecord:
        """
        Submit one call. It runs at once in a session and is dispatched otherwise,
        and it waits for any pending output among ``values``.
        """
        return self.workflow.client.submit(self.request(values or {}, **kwargs))


def local_backend(
    root: Path | str,
    *,
    registry: Registry | str | None = None,
    sources: Iterable[DatasetSource] = (),
    throwaway: bool = False,
    publishers: Mapping[str, Publisher] = {},
) -> LocalBackend:
    """
    Local mode: backend, launcher, and data store in this process.

    ``sources`` is where datasets come from, a folder in the local application.
    With ``throwaway`` every run is a subprocess (the shared-mode shape), and
    ``registry`` must then be importable by name, ``module:function``.
    """
    root = Path(root)
    records = RecordStore(root / 'records.db')
    data = DataStore(records, root / 'data')
    launcher: Launcher
    if throwaway:
        launcher = SubprocessLauncher(
            registry if isinstance(registry, str) else ENTRY_POINT_REGISTRY, data
        )
        reg = launcher.registry
    else:
        reg = (
            import_object(registry)()
            if isinstance(registry, str)
            else (registry or Registry())
        )
        launcher = SessionLauncher(reg, data)
    return LocalBackend(records, data, reg, launcher, sources, publishers)


def local(
    root: Path | str,
    *,
    instrument: str,
    proposal: str,
    submitter: str,
    **backend_kwargs: Any,
) -> Client:
    """Local mode: client and backend in this process; see :func:`local_backend`."""
    return Client(
        local_backend(root, **backend_kwargs),
        instrument=instrument,
        proposal=proposal,
        submitter=submitter,
    )
