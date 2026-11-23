# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The command ``essapps-batch reduce`` and the refusals of its command line."""

from importlib import import_module
from pathlib import Path

import pandas as pd
import pytest

from ess.apps.batch import (
    Batch,
    FolderDefinitions,
    Header,
    Range,
    left_to_reduce,
)
from ess.apps.batch.cli import main
from ess.apps.batch.table import KEY, read_text
from ess.apps.folder import open_folder

from .conftest import (
    BAD,
    SOURCES,
    app,
    edit,
    intake_on,
    later,
    write_rows,
)


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
