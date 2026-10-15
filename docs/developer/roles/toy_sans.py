# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The running example of the notebooks in this folder: a toy SANS reduction.

A run's file holds its counts per pixel and its monitor count; a sample run's
file also holds the sample's transmission. The reduction sums the pixels into
``bins`` groups, the Q bins (the *numerator*), and divides by the monitor
count, times the transmission for a sample run (the *denominator*). The can's
curve is subtracted from the sample's. Numerator and denominator stay apart
until the division, so that the runs of a sum add up correctly.

workflow-authors.ipynb writes the specs and bindings below step by step; the other
notebooks import them from here.
"""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NewType, TypeVar

import sciline
import scipp as sc
from pydantic import BaseModel, ConfigDict, Field

from ess.spec import DriverBinding, NoParams, WorkflowSpec, function_binding
from ess.spec.pipeline import PipelineBinding

# The specs: what apps, clients, and the backend see.

SampleFile = NewType('SampleFile', Path)
CanFile = NewType('CanFile', Path)
IofQ = NewType('IofQ', sc.Variable)


class Reduction(BaseModel):
    bins: int = Field(default=2, gt=0)


class Transmission(BaseModel):
    value: float = Field(gt=0, le=1)


class IofQParams(BaseModel):
    reduction: Reduction = Field(default_factory=Reduction)
    transmission: Transmission | None = Field(
        default=None, description="The sample's; None takes the one in its file."
    )


class IofQInputs(BaseModel):
    sample: SampleFile
    can: CanFile


class IofQOutputs(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    iofq: IofQ


IOFQ = WorkflowSpec(
    name='toy-sans-iofq',
    version=1,
    title='I(Q)',
    description='I(Q) of one sample run, minus that of one can run.',
    params=IofQParams,
    inputs=IofQInputs,
    outputs=IofQOutputs,
)
IOFQ_MULTI = IOFQ.tabulate(
    name='toy-sans-iofq-multi',
    version=1,
    tables={'sample': ('transmission',), 'can': ()},
    title='I(Q) of summed runs',
    description='I(Q) of the summed sample runs, minus that of the summed can runs.',
)

ForwardIntensity = NewType('ForwardIntensity', sc.Variable)


class ForwardInputs(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    curve: IofQ


class ForwardOutputs(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    i0: ForwardIntensity


FORWARD = WorkflowSpec(
    name='toy-sans-forward',
    version=1,
    title='Forward intensity',
    description='The first point of an I(Q) curve, a stand-in for a Guinier fit.',
    inputs=ForwardInputs,
    outputs=ForwardOutputs,
)

# The toy runs, as their files hold them.

SAMPLE_RUNS = {
    101: {'counts': [8.0, 6.0, 4.0, 2.0], 'monitor': 10.0, 'transmission': 0.8},
    102: {'counts': [10.0, 6.0, 3.0, 1.0], 'monitor': 20.0, 'transmission': 0.8},
    103: {'counts': [6.0, 6.0, 5.0, 3.0], 'monitor': 10.0, 'transmission': 0.5},
}
CAN_RUNS = {
    110: {'counts': [1.0, 1.0, 1.0, 1.0], 'monitor': 10.0},
    111: {'counts': [2.0, 0.0, 2.0, 0.0], 'monitor': 10.0},
}


def write_runs(directory: Path) -> dict[int, Path]:
    """Write each toy run to a file of its own in ``directory``, by run number."""
    paths = {}
    for run, content in {**SAMPLE_RUNS, **CAN_RUNS}.items():
        paths[run] = directory / f'run-{run}.json'
        paths[run].write_text(json.dumps(content))
    return paths


# The workflow package: a sciline pipeline of one sample run and one can run.
# The keys of the files, of the parameters, and of I(Q) are the types of the
# spec's fields.

SampleRun = NewType('SampleRun', int)
CanRun = NewType('CanRun', int)
RunType = TypeVar('RunType', SampleRun, CanRun)


class RunData(sciline.Scope[RunType, dict], dict):
    """A run's file, read: ``counts`` per pixel, ``monitor``, and more."""


class Counts(sciline.Scope[RunType, sc.Variable], sc.Variable):
    """A run's counts per pixel, loaded and corrected: the costly step."""


class Numerator(sciline.Scope[RunType, sc.Variable], sc.Variable):
    """A run's counts summed into Q bins."""


class Denominator(sciline.Scope[RunType, sc.Variable], sc.Variable):
    """A run's monitor count, times the transmission for a sample run."""


LOADED: list[dict] = []
"""Each run that ``load`` loaded, so that the notebooks can show what a stage
or an accumulator saves."""


def read_sample(file: SampleFile) -> RunData[SampleRun]:
    return RunData[SampleRun](json.loads(file.read_text()))


def read_can(file: CanFile) -> RunData[CanRun]:
    return RunData[CanRun](json.loads(file.read_text()))


def load(data: RunData[RunType]) -> Counts[RunType]:
    LOADED.append(data)
    counts = sc.array(dims=['pixel'], values=data['counts'], unit='counts')
    return Counts[RunType](counts)


def numerator(counts: Counts[RunType], reduction: Reduction) -> Numerator[RunType]:
    binned = counts.values.reshape(reduction.bins, -1).sum(axis=1)
    return Numerator[RunType](sc.array(dims=['Q'], values=binned, unit='counts'))


def sample_denominator(
    data: RunData[SampleRun], transmission: Transmission | None
) -> Denominator[SampleRun]:
    value = data['transmission'] if transmission is None else transmission.value
    return Denominator[SampleRun](sc.scalar(data['monitor'] * value, unit='counts'))


def can_denominator(data: RunData[CanRun]) -> Denominator[CanRun]:
    return Denominator[CanRun](sc.scalar(data['monitor'], unit='counts'))


def iofq(
    sample_numerator: Numerator[SampleRun],
    sample_denominator: Denominator[SampleRun],
    can_numerator: Numerator[CanRun],
    can_denominator: Denominator[CanRun],
) -> IofQ:
    sample = sample_numerator / sample_denominator
    return IofQ(sample - can_numerator / can_denominator)


def make_pipeline(fixed: Mapping[str, Any]) -> sciline.Pipeline:
    """The pipeline; its graph is the same for any fixed values."""
    return sciline.Pipeline(
        [
            read_sample,
            read_can,
            load,
            numerator,
            sample_denominator,
            can_denominator,
            iofq,
        ]
    )


# The bindings.

single = PipelineBinding(IOFQ, make_pipeline)


class IofQDriver:
    """
    The driver of ``IOFQ_MULTI``: it sums each run's numerator and denominator
    per run type, and computes I(Q) once from the sums.

    Each run's numerator and denominator come from a ``sciline.Stage`` of the
    single-run binding's pipeline, cut at the binding's keys of its table's
    columns, and I(Q) from one cut at the sums. A sum is made with ``a + b``,
    so that no value the driver was given, or a stage returned, is modified.
    """

    def __init__(self, fixed: Mapping[str, Any]) -> None:
        workflow = single.pipeline(fixed)
        self._contribute = {
            index: sciline.Stage(
                workflow,
                inputs=[single.keys[name] for name in IOFQ_MULTI.columns(index)],
                outputs=(Numerator[run], Denominator[run]),
            )
            for index, run in (('sample', SampleRun), ('can', CanRun))
        }
        sums = [
            key[run] for key in (Numerator, Denominator) for run in (SampleRun, CanRun)
        ]
        self._finalize = sciline.Stage(workflow, inputs=sums, outputs=(IofQ,))
        self._sums: dict[str, dict[Any, sc.Variable]] = {}  # per table, by key

    def push(self, table: str, columns: Mapping[str, Sequence[Any]]) -> None:
        stage = self._contribute[table]
        keys = [single.keys[name] for name in columns]
        for row in zip(*columns.values(), strict=True):
            parts = stage.compute(dict(zip(keys, row, strict=True)))
            old = self._sums.get(table)
            self._sums[table] = (
                parts if old is None else {k: old[k] + v for k, v in parts.items()}
            )

    def outputs(self) -> IofQOutputs:
        if empty := sorted(self._contribute.keys() - self._sums.keys()):
            raise ValueError(f'no rows in {empty} yet')
        sums = {k: v for parts in self._sums.values() for k, v in parts.items()}
        return IofQOutputs(iofq=self._finalize.compute(sums)[IofQ])


multi = DriverBinding(IOFQ_MULTI, IofQDriver)


def forward(params: NoParams, inputs: ForwardInputs) -> ForwardOutputs:
    return ForwardOutputs(i0=ForwardIntensity(inputs.curve['Q', 0].copy()))


forward_binding = function_binding(FORWARD, forward)

BINDINGS = [single, multi, forward_binding]
