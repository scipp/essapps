# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The lifecycle of an accumulator: when each of its steps starts, and what
each event does to it.

An accumulator's *steps* are its opening, which makes the driver, and then
its pushes, in the order logged. The backend does them one at a time on its
workers. Its *phase* says what the accumulator takes:

============  =====================================  ======  =======
phase         when                                   pushes  readers
============  =====================================  ======  =======
``Open``      from its opening                       yes     yes
``Checking``  while a freeze is checked              wait    yes
``Freezing``  once a freeze is logged                no      yes
``Frozen``    once the record of its freeze          no      no
              completed: that record is read
``Stopped``   once a step failed                     no      no
============  =====================================  ======  =======

A freeze may be made in ``Freezing`` once the record of the last freeze
failed or was cancelled. An accumulator that its client released takes
nothing more, in any phase, but still does the steps that its readers need.

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
                                        not complete, the accumulator stops
:meth:`~Lifecycle.step_done`            ``LetGo`` what it read, ``Reached``
                                        the state after it
:meth:`~Lifecycle.step_failed`          the accumulator stops
:meth:`~Lifecycle.freeze_finished`      ``Freezing`` becomes ``Frozen`` once
                                        its record completed
:meth:`~Lifecycle.release`              ``LetGo`` what the steps that no
                                        reader needs read
:meth:`~Lifecycle.unread`, the last     in ``Frozen`` or ``Stopped``,
reader of a state is done               ``DropDriver``; in ``Freezing`` with
                                        no steps left, ``DropOutputs``;
                                        released, as :meth:`~Lifecycle.release`
======================================  =====================================

A stop lets go of what every step not done reads, fails the records that
wait for a state it will not reach (``FailStates``), and drops the driver
once no reader holds the current state (``DropDriver``). After any event,
the backend calls :meth:`~Lifecycle.advance` again.
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
    """
    A freeze is being checked: pushes wait. ``previous`` is the phase it
    goes back to if the freeze is refused.
    """

    previous: Open | Freezing


@dataclass(frozen=True)
class Freezing:
    """The freeze that made ``record`` is logged: no more pushes."""

    record: str


@dataclass(frozen=True)
class Frozen:
    """``record``, the record of its freeze, completed, and is read instead."""

    record: str


@dataclass(frozen=True)
class Stopped:
    """A step failed, for ``reason``: no more pushes or readers."""

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
class FailStates:
    """
    Fail, for ``reason``, the records that wait for a state after ``first``
    pushes or more.
    """

    first: int
    reason: str


@dataclass(frozen=True)
class DropOutputs:
    """Drop the outputs computed for the current state."""


@dataclass(frozen=True)
class DropDriver:
    """Drop the driver, and the outputs computed for its state."""


Action = StartStep | LetGo | Reached | FailStates | DropOutputs | DropDriver


class Lifecycle:
    """
    The phase and the steps of one accumulator (see the module docstring).

    ``added`` is the number of pushes done, ``None`` until the driver is
    made. ``steps`` holds the steps logged and not yet started, in order, and
    ``running`` the one a worker does, if any. ``released`` says the client
    let go of the accumulator.
    """

    def __init__(self) -> None:
        self.phase: Phase = Open()
        self.released = False
        self.added: int | None = None
        self.steps: deque[Step] = deque()
        self.running: Step | None = None

    @property
    def step_name(self) -> str:
        """The next step to be done, as a failure names it."""
        return 'opening' if self.added is None else f'push {self.added}'

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

    def freeze_refused(self, status: Callable[[str], Status]) -> str | None:
        """
        Why a freeze is refused, or ``None``; ``status`` gives the status of
        a record.
        """
        match self.phase:
            case Freezing(record) if status(record) is Status.PENDING:
                return f'the accumulator is frozen as record {record}'
        return self.unreadable

    def is_freeze(self, record: str) -> bool:
        """Whether ``record`` is the record of the accumulator's last freeze."""
        return isinstance(self.phase, Freezing | Frozen) and self.phase.record == record

    def queue(self, step: Step) -> None:
        """A step logged, after those logged before it."""
        self.steps.append(step)

    def advance(self, held: bool, status: Callable[[str], Status]) -> list[Action]:
        """
        Start the next step once nothing holds it back, or stop if it cannot
        be done.

        ``held`` says a reader holds the current state, which holds back a
        push. ``status`` gives the status of a record, and a step waits until
        the records it reads have completed.
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
                    return self._stop(reason, held)
        self.running = self.steps.popleft()
        return [StartStep(self.running)]

    def step_done(self) -> list[Action]:
        """The running step is done: the state after it is reached."""
        step, self.running = self.running, None
        if step is None:
            raise RuntimeError('no step is running')
        self.added = 0 if self.added is None else self.added + 1
        return [LetGo(step.reads), Reached(self.added)]

    def step_failed(self, reason: str, held: bool) -> list[Action]:
        """
        The running step failed, for ``reason``: the accumulator stops.
        ``held`` says a reader holds the current state.
        """
        return self._stop(f'{self.step_name} failed: {reason}', held)

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
        if not isinstance(self.phase, Open | Freezing):
            raise RuntimeError(f'a freeze in {self.phase}')
        self.phase = Checking(self.phase)

    def end_check(self, record: str | None) -> None:
        """
        The check of a freeze ended: the freeze is logged as ``record``, or
        is refused if ``None``. Ending it again does nothing, and so does a
        stop meanwhile.
        """
        if isinstance(self.phase, Checking):
            self.phase = self.phase.previous if record is None else Freezing(record)

    def freeze_finished(self, record: str, completed: bool) -> list[Action]:
        """
        A record of a state finished; if it is the record of the last
        freeze and ``completed``, the accumulator is read through it.
        """
        if completed and self.phase == Freezing(record):
            self.phase = Frozen(record)
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
        reader holds its current state; one that takes no more pushes drops
        the outputs of its last state, since no push comes to drop them.
        """
        match self.phase:
            case Frozen() | Stopped():
                return [DropDriver()] if upto == self.added else []
            case Freezing() if not self.working and upto == self.added:
                return [DropOutputs()]
        if self.released:
            return self._unneeded(pinned())
        return []
