# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Toy specs and fixtures of the user stories (docs/developer/user-stories.md).

A dataset made by ``measure`` is a file of a list of counts. The toy specs
compute what the table in user-stories.md says, so that every number can be
checked by hand. Each toy workflow takes an instance of its spec's params model,
where each run is a group with the path of its file, and reads the counts of
each file.

After each story, every record whose outputs a client of the story still keeps
is run again as its request by that client, and must give the same outputs
(``replay``).
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any, Literal

import numpy as np
import pytest
import scipp as sc
from pydantic import BaseModel, ConfigDict, Field

from ess.dispatch import (
    Backend,
    Client,
    ClientEnded,
    NotReadable,
    Record,
    SpecId,
    Status,
)
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


class Run(BaseModel):
    """A measured run, by its file; each role of a run is a subclass."""

    file: Path


class SampleRun(Run):
    """A run of the sample."""


class CanRun(Run):
    """A run of the empty can, subtracted from a sample run."""


class DirectBeamRun(Run):
    """A run of the direct beam, without a sample."""


class VanadiumRun(Run):
    """A run of vanadium, which a normalization is computed from."""


class BackgroundRun(Run):
    """A background run, subtracted from sample runs."""


class ReferenceRun(Run):
    """A run of a reference, such as a supermirror, that a reflectivity divides by."""


class Reduction(BaseModel):
    bins: int = 2
    threshold: float = 0.0


class BeamCentre(Data):
    """
    The beam centre that ``BEAM_CENTRE`` finds, as a position: a group that
    holds a data value, so a request gives it by reference only (C1). Compare
    :class:`Centre`, a beam centre that a request may also type in (I2).
    """

    position: sc.Variable


class ComputedNormalization(Data):
    """
    The normalization that ``VANADIUM`` computes from a vanadium run: a group
    that holds a data value, so a request gives it by reference only.
    """

    kind: Literal['computed'] = 'computed'
    factor: sc.Variable


class PublishedNormalization(Data):
    """A normalization published to the catalogue, as a file of its factor (C2)."""

    kind: Literal['published'] = 'published'
    file: Path


Normalization = Annotated[
    ComputedNormalization | PublishedNormalization, Field(discriminator='kind')
]


class IofQParams(Data):
    reduction: Reduction = Field(default_factory=Reduction)
    sample: SampleRun
    beam_centre: BeamCentre | None = None
    normalization: Normalization | None = None


class IofQOutputs(Data):
    iofq: sc.Variable
    masked: sc.Variable


IOFQ = _spec('sans-iofq', IofQOutputs, params=IofQParams)


def iofq(params: IofQParams) -> IofQOutputs:
    return _reduce(params, _counts(params.sample.file))


def _reduce(params: IofQParams, counts: np.ndarray) -> IofQOutputs:
    """``IOFQ`` of the given counts, which may differ from the sample's."""
    reduction = params.reduction
    masked = np.where(counts < reduction.threshold, 0.0, counts)
    centre = params.beam_centre
    reduced = masked - (0.0 if centre is None else centre.position.value)
    reduced = reduced / _factor(params.normalization)
    return IofQOutputs(
        iofq=_array(reduced.reshape(reduction.bins, -1).sum(axis=1)),
        masked=_array(masked),
    )


def _factor(
    normalization: ComputedNormalization | PublishedNormalization | None,
) -> float:
    """The factor of a normalization, read from its file if it is published."""
    if normalization is None:
        return 1.0
    if isinstance(normalization, PublishedNormalization):
        return float(read_dataset(normalization.file))
    return normalization.factor.value


class IofQCanParams(IofQParams):
    can: CanRun


IOFQ_CAN = _spec('sans-iofq-can', IofQOutputs, params=IofQCanParams)


def iofq_can(params: IofQCanParams) -> IofQOutputs:
    return _reduce(params, _counts(params.sample.file) - _counts(params.can.file))


class ReductionV2(BaseModel):
    bins: int = 4
    mask_below: float = 0.0


class IofQV2Params(Data):
    reduction: ReductionV2 = Field(default_factory=ReductionV2)
    sample: SampleRun
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
    direct_beam: DirectBeamRun


class CentreOutputs(Data):
    centre: BeamCentre


BEAM_CENTRE = _spec('beam-centre', CentreOutputs, params=BeamCentreParams)


def beam_centre(params: BeamCentreParams) -> CentreOutputs:
    return CentreOutputs(
        centre=BeamCentre(
            position=sc.scalar(float(_counts(params.direct_beam.file).mean()))
        )
    )


class Scaling(BaseModel):
    """Shared by VANADIUM and NORMALIZE."""

    scale: float = 1.0


class VanadiumParams(Data):
    scaling: Scaling = Field(default_factory=Scaling)
    vanadium: VanadiumRun


class VanadiumOutputs(Data):
    normalization: ComputedNormalization


VANADIUM = _spec('vanadium', VanadiumOutputs, params=VanadiumParams)


def vanadium(params: VanadiumParams) -> VanadiumOutputs:
    total = float(_counts(params.vanadium.file).sum())
    return VanadiumOutputs(
        normalization=ComputedNormalization(
            factor=sc.scalar(total * params.scaling.scale)
        )
    )


class NormalizeParams(Data):
    scaling: Scaling = Field(default_factory=Scaling)
    runs: list[SampleRun] = Field(min_length=1)


class NormalizedOutputs(Data):
    normalized: sc.Variable


NORMALIZE = _spec(
    'normalize', NormalizedOutputs, params=NormalizeParams, tables=('runs',)
)


def normalize(runs: np.ndarray, scaling: Scaling) -> NormalizedOutputs:
    return NormalizedOutputs(normalized=_array(runs / runs.sum() * scaling.scale))


class BackgroundParams(Data):
    sample_runs: list[SampleRun] = Field(min_length=1)
    background_runs: list[BackgroundRun] = Field(min_length=1)


class BackgroundOutputs(Data):
    subtracted: sc.Variable


BACKGROUND = _spec(
    'background',
    BackgroundOutputs,
    params=BackgroundParams,
    tables=('sample_runs', 'background_runs'),
)


def background(
    sample_runs: np.ndarray, background_runs: np.ndarray
) -> BackgroundOutputs:
    return BackgroundOutputs(subtracted=_array(sample_runs - background_runs))


class Transmission(Data):
    """The transmission of a sample run, computed by ``CONTRIBUTE``."""

    fraction: sc.Variable


class Parts(Data):
    """The numerator and the denominator of one run, summed by ``PARTS_SUM``."""

    numerator: sc.Variable
    denominator: sc.Variable


class ContributeParams(Data):
    sample: SampleRun


class ContributeOutputs(Data):
    parts: Parts
    transmission: Transmission


CONTRIBUTE = _spec('sans-contribute', ContributeOutputs, params=ContributeParams)


def contribute(params: ContributeParams) -> ContributeOutputs:
    counts = _counts(params.sample.file)
    return ContributeOutputs(
        parts=Parts(
            numerator=_array(counts), denominator=sc.scalar(float(counts.sum()))
        ),
        transmission=Transmission(fraction=sc.scalar(float(counts[0] / counts.sum()))),
    )


class PartsParams(Data):
    parts: list[Parts]


class PartsOutputs(Data):
    numerator: sc.Variable
    denominator: sc.Variable


PARTS_SUM = _spec('sans-parts-sum', PartsOutputs, params=PartsParams)


def parts_sum(params: PartsParams) -> PartsOutputs:
    def total(field: str) -> sc.Variable:
        return functools.reduce(sc.add, [getattr(p, field) for p in params.parts])

    return PartsOutputs(numerator=total('numerator'), denominator=total('denominator'))


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

    def push(self, table: str, rows: Sequence[Any]) -> None:
        for run in rows:
            self._sums[table] += _counts(run.file)  # in place from the second row

    def outputs(self) -> BaseModel:
        return self._finalize(**self._sums, **self._fixed)


def summing(
    spec: WorkflowSpec[Any, Any], finalize: Callable[..., BaseModel]
) -> DriverBinding:
    """The binding of a spec over tables of runs, by :class:`Summing`."""
    return DriverBinding(spec, functools.partial(Summing, finalize, spec))


class Volume(Data):
    """A volume of counts, of which a client selects a part and ``CUT`` cuts."""

    counts: sc.Variable


class VolumeOutputs(Data):
    volume: Volume


class AngleParams(Data):
    sample: SampleRun


class RunsParams(Data):
    runs: list[SampleRun] = Field(min_length=1)


ANGLE = _spec('angle', VolumeOutputs, params=AngleParams)
VOLUME = _spec('volume', VolumeOutputs, params=RunsParams, tables=('runs',))


def angle(params: AngleParams) -> VolumeOutputs:
    return VolumeOutputs(volume=Volume(counts=_array(_counts(params.sample.file))))


def volume(runs: np.ndarray) -> VolumeOutputs:
    return VolumeOutputs(volume=Volume(counts=_array(runs)))


class Position(BaseModel):
    index: int


class CutParams(Data):
    position: Position
    volume: Volume


class CutOutputs(Data):
    cut: sc.Variable


CUT = _spec('cut', CutOutputs, params=CutParams)


def cut(params: CutParams) -> CutOutputs:
    counts = params.volume.counts
    return CutOutputs(cut=counts['q', params.position.index].copy())


class StitchParams(Data):
    runs: list[SampleRun] = Field(min_length=1)
    reference: ReferenceRun


class Reflectivity(Data):
    """A reflectivity curve, stitched by ``STITCH`` and exported by ``EXPORT``."""

    curve: sc.Variable


class StitchOutputs(Data):
    stitched: Reflectivity


STITCH = _spec('stitch', StitchOutputs, params=StitchParams, tables=('runs',))


def stitch(params: StitchParams) -> StitchOutputs:
    reference = _counts(params.reference.file)
    curves = [_counts(run.file) / reference for run in params.runs]
    scaled = [curves[0]]
    for curve in curves[1:]:
        scaled.append(curve * scaled[-1][-1] / curve[0])
    return StitchOutputs(stitched=Reflectivity(curve=_array(np.concatenate(scaled))))


class ExportParams(Data):
    result: Reflectivity


class ExportOutputs(BaseModel):
    text: str


EXPORT = _spec('export', ExportOutputs, params=ExportParams)


def export(params: ExportParams) -> ExportOutputs:
    return ExportOutputs(text=','.join(str(v) for v in params.result.curve.values))


class TimeRange(BaseModel):
    """The time bins ``start`` to ``stop`` of a run, ``stop`` excluded."""

    start: int
    stop: int


class SlicerParams(Data):
    sample: SampleRun


class SlicerOutputs(Data):
    slices: list[TimeRange]


SLICER = _spec('slicer', SlicerOutputs, params=SlicerParams)


def slicer(params: SlicerParams) -> SlicerOutputs:
    """A slice of every two bins of the log of the sample run."""
    n = len(_counts(params.sample.file))
    return SlicerOutputs(
        slices=[TimeRange(start=i, stop=min(i + 2, n)) for i in range(0, n, 2)]
    )


class SlicesParams(Data):
    sample: SampleRun
    time_range: list[TimeRange] = Field(min_length=1)


class SlicedOutputs(Data):
    sliced: sc.Variable


SLICES = _spec('slices', SlicedOutputs, params=SlicesParams, tables=('time_range',))


def sliced(params: SlicesParams) -> SlicedOutputs:
    """The counts of the sample in every time range, summed."""
    counts = _counts(params.sample.file)
    total = sum(float(counts[r.start : r.stop].sum()) for r in params.time_range)
    return SlicedOutputs(sliced=sc.scalar(total))


class Centre(BaseModel):
    """
    A beam centre as a number, so a request may type it in, or give it by
    reference to the output of ``FIND_CENTRE`` (I2). Compare
    :class:`BeamCentre`, which a request gives by reference only (C1).
    """

    x: float


class FindCentreParams(Data):
    direct_beam: DirectBeamRun


class FoundCentre(Data):
    centre: Centre


FIND_CENTRE = _spec('find-centre', FoundCentre, params=FindCentreParams)


def find_centre(params: FindCentreParams) -> FoundCentre:
    return FoundCentre(centre=Centre(x=float(_counts(params.direct_beam.file).mean())))


class SampleRunWithTransmission(SampleRun):
    """A sample run and its transmission, or None: a group that holds a file."""

    transmission: Transmission | None


class CentredParams(Data):
    centre: Centre
    sample: list[SampleRunWithTransmission] = Field(min_length=1)
    can: list[CanRun] = Field(min_length=1)


class CentredOutputs(Data):
    centred: sc.Variable


CENTRED_MULTI = _spec(
    'sans-centred-multi',
    CentredOutputs,
    params=CentredParams,
    tables=('sample', 'can'),
)


def centred(params: CentredParams) -> CentredOutputs:
    """
    The sample runs, each times its transmission if given, minus the cans,
    minus the centre.
    """
    samples = sum(
        _counts(s.file)
        * (1.0 if s.transmission is None else s.transmission.fraction.value)
        for s in params.sample
    )
    cans = sum((_counts(c.file) for c in params.can), 0.0)
    return CentredOutputs(centred=_array(samples - cans - params.centre.x))


TOYS: dict[SpecId, Binding[Any, Any]] = {
    SpecId.of(binding.spec): binding
    for binding in [
        StagedFunction(IOFQ, iofq),
        function_binding(IOFQ_V2, iofq_v2),
        function_binding(IOFQ_CAN, iofq_can),
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
        except NotReadable:
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
        except NotReadable:  # not kept by this client
            continue
        where = f'{record.id} {record.spec} {record.label} {record.member}'
        assert_close(client.output(again(record)), expected, where=where)
