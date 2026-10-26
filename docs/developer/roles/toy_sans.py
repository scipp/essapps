# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The running example of the notebooks in this folder: a toy SANS reduction.

A run's file holds its counts per pixel and its monitor count. The detector is
one row of four pixels, at x = 0, 1, 2, and 3 mm. A pixel's Q grows with its
distance from the beam centre, so the reduction orders the pixels by that
distance and sums them into ``bins`` groups, the Q bins (the *numerator*). It
divides by the monitor count, times the sample's transmission for a sample run
(the *denominator*). The can's curve is subtracted from the sample's. Numerator
and denominator stay apart until the division, so that the runs of a sum add up
correctly.

A beam centre is typed in, or found by ``FIND_CENTRE``; a transmission is typed
in, or computed by ``TRANSMISSION`` from a transmission run. ``FORWARD`` takes
the I(Q) that ``IOFQ`` or ``IOFQ_MULTI`` outputs, as the group ``IofQ``, since a
reference stands for a group or a file only.

Each parameter is a group, and a file is a field of one: ``Sample``, ``Can``,
or ``Run``. A ``PipelineBinding`` keys each parameter by its class, so the
sample and the can are classes of their own.

workflow-authors.ipynb writes the specs and bindings below step by step; the other
notebooks import them from here.
"""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NewType, TypeVar

import numpy as np
import sciline
import scipp as sc
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ess.spec import DriverBinding, WorkflowSpec, function_binding
from ess.spec.pipeline import PipelineBinding

# The specs: what apps, clients, and the backend see.


PIXEL_COUNT = 4
"""The detector's pixels, one row; the Q bins sum them in equal groups."""


class Reduction(BaseModel):
    bins: int = Field(default=2, gt=0)

    @field_validator('bins')
    @classmethod
    def _divides_the_pixels(cls, bins: int) -> int:
        if PIXEL_COUNT % bins:
            raise ValueError(f'bins must divide the {PIXEL_COUNT} pixels')
        return bins


class BeamCentre(BaseModel):
    """Where the beam hits the detector, in mm; (0, 0) is the nominal centre."""

    x: float = 0.0
    y: float = 0.0


class TransmissionFraction(BaseModel):
    """A transmission, typed in or computed by ``TRANSMISSION``."""

    value: float = Field(gt=0, le=1)


class IofQ(BaseModel):
    """
    I(Q) of a sample, minus that of its can: one value per Q bin.

    ``intensity`` is a data value, so a request gives an ``IofQ`` by
    reference to an output only.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    intensity: sc.Variable


class Run(BaseModel):
    """A run: its file."""

    file: Path


class Sample(Run):
    """A sample run, and its transmission."""

    transmission: TransmissionFraction


class Can(Run):
    """An empty-can run."""


class IofQSettings(BaseModel):
    reduction: Reduction = Field(default_factory=Reduction)
    beam_centre: BeamCentre = Field(default_factory=BeamCentre)


class IofQParams(IofQSettings):
    sample: Sample
    can: Can


class IofQMultiParams(IofQSettings):
    sample: list[Sample] = Field(min_length=1)
    can: list[Can] = Field(min_length=1)


class IofQOutputs(BaseModel):
    iofq: IofQ


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
    params=IofQMultiParams,
    outputs=IofQOutputs,
    tables=('sample', 'can'),
)


class ForwardParams(BaseModel):
    curve: IofQ


class ForwardOutputs(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    i0: sc.Variable


FORWARD = WorkflowSpec(
    name='toy-sans-forward',
    version=1,
    title='Forward intensity',
    description='The first point of an I(Q) curve, a stand-in for a Guinier fit.',
    params=ForwardParams,
    outputs=ForwardOutputs,
)


class FindCentreParams(BaseModel):
    sample: Run


class FindCentreOutputs(BaseModel):
    centre: BeamCentre


FIND_CENTRE = WorkflowSpec(
    name='toy-sans-find-centre',
    version=1,
    title='Beam centre',
    description="The beam centre, as the centre of a sample run's counts.",
    params=FindCentreParams,
    outputs=FindCentreOutputs,
)


class TransmissionParams(BaseModel):
    run: Run


class TransmissionOutputs(BaseModel):
    transmission: TransmissionFraction


TRANSMISSION = WorkflowSpec(
    name='toy-sans-transmission',
    version=1,
    title='Transmission',
    description='The transmission of a sample, from its transmission run.',
    params=TransmissionParams,
    outputs=TransmissionOutputs,
)

# The toy runs, as their files hold them.

SAMPLE_RUNS = {
    101: {'counts': [8.0, 6.0, 4.0, 2.0], 'monitor': 10.0},
    102: {'counts': [10.0, 6.0, 3.0, 1.0], 'monitor': 20.0},
    103: {'counts': [6.0, 6.0, 5.0, 3.0], 'monitor': 10.0},
}
CAN_RUNS = {
    110: {'counts': [1.0, 1.0, 1.0, 1.0], 'monitor': 10.0},
    111: {'counts': [2.0, 0.0, 2.0, 0.0], 'monitor': 10.0},
}
TRANSMISSION_RUNS = {
    120: {'monitor': 10.0, 'transmitted': 9.0},
}
PIXELS = sc.vectors(
    dims=['pixel'], values=[[x, 0.0, 0.0] for x in range(PIXEL_COUNT)], unit='mm'
)
"""The position of each pixel."""


def write_runs(directory: Path) -> dict[int, Path]:
    """Write each toy run to a file of its own in ``directory``, by run number."""
    paths = {}
    for run, content in {**SAMPLE_RUNS, **CAN_RUNS, **TRANSMISSION_RUNS}.items():
        paths[run] = directory / f'run-{run}.json'
        paths[run].write_text(json.dumps(content))
    return paths


# The workflow package: a sciline pipeline of one sample run and one can run.
# The keys of the parameters and of I(Q) are the classes of the spec's fields.
# ``SampleRun`` and ``CanRun`` are the pipeline's run types, keys of the
# workflow that the spec never names.
# The reduction uses the beam centre as a scipp vector, which a provider makes
# from the group.

SampleRun = NewType('SampleRun', int)
CanRun = NewType('CanRun', int)
RunType = TypeVar('RunType', SampleRun, CanRun)


BeamCentrePosition = NewType('BeamCentrePosition', sc.Variable)


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


def read_sample(sample: Sample) -> RunData[SampleRun]:
    return RunData[SampleRun](json.loads(sample.file.read_text()))


def read_can(can: Can) -> RunData[CanRun]:
    return RunData[CanRun](json.loads(can.file.read_text()))


def load(data: RunData[RunType]) -> Counts[RunType]:
    LOADED.append(data)
    counts = sc.array(dims=['pixel'], values=data['counts'], unit='counts')
    return Counts[RunType](counts)


def beam_centre_position(centre: BeamCentre) -> BeamCentrePosition:
    return BeamCentrePosition(sc.vector([centre.x, centre.y, 0.0], unit='mm'))


def numerator(
    counts: Counts[RunType], reduction: Reduction, centre: BeamCentrePosition
) -> Numerator[RunType]:
    by_q = np.argsort(sc.norm(PIXELS - centre).values, kind='stable')
    binned = counts.values[by_q].reshape(reduction.bins, -1).sum(axis=1)
    return Numerator[RunType](sc.array(dims=['Q'], values=binned, unit='counts'))


def sample_denominator(
    data: RunData[SampleRun], sample: Sample
) -> Denominator[SampleRun]:
    value = data['monitor'] * sample.transmission.value
    return Denominator[SampleRun](sc.scalar(value, unit='counts'))


def can_denominator(data: RunData[CanRun]) -> Denominator[CanRun]:
    return Denominator[CanRun](sc.scalar(data['monitor'], unit='counts'))


def iofq(
    sample_numerator: Numerator[SampleRun],
    sample_denominator: Denominator[SampleRun],
    can_numerator: Numerator[CanRun],
    can_denominator: Denominator[CanRun],
) -> IofQ:
    sample = sample_numerator / sample_denominator
    return IofQ(intensity=sample - can_numerator / can_denominator)


def make_pipeline(fixed: Mapping[str, Any]) -> sciline.Pipeline:
    """The pipeline; its graph is the same for any fixed values."""
    return sciline.Pipeline(
        [
            read_sample,
            read_can,
            load,
            beam_centre_position,
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
    single-run binding's pipeline, cut at the binding's key of its table,
    and I(Q) from one cut at the sums. A sum is made with ``a + b``, so that
    no value the driver was given, or a stage returned, is modified.
    """

    def __init__(self, fixed: Mapping[str, Any]) -> None:
        workflow = single.pipeline(fixed)
        self._contribute = {
            table: sciline.Stage(
                workflow,
                inputs=single.keys[table],
                outputs=(Numerator[run], Denominator[run]),
            )
            for table, run in (('sample', SampleRun), ('can', CanRun))
        }
        sums = [
            key[run] for key in (Numerator, Denominator) for run in (SampleRun, CanRun)
        ]
        self._finalize = sciline.Stage(workflow, inputs=sums, outputs=(IofQ,))
        self._sums: dict[str, dict[Any, sc.Variable]] = {}  # per table, by key

    def push(self, table: str, rows: Sequence[Any]) -> None:
        stage = self._contribute[table]
        (key,) = single.keys[table]
        for row in rows:
            parts = stage.compute({key: row})
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


def forward(params: ForwardParams) -> ForwardOutputs:
    return ForwardOutputs(i0=params.curve.intensity['Q', 0].copy())


def find_centre(params: FindCentreParams) -> FindCentreOutputs:
    data = json.loads(params.sample.file.read_text())
    counts = sc.array(dims=['pixel'], values=data['counts'])
    centre = (counts * PIXELS).sum() / counts.sum()
    return FindCentreOutputs(centre=BeamCentre(x=centre.fields.x.value, y=0.0))


def transmission(params: TransmissionParams) -> TransmissionOutputs:
    data = json.loads(params.run.file.read_text())
    return TransmissionOutputs(
        transmission=TransmissionFraction(value=data['transmitted'] / data['monitor'])
    )


forward_binding = function_binding(FORWARD, forward)
find_centre_binding = function_binding(FIND_CENTRE, find_centre)
transmission_binding = function_binding(TRANSMISSION, transmission)

BINDINGS = [single, multi, forward_binding, find_centre_binding, transmission_binding]
