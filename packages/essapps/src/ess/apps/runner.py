# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The runner: materialize inputs, call the workflow, validate and store outputs.

One code path for both execution shapes. In a session the runner keeps the
callable between runs (the warm workflow); in a throwaway process it is
constructed, called once, and the process exits after writing a completion
marker. The runner never touches the record store and never sees a record: it
gets a record ID and parameters and reports a :class:`RunResult`.
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import json
import os
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from .binding import Binding, Factory, Workflow, import_object
from .records import Failure, RunResult, Status
from .spec import DataRef, Kind, Ref, SpecId, WorkflowSpec, data_ref_fields

MARKER = 'done.json'
JOB = 'job.json'


class Inputs(Protocol):
    def get(self, ref: Ref, kind: Kind) -> Any: ...


class Outputs(Protocol):
    def put(self, ref: Ref, value: Any) -> None: ...


def package_versions() -> dict[str, str]:
    versions = {}
    for dist in ('essapps', 'scipp', 'sciline'):
        with contextlib.suppress(importlib.metadata.PackageNotFoundError):
            versions[dist] = importlib.metadata.version(dist)
    return versions


def environment_name() -> str | None:
    return os.environ.get('CONDA_DEFAULT_ENV') or os.environ.get('VIRTUAL_ENV')


def _materialize(value: Any, ref: DataRef, inputs: Inputs) -> Any:
    if isinstance(value, list):
        return [_materialize(v, ref, inputs) for v in value]
    if isinstance(value, dict) and 'record' not in value:
        return {k: _materialize(v, ref, inputs) for k, v in value.items()}
    return inputs.get(Ref.model_validate(value), ref.kind)


def _store_output(record_id: str, name: str, value: Any, outputs: Outputs) -> list[Ref]:
    if isinstance(value, dict):
        refs = [Ref(record=record_id, output=name, key=str(k)) for k in value]
        for ref, v in zip(refs, value.values(), strict=True):
            outputs.put(ref, v)
        return refs
    if isinstance(value, list):
        refs = [
            Ref(record=record_id, output=name, key=str(i)) for i in range(len(value))
        ]
        for ref, v in zip(refs, value, strict=True):
            outputs.put(ref, v)
        return refs
    ref = Ref(record=record_id, output=name)
    outputs.put(ref, value)
    return [ref]


class Runner:
    """Executes runs; with ``keep`` the callable survives between runs."""

    def __init__(self, *, keep: bool) -> None:
        self._keep = keep
        self._warm: dict[SpecId, Workflow] = {}

    def _callable(self, spec: WorkflowSpec, factory: Factory) -> tuple[Workflow, bool]:
        if spec.id in self._warm:
            return self._warm[spec.id], True
        workflow = factory()
        if self._keep:
            self._warm[spec.id] = workflow
        return workflow, False

    def forget(self, spec_id: SpecId) -> None:
        self._warm.pop(spec_id, None)

    def run(
        self,
        record_id: str,
        params: dict[str, Any],
        binding: Binding,
        inputs: Inputs,
        outputs: Outputs,
    ) -> RunResult:
        """Execute the request ``params`` of ``record_id`` and report what happened."""
        if binding.factory is None:
            raise ValueError(f'{binding.spec.id} has no workflow to run')
        spec = binding.spec
        result = RunResult(
            status=Status.RUNNING,
            started=datetime.now(UTC),
            finished=datetime.now(UTC),
            package_versions=package_versions(),
            environment=environment_name(),
            binding=binding.how,
        )
        try:
            validated = spec.params.model_validate(params)
            result.resolved_params = validated.model_dump(mode='json')
            materialized = validated.model_dump()
            for name, ref in data_ref_fields(spec.params).items():
                if materialized.get(name) is not None:
                    materialized[name] = _materialize(materialized[name], ref, inputs)
            workflow, result.reused = self._callable(spec, binding.factory)
            returned = workflow(spec.params.model_validate(materialized))
            model = (
                returned
                if isinstance(returned, BaseModel)
                else spec.outputs.model_validate(returned)
            )
            self._store(record_id, result, spec, model, outputs)
            result.status = Status.COMPLETED
        except Exception as e:
            result.status = Status.FAILED
            result.failure = Failure(
                kind='validation'
                if isinstance(e, ValidationError)
                else type(e).__name__,
                message=str(e),
                traceback=traceback.format_exc(),
            )
        result.finished = datetime.now(UTC)
        return result

    def _store(
        self,
        record_id: str,
        result: RunResult,
        spec: WorkflowSpec,
        model: BaseModel,
        outputs: Outputs,
    ) -> None:
        stored = data_ref_fields(spec.outputs)
        for name in type(model).model_fields:
            value = getattr(model, name)
            if value is None:
                continue
            if name in stored:
                result.stored_outputs += _store_output(record_id, name, value, outputs)
            else:
                result.outputs[name] = model.model_dump(mode='json', include={name})[
                    name
                ]


class FileInputs:
    """Inputs for a throwaway runner: locations resolved by the backend at dispatch."""

    def __init__(self, locations: dict[str, Path], load: Any) -> None:
        self._locations = locations
        self._load = load

    def get(self, ref: Ref, kind: Kind) -> Any:
        path = self._locations[str(ref)]
        return self._load(path) if kind is Kind.ARRAY else path


class WorkdirOutputs:
    """Outputs of a throwaway runner: written into its work directory."""

    def __init__(self, workdir: Path, save: Any) -> None:
        self.workdir = workdir
        self._save = save
        self.paths: dict[str, Path] = {}

    def put(self, ref: Ref, value: Any) -> None:
        name = ref.output if ref.key is None else f'{ref.output}/{ref.key}'
        self.paths[str(ref)] = self._save(value, self.workdir / name)


def main(workdir: Path) -> None:
    """Entry point of a throwaway runner process: ``python -m ess.apps.runner DIR``."""
    from .datastore import Serializers

    job = json.loads((workdir / JOB).read_text())
    registry = import_object(job['registry'])()
    binding = registry.binding(SpecId.model_validate(job['spec']))
    serializers = Serializers()
    inputs = FileInputs(
        {k: Path(v) for k, v in job['locations'].items()}, serializers.load
    )
    outputs = WorkdirOutputs(workdir, serializers.save)
    result = Runner(keep=False).run(
        job['record'], job['params'], binding, inputs, outputs
    )
    marker = {
        'result': result.model_dump(mode='json'),
        'paths': {k: str(v) for k, v in outputs.paths.items()},
    }
    tmp = workdir / (MARKER + '.tmp')
    tmp.write_text(json.dumps(marker))
    tmp.replace(workdir / MARKER)


if __name__ == '__main__':
    main(Path(sys.argv[1]))
