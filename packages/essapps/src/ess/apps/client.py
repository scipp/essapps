# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The client interface: the backend's Python interface, which is the API (D9)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .backend import Backend, ValidationReport
from .binding import ENTRY_POINT_REGISTRY, Registry, import_object
from .datastore import DataStore
from .launcher import Launcher, SessionLauncher, SubprocessLauncher
from .records import RunRecord, RunRequest, Status
from .spec import Ref, SpecId, WorkflowSpec
from .store import RecordStore
from .views import ViewSpec


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
        slot: str | None = None,
        batch: str | None = None,
        member_key: str | None = None,
    ) -> RunRequest:
        spec_id = spec.id if isinstance(spec, WorkflowSpec) else spec
        if isinstance(params, BaseModel):
            params = params.model_dump(mode='json')
        return RunRequest(
            spec=spec_id,
            params=dict(params or {}),
            instrument=self.instrument,
            proposal=self.proposal,
            submitter=self.submitter,
            slot=slot,
            batch=batch,
            member_key=member_key,
        )

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

    def file(self, path: Path | str) -> Ref:
        """The reference standing for a file on this machine."""
        record = self.backend.file_record(
            Path(path),
            instrument=self.instrument,
            proposal=self.proposal,
            submitter=self.submitter,
        )
        return Ref(record=record.id, output='file')

    def files(self, folder: Path | str, pattern: str = '*') -> dict[str, Ref]:
        """One file record per file in a folder, by file name; no bytes moved."""
        return {
            p.name: self.file(p)
            for p in sorted(Path(folder).glob(pattern))
            if p.is_file()
        }

    def record(self, record_id: str) -> RunRecord:
        return self.backend.records.get(record_id)

    def records(self, **filters: Any) -> list[RunRecord]:
        return self.backend.records.list(proposal=self.proposal, **filters)

    def latest(self, slot: str) -> RunRecord | None:
        return self.backend.records.latest(slot, self.proposal)

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
            ref = Ref(record=ref.id, output=output or _first_output(ref), key=key)
        return self.backend.output(ref)

    def view(self, ref: Ref, **spec: Any) -> dict[str, Any]:
        return self.backend.view(ref, ViewSpec(**spec))

    def write_out(self, ref: Ref) -> Path:
        return self.backend.data.write_out(ref)

    def drop(self, ref: Ref) -> None:
        self.backend.data.drop(ref)


def _first_output(record: RunRecord) -> str:
    if record.status != Status.COMPLETED:
        raise ValueError(f'{record.id} is {record.status.value}: {record.failure}')
    names = sorted(record.output_names())
    if len(names) != 1:
        raise ValueError(f'{record.id} has outputs {names}; name one')
    return names[0]


def local(
    root: Path | str,
    *,
    instrument: str,
    proposal: str,
    submitter: str,
    registry: Registry | str | None = None,
    throwaway: bool = False,
) -> Client:
    """
    Local mode: client, backend, launcher, session, and data store in this process.

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
    backend = Backend(records, data, reg, launcher)
    return Client(
        backend, instrument=instrument, proposal=proposal, submitter=submitter
    )
