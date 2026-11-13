# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The toy reduction of ``toys`` on a real in-process workspace."""

from collections.abc import Callable, Iterator
from pathlib import Path

import pandas as pd
import pytest

from ess.apps import Client, Template, Workspace
from ess.apps.batch import CorrectedFiles
from ess.apps.folder import open_folder
from ess.apps.testing import FakeFiles, StagedFunction, read_file
from ess.spec import Binding, FileRef, function_binding

import one
import support
import toys
from support import Batch


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
    support.FILES = CorrectedFiles(fake)
    return support.FILES


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
def batch(client: Client) -> Iterator[one.Batch]:
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
