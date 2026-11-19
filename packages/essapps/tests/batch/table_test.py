# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The rules of a table of rows, on plain values."""

import re
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from ess.apps import SubmitError
from ess.apps.batch import EMPTY, Row, RowTable, Scope


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


def test_a_scope_of_keys_holds_the_rows_named() -> None:
    rows = [*indexed(1, 2, 3), Row('none')]

    assert held(Scope(keys=frozenset({'3', 'none'})), rows) == ['3', 'none']


def test_a_row_without_an_index_is_held_only_by_a_scope_without_ends() -> None:
    rows = [*indexed(1), Row('none')]

    assert held(Scope(), rows) == ['1', 'none']
    assert held(Scope(start=0), rows) == ['1']


def test_a_scope_of_keys_with_ends_is_refused() -> None:
    with pytest.raises(ValueError, match='a slice or keys, not both'):
        Scope(start=1, keys=frozenset({'1'}))


def test_keys_given_as_one_string_are_refused() -> None:
    with pytest.raises(TypeError, match="not '60339'"):
        Scope(keys='60339')  # type: ignore[arg-type]


def test_keys_given_as_any_iterable_of_strings_are_kept_as_a_frozenset() -> None:
    scope = Scope(keys=['2', '1'])  # type: ignore[arg-type]

    assert scope == Scope(keys=frozenset({'1', '2'}))
    assert isinstance(scope.keys, frozenset)


@pytest.mark.parametrize('end', [datetime(2026, 11, 18, tzinfo=UTC), True])
def test_an_end_that_is_not_an_int_a_float_or_a_str_is_refused(end: object) -> None:
    with pytest.raises(TypeError, match=re.escape(repr(end))):
        Scope(start=end)  # type: ignore[arg-type]


def test_a_stop_below_the_start_is_refused() -> None:
    with pytest.raises(
        ValueError, match='the stop of a scope, 3, is below its start, 5'
    ):
        Scope(start=5, stop=3)


def test_a_stop_equal_to_the_start_holds_no_row() -> None:
    assert held(Scope(start=2, stop=2), indexed(1, 2, 3)) == []


def test_ends_that_do_not_compare_are_refused() -> None:
    with pytest.raises(ValueError, match="do not compare: '60339', 60421"):
        Scope(start='60339', stop=60421)


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
