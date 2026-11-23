# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""A batch run as a command: a batch table reduced."""

from pathlib import Path

import pandas as pd
import pydantic
import pytest

from ess.apps import Client, SubmitError, Template, file
from ess.apps.batch import Batch
from ess.apps.batch.frames import RESULT, reduce_batch, reduce_table, with_results
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
    assert list(table['status']) == ['done', 'ready']
    assert table.loc['2', 'record'] == ''


def test_reduce_batch_reduces_the_rows_of_keys_and_returns_them_in_order(
    batch: Batch, client: Client, sample: toys.Measure
) -> None:
    for run in (1, 2, 3):
        sample(run, f's{run}')
    batch.put(toys.rows(client))

    table = reduce_batch(batch, ['3', '1'])

    assert list(table.index) == ['3', '1']
    assert list(table['status']) == ['done', 'done']
    assert batch.state(['2'])['2'].record is None


# Reducing a table


def test_each_row_completes_as_a_record_labelled_and_keyed_by_the_row(
    folder_client: Client, template: Template, table: pd.DataFrame
) -> None:
    result = reduce_table(folder_client, template, table, label='scan')

    records = folder_client.records(label='scan')
    assert list(result['status']) == ['done', 'done']
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

    assert list(result['status']) == ['done', 'failed']
    assert 'refused' in result.loc['b', 'failure']
    assert result.loc['a', 'failure'] == ''


def test_a_row_that_cannot_run_is_refused_by_key_and_nothing_is_submitted(
    folder_client: Client, template: Template
) -> None:
    table = rows(sample=['run:1', 'run:2'], transmission=['run:1001', None])

    with pytest.raises(SubmitError, match='b'):
        reduce_table(folder_client, template, table, label='scan')

    assert folder_client.records(label='scan') == []


def test_a_value_of_the_template_at_a_column_is_that_of_each_empty_cell(
    folder_client: Client, template: Template, table: pd.DataFrame
) -> None:
    given = table.assign(thickness=[4, None])  # the template's thickness: 2.0

    reduce_table(folder_client, template, given, label='s')

    assert totals(folder_client, 's') == {'a': 0.25, 'b': 1.0}


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

    assert list(result['status']) == ['done', 'done']
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

        assert list(result['status']) == ['done', 'done']
        assert list(result['record']) == list(done['record'])
        assert len(second.records(label='scan')) == 2
