# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The rules of a table of rows, on plain values."""

import pytest

from ess.apps import SubmitError
from ess.apps.batch import EMPTY, Row, RowTable


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


def test_a_row_given_with_empty_has_that_cell_empty() -> None:
    t = RowTable().rescan([Row('a', {'x': EMPTY, 'y': 1})]).add(Row('b', {'x': EMPTY}))

    assert t['a'].values == {'y': 1}
    assert t['b'].values == {}
