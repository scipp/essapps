# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The client interface: the backend's Python interface, which is the API (D9)."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .backend import Backend, Publisher, ValidationReport
from .binding import ENTRY_POINT_REGISTRY, Registry, import_object
from .datastore import DataStore
from .launcher import Launcher, SessionLauncher, SubprocessLauncher
from .records import RunRecord, RunRequest, RunStage, Status, Submission
from .sources import Dataset, DatasetSource
from .spec import (
    DatasetRef,
    Kind,
    Ref,
    Reference,
    SpecId,
    WorkflowSpec,
    data_ref_fields,
)
from .store import RecordStore
from .views import ViewSpec


class Candidate(BaseModel, frozen=True):
    """A row of the picker: what may fill a data-reference field, and how to show it."""

    ref: Reference
    kind: Kind | None = Field(
        default=None, description="None for a dataset, which satisfies any kind."
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

    @property
    def registry(self) -> Registry:
        return self.backend.registry

    def bind(self, spec: WorkflowSpec, factory: Any) -> None:
        """Bind a spec to code in this process (local mode)."""
        self.registry.bind(spec, factory)

    def request(
        self,
        spec: WorkflowSpec | SpecId,
        params: BaseModel | Mapping[str, Any] | None = None,
        *,
        label: str | None = None,
        member_key: str | None = None,
        stage: RunStage = 'run',
        contributions: Iterable[Ref | RunRecord] = (),
        submission: Submission | None = None,
    ) -> RunRequest:
        """
        A request for this instrument and proposal.

        ``stage='contribute'`` is a member run of a series and
        ``stage='combine'`` a combine request, whose ``params`` are then the
        finalize parameters and whose ``contributions`` are the records to
        combine, or references to their contribution outputs (D15).
        """
        spec_id = spec.id if isinstance(spec, WorkflowSpec) else spec
        if isinstance(params, BaseModel):
            params = params.model_dump(mode='json')
        return RunRequest(
            spec=spec_id,
            params=dict(params or {}),
            stage=stage,
            contributions=[self._contribution(spec_id, c) for c in contributions],
            instrument=self.instrument,
            proposal=self.proposal,
            submitter=self.submitter,
            label=label,
            member_key=member_key,
            submission=submission or Submission(),
        )

    def _contribution(self, spec_id: SpecId, of: Ref | RunRecord) -> Ref:
        """The reference to a record's contribution: the output the spec marks."""
        if isinstance(of, Ref):
            return of
        return of.ref(self.registry.spec(spec_id).contribution)

    def validate(self, request: RunRequest) -> ValidationReport:
        return self.backend.validate(request)

    def submit(self, request: RunRequest) -> RunRecord:
        return self.backend.submit_one(request)

    def submit_group(self, group: Mapping[str, RunRequest]) -> dict[str, RunRecord]:
        """Submit together; ``@name`` in a Ref names another member of the group."""
        return self.backend.submit(group)

    def run(
        self, spec: WorkflowSpec | SpecId, params: Any = None, **kwargs: Any
    ) -> RunRecord:
        return self.submit(self.request(spec, params, **kwargs))

    @property
    def sources(self) -> list[DatasetSource]:
        """Where datasets come from; the picker lists from every one of them."""
        return self.backend.sources

    def datasets(self) -> list[Dataset]:
        """
        Every dataset the sources know for this proposal, by identity.

        Arrival may be repeated and out of order (D7), so the first dataset of
        each identity wins; nothing is stored to make the list.
        """
        seen: dict[DatasetRef, Dataset] = {}
        for source in self.sources:
            for dataset in source.new_datasets(self.proposal):
                seen.setdefault(dataset.ref, dataset)
        return list(seen.values())

    def pick(self, kind: Kind | None = None) -> list[Candidate]:
        """
        The candidates for a data-reference field of this kind: the picker.

        Completed outputs from the record store and datasets from every source.
        Nothing is stored to make the list, and a further place to pick from is
        another dataset source, not a change here.
        """
        rows = [*self._picked_outputs(), *self._picked_datasets()]
        return [row for row in rows if kind is None or row.kind in (None, kind)]

    def _picked_outputs(self) -> Iterator[Candidate]:
        for record in self.records(status=Status.COMPLETED):
            if record.spec not in self.registry:
                continue
            outputs = data_ref_fields(self.registry.spec(record.spec).outputs)
            for ref in record.stored_outputs:
                if (data := outputs.get(ref.output)) is not None:
                    yield Candidate(
                        ref=ref,
                        kind=data.kind,
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
        return self.backend.records.get(record_id)

    def records(self, **filters: Any) -> list[RunRecord]:
        return self.backend.records.list(proposal=self.proposal, **filters)

    def latest(self, label: str, member_key: str | None = None) -> RunRecord | None:
        """The record that supersedes the others under a label (a slot, D10)."""
        return self.backend.records.latest(label, self.proposal, member_key=member_key)

    def batch(self, label: str) -> list[RunRecord]:
        """The batch table under a label: the latest record per member key."""
        return self.backend.records.batch(label, self.proposal)

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
        self, ref: Ref | RunRecord, output: str | None = None, key: str | None = None
    ) -> Any:
        if isinstance(ref, RunRecord):
            ref = ref.ref(output, key)
        return self.backend.output(ref)

    def view(self, ref: Ref, **spec: Any) -> dict[str, Any]:
        return self.backend.view(ref, ViewSpec(**spec))

    def write_out(self, ref: Ref) -> Path:
        return self.backend.data.write_out(ref)

    def drop(self, ref: Ref) -> None:
        self.backend.data.drop(ref)

    def publish(self, ref: Ref, publisher: Publisher, **kwargs: Any) -> str:
        return self.backend.publish(ref, publisher, **kwargs)

    def provenance(self, record: RunRecord | str) -> dict[str, Any]:
        return self.backend.provenance(record if isinstance(record, str) else record.id)


def local(
    root: Path | str,
    *,
    instrument: str,
    proposal: str,
    submitter: str,
    registry: Registry | str | None = None,
    sources: Iterable[DatasetSource] = (),
    throwaway: bool = False,
) -> Client:
    """
    Local mode: client, backend, launcher, session, and data store in this process.

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
    backend = Backend(records, data, reg, launcher, sources)
    return Client(
        backend, instrument=instrument, proposal=proposal, submitter=submitter
    )
