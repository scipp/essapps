# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Automatic reduction: a batch reduced as its files arrive. The guide is the
section "Automatic reduction" of ``packages/essapps/docs/batch.md``.

Which files the records of each key hold at the trigger column is read from
the proposal's changes (:meth:`~ess.apps.Client.changes`), from the start when
an :class:`Automatic` is made, so a restart needs nothing on disk. Every step
works out from these files which rows run, so a step that raises, in the
changes, the settings, the source, or a submission, loses nothing: the next
step tries again. Besides, a step keeps only the rows that were refused, as
they were then, and the settings: a refused row is tried again once its cells,
its records, or the settings change.

A step costs the scan of all files and a check of each row's cell at the
trigger column. The state of the rows it submits reads every record under the
batch's label, though it works out the status of those rows only.
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
        if batch.source is None:
            raise ValueError('this batch has no source of rows')
        self.batch = batch
        self.at = at
        self.settings = settings
        self._cursor = 0
        # The identity strings, which are equal only if their files are.
        self._held: dict[str, set[str]] = {}
        self._refused: dict[str, tuple[Row, str]] = {}
        # Rows whose record did not hold their cell at `at`, as they were then.
        self._unheld: dict[str, tuple[Row, str]] = {}
        self._settings: Mapping[str, Any] | None = None

    @property
    def refused(self) -> dict[str, str]:
        """Why each row with a new file at ``at`` did not run, by key."""
        return {key: why for key, (_, why) in self._refused.items()}

    def step(self) -> dict[str, Record]:
        """
        Scan, and reduce the rows with a new file at ``at``; their records by
        key. A row that cannot run, or whose request is refused, does not hold
        back the others, and :attr:`refused` says why.

        Raises
        ------
        ValueError
            If the record of a row does not hold the files of the row's cell
            at ``at``, such as a cell that names a file by ``file(run=...)``,
            which the workspace resolves: the next step would submit the row
            again, so the loop stops. Each later step raises too, before it
            submits anything, until that cell changes.
        """
        batch = self.batch
        changes = batch.client.changes(after=self._cursor)
        for record in batch.client.records(ids=changes.records):
            if record.label == batch.label and record.member is not None:
                held = self._held.setdefault(record.member, set())
                held |= _files_at(record.request.params, self.at)
                self._refused.pop(record.member, None)
        self._cursor = changes.cursor
        settings = self.settings()
        if settings != self._settings:
            self._refused.clear()
            # A copy, so that settings changed in place count as changed.
            self._settings = copy.deepcopy(settings)
        batch.scan()
        rows = batch.rows
        self._refused = {
            key: (row, why)
            for key, (row, why) in self._refused.items()
            if key in rows and rows[key] == row
        }
        todo = [
            row.key
            for row in rows
            if row.key not in self._refused
            and _new(row.values.get(self.at), self._held.get(row.key, set()))
        ]
        self._unheld = {
            key: (row, why)
            for key, (row, why) in self._unheld.items()
            if key in rows and rows[key] == row
        }
        if stuck := [why for key, (_, why) in self._unheld.items() if key in todo]:
            raise ValueError('\n'.join(stuck))
        records = self._reduce(settings, todo)
        self._check(records)
        return records

    def _check(self, records: Mapping[str, Record]) -> None:
        """Raise if a record does not hold the files of its row's cell at ``at``."""
        rows = self.batch.rows
        unheld = {}
        for key, record in records.items():
            cell = _cell_files(rows[key].values.get(self.at))
            if missing := cell - _files_at(record.request.params, self.at):
                unheld[key] = (rows[key], _unheld(key, self.at, missing))
        if unheld:
            self._unheld |= unheld
            raise ValueError('\n'.join(why for _, why in unheld.values()))

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
                    self._refuse(keys, str(error))
                    return {}
                for key in named:
                    errors = [e for e in error.errors if e.entry == key]
                    self._refuse([key], str(SubmitError(errors)))
                keys = [k for k in keys if k not in named]
        return {}

    def _refuse(self, keys: list[str], why: str) -> None:
        rows = self.batch.rows
        self._refused |= {key: (rows[key], why) for key in keys}


def run(autos: Sequence[Automatic], interval: float = 1.0) -> None:
    """
    Step each of ``autos`` in the order given, then wait ``interval`` seconds,
    forever. A batch whose source reads the results of another comes after it.
    """
    while True:
        for auto in autos:
            auto.step()
        time.sleep(interval)


def _new(cell: Any, held: set[str]) -> bool:
    """Whether ``cell`` is a file, or a list with a file, that ``held`` lacks."""
    if isinstance(cell, FileRef):
        return cell.file not in held
    if isinstance(cell, list):
        return any(isinstance(f, FileRef) and f.file not in held for f in cell)
    return False


def _cell_files(cell: Any) -> set[str]:
    """The identities of the files of a cell: a file, or a list of files."""
    if isinstance(cell, FileRef):
        return {cell.file}
    if isinstance(cell, list):
        return {f.file for f in cell if isinstance(f, FileRef)}
    return set()


def _unheld(key: str, at: str, missing: set[str]) -> str:
    return (
        f'row {key!r}: its record does not hold {", ".join(sorted(missing))} at '
        f'{at!r}. A cell at the trigger column holds the identity of each file, '
        'as FileInfo.ref gives it. A name such as file(run=1) is resolved by the '
        'workspace, so no record would count as holding it, and the row would be '
        'reduced at every step.'
    )


def _files_at(params: Mapping[str, Any], at: str) -> set[str]:
    """
    The identities of the files at the parameter ``at`` of a request: its
    value, or each element of its list, as a file or its plain form.
    """
    return {
        ref.file
        for loc, ref in iter_refs({at: params.get(at)})
        if isinstance(ref, FileRef)
        and (len(loc) == 1 or (len(loc) == 2 and isinstance(loc[1], int)))
    }
