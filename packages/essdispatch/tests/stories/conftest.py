# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Toy specs and fixtures of the user stories (docs/developer/user-stories.md).

A dataset made by ``measure`` holds a list of counts. The toy specs compute
what the table in user-stories.md says, so that every number can be checked by
hand.

After each story, every record whose outputs a client of the story still keeps
is run again as its request by that client, and must give the same outputs
(``replay``).
"""

from __future__ import annotations

import operator
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any

import numpy as np
import pytest
import scipp as sc
from pydantic import BaseModel

from ess.dispatch import Backend, Client, ClientEnded, Record, Status
from ess.dispatch.records import map_refs
from ess.dispatch.testing import FakeDatasets
from ess.spec import (
    Array,
    DatasetRef,
    Function,
    HeldState,
    NexusFile,
    OpaqueFile,
    OutputRef,
    Ref,
    WorkflowSpec,
    combine,
)
from ess.spec.testing import assert_close


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


class RunsParams(BaseModel):
    runs: list[RunParams]


class NormalizeParams(BaseModel):
    runs: list[RunParams]
    scale: float = 1.0


class NormalizedOutputs(BaseModel):
    normalized: Array()  # type: ignore[valid-type]


NORMALIZE = _spec('normalize', NormalizeParams, NormalizedOutputs)


def normalize(runs: np.ndarray, scale: float) -> dict[str, sc.Variable]:
    return {'normalized': _array(runs / runs.sum() * scale)}


class BackgroundParams(BaseModel):
    sample_runs: list[RunParams]
    background_runs: list[RunParams]


class BackgroundOutputs(BaseModel):
    subtracted: Array()  # type: ignore[valid-type]


BACKGROUND = _spec('background', BackgroundParams, BackgroundOutputs)


def background(
    sample_runs: np.ndarray, background_runs: np.ndarray
) -> dict[str, sc.Variable]:
    return {'subtracted': _array(sample_runs - background_runs)}


class ContributeOutputs(BaseModel):
    numerator: Array()  # type: ignore[valid-type]
    denominator: Array()  # type: ignore[valid-type]
    transmission: Array()  # type: ignore[valid-type]


CONTRIBUTE = _spec('sans-contribute', RunParams, ContributeOutputs)


def contribute(run: Any) -> dict[str, sc.Variable]:
    counts = _counts(run)
    return {
        'numerator': _array(counts),
        'denominator': sc.scalar(float(counts.sum())),
        'transmission': sc.scalar(float(counts[0] / counts.sum())),
    }


class NormalizationParts(BaseModel):
    numerator: Array()  # type: ignore[valid-type]
    denominator: Array()  # type: ignore[valid-type]


class PartsSumParams(BaseModel):
    parts: list[NormalizationParts]


PARTS_SUM = _spec('sans-parts-sum', PartsSumParams, NormalizationParts)


class _Sums:
    def __init__(
        self,
        finalize: Callable[..., dict[str, Any]],
        tables: Sequence[str],
        **fixed: Any,
    ) -> None:
        self._finalize = finalize
        self._fixed = fixed
        self._sums: dict[str, Any] = dict.fromkeys(tables, 0.0)

    def push(self, rows: Mapping[str, Mapping[str, Any]]) -> None:
        for table, row in rows.items():
            self._sums[table] += _counts(row['run'])  # in place from the second row

    def outputs(self) -> Mapping[str, Any]:
        return self._finalize(**self._sums, **self._fixed)


class Summing:
    """
    The binding of a spec over tables of runs, a toy ``StreamProcessor``: the
    counts of each table's runs are summed, and ``finalize`` computes the
    outputs from the sums, by table name, and the other values.
    """

    def __init__(self, finalize: Callable[..., dict[str, Any]], *tables: str) -> None:
        self._finalize = finalize
        self._tables = tables

    def stage(self, fixed: Mapping[str, Any], blanks: Sequence[str]) -> Function:
        def compute(**values: Any) -> Mapping[str, Any]:
            params = {**fixed, **values}
            sums = {
                t: sum((_counts(row['run']) for row in params.pop(t)), 0.0)
                for t in self._tables
            }
            return self._finalize(**sums, **params)

        return compute

    def held_state(self, fixed: Mapping[str, Any]) -> HeldState:
        return _Sums(self._finalize, self._tables, **fixed)


class Counts(BaseModel):
    counts: Array()  # type: ignore[valid-type]


ANGLE = _spec('angle', RunParams, Counts)
VOLUME = _spec('volume', RunsParams, Counts)


def angle(run: Any) -> dict[str, sc.Variable]:
    return {'counts': _array(run)}


def volume(runs: np.ndarray) -> dict[str, sc.Variable]:
    return {'counts': _array(runs)}


class CutParams(BaseModel):
    data: Array()  # type: ignore[valid-type]
    index: int


class CutOutputs(BaseModel):
    cut: Array()  # type: ignore[valid-type]


CUT = _spec('cut', CutParams, CutOutputs)


def cut(data: sc.Variable, index: int) -> dict[str, sc.Variable]:
    return {'cut': data[index].copy()}


class StitchParams(BaseModel):
    runs: list[RunParams]
    reference: NexusFile


class StitchOutputs(BaseModel):
    stitched: Array()  # type: ignore[valid-type]


STITCH = _spec('stitch', StitchParams, StitchOutputs)


def stitch(runs: list[dict[str, Any]], reference: Any) -> dict[str, sc.Variable]:
    curves = [_counts(row['run']) / _counts(reference) for row in runs]
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
    NORMALIZE: Summing(normalize, 'runs'),
    BACKGROUND: Summing(background, 'sample_runs', 'background_runs'),
    CONTRIBUTE: contribute,
    ANGLE: angle,
    CUT: cut,
    STITCH: stitch,
    EXPORT: export,
    PARTS_SUM: combine(operator.add),
    VOLUME: Summing(volume, 'runs'),
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
def clients() -> list[Client]:
    """Every client that ``connect`` made."""
    return []


@pytest.fixture
def connect(backend: Backend, clients: list[Client]) -> Callable[..., Client]:
    """A new client of the same backend, by default for proposal p1."""

    def connect(proposal: str = 'p1', user: str = 'anna') -> Client:
        clients.append(Client(backend, proposal=proposal, submitter=user))
        return clients[-1]

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


@pytest.fixture(autouse=True)
def replay(backend: Backend, clients: list[Client]) -> Iterator[None]:
    """
    After the story, each client that has not ended runs every completed
    record whose outputs it keeps again as its request, and compares the
    outputs.

    This is a safety net over every story: a record says all that its outputs
    depend on. A reference to an output the client does not keep, such as one
    of the record of an accumulator's state, is replaced by the same output of
    that record run again. No record holds a reference to an accumulator.
    It runs before ``backend`` is closed.
    """
    yield
    for client in list(clients):
        try:
            records = {r.id: r for r in client.records()}
        except ClientEnded:
            continue
        client.wait(list(records.values()))
        assert not [r for r in records.values() if r.request.accumulators()]
        _replay(client, records)


def _replay(client: Client, records: Mapping[str, Record]) -> None:
    replays: dict[str, Record] = {}

    def kept(ref: OutputRef) -> bool:
        try:
            client.output(records[ref.record], ref.output)
        except LookupError:
            return False
        return True

    def again(record: Record) -> Record:
        def source(ref: Ref) -> Ref:
            if isinstance(ref, OutputRef) and not kept(ref):
                return again(records[ref.record]).ref(ref.output)
            return ref

        if record.id not in replays:
            params = map_refs(record.request.params, source)
            replays[record.id] = client.compute(record.spec, params)
        return replays[record.id]

    for record in list(records.values()):
        if client.status(record) is not Status.COMPLETED:
            continue
        try:
            expected = client.output(record)
        except LookupError:  # not kept by this client
            continue
        where = f'{record.id} {record.spec} {record.label} {record.member}'
        assert_close(client.output(again(record)), expected, where=where)
