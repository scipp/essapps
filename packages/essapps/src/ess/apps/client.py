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
from .records import Origin, RunRecord, RunRequest, Status, Template
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

    def request(
        self,
        template: Template | WorkflowSpec | SpecId,
        values: BaseModel | Mapping[str, Any] | None = None,
        *,
        label: str | None = None,
        member_key: str | None = None,
        origin: Origin | None = None,
    ) -> RunRequest:
        """
        A run request from a template, its blanks filled with ``values``.

        A spec stands for a template with nothing set and no blanks, so
        ``values`` are then the parameters of a plain run. The template's blanks
        are what the request varies. Nothing is stored until the request is
        submitted.
        """
        if not isinstance(template, Template):
            template = Template(spec=template)
        if isinstance(values, BaseModel):
            values = values.model_dump(mode='json', exclude_unset=True)
        return RunRequest(
            spec=template.spec,
            params=template.fill(**dict(values or {})),
            vary=template.blanks,
            outputs=template.outputs,
            instrument=self.instrument,
            proposal=self.proposal,
            submitter=self.submitter,
            label=template.name if label is None else label,
            member_key=member_key,
            origin=origin or Origin(),
        )

    def validate(self, request: RunRequest) -> ValidationReport:
        return self.backend.validate(request)

    def submit(self, request: RunRequest) -> RunRecord:
        return self.backend.submit({'request': request})['request']

    def submit_group(self, group: Mapping[str, RunRequest]) -> dict[str, RunRecord]:
        """Submit together; ``@name`` in a reference names a member of the group."""
        return self.backend.submit(group)

    def run(
        self,
        template: Template | WorkflowSpec | SpecId,
        values: BaseModel | Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> RunRecord:
        """
        Submit one request made from ``template``. It runs at once in a session
        and is dispatched otherwise, and it waits for any pending output among
        ``values``. Given a spec, this is a plain run.
        """
        return self.submit(self.request(template, values, **kwargs))

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

    def record(self, record_id: str) -> RunRecord:
        return self.backend.record(record_id)

    def records(self, **filters: Any) -> list[RunRecord]:
        return self.backend.records(proposal=self.proposal, **filters)

    def latest(self, label: str, member_key: str | None = None) -> RunRecord | None:
        """The record that supersedes the others under a label (a slot)."""
        return self.backend.latest(label, self.proposal, member_key=member_key)

    def batch(self, label: str) -> list[RunRecord]:
        """The batch table under a label: the latest record per member key."""
        return self.backend.batch(label, self.proposal)

    def members_to_retry(self, label: str) -> list[RunRecord]:
        """The failed or cancelled latest record of each member that never completed."""
        return self.backend.members_to_retry(label, self.proposal)

    def wait(
        self, records: Iterable[RunRecord | str], timeout: float = 60.0
    ) -> list[RunRecord]:
        ids = [r if isinstance(r, str) else r.id for r in records]
        return self.backend.wait(ids, timeout=timeout)

    def cancel(self, record: RunRecord | str) -> None:
        self.backend.cancel(record if isinstance(record, str) else record.id)

    def recompute(self, record: RunRecord | str) -> RunRecord:
        return self.backend.recompute(record if isinstance(record, str) else record.id)

    def output(
        self,
        ref: OutputRef | RunRecord,
        output: str | None = None,
        key: str | None = None,
    ) -> Any:
        if isinstance(ref, RunRecord):
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

    def provenance(self, record: RunRecord | str) -> dict[str, Any]:
        return self.backend.provenance(record if isinstance(record, str) else record.id)


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
