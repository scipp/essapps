# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The commands of ``essbatch`` that act on a batch, from rows to follow."""

import json
from pathlib import Path

import pytest
from ess.batch import (
    FolderDefinitions,
)
from ess.batch.cli import main
from ess.batch.table import read_text

from .conftest import (
    SOURCES,
    arrive,
    intake_on,
    later,
    write_rows,
)


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
        (['--first', '9'], '9 is not a complete row'),
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
