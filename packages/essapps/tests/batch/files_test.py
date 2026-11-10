# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""A batch from files: a template file, and the command on a workspace in a folder."""

from pathlib import Path

import pandas as pd
import pytest
import yaml

from ess.apps import Template
from ess.apps.batch.cli import main
from ess.apps.batch.frames import read_table
from ess.apps.batch.text import load_template
from ess.apps.folder import open_folder

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
from ess.apps.batch import Row
from ess.apps.testing import read_file
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


def pair(files):
    return [
        Row(str(f.metadata['run']), {'sample': f.ref})
        for f in files
        if f.metadata['role'] == 'sample'
    ]
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


def test_main_returns_2_and_says_why_if_a_row_cannot_run(
    command: list[str],
    cli_template: Path,
    folder: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runs = write_rows(folder, ['1', ''])

    code = main([*command, 'reduce', str(cli_template), str(runs), '-l', 'x'])

    assert code == 2
    assert 'row-1' in capsys.readouterr().err


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

    assert main([*command, 'rows', 'cli_toys_batch_files:pair', '-o', str(runs)]) == 0
    code = main(
        [*command, 'reduce', str(cli_template), str(runs), '-l', 'x', '-o', str(out)]
    )

    result = read_table(out)
    assert code == 0
    assert list(result['key']) == ['1', '2', '3']
    assert list(result['status']) == ['completed'] * 3


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
