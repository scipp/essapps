# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The toy reduction of ``toys`` on a real in-process backend."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from ess.apps import Backend, Client
from ess.apps.batch import Batch, CorrectedFiles
from ess.apps.testing import FakeFiles, StagedFunction, read_file
from ess.spec import FileRef, function_binding

from . import toys


def _find(params: toys.FindParams) -> toys.FindOutputs:
    value = read_file(params.sample)
    if value < 0:
        raise ValueError('nothing to find')
    return toys.FindOutputs(offset=toys.Offset(value=value))


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
def backend(files: CorrectedFiles, staged_binding: StagedFunction) -> Iterator[Backend]:
    plain = function_binding(toys.PLAIN, staged_binding.function)
    find = function_binding(toys.FIND, _find)
    backend = Backend(files, [staged_binding, plain, find])
    yield backend
    backend.close()


@pytest.fixture
def client(backend: Backend) -> Client:
    return Client(backend, proposal='p1', submitter='anna')


@pytest.fixture
def other(backend: Backend) -> Client:
    """Another client of the proposal of ``client``."""
    return Client(backend, proposal='p1', submitter='bob')


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
