# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Automatic reduction: a batch amended from the files and reduced as they
arrive. The guide is the section "Follow" of
``packages/essapps/docs/batch.md``; the decision is
``docs/developer/adr/0013-follow-adds-the-rows-not-considered.md``.

:func:`step` is one step on one batch. Follow is a step on the batch that the
intake keeps as the one that follows, every few seconds
(:meth:`~.intake.Intake.step`); whoever serves owns the timer that
calls it.

A step adds the rows of the source that are new and filled, as a person does
with ``put``, then reduces the rows that are ready and have no result::

    given = rows_of(batch.client, source)        # every row the source makes now
    new, waiting = added(table, given, batch.fills, considered)
    batch.put(pd.concat([table, new]))
    considered |= set(new.index) | (set(given.index) & set(table.index))
    candidates = not_reduced(batch.state(compare=False))   # ready as given, no result
    batch.reduce(not_reduced(batch.state(candidates)))

A step never changes a row that the batch table has, and never changes the blanks
of the template: it takes only the columns that the batch table has. A row whose
key is considered, such as one a person removed, is never added again; the
caller keeps :attr:`Step.considered` and gives it to the next step (``Intake``
in ``source.json``). A key of the source that the batch table has is considered,
whoever added the row, and the step keeps it so. So a row that a person added
by hand with a key that the source gives later stays away once removed, and
so does a row of a step cut off between ``put`` and keeping ``considered``:
the next step considers it.

The rule of what a step reduces is on the state, not on who changed it:
:func:`~.state.not_reduced` gives the ready rows that have no result under the
label. A row with a result is never reduced by a step, whether it is outdated,
failed or was cancelled; a person reduces it. So a row is reduced by a step
once, as soon as it is complete, whether the step or a person put it into the
batch; and a step after a restart reduces no row again. :attr:`Step.refused`
says why rows of a step were refused, and a refused row is tried again at
every step.

A step costs the listing of all files, and the results of all rows: the status
of every row's result, and the problems of every row's cells as given
(``batch.state(compare=False)``). It resolves the settings, and the cells of
the rows without a result only (:meth:`~.reduction.Batch.state` of those
keys), since a row with a result is not reduced, whatever its comparison
says.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass

import pandas as pd

from ..errors import SubmitError, said
from ..records import Record
from .reduction import Batch
from .source import RowSource, filled, rows_of
from .state import not_reduced


class SourceError(ValueError):
    """
    The source of a step cannot list its rows, or the batch refuses the rows
    that it made: a defect of the code of the application.
    """


def added(
    table: pd.DataFrame,
    given: pd.DataFrame,
    fills: Collection[str],
    considered: Collection[str],
) -> tuple[pd.DataFrame, pd.Index]:
    """
    The rows of ``given`` to add to ``table``, and the keys of those that wait.

    A row of ``given`` is new if its key is neither considered nor in
    ``table``. A new row is added once it has a value in each of ``fills``,
    the parameters that the batch waits for (:attr:`.reduction.Batch.fills`),
    and waits until then. The rows added have the columns of ``table``, those
    of ``given`` that it lacks left out.
    """
    is_new = ~given.index.isin(list(considered)) & ~given.index.isin(table.index)
    is_ready = filled(given, fills)
    new = given[is_new & is_ready].reindex(columns=table.columns)
    return new, given.index[is_new & ~is_ready]


@dataclass(frozen=True)
class Step:
    """
    What a step did.

    Parameters
    ----------
    considered:
        The keys considered after the step: those given to it, those of the
        rows it added, and those of the source that the batch table has.
    refused:
        Why the submission refused each row, by key: the rows submitted that
        a ``SubmitError`` names, or every row submitted if the reason names
        none. If the settings are refused, every row without a result that is
        ready as given: no cell is resolved while the settings are refused, so
        a row whose cell would be refused is named too.
    waiting:
        The keys of the rows of the source that the step did not add since
        they are not filled yet.
    records:
        The records of the rows reduced, by key.
    """

    considered: frozenset[str]
    refused: Mapping[str, str]
    waiting: tuple[str, ...]
    records: Mapping[str, Record]


def step(batch: Batch, source: RowSource, considered: Collection[str]) -> Step:
    """
    Add the rows of ``source`` that are new and filled to ``batch``, and
    reduce the rows that :func:`~.state.not_reduced` gives, with the batch's
    settings as they are at the step. A row whose request is refused does not
    hold back the others, and :attr:`Step.refused` says why. See the module.

    Parameters
    ----------
    batch:
        The batch. It should persist what it submits (``persist=True``),
        since nothing releases its records.
    source:
        The source that the rows are added from.
    considered:
        The keys of the source that the batch has considered: taken, or left
        out when it was made. The step adds no row of these keys.

    Raises
    ------
    SourceError
        If the source raises ``ValueError``, or the batch refuses the rows it
        made; nothing is added.
    """
    table = batch.frame()
    try:
        given = rows_of(batch.client, source)
        new, waiting = added(table, given, batch.fills, considered)
        if len(new):
            batch.put(pd.concat([table, new]))
    except ValueError as error:
        raise SourceError(said(error)) from error
    now = frozenset(considered) | set(new.index) | (set(given.index) & set(table.index))
    # Only the rows without a result are compared: a step reduces no other.
    candidates = not_reduced(batch.state(compare=False))
    try:
        state = batch.state(candidates)
    except SubmitError as error:
        return Step(now, dict.fromkeys(candidates, str(error)), tuple(waiting), {})
    records, refused = _reduce(batch, not_reduced(state))
    return Step(now, refused, tuple(waiting), records)


def _reduce(batch: Batch, keys: list[str]) -> tuple[dict[str, Record], dict[str, str]]:
    """
    Submit ``keys``, less those a refusal names, or none if a refusal names
    none; the records by key, and why each row was refused.
    """
    refused: dict[str, str] = {}
    while keys:
        try:
            return batch.reduce(keys), refused
        except SubmitError as error:  # noqa: PERF203
            named = set(keys).intersection(e.entry for e in error.errors)
            if not named:
                return {}, refused | dict.fromkeys(keys, str(error))
            for key in named:
                errors = [e for e in error.errors if e.entry == key]
                refused[key] = str(SubmitError(errors))
            keys = [k for k in keys if k not in named]
    return {}, refused
