# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Automatic reduction: a batch reduced as its files arrive. The guide is the
section "Automatic reduction" of ``packages/essapps/docs/batch.md``.
:class:`Automatic` steps one batch. Follow is a step of it on the batch that
the manager keeps as the one that follows, every few seconds::

    label = manager.following
    if label is not None:
        Automatic(manager.batch(label)).step()

Whoever serves owns the timer that calls it.

A step does what a person does with the buttons of a batch's page to scan and
to reduce, with the rule :func:`~.state.not_reduced` in place of the person's
choice::

    batch.scan()
    candidates = not_reduced(batch.results())    # ready as given, no result
    batch.reduce(not_reduced(batch.state(candidates)))

Which rows a step reduces follows from the files, the batch's rows and
settings, and the records under its label. :class:`Automatic` keeps nothing
that changes it: :attr:`Automatic.refused` says why rows of the last step were
refused, and a refused row is tried again at every step. So a step that raises
loses nothing.

The rule is on the state, not on who changed it: :func:`~.state.not_reduced`
gives the ready rows that have no result under the label. A row with a result
is never reduced by a step, whether it is outdated, failed or was cancelled; a
person reduces it. So a row is reduced by a step once, as soon as it is
complete, whether the scan or a person put it into the batch; and a restarted
loop reduces no row again.

A step costs the scan of all files, and the results of all rows: the status of
every row's result, and the problems of every row's cells as given
(:meth:`~.reduction.Batch.results`). It resolves the settings, and the cells
of the rows without a result only (:meth:`~.reduction.Batch.state` of those
keys), since a row with a result is not reduced, whatever its comparison
says.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager

from ..errors import SubmitError
from ..records import Record
from .reduction import Batch
from .state import not_reduced


class Automatic:
    """
    Reduces the rows of ``batch`` that :func:`~.state.not_reduced` gives at
    each step. See ``packages/essapps/docs/batch.md``.

    Parameters
    ----------
    batch:
        The batch, with a source. It should persist what it submits
        (``persist=True``), since nothing releases its records.

    Attributes
    ----------
    refused:
        Why the last step's submission refused each row, by key: the rows it
        submitted that a ``SubmitError`` names, or every row it submitted if
        the reason names none. If the settings are refused, every row without a
        result that is ready as given: no cell is resolved while the settings
        are refused, so a row whose cell would be refused is named too.

    Raises
    ------
    ValueError
        If ``batch`` has no source.
    """

    def __init__(self, batch: Batch) -> None:
        if batch.source is None:
            raise ValueError('this batch has no source of rows')
        self.batch = batch
        self.refused: dict[str, str] = {}

    def step(self) -> dict[str, Record]:
        """
        Scan, and reduce the rows that :func:`~.state.not_reduced` gives, with
        the batch's settings as they are at the step; their records by key. A
        row whose request is refused does not hold back the others, and
        :attr:`refused` says why.

        If the settings are refused, no row can run: the step reduces none,
        and :attr:`refused` gives the reason for each row without a result
        that is ready as given, its cells not resolved.
        """
        batch = self.batch
        batch.scan()
        # Only the rows without a result are compared: a step reduces no other.
        candidates = not_reduced(batch.results())
        try:
            state = batch.state(candidates)
        except SubmitError as error:
            self.refused = dict.fromkeys(candidates, str(error))
            return {}
        records, self.refused = self._reduce(not_reduced(state))
        return records

    def _reduce(self, keys: list[str]) -> tuple[dict[str, Record], dict[str, str]]:
        """
        Submit ``keys``, less those a refusal names, or none if a refusal names
        none; the records by key, and why each row was refused.
        """
        refused: dict[str, str] = {}
        while keys:
            try:
                return self.batch.reduce(keys), refused
            except SubmitError as error:  # noqa: PERF203
                named = set(keys).intersection(e.entry for e in error.errors)
                if not named:
                    return {}, refused | dict.fromkeys(keys, str(error))
                for key in named:
                    errors = [e for e in error.errors if e.entry == key]
                    refused[key] = str(SubmitError(errors))
                keys = [k for k in keys if k not in named]
        return {}, refused


def run(loops: Sequence[Automatic], interval: float = 1.0) -> None:
    """
    Step each of ``loops`` in the order given, then wait ``interval`` seconds,
    until a step raises. A batch whose source reads the results of another
    comes after it.
    """
    while True:
        for loop in loops:
            loop.step()
        time.sleep(interval)


@contextmanager
def every(seconds: float, actions: Iterable[Callable[[], object]]) -> Iterator[None]:
    """
    Call ``actions`` in turn from a thread, one every ``seconds``, until all are
    called or the block ends. The end of the block waits for an action that
    runs. A demo makes files arrive with it while it serves a page.
    """
    stop = threading.Event()

    def call() -> None:
        for action in actions:
            if stop.wait(seconds):
                return
            action()

    thread = threading.Thread(target=call, name='every')
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
