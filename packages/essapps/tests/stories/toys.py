# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)

"""
Toy specs of the user stories (docs/developer/user-stories.md).

A file made by ``measure`` holds a list of counts. The toy specs compute
what the table in user-stories.md says, so that every number can be checked
by hand. Each toy workflow takes an instance of its spec's params model,
where each run is the local path of its file, and reads the counts of each
file.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any

import numpy as np
import scipp as sc
from pydantic import BaseModel, ConfigDict, Field

from ess.apps import (
    SpecId,
)
from ess.apps.testing import (
    StagedFunction,
    kept_rows,
    read_file,
)
from ess.spec import (
    Binding,
    DriverBinding,
    File,
    FileRef,
    NoParams,
    Referenced,
    WorkflowSpec,
    function_binding,
)


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
    return np.asarray(read_file(path), dtype=float)


def _array(values: Any) -> sc.Variable:
    return sc.array(dims=['q'], values=np.asarray(values, dtype=float))


Run = Annotated[Path, File()]


"""A measured run, by its file; each role of a run is an alias of it."""


Sample = Run


"""A run of the sample."""


Can = Run


"""A run of the empty can, subtracted from a sample run."""


DirectBeam = Run


"""A run of the direct beam, without a sample."""


Vanadium = Run


"""A run of vanadium, which a normalization is computed from."""


Background = Run


"""A background run, subtracted from sample runs."""


Reference = Run


"""A run of a reference, such as a supermirror, that a reflectivity divides by."""


class Reduction(BaseModel):
    bins: int = 2
    threshold: float = 0.0


class BeamCentre(BaseModel):
    """
    A beam centre as a number, so a request may type it in, or give it by
    reference to the output of ``BEAM_CENTRE`` (C1, I2). The default is the
    nominal centre.
    """

    x: float = 0.0


class ComputedNormalization(Data):
    """
    The normalization that ``VANADIUM`` computes from a vanadium run: a group
    that holds a data value, so a reference fills its place,
    ``Referenced[...]``.
    """

    factor: sc.Variable


PublishedNormalization = Annotated[Path, File()]


"""
A normalization published to the catalogue, read back as a file that holds
its factor (C2). A place takes it, or a computed normalization by
reference, ``PublishedNormalization | Referenced[ComputedNormalization]``.
"""


class IofQParams(Data):
    reduction: Reduction = Field(default_factory=Reduction)
    sample: Sample
    beam_centre: BeamCentre | Referenced[BeamCentre] = BeamCentre()
    normalization: PublishedNormalization | Referenced[ComputedNormalization] | None


class IofQOutputs(Data):
    iofq: sc.Variable
    masked: sc.Variable


IOFQ = _spec('sans-iofq', IofQOutputs, params=IofQParams)


def iofq(params: IofQParams) -> IofQOutputs:
    return _reduce(params, _counts(params.sample))


def _reduce(params: IofQParams, counts: np.ndarray) -> IofQOutputs:
    """``IOFQ`` of the given counts, which may differ from the sample's."""
    reduction = params.reduction
    masked = np.where(counts < reduction.threshold, 0.0, counts)
    reduced = masked - params.beam_centre.x
    reduced = reduced / _factor(params.normalization)
    return IofQOutputs(
        iofq=_array(reduced.reshape(reduction.bins, -1).sum(axis=1)),
        masked=_array(masked),
    )


def _factor(
    normalization: PublishedNormalization | ComputedNormalization | None,
) -> float:
    """The factor of a normalization, read from the file if it is published (C2)."""
    if normalization is None:
        return 1.0
    if isinstance(normalization, Path):
        return float(read_file(normalization))
    return normalization.factor.value


class IofQCanParams(IofQParams):
    can: Can


IOFQ_CAN = _spec('sans-iofq-can', IofQOutputs, params=IofQCanParams)


def iofq_can(params: IofQCanParams) -> IofQOutputs:
    return _reduce(params, _counts(params.sample) - _counts(params.can))


class ReductionV2(BaseModel):
    bins: int = 4
    mask_below: float = 0.0


class IofQV2Params(Data):
    reduction: ReductionV2 = Field(default_factory=ReductionV2)
    sample: Sample
    beam_centre: BeamCentre | Referenced[BeamCentre] = BeamCentre()
    normalization: PublishedNormalization | Referenced[ComputedNormalization] | None


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
    direct_beam: DirectBeam


class CentreOutputs(Data):
    centre: BeamCentre


BEAM_CENTRE = _spec('beam-centre', CentreOutputs, params=BeamCentreParams)


def beam_centre(params: BeamCentreParams) -> CentreOutputs:
    return CentreOutputs(centre=BeamCentre(x=float(_counts(params.direct_beam).mean())))


class Scaling(BaseModel):
    """Shared by VANADIUM and NORMALIZE."""

    scale: float = 1.0


class VanadiumParams(Data):
    scaling: Scaling = Field(default_factory=Scaling)
    vanadium: Vanadium


class VanadiumOutputs(Data):
    normalization: ComputedNormalization


VANADIUM = _spec('vanadium', VanadiumOutputs, params=VanadiumParams)


def vanadium(params: VanadiumParams) -> VanadiumOutputs:
    total = float(_counts(params.vanadium).sum())
    return VanadiumOutputs(
        normalization=ComputedNormalization(
            factor=sc.scalar(total * params.scaling.scale)
        )
    )


class NormalizeParams(Data):
    scaling: Scaling = Field(default_factory=Scaling)
    runs: list[Sample] = Field(min_length=1)


class NormalizedOutputs(Data):
    normalized: sc.Variable


NORMALIZE = _spec(
    'normalize', NormalizedOutputs, params=NormalizeParams, tables=('runs',)
)


def normalize(runs: np.ndarray, scaling: Scaling) -> NormalizedOutputs:
    return NormalizedOutputs(normalized=_array(runs / runs.sum() * scaling.scale))


class BackgroundParams(Data):
    sample_runs: list[Sample] = Field(min_length=1)
    background_runs: list[Background] = Field(min_length=1)


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
    sample: Sample


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
        transmission=Transmission(fraction=sc.scalar(float(counts[0] / counts.sum()))),
    )


class PartsParams(Data):
    parts: list[Referenced[Parts]]


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
            self._sums[table] += _counts(run)  # in place from the second row

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
    sample: Sample


class RunsParams(Data):
    runs: list[Sample] = Field(min_length=1)


ANGLE = _spec('angle', VolumeOutputs, params=AngleParams)


VOLUME = _spec('volume', VolumeOutputs, params=RunsParams, tables=('runs',))


def angle(params: AngleParams) -> VolumeOutputs:
    return VolumeOutputs(volume=Volume(counts=_array(_counts(params.sample))))


def volume(runs: np.ndarray) -> VolumeOutputs:
    return VolumeOutputs(volume=Volume(counts=_array(runs)))


class Position(BaseModel):
    index: int


class CutParams(Data):
    position: Position
    volume: Referenced[Volume]


class CutOutputs(Data):
    cut: sc.Variable


CUT = _spec('cut', CutOutputs, params=CutParams)


def cut(params: CutParams) -> CutOutputs:
    counts = params.volume.counts
    return CutOutputs(cut=counts['q', params.position.index].copy())


class StitchParams(Data):
    runs: list[Sample] = Field(min_length=1)
    reference: Reference


class Reflectivity(Data):
    """A reflectivity curve, stitched by ``STITCH`` and exported by ``EXPORT``."""

    curve: sc.Variable


class StitchOutputs(Data):
    stitched: Reflectivity


STITCH = _spec('stitch', StitchOutputs, params=StitchParams, tables=('runs',))


def stitch(params: StitchParams) -> StitchOutputs:
    reference = _counts(params.reference)
    curves = [_counts(run) / reference for run in params.runs]
    scaled = [curves[0]]
    for curve in curves[1:]:
        scaled.append(curve * scaled[-1][-1] / curve[0])
    return StitchOutputs(stitched=Reflectivity(curve=_array(np.concatenate(scaled))))


class ExportParams(Data):
    result: Referenced[Reflectivity]


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
    sample: Sample


class SlicerOutputs(Data):
    slices: list[TimeRange]


SLICER = _spec('slicer', SlicerOutputs, params=SlicerParams)


def slicer(params: SlicerParams) -> SlicerOutputs:
    """A slice of every two bins of the log of the sample run."""
    n = len(_counts(params.sample))
    return SlicerOutputs(
        slices=[TimeRange(start=i, stop=min(i + 2, n)) for i in range(0, n, 2)]
    )


class SlicesParams(Data):
    sample: Sample
    time_range: list[TimeRange | Referenced[TimeRange]] = Field(min_length=1)


class SlicedOutputs(Data):
    sliced: sc.Variable


SLICES = _spec('slices', SlicedOutputs, params=SlicesParams, tables=('time_range',))


def sliced(params: SlicesParams) -> SlicedOutputs:
    """The counts of the sample in every time range, summed."""
    counts = _counts(params.sample)
    total = sum(float(counts[r.start : r.stop].sum()) for r in params.time_range)
    return SlicedOutputs(sliced=sc.scalar(total))


class CentredParams(Data):
    centre: BeamCentre | Referenced[BeamCentre]
    transmission: Referenced[Transmission]  # of the summed sample runs
    sample: list[Sample] = Field(min_length=1)
    can: list[Can] = Field(min_length=1)


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
    The sample runs, times their transmission, minus the cans, minus the
    centre.
    """
    fraction = params.transmission.fraction.value
    samples = sum(_counts(s) for s in params.sample) * fraction
    cans = sum((_counts(c) for c in params.can), 0.0)
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
        kept_rows(CENTRED_MULTI, centred),
    ]
}


"""The binding of each toy spec, by its ID."""


Measure = Callable[..., FileRef]
