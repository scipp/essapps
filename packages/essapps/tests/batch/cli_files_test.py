# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The command ``essapps-batch files``, and a ``files.csv`` in error."""

from pathlib import Path

import pandas as pd
import pytest

from ess.apps.batch.cli import main

from .conftest import (
    SOURCES,
    write_rows,
)


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
