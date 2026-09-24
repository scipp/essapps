# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The runner: call the workflow with its inputs, validate and store the outputs.

One code path for both execution shapes. The callable is stateless, so in a
session the runner keeps it only to save building it again, and what survives
between runs is what the session decided to hold: the stages of
:class:`ess.apps.stages.Stages`. In a throwaway process the callable is
constructed, called once, and the process exits after writing a completion
marker. The runner never touches the record store and never sees a record: it
gets a record ID and parameters and reports a :class:`RunResult`.

The callable receives the validated request, references included, and an
:class:`Inputs` to get at the bytes; the two shapes differ only in what serves
those: a work directory the backend filled at dispatch, or the session's data
store. The runner never turns a reference into anything itself.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.metadata
import json
import os
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from .binding import (
    Binding,
    Factory,
    FunctionWorkflow,
    Inputs,
    StageCall,
    Workflow,
    as_workflow,
    import_object,
)
from .records import Failure, RunResult, Status
from .spec import (
    ArraySpec,
    OutputRef,
    Ref,
    SpecId,
    WorkflowSpec,
    data_fields,
    dataset_refs,
    submodel,
)
from .stages import Stages

MARKER = 'done.json'
JOB = 'job.json'


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


class Job(BaseModel, frozen=True):
    """
    What a runner is given: a run request with its literal references inlined.

    ``params`` are the request's parameters, defaults filled, and ``vary`` the
    ones the stage takes as inputs, the hint the request was submitted with.
    """

    params: dict[str, Any]
    vary: tuple[str, ...]
    outputs: tuple[str, ...]


class OutputShapeError(Exception):
    """An output does not have the structure its spec declares."""


def file_checksum(path: Path) -> str:
    """The sha256 of a file's bytes."""
    with path.open('rb') as file:
        return hashlib.file_digest(file, 'sha256').hexdigest()


def _elements(value: Any) -> list[Any]:
    """What an output field holds: one value, or the elements of a collection."""
    if isinstance(value, dict):
        return list(value.values())
    if isinstance(value, list):
        return value
    return [value]


def _check_array(name: str, value: Any, spec: ArraySpec) -> None:
    """An array output against the structure its spec declares."""
    for element in _elements(value):
        dims = tuple(getattr(element, 'dims', ()))
        coords = getattr(element, 'coords', {})
        if missing := tuple(d for d in spec.dims if d not in dims):
            raise OutputShapeError(
                f'output {name!r} has dims {dims}, without the declared {missing}'
            )
        if absent := tuple(c for c in spec.coords if c not in coords):
            raise OutputShapeError(
                f'output {name!r} is without the declared coords {absent}'
            )


def _failure(error: Exception) -> Failure:
    """Why the run failed, under a kind a caller can branch on."""
    if isinstance(error, ValidationError):
        kind = 'validation'
    elif isinstance(error, OutputShapeError):
        kind = 'output-shape'
    else:
        kind = type(error).__name__
    return Failure(kind=kind, message=str(error), traceback=traceback.format_exc())


def _store_output(
    record_id: str, name: str, value: Any, outputs: Outputs
) -> list[OutputRef]:
    if isinstance(value, dict):
        refs = [OutputRef(record=record_id, output=name, key=str(k)) for k in value]
        for ref, v in zip(refs, value.values(), strict=True):
            outputs.put(ref, v)
        return refs
    if isinstance(value, list):
        refs = [
            OutputRef(record=record_id, output=name, key=str(i))
            for i in range(len(value))
        ]
        for ref, v in zip(refs, value, strict=True):
            outputs.put(ref, v)
        return refs
    ref = OutputRef(record=record_id, output=name)
    outputs.put(ref, value)
    return [ref]


class Runner:
    """
    Executes runs; with ``keep`` it is a session and holds stages between runs.

    ``stages`` caps how many stages it holds at once.
    A runner without ``keep`` builds the stage a request names, calls it once,
    and holds nothing.

    Checksums survive between runs as well: a file is hashed once per (path,
    size, mtime). A session that reruns a workflow over the same hundreds of
    megabytes therefore spends the sha256 once rather than on every rerun. A
    throwaway runner runs once, so it hashes each file it reads once either way.
    A session also keeps the checksum each dataset had when it read it, and a
    dataset whose bytes changed since drops every stage it holds: a held stage
    knows its datasets, the runs of a sum included, by identity alone.
    """

    def __init__(self, *, keep: bool, stages: int = 4) -> None:
        self._keep = keep
        self._workflows: dict[SpecId, Workflow] = {}
        self._stages = Stages(limit=stages) if keep else None
        self._digests: dict[tuple[Path, int, int], str] = {}
        self._read: dict[str, str] = {}

    def _checksum(self, path: Path) -> str:
        stat = path.stat()
        key = (path, stat.st_size, stat.st_mtime_ns)
        if key not in self._digests:
            self._digests[key] = file_checksum(path)
        return self._digests[key]

    def _checksums(self, values: dict[str, Any], inputs: Inputs) -> dict[str, str]:
        """
        The checksum of every local file the run reads, by reference.

        A dataset is the only input whose bytes the framework did not write, so a
        recompute can only tell whether it read the same bytes if we take these.
        The key is the reference rather than a parameter path, because a dataset
        two parameters name is one file; which parameter read it is in the
        request.
        """
        checksums = {}
        for ref in dataset_refs(values):
            located = inputs.path(ref)
            if located.is_file():
                checksums[str(ref)] = self._checksum(located)
        return checksums

    def _workflow(self, spec: WorkflowSpec, factory: Factory) -> Workflow:
        """The workflow; kept in a session only to save building it again."""
        if spec.id in self._workflows:
            return self._workflows[spec.id]
        workflow = as_workflow(factory(), spec)
        if self._keep:
            self._workflows[spec.id] = workflow
        return workflow

    def forget(self, spec_id: SpecId) -> None:
        self._workflows.pop(spec_id, None)
        if self._stages is not None:
            self._stages.clear()

    def run(
        self,
        record_id: str,
        job: Job,
        binding: Binding,
        inputs: Inputs,
        outputs: Outputs,
    ) -> RunResult:
        """Execute the run ``job`` describes and report what happened."""
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
            fixed_values = {k: v for k, v in job.params.items() if k not in job.vary}
            fixed = submodel(
                spec.params,
                [name for name in spec.params.model_fields if name not in job.vary],
                'Fixed',
            ).model_validate(fixed_values)
            varied = submodel(spec.params, job.vary, 'Varied').model_validate(
                {name: job.params[name] for name in job.vary}
            )
            result.checksums = self._checksums(job.params, inputs)
            workflow = self._workflow(spec, binding.factory)
            outputs_ = tuple(job.outputs)

            def build() -> StageCall:
                return workflow.stage(fixed, job.vary, outputs_, inputs)

            if self._stages is None or isinstance(workflow, FunctionWorkflow):
                # A plain function has no graph to cut, so its stage holds nothing.
                call = build()
            else:
                changed = any(
                    self._read.get(ref, checksum) != checksum
                    for ref, checksum in result.checksums.items()
                )
                if changed:
                    self._stages.clear()
                self._read |= result.checksums
                name = (
                    spec.id,
                    json.dumps(fixed_values, sort_keys=True),
                    tuple(sorted(job.vary)),
                    outputs_,
                )
                call, result.reused = self._stages.stage(name, build)
            self._store(
                record_id, result, spec, outputs_, dict(call(varied, inputs)), outputs
            )
            result.status = Status.COMPLETED
        except Exception as e:
            result.status = Status.FAILED
            result.failure = _failure(e)
        result.finished = datetime.now(UTC)
        return result

    def _store(
        self,
        record_id: str,
        result: RunResult,
        spec: WorkflowSpec,
        selected: tuple[str, ...],
        values: dict[str, Any],
        outputs: Outputs,
    ) -> None:
        """
        Check each selected output against its declared structure, then store it.

        Literal outputs are validated through the outputs model and kept inline;
        data outputs come back as objects, are checked against their declared
        structure, and go to the data store. An optional output may be absent.
        """
        data = data_fields(spec.outputs)
        literal_names = [name for name in selected if name not in data]
        literals = submodel(spec.outputs, literal_names, 'Literals').model_validate(
            {name: values.get(name) for name in literal_names if name in values}
        )
        result.outputs = literals.model_dump(mode='json', exclude_none=True)
        for name in selected:
            if (ref := data.get(name)) is None:
                continue
            value = values.get(name)
            if value is None:
                if spec.outputs.model_fields[name].is_required():
                    raise OutputShapeError(f'output {name!r} is missing')
                continue
            if ref.array is not None:
                _check_array(name, value, ref.array)
            result.stored_outputs += _store_output(record_id, name, value, outputs)


class FileInputs:
    """Inputs of a throwaway runner: references the backend located at dispatch."""

    def __init__(self, locations: dict[str, Path], load: Any) -> None:
        self._locations = locations
        self._load = load

    def path(self, ref: Ref) -> Path:
        return self._locations[str(ref)]

    def array(self, ref: Ref) -> Any:
        return self._load(self.path(ref))


class WorkdirOutputs:
    """Outputs of a throwaway runner: written into its work directory."""

    def __init__(self, workdir: Path, save: Any) -> None:
        self.workdir = workdir
        self._save = save
        self.written: list[tuple[OutputRef, Path]] = []

    def put(self, ref: OutputRef, value: Any) -> None:
        name = ref.output if ref.key is None else f'{ref.output}/{ref.key}'
        self.written.append((ref, self._save(value, self.workdir / name)))


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
        job['record'], Job.model_validate(job['job']), binding, inputs, outputs
    )
    marker = {
        'result': result.model_dump(mode='json'),
        'outputs': [
            {'ref': ref.model_dump(mode='json'), 'path': str(path)}
            for ref, path in outputs.written
        ],
    }
    tmp = workdir / (MARKER + '.tmp')
    tmp.write_text(json.dumps(marker))
    tmp.replace(workdir / MARKER)


if __name__ == '__main__':
    main(Path(sys.argv[1]))
