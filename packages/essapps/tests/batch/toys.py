# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A toy reduction: a row is a sample run and its transmission run.
"""

import threading
from collections.abc import Callable, Sequence
from typing import Annotated

from pydantic import BaseModel, Field

from ess.apps.batch import FileInfo, Row
from ess.spec import FileRef, Referenced, RunFile, WorkflowSpec


class Binning(BaseModel):
    bins: int = Field(default=2, ge=1)


class Offset(BaseModel):
    value: float = 0.0


class Params(BaseModel):
    binning: Binning = Field(default_factory=Binning)
    sample: RunFile
    transmission: RunFile
    thickness: Annotated[float, Field(ge=0)] = 1.0
    offset: Offset | Referenced[Offset] | None = None


class Outputs(BaseModel):
    total: float


class FindParams(BaseModel):
    sample: RunFile


class FindOutputs(BaseModel):
    offset: Offset


class SeriesParams(BaseModel):
    runs: list[RunFile] = Field(min_length=1)


class Group(BaseModel):
    run: RunFile


class GroupedParams(BaseModel):
    group: Group


STAGED = WorkflowSpec(
    name='staged',
    version=1,
    title='Staged',
    description='A toy.',
    params=Params,
    outputs=Outputs,
)
PLAIN = WorkflowSpec(
    name='plain',
    version=1,
    title='Plain',
    description='A toy.',
    params=Params,
    outputs=Outputs,
)
FIND = WorkflowSpec(
    name='find',
    version=1,
    title='Find',
    description='A toy.',
    params=FindParams,
    outputs=FindOutputs,
)
SERIES = WorkflowSpec(
    name='series',
    version=1,
    title='Series',
    description='A toy that sums a list of runs.',
    params=SeriesParams,
    outputs=Outputs,
    tables=('runs',),
)
GROUPED = WorkflowSpec(
    name='grouped',
    version=1,
    title='Grouped',
    description='A toy whose run is inside a group.',
    params=GroupedParams,
    outputs=Outputs,
)
COLUMNS = ('sample', 'transmission', 'thickness')


class Gate:
    """Holds every reduction back until opened; open at the start."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._event.set()

    def close(self) -> None:
        self._event.clear()

    def open(self) -> None:
        self._event.set()

    def wait(self) -> None:
        assert self._event.wait(30)


def pair(files: Sequence[FileInfo]) -> list[Row]:
    """A row per sample run, keyed by run, with the sample's transmission run."""
    partner = {
        f.metadata['sample']: f.ref
        for f in files
        if f.metadata.get('role') == 'transmission'
    }
    rows = []
    for f in files:
        if f.metadata.get('role') == 'sample' and not f.metadata.get('exclude'):
            values = {'sample': f.ref}
            if f.metadata['sample'] in partner:
                values['transmission'] = partner[f.metadata['sample']]
            rows.append(
                Row(str(f.metadata['run']), values, info={'name': f.metadata['sample']})
            )
    return rows


Measure = Callable[..., FileRef]
