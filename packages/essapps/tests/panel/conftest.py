# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Toy specs with each kind of place, bound on a client of ``local()``.

``FIND`` outputs a ``BeamCentre``, a plain group, which ``REDUCE`` takes
typed in or from a record. ``REDUCE`` also takes a group with defaults, a
file model and a group without a default, a union of groups, and a mask, a
file model of mask files typed ``X | None`` with a default. It divides
the counts of its run, less those of the mask, by the sample's thickness, so
a thickness of 0 fails the record. ``OPTIONS`` takes a value of each type
that has an input, a list of numbers, and a file model typed ``X | None``
without a default. ``SUBTRACT`` takes a group, and a ``BeamCentre`` typed
``X | None`` without a default, which an output of ``FIND`` fits. ``STACK``
takes two tables, of a file model and of a group with a file, and a list
of ``BeamCentre`` with a default, whose row an output of ``FIND`` fits.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

import pytest
from pydantic import BaseModel, Field

from ess.apps import Client, local
from ess.apps.testing import FakeFiles, read_file
from ess.spec import FileModel, FileRef, WorkflowSpec, function_binding


class Run(FileModel):
    """A raw run."""


class Mask(FileModel, kind='mask'):
    """Counts to leave out of a sample run."""


class BeamCentre(BaseModel):
    x: float = 0.0
    y: float = 0.0


class FindParams(BaseModel):
    run: Run


class FindOutputs(BaseModel):
    centre: BeamCentre


class Norm(StrEnum):
    monitor = 'monitor'
    none = 'none'


class Reduction(BaseModel):
    bins: int = Field(default=2, gt=0)
    norm: Norm = Norm.monitor


class Sample(BaseModel):
    thickness: float


class Front(BaseModel):
    kind: Literal['front'] = 'front'
    distance: float = 1.0


class Rear(BaseModel):
    kind: Literal['rear'] = Field('rear', title='Rear bank')
    tilt: float = 0.0


class ReduceParams(BaseModel):
    reduction: Reduction = Field(default_factory=Reduction)
    run: Run
    sample: Sample
    beam_centre: BeamCentre = Field(default_factory=BeamCentre)
    detector: Annotated[Front | Rear, Field(discriminator='kind')] = Front()
    mask: Mask | None = None


class Total(BaseModel):
    value: float


class ReduceOutputs(BaseModel):
    total: Total


class Settings(BaseModel):
    flag: bool
    verbose: bool = False
    name: str = 'run'
    note: str = ''
    count: int
    mode: Literal['fast', 'slow'] = 'fast'
    start: datetime = datetime(2026, 1, 1, tzinfo=UTC)
    banks: list[int] = []


class OptionsParams(BaseModel):
    settings: Settings
    direct_beam: Run | None


class Background(BaseModel):
    scale: float = 1.0


class SubtractParams(BaseModel):
    background: Background
    centre: BeamCentre | None


class Shot(BaseModel):
    run: Run
    thickness: float = 1.0


class StackParams(BaseModel):
    runs: list[Run] = Field(min_length=1)
    shots: list[Shot]
    centres: list[BeamCentre] = [BeamCentre(x=1.0)]


type Spec = WorkflowSpec[Any, Any]

FIND: Spec = WorkflowSpec(
    name='find',
    version=1,
    title='Find centre',
    description='The beam centre in a run.',
    params=FindParams,
    outputs=FindOutputs,
)
REDUCE: Spec = WorkflowSpec(
    name='reduce',
    version=1,
    title='Reduce',
    description='The counts of a sample run per thickness.',
    params=ReduceParams,
    outputs=ReduceOutputs,
)
OPTIONS: Spec = WorkflowSpec(
    name='options',
    version=1,
    title='Options',
    description='The count of the settings.',
    params=OptionsParams,
    outputs=ReduceOutputs,
)
SUBTRACT: Spec = WorkflowSpec(
    name='subtract',
    version=1,
    title='Subtract',
    description='The scale of the background, shifted by its centre.',
    params=SubtractParams,
    outputs=ReduceOutputs,
)
STACK: Spec = WorkflowSpec(
    name='stack',
    version=1,
    title='Stack',
    description='The counts of the runs and the shots, shifted by the centres.',
    params=StackParams,
    outputs=ReduceOutputs,
    tables=('runs', 'shots'),
)


def find(params: FindParams) -> FindOutputs:
    return FindOutputs(centre=BeamCentre(x=read_file(params.run.path)['x']))


def reduce(params: ReduceParams) -> ReduceOutputs:
    counts = read_file(params.run.path)['counts']
    if params.mask is not None:
        counts -= read_file(params.mask.path)['counts']
    return ReduceOutputs(total=Total(value=counts / params.sample.thickness))


def options(params: OptionsParams) -> ReduceOutputs:
    return ReduceOutputs(total=Total(value=params.settings.count))


def subtract(params: SubtractParams) -> ReduceOutputs:
    shift = 0.0 if params.centre is None else params.centre.x
    return ReduceOutputs(total=Total(value=params.background.scale + shift))


def stack(params: StackParams) -> ReduceOutputs:
    counts = sum(read_file(r.path)['counts'] for r in params.runs)
    counts += sum(read_file(s.run.path)['counts'] / s.thickness for s in params.shots)
    return ReduceOutputs(total=Total(value=counts + sum(c.x for c in params.centres)))


@pytest.fixture
def files(tmp_path: Path) -> FakeFiles:
    return FakeFiles('p1', tmp_path)


@pytest.fixture
def run(files: FakeFiles) -> FileRef:
    return files.measure(101, {'counts': 6.0, 'x': 1.5}, role='sample')


@pytest.fixture
def client(files: FakeFiles) -> Iterator[Client]:
    bind = [
        function_binding(FIND, find),
        function_binding(REDUCE, reduce),
        function_binding(OPTIONS, options),
        function_binding(SUBTRACT, subtract),
        function_binding(STACK, stack),
    ]
    with local(proposal='p1', files=files, bind=bind) as client:
        yield client


@pytest.fixture
def find_spec() -> Spec:
    return FIND


@pytest.fixture
def reduce_spec() -> Spec:
    return REDUCE


@pytest.fixture
def options_spec() -> Spec:
    return OPTIONS


@pytest.fixture
def subtract_spec() -> Spec:
    return SUBTRACT


@pytest.fixture
def stack_spec() -> Spec:
    return STACK
