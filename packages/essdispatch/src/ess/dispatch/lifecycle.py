# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The lifecycle of an accumulator: when each of its steps starts, and what
each event does to it.

An accumulator's *steps* are its opening, which makes the driver, and then
its pushes, in the order logged. The backend does them one at a time on its
workers. Push k, counted from 1, makes state k. Its *phase* says what the
accumulator takes:

============  =====================================  ======  =======
phase         when                                   pushes  readers
============  =====================================  ======  =======
``Open``      from its opening                       yes     yes
``Checking``  while a freeze is checked              wait    yes
``Freezing``  once a freeze is logged, until its     no      yes
              record finishes
``Frozen``    once the record of its freeze          no      no
              completed: that record is read
``Stopped``   once the driver may hold part of a     no      no
              push's rows, or was not made
============  =====================================  ======  =======

A freeze whose record fails or is cancelled leaves the accumulator
``Open``. An accumulator that its client released takes nothing more, in any
phase, but still does the steps that its readers need.

A push *fails* if a record it reads did not complete, or the step raised
before the driver got rows, or the driver raised ``PushRefused`` at the
first table of the push (see :mod:`.backend`). The push then adds no
rows: state k is state k-1, and the accumulator goes on. Any other failure
of a push, and every failure of the opening, stops the accumulator. A failed
push keeps its number, and its reason is kept in ``failed``.

A :class:`Lifecycle` holds no driver, takes no lock, and starts no thread.
Each method takes what it needs to know from the backend, changes the
lifecycle, and returns the actions that the backend then takes, in order,
under its lock. So each transition can be tested on its own:

======================================  =====================================
event                                   actions
======================================  =====================================
:meth:`~Lifecycle.queue`, a step        none
logged
:meth:`~Lifecycle.advance`, the next    ``StartStep``, once the step is not
step may start                          held back; if a record it reads did
                                        not complete, the push fails, or the
                                        opening stops the accumulator
:meth:`~Lifecycle.step_done`            ``LetGo`` what it read, ``Reached``
                                        the state after it
:meth:`~Lifecycle.step_failed`          the push fails, or the accumulator
                                        stops
:meth:`~Lifecycle.freeze_finished`      ``Freezing`` becomes ``Frozen`` if
                                        its record completed, else ``Open``
:meth:`~Lifecycle.release`              ``LetGo`` what the steps that no
                                        reader needs read
:meth:`~Lifecycle.unread`, the last     in ``Frozen`` or ``Stopped``,
reader of a state is done               ``DropDriver``; released, as
                                        :meth:`~Lifecycle.release`
======================================  =====================================

A failed push lets go of what it read, is logged (``LogFailure``), and fails
the records that wait for a state it is part of (``FailStates``): they were
pinned before the push failed, and their requests hold its rows. A stop lets
go of what every step not done reads, fails the records that wait for a
state it will not reach, and drops the driver once no reader holds the
current state (``DropDriver``). After any event, the backend calls
:meth:`~Lifecycle.advance` again, until it returns no action.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Any

from .records import Status


@dataclass(frozen=True)
class Step:
    """
    A step of an accumulator: its values by name, the template's fixed values
    for the opening and the rows of each table for a push, as the log holds
    them; and the outputs of records it reads, by record ID and name, which
    it holds as a reader until it is done.
    """

    values: dict[str, Any]
    reads: list[tuple[str, str]]


@dataclass(frozen=True)
class Open:
    """Takes pushes and readers."""


@dataclass(frozen=True)
class Checking:
    """A freeze is being checked: pushes wait."""


@dataclass(frozen=True)
class Freezing:
    """
    The freeze that made ``record`` is logged, and ``record`` is pending:
    no more pushes or freezes.
    """

    record: str


@dataclass(frozen=True)
class Frozen:
    """``record``, the record of its freeze, completed, and is read instead."""

    record: str


@dataclass(frozen=True)
class Stopped:
    """
    The opening failed, or a push failed in a way that may leave part of its
    rows in the driver, for ``reason``: no more pushes or readers.
    """

    reason: str


Phase = Open | Checking | Freezing | Frozen | Stopped


@dataclass(frozen=True)
class StartStep:
    """Drop the outputs computed for the current state, and do ``step``."""

    step: Step


@dataclass(frozen=True)
class LetGo:
    """Let go of the outputs of records that a step read."""

    reads: list[tuple[str, str]]


@dataclass(frozen=True)
class Reached:
    """Start the records that wait for the state after ``upto`` pushes."""

    upto: int


@dataclass(frozen=True)
class LogFailure:
    """Log that push ``push`` failed, for ``reason``."""

    push: int
    reason: str


@dataclass(frozen=True)
class FailStates:
    """
    Fail, for ``reason``, the records that wait for a state after ``first``
    pushes or more.
    """

    first: int
    reason: str


@dataclass(frozen=True)
class DropDriver:
    """Drop the driver, and the outputs computed for its state."""


Action = StartStep | LetGo | Reached | LogFailure | FailStates | DropDriver


class Lifecycle:
    """
    The phase and the steps of one accumulator (see the module docstring).

    ``added`` is the number of pushes done, added or failed, ``None`` until
    the driver is made. ``steps`` holds the steps logged and not yet started,
    in order, and ``running`` the one a worker does, if any. ``failed`` holds
    the reason of each push that failed, by its number, in the order they
    failed, which is the order of their numbers. ``released`` says the
    client let go of the accumulator.
    """

    def __init__(self) -> None:
        self.phase: Phase = Open()
        self.released = False
        self.added: int | None = None
        self.steps: deque[Step] = deque()
        self.running: Step | None = None
        self.failed: dict[int, str] = {}

    @property
    def step_name(self) -> str:
        """The next step to be done, as a failure names it."""
        return 'opening' if self.added is None else f'push {self.added + 1}'

    @property
    def working(self) -> bool:
        """Whether a step is running or waits to start."""
        return self.running is not None or bool(self.steps)

    @property
    def checking(self) -> bool:
        """Whether a freeze is being checked, which pushes wait for."""
        return isinstance(self.phase, Checking)

    @property
    def unreadable(self) -> str | None:
        """Why the accumulator takes no readers, or ``None``."""
        match self.phase:
            case Stopped(reason):
                return reason
            case Frozen(record):
                return f'the accumulator is frozen: read record {record} instead'
        return None

    def push_refused(self) -> str | None:
        """Why a push is refused, or ``None``; one that waits is not refused."""
        if isinstance(self.phase, Freezing):
            return f'the accumulator was frozen as record {self.phase.record}'
        return self.unreadable

    def freeze_refused(self) -> str | None:
        """Why a freeze is refused, or ``None``."""
        if isinstance(self.phase, Freezing):
            return f'the accumulator is frozen as record {self.phase.record}'
        return self.unreadable

    def is_freeze(self, record: str) -> bool:
        """Whether ``record`` is the record of the accumulator's last freeze."""
        return isinstance(self.phase, Freezing | Frozen) and self.phase.record == record

    def status(self) -> Status:
        """
        Pending while it may take pushes or its freeze is pending, completed
        once frozen, and failed once stopped.
        """
        match self.phase:
            case Frozen():
                return Status.COMPLETED
            case Stopped():
                return Status.FAILED
        return Status.PENDING

    def failure(self) -> str | None:
        """Why the accumulator stopped, or ``None``."""
        return self.phase.reason if isinstance(self.phase, Stopped) else None

    def push_status(self, push: int) -> Status:
        """
        The status of push ``push``, one logged: pending until done,
        completed once its rows are in, failed, or cancelled once it will
        never be done, since the accumulator stopped before it, or was
        released and no reader needs it.
        """
        if push in self.failed:
            return Status.FAILED
        if self.added is not None and push <= self.added:
            return Status.COMPLETED
        if push > self._queued:
            return Status.CANCELLED
        return Status.PENDING

    def push_failure(self, push: int) -> str | None:
        """Why push ``push`` failed, or was cancelled; else ``None``."""
        match self.push_status(push):
            case Status.FAILED:
                return self.failed[push]
            case Status.CANCELLED:
                return self.failure() or 'the accumulator was released'
        return None

    @property
    def _queued(self) -> int:
        """The number of the last push that is done, running, or queued."""
        done = -1 if self.added is None else self.added  # the opening is step 0
        return done + (self.running is not None) + len(self.steps)

    def failed_since(self, known: int, upto: int) -> str | None:
        """
        Why the first push up to ``upto`` failed, among the failures after
        the first ``known``; ``None`` if none did. A reader that pinned the
        state after ``upto`` pushes, when ``known`` pushes had failed, reads
        a state whose rows are gone if one did.
        """
        for push in list(self.failed)[known:]:
            if push <= upto:
                return self.failed[push]
        return None

    def queue(self, step: Step) -> None:
        """A step logged, after those logged before it."""
        self.steps.append(step)

    def advance(self, held: bool, status: Callable[[str], Status]) -> list[Action]:
        """
        Start the next step once nothing holds it back; fail the next push,
        or stop at the opening, if a record it reads did not complete.

        ``held`` says a reader holds the current state, which holds back a
        push. ``status`` gives the status of a record, and a step waits until
        the records it reads have completed. A failed push is one step, so
        the caller advances again until no action is returned.
        """
        if self.running is not None or not self.steps:
            return []
        if isinstance(self.phase, Frozen | Stopped):
            return []
        if self.added is not None and held:
            return []
        for record, _ in self.steps[0].reads:
            match status(record):
                case Status.PENDING:
                    return []
                case Status.COMPLETED:
                    continue
                case other:
                    reason = f'{self.step_name}: record {record} {other}'
                    if self.added is None:
                        return self._stop(reason, held)
                    return self._fail(self.steps.popleft(), reason)
        self.running = self.steps.popleft()
        return [StartStep(self.running)]

    def step_done(self) -> list[Action]:
        """The running step is done: the state after it is reached."""
        step = self._take()
        self.added = 0 if self.added is None else self.added + 1
        return [LetGo(step.reads), Reached(self.added)]

    def step_failed(self, reason: str, *, fatal: bool, held: bool) -> list[Action]:
        """
        The running step failed, for ``reason``. A push that is not
        ``fatal`` fails alone; otherwise the accumulator stops, since the
        driver may hold part of the rows, or was not made. ``held`` says a
        reader holds the current state.
        """
        reason = f'{self.step_name} failed: {reason}'
        if self.added is None:
            self._take()
            return self._stop(reason, held)
        if not fatal:
            return self._fail(self._take(), reason)
        push = self.added + 1
        self.failed[push] = reason
        return [LogFailure(push, reason), *self._stop(reason, held)]

    def _take(self) -> Step:
        step, self.running = self.running, None
        if step is None:
            raise RuntimeError('no step is running')
        return step

    def _fail(self, step: Step, reason: str) -> list[Action]:
        """The next push, ``step``, fails: state k is state k-1."""
        if self.added is None:
            raise RuntimeError('the opening stops the accumulator')
        push = self.added + 1
        self.added = push
        self.failed[push] = reason
        return [LetGo(step.reads), LogFailure(push, reason), FailStates(push, reason)]

    def _stop(self, reason: str, held: bool) -> list[Action]:
        stopped = f'the accumulator stopped: {reason}'
        self.phase = Stopped(stopped)
        steps = [*([self.running] if self.running is not None else []), *self.steps]
        self.running = None
        self.steps.clear()
        actions: list[Action] = [LetGo(step.reads) for step in steps]
        actions.append(FailStates(0, stopped))
        if not held:
            actions.append(DropDriver())
        return actions

    def start_check(self) -> None:
        """A freeze is being checked; the caller checked it is not refused."""
        if not isinstance(self.phase, Open):
            raise RuntimeError(f'a freeze in {self.phase}')
        self.phase = Checking()

    def end_check(self, record: str | None) -> None:
        """
        The check of a freeze ended: the freeze is logged as ``record``, or
        is refused if ``None``. Ending it again does nothing, and so does a
        stop meanwhile.
        """
        if isinstance(self.phase, Checking):
            self.phase = Open() if record is None else Freezing(record)

    def freeze_finished(self, record: str, completed: bool) -> list[Action]:
        """
        A record of a state finished. If it is the record of the freeze, the
        accumulator is read through it if it ``completed``, and takes pushes
        and freezes again if not.
        """
        if self.phase == Freezing(record):
            self.phase = Frozen(record) if completed else Open()
        return []

    def release(self, pinned: Collection[int]) -> list[Action]:
        """
        The client let go of the accumulator. ``pinned`` holds the states
        that readers hold, by their number of pushes.
        """
        self.released = True
        return self._unneeded(pinned)

    def _unneeded(self, pinned: Collection[int]) -> list[Action]:
        """
        Drop the steps of a released accumulator that no reader needs: a
        reader needs the steps up to the state it pinned. The step that
        runs cannot be stopped, so it stays. The records of states that
        wait for a dropped step have all been cancelled, since a pending one
        is a reader.
        """
        done = -1 if self.added is None else self.added  # the opening is step 0
        needed = max(pinned, default=done) - done - (self.running is not None)
        dropped: list[Action] = []
        while len(self.steps) > max(needed, 0):
            dropped.append(LetGo(self.steps.pop().reads))
        return dropped

    def unread(self, upto: int, pinned: Callable[[], Collection[int]]) -> list[Action]:
        """
        The last reader of the state after ``upto`` pushes is done.
        ``pinned`` gives the states that readers still hold.

        An accumulator that takes no more readers drops its driver once no
        reader holds its current state.
        """
        match self.phase:
            case Frozen() | Stopped():
                return [DropDriver()] if upto == self.added else []
        if self.released:
            return self._unneeded(pinned())
        return []
