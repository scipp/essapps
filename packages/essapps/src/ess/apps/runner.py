# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The runner: materialize inputs, call the workflow, validate and store outputs.

One code path for both execution shapes. In a session the runner keeps the
callable between runs (the warm workflow); in a throwaway process it is
constructed, called once, and the process exits after writing a completion
marker. The runner never touches the record store and never sees a record: it
gets a record ID and parameters and reports a :class:`RunResult`.

A request that runs part of a workflow with a declared contribution (D15) takes
the same path: a member run calls contribute and stores its contribution as the
only output, and a combine request materializes the contributions it references,
calls combine over them and finalize on the result, and stores both.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.metadata
import json
import os
import sys
import traceback
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from .binding import Binding, Factory, Workflow, combining, import_object
from .records import Failure, RunResult, RunStage, Status
from .spec import (
    DataRef,
    DatasetRef,
    Kind,
    Ref,
    Reference,
    SpecId,
    WorkflowSpec,
    as_ref,
    data_ref_fields,
    finalize_model,
    walk_refs,
)

MARKER = 'done.json'
JOB = 'job.json'


class Inputs(Protocol):
    def get(self, ref: Reference, kind: Kind) -> Any: ...


class Outputs(Protocol):
    def put(self, ref: Reference, value: Any) -> None: ...


def package_versions() -> dict[str, str]:
    versions = {}
    for dist in ('essapps', 'scipp', 'sciline'):
        with contextlib.suppress(importlib.metadata.PackageNotFoundError):
            versions[dist] = importlib.metadata.version(dist)
    return versions


def environment_name() -> str | None:
    return os.environ.get('CONDA_DEFAULT_ENV') or os.environ.get('VIRTUAL_ENV')


def _materialize(value: Any, ref: DataRef, inputs: Inputs) -> Any:
    if (found := as_ref(value)) is not None:
        return inputs.get(found, ref.kind)
    if isinstance(value, list):
        return [_materialize(v, ref, inputs) for v in value]
    if isinstance(value, dict):
        return {k: _materialize(v, ref, inputs) for k, v in value.items()}
    return value


def _checksums(params: dict[str, Any], inputs: Inputs) -> dict[str, str]:
    """
    The checksum of every local file the run reads, by parameter path.

    A dataset is the only input whose bytes the framework did not write, so a
    recompute can only tell whether it read the same bytes if we take these.
    """
    checksums = {}
    for path, ref in walk_refs(params):
        if not isinstance(ref, DatasetRef):
            continue
        located = inputs.get(ref, Kind.OPAQUE)
        if isinstance(located, Path) and located.is_file():
            digest = hashlib.sha256()
            with located.open('rb') as file:
                while chunk := file.read(1 << 20):
                    digest.update(chunk)
            checksums[path] = digest.hexdigest()
    return checksums


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
        *,
        stage: RunStage = 'run',
        contributions: Iterable[Ref] = (),
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
            # A combine request carries the finalize parameters and no others, so
            # it is validated against a model of exactly those fields.
            params_model = finalize_model(spec) if stage == 'combine' else spec.params
            validated = params_model.model_validate(params)
            result.resolved_params = validated.model_dump(mode='json')
            result.checksums = _checksums(params, inputs)
            materialized = validated.model_dump()
            for name, ref in data_ref_fields(params_model).items():
                if materialized.get(name) is not None:
                    materialized[name] = _materialize(materialized[name], ref, inputs)
            workflow, result.reused = self._callable(spec, binding.factory)
            returned = self._call(
                spec,
                workflow,
                stage,
                params_model.model_validate(materialized),
                [inputs.get(ref, Kind.ARRAY) for ref in contributions],
            )
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

    def _call(
        self,
        spec: WorkflowSpec,
        workflow: Workflow,
        stage: RunStage,
        params: BaseModel,
        contributions: list[Any],
    ) -> Any:
        """The entry points this stage runs, and the outputs they produce (D15)."""
        if stage == 'run':
            return workflow(params)
        if spec.contribution is None:
            raise ValueError(f'{spec.id} declares no contribution to {stage}')
        entry = combining(workflow)
        if stage == 'contribute':
            return {spec.contribution: entry.contribute(params)}
        combined = entry.combine(contributions)
        return {spec.contribution: combined, **entry.finalize(combined, params)}

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

    def get(self, ref: Reference, kind: Kind) -> Any:
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
        job['record'],
        job['params'],
        binding,
        inputs,
        outputs,
        stage=job['stage'],
        contributions=[Ref.model_validate(c) for c in job['contributions']],
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
