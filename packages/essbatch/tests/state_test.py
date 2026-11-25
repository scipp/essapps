# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""What is ready, left to do, and shown, on plain values: no client."""

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import pytest
from ess.batch import (
    RowState,
    earlier_settings,
    left_to_reduce,
    not_reduced,
    ready,
    to_reduce,
)
from ess.batch.state import Which, differs, results, row_states
from pydantic import BaseModel

from ess.apps import Record, Request, SpecId, Status
from ess.apps.errors import FieldError
from ess.spec import OutputRef

SPEC = SpecId(name='iofq', version=1)


class Model(BaseModel):
    shared: int = 0
    sample: str
    thickness: float = 1.0


COLUMNS = ('sample', 'thickness')
OWN: defaultdict[str, frozenset[str]] = defaultdict(lambda: frozenset(COLUMNS))
"""Each row gives every column itself, in its cell."""


def record(
    id_: str, member: str | None, *, spec: SpecId = SPEC, by: str = 'me'
) -> Record:
    return Record(
        id=id_,
        request=Request(spec),
        proposal='p1',
        submitter='me',
        created=datetime(2026, 1, 1, tzinfo=UTC),
        outputs=(),
        label='iofq',
        member=member,
    )


def row(**cells: Any) -> dict[str, Any]:
    """The cells of a row as a batch gives them: an empty thickness its default."""
    return {'thickness': 1.0, **cells}


def states(
    cells: dict[str, dict[str, Any]], *found: Record, **status: Status
) -> dict[str, RowState]:
    return row_states(cells, COLUMNS, {r.member: r for r in found}, status, own=OWN)


def test_a_row_with_an_empty_required_cell_is_not_ready() -> None:
    found = states({'a': row(sample='x'), 'b': row()})

    assert ready(found) == ['a']
    assert [(e.entry, e.loc) for e in found['b'].problems] == [('b', ('sample',))]


def test_a_cell_reading_a_failed_record_is_a_problem() -> None:
    ref = OutputRef(record='c', output='centre')
    found = states({'a': row(sample=ref)}, c=Status.FAILED)

    assert ready(found) == []
    assert 'failed' in found['a'].problems[0].msg


def test_a_cell_reading_a_pending_record_is_ready() -> None:
    ref = OutputRef(record='c', output='centre')

    assert ready(states({'a': row(sample=ref)}, c=Status.PENDING)) == ['a']


@pytest.mark.parametrize(
    ('status', 'left'),
    [
        (Status.COMPLETED, False),
        (Status.PENDING, False),
        (Status.FAILED, True),
        (Status.CANCELLED, True),
    ],
)
def test_what_is_left_to_do_by_status(status: Status, left: bool) -> None:
    found = states({'a': row(sample='x')}, record('1', 'a'), **{'1': status})

    assert left_to_reduce(found) == (['a'] if left else [])


def test_a_row_without_a_result_is_left_to_do() -> None:
    assert left_to_reduce(states({'a': row(sample='x')})) == ['a']


@pytest.mark.parametrize(
    'outdated', [{'changed': {'a': frozenset({'thickness'})}}, {'spec_changed': {'a'}}]
)
def test_an_outdated_row_is_left_to_do_and_shown_as_outdated(
    outdated: dict[str, Any],
) -> None:
    found = row_states(
        {'a': row(sample='x')},
        COLUMNS,
        {'a': record('1', 'a')},
        {'1': Status.COMPLETED},
        **outdated,
        own=OWN,
    )

    assert left_to_reduce(found) == ['a']
    assert found['a'].text == 'outdated'


@pytest.mark.parametrize('status', [Status.FAILED, Status.CANCELLED])
@pytest.mark.parametrize(
    'outdated', [{'changed': {'a': frozenset({'thickness'})}}, {'spec_changed': {'a'}}]
)
def test_a_failed_or_cancelled_row_reads_so_when_its_request_changed(
    status: Status, outdated: dict[str, Any]
) -> None:
    found = row_states(
        {'a': row(sample='x')},
        COLUMNS,
        {'a': record('1', 'a')},
        {'1': status},
        **outdated,
        own=OWN,
    )

    assert left_to_reduce(found) == ['a']
    assert found['a'].text == str(status)


@pytest.mark.parametrize(
    ('status', 'changed', 'other', 'earlier', 'left'),
    [
        (Status.COMPLETED, {'shared'}, {}, True, False),
        (Status.COMPLETED, {'shared', 'thickness'}, {}, False, True),
        (Status.COMPLETED, {'shared'}, {'spec_changed': {'a'}}, False, True),
        (Status.COMPLETED, set(), {}, False, False),
        (Status.FAILED, {'shared'}, {}, False, True),
        (Status.PENDING, {'shared'}, {}, False, False),
    ],
)
def test_a_result_that_differs_only_at_settings_is_not_left_to_reduce(
    status: Status,
    changed: set[str],
    other: dict[str, Any],
    earlier: bool,
    left: bool,
) -> None:
    found = row_states(
        {'a': row(sample='x')},
        COLUMNS,
        {'a': record('1', 'a')},
        {'1': status},
        changed={'a': frozenset(changed)},
        **other,
        own=OWN,
    )

    assert found['a'].earlier is earlier
    assert earlier_settings(found) == (['a'] if earlier else [])
    assert left_to_reduce(found) == (['a'] if left else [])


def test_a_change_at_a_column_where_the_cell_is_empty_is_of_earlier_settings() -> None:
    found = row_states(
        {'a': row(sample='x'), 'b': row(sample='y')},
        COLUMNS,
        {'a': record('1', 'a'), 'b': record('2', 'b')},
        {'1': Status.COMPLETED, '2': Status.COMPLETED},
        changed={'a': frozenset({'thickness'}), 'b': frozenset({'thickness'})},
        own={'a': {'sample'}, 'b': {'sample', 'thickness'}},
    )

    assert {k: s.text for k, s in found.items()} == {'a': 'earlier', 'b': 'outdated'}


def test_a_row_that_cannot_run_is_not_one_of_earlier_settings() -> None:
    found = row_states(
        {'a': row()},
        COLUMNS,
        {'a': record('1', 'a')},
        {'1': Status.COMPLETED},
        changed={'a': frozenset({'shared'})},
        own=OWN,
    )

    assert found['a'].problems
    assert earlier_settings(found) == []


def earlier_and_new() -> dict[str, RowState]:
    """Row ``a`` reduced with earlier settings, ``b`` without a result."""
    return row_states(
        {'a': row(sample='x'), 'b': row(sample='y')},
        COLUMNS,
        {'a': record('1', 'a')},
        {'1': Status.COMPLETED},
        changed={'a': frozenset({'shared'}), 'b': frozenset()},
        own=OWN,
    )


def test_a_row_of_earlier_settings_reads_earlier() -> None:
    found = earlier_and_new()

    assert {k: s.text for k, s in found.items()} == {'a': 'earlier', 'b': 'ready'}


@pytest.mark.parametrize(
    ('status', 'text'),
    [
        (None, 'ready'),
        (Status.PENDING, 'running'),
        (Status.COMPLETED, 'done'),
        (Status.FAILED, 'failed'),
        (Status.CANCELLED, 'cancelled'),
    ],
)
def test_a_row_reads_its_status_in_the_words_of_the_page(
    status: Status | None, text: str
) -> None:
    found = states(
        {'a': row(sample='x'), 'b': row()},
        *([] if status is None else [record('1', 'a')]),
        **({} if status is None else {'1': status}),
    )

    assert found['a'].text == text
    assert found['b'].text == 'incomplete'


@pytest.mark.parametrize(
    ('which', 'keys'),
    [('remaining', ['b']), ('earlier', ['a']), ('all', ['a', 'b'])],
)
def test_the_rows_to_reduce_of_each_choice(which: Which, keys: list[str]) -> None:
    assert to_reduce(earlier_and_new(), which) == keys


def test_a_row_without_a_result_is_left_to_reduce() -> None:
    found = states({'a': row(sample='x'), 'b': row()})

    assert left_to_reduce(found) == ['a']


@pytest.mark.parametrize(
    ('status', 'other'),
    [
        (Status.COMPLETED, {}),
        (Status.COMPLETED, {'changed': {'a': frozenset({'sample'})}}),
        (Status.COMPLETED, {'spec_changed': {'a'}}),
        (Status.COMPLETED, {'changed': None}),
        (Status.FAILED, {}),
        (Status.CANCELLED, {}),
        (Status.PENDING, {}),
    ],
)
def test_a_row_with_a_result_is_not_reduced_by_a_step(
    status: Status, other: dict[str, Any]
) -> None:
    found = row_states(
        {'a': row(sample='x')},
        COLUMNS,
        {'a': record('1', 'a')},
        {'1': status},
        **other,
        own=OWN,
    )

    assert not_reduced(found) == []


@pytest.mark.parametrize('changed', [{}, None])
def test_a_ready_row_without_a_result_is_reduced_by_a_step(
    changed: dict[str, frozenset[str]] | None,
) -> None:
    found = row_states(
        {'a': row(sample='x'), 'b': row()}, COLUMNS, {}, {}, changed=changed, own=OWN
    )

    assert not_reduced(found) == ['a']


def test_a_refused_value_is_a_problem_of_its_row() -> None:
    reason = FieldError('a', ('thickness',), 'must be positive')
    found = row_states(
        {'a': row(sample='x')},
        COLUMNS,
        {'a': record('1', 'a')},
        {'1': Status.COMPLETED},
        refused={'a': (reason,)},
        own=OWN,
    )

    assert found['a'].problems == (reason,)
    assert left_to_reduce(found) == []


def test_the_result_of_a_row_is_its_newest_record_of_any_spec() -> None:
    older = SpecId(name='iofq', version=0)

    found = results([record('1', 'a'), record('2', 'a', spec=older), record('3', 'b')])

    assert {k: r.id for k, r in found.items()} == {'a': '2', 'b': '3'}


def test_a_value_differs_if_either_side_lacks_it() -> None:
    was = {'sample': 'x', 'thickness': 1.0, 'shared': 0}

    assert differs({'sample': 'x', 'thickness': 1.0}, was, Model.model_fields) == [
        'shared'
    ]
    assert differs({'sample': 'y', 'shared': 0}, was, ['sample', 'shared']) == [
        'sample'
    ]


def test_a_list_in_another_order_differs() -> None:
    assert differs({'runs': ['b', 'a']}, {'runs': ['a', 'b']}, ['runs']) == ['runs']


def not_compared() -> dict[str, RowState]:
    return row_states(
        {'a': row(sample='x')},
        COLUMNS,
        {'a': record('1', 'a')},
        {'1': Status.COMPLETED},
        changed=None,
        own=OWN,
    )


def test_a_completed_row_not_compared_reads_outdated() -> None:
    assert not_compared()['a'].text == 'outdated'


def test_a_completed_row_not_compared_is_neither_earlier_nor_left_to_reduce() -> None:
    found = not_compared()

    assert earlier_settings(found) == []
    assert left_to_reduce(found) == []
