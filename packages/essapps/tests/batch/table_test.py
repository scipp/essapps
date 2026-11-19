# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The rules of a table of rows, on plain values; the table as a frame."""

import re
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any, Literal

import pandas as pd
import pytest
from pydantic import BaseModel
from pydantic_core import from_json, to_json

from ess.apps import SubmitError
from ess.apps.batch import EMPTY, Row, RowTable, Scope
from ess.apps.batch.table import (
    KEY,
    as_request,
    cell_of,
    data_of,
    dtypes,
    frame_of,
    typed,
)
from ess.spec import FileRef, OutputRef, Referenced, RunFile
from ess.spec.request_types import request_model

from . import toys


def cells(table: RowTable) -> dict[str, dict[str, object]]:
    return {row.key: dict(row.values) for row in table}


@pytest.fixture
def table() -> RowTable:
    return RowTable().rescan([Row('a', {'x': 1, 'y': 2}), Row('b', {'x': 3})])


def test_a_rescan_replaces_what_the_last_scan_gave(table: RowTable) -> None:
    rescanned = table.rescan([Row('a', {'x': 9}), Row('c', {'x': 5})])

    assert cells(rescanned) == {'a': {'x': 9}, 'c': {'x': 5}}


def test_a_rescan_keeps_an_edit_and_takes_the_other_cells(table: RowTable) -> None:
    edited = table.edit('a', {'x': 10})

    assert cells(edited.rescan([Row('a', {'x': 99, 'y': 98})])) == {
        'a': {'x': 10, 'y': 98}
    }


def test_a_rescan_keeps_a_cell_that_was_cleared(table: RowTable) -> None:
    cleared = table.edit('a', {'y': EMPTY})

    assert cells(cleared) == {'a': {'x': 1}, 'b': {'x': 3}}
    assert cells(cleared.rescan([Row('a', {'y': 7})])) == {'a': {}}


def test_a_rescan_keeps_a_row_removed_out(table: RowTable) -> None:
    removed = table.remove('b')

    assert 'b' not in removed.rescan([Row('a'), Row('b', {'x': 3})])


def test_a_rescan_keeps_a_row_added_and_it_wins_over_the_scan(table: RowTable) -> None:
    added = table.add(Row('c', {'x': 7}))

    rescanned = added.rescan([Row('c', {'x': 0})])

    assert cells(rescanned) == {'c': {'x': 7}}


def test_an_edited_row_the_scan_no_longer_gives_goes_and_comes_back(
    table: RowTable,
) -> None:
    edited = table.edit('b', {'x': 30})

    gone = edited.rescan([Row('a')])
    back = gone.rescan([Row('a'), Row('b', {'x': 3})])

    assert 'b' not in gone
    assert back['b'].values == {'x': 30}


def test_rows_keep_the_order_of_the_scan_then_those_added(table: RowTable) -> None:
    added = table.add(Row('z')).rescan([Row('c'), Row('a')])

    assert [row.key for row in added] == ['c', 'a', 'z']


def test_reset_forgets_the_edits_and_the_removal(table: RowTable) -> None:
    edited = table.edit('a', {'x': 10}).remove('b')

    reset = edited.reset('a').reset('b')

    assert cells(reset) == cells(table)


def test_reset_of_a_cell_forgets_its_edit_alone(table: RowTable) -> None:
    edited = table.edit('a', {'x': 10, 'y': 20})

    reset = edited.reset('a', 'x')

    assert reset['a'].values == {'x': 1, 'y': 20}
    assert reset.reset('a', 'y').edits == {}


def test_an_edit_of_several_rows_gives_the_table_of_one_edit_after_another(
    table: RowTable,
) -> None:
    table = table.add(Row('c', {'x': 7})).edit('a', {'y': 5})

    many = table.edit_each(['a', 'b', 'c'], {'x': EMPTY, 'z': 1})

    one_by_one = table
    for key in 'abc':
        one_by_one = one_by_one.edit(key, {'x': EMPTY, 'z': 1})
    assert many == one_by_one
    assert cells(many) == {
        'a': {'y': 5, 'z': 1},
        'b': {'z': 1},
        'c': {'z': 1},
    }


def test_an_edit_of_several_rows_with_one_missing_edits_none(
    table: RowTable,
) -> None:
    with pytest.raises(KeyError, match='nope'):
        table.edit_each(['a', 'nope'], {'x': 1})

    assert table.edits == {}


def test_a_removal_of_several_rows_gives_the_table_of_one_after_another(
    table: RowTable,
) -> None:
    table = table.add(Row('c', {'x': 7})).edit('a', {'x': 10}).edit('c', {'x': 8})

    many = table.remove_each(['a', 'c'])

    assert many == table.remove('a').remove('c')
    assert cells(many) == {'b': {'x': 3}}
    assert (many.removed, many.edits) == ({'a'}, {})
    assert cells(many.reset_each(['a'])) == {'a': {'x': 1, 'y': 2}, 'b': {'x': 3}}


def test_a_removal_of_several_rows_with_one_missing_removes_none(
    table: RowTable,
) -> None:
    with pytest.raises(KeyError, match='nope'):
        table.remove_each(['a', 'nope'])

    assert table.removed == frozenset()


def test_a_reset_of_several_rows_forgets_what_one_after_another_does(
    table: RowTable,
) -> None:
    edited = table.edit('a', {'x': 10, 'y': 20}).edit('b', {'x': 30}).remove('b')

    assert edited.reset_each(['a', 'b']) == edited.reset('a').reset('b')
    assert cells(edited.reset_each(['a', 'b'])) == cells(table)
    assert edited.reset_each(['a', 'b'], 'x') == edited.reset('a', 'x').reset('b', 'x')
    assert edited.reset_each(['a', 'b'], 'x').edits == {'a': {'y': 20}}
    assert edited.reset_each(['a', 'b'], 'x').removed == {'b'}
    assert edited.reset_each([]) == edited


def test_removing_a_row_added_forgets_it(table: RowTable) -> None:
    added = table.add(Row('c', {'x': 7})).edit('c', {'x': 8})

    removed = added.remove('c').add(Row('c', {'x': 1}))

    assert removed['c'].values == {'x': 1}


def test_a_key_given_twice_by_a_scan_is_refused_by_key() -> None:
    with pytest.raises(SubmitError) as error:
        RowTable().rescan([Row('a'), Row('a')])

    assert [e.entry for e in error.value.errors] == ['a']


def test_adding_a_key_that_is_there_is_refused(table: RowTable) -> None:
    with pytest.raises(SubmitError, match='a: a row has this key'):
        table.add(Row('a'))


def test_editing_a_row_that_is_not_there_raises(table: RowTable) -> None:
    with pytest.raises(KeyError):
        table.edit('nope', {'x': 1})


def test_a_key_must_be_a_string() -> None:
    with pytest.raises(TypeError, match='60395'):
        Row(60395)  # type: ignore[arg-type]


def test_a_table_is_not_changed_by_an_operation(table: RowTable) -> None:
    table.edit('a', {'x': 10}).remove('b').add(Row('c'))

    assert cells(table) == {'a': {'x': 1, 'y': 2}, 'b': {'x': 3}}


def test_removing_a_row_added_over_a_scanned_one_removes_both(table: RowTable) -> None:
    added = table.rescan([Row('c', {'x': 2})]).add(Row('d')).remove('c')
    over = RowTable().add(Row('a', {'x': 1})).rescan([Row('a', {'x': 2})])

    assert 'c' not in added
    assert 'a' not in over.remove('a')


def test_a_row_added_takes_no_edit_left_from_a_row_gone() -> None:
    orphan = RowTable().rescan([Row('a', {'x': 1})]).edit('a', {'x': 9}).rescan([])

    assert orphan.add(Row('a', {'x': 5, 'y': 7}))['a'].values == {'x': 5, 'y': 7}


def test_a_row_put_takes_no_edit_left_from_a_row_gone() -> None:
    orphan = RowTable().rescan([Row('a', {'x': 1})]).edit('a', {'x': 9}).rescan([])

    put = orphan.put([Row('a', {'x': 5, 'y': 7})], columns=('x', 'y'))

    assert put['a'].values == {'x': 5, 'y': 7}
    assert put.edits == {}


def test_a_row_added_stands_where_the_added_rows_do_when_the_scan_gives_its_key(
    table: RowTable,
) -> None:
    added = table.add(Row('c', {'x': 7}, {'name': 'by hand'}))

    rescanned = added.rescan([Row('c', {'x': 0}, {'name': 'scan'}), Row('a', {'x': 1})])

    assert [row.key for row in rescanned] == ['a', 'c']
    assert rescanned['c'].info == {'name': 'by hand'}
    assert len(rescanned) == 2


def test_a_row_given_with_empty_has_that_cell_empty() -> None:
    t = RowTable().rescan([Row('a', {'x': EMPTY, 'y': 1})]).add(Row('b', {'x': EMPTY}))

    assert t['a'].values == {'y': 1}
    assert t['b'].values == {}


def test_rows_put_show_their_cells_and_a_cell_that_was_the_same_follows_the_scan(
    table: RowTable,
) -> None:
    put = table.edit('a', {'y': 5}).put(
        [Row('a', {'x': 1}), Row('c', {'x': 4}, {'name': 'new'})], columns=('x', 'y')
    )

    assert cells(put) == {'a': {'x': 1}, 'b': {'x': 3}, 'c': {'x': 4}}
    assert put['c'].info == {'name': 'new'}
    assert cells(put.rescan([Row('a', {'x': 8, 'y': 9})])) == {
        'a': {'x': 8},
        'c': {'x': 4},
    }


def test_rows_put_with_a_key_twice_are_refused(table: RowTable) -> None:
    with pytest.raises(SubmitError, match='c'):
        table.put([Row('c'), Row('c')], columns=('x',))


def test_a_row_put_that_was_removed_is_brought_back_with_its_cells(
    table: RowTable,
) -> None:
    put = table.remove('a').put([Row('a', {'x': 1, 'y': 5})], columns=('x', 'y'))

    assert cells(put) == {'a': {'x': 1, 'y': 5}, 'b': {'x': 3}}
    assert (put.removed, put.added, dict(put.edits)) == (
        frozenset(),
        (),
        {'a': {'y': 5}},
    )


# The scope


def indexed(*indices: int) -> list[Row]:
    """A row per index, keyed by it."""
    return [Row(str(i), {'x': i}, index=i) for i in indices]


def held(scope: Scope, rows: list[Row]) -> list[str]:
    return [row.key for row in RowTable(scope=scope).rescan(rows)]


def test_a_slice_holds_the_rows_from_start_up_to_stop_excluded() -> None:
    assert held(Scope(start=2, stop=4), indexed(1, 2, 3, 4, 5)) == ['2', '3']


def test_an_open_end_holds_every_row_beyond_the_other() -> None:
    rows = indexed(1, 2, 3)

    assert held(Scope(start=2), rows) == ['2', '3']
    assert held(Scope(stop=2), rows) == ['1']


def test_a_slice_needs_no_row_at_its_ends() -> None:
    assert held(Scope(start=2, stop=6), indexed(1, 3, 5, 7)) == ['3', '5']


def test_two_slices_that_meet_share_no_row_and_leave_none_out() -> None:
    rows = indexed(1, 2, 3, 4)

    assert held(Scope(stop=3), rows) + held(Scope(start=3), rows) == [
        '1',
        '2',
        '3',
        '4',
    ]


def test_a_row_without_an_index_is_held_only_by_a_scope_without_ends() -> None:
    rows = [*indexed(1), Row('none')]

    assert held(Scope(), rows) == ['1', 'none']
    assert held(Scope(start=0), rows) == ['1']


@pytest.mark.parametrize('end', [datetime(2026, 11, 18, tzinfo=UTC), True])
def test_an_end_that_is_not_an_int_a_float_or_a_str_is_refused(end: object) -> None:
    with pytest.raises(TypeError, match=re.escape(repr(end))):
        Scope(start=end)  # type: ignore[arg-type]


def test_a_stop_equal_to_the_start_holds_no_row() -> None:
    assert held(Scope(start=2, stop=2), indexed(1, 2, 3)) == []


@pytest.fixture
def scoped() -> RowTable:
    """Rows 1 to 4, of which the scope holds 2 and 3."""
    return RowTable(scope=Scope(start=2, stop=4)).rescan(indexed(1, 2, 3, 4))


def test_a_row_added_by_hand_is_shown_whatever_the_scope(scoped: RowTable) -> None:
    added = scoped.add(Row('mine', {'x': 0}))

    assert [row.key for row in added] == ['2', '3', 'mine']


def test_an_edit_of_a_row_outside_the_scope_is_kept_and_used_when_it_widens(
    scoped: RowTable,
) -> None:
    edited = scoped.edit('3', {'x': 30})

    narrowed = replace(edited, scope=Scope(start=2, stop=3))
    widened = replace(narrowed, scope=Scope())

    assert '3' not in narrowed
    assert narrowed.edits == {'3': {'x': 30}}
    assert widened['3'].values == {'x': 30}


def test_a_table_put_with_a_row_the_scan_has_outside_the_scope_is_refused(
    scoped: RowTable,
) -> None:
    with pytest.raises(SubmitError) as refused:
        scoped.put([Row('2', {'x': 20}), Row('4', {'x': 40})], columns=('x',))

    assert [(e.entry, e.msg) for e in refused.value.errors] == [
        ('4', 'the source makes this row outside the scope')
    ]


def test_a_table_put_sets_a_row_added_by_hand_whose_key_the_scan_gives_outside(
    scoped: RowTable,
) -> None:
    added = RowTable(scope=scoped.scope).add(Row('1', {'x': 0})).rescan(scoped.scan)

    put = added.put([Row('1', {'x': 10})], columns=('x',))

    assert put['1'].values == {'x': 10}


def test_the_rows_removed_in_scope_are_those_a_person_sees_removed(
    scoped: RowTable,
) -> None:
    removed = replace(scoped, scope=Scope()).remove_each(['1', '2'])

    assert replace(removed, scope=scoped.scope).removed_in_scope == {'2'}


def test_a_row_added_by_hand_with_the_key_of_a_row_outside_the_scope_is_refused(
    scoped: RowTable,
) -> None:
    with pytest.raises(SubmitError, match='outside the scope'):
        scoped.add(Row('1'))


# The table as a frame


class Colour(Enum):
    RED = 'red'
    BLUE = 'blue'


class Kinds(BaseModel):
    count: int = 1
    flag: bool = False
    word: str = ''
    choice: Literal['a', 'b'] = 'a'
    level: Literal[1, 2] = 1
    when: datetime | None = None
    day: date | None = None
    colour: Colour = Colour.RED
    scale: float | None = None
    either: int | str = 0
    centre: Referenced[toys.Offset] | None = None
    source: RunFile | Referenced[toys.Offset] | None = None


def test_a_column_has_the_dtype_of_its_parameter() -> None:
    assert dtypes(Kinds) == {
        'count': 'Int64',
        'flag': 'boolean',
        'word': 'string',
        'choice': 'string',
        'level': 'object',
        'when': 'string',
        'day': 'string',
        'colour': 'string',
        'scale': 'Float64',
        'either': 'object',
        'centre': 'string',
        'source': 'string',
    }


def test_the_columns_of_the_toy_have_the_dtypes_of_their_parameters() -> None:
    assert dtypes(toys.Params) == {
        'binning': 'object',
        'sample': 'string',
        'transmission': 'string',
        'thickness': 'Float64',
        'offset': 'object',
    }
    assert dtypes(toys.SeriesParams) == {'runs': 'object'}


class Ambiguous(BaseModel):
    label: str | Referenced[toys.Offset]


def test_a_parameter_of_text_or_an_output_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match='label: takes text or an output'):
        dtypes(Ambiguous)


def test_typed_names_the_index_and_casts_parameters_and_notes() -> None:
    frame = pd.DataFrame(
        {
            'sample': ['run:1', None],
            'thickness': [2, None],
            'binning': [{'bins': 3}, float('nan')],
            'run': [1.0, None],
            'name': ['water', None],
        },
        index=['0342', 'b'],
    )

    table = typed(frame, toys.Params)

    assert table.index.name == KEY
    assert list(table.index) == ['0342', 'b']
    assert table.index.dtype == 'string'
    assert table.dtypes.astype(str).to_dict() == {
        'sample': 'string',
        'thickness': 'Float64',
        'binning': 'object',
        'run': 'Int64',
        'name': 'string',
    }
    assert table.loc['b', 'binning'] is None


@pytest.mark.parametrize(
    ('index', 'columns', 'message'),
    [
        (['a', 'a'], {}, "the key 'a' is given twice"),
        (['a', ''], {}, 'a key is empty'),
        ([60339.0], {}, '^a key is text, not a number such as 60339.0$'),
        (['a', 'b'], {'scale': [1.0, 'thick']}, "row 'b', column 'scale': 'thick'"),
        (['a'], {'scale': ['2']}, "row 'a', column 'scale': '2' is not a number"),
        (['a'], {'scale': [True]}, "row 'a', column 'scale': True is not a number"),
        (['a', 'b'], {'word': ['run:1', 60339.0]}, "row 'b', column 'word': 60339.0"),
        (['a'], {'count': [2.5]}, "row 'a', column 'count': 2.5 is not a whole"),
        (['a'], {'count': [float('inf')]}, "row 'a', column 'count': inf is not a"),
        (['a'], {'count': [2**63]}, f"row 'a', column 'count': {2**63} is not a"),
        (['a'], {'flag': ['yes']}, "row 'a', column 'flag': 'yes' is not true"),
        (['a'], {'flag': [1]}, "row 'a', column 'flag': 1 is not true or false"),
        (['a'], {'level': [(1, 2)]}, r"row 'a', column 'level': \(1, 2\) is not plain"),
        (['a'], {'key': ['x']}, "a column is named 'key'.*set_index"),
        (['a'], {0: ['x']}, 'the column 0 is not text'),
        (
            ['a'],
            {'when': [datetime(2026, 1, 1, tzinfo=UTC)]},
            r"column 'when': Timestamp.* is not text",
        ),
    ],
)
def test_typed_refuses_a_key_a_column_or_a_cell_and_names_it(
    index: list[Any], columns: dict[Any, list[Any]], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        typed(pd.DataFrame(columns, index=index), Kinds)


def test_typed_refuses_a_column_given_twice() -> None:
    frame = pd.DataFrame([['a', 'b']], columns=['word', 'word'], index=['r'])

    with pytest.raises(ValueError, match="the column 'word' is given twice"):
        typed(frame, Kinds)


def test_typed_refuses_a_note_of_another_dtype_by_name() -> None:
    frame = pd.DataFrame({'start': [pd.Timestamp('2026-11-19')]}, index=['a'])

    with pytest.raises(ValueError, match=r"the note 'start' is datetime64.*text"):
        typed(frame, Kinds)


def test_a_float_nan_in_a_column_of_numbers_is_an_empty_cell() -> None:
    frame = pd.DataFrame({'scale': [float('nan'), 1.0]}, index=['a', 'b'])

    table = typed(frame, Kinds)

    assert table['scale'].isna().tolist() == [True, False]
    pd.testing.assert_frame_equal(frame_of(data_of(table)), table)


@pytest.fixture
def exact() -> pd.DataFrame:
    key = pd.Index(['a', 'b', '0342'], name=KEY, dtype='string')
    return pd.DataFrame(
        {
            'x': pd.array([0.1 + 0.2, None, 6.02214076e23], dtype='Float64'),
            'n': pd.array([2**53 + 1, None, 1], dtype='Int64'),
            'f': pd.array([True, None, False], dtype='boolean'),
            's': pd.array(['run:1', None, 'r17.centre'], dtype='string'),
            'o': pd.Series([['run:1'], None, {'v': 0.1 + 0.2}], index=key),
        },
        index=key,
    )


def test_the_data_of_a_table_read_back_as_json_give_the_table_exactly(
    exact: pd.DataFrame,
) -> None:
    back = frame_of(from_json(to_json(data_of(exact))))

    pd.testing.assert_frame_equal(back, exact)
    assert back.loc['a', 'x'] == 0.1 + 0.2
    assert back.loc['a', 'n'] == 2**53 + 1


def test_the_data_of_a_table_without_rows_give_its_columns_and_dtypes(
    exact: pd.DataFrame,
) -> None:
    empty = exact.iloc[:0]

    pd.testing.assert_frame_equal(frame_of(data_of(empty)), empty)


def test_the_data_of_a_table_without_columns_give_its_keys() -> None:
    table = typed(pd.DataFrame(index=['a', 'b']), Kinds)

    pd.testing.assert_frame_equal(frame_of(data_of(table)), table)


@pytest.mark.parametrize(
    ('change', 'message'),
    [
        (lambda d: d.pop('schema'), 'not a table'),
        (lambda d: d.update(data={'a': 1}), 'data: not a list of rows'),
        (lambda d: d['data'][1].pop(KEY), r"data\[1\]: not an object with a 'key'"),
        (lambda d: d['data'][2].update(y=1), r'data\[2\]: no field y'),
        (lambda d: d['data'][0].update(n=2.5), "row 'a', column 'n': 2.5 is not"),
        (lambda d: d['data'][0].update(s=1.0), "row 'a', column 's': 1.0 is not"),
        (lambda d: d['data'][0].update(n=True), "row 'a', column 'n': True is not"),
        (lambda d: d['data'][1].update(key='a'), "the key 'a' is given twice"),
        (lambda d: d['schema']['fields'].append({'name': 'n'}), "'n' is given twice"),
    ],
)
def test_data_that_are_not_a_table_are_refused_by_entry(
    exact: pd.DataFrame, change: Callable[[dict[str, Any]], object], message: str
) -> None:
    data = from_json(to_json(data_of(exact)))
    change(data)

    with pytest.raises(ValueError, match=message):
        frame_of(data)


class Either(BaseModel):
    source: RunFile | Referenced[toys.Offset]
    centre: Referenced[toys.Offset]


def output(record: str, name: str, key: str | None = None) -> OutputRef:
    return OutputRef(record=record, output=name, key=key)


@pytest.mark.parametrize(
    ('model', 'cells', 'request_values'),
    [
        (toys.Params, {'sample': 'run:60339'}, {'sample': FileRef(file='run:60339')}),
        (toys.Params, {'sample': 'path:a.nxs'}, {'sample': FileRef(file='path:a.nxs')}),
        (toys.Params, {'sample': ''}, {'sample': ''}),
        (toys.Params, {'offset': 'r17.offset'}, {'offset': output('r17', 'offset')}),
        (toys.Params, {'binning': {'bins': 3}}, {'binning': {'bins': 3}}),
        (Either, {'centre': 'r18.slices[2]'}, {'centre': output('r18', 'slices', '2')}),
        (Either, {'source': 'run:1'}, {'source': FileRef(file='run:1')}),
        (Either, {'source': 'r17.offset'}, {'source': output('r17', 'offset')}),
        (
            toys.SeriesParams,
            {'runs': ['run:1', 'run:2']},
            {'runs': [FileRef(file='run:1'), FileRef(file='run:2')]},
        ),
    ],
)
def test_a_cell_is_converted_to_what_a_request_takes_and_back(
    model: type[BaseModel], cells: dict[str, Any], request_values: dict[str, Any]
) -> None:
    converted = as_request(model, cells)

    assert converted == request_values
    assert {name: cell_of(v) for name, v in converted.items()} == cells


def test_a_whole_number_at_a_file_is_a_run() -> None:
    assert as_request(toys.SeriesParams, {'runs': [60339, 'run:2']}) == {
        'runs': [FileRef(file='run:60339'), FileRef(file='run:2')]
    }


class Masked(BaseModel):
    mask: RunFile


class Grouped(BaseModel):
    masked: Masked
    when: datetime


def test_the_cell_of_a_group_or_a_time_is_its_json_form_which_a_request_takes() -> None:
    values = {
        'masked': Masked.model_construct(mask=FileRef(file='run:1')),
        'when': datetime(2026, 11, 19, 12, tzinfo=UTC),
    }

    cells = {name: cell_of(v) for name, v in values.items()}

    assert cells == {
        'masked': {'mask': {'@ref': 'file', 'file': 'run:1'}},
        'when': '2026-11-19T12:00:00Z',
    }
    request_model(Grouped).model_validate(as_request(Grouped, cells))
