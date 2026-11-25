# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A toy reduction: a row is a sample run and its transmission run.
"""

import threading
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any

import pandas as pd
from ess.batch import (
    Batch,
    DefinitionStore,
    FileInfo,
    Intake,
    NotKept,
    RowSource,
    rows_of,
)
from ess.batch.definition import Key
from ess.batch.table import KEY
from pydantic import BaseModel, Field, ValidationError

from ess.apps import Client
from ess.apps.testing import FakeFiles
from ess.apps.writers import About, Writer
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


class RepeatedParams(BaseModel):
    sample_runs: list[RunFile] = Field(min_length=1)
    trans_runs: list[RunFile] = Field(min_length=1)
    binning: Binning = Field(default_factory=Binning)


STAGED = WorkflowSpec(
    name='staged',
    version=1,
    title='Staged',
    description='A toy.',
    params=Params,
    outputs=Outputs,
)


def _text(outputs: Mapping[str, Any], about: About, directory: Path) -> None:
    (directory / 'total.txt').write_text(f'{about.name}: {outputs["total"]}')


TEXT = Writer(
    name='text',
    title='Text',
    description='The name and the total in total.txt.',
    outputs=('total',),
    write=_text,
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
REPEATED = WorkflowSpec(
    name='repeated',
    version=1,
    title='Repeated',
    description='A toy of sample and transmission runs, each repeated.',
    params=RepeatedParams,
    outputs=Outputs,
    tables=('sample_runs', 'trans_runs'),
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


class Flaky:
    """A client whose next ``fail`` submissions raise, as a lost connection does."""

    def __init__(self, client: Client) -> None:
        self._client = client
        self.fail = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def submit(self, *args: Any, **kwargs: Any) -> Any:
        if self.fail:
            self.fail -= 1
            raise ConnectionError('lost')
        return self._client.submit(*args, **kwargs)


@contextmanager
def unreachable(
    fake: FakeFiles, method: str, refs: Collection[FileRef] = ()
) -> Iterator[None]:
    """
    While open, ``method`` of ``fake``, ``'resolve'`` or ``'metadata'``, raises
    ``OSError`` for the files named in ``refs``, or, if none are given, at every
    call, also of ``'list'``: as a file source does whose mount is gone.
    """
    given = getattr(fake, method)

    def raising(arg: Any) -> Any:
        if not refs or arg in refs:
            raise OSError('the mount is gone')
        return given(arg)

    setattr(fake, method, raising)
    try:
        yield
    finally:
        delattr(fake, method)


class Refusing:
    """
    Definitions that refuse to save or delete the tables named ``part``, such
    as ``'header'``, or the table of the key ``part``, such as
    ``('labels', 'scan', 'header')``, and keep the others in the definitions
    they wrap; ``saved`` lists the keys saved, in order.
    """

    def __init__(self, definitions: DefinitionStore) -> None:
        self.part: str | Key | None = None
        self.saved: list[Key] = []
        self._definitions = definitions

    def load[T](self, key: Key, read: Callable[[Any], T]) -> T | None:
        return self._definitions.load(key, read)

    def save(self, key: Key, data: Any) -> None:
        self._check(key)
        self._definitions.save(key, data)
        self.saved.append(key)

    def delete(self, key: Key) -> None:
        self._check(key)
        self._definitions.delete(key)

    def labels(self) -> list[str]:
        return self._definitions.labels()

    def _check(self, key: Key) -> None:
        if self.part in (key[-1], key):
            raise NotKept(f'{key}: refused')


def pair(files: Sequence[FileInfo]) -> pd.DataFrame:
    """
    A row per sample run, keyed by run, with the sample's transmission run;
    the run and the name of the sample as notes.
    """
    partner = {
        f.metadata['sample']: str(f.ref)
        for f in files
        if f.metadata.get('role') == 'transmission'
    }
    rows = [
        {
            KEY: str(f.metadata['run']),
            'run': f.metadata['run'],
            'name': f.metadata['sample'],
            'sample': str(f.ref),
            'transmission': partner.get(f.metadata['sample']),
        }
        for f in files
        if f.metadata.get('role') == 'sample' and not f.metadata.get('exclude')
    ]
    columns = [KEY, 'run', 'name', 'sample', 'transmission']
    return pd.DataFrame.from_records(rows, columns=columns).set_index(KEY)


def samples(files: Sequence[FileInfo]) -> pd.DataFrame:
    """A row per sample run, keyed by run."""
    rows = [
        {KEY: str(f.metadata['run']), 'sample': str(f.ref)}
        for f in files
        if f.metadata.get('role') == 'sample'
    ]
    return pd.DataFrame.from_records(rows, columns=[KEY, 'sample']).set_index(KEY)


PAIRS = RowSource(pair, fills=('sample', 'transmission'))
SAMPLES = RowSource(samples, fills=('sample',))
SOURCES = {'pairs': PAIRS}
STAGED_NAMES = frozenset({STAGED.name})
"""The specs whose batches run through a stage."""

Measure = Callable[..., FileRef]


def rows(client: Client) -> pd.DataFrame:
    """
    The rows that :data:`PAIRS` makes from the files now, with an empty column
    ``thickness``: the rows of a batch whose columns are :data:`COLUMNS`.
    """
    return rows_of(client, PAIRS).assign(thickness=None)


def edit(batch: Batch, key: str, **cells: Any) -> None:
    """Set cells of the row of ``key``, ``pd.NA`` to empty one; a new key adds a row."""
    frame = batch.frame()
    for column, cell in cells.items():
        frame.loc[key, column] = cell
    batch.put(frame)


def from_pairs(intake: Intake, spec: str, **options: Any) -> str:
    """
    A batch of ``intake`` made from the source ``pairs`` with ``options``,
    with an empty column ``thickness``, so that its columns are
    :data:`COLUMNS`; its label.
    """
    made = intake.create(
        spec, source='pairs', parameter_columns=['thickness'], **options
    )
    return made.label


class _Metadata(BaseModel):
    run: int
    role: str


def refused_metadata() -> ValidationError:
    """
    What code of an application raises that validates metadata with a run
    that is no number and without a role; said as :data:`REFUSED_METADATA`.
    """
    try:
        _Metadata.model_validate({'run': 'water'})
    except ValidationError as error:
        return error
    raise AssertionError('the metadata is valid')


REFUSED_METADATA = (
    'run must be a valid integer, unable to parse string as an integer; role is missing'
)
