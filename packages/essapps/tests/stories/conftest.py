# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Toy specs and fixtures of the user stories (docs/developer/user-stories.md).

A dataset made by ``measure`` holds a list of counts. The toy specs compute
what the table in user-stories.md says, so that every number can be checked by
hand.
"""

from __future__ import annotations

import functools
import operator
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any

import numpy as np
import pytest
import scipp as sc
from ess.reduce.spec import Array, DatasetRef, NexusFile, OpaqueFile, WorkflowSpec
from pydantic import BaseModel

from ess.apps import Backend, Client, combine
from ess.apps.bindings import ElementAccumulator, Function
from ess.apps.testing import FakeDatasets


def _spec(name: str, params: type[BaseModel], outputs: type[BaseModel]) -> WorkflowSpec:
    return WorkflowSpec(
        name=name,
        version=1,
        title=name,
        description=f'toy spec {name}',
        params=params,
        outputs=outputs,
    )


def _counts(values: Any) -> np.ndarray:
    return np.asarray(values, dtype=float)


def _array(values: Any) -> sc.Variable:
    return sc.array(dims=['q'], values=_counts(values))


class IofQParams(BaseModel):
    run: NexusFile
    bins: int = 2
    threshold: float = 0.0
    can: NexusFile | None = None
    beam_centre: Array() | None = None  # type: ignore[valid-type]
    normalization: Array() | None = None  # type: ignore[valid-type]


class IofQOutputs(BaseModel):
    iofq: Array()  # type: ignore[valid-type]
    masked: Array()  # type: ignore[valid-type]


IOFQ = _spec('sans-iofq', IofQParams, IofQOutputs)


def iofq(
    run: Any,
    bins: int,
    threshold: float,
    can: Any,
    beam_centre: sc.Variable | None,
    normalization: sc.Variable | None,
) -> dict[str, sc.Variable]:
    counts = _counts(run) - (0.0 if can is None else _counts(can))
    masked = np.where(counts < threshold, 0.0, counts)
    reduced = masked - (0.0 if beam_centre is None else beam_centre.value)
    reduced = reduced / (1.0 if normalization is None else normalization.value)
    return {
        'iofq': _array(reduced.reshape(bins, -1).sum(axis=1)),
        'masked': _array(masked),
    }


class IofQV2Params(BaseModel):
    run: NexusFile
    bins: int = 4
    mask_below: float = 0.0
    can: NexusFile | None = None
    beam_centre: Array() | None = None  # type: ignore[valid-type]
    normalization: Array() | None = None  # type: ignore[valid-type]


IOFQ_V2 = WorkflowSpec(
    name='sans-iofq',
    version=2,
    title='sans-iofq',
    description='toy spec sans-iofq, version 2: threshold renamed mask_below',
    params=IofQV2Params,
    outputs=IofQOutputs,
)


def iofq_v2(mask_below: float, **values: Any) -> dict[str, sc.Variable]:
    return iofq(threshold=mask_below, **values)


class RunParams(BaseModel):
    run: NexusFile


class CentreOutputs(BaseModel):
    centre: Array()  # type: ignore[valid-type]


BEAM_CENTRE = _spec('beam-centre', RunParams, CentreOutputs)


def beam_centre(run: Any) -> dict[str, sc.Variable]:
    return {'centre': sc.scalar(float(_counts(run).mean()))}


class VanadiumParams(BaseModel):
    run: NexusFile
    scale: float = 1.0


class VanadiumOutputs(BaseModel):
    normalization: Array()  # type: ignore[valid-type]


VANADIUM = _spec('vanadium', VanadiumParams, VanadiumOutputs)


def vanadium(run: Any, scale: float) -> dict[str, sc.Variable]:
    return {'normalization': sc.scalar(float(_counts(run).sum()) * scale)}


class NormalizeParams(BaseModel):
    runs: list[NexusFile]
    scale: float = 1.0


class NormalizedOutputs(BaseModel):
    normalized: Array()  # type: ignore[valid-type]


NORMALIZE = _spec('normalize', NormalizeParams, NormalizedOutputs)


def normalize(runs: list[Any], scale: float) -> dict[str, sc.Variable]:
    total = sum(_counts(r) for r in runs)
    return {'normalized': _array(total / total.sum() * scale)}


class BackgroundParams(BaseModel):
    sample_runs: list[NexusFile]
    background_runs: list[NexusFile]


class BackgroundOutputs(BaseModel):
    subtracted: Array()  # type: ignore[valid-type]


BACKGROUND = _spec('background', BackgroundParams, BackgroundOutputs)


def background(sample_runs: list[Any], background_runs: list[Any]) -> dict[str, Any]:
    samples = sum(_counts(r) for r in sample_runs)
    backgrounds = sum(_counts(r) for r in background_runs)
    return {'subtracted': _array(samples - backgrounds)}


class ContributeOutputs(BaseModel):
    numerator: Array()  # type: ignore[valid-type]
    denominator: Array()  # type: ignore[valid-type]
    transmission: Array()  # type: ignore[valid-type]


CONTRIBUTE = _spec('sans-contribute', RunParams, ContributeOutputs)


def normalization_parts(run: Any) -> dict[str, sc.Variable]:
    counts = _counts(run)
    return {'numerator': _array(counts), 'denominator': sc.scalar(float(counts.sum()))}


def contribute(run: Any) -> dict[str, sc.Variable]:
    counts = _counts(run)
    transmission = sc.scalar(float(counts[0] / counts.sum()))
    return {**normalization_parts(run), 'transmission': transmission}


class FinalizeParams(BaseModel):
    numerator: Array()  # type: ignore[valid-type]
    denominator: Array()  # type: ignore[valid-type]
    scale: float = 1.0


FINALIZE = _spec('sans-finalize', FinalizeParams, NormalizedOutputs)


def finalize(
    numerator: sc.Variable, denominator: sc.Variable, scale: float
) -> dict[str, sc.Variable]:
    return {'normalized': numerator / denominator * scale}


class NormalizationParts(BaseModel):
    numerator: Array()  # type: ignore[valid-type]
    denominator: Array()  # type: ignore[valid-type]


class PartsSumParams(BaseModel):
    parts: list[NormalizationParts]


PARTS_SUM = _spec('sans-parts-sum', PartsSumParams, NormalizationParts)


class RunsParams(BaseModel):
    runs: list[RunParams]


class _RunTotal:
    def __init__(self, reduce: Callable[[Any], dict[str, sc.Variable]]) -> None:
        self._reduce = reduce
        self._sum = combine(operator.iadd).accumulator({})

    def push(self, row: Mapping[str, Any]) -> None:
        self._sum.push(self._reduce(row['run']))

    @property
    def value(self) -> Mapping[str, Any]:
        return self._sum.value


class RunSum:
    """
    The binding of a spec over a table of runs: ``reduce`` reduces each run,
    and the results are added in place.
    """

    def __init__(self, reduce: Callable[[Any], dict[str, sc.Variable]]) -> None:
        self._reduce = reduce

    def stage(self, fixed: Mapping[str, Any], blanks: Sequence[str]) -> Function:
        def total(runs: list[Mapping[str, Any]]) -> Mapping[str, Any]:
            held = self.accumulator({})
            for row in runs:
                held.push(row)
            return held.value

        return functools.partial(total, **fixed)

    def accumulator(self, fixed: Mapping[str, Any]) -> ElementAccumulator:
        return _RunTotal(self._reduce)


SANS_SUM = _spec('sans-sum', RunsParams, NormalizationParts)


class Counts(BaseModel):
    counts: Array()  # type: ignore[valid-type]


ANGLE = _spec('angle', RunParams, Counts)
VOLUME = _spec('volume', RunsParams, Counts)


def angle(run: Any) -> dict[str, sc.Variable]:
    return {'counts': _array(run)}


class Data(BaseModel):
    data: Array()  # type: ignore[valid-type]


COPY = _spec('copy', Data, Data)


def copy(data: sc.Variable) -> dict[str, sc.Variable]:
    return {'data': data.copy()}


class CutParams(BaseModel):
    data: Array()  # type: ignore[valid-type]
    index: int


class CutOutputs(BaseModel):
    cut: Array()  # type: ignore[valid-type]


CUT = _spec('cut', CutParams, CutOutputs)


def cut(data: sc.Variable, index: int) -> dict[str, sc.Variable]:
    return {'cut': data[index].copy()}


class StitchParams(BaseModel):
    runs: list[NexusFile]
    reference: NexusFile


class StitchOutputs(BaseModel):
    stitched: Array()  # type: ignore[valid-type]


STITCH = _spec('stitch', StitchParams, StitchOutputs)


def stitch(runs: list[Any], reference: Any) -> dict[str, sc.Variable]:
    curves = [_counts(run) / _counts(reference) for run in runs]
    scaled = [curves[0]]
    for curve in curves[1:]:
        scaled.append(curve * scaled[-1][-1] / curve[0])
    return {'stitched': _array(np.concatenate(scaled))}


class ExportParams(BaseModel):
    data: Array()  # type: ignore[valid-type]


class ExportOutputs(BaseModel):
    text: OpaqueFile


EXPORT = _spec('export', ExportParams, ExportOutputs)


def export(data: sc.Variable) -> dict[str, str]:
    return {'text': ','.join(str(v) for v in data.values)}


TOYS = {
    IOFQ: iofq,
    IOFQ_V2: iofq_v2,
    BEAM_CENTRE: beam_centre,
    VANADIUM: vanadium,
    NORMALIZE: normalize,
    BACKGROUND: background,
    CONTRIBUTE: contribute,
    FINALIZE: finalize,
    ANGLE: angle,
    CUT: cut,
    STITCH: stitch,
    EXPORT: export,
    PARTS_SUM: combine(operator.add),
    SANS_SUM: RunSum(normalization_parts),
    VOLUME: RunSum(angle),
    COPY: copy,
}


@pytest.fixture
def datasets() -> FakeDatasets:
    return FakeDatasets(proposal='p1')


@pytest.fixture
def backend(datasets: FakeDatasets) -> Iterator[Backend]:
    backend = Backend(datasets, TOYS)
    yield backend
    backend.close()


@pytest.fixture
def upgrade(datasets: FakeDatasets) -> Iterator[Callable[..., Client]]:
    """A client of a new backend that offers only the given toy specs."""
    backends: list[Backend] = []

    def upgrade(specs: list[WorkflowSpec]) -> Client:
        backends.append(Backend(datasets, {s: TOYS[s] for s in specs}))
        return Client(backends[-1], proposal='p1', submitter='anna')

    yield upgrade
    for backend in backends:
        backend.close()


@pytest.fixture
def connect(backend: Backend) -> Callable[..., Client]:
    """A new client of the same backend, by default for proposal p1."""

    def connect(proposal: str = 'p1', user: str = 'anna') -> Client:
        return Client(backend, proposal=proposal, submitter=user)

    return connect


@pytest.fixture
def client(connect: Callable[..., Client]) -> Client:
    return connect()


Measure = Callable[..., DatasetRef]


@pytest.fixture
def measure(datasets: FakeDatasets) -> Measure:
    """Make run ``n`` appear with the given counts and metadata; return its identity."""

    def measure(n: int, counts: list[float], **fields: Any) -> DatasetRef:
        return datasets.measure(n, counts, **fields)

    return measure


@pytest.fixture
def corrupt(datasets: FakeDatasets) -> Callable[..., None]:
    return datasets.corrupt


@pytest.fixture
def repair(datasets: FakeDatasets) -> Callable[..., None]:
    return datasets.repair
