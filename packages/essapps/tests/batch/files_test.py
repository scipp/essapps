# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""A batch from files: a template file, and the command on a workspace in a folder."""

import json
import signal
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from importlib import import_module
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import yaml

from ess.apps import Client, Status, Stopped, Template, Workspace, file
from ess.apps.batch import (
    Batch,
    BatchManager,
    CorrectedFiles,
    FileInfo,
    FolderDefinitions,
    Header,
    Intake,
    Range,
    RowSource,
    left_to_reduce,
)
from ess.apps.batch.cli import follow, main
from ess.apps.batch.table import KEY, read_text
from ess.apps.batch.templates import load_template
from ess.apps.folder import folder_files, open_folder
from ess.apps.log import Event, Log
from ess.apps.testing import FakeStore, StagedFunction
from ess.spec import function_binding

from . import toys
from .conftest import BAD


def test_a_template_is_loaded_from_yaml(tmp_path: Path) -> None:
    path = tmp_path / 'template.yaml'
    path.write_text(
        yaml.safe_dump(
            {
                'spec': {'name': 'plain', 'version': 1},
                'blanks': ['sample'],
                'params': {'thickness': 2.0, 'transmission': 7},
            }
        )
    )

    assert load_template(path) == Template(
        toys.PLAIN, params={'thickness': 2.0, 'transmission': 7}, blanks=['sample']
    )


def test_a_template_may_leave_out_the_blanks_and_the_params(tmp_path: Path) -> None:
    path = tmp_path / 'template.yaml'
    path.write_text('spec: {name: plain, version: 1}')

    assert load_template(path) == Template(toys.PLAIN)


def test_a_template_whose_params_are_all_commented_out_has_none(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'template.yaml'
    path.write_text('spec: {name: plain, version: 1}\nparams:\n  #sample:\n')

    assert load_template(path) == Template(toys.PLAIN)


# The command

MODULE = '''
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


def test_main_reduces_a_table_into_a_file_of_results(
    command: list[str], cli_template: Path, folder: Path
) -> None:
    runs = write_rows(folder, ['1', '2'])
    out = folder / 'results.csv'

    code = main(
        [*command, 'reduce', str(cli_template), str(runs), '-l', 'x', '-o', str(out)]
    )

    assert code == 0
    assert list(read_text(out)['status']) == ['done', 'done']


def test_main_returns_3_if_a_row_failed(
    command: list[str], cli_template: Path, folder: Path
) -> None:
    runs = write_rows(folder, ['1', str(BAD)])
    out = folder / 'results.csv'

    code = main(
        [*command, 'reduce', str(cli_template), str(runs), '-l', 'x', '-o', str(out)]
    )

    assert code == 3
    assert list(read_text(out)['status']) == ['done', 'failed']


@pytest.mark.parametrize('remaining', [[], ['--remaining']])
@pytest.mark.parametrize('sample', ['', '99'])
def test_main_returns_2_and_says_why_if_a_row_cannot_run(
    command: list[str],
    cli_template: Path,
    folder: Path,
    capsys: pytest.CaptureFixture[str],
    sample: str,
    remaining: list[str],
) -> None:
    runs = write_rows(folder, ['1', sample])

    code = main(
        [*command, 'reduce', str(cli_template), str(runs), '-l', 'x', *remaining]
    )

    assert code == 2
    assert 'row-1' in capsys.readouterr().err


@pytest.mark.parametrize('remaining', [[], ['--remaining']])
def test_main_returns_2_and_says_why_if_the_settings_are_refused(
    command: list[str],
    folder: Path,
    capsys: pytest.CaptureFixture[str],
    remaining: list[str],
) -> None:
    template = folder / 'iofq.yaml'
    template.write_text(
        'spec: {name: cli-toy, version: 1}\nblanks: [sample]\n'
        'params: {offset: {value: nope}}\n'
    )
    runs = write_rows(folder, ['1'])

    code = main([*command, 'reduce', str(template), str(runs), '-l', 'x', *remaining])

    err = capsys.readouterr().err
    assert code == 2
    assert 'offset' in err
    assert 'row-0' not in err


def test_main_with_remaining_reduces_the_rows_whose_cells_changed(
    command: list[str], cli_template: Path, folder: Path
) -> None:
    out = folder / 'results.csv'

    def records(samples: list[str]) -> list[str]:
        runs = write_rows(folder, samples)
        args = [str(cli_template), str(runs), '-l', 'x', '--remaining', '-o', str(out)]
        assert main([*command, 'reduce', *args]) == 0
        return list(read_text(out)['record'])

    first = records(['1', '2'])
    cell = records(['1', '3'])

    assert (cell[0], cell[1] != first[1]) == (first[0], True)
    assert records(['1', '3']) == cell


@pytest.fixture
def settings_changed(command: list[str], cli_template: Path, folder: Path) -> Path:
    """The table of a reduction of two rows, after the template's settings changed."""
    runs = write_rows(folder, ['1', '2'])
    out = folder / 'results.csv'
    assert main([*command, 'reduce', str(cli_template), str(runs), '-l', 'x']) == 0
    cli_template.write_text(
        'spec: {name: cli-toy, version: 1}\nblanks: [sample]\n'
        'params: {offset: {value: 1.0}}\n'
    )
    return out


def test_main_with_remaining_leaves_the_rows_reduced_with_earlier_settings(
    command: list[str],
    cli_template: Path,
    folder: Path,
    settings_changed: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs = folder / 'runs.csv'
    capsys.readouterr()

    code = main(
        [
            *command,
            'reduce',
            str(cli_template),
            str(runs),
            '-l',
            'x',
            '--remaining',
            '-o',
            str(settings_changed),
        ]
    )

    table = read_text(settings_changed)
    assert list(table['status']) == ['earlier'] * 2
    assert code == 3
    assert 'left 2 rows' in capsys.readouterr().err
    with app(folder) as batch:
        assert len(batch.client.records(label='x')) == 2


def test_main_with_remaining_and_earlier_settings_reduces_those_rows_too(
    command: list[str], cli_template: Path, folder: Path, settings_changed: Path
) -> None:
    runs = folder / 'runs.csv'
    args = [
        str(cli_template),
        str(runs),
        '-l',
        'x',
        '--remaining',
        '--earlier-settings',
    ]

    code = main([*command, 'reduce', *args, '-o', str(settings_changed)])

    table = read_text(settings_changed)
    assert code == 0
    assert list(table['status']) == ['done'] * 2
    with app(folder) as batch:
        assert len(batch.client.records(label='x')) == 4


def test_main_refuses_earlier_settings_without_remaining(
    command: list[str],
    cli_template: Path,
    folder: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs = write_rows(folder, ['1'])

    with pytest.raises(SystemExit) as exit_:
        main(
            [
                *command,
                *('reduce', str(cli_template), str(runs), '-l', 'x'),
                '--earlier-settings',
            ]
        )

    assert exit_.value.code == 2
    assert '--remaining' in capsys.readouterr().err


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


def test_a_batch_on_the_label_reduced_has_the_settings_and_rows_of_the_files(
    reduced_once: Path,
) -> None:
    with app(reduced_once) as batch:
        assert batch.settings == {'offset': {'value': 0.5}}
        assert batch.frame()['sample'].to_dict() == {
            'row-0': 'run:1',
            'row-1': 'run:2',
        }
        assert left_to_reduce(batch.state()) == []


def test_an_edit_of_an_app_is_reduced_by_the_command_given_no_files(
    command: list[str], reduced_once: Path
) -> None:
    with app(reduced_once) as batch:
        edit(batch, 'row-1', 'run:3')
    out = reduced_once / 'out.csv'

    code = main([*command, 'reduce', '-l', 'x', '--remaining', '-o', str(out)])

    with app(reduced_once) as batch:
        state = batch.state()
        totals = {k: batch.client.output(s.record, 'total') for k, s in state.items()}
        records = batch.client.records(label='x')
    assert code == 0
    assert list(read_text(out)['key']) == ['row-0', 'row-1']
    assert totals == {'row-0': 1.0, 'row-1': 3.0}
    assert len(records) == 3  # row-0 is not reduced again


def test_a_table_given_to_the_command_takes_the_place_of_an_edit_of_an_app(
    command: list[str], reduced_once: Path
) -> None:
    with app(reduced_once) as batch:
        edit(batch, 'row-1', 'run:3')
    runs = write_rows(reduced_once, ['1', '2'])
    template = reduced_once / 'offset.yaml'

    assert main([*command, 'reduce', str(template), str(runs), '-l', 'x']) == 0

    with app(reduced_once) as batch:
        assert batch.frame().loc['row-1', 'sample'] == 'run:2'


def test_a_value_of_the_template_at_a_column_is_that_of_each_empty_cell(
    command: list[str], folder: Path
) -> None:
    template = folder / 'one.yaml'
    template.write_text('spec: {name: cli-toy, version: 1}\nparams: {sample: run:1}\n')
    runs = write_rows(folder, ['2', ''])
    again = folder / 'three.yaml'
    again.write_text('spec: {name: cli-toy, version: 1}\nparams: {sample: run:3}\n')

    assert main([*command, 'reduce', str(template), str(runs), '-l', 'x']) == 0
    with app(folder) as batch:
        state = batch.state()
        totals = {k: batch.client.output(s.record, 'total') for k, s in state.items()}
    assert main([*command, 'reduce', str(again), '-l', 'x']) == 0

    assert totals == {'row-0': 2.0, 'row-1': 1.0}
    with app(folder) as batch:
        assert batch.frame()['sample'].isna().tolist() == [False, True]
        state = batch.state()
        totals = {k: batch.client.output(s.record, 'total') for k, s in state.items()}
    assert totals == {'row-0': 2.0, 'row-1': 3.0}


SOURCES = ['-s', 'cli_toys_batch_files:SOURCES']


@pytest.mark.parametrize('sources', [SOURCES, []], ids=['sources', 'no-sources'])
def test_a_template_leaves_out_a_value_at_a_column_that_the_source_fills(
    command: list[str], folder: Path, sources: list[str]
) -> None:
    with intake_on(folder) as intake:
        label = later(intake)
        batch = intake.manager.batch(label)
        waits = pd.DataFrame({'sample': [None]}, index=pd.Index(['9'], name=KEY))
        batch.put(pd.concat([batch.frame(), waits]))
    template = folder / 'one.yaml'
    template.write_text(
        'spec: {name: cli-toy, version: 1}\n'
        'params: {sample: run:1, offset: {value: 0.5}}\n'
    )

    main([*command, *sources, 'reduce', str(template), '-l', label])

    with intake_on(folder) as intake:
        batch = intake.manager.batch(label)
        assert batch.settings == {'offset': {'value': 0.5}}
        assert batch.state()['9'].text == 'incomplete'
        assert '9' not in {r.member for r in intake.manager.client.records(label=label)}


def test_a_table_given_with_a_source_is_refused(
    command: list[str],
    cli_template: Path,
    folder: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs = write_rows(folder, ['1'])
    reduce = ['--source', 'pairs', str(cli_template), str(runs), '-l', 'x']

    with pytest.raises(SystemExit):
        main([*command, *SOURCES, 'reduce', *reduce])

    assert 'give a table file or --source, not both' in capsys.readouterr().err
    assert FolderDefinitions(folder).labels() == []


# The command and the batches of an intake on one folder


@contextmanager
def intake_on(folder: Path) -> Iterator[Intake]:
    """The intake of an application on the folder, with the sources by name."""
    module = import_module('cli_toys_batch_files')
    definitions = FolderDefinitions(folder)
    files = CorrectedFiles(folder_files(folder), definitions=definitions)
    with open_folder(folder, module.BINDINGS, files=files) as client:
        manager = BatchManager(client, definitions=definitions)
        yield Intake(manager, sources=module.SOURCES)


def test_reduce_on_a_new_label_writes_a_header_so_the_manager_lists_it(
    command: list[str], cli_template: Path, folder: Path
) -> None:
    reduce = [str(cli_template), '-l', 'x', '--source', 'pairs']

    assert main([*command, *SOURCES, 'reduce', *reduce]) == 0

    with intake_on(folder) as intake:
        assert intake.manager.headers() == {'x': Header(title='x')}


def test_reduce_with_a_source_and_no_table_makes_a_batch_of_every_row_of_the_source(
    command: list[str], cli_template: Path, folder: Path
) -> None:
    out = folder / 'done.csv'
    reduce = [str(cli_template), '-l', 'x', '--source', 'pairs', '-o', str(out)]

    assert main([*command, *SOURCES, 'reduce', *reduce]) == 0

    assert list(read_text(out)['key']) == ['1', '2', '3']
    with intake_on(folder) as intake:
        assert intake.offered('x').empty


def test_reduce_refuses_a_label_that_has_definitions_but_no_batch(
    command: list[str],
    cli_template: Path,
    folder: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with intake_on(folder) as intake:
        old = Batch(
            intake.manager.client,
            import_module('cli_toys_batch_files').SPEC,
            label='old',
            definitions=FolderDefinitions(folder),
        )
        old.settings = {'offset': {'value': 0.5}}
    runs = write_rows(folder, ['1'])

    code = main([*command, 'reduce', str(cli_template), str(runs), '-l', 'old'])

    assert code == 2
    assert 'old: the label has definitions or records' in capsys.readouterr().err
    with intake_on(folder) as intake:
        assert intake.manager.headers() == {}
        assert intake.manager.client.records(label='old') == []


def later(intake: BatchManager, **options: object) -> str:
    """A batch of the samples 2 and 3 of the source, which it keeps."""
    return intake.create(
        'cli-toy',
        title='Later',
        source='pairs',
        choose=lambda given: given.loc[['2', '3']],
        **options,
    ).label


def test_reduce_on_a_batch_of_the_manager_reduces_its_rows_without_its_source(
    command: list[str], folder: Path
) -> None:
    with intake_on(folder) as intake:
        label = later(intake)
        before = intake.manager.headers()
    out = folder / 'done.csv'

    code = main([*command, 'reduce', '-l', label, '-o', str(out)])

    assert code == 0
    assert list(read_text(out)['key']) == ['2', '3']
    with intake_on(folder) as intake:
        assert intake.manager.headers() == before
        reduced = {r.member for r in intake.manager.client.records(label=label)}
        assert reduced == {'2', '3'}


def test_reduce_refuses_a_source_for_a_batch_that_exists(
    command: list[str],
    cli_template: Path,
    folder: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs = write_rows(folder, ['1'])
    assert main([*command, 'reduce', str(cli_template), str(runs), '-l', 'x']) == 0

    code = main([*command, *SOURCES, 'reduce', '--source', 'pairs', '-l', 'x'])

    assert code == 2
    assert 'x: the batch exists; --source makes a new batch' in capsys.readouterr().err


def test_reduce_refuses_a_template_of_another_spec_than_the_batch(
    command: list[str],
    cli_template: Path,
    folder: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs = write_rows(folder, ['1'])
    assert main([*command, 'reduce', str(cli_template), str(runs), '-l', 'x']) == 0
    other = folder / 'other.yaml'
    other.write_text('spec: {name: cli-find, version: 1}\nblanks: [sample]\n')
    capsys.readouterr()

    code = main([*command, 'reduce', str(other), str(runs), '-l', 'x'])

    assert code == 2
    assert (
        f'{other}: it holds the settings of cli-find, not cli-toy'
        in capsys.readouterr().err
    )


def test_reduce_refuses_a_label_that_is_not_one_segment_of_a_url(
    command: list[str],
    cli_template: Path,
    folder: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs = write_rows(folder, ['1'])

    code = main([*command, 'reduce', str(cli_template), str(runs), '-l', 'auto/x'])

    assert code == 2
    assert "'auto/x': a label is letters" in capsys.readouterr().err
    assert FolderDefinitions(folder).labels() == []


def test_reduce_takes_the_version_of_the_spec_that_the_workspace_binds(
    command: list[str], cli_template: Path, folder: Path
) -> None:
    runs = write_rows(folder, ['1'])
    assert main([*command, 'reduce', str(cli_template), str(runs), '-l', 'x']) == 0
    upgraded = [*command[:-1], 'cli_toys_batch_files:UPGRADED']

    assert main([*upgraded, 'reduce', '-l', 'x', '--remaining']) == 0

    with open_folder(folder, []) as client:
        versions = [r.spec.version for r in client.records(label='x')]
    assert versions == [1, 2]


def test_reduce_refuses_a_template_of_another_version_as_the_page_does(
    command: list[str],
    cli_template: Path,
    folder: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs = write_rows(folder, ['1'])
    assert main([*command, 'reduce', str(cli_template), str(runs), '-l', 'x']) == 0
    upgraded = [*command[:-1], 'cli_toys_batch_files:UPGRADED']

    code = main([*upgraded, 'reduce', str(cli_template), str(runs), '-l', 'x'])

    assert code == 2
    assert 'of version 1 of cli-toy, not of version 2' in capsys.readouterr().err


def test_reduce_whose_definitions_refuse_a_write_says_so(
    command: list[str],
    cli_template: Path,
    folder: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs = write_rows(folder, ['1'])
    definitions = folder / 'definitions'
    definitions.mkdir()
    definitions.chmod(0o500)
    try:
        code = main([*command, 'reduce', str(cli_template), str(runs), '-l', 'x'])
    finally:
        definitions.chmod(0o700)

    assert code == 1
    assert 'not kept: ' in capsys.readouterr().err


def test_reduce_on_a_folder_that_another_process_holds_says_so(
    command: list[str],
    cli_template: Path,
    folder: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs = write_rows(folder, ['1'])

    with open_folder(folder, []):
        code = main([*command, 'reduce', str(cli_template), str(runs), '-l', 'x'])

    assert code == 1
    assert 'is held by another process' in capsys.readouterr().err


def test_a_template_refused_on_a_new_label_leaves_no_batch(
    command: list[str], cli_template: Path, folder: Path
) -> None:
    runs = write_rows(folder, ['1'])
    refused = folder / 'refused.yaml'
    refused.write_text('spec: {name: cli-toy, version: 2}\nblanks: [sample]\n')

    assert main([*command, 'reduce', str(refused), str(runs), '-l', 'x']) == 2

    assert FolderDefinitions(folder).labels() == []
    assert main([*command, 'reduce', str(cli_template), str(runs), '-l', 'x']) == 0


def test_a_source_that_raises_is_an_error_and_leaves_no_batch(
    command: list[str], cli_template: Path, folder: Path
) -> None:
    reduce = [str(cli_template), '-l', 'x', '--source', 'broken']

    with pytest.raises(KeyError, match='nope'):
        main([*command, *SOURCES, 'reduce', *reduce])

    assert FolderDefinitions(folder).labels() == []


def test_reduce_refuses_a_source_that_fills_a_parameter_the_spec_lacks(
    command: list[str], folder: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    template = folder / 'pair.yaml'
    template.write_text('spec: {name: cli-pair, version: 1}\n')
    reduce = [str(template), '-l', 'x', '--source', 'pairs']

    assert main([*command, *SOURCES, 'reduce', *reduce]) == 2

    assert capsys.readouterr().err == (
        'the spec cli-pair has no parameter sample, which the source pairs fills\n'
    )
    assert FolderDefinitions(folder).labels() == []


def kept(folder: Path, label: str) -> dict[str, str]:
    """The files that the definitions of ``label`` are kept in, by name."""
    files = (folder / 'definitions' / 'labels' / label).iterdir()
    return {path.name: path.read_text() for path in files}


@pytest.mark.parametrize(
    'table',
    ['sample\nrun:2\n', 'key,sample\n2,run:2\n2,run:3\n'],
    ids=['no-key', 'key-twice'],
)
def test_a_refused_reduce_changes_neither_the_settings_nor_the_rows_of_a_batch(
    command: list[str], folder: Path, table: str
) -> None:
    with intake_on(folder) as intake:
        label = later(intake, settings={'offset': {'value': 0.5}})
        edit(intake.manager.batch(label), '2', 'run:3')
    before = kept(folder, label)
    template = folder / 'other.yaml'
    template.write_text(
        'spec: {name: cli-toy, version: 1}\nblanks: [sample]\n'
        'params: {offset: {value: 9.0}}\n'
    )
    runs = folder / 'runs.csv'
    runs.write_text(table)

    code = main([*command, *SOURCES, 'reduce', str(template), str(runs), '-l', label])

    assert code == 2
    assert kept(folder, label) == before


def test_reduce_on_a_batch_without_rows_writes_a_table_of_its_columns(
    command: list[str], folder: Path
) -> None:
    with intake_on(folder) as intake:
        label = intake.create(
            'cli-toy', title='None', source='pairs', choose=Range(later=True)
        ).label
    out = folder / 'done.csv'

    code = main([*command, 'reduce', '-l', label, '-o', str(out)])

    assert code == 0
    assert list(read_text(out).columns) == [
        'key',
        'run',
        'sample',
        'status',
        'record',
        'failure',
    ]
    assert read_text(out).empty


def test_a_template_without_a_table_is_the_settings_of_a_batch_that_keeps_its_rows(
    command: list[str], folder: Path
) -> None:
    with intake_on(folder) as intake:
        label = later(intake)
    template = folder / 'offset.yaml'
    template.write_text(
        'spec: {name: cli-toy, version: 1}\nblanks: [sample]\n'
        'params: {offset: {value: 0.5}}\n'
    )
    out = folder / 'done.csv'

    assert main([*command, 'reduce', str(template), '-l', label, '-o', str(out)]) == 0

    assert list(read_text(out)['key']) == ['2', '3']
    with intake_on(folder) as intake:
        assert intake.manager.batch(label).settings == {'offset': {'value': 0.5}}


def test_reduce_with_staged_runs_the_rows_of_its_batch_through_a_stage(
    command: list[str], cli_template: Path, folder: Path
) -> None:
    runs = write_rows(folder, ['1'])
    assert main([*command, 'reduce', str(cli_template), str(runs), '-l', 'x']) == 0
    staging = [*command[:-1], 'cli_toys_batch_files:STAGING']

    assert main([*staging, 'reduce', '-l', 'x', '--staged']) == 0

    assert import_module('cli_toys_batch_files').STAGING[0].staged == [('sample',)]


def test_reduce_refuses_a_spec_that_the_bindings_lack(
    command: list[str], folder: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    template = folder / 'other.yaml'
    template.write_text('spec: {name: nope, version: 1}\nparams: {sample: 1}\n')

    assert main([*command, 'reduce', str(template), '-l', 'x']) == 2
    assert 'the workspace binds no spec nope' in capsys.readouterr().err


def test_reduce_without_a_template_on_a_label_without_a_batch_says_so(
    command: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([*command, 'reduce', '-l', 'nothing']) == 2
    assert (
        'nothing: no batch has the label, so give a template' in capsys.readouterr().err
    )


def test_definitions_that_cannot_be_read_are_said_without_a_traceback(
    command: list[str], folder: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (folder / 'definitions').mkdir()
    (folder / 'definitions' / 'corrections.json').write_text('{"x": ')

    code = main([*command, *SOURCES, 'rows', 'pairs'])

    assert code == 2
    assert 'definitions/corrections.json: ' in capsys.readouterr().err


def test_main_reduces_a_template_without_a_table_as_one_row_keyed_by_the_label(
    command: list[str], folder: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = folder / 'single.yaml'
    path.write_text('spec: {name: cli-toy, version: 1}\nparams: {sample: 1}\n')

    code = main([*command, 'reduce', str(path), '-l', 'detector'])

    result = pd.read_csv(pd.io.common.StringIO(capsys.readouterr().out), dtype=str)
    assert code == 0
    assert list(result['key']) == ['detector']
    assert list(result['status']) == ['done']


@pytest.mark.parametrize(
    ('arguments', 'said'),
    [
        (
            ['-s', 'cli_toys_batch_files:pair', 'sources'],
            'is a function, not a mapping of names to row sources',
        ),
        (['-s', 'nope.module:SOURCES', 'sources'], 'cannot import nope.module'),
        (['-s', 'cli_toys_batch_files:NOPE', 'sources'], 'has no NOPE'),
        (['-s', 'cli_toys_batch_files', 'sources'], 'as package.module:NAME'),
        (['-b', 'cli_toys_batch_files:SOURCES', 'specs'], 'not a list of bindings'),
        (['-b', 'cli_toys_batch_files:NOPE', 'specs'], 'has no NOPE'),
        (['files', 'cli_toys_batch_files:SPEC'], 'is a WorkflowSpec, not a function'),
        (['files', 'nope:read'], 'cannot import nope'),
        (['reduce', 'nope.yaml', '-l', 'x'], 'nope.yaml: no such file'),
        (['export', 'nope.csv', 'out'], 'nope.csv: no such file'),
        (
            ['-b', 'cli_toys_batch_files:BINDINGS', '-w', 'nope', 'specs'],
            'no files.csv',
        ),
        (
            [*SOURCES, 'rows', 'p'],
            'the source p is not registered; registered: pairs, broken',
        ),
        (
            [*SOURCES, 'reduce', '--source', 'p', '-l', 'x'],
            'the source p is not registered; registered: pairs, broken',
        ),
        (['rows', 'pairs'], 'rows needs -s/--sources'),
        (['reduce', '--source', 'pairs', '-l', 'x'], '--source needs -s/--sources'),
    ],
)
def test_main_refuses_a_wrong_name_in_a_line_before_the_folder_is_opened(
    folder: Path,
    command: list[str],
    arguments: list[str],
    said: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_:
        main(['-w', str(folder), *arguments])

    err = capsys.readouterr().err
    assert exit_.value.code == 2
    assert said in err
    assert 'Traceback' not in err
    assert not (folder / 'log.jsonl').exists()


def test_main_says_metadata_that_a_row_source_refuses_at_its_fields(
    command: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code = main([*command, '-s', 'cli_toys_batch_files:REFUSING', 'rows', 'refusing'])

    assert code == 2
    assert capsys.readouterr().err == (
        'run must be a valid integer, unable to parse string as an integer\n'
    )


def test_main_refuses_reduce_without_a_label(
    command: list[str], cli_template: Path
) -> None:
    with pytest.raises(SystemExit) as exit_:
        main([*command, 'reduce', str(cli_template)])

    assert exit_.value.code == 2


def test_main_refuses_an_output_that_is_not_a_table_before_anything_runs(
    command: list[str], cli_template: Path, folder: Path
) -> None:
    runs = write_rows(folder, ['1'])

    out = ['-o', str(folder / 'out.txt')]

    with pytest.raises(SystemExit) as exit_:
        main([*command, 'reduce', str(cli_template), str(runs), '-l', 'x', *out])

    assert exit_.value.code == 2
    with open_folder(folder, []) as client:
        assert client.records() == []


def test_main_writes_the_results_as_the_suffix_of_the_output_says(
    command: list[str], cli_template: Path, folder: Path
) -> None:
    runs = write_rows(folder, ['1'])
    out = folder / 'results.xlsx'

    main([*command, 'reduce', str(cli_template), str(runs), '-l', 'x', '-o', str(out)])

    assert list(read_text(out)['status']) == ['done']


def test_main_exports_the_files_of_a_table_that_it_reduced(
    command: list[str], cli_template: Path, folder: Path
) -> None:
    runs = write_rows(folder, ['1', str(BAD)])
    done, files = folder / 'done.csv', folder / 'exported.csv'
    reduced = ['reduce', str(cli_template), str(runs), '-l', 'x', '-o', str(done)]
    assert main([*command, *reduced]) == 3

    code = main(
        [*command, 'export', str(done), str(folder / 'reduced'), '-o', str(files)]
    )

    result = read_text(files)
    assert code == 0
    assert list(result['total']) == [
        str(folder / 'reduced' / 'row-0' / 'total.h5'),
        '',
    ]
    assert Path(result.loc[0, 'total']).is_file()


def test_the_rows_that_main_makes_from_the_files_are_a_table_to_reduce(
    command: list[str], cli_template: Path, folder: Path
) -> None:
    runs = folder / 'runs.csv'
    out = folder / 'results.csv'

    assert main([*command, *SOURCES, 'rows', 'pairs', '-o', str(runs)]) == 0
    code = main(
        [*command, 'reduce', str(cli_template), str(runs), '-l', 'x', '-o', str(out)]
    )

    result = read_text(out)
    assert code == 0
    assert list(result['key']) == ['1', '2', '3']
    assert list(result['status']) == ['done'] * 3


def test_the_rows_that_main_makes_read_the_corrections_kept_in_the_folder(
    command: list[str], folder: Path
) -> None:
    files = CorrectedFiles(folder_files(folder), definitions=FolderDefinitions(folder))
    files.correct(files.resolve(file(run=BAD)), role='sample')
    runs = folder / 'runs.csv'

    assert main([*command, *SOURCES, 'rows', 'pairs', '-o', str(runs)]) == 0

    assert list(read_text(runs)['key']) == ['1', '2', '3', str(BAD)]


def test_main_runs_with_the_proposal_of_the_files(
    command: list[str], cli_template: Path, folder: Path
) -> None:
    table = folder / 'files.csv'
    lines = table.read_text().splitlines()
    table.write_text(
        '\n'.join([lines[0] + ',proposal', *(f'{ln},p1' for ln in lines[1:])])
    )
    out = folder / 'results.csv'
    runs = write_rows(folder, ['1'])

    reduce = ['reduce', str(cli_template), str(runs), '-l', 'x', '-o', str(out)]

    code = main([*command, '--proposal', 'p1', *reduce])

    assert code == 0
    assert list(read_text(out)['status']) == ['done']


@pytest.fixture
def find_template(folder: Path) -> Path:
    path = folder / 'find.yaml'
    path.write_text('spec: {name: cli-find, version: 1}\nblanks: [sample]\n')
    return path


@pytest.fixture
def offset_template(folder: Path) -> Path:
    path = folder / 'offset.yaml'
    path.write_text(
        'spec: {name: cli-toy, version: 1}\n'
        'blanks: [sample]\n'
        'params: {offset: {"@latest": offset, output: offset}}\n'
    )
    return path


def test_main_reduces_with_the_newest_record_of_a_label_in_the_template(
    command: list[str], find_template: Path, offset_template: Path, folder: Path
) -> None:
    runs = write_rows(folder, ['1'])
    assert (
        main([*command, 'reduce', str(find_template), str(runs), '-l', 'offset']) == 0
    )

    code = main([*command, 'reduce', str(offset_template), str(runs), '-l', 'x'])

    assert code == 0
    with open_folder(folder, []) as client:
        (found,) = client.records(label='offset')
        (record,) = client.records(label='x')
        assert record.request.params['offset'].record == found.id


def test_main_returns_2_and_says_why_if_the_template_names_a_label_without_records(
    command: list[str],
    offset_template: Path,
    folder: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs = write_rows(folder, ['1'])

    code = main([*command, 'reduce', str(offset_template), str(runs), '-l', 'x'])

    assert code == 2
    assert "no record under 'offset'" in capsys.readouterr().err


def test_main_writes_the_table_of_files_that_a_later_command_opens_the_folder_with(
    folder: Path, command: list[str], cli_template: Path
) -> None:
    reader = 'def read(path):\n'
    reader += "    if path.suffix == '.json':\n"
    reader += "        return {'run': path.stem.removeprefix('run-')}\n"
    (folder / 'cli_toys_read_files.py').write_text(reader)
    (folder / 'files.csv').unlink()
    table = write_rows(folder, ['1', '2'])

    out = str(folder / 'files.csv')
    scanned = main(['-w', str(folder), 'files', 'cli_toys_read_files:read', '-o', out])
    reduced = main([*command, 'reduce', str(cli_template), str(table), '-l', 'scan'])

    assert (scanned, reduced) == (0, 0)


@pytest.fixture
def reader(folder: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A reader that gives every JSON file the run 1."""
    (folder / 'cli_toys_one_run.py').write_text(
        "def read(path):\n    return {'run': 1} if path.suffix == '.json' else None\n"
    )
    monkeypatch.syspath_prepend(folder)
    return 'cli_toys_one_run:read'


def test_main_refuses_to_overwrite_a_table_of_files_that_exists(
    folder: Path, reader: str
) -> None:
    out = folder / 'files.csv'
    before = out.read_text()

    with pytest.raises(SystemExit) as exit_:
        main(['-w', str(folder), 'files', reader, '-o', str(out)])

    assert exit_.value.code == 2
    assert out.read_text() == before


@pytest.mark.parametrize(
    'arguments',
    [
        [*SOURCES, 'rows', 'pairs'],
        ['export', 'done.csv', 'out'],
        ['specs'],
        ['template', 'cli-toy'],
        ['batches'],
        ['show', '-l', 'x'],
    ],
)
def test_main_refuses_the_commands_that_need_bindings_without_them(
    folder: Path, command: list[str], arguments: list[str]
) -> None:
    with pytest.raises(SystemExit) as exit_:
        main(['-w', str(folder), *arguments])

    assert exit_.value.code == 2


def test_main_returns_2_and_says_why_if_two_files_are_of_one_run(
    folder: Path,
    reader: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(['-w', str(folder), 'files', reader])

    assert code == 2
    assert 'run' in capsys.readouterr().err


def test_main_writes_the_files_with_a_run_table_over_a_table_that_exists(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (folder / 'cli_toys_by_stem.py').write_text(
        "def read(path):\n"
        "    if path.suffix == '.json':\n"
        "        return {'run': path.stem.removeprefix('run-')}\n"
    )
    monkeypatch.syspath_prepend(folder)
    runs = str(folder / 'runs.csv')
    Path(runs).write_text('run,role\n1,sample\n999,sample\n')
    out = folder / 'files.csv'

    code = main(
        [
            '-w',
            str(folder),
            'files',
            'cli_toys_by_stem:read',
            '-o',
            str(out),
            '--with',
            runs,
        ]
    )

    files = pd.read_csv(out, dtype=str, keep_default_na=False)
    assert code == 0
    assert dict(zip(files['run'], files['role'], strict=True))['1'] == 'sample'
    assert '999' not in set(files['run'])


# Discovering and looking


def test_specs_lists_each_spec_that_the_bindings_bind(
    command: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([*command, 'specs']) == 0

    assert capsys.readouterr().out.splitlines() == [
        'name,version,title,outputs',
        'cli-toy,1,Toy,total',
        'cli-find,1,Find,offset',
        'cli-pair,1,Pair,total',
        'cli-lock,1,Lock,total',
    ]


def test_sources_lists_each_row_source_with_the_parameters_that_it_fills(
    folder: Path, command: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(['-w', str(folder), *SOURCES, 'sources']) == 0

    assert capsys.readouterr().out.splitlines() == [
        'name,fills,description',
        'pairs,sample,A row per sample run.',
        'broken,sample,',
    ]


def test_batches_lists_each_batch_with_its_rows_reduced_and_source(
    command: list[str], cli_template: Path, folder: Path
) -> None:
    with intake_on(folder) as intake:
        later(intake, follow=True)
    runs = write_rows(folder, ['1', str(BAD)])
    assert main([*command, 'reduce', str(cli_template), str(runs), '-l', 'x']) == 3
    out = folder / 'batches.csv'

    assert main([*command, *SOURCES, 'batches', '-o', str(out)]) == 0

    listed = read_text(out)
    assert list(listed.columns) == [
        'label',
        'title',
        'spec',
        'rows',
        'reduced',
        'source',
        'follows',
    ]
    assert listed.drop(columns='label').to_dict('records') == [
        {
            'title': 'Later',
            'spec': 'cli-toy',
            'rows': '2',
            'reduced': '0',
            'source': 'pairs',
            'follows': 'True',
        },
        {
            'title': 'x',
            'spec': 'cli-toy',
            'rows': '2',
            'reduced': '1',
            'source': '',
            'follows': 'False',
        },
    ]


def test_show_writes_the_table_of_a_batch_with_results_and_reduces_nothing(
    command: list[str], reduced_once: Path
) -> None:
    with app(reduced_once) as batch:
        edit(batch, 'row-1', 'run:3')
    out = reduced_once / 'shown.csv'

    assert main([*command, 'show', '-l', 'x', '-o', str(out)]) == 0

    shown = read_text(out)
    assert list(shown['key']) == ['row-0', 'row-1']
    assert list(shown['status']) == ['done', 'outdated']
    with app(reduced_once) as batch:
        assert len(batch.client.records(label='x')) == 2


@pytest.mark.parametrize('subcommand', ['show', 'template'])
def test_a_label_without_a_batch_is_refused_with_a_message(
    command: list[str], subcommand: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([*command, subcommand, '-l', 'nothing']) == 2
    assert 'nothing: no batch has the label' in capsys.readouterr().err


def test_json_writes_a_list_of_objects_one_per_row_with_the_key_as_a_field(
    command: list[str], reduced_once: Path
) -> None:
    out = reduced_once / 'shown.json'

    assert main([*command, '--json', 'show', '-l', 'x', '-o', str(out)]) == 0

    rows = json.loads(out.read_text())
    assert [(r['key'], r['sample'], r['status']) for r in rows] == [
        ('row-0', 'run:1', 'done'),
        ('row-1', 'run:2', 'done'),
    ]
    assert (rows[0]['failure'], rows[0]['record'] is None) == (None, False)


@pytest.fixture
def toy_template(command: list[str], folder: Path) -> Path:
    """The template of cli-toy that the command writes to fill in."""
    path = folder / 'toy.yaml'
    assert main([*command, 'template', 'cli-toy', '-o', str(path)]) == 0
    return path


def test_the_template_of_a_spec_says_what_each_parameter_takes(
    toy_template: Path,
) -> None:
    lines = toy_template.read_text().splitlines()

    assert lines[lines.index('params:') :] == [
        'params:',
        '  # sample (required): a run.',
        '  #sample:',
        '  # offset (default null): a group of the fields below, or an output of '
        'type Offset,',
        '  #     such as offset of cli-find, or null for none.',
        '  #   value (default 0.0): a number.',
        '  #offset: null',
    ]


def test_the_template_of_a_spec_is_refused_naming_each_required_parameter(
    command: list[str], toy_template: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert load_template(toy_template) == Template(
        import_module('cli_toys_batch_files').SPEC
    )

    assert main([*command, 'reduce', str(toy_template), '-l', 'x']) == 2
    assert 'sample: a required parameter, not given' in capsys.readouterr().err


def test_the_template_of_a_spec_filled_in_reduces(
    command: list[str], toy_template: Path
) -> None:
    toy_template.write_text(toy_template.read_text().replace('#sample:', 'sample: 1'))

    assert main([*command, 'reduce', str(toy_template), '-l', 'x']) == 0


def test_the_template_of_a_batch_holds_its_settings(
    command: list[str], reduced_once: Path
) -> None:
    path = reduced_once / 'now.yaml'

    assert main([*command, 'template', '-l', 'x', '-o', str(path)]) == 0

    assert load_template(path) == Template(
        import_module('cli_toys_batch_files').SPEC, params={'offset': {'value': 0.5}}
    )


def test_a_reduce_that_makes_a_batch_and_is_refused_leaves_none(
    command: list[str], folder: Path
) -> None:
    template = folder / 'offset.yaml'
    template.write_text(
        'spec: {name: cli-toy, version: 1}\nparams: {offset: {value: nope}}\n'
    )
    reduce = [*command, *SOURCES, 'reduce', str(template), '--source', 'pairs']

    assert main([*reduce, '-l', 'a']) == 2

    assert FolderDefinitions(folder).labels() == []
    template.write_text('spec: {name: cli-toy, version: 1}\n')
    assert main([*reduce, '-l', 'a']) == 0


def test_a_reduce_whose_new_batch_cannot_be_removed_says_why_it_was_refused(
    command: list[str], folder: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    template = folder / 'lock.yaml'
    template.write_text('spec: {name: cli-lock, version: 1}\nparams: {lock: x}\n')
    labels = folder / 'definitions' / 'labels'
    import_module('cli_toys_batch_files').LOCKED.append(labels / 'a')
    reduce = [*SOURCES, 'reduce', str(template), '--source', 'pairs', '-l', 'a']
    try:
        code = main([*command, *reduce])
    finally:
        (labels / 'a').chmod(0o700)

    err = capsys.readouterr().err
    assert code == 2
    assert 'locked' in err
    assert 'not kept' not in err
    assert (labels / 'a' / 'header.json').exists()  # the removal was refused


def test_a_file_in_a_group_is_written_as_the_template_says(
    command: list[str], folder: Path
) -> None:
    path = folder / 'pair.yaml'
    assert main([*command, 'template', 'cli-pair', '-o', str(path)]) == 0
    said = "  #   sample (required): a run, written {'@ref': file, file: ...}."
    filled = "  pair: {sample: {'@ref': file, file: run:1}}"

    lines = path.read_text().splitlines()
    path.write_text(path.read_text().replace('  #pair:', filled))

    assert said in lines
    assert main([*command, 'reduce', str(path), '-l', 'x']) == 0


@pytest.mark.parametrize(
    ('given', 'said'),
    [
        (
            'spec: {name: cli-toy, version: 1}\nparams: {sample: 1, offset: nope}\n',
            "offset: 'nope' names no output: a template names one as "
            "{'@latest': LABEL, output: NAME} or RECORD.OUTPUT",
        ),
        (
            'key,sample,offset\nrow-0,run:1,nope\n',
            "row 'row-0', column 'offset': 'nope' names no output: a cell names "
            'one as RECORD.OUTPUT, such as r17.centre',
        ),
    ],
    ids=['template', 'table'],
)
def test_text_that_names_no_output_is_refused_saying_how_to_name_one(
    command: list[str],
    cli_template: Path,
    folder: Path,
    given: str,
    said: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = folder / ('given.csv' if given.startswith('key') else 'given.yaml')
    path.write_text(given)
    files = [str(path)] if path.suffix == '.yaml' else [str(cli_template), str(path)]

    assert main([*command, 'reduce', *files, '-l', 'x']) == 2

    assert said in capsys.readouterr().err
    assert FolderDefinitions(folder).labels() == []


def test_export_of_a_label_writes_the_outputs_of_its_rows_as_they_stand(
    command: list[str], reduced_once: Path
) -> None:
    out = reduced_once / 'exported.csv'

    code = main(
        [*command, 'export', '-l', 'x', str(reduced_once / 'out'), '-o', str(out)]
    )

    assert code == 0
    assert [Path(p).is_file() for p in read_text(out)['total']] == [True, True]


# Acting: rows of the row source, Follow, stop and remove


@pytest.mark.parametrize(
    ('pick', 'keys'),
    [(['--first', '2'], ['2', '3']), (['--last', '2'], ['1', '2']), (['--later'], [])],
    ids=['first', 'last', 'later'],
)
def test_reduce_with_a_source_makes_a_batch_of_the_rows_picked(
    command: list[str],
    cli_template: Path,
    folder: Path,
    pick: list[str],
    keys: list[str],
) -> None:
    out = folder / 'done.csv'
    reduce = [str(cli_template), '--source', 'pairs', *pick, '-l', 'x', '-o', str(out)]

    assert main([*command, *SOURCES, 'reduce', *reduce]) == 0

    assert list(read_text(out)['key']) == keys


@pytest.mark.parametrize(
    ('pick', 'said'),
    [
        (['--first', '9'], '9 is not a filled row'),
        (['--first', '3', '--last', '2'], 'the row 3 comes after the row 2'),
        (['--later', '--first', '2'], 'arrive later has no first or last'),
    ],
)
def test_rows_picked_that_the_source_refuses_leave_no_batch(
    command: list[str],
    cli_template: Path,
    folder: Path,
    pick: list[str],
    said: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reduce = [str(cli_template), '--source', 'pairs', *pick, '-l', 'x']

    assert main([*command, *SOURCES, 'reduce', *reduce]) == 2

    assert said in capsys.readouterr().err
    assert FolderDefinitions(folder).labels() == []


def test_rows_picked_without_a_source_are_refused_on_the_command_line(
    command: list[str], cli_template: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        main([*command, 'reduce', str(cli_template), '--later', '-l', 'x'])

    assert '--later pick rows of --source' in capsys.readouterr().err


def test_take_puts_offered_rows_into_the_batch_and_reduces_nothing(
    command: list[str], folder: Path
) -> None:
    with intake_on(folder) as intake:
        label = later(intake)
    offered, taken = folder / 'offered.csv', folder / 'taken.csv'
    act = [*command, *SOURCES]

    assert main([*act, 'offered', '-l', label, '-o', str(offered)]) == 0
    assert main([*act, 'take', '-l', label, '1', '-o', str(taken)]) == 0

    assert read_text(offered)[['key', 'waits']].to_dict('records') == [
        {'key': '1', 'waits': 'False'}
    ]
    assert read_text(taken)[['key', 'status']].to_dict('records') == [
        {'key': '1', 'status': 'ready'}
    ]
    with intake_on(folder) as intake:
        assert list(intake.manager.batch(label).frame().index) == ['2', '3', '1']
        assert intake.manager.client.records(label=label) == []


def test_remove_removes_the_batch_and_says_that_its_records_stay(
    command: list[str], reduced_once: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([*command, 'remove', '-l', 'x']) == 0

    assert 'the folder keeps its 2 records under the label' in capsys.readouterr().err
    with intake_on(reduced_once) as intake:
        assert intake.manager.headers() == {}
        assert len(intake.manager.client.records(label='x')) == 2
    assert main([*command, 'remove', '-l', 'x']) == 2


def test_stop_ends_follow_and_says_on_which_batch(
    command: list[str], folder: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with intake_on(folder) as intake:
        label = later(intake, follow=True)

    assert main([*command, 'stop']) == 0
    assert main([*command, 'stop']) == 0

    err = capsys.readouterr().err.splitlines()
    assert err == [f'{label} follows no more', 'no batch followed']
    with intake_on(folder) as intake:
        assert intake.following is None


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


def test_follow_adds_and_reduces_the_rows_that_arrived_and_goes_on_where_it_ended(
    from_3: list[str], folder: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    arrive(folder, 5)
    arrive(folder, 6)
    assert main([*from_3, 'follow', '-l', 'x', '--steps', '1']) == 0
    first = capsys.readouterr().out
    arrive(folder, 7)

    assert main([*from_3, 'follow', '-l', 'x', '--steps', '2', '--interval', '0']) == 0

    assert 'added 5 6 | reducing 5 6' in first
    assert 'added 7 | reducing 7' in capsys.readouterr().out
    with intake_on(folder) as intake:
        records = intake.manager.client.records(label='x')
        assert sorted(r.member for r in records) == ['3', '5', '6', '7']
        assert intake.following == 'x'


def test_follow_writes_an_object_per_line_with_json(
    from_3: list[str], folder: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    arrive(folder, 5)

    assert main(['--json', *from_3, 'follow', '-l', 'x', '--steps', '1']) == 0

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[0]['added'] == ['5']
    assert lines[0]['reducing'] == ['5']
    assert {k: v for line in lines for k, v in line['ended'].items()} == {'5': 'done'}
    assert set(lines[0]) == {
        'time',
        'added',
        'reducing',
        'ended',
        'failure',
        'refused',
        'waiting',
        'error',
    }
    assert [line['error'] for line in lines] == [None] * len(lines)


def test_follow_exits_with_3_if_a_row_failed_and_says_why(
    from_3: list[str], folder: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    arrive(folder, 5, content=-1)

    assert main([*from_3, 'follow', '-l', 'x', '--steps', '1']) == 3

    said = capsys.readouterr()
    assert 'failed 5 (' in said.out
    assert '1 row of x not done: show -l x says why' in said.err


@pytest.mark.parametrize(
    ('label', 'said'),
    [('nothing', 'nothing: no batch has the label'), ('y', 'y was not made from')],
)
def test_follow_on_a_batch_that_cannot_follow_is_refused(
    command: list[str],
    cli_template: Path,
    folder: Path,
    label: str,
    said: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs = write_rows(folder, ['1'])
    assert main([*command, 'reduce', str(cli_template), str(runs), '-l', 'y']) == 0

    assert main([*command, *SOURCES, 'follow', '-l', label, '--steps', '1']) == 2

    assert said in capsys.readouterr().err


# The loop of follow, on the toys


@pytest.fixture
def following(intake: Intake, sample: toys.Measure) -> Batch:
    """A batch of ``intake`` that follows, made when run 1 was measured."""
    sample(1, 'water')
    label = toys.from_pairs(intake, 'plain', title='A', follow=True)
    return intake.manager.batch(label)


@pytest.mark.parametrize(
    ('error', 'said'),
    [
        (ValueError('no'), 'the source pairs: no'),
        (RuntimeError('boom'), 'RuntimeError: boom'),
        (toys.refused_metadata(), f'the source pairs: {toys.REFUSED_METADATA}'),
    ],
    ids=['refusal', 'defect', 'metadata refused'],
)
def test_a_step_that_raises_is_said_while_it_changes_and_the_steps_go_on(
    manager: BatchManager,
    sample: toys.Measure,
    error: Exception,
    said: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raising: list[Exception] = []

    def rows(files: Sequence[FileInfo]) -> pd.DataFrame:
        if raising:
            raise raising[0]
        return toys.pair(files)

    intake = Intake(manager, sources={'pairs': RowSource(rows, toys.PAIRS.fills)})
    sample(1, 'water')
    label = toys.from_pairs(intake, 'plain', title='A', follow=True)
    after = [
        lambda: raising.append(error),
        lambda: None,  # the next step raises the same
        lambda: (raising.clear(), sample(2, 'oil')),
        lambda: raising.append(error),  # said again
    ]
    lines: list[Mapping[str, Any]] = []

    follow(
        intake,
        say=lines.append,
        pause=lambda: after.pop(0)(),
        stopped=lambda: False,
        steps=5,
    )

    assert capsys.readouterr().err.count(f'Follow could not step: {said}') == 2
    assert {line['error'] for line in lines} == {None, said}
    assert list(intake.manager.batch(label).frame().index) == ['1', '2']


def test_a_stop_asked_ends_the_steps_once_the_reductions_that_run_end(
    intake: Intake,
    following: Batch,
    sample: toys.Measure,
    gate: toys.Gate,
) -> None:
    gate.close()  # the reduction of row 1 runs until the stop is asked
    asked: list[bool] = []
    lines: list[Mapping[str, Any]] = []

    def stopped() -> bool:
        if asked:
            sample(2, 'oil')  # arrives, but no step adds it
            gate.open()
        asked.append(True)
        return len(asked) > 1

    follow(intake, say=lines.append, pause=lambda: None, stopped=stopped)

    record = following.state()['1'].record
    assert following.client.status([record]) == [Status.COMPLETED]
    assert list(following.frame().index) == ['1']
    assert lines[0]['reducing'] == ['1']
    assert lines[-1]['ended'] == {'1': 'done'}


def test_a_step_that_finds_nothing_new_says_nothing(
    intake: Intake, following: Batch
) -> None:
    lines: list[Mapping[str, Any]] = []
    steps = {'pause': lambda: None, 'stopped': lambda: False, 'steps': 1}
    follow(intake, say=lambda _: None, **steps)

    follow(intake, say=lines.append, **steps)

    assert following.state()['1'].text == 'done'
    assert lines == []


def test_reductions_that_run_when_the_steps_start_are_said_once_they_end(
    intake: Intake, following: Batch, gate: toys.Gate
) -> None:
    gate.close()
    following.reduce(['1'])  # as by a process that ended meanwhile
    lines: list[Mapping[str, Any]] = []

    def stopped() -> bool:
        gate.open()
        return False

    follow(intake, say=lines.append, pause=lambda: None, stopped=stopped, steps=1)

    assert [line['reducing'] for line in lines] == [[]] * len(lines)
    assert {k: v for line in lines for k, v in line['ended'].items()} == {'1': 'done'}
    assert len(following.client.records(label=following.label)) == 1


class FullDisk(Log):
    """A log in memory whose appends fail once the disk is full."""

    full = False

    def append(self, event: Event) -> Event:
        if self.full:
            raise OSError('no space left on device')
        return super().append(event)


def test_a_workspace_that_stopped_ends_the_steps(
    files: CorrectedFiles,
    staged_binding: StagedFunction,
    definitions: FolderDefinitions,
    sample: toys.Measure,
) -> None:
    log = FullDisk()
    plain = function_binding(toys.PLAIN, staged_binding.function)
    workspace = Workspace(
        proposal='p1', files=files, bind=[plain], log=log, store=FakeStore()
    )
    manager = BatchManager(Client(workspace, submitter='anna'), definitions=definitions)
    intake = Intake(manager, sources=toys.SOURCES)
    sample(1, 'water')
    toys.from_pairs(intake, 'plain', title='A', follow=True)
    log.full = True

    with pytest.raises(Stopped, match='the workspace stopped'):
        follow(
            intake,
            say=lambda _: None,
            pause=lambda: None,
            stopped=lambda: False,
            steps=3,
        )
    with suppress(Stopped):
        workspace.close()


def test_a_signal_ends_follow_once_the_reduction_that_runs_ended(
    from_3: list[str], folder: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = signal.getsignal(signal.SIGINT)
    arrive(folder, 5, content=77)  # its reduction sends SIGINT

    assert main([*from_3, 'follow', '-l', 'x', '--interval', '0']) == 0

    said = capsys.readouterr()
    assert 'stopping after the reductions that run' in said.err
    assert 'done 5' in said.out
    assert signal.getsignal(signal.SIGINT) is before


@pytest.mark.parametrize(
    'option', [['--steps', '0'], ['--interval', '-1'], ['--interval', 'inf']]
)
def test_follow_refuses_steps_and_intervals_that_cannot_be(
    command: list[str], option: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        main([*command, *SOURCES, 'follow', '-l', 'x', *option])

    assert f'{option[1]}: give a number of' in capsys.readouterr().err


def test_files_replaces_the_table_of_files_at_once(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (folder / 'cli_toys_by_stem.py').write_text(
        "def read(path):\n"
        "    if path.suffix == '.json':\n"
        "        return {'run': path.stem.removeprefix('run-')}\n"
    )
    monkeypatch.syspath_prepend(folder)
    out = folder / 'files.csv'
    runs = folder / 'runs.csv'
    runs.write_text('run,role\n1,sample\n')
    text = out.read_text()
    files = ['files', 'cli_toys_by_stem:read', '-o', str(out), '--with', str(runs)]

    with out.open() as old:
        assert main(['-w', str(folder), *files]) == 0
        assert old.read() == text  # never written to: the new file took its name

    assert out.read_text() != text
    assert [p.name for p in folder.iterdir() if 'partial' in p.name] == []


@pytest.mark.parametrize(
    ('line', 'said'),
    [
        ('run-1.json,1,sample', 'files.csv: two rows name the file'),
        ('nope.json,9,sample', 'nope.json, which does not exist'),
    ],
    ids=['twice', 'missing'],
)
def test_a_table_of_files_in_error_is_refused_before_a_command_runs(
    command: list[str],
    folder: Path,
    line: str,
    said: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (folder / 'files.csv').open('a') as f:
        f.write(f'\n{line}')

    assert main([*command, *SOURCES, 'rows', 'pairs']) == 2

    assert said in capsys.readouterr().err
