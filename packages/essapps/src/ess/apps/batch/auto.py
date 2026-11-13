# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Automatic reduction: a batch reduced as its files arrive. The guide is the
section "Automatic reduction" of ``packages/essapps/docs/batch.md``.

Which files the records of a row hold at the trigger column is read from the
proposal's changes (:meth:`~ess.apps.Client.changes`), from the start when an
:class:`Automatic` is made, so a restart needs nothing on disk. What a step
keeps besides, the rows and the settings of the last step, only spares it
work. A step looks at the rows whose cells changed, whose records changed, and,
if the settings changed, the rows that were refused. It costs the scan of all
files, and the state and submission of the rows it looks at, which
:meth:`Batch.state <ess.apps.batch.Batch.state>` works out for those rows only.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ess.spec import FileRef
from ess.spec.refs import iter_refs

from ..errors import SubmitError
from ..records import Record
from .reduction import Batch
from .table import Row


class Automatic:
    """
    Reduces each row of ``batch`` that holds a new file at the column ``at``.
    See ``packages/essapps/docs/batch.md``.

    Parameters
    ----------
    batch:
        The batch, with a source. It should persist what it submits
        (``persist=True``), since nothing releases its records.
    at:
        The trigger column, of a file or a list of files.
    settings:
        Gives the settings. It is called at each step, so that a change
        applies to the rows not reduced yet.

    Raises
    ------
    KeyError
        If ``at`` is not a column of ``batch``.
    """

    def __init__(
        self,
        batch: Batch,
        *,
        at: str,
        settings: Callable[[], Mapping[str, Any]],
    ) -> None:
        if at not in batch.columns:
            raise KeyError(f'{at!r} is not a column of the batch')
        self.batch = batch
        self.at = at
        self.settings = settings
        self.refused: dict[str, str] = {}
        """Why each row with a new file at ``at`` did not run, by key."""
        self._cursor = 0
        self._held: dict[str, set[FileRef]] = {}
        self._rows: dict[str, Row] = {}
        self._settings: Mapping[str, Any] | None = None

    def step(self) -> dict[str, Record]:
        """
        Scan, and reduce the rows with a new file at ``at``; their records by
        key. A row that cannot run, or whose request is refused, does not hold
        back the others, and :attr:`refused` says why.
        """
        batch = self.batch
        changes = batch.client.changes(after=self._cursor)
        self._cursor = changes.cursor
        touched: set[str] = set()
        for record in batch.client.records(ids=changes.records):
            if record.label == batch.label and record.member is not None:
                held = self._held.setdefault(record.member, set())
                held |= _files_at(record.request.params, self.at)
                touched.add(record.member)
        batch.scan()
        rows = {row.key: row for row in batch.rows}
        settings = self.settings()
        look_at = {k for k, row in rows.items() if self._rows.get(k) != row}
        look_at |= touched & rows.keys()
        if settings != self._settings:
            look_at |= self.refused.keys() & rows.keys()
        # A copy, so that settings changed in place count as changed.
        self._rows, self._settings = rows, copy.deepcopy(settings)
        self.refused = {
            k: why for k, why in self.refused.items() if k in rows and k not in look_at
        }
        todo = [
            k
            for k in rows  # in the order of the rows
            if k in look_at
            and _files_at(rows[k].values, self.at) - self._held.get(k, set())
        ]
        return self._reduce(settings, todo)

    def _reduce(
        self, settings: Mapping[str, Any], keys: list[str]
    ) -> dict[str, Record]:
        """Submit ``keys``, less those a refusal names; none if it names none."""
        while keys:
            try:
                return self.batch.reduce(settings, keys)
            except SubmitError as error:  # noqa: PERF203
                named = set(keys).intersection(e.entry for e in error.errors)
                if not named:
                    self.refused |= dict.fromkeys(keys, str(error))
                    return {}
                for key in named:
                    errors = [e for e in error.errors if e.entry == key]
                    self.refused[key] = str(SubmitError(errors))
                keys = [k for k in keys if k not in named]
        return {}


def run(autos: Sequence[Automatic], interval: float = 1.0) -> None:
    """
    Step each of ``autos`` in the order given, then wait ``interval`` seconds,
    forever. A batch whose source reads the results of another comes after it.
    """
    while True:
        for auto in autos:
            auto.step()
        time.sleep(interval)


def _files_at(params: Mapping[str, Any], at: str) -> set[FileRef]:
    """The files at the parameter ``at``: its value, or each element of its list."""
    return {
        ref
        for loc, ref in iter_refs({at: params.get(at)})
        if isinstance(ref, FileRef)
        and (len(loc) == 1 or (len(loc) == 2 and isinstance(loc[1], int)))
    }
