# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The toy reduction of ``toys`` on a real in-process workspace."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path

import pandas as pd
import pytest

from ess.apps import Client, Template, Workspace
from ess.apps.batch import (
    Batch,
    BatchManager,
    CorrectedFiles,
    FolderDefinitions,
    Intake,
)
from ess.apps.batch.cli import main
from ess.apps.batch.table import KEY
from ess.apps.folder import folder_files, open_folder
from ess.apps.testing import FakeFiles, FakeStore, StagedFunction, read_file
from ess.spec import Binding, FileRef, function_binding

from . import toys


def _series(gate: toys.Gate) -> Callable[[toys.SeriesParams], toys.Outputs]:
    def series(params: toys.SeriesParams) -> toys.Outputs:
        gate.wait()
        return toys.Outputs(total=sum(read_file(run) for run in params.runs))

    return series


def _repeated(gate: toys.Gate) -> Callable[[toys.RepeatedParams], toys.Outputs]:
    def repeated(params: toys.RepeatedParams) -> toys.Outputs:
        gate.wait()
        runs = [*params.sample_runs, *params.trans_runs]
        return toys.Outputs(total=sum(read_file(run) for run in runs))

    return repeated


@pytest.fixture
def gate() -> toys.Gate:
    return toys.Gate()


@pytest.fixture
def fake(tmp_path: Path) -> FakeFiles:
    return FakeFiles('p1', tmp_path)


@pytest.fixture
def files(fake: FakeFiles) -> CorrectedFiles:
    return CorrectedFiles(fake)


@pytest.fixture
def staged_binding(gate: toys.Gate) -> StagedFunction:
    def reduce(params: toys.Params) -> toys.Outputs:
        gate.wait()
        if params.binning.bins == 1:
            raise ZeroDivisionError('one bin')
        offset = 0.0 if params.offset is None else params.offset.value
        return toys.Outputs(total=read_file(params.sample) / params.thickness + offset)

    return StagedFunction(toys.STAGED, reduce)


@pytest.fixture
def workspace(
    files: CorrectedFiles, staged_binding: StagedFunction, gate: toys.Gate
) -> Iterator[Workspace]:
    plain = function_binding(toys.PLAIN, staged_binding.function)
    find = function_binding(toys.FIND, _find)
    series = function_binding(toys.SERIES, _series(gate))
    repeated = function_binding(toys.REPEATED, _repeated(gate))
    workspace = Workspace(
        proposal='p1',
        files=files,
        bind=[staged_binding, plain, find, series, repeated],
        store=FakeStore(),
    )
    yield workspace
    workspace.close()


@pytest.fixture
def client(workspace: Workspace) -> Client:
    return Client(workspace, submitter='anna')


@pytest.fixture
def other(workspace: Workspace) -> Client:
    """Another client of the proposal of ``client``."""
    return Client(workspace, submitter='bob')


@pytest.fixture
def sample(fake: FakeFiles) -> toys.Measure:
    """toys.Measure a sample run and, unless told not to, its transmission run."""

    def measure(run: int, name: str, *, partner: bool = True) -> FileRef:
        ref = fake.measure(run, float(run), role='sample', sample=name)
        if partner:
            fake.measure(run + 1000, 1.0, role='transmission', sample=name)
        return ref

    return measure


@pytest.fixture
def batch(client: Client) -> Iterator[Batch]:
    """
    A staged batch of :data:`toys.COLUMNS` with the rows that ``toys.PAIRS``
    makes before a file is measured: none. ``batch.put(toys.rows(client))``
    takes those measured since.
    """
    batch = Batch(
        client, toys.STAGED, label='scan', rows=toys.rows(client), staged=True
    )
    yield batch
    batch.close()


@pytest.fixture
def done_batch(fake: FakeFiles, client: Client, manager: BatchManager) -> Batch:
    """The batch Morning of two rows, water (1) and sample A (2), both reduced."""
    for run, name in [(1, 'water'), (2, 'sample A')]:
        fake.measure(run, float(run), role='sample', sample=name, pid=f'20.500/s{run}')
        fake.measure(run + 1000, 1.0, role='transmission', sample=name)
    batch = manager.create(
        'plain', title='Morning', description='2 mm cells', rows=toys.rows(client)
    )
    client.wait(list(batch.reduce(['1', '2']).values()))
    return batch


@pytest.fixture
def definitions(tmp_path: Path) -> FolderDefinitions:
    return FolderDefinitions(tmp_path / 'workspace')


@pytest.fixture
def refusing(definitions: FolderDefinitions) -> toys.Refusing:
    return toys.Refusing(definitions)


@pytest.fixture
def manager(client: Client, refusing: toys.Refusing) -> BatchManager:
    """A manager on ``definitions``, through ``refusing``, which refuses nothing."""
    return BatchManager(client, definitions=refusing, staged=toys.STAGED_NAMES)


@pytest.fixture
def intake(manager: BatchManager) -> Intake:
    """The intake of ``manager``, with the sources of ``toys``."""
    return Intake(manager, sources=toys.SOURCES)


@pytest.fixture
def label(intake: Intake) -> str:
    """
    A batch of ``intake.manager`` with the spec, columns and label of
    ``batch``, made from the source ``pairs`` before a file is measured: it
    has no rows, and Follow or ``intake.take`` adds those measured since.
    """
    return toys.from_pairs(intake, toys.STAGED.name, title='Scan', label='scan')


# A workspace kept in a folder of toy files, for batches from tables

BAD = 4
"""The run whose file holds a negative number, which the toy refuses."""


def _reduce(params: toys.Params) -> toys.Outputs:
    value = read_file(params.sample)
    if value < 0 or params.binning.bins == 1:
        raise ValueError('refused')
    offset = 0.0 if params.offset is None else params.offset.value
    return toys.Outputs(total=value / params.thickness + offset)


def _find(params: toys.FindParams) -> toys.FindOutputs:
    return toys.FindOutputs(offset=toys.Offset(value=read_file(params.sample)))


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    """Samples 1, 2, 3 holding their numbers, BAD holding -1, and transmissions."""
    runs = {1: (1, 'sample'), 2: (2, 'sample'), 3: (3, 'sample')}
    runs |= {BAD: (-1, 'bad'), 1001: (1, 'transmission'), 1002: (1, 'transmission')}
    lines = ['file,run,role']
    for run, (content, role) in runs.items():
        (tmp_path / f'run-{run}.json').write_text(str(content))
        lines.append(f'run-{run}.json,{run},{role}')
    (tmp_path / 'files.csv').write_text('\n'.join(lines))
    return tmp_path


@pytest.fixture
def folder_staged() -> StagedFunction:
    return StagedFunction(toys.STAGED, _reduce)


@pytest.fixture
def folder_bindings(folder_staged: StagedFunction) -> list[Binding]:
    return [
        folder_staged,
        function_binding(toys.PLAIN, _reduce),
        function_binding(toys.FIND, _find),
    ]


@pytest.fixture
def folder_client(folder: Path, folder_bindings: list[Binding]) -> Iterator[Client]:
    with open_folder(folder, folder_bindings) as client:
        yield client


@pytest.fixture
def template() -> Template:
    return Template(
        toys.PLAIN,
        params={'thickness': 2.0, 'binning': {'bins': 4}},
        blanks=['sample', 'transmission'],
    )


@pytest.fixture
def table() -> pd.DataFrame:
    """Two rows for the template: samples 1 and 2, each with a transmission."""
    return pd.DataFrame(
        {
            'name': ['water', 'sds'],
            'sample': ['run:1', 'run:2'],
            'transmission': ['run:1001', 'run:1002'],
        },
        index=pd.Index(['a', 'b'], name=KEY),
    )


MODULE = (
    f'from {toys.__name__} import TEXT\n'
    + '''
import os
import signal
from typing import Annotated

import pandas as pd
from ess.apps.batch import RowSource
from ess.apps.testing import StagedFunction, read_file
from ess.spec import RunFile, WorkflowSpec, function_binding
from pydantic import AfterValidator, BaseModel
from ess.spec import Referenced


class Offset(BaseModel):
    value: float = 0.0


class Params(BaseModel):
    sample: RunFile
    offset: Offset | Referenced[Offset] | None = None


class FindOutputs(BaseModel):
    offset: Offset


class FindParams(BaseModel):
    sample: RunFile


FIND = WorkflowSpec(
    name='cli-find', version=1, title='Find', description='A toy.',
    params=FindParams, outputs=FindOutputs,
)


class Outputs(BaseModel):
    total: float


SPEC = WorkflowSpec(
    name='cli-toy', version=1, title='Toy', description='A toy.',
    params=Params, outputs=Outputs,
)


def _reduce(params: Params) -> Outputs:
    value = read_file(params.sample)
    if value < 0:
        raise ValueError('refused')
    if value == 77:  # as a person who presses Ctrl-C while it runs
        os.kill(os.getpid(), signal.SIGINT)
    return Outputs(total=value)


def _find(params: FindParams) -> FindOutputs:
    return FindOutputs(offset=Offset(value=read_file(params.sample)))


class Pair(BaseModel):
    sample: RunFile


class PairParams(BaseModel):
    pair: Pair


PAIR = WorkflowSpec(
    name='cli-pair', version=1, title='Pair', description='A file in a group.',
    params=PairParams, outputs=Outputs,
)


def _pair(params: PairParams) -> Outputs:
    return Outputs(total=read_file(params.pair.sample))


LOCKED = []  # folders that validating cli-lock makes read-only


def _lock(value: str) -> str:
    for path in LOCKED:
        path.chmod(0o500)
    raise ValueError('locked')


class LockParams(BaseModel):
    sample: RunFile
    lock: Annotated[str, AfterValidator(_lock)]


LOCK = WorkflowSpec(
    name='cli-lock', version=1, title='Lock', description='Refuses its settings.',
    params=LockParams, outputs=Outputs,
)


BINDINGS = [
    function_binding(SPEC, _reduce),
    function_binding(FIND, _find),
    function_binding(PAIR, _pair),
    function_binding(LOCK, _reduce),
]
UPGRADED = [
    function_binding(SPEC.model_copy(update={'version': 2}), _reduce),
    function_binding(FIND, _find),
]
STAGING = [StagedFunction(SPEC, _reduce), function_binding(FIND, _find)]


WRITERS = {'cli-toy': [TEXT]}


def pair(files):
    rows = [
        {'key': str(run := f.metadata['run']), 'run': int(run), 'sample': str(f.ref)}
        for f in files
        if f.metadata['role'] == 'sample'
    ]
    columns = ['key', 'run', 'sample']
    return pd.DataFrame.from_records(rows, columns=columns).set_index('key')


PAIRS = RowSource(pair, fills=('sample',), description='A row per sample run.')
BROKEN = RowSource(lambda files: [f.metadata['nope'] for f in files], fills=('sample',))
SOURCES = {'pairs': PAIRS, 'broken': BROKEN}


class Metadata(BaseModel):
    run: int


REFUSING = {
    'refusing': RowSource(
        lambda files: Metadata.model_validate({'run': 'water'}), fills=('sample',)
    )
}
'''
)


@pytest.fixture
def command(folder: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The arguments that select the folder and the bindings of an importable module."""
    (folder / 'cli_toys_batch_files.py').write_text(MODULE)
    monkeypatch.syspath_prepend(folder)
    return ['-w', str(folder), '-b', 'cli_toys_batch_files:BINDINGS']


@pytest.fixture
def cli_template(folder: Path) -> Path:
    path = folder / 'iofq.yaml'
    path.write_text('spec: {name: cli-toy, version: 1}\nblanks: [sample]\n')
    return path


def write_rows(folder: Path, samples: list[str]) -> Path:
    """A table of a row per sample run, keyed ``row-0``, ``row-1``, ..."""
    path = folder / 'runs.csv'
    keys = [f'row-{i}' for i in range(len(samples))]
    cells = [f'run:{run}' if run else '' for run in samples]
    pd.DataFrame({'key': keys, 'sample': cells}).to_csv(path, index=False)
    return path


@contextmanager
def app(folder: Path, label: str = 'x') -> Iterator[Batch]:
    """The batch of ``label`` as an application on the folder makes it."""
    with intake_on(folder) as intake:
        yield intake.manager.batch(label)


def edit(batch: Batch, key: str, sample: str) -> None:
    """Set the sample of the row of ``key``, as a page does."""
    frame = batch.frame()
    frame.loc[key, 'sample'] = sample
    batch.put(frame)


@pytest.fixture
def reduced_once(command: list[str], folder: Path) -> Path:
    """The folder after a reduction of samples 1 and 2 with an offset, as ``x``."""
    template = folder / 'offset.yaml'
    template.write_text(
        'spec: {name: cli-toy, version: 1}\nblanks: [sample]\n'
        'params: {offset: {value: 0.5}}\n'
    )
    runs = write_rows(folder, ['1', '2'])
    assert main([*command, 'reduce', str(template), str(runs), '-l', 'x']) == 0
    return folder


SOURCES = ['-s', 'cli_toys_batch_files:SOURCES']


@contextmanager
def intake_on(folder: Path) -> Iterator[Intake]:
    """The intake of an application on the folder, with the sources by name."""
    module = import_module('cli_toys_batch_files')
    definitions = FolderDefinitions(folder)
    files = CorrectedFiles(folder_files(folder), definitions=definitions)
    with open_folder(folder, module.BINDINGS, files=files) as client:
        manager = BatchManager(client, definitions=definitions)
        yield Intake(manager, sources=module.SOURCES)


def later(intake: BatchManager, **options: object) -> str:
    """A batch of the samples 2 and 3 of the source, which it keeps."""
    return intake.create(
        'cli-toy',
        title='Later',
        source='pairs',
        choose=lambda given: given.loc[['2', '3']],
        **options,
    ).label


@pytest.fixture
def toy_template(command: list[str], folder: Path) -> Path:
    """The template of cli-toy that the command writes to fill in."""
    path = folder / 'toy.yaml'
    assert main([*command, 'template', 'cli-toy', '-o', str(path)]) == 0
    return path


def arrive(folder: Path, run: int, content: int | None = None) -> None:
    """A sample run that arrives: its file, then its row in files.csv."""
    (folder / f'run-{run}.json').write_text(str(run if content is None else content))
    with (folder / 'files.csv').open('a') as f:
        f.write(f'\nrun-{run}.json,{run},sample')


@pytest.fixture
def from_3(command: list[str], cli_template: Path, folder: Path) -> list[str]:
    """
    The folder with the batch ``x`` of sample run 3 on, reduced; the command
    with the sources.
    """
    reduce = [str(cli_template), '--source', 'pairs', '--first', '3', '-l', 'x']
    assert main([*command, *SOURCES, 'reduce', *reduce]) == 0
    return [*command, *SOURCES]
