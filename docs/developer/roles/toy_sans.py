# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The running example of the notebooks in this folder: a toy SANS reduction.

A run's data holds its counts per pixel and its monitor count. The reduction
sums the pixels into ``bins`` groups, the Q bins (the *numerator*), and
divides by the monitor count (the *denominator*). The can's curve is
subtracted from the sample's. Numerator and denominator stay apart
until the division, so that the runs of a sum add up correctly.

workflow-authors.ipynb writes the specs and bindings below step by step; the other
notebooks import them from here.
"""

import functools
from collections.abc import Mapping, Sequence
from typing import Any, NewType, TypeVar

import sciline
import scipp as sc
from pydantic import BaseModel, Field

from ess.spec import (
    Array,
    ArraySpec,
    DriverBinding,
    NexusFile,
    WorkflowSpec,
    function_binding,
)
from ess.spec.pipeline import PipelineBinding

# The workflow package: a sciline pipeline of one sample run and one can run.

SampleRun = NewType('SampleRun', int)
CanRun = NewType('CanRun', int)
RunType = TypeVar('RunType', SampleRun, CanRun)


class RunData(sciline.Scope[RunType, dict], dict):
    """A run's ``counts`` per pixel and ``monitor`` count, as read from its dataset."""


class Counts(sciline.Scope[RunType, sc.Variable], sc.Variable):
    """A run's counts per pixel, loaded and corrected: the costly step."""


class Numerator(sciline.Scope[RunType, sc.Variable], sc.Variable):
    """A run's counts summed into Q bins."""


class Denominator(sciline.Scope[RunType, sc.Variable], sc.Variable):
    """A run's monitor count."""


Bins = NewType('Bins', int)
IofQ = NewType('IofQ', sc.Variable)


LOADED: list[dict] = []
"""Each run that ``load`` loaded, so that the notebooks can show what a stage
or an accumulator saves."""


def load(data: RunData[RunType]) -> Counts[RunType]:
    LOADED.append(data)
    counts = sc.array(dims=['pixel'], values=data['counts'], unit='counts')
    return Counts[RunType](counts)


def numerator(counts: Counts[RunType], bins: Bins) -> Numerator[RunType]:
    binned = counts.values.reshape(bins, -1).sum(axis=1)
    return Numerator[RunType](sc.array(dims=['Q'], values=binned, unit='counts'))


def denominator(data: RunData[RunType]) -> Denominator[RunType]:
    return Denominator[RunType](sc.scalar(data['monitor'], unit='counts'))


def iofq(
    sample_numerator: Numerator[SampleRun],
    sample_denominator: Denominator[SampleRun],
    can_numerator: Numerator[CanRun],
    can_denominator: Denominator[CanRun],
) -> IofQ:
    sample = sample_numerator / sample_denominator
    return IofQ(sample - can_numerator / can_denominator)


pipeline = sciline.Pipeline([load, numerator, denominator, iofq])

# The toy runs, as their datasets hold them.

SAMPLE_RUNS = {
    101: {'counts': [8.0, 6.0, 4.0, 2.0], 'monitor': 10.0},
    102: {'counts': [10.0, 6.0, 3.0, 1.0], 'monitor': 20.0},
    103: {'counts': [6.0, 6.0, 5.0, 3.0], 'monitor': 10.0},
}
CAN_RUNS = {
    110: {'counts': [1.0, 1.0, 1.0, 1.0], 'monitor': 10.0},
    111: {'counts': [2.0, 0.0, 2.0, 0.0], 'monitor': 10.0},
}

# The specs.

CURVE = ArraySpec(dims=('Q',), unit='dimensionless')


class Run(BaseModel):
    """One run: a group of the single-run spec, and a row of the multi-run spec."""

    file: NexusFile


class Reduction(BaseModel):
    bins: int = Field(default=2, gt=0)


class IofQParams(BaseModel):
    sample: Run
    can: Run
    reduction: Reduction = Field(default_factory=Reduction)


class MultiIofQParams(BaseModel):
    sample: list[Run] = Field(min_length=1)
    can: list[Run] = Field(min_length=1)
    reduction: Reduction = Field(default_factory=Reduction)


class IofQOutputs(BaseModel):
    iofq: Array(CURVE)  # type: ignore[valid-type]


IOFQ = WorkflowSpec(
    name='toy-sans-iofq',
    version=1,
    title='I(Q)',
    description='I(Q) of one sample run, minus that of one can run.',
    params=IofQParams,
    outputs=IofQOutputs,
)
IOFQ_MULTI = WorkflowSpec(
    name='toy-sans-iofq-multi',
    version=1,
    title='I(Q) of summed runs',
    description='I(Q) of the summed sample runs, minus that of the summed can runs.',
    params=MultiIofQParams,
    outputs=IofQOutputs,
)


class Curve(BaseModel):
    data: Array(CURVE)  # type: ignore[valid-type]


class ForwardParams(BaseModel):
    curve: Curve


class ForwardOutputs(BaseModel):
    i0: Array(ArraySpec(dims=(), unit='dimensionless'))  # type: ignore[valid-type]


FORWARD = WorkflowSpec(
    name='toy-sans-forward',
    version=1,
    title='Forward intensity',
    description='The first point of an I(Q) curve, a stand-in for a Guinier fit.',
    params=ForwardParams,
    outputs=ForwardOutputs,
)

# The bindings.

single = PipelineBinding(
    IOFQ,
    pipeline,
    params={
        'sample.file': RunData[SampleRun],
        'can.file': RunData[CanRun],
        'reduction.bins': Bins,
    },
    outputs={'iofq': IofQ},
)


class IofQDriver:
    """
    The driver of ``IOFQ_MULTI``: it sums each run's numerator and denominator
    per run type, and computes I(Q) once from the sums.

    Each run's numerator and denominator come from a ``sciline.Stage`` of the
    pipeline cut at the run's data, and I(Q) from one cut at the sums. A sum is
    made with ``a + b``, so that no value the driver was given, or a stage
    returned, is modified.
    """

    def __init__(self, pipeline: sciline.Pipeline, fixed: Mapping[str, Any]) -> None:
        workflow = pipeline.copy()
        workflow[Bins] = fixed['reduction']['bins']
        self._contribute = {
            table: (
                RunData[run],
                sciline.Stage(
                    workflow,
                    inputs=(RunData[run],),
                    outputs=(Numerator[run], Denominator[run]),
                ),
            )
            for table, run in (('sample', SampleRun), ('can', CanRun))
        }
        sums = [
            key[run] for key in (Numerator, Denominator) for run in (SampleRun, CanRun)
        ]
        self._finalize = sciline.Stage(workflow, inputs=sums, outputs=(IofQ,))
        self._sums: dict[str, dict[Any, sc.Variable]] = {}  # per table, by key

    def push(self, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
        key, stage = self._contribute[table]
        for row in rows:
            parts = stage.compute({key: row['file']})
            old = self._sums.get(table)
            self._sums[table] = (
                parts if old is None else {k: old[k] + v for k, v in parts.items()}
            )

    def outputs(self) -> dict[str, Any]:
        if empty := sorted(self._contribute.keys() - self._sums.keys()):
            raise ValueError(f'no rows in {empty} yet')
        sums = {k: v for parts in self._sums.values() for k, v in parts.items()}
        return {'iofq': self._finalize.compute(sums)[IofQ]}


multi = DriverBinding(IOFQ_MULTI, functools.partial(IofQDriver, pipeline))


def forward(curve: dict) -> dict:
    return {'i0': curve['data']['Q', 0].copy()}


forward_binding = function_binding(FORWARD, forward)

BINDINGS = [single, multi, forward_binding]
