# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Example workflows exercising the contract: a load-and-histogram pipeline to
stage, a per-run reduction, a combine step over a list of references, and
pipelines that take a list of runs and sum what each contributes, through an
aggregation the example provides as a package would, so a sum over runs,
chaining, and a growing series can be tried without instrument code.
``registry`` is importable by the subprocess launcher.

See docs/developer/aggregation.md.
"""

from __future__ import annotations

import operator
from functools import reduce
from pathlib import Path
from typing import Any, NewType

import sciline
import scipp as sc
from pydantic import BaseModel, Field

from .adapter import PipelineAdapter
from .binding import Inputs, Registry
from .spec import Array, ArraySpec, OpaqueFile, OutputRef, Quantity, WorkflowSpec


class LoadParams(BaseModel):
    run: OpaqueFile
    scale: float = 1.0


class LoadOutputs(BaseModel):
    data: Array(ArraySpec(dims=('x',), unit='counts'))
    total: Quantity


def edges(data: sc.DataArray, bins: int) -> sc.Variable:
    """Bin edges covering every point of an integer-spaced x coordinate."""
    return sc.linspace('x', 0.0, float(data.sizes['x']), bins + 1, unit='m')


def load_workflow() -> Any:
    def run(params: LoadParams, inputs: Inputs) -> dict[str, Any]:
        data = sc.io.load_hdf5(inputs.path(params.run)) * params.scale
        return {
            'data': data,
            'total': Quantity(value=float(data.sum().value), unit=str(data.unit)),
        }

    return run


LOAD = WorkflowSpec(
    name='load',
    version=1,
    title='Load',
    description='Load a run from a scipp HDF5 file and scale it.',
    params=LoadParams,
    outputs=LoadOutputs,
)


class RebinParams(BaseModel):
    data: Array(ArraySpec(dims=('x',), unit='counts'))
    bins: int = Field(default=4, ge=1)
    offset: Quantity | OutputRef | None = None


class RebinOutputs(BaseModel):
    result: Array(ArraySpec(dims=('x',), unit='counts'))


def rebin_workflow() -> Any:
    def run(params: RebinParams, inputs: Inputs) -> dict[str, Any]:
        data = inputs.array(params.data)
        if params.offset is not None:
            data = data + sc.scalar(params.offset.value, unit=params.offset.unit)
        return {'result': data.hist(x=edges(data, params.bins))}

    return run


REBIN = WorkflowSpec(
    name='rebin',
    version=1,
    title='Rebin',
    description='Histogram a run onto a coarser axis; the stage after loading.',
    params=RebinParams,
    outputs=RebinOutputs,
)


class SumParams(BaseModel):
    runs: list[Array(ArraySpec(dims=('x',), unit='counts'))]


class SumOutputs(BaseModel):
    total: Array(ArraySpec(dims=('x',), unit='counts'))
    per_run: dict[str, Array(ArraySpec(dims=('x',), unit='counts'))]
    totals: dict[str, Quantity]


def sum_workflow() -> Any:
    def run(params: SumParams, inputs: Inputs) -> dict[str, Any]:
        runs = [inputs.array(ref) for ref in params.runs]
        total = runs[0].copy()
        for r in runs[1:]:
            total += r
        return {
            'total': total,
            'per_run': {str(i): r for i, r in enumerate(runs)},
            'totals': {
                'x': Quantity(value=float(total.sum().value), unit=str(total.unit))
            },
        }

    return run


SUM = WorkflowSpec(
    name='sum',
    version=1,
    title='Sum',
    description='Combine runs by summation; the combine half of map-combine.',
    params=SumParams,
    outputs=SumOutputs,
)


class ExportParams(BaseModel):
    data: Array(ArraySpec(dims=('x',), unit='counts'))


class ExportOutputs(BaseModel):
    csv: OpaqueFile


def export_workflow() -> Any:
    def run(params: ExportParams, inputs: Inputs) -> dict[str, Any]:
        data = inputs.array(params.data)
        rows = zip(data.coords['x'].values, data.values, strict=True)
        text = 'x,counts\n' + ''.join(f'{x},{y}\n' for x, y in rows)
        return {'csv': text.encode()}

    return run


EXPORT = WorkflowSpec(
    name='export',
    version=1,
    title='Export',
    description='Write a run as CSV; an opaque file output.',
    params=ExportParams,
    outputs=ExportOutputs,
)


class FailParams(BaseModel):
    message: str = 'boom'


def fail_workflow() -> Any:
    def run(params: FailParams, inputs: Inputs) -> Any:
        raise RuntimeError(params.message)

    return run


FAIL = WorkflowSpec(
    name='fail',
    version=1,
    title='Fail',
    description='Always fails.',
    params=FailParams,
    outputs=LoadOutputs,
)


class SubtractParams(BaseModel):
    sample: OpaqueFile
    can: OpaqueFile


class SubtractOutputs(BaseModel):
    result: Array(ArraySpec(dims=('x',), unit='counts'))


def subtract_workflow() -> Any:
    def run(params: SubtractParams, inputs: Inputs) -> dict[str, Any]:
        sample = sc.io.load_hdf5(inputs.path(params.sample))
        can = sc.io.load_hdf5(inputs.path(params.can))
        return {'result': sample - can}

    return run


SUBTRACT = WorkflowSpec(
    name='subtract',
    version=1,
    title='Subtract',
    description='Subtract a can run from a sample run; two dataset fields, '
    'for exercising a nearest lookup fill.',
    params=SubtractParams,
    outputs=SubtractOutputs,
)


def registry() -> Registry:
    reg = Registry()
    for spec, factory in (
        (LOAD, load_workflow),
        (REBIN, rebin_workflow),
        (SUM, sum_workflow),
        (FAIL, fail_workflow),
        (HISTOGRAM, histogram_workflow),
        (NORMALIZE, normalize_workflow),
        (FLOORED, floored_workflow),
        (CONTRIBUTE, contribute_workflow),
        (COMBINE, combine_workflow),
        (EXPORT, export_workflow),
        (SUBTRACT, subtract_workflow),
        (BACKGROUND, background_workflow),
    ):
        reg.bind(spec, factory)
    return reg


def write_run(path: Path, values: list[float]) -> Path:
    """A synthetic 'raw' run for examples and tests."""
    data = sc.DataArray(
        sc.array(dims=['x'], values=values, unit='counts'),
        coords={'x': sc.arange('x', float(len(values)), unit='m')},
    )
    data.save_hdf5(path)
    return path


# A sciline pipeline behind the contract: filtering is the expensive part, and
# the bin count is what a person moves.

RawData = NewType('RawData', sc.DataArray)
Threshold = NewType('Threshold', float)
Filtered = NewType('Filtered', sc.DataArray)
Bins = NewType('Bins', int)
Histogram = NewType('Histogram', sc.DataArray)


def filter_data(data: RawData, threshold: Threshold) -> Filtered:
    """Stands in for the expensive stage: loading, masking, coordinate conversion."""
    kept = data.data > sc.scalar(threshold, unit=data.unit)
    filtered = data.copy()
    filtered.data = sc.where(kept, data.data, sc.scalar(0.0, unit=data.unit))
    return Filtered(filtered)


def histogram(data: Filtered, bins: Bins) -> Histogram:
    return Histogram(data.hist(x=edges(data, bins)))


class HistogramParams(BaseModel):
    data: Array(ArraySpec(dims=('x',), unit='counts'))
    threshold: float = 0.0
    bins: int = Field(default=4, ge=1)


class HistogramOutputs(BaseModel):
    histogram: Array(ArraySpec(dims=('x',), unit='counts'))


def histogram_workflow() -> PipelineAdapter:
    pipeline = sciline.Pipeline([filter_data, histogram])
    return PipelineAdapter(
        pipeline,
        keys={'data': RawData, 'threshold': Threshold, 'bins': Bins},
        resolve={'data': 'array'},
        targets={'histogram': Histogram},
    )


HISTOGRAM = WorkflowSpec(
    name='histogram',
    version=1,
    title='Histogram',
    description='Filter then histogram; a sciline pipeline a session stages.',
    params=HistogramParams,
    outputs=HistogramOutputs,
)


# One pipeline whose numerator and denominator add over runs: normalisation
# comes after them, and the run file is the key each member sets.

RunFile = NewType('RunFile', Path)
Floor = NewType('Floor', float)
Counts = NewType('Counts', sc.DataArray)
Numerator = NewType('Numerator', sc.DataArray)
Denominator = NewType('Denominator', sc.Variable)
Scale = NewType('Scale', float)
Normalized = NewType('Normalized', sc.DataArray)


def load_counts(path: RunFile) -> Counts:
    return Counts(sc.io.load_hdf5(path))


def numerator(counts: Counts, floor: Floor) -> Numerator:
    """Counts above the floor; summed over the members."""
    masked = counts.copy()
    masked.data = sc.where(
        counts.data > sc.scalar(floor, unit=counts.unit),
        counts.data,
        sc.zeros_like(counts.data),
    )
    return Numerator(masked)


def denominator(counts: Counts) -> Denominator:
    """The run's total, standing in for a monitor sum; summed over the members."""
    return Denominator(counts.data.sum())


def normalized(num: Numerator, den: Denominator, scale: Scale) -> Normalized:
    """Normalisation after the accumulation keys, which is what makes it additive."""
    return Normalized(num / den * scale)


def add(*parts: Any) -> Any:
    """The package's combine function; ``Buffered`` makes an accumulator of it."""
    return reduce(operator.add, parts)


ACCUMULATORS = {Numerator: sciline.Buffered(add), Denominator: sciline.Buffered(add)}


def normalize_pipeline() -> sciline.Pipeline:
    return sciline.Pipeline([load_counts, numerator, denominator, normalized])


def normalize_aggregation(pipeline: sciline.Pipeline) -> sciline.Aggregation:
    """
    The sum over runs: what the package provides, for notebooks and bindings.

    Built from a pipeline with its parameters set, as every aggregation is.
    """
    return sciline.Aggregation(
        pipeline, members=[RunFile], accumulators=ACCUMULATORS, outputs=[Normalized]
    )


class NormalizeParams(BaseModel):
    runs: list[OpaqueFile] = Field(min_length=1)
    floor: float = 0.0
    scale: float = 1.0


class NormalizeOutputs(BaseModel):
    normalized: Array(ArraySpec(dims=('x',)))
    numerator: Array()
    denominator: Array()


NORMALIZE = WorkflowSpec(
    name='normalize',
    version=1,
    title='Normalize',
    description='Numerator and denominator summed over runs, then normalised. '
    'The numerator and denominator are intermediates a request may ask for.',
    params=NormalizeParams,
    outputs=NormalizeOutputs,
    intermediates=('numerator', 'denominator'),
)


def normalize_workflow() -> PipelineAdapter:
    return PipelineAdapter(
        normalize_pipeline(),
        keys={'runs': RunFile, 'floor': Floor, 'scale': Scale},
        resolve={'runs': 'path'},
        targets={
            'normalized': Normalized,
            'numerator': Numerator,
            'denominator': Denominator,
        },
        aggregations={'runs': normalize_aggregation},
    )


# A value that differs per run is a second column of the member table: each
# element of the list is a row, a run with its own floor.


class FlooredRun(BaseModel):
    """One member of the sum: a run and the floor its counts are cut at."""

    run: OpaqueFile
    floor: float = 0.0


class FlooredParams(BaseModel):
    runs: list[FlooredRun] = Field(min_length=1)
    scale: float = 1.0


FLOORED = WorkflowSpec(
    name='normalize-floored',
    version=1,
    title='Normalize, a floor per run',
    description='As Normalize, with each run cut at its own floor.',
    params=FlooredParams,
    outputs=NormalizeOutputs,
    intermediates=('numerator', 'denominator'),
)


def floored_aggregation(pipeline: sciline.Pipeline) -> sciline.Aggregation:
    """The sum over runs with the run file and the floor as member keys."""
    return sciline.Aggregation(
        pipeline,
        members=[RunFile, Floor],
        accumulators=ACCUMULATORS,
        outputs=[Normalized],
    )


def floored_workflow() -> PipelineAdapter:
    return PipelineAdapter(
        normalize_pipeline(),
        keys={'runs': {'run': RunFile, 'floor': Floor}, 'scale': Scale},
        resolve={'runs': {'run': 'path'}},
        targets={
            'normalized': Normalized,
            'numerator': Numerator,
            'denominator': Denominator,
        },
        aggregations={'runs': floored_aggregation},
    )


# The same sum split into two specs, so that each run is reduced in its own
# request: CONTRIBUTE reduces one run to what it adds to the sum, and COMBINE
# takes references to those outputs, combines, and normalises. Both are built
# from the one aggregation of NORMALIZE, and each record describes only its own
# computation.


class ContributeParams(BaseModel):
    run: OpaqueFile
    floor: float = 0.0


class Contribution(BaseModel):
    """What one run adds to the sum, the values at the accumulation keys."""

    numerator: Array()
    denominator: Array()


CONTRIBUTE = WorkflowSpec(
    name='normalize-contribute',
    version=1,
    title='Normalize: contribute one run',
    description="One run's numerator and denominator, to be combined.",
    params=ContributeParams,
    outputs=Contribution,
)


def contribute_workflow() -> Any:
    def run(params: ContributeParams, inputs: Inputs) -> dict[str, Any]:
        pipeline = normalize_pipeline()
        pipeline[Floor] = params.floor
        part = normalize_aggregation(pipeline).contribute(
            {RunFile: inputs.path(params.run)}
        )
        return {'numerator': part[Numerator], 'denominator': part[Denominator]}

    return run


class CombineParams(BaseModel):
    # The outputs model of CONTRIBUTE is the row model on purpose: a row holds a
    # reference to each output of one contribute record, by output name.
    parts: list[Contribution] = Field(min_length=1)
    scale: float = 1.0


class CombineOutputs(BaseModel):
    normalized: Array(ArraySpec(dims=('x',)))


COMBINE = WorkflowSpec(
    name='normalize-combine',
    version=1,
    title='Normalize: combine contributions',
    description='Combine the contributions of runs and normalise.',
    params=CombineParams,
    outputs=CombineOutputs,
)


def combine_workflow() -> Any:
    def run(params: CombineParams, inputs: Inputs) -> dict[str, Any]:
        pipeline = normalize_pipeline()
        pipeline[Scale] = params.scale
        aggregation = normalize_aggregation(pipeline)
        parts = [
            {
                Numerator: inputs.array(part.numerator),
                Denominator: inputs.array(part.denominator),
            }
            for part in params.parts
        ]
        return {
            'normalized': aggregation.finalize(aggregation.combine(parts))[Normalized]
        }

    return run


# Two member tables: sample runs and background runs are summed separately, and
# the background is subtracted after both sums.

SampleFile = NewType('SampleFile', Path)
BackgroundFile = NewType('BackgroundFile', Path)
SampleCounts = NewType('SampleCounts', sc.DataArray)
BackgroundCounts = NewType('BackgroundCounts', sc.DataArray)
Subtracted = NewType('Subtracted', sc.DataArray)


def sample_counts(path: SampleFile) -> SampleCounts:
    return SampleCounts(sc.io.load_hdf5(path))


def background_counts(path: BackgroundFile) -> BackgroundCounts:
    return BackgroundCounts(sc.io.load_hdf5(path))


def subtracted(sample: SampleCounts, background: BackgroundCounts) -> Subtracted:
    return Subtracted(sample - background)


class BackgroundParams(BaseModel):
    sample_runs: list[OpaqueFile] = Field(min_length=1)
    background_runs: list[OpaqueFile] = Field(min_length=1)


class BackgroundOutputs(BaseModel):
    subtracted: Array(ArraySpec(dims=('x',), unit='counts'))


BACKGROUND = WorkflowSpec(
    name='background',
    version=1,
    title='Background subtraction',
    description='Summed sample runs minus summed background runs: a sum over '
    'runs with two member tables.',
    params=BackgroundParams,
    outputs=BackgroundOutputs,
)


def sample_aggregation(pipeline: sciline.Pipeline) -> sciline.Aggregation:
    """The sum over sample runs, without outputs: it shares a final stage."""
    return sciline.Aggregation(
        pipeline,
        members=[SampleFile],
        accumulators={SampleCounts: sciline.Buffered(add)},
    )


def background_aggregation(pipeline: sciline.Pipeline) -> sciline.Aggregation:
    """The sum over background runs, without outputs: it shares a final stage."""
    return sciline.Aggregation(
        pipeline,
        members=[BackgroundFile],
        accumulators={BackgroundCounts: sciline.Buffered(add)},
    )


def background_workflow() -> PipelineAdapter:
    return PipelineAdapter(
        sciline.Pipeline([sample_counts, background_counts, subtracted]),
        keys={'sample_runs': SampleFile, 'background_runs': BackgroundFile},
        resolve={'sample_runs': 'path', 'background_runs': 'path'},
        targets={'subtracted': Subtracted},
        aggregations={
            'sample_runs': sample_aggregation,
            'background_runs': background_aggregation,
        },
    )
