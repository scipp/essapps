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

A file is compared by its identity, as the scan lists it, and a client has no
call that resolves a name such as ``file(run=1)``. A record holds the identity
in place of the name, so a row with a name at the trigger column would run at
every step. Such a row is refused before it is submitted: each file of its cell
at the trigger column must be one of the files of the scan.

A step costs the scan of all files and a check of each row's cell at the
trigger column. The state of the rows it submits reads the records that
changed since the batch last read them, and the status of those rows only.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ess.spec import FileRef
from ess.spec.refs import iter_refs

from ..errors import FieldError, SubmitError
from ..records import Record
from .reduction import Batch, FileInfo
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
        The trigger column, of a file or a list of files, each as
        ``FileInfo.ref`` gives it.
    settings:
        Gives the settings. It is called at each step, so that a change
        applies to the rows not reduced yet.

    Raises
    ------
    KeyError
        If ``at`` is not a column of ``batch``.
    ValueError
        If ``batch`` has no source.
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
        files = batch.scan()
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
            and not _cell_files(row.values.get(self.at))
            <= self._held.get(row.key, set())
        ]
        return self._reduce(settings, self._listed(todo, files))

    def _listed(self, keys: list[str], files: Sequence[FileInfo]) -> list[str]:
        """
        ``keys``, less the rows refused for a file at ``at`` that is not one of
        ``files``, those of the scan.
        """
        if not keys:
            return keys
        rows = self.batch.rows
        listed = {f.ref.file for f in files}
        kept = []
        for key in keys:
            if unlisted := _cell_files(rows[key].values.get(self.at)) - listed:
                error = FieldError(key, (self.at,), _not_listed(unlisted))
                self._refuse([key], str(error))
            else:
                kept.append(key)
        return kept

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


def _cell_files(cell: Any) -> set[str]:
    """
    The identities of the files of a cell: a file, or the files of a list. A
    file deeper inside the cell does not count.
    """
    if isinstance(cell, FileRef):
        return {cell.file}
    if isinstance(cell, list):
        return {f.file for f in cell if isinstance(f, FileRef)}
    return set()


def _not_listed(files: set[str]) -> str:
    return (
        f'{", ".join(sorted(files))} is not a file of the scan. A cell at the '
        'trigger column holds each file as FileInfo.ref gives it, not a name '
        'such as file(run=1)'
    )


def _files_at(params: Mapping[str, Any], at: str) -> set[str]:
    """
    The identities of the files at the parameter ``at`` of a request, each as
    a file or its plain form.
    """
    return {
        ref.file
        for _, ref in iter_refs({at: params.get(at)})
        if isinstance(ref, FileRef)
    }
