# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Toy specs and fixtures of the user stories (docs/developer/user-stories.md).

A dataset made by ``measure`` is a file of a list of counts. The toy specs
compute what the table in user-stories.md says, so that every number can be
checked by hand. Each toy workflow takes an instance of its spec's params model,
where a file is a path, and reads the counts of each file.

After each story, every record whose outputs a client of the story still keeps
is run again as its request by that client, and must give the same outputs
(``replay``).
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any, NewType

import numpy as np
import pytest
import scipp as sc
from pydantic import BaseModel, ConfigDict, Field

from ess.dispatch import Backend, Client, ClientEnded, Record, SpecId, Status
from ess.dispatch.log import Log
from ess.dispatch.testing import (
    FakeDatasets,
    FakeStore,
    StagedFunction,
    kept_rows,
    read_dataset,
)
from ess.spec import (
    Binding,
    DatasetRef,
    DriverBinding,
    NoParams,
    OutputRef,
    Ref,
    WorkflowSpec,
    function_binding,
)
from ess.spec.refs import map_refs
from ess.spec.testing import assert_close


class Data(BaseModel):
    """Base of the toy params and outputs models, which hold scipp objects."""

    model_config = ConfigDict(arbitrary_types_allowed=True)


def _spec(
    name: str,
    outputs: type[BaseModel],
    *,
    params: type[BaseModel] = NoParams,
    tables: tuple[str, ...] = (),
) -> WorkflowSpec[Any, Any]:
    return WorkflowSpec(
        name=name,
        version=1,
        title=name,
        description=f'toy spec {name}',
        params=params,
        outputs=outputs,
        tables=tables,
    )


def _counts(path: Path) -> np.ndarray:
    return np.asarray(read_dataset(path), dtype=float)


def _array(values: Any) -> sc.Variable:
    return sc.array(dims=['q'], values=np.asarray(values, dtype=float))


SampleFile = NewType('SampleFile', Path)
CanFile = NewType('CanFile', Path)
DirectBeamFile = NewType('DirectBeamFile', Path)
VanadiumFile = NewType('VanadiumFile', Path)
RunFile = NewType('RunFile', Path)
BackgroundFile = NewType('BackgroundFile', Path)
ReferenceFile = NewType('ReferenceFile', Path)

BeamCentre = NewType('BeamCentre', sc.Variable)
Normalization = NewType('Normalization', sc.Variable)
IofQ = NewType('IofQ', sc.Variable)
Masked = NewType('Masked', sc.Variable)


class Reduction(BaseModel):
    bins: int = 2
    threshold: float = 0.0


class IofQParams(Data):
    reduction: Reduction = Field(default_factory=Reduction)
    sample: SampleFile
    can: CanFile | None = None
    beam_centre: BeamCentre | None = None
    normalization: Normalization | None = None


class IofQOutputs(Data):
    iofq: IofQ
    masked: Masked


IOFQ = _spec('sans-iofq', IofQOutputs, params=IofQParams)


def iofq(params: IofQParams) -> IofQOutputs:
    reduction = params.reduction
    can = 0.0 if params.can is None else _counts(params.can)
    counts = _counts(params.sample) - can
    masked = np.where(counts < reduction.threshold, 0.0, counts)
    centre = params.beam_centre
    reduced = masked - (0.0 if centre is None else centre.value)
    norm = params.normalization
    reduced = reduced / (1.0 if norm is None else norm.value)
    return IofQOutputs(
        iofq=IofQ(_array(reduced.reshape(reduction.bins, -1).sum(axis=1))),
        masked=Masked(_array(masked)),
    )


class ReductionV2(BaseModel):
    bins: int = 4
    mask_below: float = 0.0


class IofQV2Params(Data):
    reduction: ReductionV2 = Field(default_factory=ReductionV2)
    sample: SampleFile
    can: CanFile | None = None
    beam_centre: BeamCentre | None = None
    normalization: Normalization | None = None


IOFQ_V2 = WorkflowSpec(
    name='sans-iofq',
    version=2,
    title='sans-iofq',
    description='toy spec sans-iofq, version 2: threshold renamed mask_below',
    params=IofQV2Params,
    outputs=IofQOutputs,
)


def iofq_v2(params: IofQV2Params) -> IofQOutputs:
    reduction = params.reduction
    renamed = Reduction(bins=reduction.bins, threshold=reduction.mask_below)
    return iofq(IofQParams(**{**dict(params), 'reduction': renamed}))


class BeamCentreParams(Data):
    direct_beam: DirectBeamFile


class CentreOutputs(Data):
    centre: BeamCentre


BEAM_CENTRE = _spec('beam-centre', CentreOutputs, params=BeamCentreParams)


def beam_centre(params: BeamCentreParams) -> CentreOutputs:
    return CentreOutputs(
        centre=BeamCentre(sc.scalar(float(_counts(params.direct_beam).mean())))
    )


class Scaling(BaseModel):
    """Shared by VANADIUM and NORMALIZE."""

    scale: float = 1.0


class VanadiumParams(Data):
    scaling: Scaling = Field(default_factory=Scaling)
    vanadium: VanadiumFile


class VanadiumOutputs(Data):
    normalization: Normalization


VANADIUM = _spec('vanadium', VanadiumOutputs, params=VanadiumParams)


def vanadium(params: VanadiumParams) -> VanadiumOutputs:
    total = float(_counts(params.vanadium).sum())
    return VanadiumOutputs(
        normalization=Normalization(sc.scalar(total * params.scaling.scale))
    )


class NormalizeParams(Data):
    scaling: Scaling = Field(default_factory=Scaling)
    runs: list[RunFile] = Field(min_length=1)


Normalized = NewType('Normalized', sc.Variable)


class NormalizedOutputs(Data):
    normalized: Normalized


NORMALIZE = _spec(
    'normalize', NormalizedOutputs, params=NormalizeParams, tables=('runs',)
)


def normalize(runs: np.ndarray, scaling: Scaling) -> NormalizedOutputs:
    return NormalizedOutputs(
        normalized=Normalized(_array(runs / runs.sum() * scaling.scale))
    )


class BackgroundParams(Data):
    sample_runs: list[SampleFile] = Field(min_length=1)
    background_runs: list[BackgroundFile] = Field(min_length=1)


Subtracted = NewType('Subtracted', sc.Variable)


class BackgroundOutputs(Data):
    subtracted: Subtracted


BACKGROUND = _spec(
    'background',
    BackgroundOutputs,
    params=BackgroundParams,
    tables=('sample_runs', 'background_runs'),
)


def background(
    sample_runs: np.ndarray, background_runs: np.ndarray
) -> BackgroundOutputs:
    return BackgroundOutputs(
        subtracted=Subtracted(_array(sample_runs - background_runs))
    )


Transmission = NewType('Transmission', sc.Variable)


class Parts(Data):
    """The numerator and the denominator of one run, summed by ``PARTS_SUM``."""

    numerator: sc.Variable
    denominator: sc.Variable


class ContributeParams(Data):
    sample: SampleFile


class ContributeOutputs(Data):
    parts: Parts
    transmission: Transmission


CONTRIBUTE = _spec('sans-contribute', ContributeOutputs, params=ContributeParams)


def contribute(params: ContributeParams) -> ContributeOutputs:
    counts = _counts(params.sample)
    return ContributeOutputs(
        parts=Parts(
            numerator=_array(counts), denominator=sc.scalar(float(counts.sum()))
        ),
        transmission=Transmission(sc.scalar(float(counts[0] / counts.sum()))),
    )


class PartsParams(Data):
    parts: list[Parts]


TotalNumerator = NewType('TotalNumerator', sc.Variable)
TotalDenominator = NewType('TotalDenominator', sc.Variable)


class PartsOutputs(Data):
    numerator: TotalNumerator
    denominator: TotalDenominator


PARTS_SUM = _spec('sans-parts-sum', PartsOutputs, params=PartsParams)


def parts_sum(params: PartsParams) -> PartsOutputs:
    def total(field: str) -> sc.Variable:
        return functools.reduce(sc.add, [getattr(p, field) for p in params.parts])

    return PartsOutputs(
        numerator=TotalNumerator(total('numerator')),
        denominator=TotalDenominator(total('denominator')),
    )


class Summing:
    """
    A driver over tables of runs, a toy ``StreamProcessor``: the counts of
    each table's runs are summed, and ``finalize`` computes the outputs from
    the sums, by table name, and the fixed values by name.
    """

    def __init__(
        self,
        finalize: Callable[..., BaseModel],
        spec: WorkflowSpec[Any, Any],
        fixed: Mapping[str, Any],
    ) -> None:
        self._finalize = finalize
        self._fixed = fixed
        self._sums: dict[str, Any] = dict.fromkeys(spec.tables, 0.0)

    def push(self, table: str, columns: Mapping[str, list[Any]]) -> None:
        for path in columns[table]:
            self._sums[table] += _counts(path)  # in place from the second row

    def outputs(self) -> BaseModel:
        return self._finalize(**self._sums, **self._fixed)


def summing(
    spec: WorkflowSpec[Any, Any], finalize: Callable[..., BaseModel]
) -> DriverBinding:
    """The binding of a spec over tables of runs, by :class:`Summing`."""
    return DriverBinding(spec, functools.partial(Summing, finalize, spec))


Counts = NewType('Counts', sc.Variable)


class CountsOutputs(Data):
    counts: Counts


class AngleParams(Data):
    sample: SampleFile


class RunsParams(Data):
    runs: list[RunFile] = Field(min_length=1)


ANGLE = _spec('angle', CountsOutputs, params=AngleParams)
VOLUME = _spec('volume', CountsOutputs, params=RunsParams, tables=('runs',))


def angle(params: AngleParams) -> CountsOutputs:
    return CountsOutputs(counts=Counts(_array(_counts(params.sample))))


def volume(runs: np.ndarray) -> CountsOutputs:
    return CountsOutputs(counts=Counts(_array(runs)))


class Position(BaseModel):
    index: int


class CutParams(Data):
    position: Position
    volume: sc.Variable


Cut = NewType('Cut', sc.Variable)


class CutOutputs(Data):
    cut: Cut


CUT = _spec('cut', CutOutputs, params=CutParams)


def cut(params: CutParams) -> CutOutputs:
    return CutOutputs(cut=Cut(params.volume['q', params.position.index].copy()))


class StitchParams(Data):
    runs: list[RunFile] = Field(min_length=1)
    reference: ReferenceFile


Stitched = NewType('Stitched', sc.Variable)


class StitchOutputs(Data):
    stitched: Stitched


STITCH = _spec('stitch', StitchOutputs, params=StitchParams, tables=('runs',))


def stitch(params: StitchParams) -> StitchOutputs:
    reference = _counts(params.reference)
    curves = [_counts(run) / reference for run in params.runs]
    scaled = [curves[0]]
    for curve in curves[1:]:
        scaled.append(curve * scaled[-1][-1] / curve[0])
    return StitchOutputs(stitched=Stitched(_array(np.concatenate(scaled))))


class ExportParams(Data):
    result: sc.Variable


class ExportOutputs(BaseModel):
    text: str


EXPORT = _spec('export', ExportOutputs, params=ExportParams)


def export(params: ExportParams) -> ExportOutputs:
    return ExportOutputs(text=','.join(str(v) for v in params.result.values))


class TimeRange(BaseModel):
    """The time bins ``start`` to ``stop`` of a run, ``stop`` excluded."""

    start: int
    stop: int


class SlicerParams(Data):
    log: RunFile


class SlicerOutputs(Data):
    slices: list[TimeRange]


SLICER = _spec('slicer', SlicerOutputs, params=SlicerParams)


def slicer(params: SlicerParams) -> SlicerOutputs:
    """A slice of every two bins of the log."""
    n = len(_counts(params.log))
    return SlicerOutputs(
        slices=[TimeRange(start=i, stop=min(i + 2, n)) for i in range(0, n, 2)]
    )


class SlicesParams(Data):
    sample: SampleFile
    time_range: list[TimeRange] = Field(min_length=1)


Sliced = NewType('Sliced', sc.Variable)


class SlicedOutputs(Data):
    sliced: Sliced


SLICES = _spec('slices', SlicedOutputs, params=SlicesParams, tables=('time_range',))


def sliced(params: SlicesParams) -> SlicedOutputs:
    """The counts of the sample in every time range, summed."""
    counts = _counts(params.sample)
    total = sum(float(counts[r.start : r.stop].sum()) for r in params.time_range)
    return SlicedOutputs(sliced=Sliced(sc.scalar(total)))


class Centre(BaseModel):
    """A beam centre as plain data: typed in, or found by ``FIND_CENTRE``."""

    x: float


class FindCentreParams(Data):
    direct_beam: DirectBeamFile


class FoundCentre(Data):
    centre: Centre


FIND_CENTRE = _spec('find-centre', FoundCentre, params=FindCentreParams)


def find_centre(params: FindCentreParams) -> FoundCentre:
    return FoundCentre(centre=Centre(x=float(_counts(params.direct_beam).mean())))


class CentredParams(Data):
    centre: Centre
    sample: SampleFile
    transmission: Transmission | None = None
    can: CanFile | None = None


Centred = NewType('Centred', sc.Variable)


class CentredOutputs(Data):
    centred: Centred


CENTRED_MULTI = _spec('sans-centred', CentredOutputs, params=CentredParams).tabulate(
    name='sans-centred-multi',
    version=1,
    tables={'sample': ('transmission',), 'can': ()},
)


def centred(params: Any) -> CentredOutputs:
    """
    The sample runs, each times its transmission if given, minus the cans,
    minus the centre.
    """
    rows = zip(params.sample, params.transmission, strict=True)
    samples = sum(_counts(s) * (1.0 if t is None else t.value) for s, t in rows)
    cans = sum((_counts(c) for c in params.can), 0.0)
    return CentredOutputs(centred=Centred(_array(samples - cans - params.centre.x)))


TOYS: dict[SpecId, Binding[Any, Any, Any]] = {
    SpecId.of(binding.spec): binding
    for binding in [
        StagedFunction(IOFQ, iofq),
        function_binding(IOFQ_V2, iofq_v2),
        function_binding(BEAM_CENTRE, beam_centre),
        StagedFunction(VANADIUM, vanadium),
        summing(NORMALIZE, normalize),
        summing(BACKGROUND, background),
        function_binding(CONTRIBUTE, contribute),
        function_binding(ANGLE, angle),
        function_binding(CUT, cut),
        kept_rows(STITCH, stitch),
        function_binding(EXPORT, export),
        function_binding(PARTS_SUM, parts_sum),
        summing(VOLUME, volume),
        function_binding(SLICER, slicer),
        kept_rows(SLICES, sliced),
        function_binding(FIND_CENTRE, find_centre),
        kept_rows(CENTRED_MULTI, centred),
    ]
}
"""The binding of each toy spec, by its ID."""


@pytest.fixture
def datasets(tmp_path: Path) -> FakeDatasets:
    return FakeDatasets(proposal='p1', directory=tmp_path)


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def history() -> Log:
    """The history of the backend, in memory, which an upgrade takes over."""
    return Log()


@pytest.fixture
def backends(
    datasets: FakeDatasets, store: FakeStore, history: Log
) -> Iterator[list[Backend]]:
    """The backend, and those that upgrades started after it; the current one last."""
    backends = [Backend(datasets, list(TOYS.values()), log=history, store=store)]
    yield backends
    for backend in backends:
        backend.close()


@pytest.fixture
def clients() -> list[Client]:
    """Every client that ``connect`` and ``upgrade`` made."""
    return []


@pytest.fixture
def upgrade(
    datasets: FakeDatasets,
    store: FakeStore,
    history: Log,
    backends: list[Backend],
    clients: list[Client],
) -> Callable[..., Client]:
    """
    A client of a new backend over the same datasets, history, and store,
    which offers the given toy specs, every one by default; ``connect`` then
    makes clients of it. The upgrade ends every client of the backend before
    it, and that backend once its persisted work is done.
    """

    def upgrade(specs: list[WorkflowSpec[Any, Any]] | None = None) -> Client:
        for client in clients:
            client.close()
        backends[-1].close()
        bind = (
            list(TOYS.values())
            if specs is None
            else [TOYS[SpecId.of(s)] for s in specs]
        )
        backends.append(Backend(datasets, bind, log=history, store=store))
        clients.append(Client(backends[-1], proposal='p1', submitter='anna'))
        return clients[-1]

    return upgrade


@pytest.fixture
def connect(backends: list[Backend], clients: list[Client]) -> Callable[..., Client]:
    """A new client of the current backend, by default for proposal p1."""

    def connect(proposal: str = 'p1', user: str = 'anna') -> Client:
        clients.append(Client(backends[-1], proposal=proposal, submitter=user))
        return clients[-1]

    return connect


@pytest.fixture
def client(connect: Callable[..., Client]) -> Client:
    return connect()


Measure = Callable[..., DatasetRef]


@pytest.fixture
def measure(datasets: FakeDatasets) -> Measure:
    """
    Make run ``n`` appear as a file of the given counts, with the given
    metadata; return its identity.
    """

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
def replay(backends: list[Backend], clients: list[Client]) -> Iterator[None]:
    """
    After the story, each client that has not ended runs every completed
    record whose outputs it keeps again as its request, and compares the
    outputs.

    This is a safety net over every story: a record says all that its outputs
    depend on. A reference to an output the client does not keep, such as one
    of the record of an accumulator's state, is replaced by the same output of
    that record run again. No record holds a reference to an accumulator.
    It runs before the backends are closed.
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
                return ref.model_copy(update={'record': again(records[ref.record]).id})
            return ref

        if record.id not in replays:
            request = record.request
            params = map_refs(request.params, source)
            replays[record.id] = client.compute(record.spec, params=params)
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
