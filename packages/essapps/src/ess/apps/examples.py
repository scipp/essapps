# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Example workflows exercising the contract: a load-and-histogram stage, a
per-run reduction, a combine step, and a workflow with a declared contribution
over two accumulation keys (D15), so map-combine, chaining, and a combined
series can be tried without instrument code. ``registry`` is importable by the
subprocess launcher.
"""

from __future__ import annotations

import operator
from functools import reduce
from pathlib import Path
from typing import Any, NewType

import sciline
import scipp as sc
from pydantic import BaseModel, Field

from .aggregation import AggregatePipeline
from .binding import Inputs, Registry
from .spec import Array, ArraySpec, OpaqueFile, OutputRef, Quantity, WorkflowSpec
from .warm import WarmPipeline


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
    'for exercising an as-of lookup fill.',
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
        (EXPORT, export_workflow),
        (SUBTRACT, subtract_workflow),
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


# A sciline pipeline behind the contract: threshold is held, bins is a stage input.

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


def histogram_workflow() -> WarmPipeline:
    pipeline = sciline.Pipeline([filter_data, histogram])
    return WarmPipeline(
        pipeline,
        keys={'data': RawData, 'threshold': Threshold, 'bins': Bins},
        resolve={'data': 'array'},
        targets={'histogram': Histogram},
        stage_inputs=['bins'],
    )


HISTOGRAM = WorkflowSpec(
    name='histogram',
    version=1,
    title='Histogram',
    description='Filter then histogram; a sciline pipeline kept warm in a session.',
    params=HistogramParams,
    outputs=HistogramOutputs,
)


# A declared additive combine (D15): two accumulation keys, normalisation after
# them, and the run file as the member key.

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


class NormalizeParams(BaseModel):
    run: OpaqueFile
    floor: float = 0.0
    scale: float = 1.0


class NormalizeOutputs(BaseModel):
    contribution: Array(ArraySpec(dims=('x',)))
    normalized: Array(ArraySpec(dims=('x',))) | None = None


def normalize_pipeline() -> sciline.Pipeline:
    return sciline.Pipeline([load_counts, numerator, denominator, normalized])


def normalize_workflow() -> AggregatePipeline:
    return AggregatePipeline(normalize_pipeline(), **NORMALIZE_WIRING)


NORMALIZE = WorkflowSpec(
    name='normalize',
    version=1,
    title='Normalize',
    description='Sum a numerator and a denominator over runs, then normalise: '
    'the declared additive combine.',
    params=NormalizeParams,
    outputs=NormalizeOutputs,
    contribution='contribution',
    finalize_params=frozenset({'scale'}),
)

NORMALIZE_WIRING: dict[str, Any] = {
    'keys': {'run': RunFile, 'floor': Floor, 'scale': Scale},
    'resolve': {'run': 'path'},
    'targets': {'normalized': Normalized},
    'contribution': NORMALIZE.contribution,
    'accumulation_keys': {'numerator': Numerator, 'denominator': Denominator},
    'combine': add,
    'finalize_params': NORMALIZE.finalize_params,
}
"""How NORMALIZE binds to its pipeline: the field-to-key maps, the accumulation
keys, and the spec's own declaration of what is combined and what finalize
reads."""
