# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""What is ready, left to do, and shown, on plain values: no client."""

from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from ess.apps import Record, Request, SpecId, Status
from ess.apps.batch import Row, RowState, ready, remaining, summary
from ess.apps.batch.state import row_states
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
        request=Request(SPEC),
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


def test_a_stale_row_is_left_to_do_and_shown_as_stale() -> None:
    found = row_states(
        [Row('a', {'sample': 'x'})],
        Model,
        COLUMNS,
        {'a': record('1', 'a')},
        {'1': Status.COMPLETED},
        stale={'a'},
    )

    assert remaining(found) == ['a']
    assert found['a'].text == 'stale'


def test_summary_counts_the_rows_by_what_they_show() -> None:
    found = states(
        [Row('a', {'sample': 'x'}), Row('b'), Row('c', {'sample': 'y'})],
        record('1', 'a'),
        **{'1': Status.COMPLETED},
    )

    assert summary(found) == {'completed': 1, 'incomplete': 1, 'not reduced': 1}
