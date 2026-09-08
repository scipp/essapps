# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Example workflows exercising the contract: a load-and-histogram stage, a
per-run reduction, and a combine step, so map-combine and chaining can be tried
without instrument code. ``registry`` is importable by the subprocess launcher.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import scipp as sc
from pydantic import BaseModel, Field

from .binding import Registry
from .spec import Array, ArraySpec, OpaqueFile, Quantity, Ref, WorkflowSpec


class LoadParams(BaseModel):
    run: OpaqueFile
    scale: float = 1.0


class LoadOutputs(BaseModel):
    data: Array(ArraySpec(dims=('x',), unit='counts'))
    total: Quantity


def load_workflow() -> Any:
    def run(params: LoadParams) -> LoadOutputs:
        data = sc.io.load_hdf5(params.run) * params.scale
        return LoadOutputs(
            data=data,
            total=Quantity(value=float(data.sum().value), unit=str(data.unit)),
        )

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
    offset: Quantity | Ref | None = None


class RebinOutputs(BaseModel):
    result: Array(ArraySpec(dims=('x',), unit='counts'))


def rebin_workflow() -> Any:
    def run(params: RebinParams) -> RebinOutputs:
        data = params.data
        if params.offset is not None:
            data = data + sc.scalar(params.offset.value, unit=params.offset.unit)
        edges = sc.linspace(
            'x', data.coords['x'].min(), data.coords['x'].max(), params.bins + 1
        )
        return RebinOutputs(result=data.hist(x=edges))

    return run


REBIN = WorkflowSpec(
    name='rebin',
    version=1,
    title='Rebin',
    description='Histogram a run onto a coarser axis; the cheap stage after loading.',
    params=RebinParams,
    outputs=RebinOutputs,
    cheap=frozenset({'bins'}),
)


class SumParams(BaseModel):
    runs: list[Array(ArraySpec(dims=('x',), unit='counts'))]


class SumOutputs(BaseModel):
    total: Array(ArraySpec(dims=('x',), unit='counts'))
    per_run: dict[str, Array(ArraySpec(dims=('x',), unit='counts'))]


def sum_workflow() -> Any:
    def run(params: SumParams) -> SumOutputs:
        total = params.runs[0].copy()
        for r in params.runs[1:]:
            total += r
        return SumOutputs(
            total=total, per_run={str(i): r for i, r in enumerate(params.runs)}
        )

    return run


SUM = WorkflowSpec(
    name='sum',
    version=1,
    title='Sum',
    description='Combine runs by summation; the combine half of map-combine.',
    params=SumParams,
    outputs=SumOutputs,
)


class FailParams(BaseModel):
    message: str = 'boom'


def fail_workflow() -> Any:
    def run(params: FailParams) -> Any:
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


def registry() -> Registry:
    reg = Registry()
    for spec, factory in (
        (LOAD, load_workflow),
        (REBIN, rebin_workflow),
        (SUM, sum_workflow),
        (FAIL, fail_workflow),
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
