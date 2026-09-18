# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Launchers: where a run executes (D3).

Two execution shapes. A session launcher runs in this process, inputs from the
private cache and outputs staying there, nothing written. A subprocess launcher
is the throwaway shape: outputs go to disk with a completion marker before the
process exits, and the backend reconciles from the marker. Which shape a
launcher is shows in its interface as ``needs_disk_inputs``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from .binding import Registry, import_object
from .datastore import DataStore
from .records import Failure, RunRecord, RunResult, Status
from .runner import JOB, MARKER, Runner
from .spec import OutputRef, Ref, SpecId


class Launcher(Protocol):
    needs_disk_inputs: bool
    """Whether inputs must have a disk copy, resolved to a path at dispatch."""

    def can_run(self, spec: SpecId) -> bool: ...

    def start(
        self,
        record: RunRecord,
        params: dict[str, Any],
        locations: dict[Ref, Path],
    ) -> RunRecord:
        """Begin executing; returns the record terminal (session) or dispatched."""
        ...

    def poll(self, record: RunRecord) -> RunRecord:
        """Reconcile a dispatched record; returns it unchanged if still running."""
        ...

    def cancel(self, record: RunRecord) -> None: ...


class _CacheOutputs:
    def __init__(self, data: DataStore) -> None:
        self._data = data

    def put(self, ref: OutputRef, value: Any) -> None:
        self._data.put(ref, value, to_disk=False)


class _SessionInputs:
    """Outputs from the private cache, datasets from where dispatch located them."""

    def __init__(self, data: DataStore, locations: Mapping[Ref, Path]) -> None:
        self._data = data
        self._locations = locations

    def path(self, ref: Ref) -> Path:
        located = self._locations.get(ref)
        return self._data.path(ref) if located is None else located

    def array(self, ref: Ref) -> Any:
        located = self._locations.get(ref)
        if located is None:
            return self._data.array(ref)
        return self._data.serializers.load(located)


class SessionLauncher:
    """Runs in this process, the runner holding the stages; the session shape."""

    needs_disk_inputs = False

    def __init__(self, registry: Registry, data: DataStore) -> None:
        self.registry = registry
        self._data = data
        self.runner = Runner(keep=True)

    def can_run(self, spec: SpecId) -> bool:
        return spec in self.registry

    def start(
        self,
        record: RunRecord,
        params: dict[str, Any],
        locations: dict[Ref, Path],
    ) -> RunRecord:
        result = self.runner.run(
            record.id,
            params,
            self.registry.binding(record.spec),
            _SessionInputs(self._data, locations),
            _CacheOutputs(self._data),
            label=record.request.label,
            member_key=record.request.member_key,
        )
        record = record.model_copy()
        record.apply(result)
        return record

    def poll(self, record: RunRecord) -> RunRecord:
        return record

    def cancel(self, record: RunRecord) -> None:
        pass


class SubprocessLauncher:
    """
    The throwaway shape: one process per run, outputs to disk, completion marker.

    ``registry`` names, as ``module:function``, how the subprocess builds its
    registry, which is what "the specs this environment can run" means.
    Reconciliation reads the marker, then the process; a run whose process is
    gone without a marker has failed.
    """

    needs_disk_inputs = True

    def __init__(
        self, registry: str, data: DataStore, *, python: str = sys.executable
    ) -> None:
        self.registry_path = registry
        self._registry: Registry = import_object(registry)()
        self._data = data
        self._python = python
        self._procs: dict[str, subprocess.Popen[bytes]] = {}

    @property
    def registry(self) -> Registry:
        return self._registry

    def can_run(self, spec: SpecId) -> bool:
        return spec in self._registry

    def workdir(self, record: RunRecord) -> Path:
        return self._data.root / record.id

    def start(
        self,
        record: RunRecord,
        params: dict[str, Any],
        locations: dict[Ref, Path],
    ) -> RunRecord:
        workdir = self.workdir(record)
        workdir.mkdir(parents=True, exist_ok=True)
        job = {
            'record': record.id,
            'spec': record.spec.model_dump(),
            'params': params,
            'registry': self.registry_path,
            'locations': {str(k): str(v) for k, v in locations.items()},
        }
        (workdir / JOB).write_text(json.dumps(job))
        with (
            (workdir / 'stdout.txt').open('wb') as out,
            (workdir / 'stderr.txt').open('wb') as err,
        ):
            proc = subprocess.Popen(  # noqa: S603
                [self._python, '-m', 'ess.apps.runner', str(workdir)],
                stdout=out,
                stderr=err,
            )
        self._procs[record.id] = proc
        record = record.model_copy()
        record.status = Status.DISPATCHED
        record.launcher_job = str(proc.pid)
        return record

    def poll(self, record: RunRecord) -> RunRecord:
        marker = self.workdir(record) / MARKER
        if marker.exists():
            done = json.loads(marker.read_text())
            for output in done['outputs']:
                ref = OutputRef.model_validate(output['ref'])
                self._data.adopt(ref, Path(output['path']), store_owned=True)
            self._reap(record)
            record = record.model_copy()
            record.apply(RunResult.model_validate(done['result']))
            return record
        if self._alive(record):
            return record
        record = record.model_copy()
        record.status = Status.FAILED
        record.failure = Failure(
            kind='runner-exit',
            message='runner process is gone without a completion marker',
            traceback=(self.workdir(record) / 'stderr.txt').read_text(),
        )
        return record

    def _alive(self, record: RunRecord) -> bool:
        proc = self._procs.get(record.id)
        if proc is not None:
            if proc.poll() is None:
                return True
            self._reap(record)
            return False
        if record.launcher_job is None:
            return False
        try:
            os.kill(int(record.launcher_job), 0)
        except (ProcessLookupError, PermissionError):
            return False
        return True

    def _reap(self, record: RunRecord) -> None:
        proc = self._procs.pop(record.id, None)
        if proc is not None:
            proc.wait()

    def cancel(self, record: RunRecord) -> None:
        proc = self._procs.pop(record.id, None)
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
