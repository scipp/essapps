# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""What is ready, left to do, and shown, on plain values: no client."""

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel

from ess.apps import Record, Request, SpecId, Status
from ess.apps.batch import (
    Row,
    RowState,
    arrived,
    earlier_settings,
    left_to_reduce,
    ready,
    refused_again,
    remaining,
    status_texts,
    summary,
)
from ess.apps.batch.state import differs, results, row_states
from ess.apps.errors import FieldError
from ess.spec import OutputRef

SPEC = SpecId(name='iofq', version=1)


class Model(BaseModel):
    shared: int = 0
    sample: str
    thickness: float = 1.0


COLUMNS = ('sample', 'thickness')


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


def states(rows: list[Row], *found: Record, **status: Status) -> dict[str, RowState]:
    return row_states(rows, Model, COLUMNS, {r.member: r for r in found}, status)


def test_a_row_with_an_empty_required_cell_is_not_ready() -> None:
    found = states([Row('a', {'sample': 'x'}), Row('b')])

    assert ready(found) == ['a']
    assert [(e.entry, e.loc) for e in found['b'].problems] == [('b', ('sample',))]


def test_an_empty_cell_of_a_column_with_a_default_is_ready() -> None:
    assert ready(states([Row('a', {'sample': 'x'})])) == ['a']


def test_a_value_outside_the_columns_is_a_problem() -> None:
    found = states([Row('a', {'sample': 'x', 'shared': 2})])

    assert [e.loc for e in found['a'].problems] == [('shared',)]


def test_a_cell_reading_a_failed_record_is_a_problem() -> None:
    ref = OutputRef(record='c', output='centre')
    found = states([Row('a', {'sample': ref})], c=Status.FAILED)

    assert ready(found) == []
    assert 'failed' in found['a'].problems[0].msg


def test_a_cell_reading_a_pending_record_is_ready() -> None:
    ref = OutputRef(record='c', output='centre')

    assert ready(states([Row('a', {'sample': ref})], c=Status.PENDING)) == ['a']


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
    found = states([Row('a', {'sample': 'x'})], record('1', 'a'), **{'1': status})

    assert remaining(found) == (['a'] if left else [])


def test_a_row_without_a_result_is_left_to_do() -> None:
    assert remaining(states([Row('a', {'sample': 'x'})])) == ['a']


@pytest.mark.parametrize(
    'outdated', [{'changed': {'a': frozenset({'thickness'})}}, {'spec_changed': {'a'}}]
)
def test_an_outdated_row_is_left_to_do_and_shown_as_outdated(
    outdated: dict[str, Any],
) -> None:
    found = row_states(
        [Row('a', {'sample': 'x'})],
        Model,
        COLUMNS,
        {'a': record('1', 'a')},
        {'1': Status.COMPLETED},
        **outdated,
    )

    assert remaining(found) == ['a']
    assert found['a'].text == 'outdated'


@pytest.mark.parametrize(
    ('changed', 'outdated'), [({'shared'}, False), ({'shared', 'thickness'}, True)]
)
def test_a_tweak_is_outdated_only_where_a_column_changed(
    changed: set[str], outdated: bool
) -> None:
    found = row_states(
        [Row('a', {'sample': 'x'})],
        Model,
        COLUMNS,
        {'a': record('1', 'a')},
        {'1': Status.COMPLETED},
        changed={'a': frozenset(changed)},
        tweaked={'a'},
    )

    assert found['a'].changed == changed
    assert found['a'].outdated is outdated
    assert (remaining(found) == ['a']) is outdated


@pytest.mark.parametrize(
    ('status', 'changed', 'other', 'earlier', 'left'),
    [
        (Status.COMPLETED, {'shared'}, {}, True, False),
        (Status.COMPLETED, {'shared', 'thickness'}, {}, False, True),
        (Status.COMPLETED, {'shared'}, {'spec_changed': {'a'}}, False, True),
        (Status.COMPLETED, {'shared'}, {'tweaked': {'a'}}, False, False),
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
        [Row('a', {'sample': 'x'})],
        Model,
        COLUMNS,
        {'a': record('1', 'a')},
        {'1': status},
        changed={'a': frozenset(changed)},
        **other,
    )

    assert earlier_settings(found, COLUMNS) == (['a'] if earlier else [])
    assert left_to_reduce(found, COLUMNS) == (['a'] if left else [])
    assert (remaining(found) == ['a']) is (earlier or left)


def test_a_row_that_cannot_run_is_not_one_of_earlier_settings() -> None:
    found = row_states(
        [Row('a')],
        Model,
        COLUMNS,
        {'a': record('1', 'a')},
        {'1': Status.COMPLETED},
        changed={'a': frozenset({'shared'})},
    )

    assert found['a'].problems
    assert earlier_settings(found, COLUMNS) == []


def earlier_and_new() -> dict[str, RowState]:
    """Row ``a`` reduced with earlier settings, ``b`` without a result."""
    return row_states(
        [Row('a', {'sample': 'x'}), Row('b', {'sample': 'y'})],
        Model,
        COLUMNS,
        {'a': record('1', 'a')},
        {'1': Status.COMPLETED},
        changed={'a': frozenset({'shared'}), 'b': frozenset()},
    )


def test_a_table_marks_the_rows_of_earlier_settings_as_completed() -> None:
    assert status_texts(earlier_and_new(), COLUMNS) == {
        'a': 'completed, earlier settings',
        'b': 'not reduced',
    }
    assert status_texts(not_compared(), COLUMNS) == {'a': 'outdated'}


def test_refused_again_keeps_the_reasons_of_the_rows_a_step_submits_again() -> None:
    refused = {'a': 'no', 'b': 'no either'}

    assert refused_again(earlier_and_new(), {'sample'}, refused) == {'b': 'no either'}
    assert refused_again(not_compared(), {'sample'}, {'a': 'no'}) == {}


def test_a_row_without_a_result_is_left_to_reduce() -> None:
    found = states([Row('a', {'sample': 'x'}), Row('b')])

    assert left_to_reduce(found, COLUMNS) == ['a']


@pytest.mark.parametrize(
    ('status', 'changed', 'at', 'given'),
    [
        (Status.COMPLETED, {'sample'}, {'sample'}, True),
        (Status.COMPLETED, {'sample', 'shared'}, {'thickness'}, False),
        (Status.COMPLETED, {'sample'}, set(), False),
        (Status.COMPLETED, set(), {'sample', 'thickness'}, False),
        (Status.FAILED, set(), {'sample'}, False),
        (Status.FAILED, {'sample'}, {'sample'}, True),
        (Status.CANCELLED, {'shared'}, {'sample'}, False),
        (Status.PENDING, {'sample'}, {'sample'}, False),
    ],
)
def test_a_row_with_a_result_arrives_for_a_change_at_a_column_of_at(
    status: Status, changed: set[str], at: set[str], given: bool
) -> None:
    found = row_states(
        [Row('a', {'sample': 'x'})],
        Model,
        COLUMNS,
        {'a': record('1', 'a')},
        {'1': status},
        changed={'a': frozenset(changed)},
    )

    assert arrived(found, at) == (['a'] if given else [])


def test_a_ready_row_without_a_result_arrives_whatever_at_is() -> None:
    found = states([Row('a', {'sample': 'x'}), Row('b')])

    assert arrived(found, set()) == ['a']


def test_a_refused_value_is_a_problem_of_its_row() -> None:
    reason = FieldError('a', ('thickness',), 'must be positive')
    found = row_states(
        [Row('a', {'sample': 'x'})],
        Model,
        COLUMNS,
        {'a': record('1', 'a')},
        {'1': Status.COMPLETED},
        refused={'a': (reason,)},
    )

    assert found['a'].problems == (reason,)
    assert remaining(found) == []


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


def test_summary_counts_the_rows_by_what_they_show() -> None:
    found = states(
        [Row('a', {'sample': 'x'}), Row('b'), Row('c', {'sample': 'y'})],
        record('1', 'a'),
        **{'1': Status.COMPLETED},
    )

    assert summary(found) == {'completed': 1, 'incomplete': 1, 'not reduced': 1}


def not_compared(*, tweaked: bool = False) -> dict[str, RowState]:
    return row_states(
        [Row('a', {'sample': 'x'})],
        Model,
        COLUMNS,
        {'a': record('1', 'a')},
        {'1': Status.COMPLETED},
        changed=None,
        tweaked={'a'} if tweaked else set(),
    )


def test_a_completed_row_not_compared_reads_outdated_but_a_tweak_reads_completed() -> (
    None
):
    assert not_compared()['a'].text == 'outdated'
    assert not_compared(tweaked=True)['a'].text == 'completed'


@pytest.mark.parametrize(
    'rule',
    [
        remaining,
        lambda s: arrived(s, {'sample'}),
        lambda s: earlier_settings(s, COLUMNS),
        lambda s: left_to_reduce(s, COLUMNS),
    ],
)
def test_the_rules_that_read_what_changed_refuse_rows_not_compared(rule: Any) -> None:
    with pytest.raises(ValueError, match=r"\['a'\] were not compared"):
        rule(not_compared())
