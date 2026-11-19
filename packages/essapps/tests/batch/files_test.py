# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""A batch from files: a template file, and the command on a workspace in a folder."""

from collections.abc import Iterator
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path

import pandas as pd
import pytest
import yaml

from ess.apps import Template, file
from ess.apps.batch import (
    Batch,
    BatchManager,
    CorrectedFiles,
    FolderDefinitions,
    Header,
    Scope,
    left_to_reduce,
)
from ess.apps.batch.cli import main
from ess.apps.batch.definition import edits_key, settings_key, template_of
from ess.apps.batch.frames import read_table
from ess.apps.batch.text import load_template
from ess.apps.folder import folder_files, open_folder

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


# The command

MODULE = '''
from ess.apps.batch import Row, Source
from ess.apps.testing import StagedFunction, read_file
from ess.spec import RunFile, WorkflowSpec, function_binding
from pydantic import BaseModel
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
    return Outputs(total=value)


def _find(params: FindParams) -> FindOutputs:
    return FindOutputs(offset=Offset(value=read_file(params.sample)))


BINDINGS = [function_binding(SPEC, _reduce), function_binding(FIND, _find)]
UPGRADED = [
    function_binding(SPEC.model_copy(update={'version': 2}), _reduce),
    function_binding(FIND, _find),
]
STAGING = [StagedFunction(SPEC, _reduce), function_binding(FIND, _find)]


def pair(files):
    return [
        Row(str(f.metadata['run']), {'sample': f.ref}, index=int(f.metadata['run']))
        for f in files
        if f.metadata['role'] == 'sample'
    ]


PAIRS = Source(pair, fills=('sample',))
BROKEN = Source(lambda files: [f.metadata['nope'] for f in files], fills=('sample',))
SOURCES = {'pairs': PAIRS, 'broken': BROKEN}
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
    path = folder / 'runs.csv'
    keys = [f'row-{i}' for i in range(len(samples))]
    pd.DataFrame({'key': keys, 'sample': samples}, dtype=str).to_csv(path, index=False)
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
    assert list(read_table(out)['status']) == ['completed', 'completed']


def test_main_returns_3_if_a_row_failed(
    command: list[str], cli_template: Path, folder: Path
) -> None:
    runs = write_rows(folder, ['1', str(BAD)])
    out = folder / 'results.csv'

    code = main(
        [*command, 'reduce', str(cli_template), str(runs), '-l', 'x', '-o', str(out)]
    )

    assert code == 3
    assert list(read_table(out)['status']) == ['completed', 'failed']


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
        return list(read_table(out)['record'])

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

    table = read_table(settings_changed)
    assert list(table['status']) == ['completed, earlier settings'] * 2
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

    assert code == 0
    assert list(read_table(settings_changed)['status']) == ['completed'] * 2
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
    definitions = FolderDefinitions(folder)
    files = CorrectedFiles(folder_files(folder), definitions=definitions)
    bind = import_module('cli_toys_batch_files').BINDINGS
    template = definitions.load(settings_key(label), template_of)
    assert template is not None
    with open_folder(folder, bind, files=files) as client:
        yield Batch(
            client,
            client.spec(template.spec),
            columns=template.blanks,
            label=label,
            definitions=definitions,
        )


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
        assert {r.key: r.values['sample'] for r in batch.rows} == {
            'row-0': file(run=1),
            'row-1': file(run=2),
        }
        assert left_to_reduce(batch.state(), batch.columns) == []


def test_an_edit_of_an_app_is_reduced_by_the_command_given_no_files(
    command: list[str], reduced_once: Path
) -> None:
    with app(reduced_once) as batch:
        batch.rows = batch.rows.edit('row-1', {'sample': file(run=3)})
    out = reduced_once / 'out.csv'

    code = main([*command, 'reduce', '-l', 'x', '--remaining', '-o', str(out)])

    with app(reduced_once) as batch:
        state = batch.state()
        totals = {k: batch.client.output(s.record, 'total') for k, s in state.items()}
        records = batch.client.records(label='x')
    assert code == 0
    assert list(read_table(out)['key']) == ['row-0', 'row-1']
    assert totals == {'row-0': 1.0, 'row-1': 3.0}
    assert len(records) == 3  # row-0 is not reduced again


def test_a_table_given_to_the_command_takes_the_place_of_an_edit_of_an_app(
    command: list[str], reduced_once: Path
) -> None:
    with app(reduced_once) as batch:
        batch.rows = batch.rows.edit('row-1', {'sample': file(run=3)})
    runs = write_rows(reduced_once, ['1', '2'])
    template = reduced_once / 'offset.yaml'

    assert main([*command, 'reduce', str(template), str(runs), '-l', 'x']) == 0

    with app(reduced_once) as batch:
        assert batch.rows['row-1'].values['sample'] == file(run=2)


def test_a_table_given_with_a_source_keeps_only_what_differs_from_the_scan(
    command: list[str], cli_template: Path, folder: Path
) -> None:
    runs = folder / 'runs.csv'
    assert main([*command, 'rows', 'cli_toys_batch_files:PAIRS', '-o', str(runs)]) == 0
    runs.write_text(runs.read_text().replace('3,path:run-3.json', '3,path:run-1.json'))
    source = ['-s', 'cli_toys_batch_files:SOURCES', '--source', 'pairs']

    assert (
        main([*command, 'reduce', *source, str(cli_template), str(runs), '-l', 'x'])
        == 0
    )

    edits = FolderDefinitions(folder).load(edits_key('x'), lambda data: data)
    assert edits == {
        'added': [],
        'edited': {'3': {'sample': {'@ref': 'file', 'file': 'path:run-1.json'}}},
        'cleared': {},
        'removed': [],
    }


# The command and the batches of a manager on one folder


@contextmanager
def manager(folder: Path) -> Iterator[BatchManager]:
    """The manager of an application on the folder, with the sources by name."""
    module = import_module('cli_toys_batch_files')
    definitions = FolderDefinitions(folder)
    files = CorrectedFiles(folder_files(folder), definitions=definitions)
    with open_folder(folder, module.BINDINGS, files=files) as client:
        yield BatchManager(client, sources=module.SOURCES, definitions=definitions)


SOURCES = ['-s', 'cli_toys_batch_files:SOURCES']


def test_reduce_on_a_new_label_writes_a_header_so_the_manager_lists_it(
    command: list[str], cli_template: Path, folder: Path
) -> None:
    runs = write_rows(folder, ['1'])
    reduce = [str(cli_template), str(runs), '-l', 'x', '--source', 'pairs']

    assert main([*command, 'reduce', *SOURCES, *reduce]) == 0

    with manager(folder) as batches:
        assert batches.headers() == {
            'x': Header(spec='cli-toy', columns=('sample',), source='pairs', title='x')
        }


def test_reduce_refuses_a_label_that_has_definitions_but_no_batch(
    command: list[str],
    cli_template: Path,
    folder: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with manager(folder) as batches:
        old = Batch(
            batches.client,
            import_module('cli_toys_batch_files').SPEC,
            columns=['sample'],
            label='old',
            definitions=FolderDefinitions(folder),
        )
        old.settings = {'offset': {'value': 0.5}}
    runs = write_rows(folder, ['1'])

    code = main([*command, 'reduce', str(cli_template), str(runs), '-l', 'old'])

    assert code == 2
    assert 'old: the label has definitions or records' in capsys.readouterr().err
    with manager(folder) as batches:
        assert batches.headers() == {}
        assert batches.client.records(label='old') == []


def test_reduce_on_a_batch_of_the_manager_takes_its_source_and_its_scope(
    command: list[str], folder: Path
) -> None:
    with manager(folder) as batches:
        label = batches.create(
            'cli-toy', title='Later', source='pairs', scope=Scope(start=2)
        )
        before = batches.headers()
    out = folder / 'done.csv'

    code = main([*command, 'reduce', *SOURCES, '-l', label, '-o', str(out)])

    assert code == 0
    assert list(read_table(out)['key']) == ['2', '3']
    with manager(folder) as batches:
        assert batches.headers() == before
        reduced = {r.member for r in batches.client.records(label=label)}
        assert reduced == {'2', '3'}


def test_reduce_refuses_a_row_that_the_source_makes_outside_the_scope(
    command: list[str],
    cli_template: Path,
    folder: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with manager(folder) as batches:
        label = batches.create(
            'cli-toy', title='Later', source='pairs', scope=Scope(start=2)
        )
    runs = folder / 'runs.csv'
    runs.write_text('sample\n1\n')
    reduce = [str(cli_template), str(runs), '-l', label]

    assert main([*command, 'reduce', *SOURCES, *reduce]) == 2
    assert 'the source makes this row outside the scope' in capsys.readouterr().err


def test_reduce_refuses_a_batch_whose_source_is_not_registered(
    command: list[str], folder: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with manager(folder) as batches:
        label = batches.create('cli-toy', title='All', source='pairs')

    assert main([*command, 'reduce', '-l', label]) == 2
    assert (
        f'{label}: the source pairs is not registered; give the sources of the '
        'application with -s/--sources package.module:NAME'
    ) in capsys.readouterr().err


def test_reduce_refuses_a_source_other_than_that_of_the_header(
    command: list[str],
    cli_template: Path,
    folder: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs = write_rows(folder, ['1'])
    assert main([*command, 'reduce', str(cli_template), str(runs), '-l', 'x']) == 0

    code = main([*command, 'reduce', *SOURCES, '--source', 'pairs', '-l', 'x'])

    assert code == 2
    assert 'x: the batch has no source, not pairs' in capsys.readouterr().err


@pytest.mark.parametrize(
    ('template', 'said'),
    [
        (
            'spec: {name: cli-toy, version: 1}\nblanks: [sample, offset]\n',
            "the template of cli-toy with the blanks ['sample', 'offset']",
        ),
        (
            'spec: {name: cli-find, version: 1}\nblanks: [sample]\n',
            "the template of cli-find with the blanks ['sample']",
        ),
    ],
    ids=['blanks', 'spec'],
)
def test_reduce_refuses_a_template_other_than_the_header_says(
    command: list[str],
    cli_template: Path,
    folder: Path,
    capsys: pytest.CaptureFixture[str],
    template: str,
    said: str,
) -> None:
    runs = write_rows(folder, ['1'])
    assert main([*command, 'reduce', str(cli_template), str(runs), '-l', 'x']) == 0
    other = folder / 'other.yaml'
    other.write_text(template)
    capsys.readouterr()

    code = main([*command, 'reduce', str(other), str(runs), '-l', 'x'])

    err = capsys.readouterr().err
    assert code == 2
    assert "x: the batch is of cli-toy with the columns ['sample']" in err
    assert said in err


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


@pytest.mark.parametrize('given', [True, False], ids=['template', 'kept'])
def test_reduce_takes_the_version_of_the_spec_that_the_workspace_binds(
    command: list[str], cli_template: Path, folder: Path, given: bool
) -> None:
    runs = write_rows(folder, ['1'])
    assert main([*command, 'reduce', str(cli_template), str(runs), '-l', 'x']) == 0
    upgraded = [*command[:-1], 'cli_toys_batch_files:UPGRADED']
    template = [str(cli_template), str(runs)] if given else []

    assert main([*upgraded, 'reduce', *template, '-l', 'x', '--remaining']) == 0

    with open_folder(folder, []) as client:
        versions = [r.spec.version for r in client.records(label='x')]
    assert versions == [1, 2]


@pytest.mark.parametrize(
    'template',
    [
        'spec: {name: cli-toy, version: 1}\nblanks: [sample, offset]\n',
        'spec: {name: cli-toy, version: 1}\nparams: {offset: {value: nope}}\n'
        'blanks: [sample]\n',
    ],
    ids=['table', 'settings'],
)
def test_a_refused_first_run_leaves_no_batch(
    command: list[str], cli_template: Path, folder: Path, template: str
) -> None:
    runs = write_rows(folder, ['1'])
    refused = folder / 'refused.yaml'
    refused.write_text(template)

    assert main([*command, 'reduce', str(refused), str(runs), '-l', 'x']) == 2

    assert FolderDefinitions(folder).labels() == []
    assert main([*command, 'reduce', str(cli_template), str(runs), '-l', 'x']) == 0


def test_a_source_that_raises_is_an_error_and_leaves_no_batch(
    command: list[str], cli_template: Path, folder: Path
) -> None:
    runs = write_rows(folder, ['1'])
    reduce = [str(cli_template), str(runs), '-l', 'x', '--source', 'broken']

    with pytest.raises(KeyError, match='nope'):
        main([*command, 'reduce', *SOURCES, *reduce])

    assert FolderDefinitions(folder).labels() == []


def kept(folder: Path, label: str) -> dict[str, str]:
    """The files that the definitions of ``label`` are kept in, by name."""
    files = (folder / 'definitions' / 'labels' / label).iterdir()
    return {path.name: path.read_text() for path in files}


@pytest.mark.parametrize(
    'table', ['sampel\n2\n', 'sample\n1\n'], ids=['column', 'scope']
)
def test_a_refused_reduce_changes_neither_the_settings_nor_the_rows_of_a_batch(
    command: list[str], folder: Path, table: str
) -> None:
    with manager(folder) as batches:
        label = batches.create(
            'cli-toy',
            title='Later',
            source='pairs',
            scope=Scope(start=2),
            settings={'offset': {'value': 0.5}},
        )
        batch = batches.batch(label)
        batch.rows = batch.rows.edit('2', {'sample': file(run=3)})
    before = kept(folder, label)
    template = folder / 'other.yaml'
    template.write_text(
        'spec: {name: cli-toy, version: 1}\nblanks: [sample]\n'
        'params: {offset: {value: 9.0}}\n'
    )
    runs = folder / 'runs.csv'
    runs.write_text(table)

    code = main([*command, 'reduce', *SOURCES, str(template), str(runs), '-l', label])

    assert code == 2
    assert kept(folder, label) == before


def test_reduce_on_a_batch_without_rows_writes_a_table_of_its_columns(
    command: list[str], folder: Path
) -> None:
    with manager(folder) as batches:
        label = batches.create(
            'cli-toy', title='Later', source='pairs', scope=Scope(start=99)
        )
    out = folder / 'done.csv'

    code = main([*command, 'reduce', *SOURCES, '-l', label, '-o', str(out)])

    assert code == 0
    assert list(read_table(out).columns) == [
        'key',
        'sample',
        'status',
        'record',
        'failure',
    ]
    assert read_table(out).empty


def test_a_template_without_a_table_is_the_settings_of_a_batch_with_a_source(
    command: list[str], folder: Path
) -> None:
    with manager(folder) as batches:
        label = batches.create(
            'cli-toy', title='Later', source='pairs', scope=Scope(start=2)
        )
    template = folder / 'offset.yaml'
    template.write_text(
        'spec: {name: cli-toy, version: 1}\nblanks: [sample]\n'
        'params: {offset: {value: 0.5}}\n'
    )
    out = folder / 'done.csv'

    reduce = [str(template), '-l', label, '-o', str(out)]
    assert main([*command, 'reduce', *SOURCES, *reduce]) == 0

    assert list(read_table(out)['key']) == ['2', '3']
    with manager(folder) as batches:
        assert batches.batch(label).settings == {'offset': {'value': 0.5}}


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

    code = main([*command, 'rows', 'cli_toys_batch_files:PAIRS'])

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
    assert list(result['status']) == ['completed']


@pytest.mark.parametrize(
    ('arguments', 'said'),
    [
        (['rows', 'cli_toys_batch_files:pair'], 'is a function, not a Source'),
        (
            ['reduce', '-s', 'cli_toys_batch_files:pair', '-l', 'x'],
            'is a function, not a mapping of names to Sources',
        ),
        (
            ['reduce', *SOURCES, '--source', 'p', '-l', 'x'],
            '--source p is not a name in --sources: pairs, broken',
        ),
        (
            ['reduce', '--source', 'pairs', '-l', 'x'],
            '--source pairs is not a name in --sources: none given',
        ),
    ],
    ids=['rows', 'reduce', 'reduce-name', 'reduce-no-sources'],
)
def test_main_refuses_a_source_that_is_not_a_source_before_the_folder_is_opened(
    command: list[str],
    folder: Path,
    arguments: list[str],
    said: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_:
        main([*command, *arguments])

    assert exit_.value.code == 2
    assert said in capsys.readouterr().err
    assert not (folder / 'log.jsonl').exists()


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

    assert list(read_table(out)['status']) == ['completed']


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

    result = read_table(files)
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

    assert main([*command, 'rows', 'cli_toys_batch_files:PAIRS', '-o', str(runs)]) == 0
    code = main(
        [*command, 'reduce', str(cli_template), str(runs), '-l', 'x', '-o', str(out)]
    )

    result = read_table(out)
    assert code == 0
    assert list(result['key']) == ['1', '2', '3']
    assert list(result['status']) == ['completed'] * 3


def test_the_rows_that_main_makes_read_the_corrections_kept_in_the_folder(
    command: list[str], folder: Path
) -> None:
    files = CorrectedFiles(folder_files(folder), definitions=FolderDefinitions(folder))
    files.correct(files.resolve(file(run=BAD)), role='sample')
    runs = folder / 'runs.csv'

    assert main([*command, 'rows', 'cli_toys_batch_files:PAIRS', '-o', str(runs)]) == 0

    assert list(read_table(runs)['key']) == ['1', '2', '3', str(BAD)]


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
    assert list(read_table(out)['status']) == ['completed']


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


@pytest.mark.parametrize('subcommand', ['rows', 'export'])
def test_main_refuses_the_commands_that_need_bindings_without_them(
    folder: Path, subcommand: str
) -> None:
    arguments = {'rows': ['x:y'], 'export': ['done.csv', 'out']}[subcommand]

    with pytest.raises(SystemExit) as exit_:
        main(['-w', str(folder), subcommand, *arguments])

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
