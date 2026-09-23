# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""What the runner does around the callable: checksums and output structure."""

from pathlib import Path
from typing import Any

import scipp as sc
from pydantic import BaseModel

from ess.apps.binding import Binding, Inputs
from ess.apps.examples import (
    LOAD,
    NORMALIZE,
    LoadParams,
    load_workflow,
    normalize_workflow,
    write_run,
)
from ess.apps.records import Status
from ess.apps.runner import FileInputs, Job, Runner
from ess.apps.spec import (
    Array,
    ArraySpec,
    OpaqueFile,
    Quantity,
    WorkflowSpec,
    dataset_ref,
)


class Collected:
    """Outputs of a run kept in memory; the runner never sees a store."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    def put(self, ref: Any, value: Any) -> None:
        self.values[str(ref)] = value


class TwoRunParams(BaseModel):
    """Two parameters that may name one dataset, as LoKI's transmission runs do."""

    background: OpaqueFile
    empty_beam: OpaqueFile


class TwoRunOutputs(BaseModel):
    total: Quantity


TWO_RUNS = WorkflowSpec(
    name='two-runs',
    version=1,
    title='Two runs',
    description='Reads two run files, which may be the same file.',
    params=TwoRunParams,
    outputs=TwoRunOutputs,
)


def two_run_workflow() -> Any:
    def run(params: TwoRunParams, inputs: Inputs) -> dict[str, Any]:
        background = sc.io.load_hdf5(inputs.path(params.background))
        empty_beam = sc.io.load_hdf5(inputs.path(params.empty_beam))
        data = background + empty_beam
        return {'total': Quantity(value=float(data.sum().value), unit=str(data.unit))}

    return run


def job(
    params: dict[str, Any], outputs: tuple[str, ...], vary: tuple[str, ...] = ()
) -> Job:
    """A job as the backend makes it; the workflow ID only names a held stage."""
    return Job(workflow='wf', params=params, supplied={}, vary=vary, outputs=outputs)


def test_a_dataset_two_parameters_name_is_checksummed_under_one_key(
    tmp_path: Path,
) -> None:
    file = write_run(tmp_path / 'dream_1.h5', [1.0, 2.0, 3.0])
    ref = dataset_ref(instrument='dream', run=1)
    params = {'background': ref.model_dump(mode='json')}
    params['empty_beam'] = params['background']
    session = Runner(keep=True)
    binding = Binding(TWO_RUNS, two_run_workflow, 'in_process')
    inputs = FileInputs({str(ref): file}, sc.io.load_hdf5)

    first = session.run('r1', job(params, ('total',)), binding, inputs, Collected())
    second = session.run('r2', job(params, ('total',)), binding, inputs, Collected())

    assert first.status is Status.COMPLETED, first.failure
    assert second.status is Status.COMPLETED, second.failure
    assert first.checksums == second.checksums
    assert set(first.checksums) == {str(ref)}


def test_a_changed_file_is_hashed_again(tmp_path: Path) -> None:
    file = write_run(tmp_path / 'dream_1.h5', [1.0, 2.0, 3.0])
    ref = dataset_ref(path=file)
    params = {'background': ref.model_dump(mode='json')}
    params['empty_beam'] = params['background']
    session = Runner(keep=True)
    binding = Binding(TWO_RUNS, two_run_workflow, 'in_process')
    inputs = FileInputs({str(ref): file}, sc.io.load_hdf5)

    first = session.run('r1', job(params, ('total',)), binding, inputs, Collected())
    write_run(file, [4.0, 5.0, 6.0, 7.0])
    second = session.run('r2', job(params, ('total',)), binding, inputs, Collected())

    assert first.checksums != second.checksums


def test_a_dataset_in_a_varied_parameter_is_checksummed_but_does_not_name_the_stage(
    tmp_path: Path,
) -> None:
    """
    The held stage is named by the datasets of the parameters not varied; a
    dataset in a varied parameter changes per call and only its checksum is
    recorded.
    """
    files = [write_run(tmp_path / f'dream_{i}.h5', [float(i)] * 3) for i in (1, 2)]
    refs = [dataset_ref(path=file) for file in files]
    session = Runner(keep=True)
    binding = Binding(NORMALIZE, normalize_workflow, 'in_process')
    inputs = FileInputs(
        {str(ref): file for ref, file in zip(refs, files, strict=True)},
        sc.io.load_hdf5,
    )
    first, second = (
        session.run(
            f'r{i}',
            job({'run': ref.model_dump(mode='json')}, ('normalized',), vary=('run',)),
            binding,
            inputs,
            Collected(),
        )
        for i, ref in enumerate(refs)
    )
    assert second.status is Status.COMPLETED, second.failure
    assert not first.reused
    assert second.reused
    assert set(first.checksums) == {str(refs[0])}
    assert set(second.checksums) == {str(refs[1])}


def test_an_output_without_the_declared_dims_fails_the_run(tmp_path: Path) -> None:
    """The spec's ``data`` is over ``x``; the run must not store a ``y`` array."""
    file = write_run(tmp_path / 'dream_1.h5', [1.0, 2.0])
    ref = dataset_ref(path=file)

    def wrong_dims() -> Any:
        def run(params: LoadParams, inputs: Inputs) -> dict[str, Any]:
            return {
                'data': sc.DataArray(sc.arange('y', 3.0, unit='counts')),
                'total': Quantity(value=3.0, unit='counts'),
            }

        return run

    outputs = Collected()
    result = Runner(keep=False).run(
        'r1',
        job({'run': ref.model_dump(mode='json')}, ('data', 'total')),
        Binding(LOAD, wrong_dims, 'in_process'),
        FileInputs({str(ref): file}, sc.io.load_hdf5),
        outputs,
    )

    assert result.status is Status.FAILED
    assert result.failure.kind == 'output-shape'
    assert "'data'" in result.failure.message
    assert outputs.values == {}


def test_an_output_without_a_declared_coord_fails_the_run() -> None:
    class Outputs(BaseModel):
        curve: Array(ArraySpec(dims=('x',), coords={'x': 'm'}))

    spec = WorkflowSpec(
        name='curve',
        version=1,
        title='Curve',
        description='An output whose spec declares a coordinate.',
        outputs=Outputs,
    )

    def without_the_coord() -> Any:
        return lambda params, inputs: {'curve': sc.DataArray(sc.arange('x', 3.0))}

    result = Runner(keep=False).run(
        'r1',
        job({}, ('curve',)),
        Binding(spec, without_the_coord, 'in_process'),
        FileInputs({}, sc.io.load_hdf5),
        Collected(),
    )

    assert result.status is Status.FAILED
    assert result.failure.kind == 'output-shape'
    assert "'curve'" in result.failure.message


def test_a_plain_function_holds_nothing_between_runs(tmp_path: Path) -> None:
    """A function has no graph to cut, so no run of it comes out of held state."""
    file = write_run(tmp_path / 'dream_1.h5', [1.0, 2.0, 3.0])
    ref = dataset_ref(path=file)
    session = Runner(keep=True)
    binding = Binding(LOAD, load_workflow, 'in_process')
    inputs = FileInputs({str(ref): file}, sc.io.load_hdf5)
    results = [
        session.run(
            f'r{i}',
            job({'run': ref.model_dump(mode='json')}, ('total',), vary=('run',)),
            binding,
            inputs,
            Collected(),
        )
        for i in range(2)
    ]
    assert [r.status for r in results] == [Status.COMPLETED] * 2
    assert not any(r.reused for r in results)
