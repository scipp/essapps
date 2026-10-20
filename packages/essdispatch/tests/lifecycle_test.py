# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The lifecycle of an accumulator, event by event, without threads."""

import pytest

from ess.dispatch import Status
from ess.dispatch.lifecycle import (
    Action,
    Checking,
    DropDriver,
    FailStates,
    Freezing,
    Frozen,
    LetGo,
    Lifecycle,
    LogFailure,
    Open,
    Reached,
    StartStep,
    Step,
    Stopped,
)

OPENING = Step({'scale': 2.0}, [])


def push(*reads: str) -> Step:
    """A push of one row, which reads the output ``value`` of each record."""
    return Step({'runs': [len(reads)]}, [(r, 'value') for r in reads])


def advanced(
    lifecycle: Lifecycle, *, held: bool = False, **statuses: Status
) -> list[Action]:
    """Advance, with each record in ``statuses`` of its status, the others completed."""
    return lifecycle.advance(held, lambda r: statuses.get(r, Status.COMPLETED))


@pytest.fixture
def opened() -> Lifecycle:
    """A lifecycle whose opening is done: state 0 is reached."""
    lifecycle = Lifecycle()
    lifecycle.queue(OPENING)
    assert advanced(lifecycle) == [StartStep(OPENING)]
    assert lifecycle.step_done() == [LetGo([]), Reached(0)]
    return lifecycle


def test_the_opening_starts_at_once_and_reaches_state_0() -> None:
    lifecycle = Lifecycle()
    lifecycle.queue(OPENING)

    assert lifecycle.step_name == 'opening'
    assert advanced(lifecycle, held=True) == [StartStep(OPENING)]  # no state yet
    assert lifecycle.running == OPENING
    assert advanced(lifecycle) == []  # one step at a time
    assert lifecycle.step_done() == [LetGo([]), Reached(0)]
    assert (lifecycle.added, lifecycle.working) == (0, False)


def test_a_push_waits_for_the_readers_of_the_state_before_it(
    opened: Lifecycle,
) -> None:
    step = push('a')
    opened.queue(step)

    assert advanced(opened, held=True) == []
    assert advanced(opened) == [StartStep(step)]
    assert opened.step_name == 'push 1'
    assert opened.push_status(1) is Status.PENDING
    assert opened.step_done() == [LetGo([('a', 'value')]), Reached(1)]
    assert opened.push_status(1) is Status.COMPLETED


def test_a_push_waits_for_the_records_it_reads(opened: Lifecycle) -> None:
    step = push('a')
    opened.queue(step)

    assert advanced(opened, a=Status.PENDING) == []
    assert advanced(opened) == [StartStep(step)]


def test_a_record_that_did_not_complete_fails_the_push_alone(
    opened: Lifecycle,
) -> None:
    failing, after = push('a'), push('b')
    opened.queue(failing)
    opened.queue(after)

    reason = 'push 1: record a failed'
    assert advanced(opened, a=Status.FAILED) == [
        LetGo(failing.reads),
        LogFailure(1, reason),
        FailStates(1, reason),
    ]
    assert opened.added == 1  # state 1 is state 0
    assert (opened.push_status(1), opened.push_failure(1)) == (Status.FAILED, reason)
    assert opened.status() is Status.PENDING
    assert advanced(opened) == [StartStep(after)]
    assert opened.step_done() == [LetGo(after.reads), Reached(2)]


def test_a_record_that_the_opening_reads_and_did_not_complete_stops_it() -> None:
    lifecycle = Lifecycle()
    lifecycle.queue(opening := Step({}, [('a', 'value')]))
    lifecycle.queue(push())

    reason = 'the accumulator stopped: opening: record a cancelled'
    assert advanced(lifecycle, a=Status.CANCELLED) == [
        LetGo(opening.reads),
        LetGo([]),
        FailStates(0, reason),
        DropDriver(),
    ]
    assert lifecycle.phase == Stopped(reason)
    assert (lifecycle.status(), lifecycle.failure()) == (Status.FAILED, reason)
    assert (lifecycle.push_status(1), lifecycle.push_failure(1)) == (
        Status.CANCELLED,
        reason,
    )


def test_a_push_that_fails_before_the_driver_has_rows_fails_alone(
    opened: Lifecycle,
) -> None:
    opened.queue(step := push('a'))
    advanced(opened)

    reason = 'push 1 failed: file signature not found'
    assert opened.step_failed(
        'file signature not found', driver_changed=False, held=True
    ) == [
        LetGo(step.reads),
        LogFailure(1, reason),
        FailStates(1, reason),
    ]
    assert opened.phase == Open()
    assert opened.unreadable is None


def test_a_push_that_may_leave_rows_in_the_driver_stops_the_accumulator(
    opened: Lifecycle,
) -> None:
    opened.queue(step := push())
    opened.queue(later := push('b'))
    advanced(opened)

    failed = 'push 1 failed: cannot add'
    reason = f'the accumulator stopped: {failed}'
    assert opened.step_failed('cannot add', driver_changed=True, held=True) == [
        LogFailure(1, failed),
        LetGo(step.reads),
        LetGo(later.reads),
        FailStates(0, reason),
    ]
    assert (opened.unreadable, opened.refusal()) == (reason, reason)
    assert opened.push_failure(1) == failed
    assert (opened.push_status(2), opened.push_failure(2)) == (
        Status.CANCELLED,
        reason,
    )
    assert advanced(opened) == []
    assert opened.unread(0, list) == [DropDriver()]  # the reader held the driver
    assert not opened.working


def test_a_failed_opening_stops_the_accumulator() -> None:
    lifecycle = Lifecycle()
    lifecycle.queue(opening := Step({}, [('a', 'value')]))
    advanced(lifecycle)

    reason = 'the accumulator stopped: opening failed: no such file'
    failed = lifecycle.step_failed('no such file', driver_changed=False, held=False)
    assert failed == [LetGo(opening.reads), FailStates(0, reason), DropDriver()]


def test_a_failure_since_a_pin_of_a_push_it_holds_is_found(opened: Lifecycle) -> None:
    for name in 'abc':
        opened.queue(push(name))
    known = len(opened.failed)  # a read pins state 2
    advanced(opened, a=Status.FAILED)
    advanced(opened)
    opened.step_done()

    assert opened.failed_since(known, 2) == 'push 1: record a failed'
    assert opened.failed_since(len(opened.failed), 2) is None  # pinned after it
    advanced(opened, c=Status.FAILED)
    assert opened.failed_since(1, 2) is None  # push 3 is not in state 2


def test_pushes_wait_while_a_freeze_is_checked(opened: Lifecycle) -> None:
    opened.start_check()

    assert opened.phase == Checking()
    opened.end_check(None)  # refused
    assert opened.phase == Open()
    opened.end_check(None)  # again: nothing changes
    assert opened.phase == Open()


def test_a_freeze_takes_no_pushes_and_its_record_is_read_once_completed(
    opened: Lifecycle,
) -> None:
    opened.start_check()
    opened.end_check('r')
    opened.end_check(None)  # the check's cleanup, once logged

    assert opened.phase == Freezing('r')
    assert opened.is_freeze('r')
    assert not opened.is_freeze('other')
    assert opened.refusal() == (
        'the accumulator is frozen as record r; '
        'it takes pushes and freezes again if that record fails'
    )
    assert (opened.unreadable, opened.status()) == (None, Status.PENDING)
    opened.freeze_finished('other', completed=True)
    assert opened.phase == Freezing('r')
    opened.freeze_finished('r', completed=True)
    assert opened.phase == Frozen('r')
    assert opened.status() is Status.COMPLETED
    assert opened.unreadable == 'the accumulator is frozen: read record r instead'
    assert opened.unread(0, list) == [DropDriver()]


def test_a_freeze_whose_record_fails_leaves_the_accumulator_open(
    opened: Lifecycle,
) -> None:
    opened.start_check()
    opened.end_check('r')
    opened.freeze_finished('r', completed=False)

    assert opened.phase == Open()
    assert opened.refusal() is None


def test_a_stop_while_a_freeze_is_checked_stays(opened: Lifecycle) -> None:
    opened.queue(push())
    advanced(opened)
    opened.start_check()
    opened.step_failed('cannot add', driver_changed=True, held=False)
    opened.end_check(None)

    assert isinstance(opened.phase, Stopped)


def test_a_release_drops_the_steps_that_no_reader_needs(opened: Lifecycle) -> None:
    steps = [push(name) for name in 'abcd']
    for step in steps:
        opened.queue(step)
    advanced(opened)  # push 1 runs

    assert opened.release([2]) == [LetGo(steps[3].reads), LetGo(steps[2].reads)]
    assert opened.released
    assert list(opened.steps) == [steps[1]]
    assert opened.push_status(2) is Status.PENDING
    assert opened.unread(2, list) == [LetGo(steps[1].reads)]  # no reader left
    assert opened.running == steps[0]  # a running step stays
