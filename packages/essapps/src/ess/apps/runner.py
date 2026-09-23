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
from .records import Accumulate, Failure, RunResult, Status
from .spec import (
    ArraySpec,
    Format,
    OutputRef,
    Ref,
    SpecId,
    WorkflowSpec,
    as_ref,
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

    ``workflow`` is the request's workflow ID, which names the stage a session
    holds; ``params`` are the request's parameters, defaults filled, ``vary``
    the ones the stage takes as inputs, and ``supplied`` the intermediates
    supplied in place of what computes them.
    """

    workflow: str
    params: dict[str, Any]
    supplied: dict[str, Any]
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

    ``stages`` caps how many stages, and how many accumulators, it holds at once.
    A runner without ``keep`` builds the stage a request names, calls it once,
    and holds nothing.

    Checksums survive between runs as well: a file is hashed once per (path,
    size, mtime). A session that reruns a workflow over the same hundreds of
    megabytes therefore spends the sha256 once rather than on every rerun. A
    throwaway runner runs once, so it hashes each file it reads once either way.
    """

    def __init__(self, *, keep: bool, stages: int = 4) -> None:
        self._keep = keep
        self._workflows: dict[SpecId, Workflow] = {}
        self._stages = Stages(limit=stages) if keep else None
        self._digests: dict[tuple[Path, int, int], str] = {}

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
                [
                    name
                    for name, info in spec.params.model_fields.items()
                    if name not in job.vary
                    and (name in fixed_values or not info.is_required())
                ],
                'Fixed',
            ).model_validate(fixed_values)
            varied = submodel(spec.params, job.vary, 'Varied').model_validate(
                {name: job.params[name] for name in job.vary}
            )
            result.checksums = self._checksums({**job.params, **job.supplied}, inputs)
            workflow = self._workflow(spec, binding.factory)
            outputs_ = tuple(job.outputs)
            stage_inputs = (*job.vary, *job.supplied)

            def build() -> StageCall:
                return workflow.stage(fixed, stage_inputs, outputs_, inputs)

            if self._stages is None or isinstance(workflow, FunctionWorkflow):
                # A plain function has no graph to cut, so its stage holds nothing.
                call = build()
            else:
                held = {str(ref) for ref in dataset_refs(fixed_values)}
                name = (
                    job.workflow,
                    tuple(sorted(job.vary)),
                    tuple(sorted(job.supplied)),
                    outputs_,
                    tuple(sorted(c for c in result.checksums.items() if c[0] in held)),
                )
                call, result.reused = self._stages.stage(name, build)
            values = {}
            for name, value in job.supplied.items():
                values[name], reused = self._intermediate(
                    spec, workflow, job.workflow, name, value, inputs
                )
                result.reused |= reused
            self._store(
                record_id,
                result,
                spec,
                outputs_,
                dict(call(varied, values, inputs)),
                outputs,
            )
            result.status = Status.COMPLETED
        except Exception as e:
            result.status = Status.FAILED
            result.failure = _failure(e)
        result.finished = datetime.now(UTC)
        return result

    def _intermediate(
        self,
        spec: WorkflowSpec,
        workflow: Workflow,
        workflow_id: str,
        name: str,
        value: Any,
        inputs: Inputs,
    ) -> tuple[Any, bool]:
        """
        The object an intermediate input stands for, and whether held state served it.

        A reference is loaded in the form the intermediate's format says; a
        literal was put in place of its reference at dispatch; an
        :class:`Accumulate` is loaded element by element and accumulated with
        the workflow's accumulator for the input, which a session may hold.
        """
        field = data_fields(spec.outputs).get(name)
        if field is None:
            return value, False

        def load(ref: Ref) -> Any:
            if field.format is Format.SCIPP:
                return inputs.array(ref)
            return inputs.path(ref)

        if (ref := as_ref(value)) is not None:
            return load(ref), False
        refs = Accumulate.model_validate(value).accumulate
        if self._stages is None:
            accumulator = workflow.accumulator(name)
            for element in refs:
                accumulator.push(load(element))
            return accumulator.value, False
        return self._stages.accumulate(
            (workflow_id, name), refs, lambda: workflow.accumulator(name), load
        )

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
