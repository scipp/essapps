# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""A batch run as a command: a table reduced, and the table of files."""

from pathlib import Path

import pandas as pd
import pydantic
import pytest

from ess.apps import Client, SubmitError, Template, file
from ess.apps.batch import Batch
from ess.apps.batch.frames import (
    RESULT,
    described,
    reduce_batch,
    reduce_table,
    with_results,
)
from ess.apps.batch.table import KEY
from ess.apps.folder import open_folder
from ess.apps.log import Finished, Log, Started
from ess.apps.testing import CountingClient, StagedFunction
from ess.spec import Binding, WorkflowSpec, function_binding

from . import toys
from .conftest import BAD


def rows(**columns: list[object]) -> pd.DataFrame:
    """A table of the rows ``a`` and ``b``."""
    return pd.DataFrame(columns, index=pd.Index(['a', 'b'], name=KEY))


def totals(client: Client, label: str) -> dict[str, float]:
    return {
        r.member: client.output(r, 'total')
        for r in client.records(label=label)
        if r.member is not None
    }


def test_a_table_with_results_has_what_came_of_each_row(
    batch: Batch, client: Client, sample: toys.Measure
) -> None:
    sample(1, 'water')
    sample(2, 'sds')
    batch.put(toys.rows(client))
    client.wait(batch.reduce(['1']))

    table = with_results(client, batch.state(), batch.frame())

    assert list(table.columns) == ['run', 'name', *toys.COLUMNS, *RESULT]
    assert list(table['status']) == ['completed', 'not reduced']
    assert table.loc['2', 'record'] == ''


def test_reduce_batch_reduces_the_rows_of_keys_and_returns_them_in_order(
    batch: Batch, client: Client, sample: toys.Measure
) -> None:
    for run in (1, 2, 3):
        sample(run, f's{run}')
    batch.put(toys.rows(client))

    table = reduce_batch(batch, ['3', '1'])

    assert list(table.index) == ['3', '1']
    assert list(table['status']) == ['completed', 'completed']
    assert batch.state(['2'])['2'].record is None


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
    pd.testing.assert_frame_equal(
        result.drop(columns=list(RESULT)),
        table,
        check_dtype=False,
        check_index_type=False,
    )
    assert totals(folder_client, 'scan') == {'a': 0.5, 'b': 1.0}


def test_an_empty_cell_of_a_column_with_a_default_takes_the_default(
    folder_client: Client, table: pd.DataFrame
) -> None:
    template = Template(toys.PLAIN)

    reduce_table(folder_client, template, table.assign(thickness=[None, 4]), label='s')

    assert totals(folder_client, 's') == {'a': 1.0, 'b': 0.5}


def test_a_row_that_fails_says_why_and_the_other_rows_complete(
    folder_client: Client, template: Template
) -> None:
    table = rows(sample=['run:1', f'run:{BAD}'], transmission=['run:1001'] * 2)

    result = reduce_table(folder_client, template, table, label='scan')

    assert list(result['status']) == ['completed', 'failed']
    assert 'refused' in result.loc['b', 'failure']
    assert result.loc['a', 'failure'] == ''


def test_a_row_that_cannot_run_is_refused_by_key_and_nothing_is_submitted(
    folder_client: Client, template: Template
) -> None:
    table = rows(sample=['run:1', 'run:2'], transmission=['run:1001', None])

    with pytest.raises(SubmitError, match='b'):
        reduce_table(folder_client, template, table, label='scan')

    assert folder_client.records(label='scan') == []


def test_a_column_named_as_a_setting_of_the_template_is_a_blank(
    folder_client: Client, template: Template, table: pd.DataFrame
) -> None:
    reduce_table(folder_client, template, table.assign(thickness=[4, 4]), label='s')

    assert totals(folder_client, 's') == {'a': 0.25, 'b': 0.5}


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
        reduce_table(client, template, rows(status=[1.0, 2.0]), label='scan')


def test_remaining_leaves_out_the_rows_completed(
    folder_client: Client, template: Template
) -> None:
    table = rows(sample=['run:1', f'run:{BAD}'], transmission=['run:1001'] * 2)
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


def test_a_table_is_reduced_with_one_state_to_choose_the_rows_and_one_to_report(
    folder_client: Client, template: Template, table: pd.DataFrame
) -> None:
    counting = CountingClient(folder_client)

    reduce_table(counting, template, table, label='scan')  # type: ignore[arg-type]

    assert counting.reads['resolve'] == 2


def test_rows_pending_after_a_restart_finish_and_are_not_submitted_again(
    folder: Path,
    folder_bindings: list[Binding],
    template: Template,
    table: pd.DataFrame,
) -> None:
    with open_folder(folder, folder_bindings) as first:
        done = reduce_table(first, template, table, label='scan', persist=True)
    log = folder / 'log.jsonl'
    header, *lines = log.read_bytes().splitlines(keepends=True)
    events = Log.read(log)
    assert len(lines) == len(events)
    unstarted = [
        ln
        for ln, e in zip(lines, events, strict=True)
        if not isinstance(e, Started | Finished)
    ]
    assert len(unstarted) < len(lines)
    # as if the process had been killed before the records started
    log.write_bytes(b''.join([header, *unstarted]))

    with open_folder(folder, folder_bindings) as second:
        result = reduce_table(
            second, template, done, label='scan', persist=True, remaining=True
        )

        assert list(result['status']) == ['completed', 'completed']
        assert list(result['record']) == list(done['record'])
        assert len(second.records(label='scan')) == 2


# The table of files


@pytest.fixture
def scanned() -> pd.DataFrame:
    """The table of a scan: two runs with a title, and a file that is no run."""
    return pd.DataFrame(
        {
            'file': ['1.nxs', '2.nxs', 'beam.h5'],
            'run': ['1', '2', ''],
            'title': ['one', 'two', ''],
        }
    )


def test_a_file_gets_what_the_run_table_says_of_its_run(scanned: pd.DataFrame) -> None:
    run_table = pd.DataFrame({'run': [2, 1], 'role': ['transmission', 'sample']})

    files = described(scanned, run_table)

    assert list(files.columns) == ['file', 'run', 'title', 'role']
    assert list(files['role']) == ['sample', 'transmission', '']


def test_a_run_of_the_run_table_without_a_file_is_ignored(
    scanned: pd.DataFrame,
) -> None:
    run_table = pd.DataFrame({'run': ['1', '7'], 'role': ['sample', 'sample']})

    files = described(scanned, run_table)

    assert list(files['file']) == ['1.nxs', '2.nxs', 'beam.h5']
    assert list(files['role']) == ['sample', '', '']


def test_a_row_of_the_run_table_without_a_run_describes_the_file_it_names(
    scanned: pd.DataFrame,
) -> None:
    run_table = pd.DataFrame(
        {'run': ['1', ''], 'file': ['', 'beam.h5'], 'role': ['sample', 'direct_beam']}
    )

    files = described(scanned, run_table)

    assert list(files['role']) == ['sample', '', 'direct_beam']


def test_a_cell_of_the_run_table_replaces_what_the_file_says_unless_it_is_empty(
    scanned: pd.DataFrame,
) -> None:
    run_table = pd.DataFrame({'run': ['1', '2'], 'title': ['water', '']})

    files = described(scanned, run_table)

    assert list(files['title']) == ['water', 'two', '']


def test_two_rows_of_the_run_table_for_one_run_are_refused(
    scanned: pd.DataFrame,
) -> None:
    run_table = pd.DataFrame({'run': ['1', 1], 'role': ['sample', 'background']})

    with pytest.raises(
        ValueError, match='two rows of the run table describe the run 1'
    ):
        described(scanned, run_table)


def test_a_row_of_the_run_table_that_names_neither_run_nor_file_is_refused(
    scanned: pd.DataFrame,
) -> None:
    with pytest.raises(ValueError, match='neither a run nor a file'):
        described(scanned, pd.DataFrame({'run': [''], 'role': ['sample']}))
