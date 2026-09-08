# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Launchers: where a run executes (D3).

Two execution shapes. A session launcher runs in this process, inputs from the
private cache and outputs staying there, nothing written. A subprocess launcher
is the throwaway shape: outputs go to disk with a completion marker before the
process exits, and the backend reconciles from the marker.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Protocol

from .binding import Registry
from .datastore import DataStore
from .records import Failure, RunRecord, Status
from .runner import JOB, MARKER, FileInputs, Runner
from .spec import Kind, Ref, SpecId


class Launcher(Protocol):
    def can_run(self, spec: SpecId) -> bool: ...

    def start(self, record: RunRecord, locations: dict[Ref, Path]) -> RunRecord:
        """Begin executing; returns the record terminal (session) or dispatched."""
        ...

    def poll(self, record: RunRecord) -> RunRecord:
        """Reconcile a dispatched record; returns it unchanged if still running."""
        ...

    def cancel(self, record: RunRecord) -> None: ...


class _CacheOutputs:
    def __init__(self, data: DataStore) -> None:
        self._data = data

    def put(self, ref: Ref, value: Any) -> None:
        self._data.put(ref, value, to_disk=False)


class SessionLauncher:
    """Runs in this process with a warm workflow per spec; the session shape."""

    def __init__(self, registry: Registry, data: DataStore) -> None:
        self.registry = registry
        self._data = data
        self.runner = Runner(keep=True)

    def can_run(self, spec: SpecId) -> bool:
        return spec in self.registry

    def start(self, record: RunRecord, locations: dict[Ref, Path]) -> RunRecord:
        return self.runner.run(
            record, self.registry[record.spec], self._data, _CacheOutputs(self._data)
        )

    def poll(self, record: RunRecord) -> RunRecord:
        return record

    def cancel(self, record: RunRecord) -> None:
        pass


class SubprocessLauncher:
    """
    The throwaway shape: one process per run, outputs to disk, completion marker.

    ``registry`` names, as ``module:function``, how the subprocess builds its
    registry, which is what "the specs this environment can run" means.
    """

    def __init__(
        self, registry: str, data: DataStore, *, python: str = sys.executable
    ) -> None:
        self.registry_path = registry
        self._registry = _import(registry)()
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

    def start(self, record: RunRecord, locations: dict[Ref, Path]) -> RunRecord:
        workdir = self.workdir(record)
        workdir.mkdir(parents=True, exist_ok=True)
        job = {
            'record': record.model_dump(mode='json'),
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
            finished = RunRecord.model_validate(done['record'])
            for ref_str, path in done['paths'].items():
                self._data.adopt(_parse_ref(ref_str), Path(path), store_owned=True)
            proc = self._procs.pop(record.id, None)
            if proc is not None:
                proc.wait()
            return finished
        proc = self._procs.get(record.id)
        if proc is not None and proc.poll() is not None:
            record = record.model_copy()
            record.status = Status.FAILED
            record.failure = Failure(
                kind='runner-exit',
                message=f'runner exited with {proc.returncode}, no completion marker',
                traceback=(self.workdir(record) / 'stderr.txt').read_text(),
            )
            self._procs.pop(record.id)
        return record

    def cancel(self, record: RunRecord) -> None:
        proc = self._procs.pop(record.id, None)
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()


def _import(path: str) -> Any:
    from .binding import import_object

    return import_object(path)


def _parse_ref(text: str) -> Ref:
    record, rest = text.split('.', 1)
    if rest.endswith(']'):
        output, key = rest[:-1].split('[', 1)
        return Ref(record=record, output=output, key=key)
    return Ref(record=record, output=rest)


__all__ = ['FileInputs', 'Kind', 'Launcher', 'SessionLauncher', 'SubprocessLauncher']
