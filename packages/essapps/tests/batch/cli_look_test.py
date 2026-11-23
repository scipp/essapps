# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The commands of ``essapps-batch`` that look at a folder, such as rows and show."""

import json
from importlib import import_module
from pathlib import Path

import pytest

from ess.apps import Template, file
from ess.apps.batch import (
    CorrectedFiles,
    FolderDefinitions,
)
from ess.apps.batch.cli import main
from ess.apps.batch.table import read_text
from ess.apps.batch.templates import load_template
from ess.apps.folder import folder_files

from .conftest import (
    BAD,
    SOURCES,
    app,
    edit,
    intake_on,
    later,
    write_rows,
)


def test_definitions_that_cannot_be_read_are_said_without_a_traceback(
    command: list[str], folder: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (folder / 'definitions').mkdir()
    (folder / 'definitions' / 'corrections.json').write_text('{"x": ')

    code = main([*command, *SOURCES, 'rows', 'pairs'])

    assert code == 2
    assert 'definitions/corrections.json: ' in capsys.readouterr().err


def test_main_says_metadata_that_a_row_source_refuses_at_its_fields(
    command: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code = main([*command, '-s', 'cli_toys_batch_files:REFUSING', 'rows', 'refusing'])

    assert code == 2
    assert capsys.readouterr().err == (
        'run must be a valid integer, unable to parse string as an integer\n'
    )


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


def test_the_rows_that_main_makes_read_the_corrections_kept_in_the_folder(
    command: list[str], folder: Path
) -> None:
    files = CorrectedFiles(folder_files(folder), definitions=FolderDefinitions(folder))
    files.correct(files.resolve(file(run=BAD)), role='sample')
    runs = folder / 'runs.csv'

    assert main([*command, *SOURCES, 'rows', 'pairs', '-o', str(runs)]) == 0

    assert list(read_text(runs)['key']) == ['1', '2', '3', str(BAD)]


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


def test_the_template_of_a_batch_holds_its_settings(
    command: list[str], reduced_once: Path
) -> None:
    path = reduced_once / 'now.yaml'

    assert main([*command, 'template', '-l', 'x', '-o', str(path)]) == 0

    assert load_template(path) == Template(
        import_module('cli_toys_batch_files').SPEC, params={'offset': {'value': 0.5}}
    )


def test_export_of_a_label_writes_the_outputs_of_its_rows_as_they_stand(
    command: list[str], reduced_once: Path
) -> None:
    out = reduced_once / 'exported.csv'

    code = main(
        [*command, 'export', '-l', 'x', str(reduced_once / 'out'), '-o', str(out)]
    )

    assert code == 0
    assert [Path(p).is_file() for p in read_text(out)['total']] == [True, True]
