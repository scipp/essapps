# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Toy specs with each kind of place, bound on a client of ``local()``.

``FIND`` outputs a ``BeamCentre``, a plain group, which ``REDUCE`` takes
typed in or from a record. ``REDUCE`` also takes a group with defaults, a
dataset model and a group without a default, a union of groups, and a mask, a
dataset model of mask datasets typed ``X | None`` with a default. It divides
the counts of its run, less those of the mask, by the sample's thickness, so
a thickness of 0 fails the record. ``OPTIONS`` takes a value of each type
that has an input, a list of numbers, and a dataset model typed ``X | None``
without a default. ``SUBTRACT`` takes a group, and a ``BeamCentre`` typed
``X | None`` without a default, which an output of ``FIND`` fits.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

import pytest
from pydantic import BaseModel, Field

from ess.apps import Client, local
from ess.apps.testing import FakeDatasets, read_dataset
from ess.spec import DatasetModel, DatasetRef, WorkflowSpec, function_binding


class Run(DatasetModel):
    """A raw run."""


class Mask(DatasetModel, kind='mask'):
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


def find(params: FindParams) -> FindOutputs:
    return FindOutputs(centre=BeamCentre(x=read_dataset(params.run.path)['x']))


def reduce(params: ReduceParams) -> ReduceOutputs:
    counts = read_dataset(params.run.path)['counts']
    if params.mask is not None:
        counts -= read_dataset(params.mask.path)['counts']
    return ReduceOutputs(total=Total(value=counts / params.sample.thickness))


def options(params: OptionsParams) -> ReduceOutputs:
    return ReduceOutputs(total=Total(value=params.settings.count))


def subtract(params: SubtractParams) -> ReduceOutputs:
    shift = 0.0 if params.centre is None else params.centre.x
    return ReduceOutputs(total=Total(value=params.background.scale + shift))


@pytest.fixture
def datasets(tmp_path: Path) -> FakeDatasets:
    return FakeDatasets('p1', tmp_path)


@pytest.fixture
def run(datasets: FakeDatasets) -> DatasetRef:
    return datasets.measure(101, {'counts': 6.0, 'x': 1.5}, role='sample')


@pytest.fixture
def client(datasets: FakeDatasets) -> Iterator[Client]:
    bind = [
        function_binding(FIND, find),
        function_binding(REDUCE, reduce),
        function_binding(OPTIONS, options),
        function_binding(SUBTRACT, subtract),
    ]
    with local(proposal='p1', datasets=datasets, bind=bind) as client:
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
