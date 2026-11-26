# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""A batch on a real in-process workspace with fake workflows."""

import csv
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from ess.batch import (
    Batch,
    CorrectedFiles,
    DefinitionWriteError,
    FolderDefinitions,
    RowState,
    definition,
    left_to_reduce,
    ready,
    rows_of,
    with_changed_settings,
)
from ess.batch.table import as_request, cell_of, data_of
from pydantic import BaseModel, Field, StrictInt

from ess.apps import Client, Status, SubmitError, Template, Workspace, file, local
from ess.apps.folder import ArrivingFiles, open_folder, write_files_table
from ess.apps.log import Log
from ess.apps.testing import CountingClient, FakeFiles, FakeStore, StagedFunction
from ess.spec import (
    Binding,
    FileRef,
    Referenced,
    RunFile,
    SampleRunFile,
    WorkflowSpec,
    function_binding,
)

from .toys import (
    COLUMNS,
    FIND,
    PLAIN,
    SAMPLES,
    STAGED,
    Gate,
    Measure,
    Offset,
    Outputs,
    Refusing,
    edit,
    rows,
    unreachable,
)

PLAIN_V2 = PLAIN.model_copy(update={'version': 2})


class StrictParams(BaseModel):
    sample: RunFile
    counts: list[StrictInt] = Field(default_factory=lambda: [1])
    numbers: list[int] = Field(default_factory=lambda: [1])


STRICT = WorkflowSpec(
    name='strict',
    version=1,
    title='Strict',
    description='A toy whose parameters tell 1 and True apart.',
    params=StrictParams,
    outputs=Outputs,
)


class OptionalParams(BaseModel):
    sample: RunFile
    transmission: RunFile | None


OPTIONAL = WorkflowSpec(
    name='optional',
    version=1,
    title='Optional',
    description='A toy whose transmission run may be none, with no default.',
    params=OptionalParams,
    outputs=Outputs,
)


class NumberParams(BaseModel):
    bins: StrictInt
    scale: float


NUMBERS = WorkflowSpec(
    name='numbers',
    version=1,
    title='Numbers',
    description='A toy whose parameters are all numbers, one a strict int.',
    params=NumberParams,
    outputs=Outputs,
)

SETTINGS: dict[str, Any] = {'binning': {'bins': 2}}
OTHER_SETTINGS: dict[str, Any] = {'binning': {'bins': 3}}


@pytest.fixture
def measured(batch: Batch, client: Client, sample: Measure) -> Batch:
    """``batch`` with the rows of samples 1 and 2, and settings."""
    sample(1, 'water')
    sample(2, 'sds')
    batch.set_table(rows(client))
    batch.settings = SETTINGS
    return batch


def test_the_table_set_is_the_table_and_its_parameter_columns_the_blanks(
    batch: Batch, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    table = rows(client).assign(note='a')

    batch.set_table(table)

    frame = batch.table()
    assert list(frame.index) == ['1']
    assert list(frame.columns) == ['run', 'name', *COLUMNS, 'note']
    assert (frame.loc['1', 'name'], frame.loc['1', 'sample']) == ('water', 'uuid:run-1')
    assert batch.parameter_columns == batch.template().blanks == COLUMNS
    assert batch.settings_fields == ('binning', 'offset')


def test_a_table_that_is_refused_changes_nothing(measured: Batch) -> None:
    before = measured.table()
    twice = pd.concat([before, before.loc[['1']]])
    thick = before.assign(thickness='thick')

    for frame in (twice, thick):
        with pytest.raises(ValueError, match="'1'"):
            measured.set_table(frame)

    pd.testing.assert_frame_equal(measured.table(), before)


def test_a_table_set_again_writes_nothing(
    client: Client, sample: Measure, refusing: Refusing
) -> None:
    sample(1, 'water')
    batch = Batch(client, PLAIN, label='s', table=rows(client), definitions=refusing)
    batch.set_table(batch.table().assign(thickness=2.0))
    refusing.part = 'table'

    batch.set_table(batch.table())

    with pytest.raises(DefinitionWriteError):
        batch.set_table(batch.table().assign(thickness=3.0))
    assert batch.table().loc['1', 'thickness'] == 2.0


def test_set_table_keeps_the_settings_at_a_column_added_and_at_a_column_dropped(
    client: Client, sample: Measure, definitions: FolderDefinitions
) -> None:
    sample(1, 'water')
    settings = {**SETTINGS, 'offset': {'value': 1.0}}
    batch = Batch(client, PLAIN, label='s', table=rows(client), definitions=definitions)
    batch.settings = settings

    batch.set_table(batch.table().assign(offset=None))
    as_column = (batch.settings, batch.parameter_columns, batch.template().params)
    batch.set_table(batch.table().drop(columns=['offset', 'thickness']))

    assert as_column == (settings, (*COLUMNS, 'offset'), SETTINGS)
    assert (batch.settings, batch.parameter_columns) == (
        settings,
        ('sample', 'transmission'),
    )
    assert batch.template().params == settings
    kept = definition.SETTINGS.load(definitions, 's')
    assert kept == Template(PLAIN, params=settings)


def test_rows_are_reduced_and_found_by_their_key(
    measured: Batch, client: Client
) -> None:
    records = measured.reduce(['1', '2'])
    client.wait(records)

    assert client.output(records['2'], 'total') == 2.0
    assert {r.member for r in client.records(label='scan')} == {'1', '2'}
    state = measured.state()
    assert state['1'].record == records['1']
    assert state['1'].record_status is Status.COMPLETED


def test_the_state_of_chosen_rows_asks_the_status_of_their_records_only(
    client: Client, sample: Measure
) -> None:
    counting = CountingClient(client)
    sample(1, 'water')
    sample(2, 'sds')
    batch = Batch(counting, STAGED, label='scan', table=rows(client))  # type: ignore[arg-type]
    client.wait(batch.reduce(['1']))
    counting.reads.clear()

    assert list(batch.state(['2'])) == ['2']
    assert counting.reads['status'] == 0


def test_a_batch_starts_with_a_copy_of_the_settings_given(
    client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    given = {'binning': {'bins': 3}}
    batch = Batch(client, PLAIN, label='s', table=rows(client), settings=given)
    given['binning']['bins'] = 4

    records = batch.reduce(['1'])

    assert records['1'].request.params['binning'] == {'bins': 3}


def test_a_reduction_compares_no_row_and_a_state_resolves_the_settings_once(
    client: Client, sample: Measure
) -> None:
    counting = CountingClient(client)
    sample(1, 'water')
    batch = Batch(counting, PLAIN, label='scan', table=rows(client))  # type: ignore[arg-type]
    client.wait(batch.reduce(['1']))
    batch.settings = OTHER_SETTINGS

    client.wait(batch.reduce(['1']))
    batch.state(compare=False)
    reduced = counting.reads['resolve']
    batch.state()

    assert (reduced, counting.reads['resolve']) == (0, 1)


def test_the_settings_change_only_by_an_assignment_of_all_of_them(
    measured: Batch,
) -> None:
    given = {'binning': {'bins': 3}}
    measured.settings = given
    given['binning']['bins'] = 4
    measured.settings['binning']['bins'] = 5
    measured.template().params['binning']['bins'] = 7

    with pytest.raises(TypeError):
        measured.settings['binning'] = {'bins': 6}  # type: ignore[index]
    assert measured.settings == {'binning': {'bins': 3}}


def test_an_empty_cell_of_a_column_with_a_default_gets_it(measured: Batch) -> None:
    records = measured.reduce(['1'])

    assert records['1'].request.params['thickness'] == 1.0


def test_an_empty_cell_of_a_parameter_that_takes_none_gives_none(
    files: CorrectedFiles, sample: Measure
) -> None:
    sample(1, 'water')
    bind = [function_binding(OPTIONAL, lambda _: Outputs(total=1.0))]
    with local(proposal='p1', files=files, bind=bind) as client:
        table = pd.DataFrame({'sample': ['run:1'], 'transmission': [None]}, index=['1'])
        batch = Batch(client, OPTIONAL, label='s', table=table)

        assert batch.state()['1'].status == 'ready'
        records = batch.reduce(['1'])

    assert records['1'].request.params['transmission'] is None


def test_a_whole_number_is_an_int_in_the_request_where_all_columns_are_numbers(
    files: CorrectedFiles,
) -> None:
    bind = [function_binding(NUMBERS, lambda _: Outputs(total=1.0))]
    with local(proposal='p1', files=files, bind=bind) as client:
        table = pd.DataFrame({'bins': [3], 'scale': [0.5]}, index=['1'])
        records = Batch(client, NUMBERS, label='s', table=table).reduce(['1'])

    assert type(records['1'].request.params['bins']) is int


def test_a_list_in_a_cell_changes_only_by_put(strict: Client) -> None:
    table = pd.DataFrame({'sample': ['run:1'], 'counts': [[1]]}, index=['a'])
    batch = Batch(strict, STRICT, label='s', table=table)
    table.loc['a', 'counts'].append(2)
    frame = batch.table()
    frame.loc['a', 'counts'].append(3)

    assert batch.table().loc['a', 'counts'] == [1]
    batch.set_table(frame)
    frame.loc['a', 'counts'].append(4)
    assert batch.table().loc['a', 'counts'] == [1, 3]


def test_a_person_can_add_a_row_by_run_number(
    measured: Batch, client: Client, sample: Measure
) -> None:
    sample(7, 'water')
    edit(measured, 'mine', sample='run:7', transmission='run:1007')

    records = measured.reduce(['mine'])
    client.wait(records)

    assert client.output(records['mine'], 'total') == 7.0


def test_a_row_without_a_partner_is_skipped_by_ready_and_refused_when_picked(
    batch: Batch, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    sample(2, 'sds', partner=False)
    batch.set_table(rows(client))

    assert ready(batch.state()) == ['1']
    with pytest.raises(SubmitError) as error:
        batch.reduce(['2'])
    assert [(e.request_key, e.loc) for e in error.value.errors] == [
        ('2', ('transmission',))
    ]


def test_every_refused_row_is_named_and_none_is_submitted(
    measured: Batch, client: Client
) -> None:
    measured.set_table(measured.table().assign(thickness=[-1.0, -2.0]))

    with pytest.raises(SubmitError) as error:
        measured.reduce(['1', '2'])

    assert [(e.request_key, e.loc) for e in error.value.errors] == [
        ('1', ('thickness',)),
        ('2', ('thickness',)),
    ]
    assert client.records(label='scan') == []


@pytest.mark.parametrize('spec', [STAGED, PLAIN])
def test_a_reason_about_the_settings_is_named_once_without_a_key(
    client: Client, sample: Measure, spec: Any
) -> None:
    sample(1, 'water')
    sample(2, 'sds')
    batch = Batch(client, spec, label='s', table=rows(client), staged=spec is STAGED)

    batch.settings = {'binning': {'bins': 0}}

    with pytest.raises(SubmitError) as error:
        batch.reduce(['1', '2'])

    assert [(e.request_key, e.loc[0]) for e in error.value.errors] == [
        (None, 'binning')
    ]
    batch.close()


def test_a_binding_without_stages_works_with_staged_off(
    client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    batch = Batch(client, PLAIN, label='plain', table=rows(client))

    records = batch.reduce(['1'])

    assert client.wait(records) == {'1': 'completed'}


def test_the_stage_is_reused_while_the_settings_stay(
    measured: Batch, client: Client, staged_binding: StagedFunction
) -> None:
    client.wait(measured.reduce(['1']))
    client.wait(measured.reduce(['2']))

    assert len(staged_binding.staged) == 1


def test_changed_settings_open_a_new_stage(
    measured: Batch, client: Client, staged_binding: StagedFunction
) -> None:
    client.wait(measured.reduce(['1']))
    measured.settings = OTHER_SETTINGS
    client.wait(measured.reduce(['1']))

    assert len(staged_binding.staged) == 2


def test_a_row_refused_leaves_the_stage_alone(
    measured: Batch, client: Client, staged_binding: StagedFunction
) -> None:
    client.wait(measured.reduce(['1']))
    edit(measured, '2', thickness=-1.0)
    with pytest.raises(SubmitError):
        measured.reduce(['2'])
    edit(measured, '2', thickness=pd.NA)

    client.wait(measured.reduce(['2']))

    assert len(staged_binding.staged) == 1


def test_a_stage_that_failed_is_replaced_within_one_reduce(
    measured: Batch, client: Client, staged_binding: StagedFunction
) -> None:
    staged_binding.fail_next = True
    assert client.wait(measured.reduce(['1'])) == {'1': 'failed'}

    records = measured.reduce(['1', '2'])

    assert client.wait(records) == {'1': 'completed', '2': 'completed'}


def test_a_rerun_becomes_the_result(measured: Batch, client: Client) -> None:
    measured.settings = OTHER_SETTINGS
    client.wait(measured.reduce(['1']))
    measured.settings = SETTINGS
    again = measured.reduce(['1'])
    client.wait(again)

    assert measured.state()['1'].record == again['1']


def test_a_row_removed_and_added_again_has_its_result_again(
    measured: Batch, client: Client
) -> None:
    records = measured.reduce(['1'])
    client.wait(records)
    table = measured.table()
    measured.set_table(table.drop('1'))
    measured.set_table(table)

    assert measured.state()['1'].record == records['1']
    assert measured.state()['1'].status == 'done'


def test_an_edited_row_is_outdated_and_left_to_do(
    measured: Batch, client: Client
) -> None:
    client.wait(measured.reduce(['1', '2']))
    edit(measured, '1', thickness=2.0)

    assert left_to_reduce(measured.state()) == ['1']


def test_changed_settings_make_every_result_outdated(
    measured: Batch, client: Client
) -> None:
    client.wait(measured.reduce(['1', '2']))

    measured.settings = OTHER_SETTINGS

    states = measured.state()
    assert [states[k].outdated for k in ('1', '2')] == [True, True]
    assert with_changed_settings(states) == ['1', '2']
    assert left_to_reduce(states) == []


def test_changed_names_the_cell_and_the_setting_that_differ(
    measured: Batch, client: Client
) -> None:
    client.wait(measured.reduce(['1', '2']))
    edit(measured, '1', thickness=2.0)

    measured.settings = OTHER_SETTINGS

    changed = {k: s.changed for k, s in measured.state().items()}

    assert changed == {'1': {'binning', 'thickness'}, '2': {'binning'}}


def test_a_setting_made_a_column_changes_no_row(
    measured: Batch, client: Client
) -> None:
    measured.settings = {**SETTINGS, 'offset': {'value': 0.5}}
    client.wait(measured.reduce(['1', '2']))

    measured.make_column('offset')

    assert {k: s.changed for k, s in measured.state().items()} == {
        '1': set(),
        '2': set(),
    }


# A column and its setting


@pytest.mark.parametrize(
    ('cell', 'setting', 'value'),
    [
        (1.5, {'thickness': 2.0}, 1.5),
        (pd.NA, {'thickness': 2.0}, 2.0),
        (pd.NA, {}, 1.0),
    ],
    ids=['cell', 'empty cell, setting', 'empty cell, no setting: default'],
)
def test_a_row_takes_its_cell_else_the_setting_else_the_default(
    measured: Batch, cell: Any, setting: dict[str, Any], value: float
) -> None:
    measured.settings = {**SETTINGS, **setting}
    edit(measured, '1', thickness=cell)

    records = measured.reduce(['1'])

    assert records['1'].request.params['thickness'] == value


def test_a_row_takes_the_setting_of_a_parameter_that_is_no_column(
    measured: Batch,
) -> None:
    measured.settings = {**SETTINGS, 'thickness': 2.0}
    measured.set_table(measured.table().drop(columns='thickness'))

    records = measured.reduce(['1'])

    assert records['1'].request.params['thickness'] == 2.0


def test_make_column_adds_empty_cells_and_keeps_the_setting_for_every_row(
    measured: Batch,
) -> None:
    settings = {**SETTINGS, 'offset': {'value': 0.5}}
    measured.settings = settings

    measured.make_column('offset')
    edit(measured, 'mine', sample='run:1', transmission='run:1001')

    assert measured.table()['offset'].isna().all()
    assert measured.settings == settings
    assert {k: c['offset'] for k, c in measured.cells().items()} == {
        '1': {'value': 0.5},
        '2': {'value': 0.5},
        'mine': {'value': 0.5},
    }


@pytest.mark.parametrize('name', ['thickness', 'nope'])
def test_make_column_refuses_a_column_and_a_name_that_is_no_parameter(
    measured: Batch, name: str
) -> None:
    with pytest.raises(ValueError, match=f'{name} is not a setting'):
        measured.make_column(name)


@pytest.mark.parametrize(
    ('cells', 'key', 'setting'),
    [
        ((pd.NA, pd.NA), None, 2.0),
        ((2.0, pd.NA), None, 2.0),
        ((3.0, 3.0), None, 3.0),
        ((3.0, pd.NA), '1', 3.0),
        ((3.0, pd.NA), '2', 2.0),
    ],
    ids=[
        'all empty: the setting stays',
        'a cell equal to the setting',
        'every cell the same',
        'the cell of the row chosen',
        'the row chosen takes the setting',
    ],
)
def test_make_setting_gives_the_setting_the_value_that_the_row_takes(
    measured: Batch, cells: tuple[Any, Any], key: str | None, setting: float
) -> None:
    measured.settings = {**SETTINGS, 'thickness': 2.0}
    edit(measured, '1', thickness=cells[0])
    edit(measured, '2', thickness=cells[1])

    measured.make_setting('thickness', key)

    assert 'thickness' not in measured.parameter_columns
    assert measured.settings == {**SETTINGS, 'thickness': setting}


def test_make_setting_of_empty_cells_without_a_setting_leaves_the_default(
    measured: Batch,
) -> None:
    measured.make_setting('thickness')

    assert measured.settings == SETTINGS
    assert measured.reduce(['1'])['1'].request.params['thickness'] == 1.0


def test_make_setting_refuses_rows_that_take_different_values_without_a_row(
    measured: Batch,
) -> None:
    measured.settings = {**SETTINGS, 'thickness': 2.0}
    edit(measured, '1', thickness=3.0)

    assert measured.values('thickness') == {'1': 3.0, '2': 2.0}
    with pytest.raises(ValueError, match='the rows differ at thickness'):
        measured.make_setting('thickness')
    assert 'thickness' in measured.parameter_columns


@pytest.mark.parametrize('name', ['binning', 'run'], ids=['setting', 'note'])
@pytest.mark.parametrize('key', [None, '1'])
def test_make_setting_refuses_a_name_that_is_no_parameter_column(
    measured: Batch, name: str, key: str | None
) -> None:
    before = (measured.table(), dict(measured.settings), measured.revision)

    with pytest.raises(ValueError, match=f'^{name} is not a column of the batch$'):
        measured.make_setting(name, key)

    pd.testing.assert_frame_equal(measured.table(), before[0])
    assert (dict(measured.settings), measured.revision) == before[1:]


def test_make_setting_of_a_batch_without_rows_keeps_the_setting(
    batch: Batch,
) -> None:
    batch.settings = {'thickness': 2.0}

    batch.make_setting('thickness')

    assert batch.settings == {'thickness': 2.0}


FILLS = ('sample', 'transmission', 'offset')


@pytest.fixture
def from_source(client: Client, sample: Measure) -> Batch:
    """
    A batch of the rows of sample 1, whose source fills sample, transmission
    and offset, made with a setting at transmission, as definitions may hold.
    """
    sample(1, 'water')
    table = rows(client).assign(transmission=None, offset=None)
    stale = {'transmission': 'run:1001'}
    return Batch(
        client, PLAIN, label='s', table=table, settings=stale, source_columns=FILLS
    )


def test_a_column_that_the_source_fills_takes_its_value_from_the_cell_only(
    from_source: Batch,
) -> None:
    from_source.settings = {**from_source.settings, 'thickness': 3.0}
    (row,) = from_source.state().values()

    # The setting is never read, and offset waits although it has a default.
    assert from_source.settings == {'thickness': 3.0}
    assert row.status == 'incomplete'
    assert sorted(e.loc for e in row.problems) == [('offset',), ('transmission',)]
    assert from_source.values('transmission') == {'1': None}
    assert 'transmission' not in from_source.cells()['1']


def test_a_batch_from_a_source_refuses_a_setting_at_and_a_table_without_a_column(
    from_source: Batch,
) -> None:
    given = {'transmission': 'run:1001', 'thickness': 2.0}

    with pytest.raises(ValueError, match='the source of the batch fills transmission'):
        from_source.settings = given
    with pytest.raises(ValueError, match='fills transmission: a row is incomplete'):
        from_source.set_table(from_source.table().drop(columns='transmission'))
    with pytest.raises(ValueError, match='fills transmission: a row is incomplete'):
        from_source.make_setting('transmission')
    from_source.settings = from_source.settable(given)

    assert from_source.settings == {'thickness': 2.0}
    assert set(FILLS) <= set(from_source.parameter_columns)


def test_a_batch_is_not_made_on_a_table_without_a_column_of_its_fills(
    from_source: Batch,
) -> None:
    # As table.json without offset, whose parameter has a default, would be.
    table = from_source.table().drop(columns='offset')

    with pytest.raises(ValueError, match='fills offset: a row is incomplete'):
        Batch(from_source.client, PLAIN, label='s', table=table, source_columns=FILLS)


def test_a_changed_setting_at_a_column_reads_settings_changed_for_rows_that_take_it(
    measured: Batch, client: Client
) -> None:
    measured.settings = {**SETTINGS, 'thickness': 2.0}
    edit(measured, '2', thickness=1.5)
    client.wait(measured.reduce(['1', '2']))

    measured.settings = {**SETTINGS, 'thickness': 3.0}
    states = measured.state()
    edit(measured, '2', thickness=2.5)
    edited = measured.state()['2']

    assert (states['1'].status, states['1'].changed) == (
        'settings changed',
        {'thickness'},
    )
    assert (states['2'].status, states['2'].changed) == ('done', set())
    assert (edited.status, edited.changed) == ('outdated', {'thickness'})


def test_a_second_batch_on_the_label_reads_the_same_changed(
    measured: Batch, client: Client
) -> None:
    client.wait(measured.reduce(['1', '2']))
    second = Batch(client, STAGED, label='scan', table=measured.table())
    for batch in (measured, second):
        edit(batch, '1', thickness=2.0)

    def changed(batch: Batch) -> dict[str, frozenset[str]]:
        return {k: s.changed for k, s in batch.state().items()}

    assert changed(second) == changed(measured) == {'1': {'thickness'}, '2': set()}


def test_a_default_written_out_and_a_file_by_name_do_not_change_a_row(
    measured: Batch, client: Client
) -> None:
    measured.settings = {}
    client.wait(measured.reduce(['1', '2']))
    edit(measured, '1', sample='run:1', transmission='run:1001', thickness=1.0)
    measured.settings = {'binning': {'bins': 2}, 'offset': None}

    states = measured.state()

    assert {k: s.changed for k, s in states.items()} == {'1': set(), '2': set()}
    assert left_to_reduce(states) == []


def test_a_cell_emptied_is_one_problem(measured: Batch, client: Client) -> None:
    client.wait(measured.reduce(['1']))
    edit(measured, '1', sample=pd.NA)

    state = measured.state()['1']

    assert [(e.loc, e.msg) for e in state.problems] == [(('sample',), 'empty')]


def test_a_file_named_before_it_is_measured_is_a_problem_until_it_is(
    measured: Batch, sample: Measure
) -> None:
    edit(measured, '9', sample='run:9', transmission='run:1001')

    before = measured.state()
    sample(9, 'oil')
    after = measured.state()

    assert [e.loc for e in before['9'].problems] == [('sample',)]
    assert ready(before) == ['1', '2']
    assert measured.state(compare=False)['9'].problems == ()
    assert (after['9'].problems, after['9'].status) == ((), 'ready')


def texts(states: dict[str, RowState]) -> dict[str, str]:
    return {key: s.status for key, s in states.items()}


@pytest.mark.parametrize('method', ['metadata', 'path'])
def test_a_file_the_source_cannot_read_is_a_problem_of_its_rows_until_it_can(
    measured: Batch, fake: FakeFiles, method: str
) -> None:
    with unreachable(fake, method, [FileRef(file='uuid:run-1')]):
        before = measured.state()
    after = measured.state()

    assert [(e.loc, e.msg) for e in before['1'].problems] == [
        (('sample',), 'file uuid:run-1 cannot be read: the mount is gone')
    ]
    assert texts(before) == {'1': 'incomplete', '2': 'ready'}
    assert texts(after) == {'1': 'ready', '2': 'ready'}


def test_while_no_file_resolves_a_result_stays_unless_its_cell_names_the_file_anew(
    measured: Batch, client: Client, fake: FakeFiles
) -> None:
    client.wait(measured.reduce(['1', '2']))
    edit(measured, '2', sample='run:2')  # as the record holds it, once resolved
    edit(measured, '3', sample='uuid:run-1', transmission='uuid:run-1001')

    with unreachable(fake, 'resolve'):
        states = measured.state()

    assert texts(states) == {'1': 'done', '2': 'incomplete', '3': 'incomplete'}


def test_while_the_table_of_files_is_broken_each_row_that_names_a_file_waits(
    folder: Path, folder_bindings: list[Binding], table: pd.DataFrame
) -> None:
    with (folder / 'files.csv').open() as f:
        listed = list(csv.DictReader(f))
    with open_folder(folder, folder_bindings, files=ArrivingFiles(folder)) as client:
        batch = Batch(client, PLAIN, label='s', table=table)
        write_files_table(folder, [*listed, {'file': 'gone.json', 'run': 9}])
        broken = batch.state()
        write_files_table(folder, listed)
        mended = batch.state()

    assert texts(broken) == {'a': 'incomplete', 'b': 'incomplete'}
    ((at, reason),) = [(e.loc, e.msg) for e in broken['a'].problems]
    assert at == ('sample',)
    assert reason.startswith('file run:1 cannot be read: ')
    assert reason.endswith('gone.json')
    assert texts(mended) == {'a': 'ready', 'b': 'ready'}


def _request_of(batch: Batch, key: str) -> Any:
    """The request of the row of ``key``, its empty thickness given."""
    cells = batch.table()[['sample', 'transmission']].to_dict('index')[key]
    params = {**as_request(batch.spec.params, cells), 'thickness': 1.0}
    return batch.template().fill(params=params)


def test_an_older_record_that_ends_later_is_not_the_result(
    measured: Batch, client: Client, gate: Gate
) -> None:
    gate.close()
    older = measured.reduce(['1'])['1']
    measured.state()
    newer = client.submit(_request_of(measured, '1'), label='scan', member='1')
    client.cancel([older])

    assert measured.state()['1'].record == newer
    gate.open()
    client.wait(newer)


def test_a_record_under_another_label_is_not_a_rows_result(
    measured: Batch, client: Client
) -> None:
    mine = measured.reduce(['1'])['1']
    client.wait(mine)
    measured.state()

    client.compute(_request_of(measured, '1'), label='other', member='1')

    assert measured.state()['1'].record == mine


def test_a_cell_refused_is_a_problem_of_its_row_and_not_changed(
    measured: Batch, client: Client
) -> None:
    client.wait(measured.reduce(['1', '2']))
    edit(measured, '1', sample='run:99')

    state = measured.state()['1']

    assert [e.loc for e in state.problems] == [('sample',)]
    assert state.changed == frozenset()
    assert state.status == 'incomplete'


class Changing:
    """
    A file source whose names change: a name in ``names`` resolves to the
    identity there, or to no file for ``None``.
    """

    def __init__(self, files: CorrectedFiles) -> None:
        self.files = files
        self.names: dict[FileRef, FileRef | None] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self.files, name)

    def resolve(self, name: FileRef) -> FileRef:
        if name not in self.names:
            return self.files.resolve(name)
        if (ref := self.names[name]) is None:
            raise KeyError(name)
        return ref

    def metadata(self, ref: FileRef) -> dict[str, Any]:
        return self.files.metadata(self._underlying(ref))

    def path(self, ref: FileRef) -> Path:
        return self.files.path(self._underlying(ref))

    def _underlying(self, ref: FileRef) -> FileRef:
        """The identity that ``files`` gives the file of ``ref``."""
        names = [n for n, r in self.names.items() if r == ref]
        return self.files.resolve(names[0]) if names else ref


@pytest.fixture
def bound_twice(
    files: CorrectedFiles, sample: Measure
) -> Iterator[tuple[Client, Changing]]:
    """A client that runs PLAIN and PLAIN_V2, of rows 1 and 2 reduced by PLAIN."""
    changing = Changing(files)
    bind = [
        function_binding(spec, lambda _: Outputs(total=1.0))
        for spec in (PLAIN, PLAIN_V2)
    ]
    sample(1, 'water')
    sample(2, 'sds')
    with local(proposal='p1', files=changing, bind=bind) as client:  # type: ignore[arg-type]
        old = Batch(client, PLAIN, label='s', table=rows(client))
        client.wait(old.reduce(['1', '2']))
        yield client, changing


@pytest.mark.parametrize('spec', [PLAIN, PLAIN_V2])
def test_settings_refused_are_raised_naming_no_row(
    bound_twice: tuple[Client, Changing], sample: Measure, spec: Any
) -> None:
    client, _ = bound_twice
    sample(3, 'oil')
    batch = Batch(client, spec, label='s', table=rows(client))

    batch.settings = {'binning': {'bins': 0}}

    with pytest.raises(SubmitError) as error:
        batch.state()

    assert [(e.request_key, e.loc[0]) for e in error.value.errors] == [
        (None, 'binning')
    ]
    assert batch.state(compare=False)['3'].problems == ()


def test_a_changed_setting_is_found_when_no_result_s_files_are_known(
    bound_twice: tuple[Client, Changing],
) -> None:
    client, changing = bound_twice
    batch = Batch(client, PLAIN, label='s', table=rows(client))
    changing.names |= dict.fromkeys(FileRef(file=f) for f in batch.table()['sample'])
    batch.settings = OTHER_SETTINGS

    changed = {k: s.changed for k, s in batch.state().items()}

    assert changed == {'1': {'binning'}, '2': {'binning'}}


@pytest.fixture
def strict(files: CorrectedFiles, sample: Measure) -> Iterator[Client]:
    """A client that runs STRICT, with sample run 1."""
    sample(1, 'water')
    bind = [function_binding(STRICT, lambda _: Outputs(total=1.0))]
    with local(proposal='p1', files=files, bind=bind) as client:
        yield client


def problems(batch: Batch) -> dict[str, list[tuple[str | int, ...]]]:
    return {k: [e.loc for e in s.problems] for k, s in batch.state().items()}


def strict_batch(client: Client, cells: dict[str, dict[str, Any]]) -> Batch:
    table = pd.DataFrame.from_dict(
        {
            k: {'sample': 'run:1', 'counts': None, 'numbers': None, **c}
            for k, c in cells.items()
        },
        orient='index',
    )
    return Batch(client, STRICT, label='s', table=table)


def test_a_value_kept_is_not_taken_for_an_equal_one_of_another_type(
    strict: Client,
) -> None:
    cached = strict_batch(strict, {'a': {'counts': [1]}})
    problems(cached)

    frame = cached.table()
    frame.at['a', 'counts'] = [True]
    cached.set_table(frame)

    assert problems(cached)['a'] != []
    assert problems(cached) == problems(strict_batch(strict, {'a': {'counts': [True]}}))


def test_a_value_kept_at_one_column_is_not_taken_at_another(strict: Client) -> None:
    cells = {'a': {'numbers': [True]}, 'b': {'counts': [True]}}
    cached = strict_batch(strict, {'a': cells['a']})
    problems(cached)

    frame = cached.table()
    frame.loc['b'] = pd.Series({'sample': 'run:1', 'counts': [True]}, dtype=object)
    cached.set_table(frame)

    assert problems(cached) == problems(strict_batch(strict, cells))
    assert problems(cached) == {'a': [], 'b': [('counts', 0)]}


def test_a_batch_reads_a_file_by_run_as_a_new_one_does_after_a_put(
    bound_twice: tuple[Client, Changing],
) -> None:
    client, changing = bound_twice

    def by_run() -> Batch:
        batch = Batch(client, PLAIN, label='s', table=rows(client))
        edit(batch, '1', sample='run:1')
        return batch

    def changed(batch: Batch) -> frozenset[str]:
        return batch.state()['1'].changed

    batch = by_run()
    before = changed(batch)
    changing.names[file(run=1)] = FileRef(file='pid:20.500.12269/water')
    kept = changed(batch)
    batch.set_table(batch.table())

    assert (before, kept) == (set(), set())
    assert changed(batch) == changed(by_run()) == {'sample'}


def test_a_row_naming_its_file_by_run_is_changed_only_once_its_cell_is(
    measured: Batch, client: Client
) -> None:
    edit(measured, '1', sample='run:1')
    client.wait(measured.reduce(['1']))

    first, again = (measured.state()['1'].changed for _ in range(2))
    edit(measured, '1', sample='run:2')

    assert (first, again) == (set(), set())
    assert measured.state()['1'].changed == {'sample'}


def test_a_result_of_another_spec_version_is_the_rows_result_and_outdated(
    bound_twice: tuple[Client, Changing],
) -> None:
    client, _ = bound_twice
    new = Batch(client, PLAIN_V2, label='s', table=rows(client))
    client.wait(new.reduce(['2']))

    states = new.state()

    assert [r.member for r in client.records(label='s')] == ['1', '2', '2']
    assert states['1'].record == client.records(label='s')[0]
    assert (states['1'].spec_changed, states['2'].spec_changed) == (True, False)
    assert left_to_reduce(states) == ['1']


class Redefined(BaseModel):
    """The params of ``PLAIN`` with another definition of its binning."""

    count: int = 2
    sample: SampleRunFile
    transmission: SampleRunFile
    thickness: float = 1.0


def test_a_result_of_a_spec_of_the_same_version_but_another_definition_is_outdated(
    files: CorrectedFiles, sample: Measure, tmp_path: Path
) -> None:
    redefined = PLAIN.model_copy(update={'params': Redefined})
    sample(1, 'water')
    kept = tmp_path / 'log'
    reduced = Workspace(
        proposal='p1',
        files=files,
        bind=[function_binding(PLAIN, lambda _: Outputs(total=1.0))],
        log=Log(kept),
    )
    with Client(reduced, submitter='anna', on_close=reduced.close) as client:
        old = Batch(client, PLAIN, label='s', table=rows(client))
        client.wait(old.reduce(['1']))
    again = Workspace(
        proposal='p1',
        files=files,
        bind=[function_binding(redefined, lambda _: Outputs(total=1.0))],
        log=Log(kept),
    )
    with Client(again, submitter='anna', on_close=again.close) as client:
        new = Batch(client, redefined, label='s', table=rows(client))

        state = new.state()['1']

        assert (state.record_status, state.spec_changed) == (Status.COMPLETED, True)
        assert state.status == 'outdated'


def test_the_state_of_chosen_rows_reads_only_the_records_that_changed(
    client: Client, sample: Measure
) -> None:
    counting = CountingClient(client)
    sample(1, 'water')
    sample(2, 'sds')
    batch = Batch(counting, STAGED, label='scan', table=rows(client))  # type: ignore[arg-type]
    client.wait(batch.reduce(['1', '2']))
    batch.state()
    counting.reads.clear()

    batch.state(['2'])

    assert counting.reads['records'] == 0


def test_a_row_running_is_not_submitted_twice(
    measured: Batch, client: Client, gate: Gate
) -> None:
    gate.close()
    first = measured.reduce(ready(measured.state()))

    assert ready(measured.state()) == []
    gate.open()
    client.wait(first)


def test_cancel_reaches_what_was_just_submitted(
    measured: Batch, client: Client, gate: Gate
) -> None:
    gate.close()
    records = measured.reduce(['1', '2'])

    measured.cancel()
    gate.open()

    assert client.wait(records) == {'1': 'cancelled', '2': 'cancelled'}
    assert left_to_reduce(measured.state()) == ['1', '2']


def test_cancel_leaves_other_rows_when_given_keys(
    measured: Batch, client: Client, gate: Gate
) -> None:
    gate.close()
    records = measured.reduce(['1', '2'])

    measured.cancel(['1'])

    assert client.status(records['1']) is Status.CANCELLED
    assert client.status(records['2']) is Status.PENDING
    gate.open()


@pytest.fixture
def chain(client: Client, sample: Measure) -> tuple[Batch, Batch]:
    """A batch that finds an offset per sample, and one that reads it per row."""
    sample(1, 'water')
    sample(2, 'sds')
    find = Batch(client, FIND, label='find', table=rows_of(client, SAMPLES))
    reduce = Batch(
        client, PLAIN, label='reduce', table=rows(client).assign(offset=None)
    )
    return find, reduce


def test_a_row_can_read_the_record_of_its_row_in_another_batch(
    chain: tuple[Batch, Batch], client: Client
) -> None:
    find, reduce = chain
    found = find.reduce(['1', '2'])  # the second batch need not wait
    for key, record in found.items():
        edit(reduce, key, offset=cell_of(record.ref('offset')))

    records = reduce.reduce(['1', '2'])
    client.wait(records)

    assert client.output(records['2'], 'total') == 4.0
    assert records['2'].request.params['offset'] == found['2'].ref('offset')


def test_a_cell_reading_a_failed_record_stops_only_its_row(
    chain: tuple[Batch, Batch], client: Client
) -> None:
    _, reduce = chain
    reduce.settings = {'binning': {'bins': 1}}
    failed = reduce.reduce(['1'])
    client.wait(failed)
    edit(reduce, '2', offset=cell_of(failed['1'].ref('total')))

    assert ready(reduce.state()) == ['1']


def test_a_cell_reading_a_cancelled_record_stops_only_its_row(
    chain: tuple[Batch, Batch], client: Client, gate: Gate
) -> None:
    _, reduce = chain
    gate.close()
    cancelled = reduce.reduce(['1'])
    reduce.cancel(['1'])
    gate.open()
    client.wait(cancelled)
    edit(reduce, '2', offset=cell_of(cancelled['1'].ref('total')))

    states = reduce.state()

    assert ready(states) == ['1']
    assert [e.msg for e in states['2'].problems] == ['the record it reads is cancelled']


# Clearing outputs


def test_a_cleared_row_shows_so_and_a_reduction_gives_it_a_new_record(
    measured: Batch, client: Client
) -> None:
    first = measured.reduce(['1', '2'])
    client.wait(first)

    measured.clear(['1'])
    states = measured.state()

    assert {k: s.status for k, s in states.items()} == {'1': 'cleared', '2': 'done'}
    assert left_to_reduce(states) == ['1']
    again = measured.reduce(['1'])
    client.wait(again)
    assert again['1'].id != first['1'].id
    assert measured.state()['1'].status == 'done'
    assert client.cleared([first['1'], again['1']]) == [True, False]


def test_a_cell_reading_a_cleared_record_says_to_reduce_its_batch_and_then_to_set_it(
    chain: tuple[Batch, Batch], client: Client
) -> None:
    find, reduce = chain
    found = find.reduce(['1'])
    client.wait(found)
    edit(reduce, '1', offset=cell_of(found['1'].ref('offset')))

    find.clear()
    before = [str(e) for e in reduce.state()['1'].problems]
    client.wait(find.reduce(['1']))
    after = [str(e) for e in reduce.state()['1'].problems]

    assert before == [
        '1: offset: its outputs are cleared; reduce the batch find again, then '
        'set offset to the new result'
    ]
    assert after == [
        '1: offset: its outputs are cleared; the batch find has a newer result, '
        'set offset to it'
    ]


def test_a_done_row_stays_done_when_a_record_it_read_is_cleared(
    chain: tuple[Batch, Batch], client: Client
) -> None:
    find, reduce = chain
    found = find.reduce(['1'])
    client.wait(found)
    edit(reduce, '1', offset=cell_of(found['1'].ref('offset')))
    client.wait(reduce.reduce(['1']))

    find.clear()

    assert reduce.state()['1'].status == 'done'
    with pytest.raises(SubmitError, match='offset: its outputs are cleared'):
        reduce.reduce(['1'])


def test_a_setting_reading_a_cleared_record_is_one_problem_of_the_settings(
    chain: tuple[Batch, Batch], measured: Batch, client: Client
) -> None:
    find, _ = chain
    found = find.reduce(['1'])
    client.wait(found)
    measured.settings = {**SETTINGS, 'offset': found['1'].ref('offset')}
    client.wait(measured.reduce(['1']))

    find.clear()
    states, problems = measured.shown()

    problem = (
        'offset: its outputs are cleared; reduce the batch find again, then set '
        'offset to the new result'
    )
    assert {k: s.status for k, s in states.items()} == {'1': 'done', '2': 'ready'}
    assert [str(e) for e in problems] == [problem]
    with pytest.raises(SubmitError) as refused:
        measured.reduce(['2'])
    assert [(e.request_key, str(e)) for e in refused.value.errors] == [(None, problem)]


def test_a_batch_with_a_running_row_clears_nothing_and_names_the_row(
    measured: Batch, client: Client, gate: Gate
) -> None:
    done = measured.reduce(['2'])
    client.wait(done)
    gate.close()
    running = measured.reduce(['1'])

    with pytest.raises(ValueError, match=r'^row 1 of scan is still running$'):
        measured.clear()
    gate.open()
    client.wait(running)
    assert client.cleared(done['2']) is False


def test_a_clear_refused_for_a_reader_in_another_batch_names_both_rows(
    chain: tuple[Batch, Batch], client: Client, gate: Gate
) -> None:
    find, reduce = chain
    found = find.reduce(['1'])
    client.wait(found)
    edit(reduce, '1', offset=cell_of(found['1'].ref('offset')))
    gate.close()
    running = reduce.reduce(['1'])

    with pytest.raises(
        ValueError,
        match=r'^row 1 of find is read by row 1 of reduce, which is still running$',
    ):
        find.clear()
    gate.open()
    client.wait(running)


def test_a_staged_batch_whose_submit_is_rejected_holds_no_record_it_reads(
    chain: tuple[Batch, Batch], measured: Batch, client: Client
) -> None:
    find, _ = chain
    found = find.reduce(['1'])
    client.wait(found)
    measured.settings = {**SETTINGS, 'offset': found['1'].ref('offset')}
    edit(measured, '2', sample='uuid:run-99')  # no such file

    with pytest.raises(SubmitError):
        measured.reduce(['1', '2'])
    find.clear()

    assert client.cleared(found['1']) is True


def test_a_clear_of_a_batch_takes_older_records_and_members_no_longer_rows(
    measured: Batch, client: Client
) -> None:
    first = measured.reduce(['1', '2'])
    client.wait(first)
    again = measured.reduce(['1'])
    client.wait(again)
    measured.set_table(measured.table().drop(index='2'))

    measured.clear()

    assert client.cleared([first['1'], again['1'], first['2']]) == [True] * 3


def test_a_clear_of_rows_refuses_a_key_that_is_no_row(
    measured: Batch, client: Client
) -> None:
    records = measured.reduce(['1', '2'])
    client.wait(records)

    with pytest.raises(KeyError, match='the batch has no row nope'):
        measured.clear(['1', 'nope'])
    assert client.cleared(list(records.values())) == [False, False]


def test_an_unknown_record_in_a_cell_touches_only_its_row(
    chain: tuple[Batch, Batch], client: Client
) -> None:
    _, reduce = chain
    reduce.settings = {'binning': {'bins': 1}}
    failed = reduce.reduce(['1'])
    client.wait(failed)
    edit(reduce, '1', offset='nope.x')
    edit(reduce, '2', offset=cell_of(failed['1'].ref('total')))

    whole, chosen = reduce.state()['2'], reduce.state(['2'])['2']

    assert [e.msg for e in whole.problems] == ['the record it reads is failed']
    assert chosen.problems == whole.problems


def test_a_record_of_another_submitter_is_a_rows_result(
    measured: Batch, client: Client, other: Client
) -> None:
    client.wait(measured.reduce(['1']))

    theirs = other.compute(_request_of(measured, '1'), label='scan', member='1')

    assert measured.state()['1'].record == theirs


def test_persisting_goes_with_the_batch(files: CorrectedFiles, sample: Measure) -> None:
    store = FakeStore()
    binding = function_binding(PLAIN, lambda _: Outputs(total=1.0))
    sample(1, 'water')
    with local(proposal='p1', files=files, bind=[binding], store=store) as client:
        batch = Batch(client, PLAIN, label='s', table=rows(client), persist=True)
        records = batch.reduce(['1'])
        client.wait(records)

        assert (records['1'].id, 'total') in store


def test_a_row_running_is_refused_when_picked(
    measured: Batch, client: Client, gate: Gate
) -> None:
    gate.close()
    first = measured.reduce(['1'])

    with pytest.raises(SubmitError, match='1: still running'):
        measured.reduce(['1'])
    gate.open()
    client.wait(first)
    assert len(client.records(label='scan')) == 1


def test_cancel_reaches_a_row_removed_since(
    measured: Batch, client: Client, gate: Gate
) -> None:
    gate.close()
    records = measured.reduce(['1'])
    measured.set_table(measured.table().drop('1'))

    measured.cancel()
    gate.open()

    assert client.wait(records) == {'1': 'cancelled'}


# The revision


def _shown(batch: Batch) -> tuple[Any, ...]:
    """What the revision stands for."""
    return data_of(batch.table()), batch.template(), batch.shown()


@pytest.mark.parametrize(
    'change',
    [
        lambda b: edit(b, '1', thickness=2.0),
        lambda b: setattr(b, 'settings', OTHER_SETTINGS),
        lambda b: b.make_column('binning'),
        lambda b: b.make_setting('thickness'),
    ],
    ids=['put', 'settings', 'make_column', 'make_setting'],
)
def test_the_version_moves_at_each_change_of_the_table_or_the_template(
    measured: Batch, change: Any
) -> None:
    before = measured.revision

    change(measured)

    assert measured.revision != before


@pytest.mark.parametrize(
    'read',
    [
        lambda b: b.set_table(b.table()),
        lambda b: b.table(),
        lambda b: b.template(),
        lambda b: b.settings,
        lambda b: b.cells(),
        lambda b: b.values('sample'),
        lambda b: b.state(),
        lambda b: b.state(compare=False),
        lambda b: b.shown(['1']),
    ],
    ids=[
        'put an equal table',
        'frame',
        'template',
        'settings',
        'cells',
        'values',
        'state',
        'state not compared',
        'shown',
    ],
)
def test_the_revision_stays_at_a_read_and_at_set_table_of_an_equal_table(
    measured: Batch, client: Client, read: Any
) -> None:
    client.wait(measured.reduce(['1']))
    before = measured.revision

    read(measured)

    assert measured.revision == before


def test_a_row_reduced_reads_the_same_until_it_finishes_and_the_version_stays(
    measured: Batch, client: Client, gate: Gate
) -> None:
    gate.close()
    records = measured.reduce(['1'])
    revision, running = measured.revision, _shown(measured)

    assert _shown(measured) == running
    gate.open()
    client.wait(records)

    assert measured.shown()[0]['1'].record_status is Status.COMPLETED
    assert measured.revision == revision


def test_a_record_that_a_cell_reads_is_read_again_when_it_finishes(
    measured: Batch, client: Client, gate: Gate
) -> None:
    upstream = Batch(client, PLAIN, label='up', table=measured.table())
    upstream.settings = {'binning': {'bins': 1}}  # the toy fails with one bin
    gate.close()
    found = upstream.reduce(['1'])
    edit(measured, '2', offset=cell_of(found['1'].ref('total')))
    running = _shown(measured)

    assert _shown(measured) == running
    gate.open()
    client.wait(found)

    problems = measured.shown()[0]['2'].problems
    assert [e.msg for e in problems] == ['the record it reads is failed']


# The files that a batch names


class Background(BaseModel):
    run: RunFile | None = None


class NamingParams(BaseModel):
    sample: RunFile
    runs: list[RunFile] = Field(default_factory=list)
    can: RunFile | None = None
    background: Background = Field(default_factory=Background)
    offset: Offset | Referenced[Offset] | None = None


NAMING = WorkflowSpec(
    name='naming',
    version=1,
    title='Naming',
    description='A toy that names files in cells, lists, settings and groups.',
    params=NamingParams,
    outputs=Outputs,
)


@pytest.fixture
def naming(files: CorrectedFiles, fake: FakeFiles) -> Iterator[Client]:
    """A client that runs NAMING, with runs 1 to 5, identified as ``uuid:run-N``."""
    for run in range(1, 6):
        fake.measure(run, float(run))
    bind = [function_binding(NAMING, lambda _: Outputs(total=1.0))]
    with local(proposal='p1', files=files, bind=bind) as client:
        yield client


def identities(*numbers: int) -> set[FileRef]:
    return {FileRef(file=f'uuid:run-{n}') for n in numbers}


def naming_batch(
    client: Client, cells: dict[str, dict[str, Any]], **settings: Any
) -> Batch:
    table = pd.DataFrame.from_dict(
        {k: {'runs': None, 'offset': None, **c} for k, c in cells.items()},
        orient='index',
    )
    return Batch(client, NAMING, label='n', table=table, settings=settings)


def test_a_batch_names_the_files_of_its_cells_at_any_depth_by_identity(
    naming: Client,
) -> None:
    batch = naming_batch(
        naming,
        {
            'a': {'sample': 'run:1', 'runs': ['uuid:run-2', 'run:3']},
            'b': {'sample': 'uuid:run-4', 'offset': 'r1.offset'},  # runs empty
        },
    )

    assert batch.files() == identities(1, 2, 3, 4)


def test_a_batch_names_the_files_of_its_settings_at_any_depth(naming: Client) -> None:
    batch = naming_batch(
        naming,
        {'a': {'sample': 'run:1'}},
        can=file(run=2),
        background={'run': file(run=3)},
    )

    assert batch.files() == identities(1, 2, 3)


def test_a_value_refused_names_no_file_and_hides_no_other(naming: Client) -> None:
    batch = naming_batch(
        naming, {'a': {'sample': 'run:1', 'runs': ['run:99']}}, can=file(run=98)
    )

    assert batch.files() == identities(1)


def test_an_element_of_a_list_not_listed_hides_no_other_element(
    naming: Client,
) -> None:
    runs = ['run:2', 'run:99', 'uuid:run-3']
    batch = naming_batch(naming, {'a': {'sample': 'run:1', 'runs': runs}})

    assert batch.files() == identities(1, 2, 3)
