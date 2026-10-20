# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The lifecycle of an accumulator, event by event, without threads."""

import pytest

from ess.dispatch import Status
from ess.dispatch.lifecycle import (
    Action,
    Checking,
    DropDriver,
    DropOutputs,
    FailStates,
    Freezing,
    Frozen,
    LetGo,
    Lifecycle,
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
    assert opened.step_name == 'push 0'
    assert opened.step_done() == [LetGo([('a', 'value')]), Reached(1)]


def test_a_push_waits_for_the_records_it_reads(opened: Lifecycle) -> None:
    step = push('a')
    opened.queue(step)

    assert advanced(opened, a=Status.PENDING) == []
    assert advanced(opened) == [StartStep(step)]


def test_a_record_that_did_not_complete_stops_the_accumulator(
    opened: Lifecycle,
) -> None:
    opened.queue(push('a'))
    opened.queue(push('b'))

    reason = 'the accumulator stopped: push 0: record a failed'
    assert advanced(opened, a=Status.FAILED) == [
        LetGo([('a', 'value')]),
        LetGo([('b', 'value')]),
        FailStates(0, reason),
        DropDriver(),
    ]
    assert opened.phase == Stopped(reason)
    assert (opened.unreadable, opened.push_refused()) == (reason, reason)
    assert not opened.working


def test_a_stopped_accumulator_keeps_its_driver_while_a_reader_holds_it(
    opened: Lifecycle,
) -> None:
    opened.queue(step := push())
    advanced(opened)

    reason = 'the accumulator stopped: push 0 failed: cannot add'
    assert opened.step_failed('cannot add', held=True) == [
        LetGo(step.reads),
        FailStates(0, reason),
    ]
    assert advanced(opened) == []
    assert opened.unread(0, list) == [DropDriver()]


def test_a_failed_opening_stops_the_accumulator() -> None:
    lifecycle = Lifecycle()
    lifecycle.queue(OPENING)
    advanced(lifecycle)

    reason = 'the accumulator stopped: opening failed: no such file'
    assert lifecycle.step_failed('no such file', held=False) == [
        LetGo([]),
        FailStates(0, reason),
        DropDriver(),
    ]


def test_pushes_wait_while_a_freeze_is_checked(opened: Lifecycle) -> None:
    opened.start_check()

    assert opened.checking
    assert opened.phase == Checking(Open())
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
    assert opened.push_refused() == 'the accumulator was frozen as record r'
    assert opened.freeze_refused(lambda record: Status.PENDING) == (
        'the accumulator is frozen as record r'
    )
    assert opened.unreadable is None
    assert opened.freeze_finished('other', completed=True) == []
    assert opened.phase == Freezing('r')
    assert opened.freeze_finished('r', completed=True) == []
    assert opened.phase == Frozen('r')
    assert opened.unreadable == 'the accumulator is frozen: read record r instead'
    assert opened.unread(0, list) == [DropDriver()]


def test_a_failed_freeze_may_be_made_again_and_takes_no_pushes(
    opened: Lifecycle,
) -> None:
    opened.start_check()
    opened.end_check('r')
    opened.freeze_finished('r', completed=False)

    assert opened.freeze_refused(lambda record: Status.FAILED) is None
    assert opened.push_refused() == 'the accumulator was frozen as record r'
    # no push comes to drop the outputs of its state
    assert opened.unread(0, list) == [DropOutputs()]
    opened.start_check()
    opened.end_check(None)
    assert opened.phase == Freezing('r')


def test_a_stop_while_a_freeze_is_checked_stays(opened: Lifecycle) -> None:
    opened.queue(push())
    advanced(opened)
    opened.start_check()
    opened.step_failed('cannot add', held=False)
    opened.end_check(None)

    assert isinstance(opened.phase, Stopped)


def test_a_release_drops_the_steps_that_no_reader_needs(opened: Lifecycle) -> None:
    steps = [push(name) for name in 'abcd']
    for step in steps:
        opened.queue(step)
    advanced(opened)  # push 0 runs

    assert opened.release([2]) == [LetGo(steps[3].reads), LetGo(steps[2].reads)]
    assert opened.released
    assert list(opened.steps) == [steps[1]]
    assert opened.unread(2, list) == [LetGo(steps[1].reads)]  # no reader left
    assert opened.running == steps[0]  # a running step stays
