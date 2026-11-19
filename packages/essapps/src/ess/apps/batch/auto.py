# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Automatic reduction: a batch amended from the files and reduced as they
arrive. The guide is the section "Automatic reduction" of
``packages/essapps/docs/batch.md``; the decision is
``docs/developer/adr/0013-follow-adds-the-rows-not-considered.md``.

:class:`Automatic` steps one batch. Follow is a step of it on the batch that
the manager keeps as the one that follows, every few seconds
(:meth:`~.manager.BatchManager.step`); whoever serves owns the timer that
calls it.

A step adds the rows of the source that are new and filled, as a person does
with ``put``, then reduces the rows that are ready and have no result::

    given = rows_of(batch.client, source)        # every row the source makes now
    new, waiting = added(batch.frame(), given, source.fills, considered)
    batch.put(pd.concat([batch.frame(), new]))
    considered |= set(new.index)
    candidates = not_reduced(batch.results())    # ready as given, no result
    batch.reduce(not_reduced(batch.state(candidates)))

A step never changes a row that the table has, and never changes the blanks of
the template: it takes only the columns that the table has. A row whose key is
considered, such as one a person removed, is never added again; the caller
keeps :attr:`Automatic.considered` (the manager in ``source.json``). A key
that the table has counts as considered, so a step cut off between ``put`` and
keeping ``considered`` adds no row twice.

The rule of what a step reduces is on the state, not on who changed it:
:func:`~.state.not_reduced` gives the ready rows that have no result under the
label. A row with a result is never reduced by a step, whether it is outdated,
failed or was cancelled; a person reduces it. So a row is reduced by a step
once, as soon as it is complete, whether the step or a person put it into the
batch; and a restarted loop reduces no row again. :attr:`Automatic.refused`
says why rows of the last step were refused, and a refused row is tried again
at every step.

A step costs the listing of all files, and the results of all rows: the status
of every row's result, and the problems of every row's cells as given
(:meth:`~.reduction.Batch.results`). It resolves the settings, and the cells
of the rows without a result only (:meth:`~.reduction.Batch.state` of those
keys), since a row with a result is not reduced, whatever its comparison
says.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Collection, Iterable, Iterator
from contextlib import contextmanager

import pandas as pd

from ..errors import SubmitError
from ..records import Record
from .reduction import Batch
from .source import Source, rows_of
from .state import not_reduced


def added(
    table: pd.DataFrame,
    given: pd.DataFrame,
    fills: Collection[str],
    considered: Collection[str],
) -> tuple[pd.DataFrame, pd.Index]:
    """
    The rows of ``given`` to add to ``table``, and the keys of those that wait.

    A row of ``given`` is new if its key is neither considered nor in
    ``table``. A new row is added once it has a value in each of ``fills``
    that is a column of ``table``, and waits until then. The rows added have
    the columns of ``table``, those of ``given`` that it lacks left out.
    """
    filled = [c for c in fills if c in table.columns]
    is_new = ~given.index.isin(list(considered)) & ~given.index.isin(table.index)
    is_ready = given[filled].notna().all(axis=1)
    new = given[is_new & is_ready].reindex(columns=table.columns)
    return new, given.index[is_new & ~is_ready]


class Automatic:
    """
    Adds the new rows of ``source`` to ``batch`` at each step, and reduces
    the rows that :func:`~.state.not_reduced` gives. See
    ``packages/essapps/docs/batch.md``.

    Parameters
    ----------
    batch:
        The batch. It should persist what it submits (``persist=True``),
        since nothing releases its records.
    source:
        The source that the rows are added from.
    considered:
        The keys of the source that the batch has considered: taken, or left
        out when it was made. A step adds no row of these keys.

    Attributes
    ----------
    considered:
        The keys given, and those of the rows that the steps added.
    refused:
        Why the last step's submission refused each row, by key: the rows it
        submitted that a ``SubmitError`` names, or every row it submitted if
        the reason names none. If the settings are refused, every row without a
        result that is ready as given: no cell is resolved while the settings
        are refused, so a row whose cell would be refused is named too.
    waiting:
        The keys of the rows of the source that the last step did not add
        since they are not filled yet.
    """

    def __init__(
        self, batch: Batch, source: Source, *, considered: Collection[str] = ()
    ) -> None:
        self.batch = batch
        self.source = source
        self.considered = frozenset(considered)
        self.refused: dict[str, str] = {}
        self.waiting: tuple[str, ...] = ()

    def step(self) -> dict[str, Record]:
        """
        Add the rows of the source that are new and filled, and reduce the
        rows that :func:`~.state.not_reduced` gives, with the batch's settings
        as they are at the step; their records by key. A row whose request is
        refused does not hold back the others, and :attr:`refused` says why.

        If the settings are refused, no row can run: the step reduces none,
        and :attr:`refused` gives the reason for each row without a result
        that is ready as given, its cells not resolved.
        """
        batch = self.batch
        table = batch.frame()
        given = rows_of(batch.client, self.source)
        new, waiting = added(table, given, self.source.fills, self.considered)
        if len(new):
            batch.put(pd.concat([table, new]))
        self.considered |= set(new.index)
        self.waiting = tuple(waiting)
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
