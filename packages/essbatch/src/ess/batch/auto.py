# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
One step of Follow on a batch::

    table = batch.frame()
    given = rows_of(batch.client, source)                  # lists every file
    new, waiting = added(table, given, batch.fills, considered)
    batch.put(pd.concat([table, new]))
    considered |= set(new.index) | (set(given.index) & set(table.index))
    candidates = not_reduced(batch.state(compare=False))   # resolves no cell
    batch.reduce(not_reduced(batch.state(candidates)))     # resolves their cells

The caller keeps ``considered`` from one step to the next
(:class:`~.intake.Intake` in ``source.json``).

Invariants:

- A step never changes a row that the batch table has, nor the template's blanks.
- A step never adds a row whose key is considered.
- A step never reduces a row that has a result.

Guide: ``packages/essbatch/docs/batch.md``, "Follow".
Decision: ``docs/developer/adr/0013-follow-adds-the-rows-not-considered.md``.
Cost: ``docs/developer/automatic-reduction.md``, "Cost".
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass

import pandas as pd

from ess.apps.errors import SubmitError, said
from ess.apps.records import Record

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
    The rows of ``given`` to add to ``table``, with the columns of ``table``,
    and the keys of the rows that wait.

    A row is added if its key is neither considered nor in ``table`` and it
    is filled: it has a value in each of ``fills``. A row with such a key
    that is not filled waits.
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
    added:
        The keys of the rows that the step added, in the order of the batch
        table.
    considered:
        The keys considered after the step: those given to it, those of the
        rows it added, and those of the source that the batch table has.
    refused:
        Why the submission refused each row, by key: the rows submitted that
        a ``SubmitError`` names, or every row submitted if the reason names
        none. If the settings are refused, every row without a result that is
        ready as given, also a row whose cell would be refused, since no cell
        is resolved then.
    waiting:
        The keys of the new rows that are not filled yet.
    records:
        The records of the rows reduced, by key.
    """

    added: tuple[str, ...]
    considered: frozenset[str]
    refused: Mapping[str, str]
    waiting: tuple[str, ...]
    records: Mapping[str, Record]


def step(batch: Batch, source: RowSource, considered: Collection[str]) -> Step:
    """
    Add the rows of ``source`` that are new and filled to ``batch``, then
    reduce the rows of :func:`~.state.not_reduced`. A row whose request is
    refused does not hold back the others: :attr:`Step.refused` says why.

    Parameters
    ----------
    batch:
        The batch. It should persist what it submits (``persist=True``),
        since nothing releases its records.
    source:
        The source that the rows are added from.
    considered:
        The keys of the source that the batch has considered.

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
        refused = dict.fromkeys(candidates, str(error))
        return Step(tuple(new.index), now, refused, tuple(waiting), {})
    records, refused = _reduce(batch, not_reduced(state))
    return Step(tuple(new.index), now, refused, tuple(waiting), records)


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
