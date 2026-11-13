# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The toy reduction of ``toys`` on a real in-process workspace."""

from collections.abc import Callable, Iterator
from pathlib import Path

import pandas as pd
import pytest

from ess.apps import Client, Template, Workspace
from ess.apps.batch import Batch, CorrectedFiles
from ess.apps.folder import open_folder
from ess.apps.testing import FakeFiles, StagedFunction, read_file
from ess.spec import Binding, FileRef, function_binding

from . import toys


def _find(params: toys.FindParams) -> toys.FindOutputs:
    value = read_file(params.sample)
    if value < 0:
        raise ValueError('nothing to find')
    return toys.FindOutputs(offset=toys.Offset(value=value))


def _series(gate: toys.Gate) -> Callable[[toys.SeriesParams], toys.Outputs]:
    def series(params: toys.SeriesParams) -> toys.Outputs:
        gate.wait()
        return toys.Outputs(total=sum(read_file(run) for run in params.runs))

    return series


@pytest.fixture
def gate() -> toys.Gate:
    return toys.Gate()


@pytest.fixture
def fake(tmp_path: Path) -> FakeFiles:
    return FakeFiles('p1', tmp_path)


@pytest.fixture
def files(fake: FakeFiles) -> CorrectedFiles:
    return CorrectedFiles(fake)


@pytest.fixture
def staged_binding(gate: toys.Gate) -> StagedFunction:
    def reduce(params: toys.Params) -> toys.Outputs:
        gate.wait()
        if params.binning.bins == 1:
            raise ZeroDivisionError('one bin')
        offset = 0.0 if params.offset is None else params.offset.value
        return toys.Outputs(total=read_file(params.sample) / params.thickness + offset)

    return StagedFunction(toys.STAGED, reduce)


@pytest.fixture
def workspace(
    files: CorrectedFiles, staged_binding: StagedFunction, gate: toys.Gate
) -> Iterator[Workspace]:
    plain = function_binding(toys.PLAIN, staged_binding.function)
    find = function_binding(toys.FIND, _find)
    series = function_binding(toys.SERIES, _series(gate))
    grouped = function_binding(
        toys.GROUPED, lambda p: toys.Outputs(total=read_file(p.group.run))
    )
    workspace = Workspace(
        proposal='p1',
        files=files,
        bind=[staged_binding, plain, find, series, grouped],
    )
    yield workspace
    workspace.close()


@pytest.fixture
def client(workspace: Workspace) -> Client:
    return Client(workspace, submitter='anna')


@pytest.fixture
def other(workspace: Workspace) -> Client:
    """Another client of the proposal of ``client``."""
    return Client(workspace, submitter='bob')


@pytest.fixture
def sample(fake: FakeFiles) -> toys.Measure:
    """toys.Measure a sample run and, unless told not to, its transmission run."""

    def measure(run: int, name: str, *, partner: bool = True) -> FileRef:
        ref = fake.measure(run, float(run), role='sample', sample=name)
        if partner:
            fake.measure(run + 1000, 1.0, role='transmission', sample=name)
        return ref

    return measure


@pytest.fixture
def batch(client: Client) -> Iterator[Batch]:
    batch = Batch(
        client,
        toys.STAGED,
        columns=toys.COLUMNS,
        label='scan',
        source=toys.pair,
        staged=True,
    )
    yield batch
    batch.close()


# A workspace kept in a folder of toy files, for batches from tables

BAD = 4
"""The run whose file holds a negative number, which the toy refuses."""


def _reduce(params: toys.Params) -> toys.Outputs:
    value = read_file(params.sample)
    if value < 0 or params.binning.bins == 1:
        raise ValueError('refused')
    offset = 0.0 if params.offset is None else params.offset.value
    return toys.Outputs(total=value / params.thickness + offset)


def _find(params: toys.FindParams) -> toys.FindOutputs:
    return toys.FindOutputs(offset=toys.Offset(value=read_file(params.sample)))


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    """Samples 1, 2, 3 holding their numbers, BAD holding -1, and transmissions."""
    runs = {1: (1, 'sample'), 2: (2, 'sample'), 3: (3, 'sample')}
    runs |= {BAD: (-1, 'bad'), 1001: (1, 'transmission'), 1002: (1, 'transmission')}
    lines = ['file,run,role']
    for run, (content, role) in runs.items():
        (tmp_path / f'run-{run}.json').write_text(str(content))
        lines.append(f'run-{run}.json,{run},{role}')
    (tmp_path / 'files.csv').write_text('\n'.join(lines))
    return tmp_path


@pytest.fixture
def folder_staged() -> StagedFunction:
    return StagedFunction(toys.STAGED, _reduce)


@pytest.fixture
def folder_bindings(folder_staged: StagedFunction) -> list[Binding]:
    return [
        folder_staged,
        function_binding(toys.PLAIN, _reduce),
        function_binding(toys.FIND, _find),
    ]


@pytest.fixture
def folder_client(folder: Path, folder_bindings: list[Binding]) -> Iterator[Client]:
    with open_folder(folder, folder_bindings) as client:
        yield client


@pytest.fixture
def template() -> Template:
    return Template(
        toys.PLAIN,
        params={'thickness': 2.0, 'binning': {'bins': 4}},
        blanks=['sample', 'transmission'],
    )


@pytest.fixture
def table() -> pd.DataFrame:
    return pd.DataFrame(
        {'name': ['a', 'b'], 'sample': ['1', '2'], 'transmission': ['1001', '1002']},
        dtype=str,
    )
