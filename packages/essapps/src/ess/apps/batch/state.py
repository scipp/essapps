# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Where each row of a batch stands, worked out from plain data.

Nothing here calls a client, so every rule of what is ready, what is left to
do, and what a cell shows is tested on values. :class:`~.reduction.Batch` reads
the records and gives them to these functions.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Iterable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel
from pydantic_core import to_json

from ess.spec import FileRef, OutputRef
from ess.spec.refs import as_ref, iter_refs

from ..errors import FieldError
from ..records import Record, SpecId, Status
from .table import Row

_RERUN = (Status.FAILED, Status.CANCELLED)


class Labels(Protocol):
    """How a cell names a file or an output; ``Listing`` is one."""

    def file_label(self, ref: FileRef) -> str: ...

    def output_label(self, ref: OutputRef) -> str: ...


@dataclass(frozen=True)
class RowState:
    """
    Where one row stands.

    ``record`` is the row's result, if it has one, and ``status`` its status.
    ``problems`` say why the row cannot be reduced now. ``stale`` is true if
    the row or the settings differ from those of its result.
    """

    key: str
    record: Record | None = None
    status: Status | None = None
    problems: tuple[FieldError, ...] = ()
    stale: bool = False

    @property
    def text(self) -> str:
        """What a table shows, and what :func:`summary` counts."""
        if self.status is Status.PENDING:
            return 'pending'
        if self.problems:
            return 'incomplete'
        if self.status is None:
            return 'not reduced'
        return 'stale' if self.stale else str(self.status)


def problems(
    row: Row,
    model: type[BaseModel],
    columns: Collection[str],
    status: Mapping[str, Status],
) -> tuple[FieldError, ...]:
    """
    Why ``row`` cannot be reduced: a value at a name that is not a column, an
    empty cell of a column with no default, or a reference to a record that
    failed or was cancelled, per ``status`` by record ID.
    """
    found = [
        FieldError(row.key, (name,), 'not a column')
        for name in row.values
        if name not in columns
    ]
    found += [
        FieldError(row.key, (column,), 'empty')
        for column in columns
        if column not in row.values and model.model_fields[column].is_required()
    ]
    found += [
        FieldError(row.key, loc, f'the record it reads is {status[ref.record]}')
        for loc, ref in iter_refs(row.values)
        if isinstance(ref, OutputRef) and status.get(ref.record) in _RERUN
    ]
    return tuple(found)


def results(
    records: Iterable[Record], spec: SpecId, submitter: str
) -> dict[str, Record]:
    """
    The result of each row by its key: the newest of ``records``, oldest
    first, of ``spec`` and ``submitter`` with that member. So a rerun of a row
    is its result, and a record of someone else under the same label is not.
    """
    return {
        r.member: r
        for r in records
        if r.member is not None and r.spec == spec and r.submitter == submitter
    }


def references(rows: Iterable[Row]) -> set[str]:
    """The IDs of the records that the cells of ``rows`` reference."""
    return {
        ref.record
        for row in rows
        for _, ref in iter_refs(row.values)
        if isinstance(ref, OutputRef)
    }


def row_states(
    rows: Iterable[Row],
    model: type[BaseModel],
    columns: Collection[str],
    found: Mapping[str, Record],
    status: Mapping[str, Status],
    stale: AbstractSet[str] = frozenset(),
) -> dict[str, RowState]:
    """
    The state of each row. ``found`` holds their results by key, ``status`` the
    status of those and of the records their cells reference, by record ID,
    and ``stale`` the keys that differ from their results.
    """
    states = {}
    for row in rows:
        record = found.get(row.key)
        states[row.key] = RowState(
            key=row.key,
            record=record,
            status=None if record is None else status[record.id],
            problems=problems(row, model, columns, status),
            stale=row.key in stale,
        )
    return states


def ready(states: Mapping[str, RowState]) -> list[str]:
    """The keys of the rows that can be reduced now: complete and not running."""
    return [
        k
        for k, s in states.items()
        if not s.problems and s.status is not Status.PENDING
    ]


def remaining(states: Mapping[str, RowState]) -> list[str]:
    """The ready rows with no result yet, one that failed or was cancelled, or stale."""
    return [
        k
        for k in ready(states)
        if states[k].status is None or states[k].status in _RERUN or states[k].stale
    ]


def pending(states: Mapping[str, RowState]) -> list[Record]:
    """The results that are still running."""
    return [
        s.record
        for s in states.values()
        if s.record is not None and s.status is Status.PENDING
    ]


def summary(states: Mapping[str, RowState]) -> dict[str, int]:
    """The number of rows of each :attr:`RowState.text`, sorted by it."""
    return dict(sorted(Counter(s.text for s in states.values()).items()))


def cell_text(value: Any, labels: Labels) -> str:
    """What a table shows for a cell: a file or an output by its label."""
    ref = as_ref(value)
    if isinstance(ref, OutputRef):
        return labels.output_label(ref)
    if isinstance(ref, FileRef):
        return labels.file_label(ref)
    if isinstance(value, list):
        return ', '.join(cell_text(item, labels) for item in value)
    if isinstance(value, dict):
        return to_json(value).decode()
    return 'none' if value is None else str(value)
