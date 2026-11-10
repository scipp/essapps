# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The rows of a batch as a table of text, and a table reduced."""

from pathlib import Path

import pandas as pd
import pydantic
import pytest

from ess.apps import Client, SubmitError, Template, file
from ess.apps.batch import Row
from ess.apps.batch.tables import (
    read_table,
    reduce_table,
    rows_of,
    table_of,
    write_table,
)
from ess.apps.folder import open_folder
from ess.apps.log import Finished, Log
from ess.apps.testing import StagedFunction
from ess.spec import Binding, WorkflowSpec, function_binding

from . import toys
from .conftest import BAD


def frame(**columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns, dtype=str)


def totals(client: Client, label: str) -> dict[str, float]:
    return {
        r.member: client.output(r, 'total')
        for r in client.records(label=label)
        if r.member is not None
    }


def test_the_key_of_a_row_is_its_first_column_as_text() -> None:
    table = frame(name=['water'], sample=['60339'])

    (row,) = rows_of(table, toys.Params, ['sample'])

    assert row.key == 'water'


def test_a_column_of_the_batch_is_read_as_a_cell_and_named_files_are_files() -> None:
    table = frame(name=['a'], sample=['60339'], thickness=['2'])

    (row,) = rows_of(table, toys.Params, ['sample', 'thickness'])

    assert row.values == {'sample': file(run=60339), 'thickness': 2}


def test_an_empty_cell_is_left_out_of_the_values() -> None:
    table = frame(name=['a'], sample=['60339'], thickness=[''])

    (row,) = rows_of(table, toys.Params, ['sample', 'thickness'])

    assert row.values == {'sample': file(run=60339)}


def test_a_column_that_is_not_of_the_batch_is_info() -> None:
    table = frame(name=['a'], sample=['60339'], note=['dry'], gap=[''])

    (row,) = rows_of(table, toys.Params, ['sample'])

    assert row.info == {'name': 'a', 'note': 'dry'}


def test_the_key_column_can_be_a_column_of_the_batch() -> None:
    table = frame(sample=['60339'])

    (row,) = rows_of(table, toys.Params, ['sample'])

    assert (row.key, row.values) == ('60339', {'sample': file(run=60339)})


def test_a_column_of_the_batch_that_the_table_lacks_is_refused_by_name() -> None:
    table = frame(name=['a'], sample=['60339'])

    with pytest.raises(ValueError, match='transmission'):
        rows_of(table, toys.Params, ['sample', 'transmission'])


def test_rows_written_as_a_table_are_read_back() -> None:
    rows = [
        Row('a', {'sample': file(run=1), 'thickness': 2.5}, {'name': 'water'}),
        Row('b', {'sample': file(run=2)}, {'name': 'sds'}),
    ]

    read = rows_of(table_of(rows), toys.Params, ['sample', 'thickness'])

    assert [(r.key, r.values) for r in read] == [(r.key, r.values) for r in rows]
    assert [
        {n: r.info[n] for n in w.info} for r, w in zip(read, rows, strict=True)
    ] == [r.info for r in rows]


def test_a_column_named_as_a_parameter_that_is_not_a_blank_is_refused() -> None:
    table = frame(name=['a'], sample=['1'], thickness=['3'])

    with pytest.raises(ValueError, match='thickness'):
        rows_of(table, toys.Params, ['sample'])


def test_info_named_as_a_value_column_is_refused() -> None:
    row = Row('a', {'thickness': 2}, {'thickness': 'thick'})

    with pytest.raises(ValueError, match='thickness'):
        table_of([row])


def test_a_table_of_rows_names_its_key_column_key() -> None:
    table = table_of([Row('a', {'thickness': 2}, {'name': 'x'})])

    assert list(table.columns) == ['key', 'thickness', 'name']


@pytest.mark.parametrize('suffix', ['.csv', '.tsv', '.xlsx'])
def test_a_table_written_is_read_back_with_every_cell_as_text(
    tmp_path: Path, suffix: str
) -> None:
    table = frame(name=['a', 'b'], sample=['60339', ''], thickness=['1.0', '2'])
    path = tmp_path / f'table{suffix}'

    write_table(table, path)

    pd.testing.assert_frame_equal(read_table(path), table)


@pytest.mark.parametrize('function', [read_table, lambda p: write_table(frame(), p)])
def test_a_table_of_an_unknown_suffix_is_refused(
    tmp_path: Path, function: object
) -> None:
    with pytest.raises(ValueError, match='table'):
        function(tmp_path / 'table.json')  # type: ignore[operator]


# Reducing a table


def test_each_row_completes_as_a_record_labelled_and_keyed_by_the_row(
    folder_client: Client, template: Template, table: pd.DataFrame
) -> None:
    result = reduce_table(folder_client, template, table, label='scan')

    records = folder_client.records(label='scan')
    assert list(result['status']) == ['completed', 'completed']
    assert list(result['record']) == [
        {r.member: r.id for r in records}[k] for k in 'ab'
    ]
    assert totals(folder_client, 'scan') == {'a': 0.5, 'b': 1.0}


def test_an_empty_cell_of_a_column_with_a_default_takes_the_default(
    folder_client: Client, table: pd.DataFrame
) -> None:
    template = Template(
        toys.PLAIN, params={}, blanks=['sample', 'transmission', 'thickness']
    )

    reduce_table(folder_client, template, table.assign(thickness=['', '4']), label='s')

    assert totals(folder_client, 's') == {'a': 1.0, 'b': 0.5}


def test_a_row_that_fails_says_why_and_the_other_rows_complete(
    folder_client: Client, template: Template
) -> None:
    table = frame(name=['a', 'b'], sample=['1', str(BAD)], transmission=['1001'] * 2)

    result = reduce_table(folder_client, template, table, label='scan')

    assert list(result['status']) == ['completed', 'failed']
    assert 'refused' in result.loc[1, 'failure']
    assert result.loc[0, 'failure'] == ''


def test_a_row_that_cannot_run_is_refused_by_key_and_nothing_is_submitted(
    folder_client: Client, template: Template
) -> None:
    table = frame(name=['a', 'b'], sample=['1', '2'], transmission=['1001', ''])

    with pytest.raises(SubmitError, match='b'):
        reduce_table(folder_client, template, table, label='scan')

    assert folder_client.records(label='scan') == []


def test_a_column_of_a_fixed_parameter_is_refused_and_nothing_is_submitted(
    folder_client: Client, template: Template, table: pd.DataFrame
) -> None:
    with pytest.raises(ValueError, match='thickness'):
        reduce_table(
            folder_client, template, table.assign(thickness=['3', '4']), label='scan'
        )

    assert folder_client.records(label='scan') == []


def test_a_blank_named_as_a_result_column_is_refused(
    folder: Path, table: pd.DataFrame
) -> None:
    class Params(pydantic.BaseModel):
        status: float

    class Outputs(pydantic.BaseModel):
        total: float

    spec = WorkflowSpec(
        name='status', version=1, title='S', description='S.', params=Params,
        outputs=Outputs,
    )  # fmt: skip
    template = Template(spec, blanks=['status'])
    bind = [function_binding(spec, lambda p: Outputs(total=p.status))]

    with open_folder(folder, bind) as client, pytest.raises(ValueError, match='status'):
        reduce_table(client, template, frame(key=['a'], status=['1']), label='scan')


def test_remaining_leaves_out_the_rows_completed(
    folder_client: Client, template: Template
) -> None:
    table = frame(name=['a', 'b'], sample=['1', str(BAD)], transmission=['1001'] * 2)
    reduce_table(folder_client, template, table, label='scan')

    reduce_table(folder_client, template, table, label='scan', remaining=True)

    members = [r.member for r in folder_client.records(label='scan')]
    assert (members.count('a'), members.count('b')) == (1, 2)


def test_without_remaining_every_row_is_submitted_again(
    folder_client: Client, template: Template, table: pd.DataFrame
) -> None:
    reduce_table(folder_client, template, table, label='scan')

    reduce_table(folder_client, template, table, label='scan')

    assert len(folder_client.records(label='scan')) == 4


def test_the_result_is_the_table_of_the_next_call(
    folder_client: Client, template: Template, table: pd.DataFrame
) -> None:
    first = reduce_table(folder_client, template, table, label='scan')

    again = reduce_table(folder_client, template, first, label='scan', remaining=True)
    new = reduce_table(folder_client, template, first, label='scan')

    assert list(again.columns) == list(first.columns)
    pd.testing.assert_frame_equal(again, first)
    assert len(folder_client.records(label='scan')) == 4
    assert not set(new['record']) & set(first['record'])


def test_a_template_references_a_record_of_an_earlier_session(
    folder: Path, folder_bindings: list[Binding], table: pd.DataFrame
) -> None:
    with open_folder(folder, folder_bindings) as first:
        found = first.submit(toys.FIND, params={'sample': file(run=3)}, persist=True)
        first.wait([found])
    template = Template(
        toys.PLAIN,
        params={
            'thickness': 1.0,
            'offset': {'@ref': 'output', 'record': found.id, 'output': 'offset'},
        },
        blanks=['sample', 'transmission'],
    )

    with open_folder(folder, folder_bindings) as second:
        reduce_table(second, template, table, label='scan')

        assert totals(second, 'scan') == {'a': 4.0, 'b': 5.0}


def test_rows_staged_run_through_a_stage(
    folder_client: Client, folder_staged: StagedFunction, table: pd.DataFrame
) -> None:
    template = Template(
        toys.STAGED, params={'thickness': 2.0}, blanks=['sample', 'transmission']
    )

    result = reduce_table(folder_client, template, table, label='scan', staged=True)

    assert list(result['status']) == ['completed', 'completed']
    assert folder_staged.staged != []


def test_rows_pending_after_a_restart_finish_and_are_not_submitted_again(
    folder: Path,
    folder_bindings: list[Binding],
    template: Template,
    table: pd.DataFrame,
) -> None:
    with open_folder(folder, folder_bindings) as first:
        done = reduce_table(first, template, table, label='scan', persist=True)
    log = folder / 'log.jsonl'
    lines = log.read_bytes().splitlines(keepends=True)
    events = Log.read(log)
    assert len(lines) == len(events)
    unfinished = [
        ln for ln, e in zip(lines, events, strict=True) if type(e) is not Finished
    ]
    assert len(unfinished) < len(lines)
    log.write_bytes(b''.join(unfinished))  # as if the process had been killed

    with open_folder(folder, folder_bindings) as second:
        result = reduce_table(
            second, template, done, label='scan', persist=True, remaining=True
        )

        assert list(result['status']) == ['completed', 'completed']
        assert list(result['record']) == list(done['record'])
        assert len(second.records(label='scan')) == 2


# A table made in a notebook


def test_a_table_of_numbers_and_gaps_is_read_as_one_of_text() -> None:
    typed = pd.DataFrame(
        {
            'name': ['a', 'b', 'c'],
            'sample': [1.0, 2.0, None],  # pandas holds numbers next to a gap as floats
            'thickness': [0.5, float('nan'), 2],
        }
    )
    text = frame(
        name=['a', 'b', 'c'], sample=['1', '2', ''], thickness=['0.5', '', '2']
    )
    columns = ['sample', 'thickness']

    assert rows_of(typed, toys.Params, columns) == rows_of(text, toys.Params, columns)


def test_a_cell_may_hold_a_value_that_is_not_text() -> None:
    table = pd.DataFrame({'name': ['a'], 'binning': [{'bins': 3}], 'offset': [None]})

    (row,) = rows_of(table, toys.Params, ['binning', 'offset'])

    assert row.values == {'binning': {'bins': 3}}


def test_a_table_of_numbers_made_in_a_notebook_is_reduced(
    folder_client: Client, template: Template
) -> None:
    table = pd.DataFrame({'sample': [1, 2], 'transmission': [1001, 1002]})

    result = reduce_table(folder_client, template, table, label='typed')

    assert list(result['status']) == ['completed', 'completed']
    assert [r.member for r in folder_client.records(label='typed')] == ['1', '2']
