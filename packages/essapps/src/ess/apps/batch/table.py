# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The rows of a batch, as an immutable value.

The table holds two layers: what the last scan gave, and what was set by hand
since, a person's edit or a program's. A row's cells are the scan's with the
edits on top, worked out when asked for. So a new scan replaces only its own
layer, and no merge is needed: each rule below follows from which layer an
operation writes. Cells are opaque; this module knows nothing of specs, files,
or widgets.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from functools import cached_property
from typing import Any

from ..errors import FieldError, SubmitError


class _Empty:
    def __repr__(self) -> str:
        return 'EMPTY'


EMPTY: Any = _Empty()
"""Written to a cell by :meth:`RowTable.edit` to clear it, whatever the scan gave."""


@dataclass(frozen=True)
class Row:
    """
    One row: ``values`` by column, and ``info``, read-only text to show with it.

    A column left out of ``values`` is an empty cell; ``None`` is a value.
    ``key`` must stay the same from one scan to the next, so it is best derived
    from something that does not change, such as the run number, with the name
    of the sample in ``info``.

    Raises ``TypeError`` if ``key`` is not a string.
    """

    key: str
    values: Mapping[str, Any] = field(default_factory=dict)
    info: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.key, str):
            raise TypeError(f'the key of a row is a string, not {self.key!r}')


def _unique(rows: Iterable[Row]) -> tuple[Row, ...]:
    """``rows``; raises ``SubmitError`` for a key given twice."""
    rows = tuple(rows)
    count = Counter(row.key for row in rows)
    if twice := sorted(key for key, n in count.items() if n > 1):
        raise SubmitError(FieldError(k, (), 'two rows have this key') for k in twice)
    return rows


@dataclass(frozen=True)
class RowTable:
    """
    Rows by key: those of the scan, then those added by hand. Each method
    returns a new table. See ``packages/essapps/docs/batch.md`` for the rules.
    """

    scan: tuple[Row, ...] = ()
    added: tuple[Row, ...] = ()
    edits: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    removed: frozenset[str] = frozenset()

    @cached_property
    def _rows(self) -> dict[str, Row]:
        hidden = self.removed | {row.key for row in self.added}
        shown = [r for r in self.scan if r.key not in hidden]
        return {row.key: self._with_edits(row) for row in (*shown, *self.added)}

    def _with_edits(self, row: Row) -> Row:
        edits = self.edits.get(row.key)
        if edits is None and not any(v is EMPTY for v in row.values.values()):
            return row
        cells = {**row.values, **(edits or {})}
        return replace(
            row, values={column: v for column, v in cells.items() if v is not EMPTY}
        )

    def __iter__(self) -> Iterator[Row]:
        return iter(self._rows.values())

    def __len__(self) -> int:
        return len(self._rows)

    def __contains__(self, key: str) -> bool:
        return key in self._rows

    def __getitem__(self, key: str) -> Row:
        return self._rows[key]

    def rescan(self, rows: Iterable[Row]) -> RowTable:
        """
        Replace the scan's layer. What was set by hand stays: an edit, a row
        added, and a row removed. Raises ``SubmitError`` for a key given twice.
        """
        return replace(self, scan=_unique(rows))

    def put(self, rows: Iterable[Row], columns: Iterable[str]) -> RowTable:
        """
        Set ``rows`` by hand, as a table of them gives them: a row of a key
        that the table does not show is added, but one of the scan that was
        removed is brought back; and of a row shown, or brought back, each of
        ``columns`` whose cell differs is set, or cleared if the row given
        leaves it empty. A cell that does not differ is left as it is, so one
        from the scan keeps following the scan. Raises ``SubmitError`` for a
        key given twice.
        """
        columns = tuple(columns)
        added = list(self.added)
        edits = dict(self.edits)
        removed = set(self.removed)
        scan = {row.key: row for row in self.scan if row.key in removed}
        for row in _unique(rows):
            if row.key in scan:
                removed.discard(row.key)
                shown = self._with_edits(scan[row.key]).values
            elif row.key in self:
                shown = self[row.key].values
            else:
                added.append(row)
                edits.pop(row.key, None)  # as :meth:`add`
                continue
            changed = {
                c: row.values.get(c, EMPTY)
                for c in columns
                if (c in row.values, row.values.get(c)) != (c in shown, shown.get(c))
            }
            if changed:
                edits[row.key] = {**edits.get(row.key, {}), **changed}
        return replace(
            self, added=tuple(added), edits=edits, removed=frozenset(removed)
        )

    def add(self, row: Row) -> RowTable:
        """
        Add a row by hand; it stays whatever a scan gives. Raises
        ``SubmitError`` if the table has a row of that key.
        """
        if row.key in self:
            raise SubmitError([FieldError(row.key, (), 'a row has this key already')])
        edits = {k: v for k, v in self.edits.items() if k != row.key}
        return replace(self, added=(*self.added, row), edits=edits)

    def edit(self, key: str, values: Mapping[str, Any]) -> RowTable:
        """
        Set cells of a row by hand, :data:`EMPTY` to clear one. They stay
        until set again or :meth:`reset`. Raises ``KeyError`` if there is no such
        row.
        """
        return self.edit_each([key], values)

    def edit_each(self, keys: Iterable[str], values: Mapping[str, Any]) -> RowTable:
        """
        :meth:`edit` of each of ``keys`` in one pass. Raises ``KeyError``, and
        edits none, if the table has no row for one of them.
        """
        keys = tuple(keys)
        for key in keys:
            self[key]
        edits = dict(self.edits)
        for key in keys:
            edits[key] = {**edits.get(key, {}), **values}
        return replace(self, edits=edits)

    def remove(self, key: str) -> RowTable:
        """
        Remove a row. One from the scan stays removed after the next scan,
        until :meth:`reset`. Raises ``KeyError`` if there is no such row.
        """
        return self.remove_each([key])

    def remove_each(self, keys: Iterable[str]) -> RowTable:
        """
        :meth:`remove` of each of ``keys`` in one pass. Raises ``KeyError``, and
        removes none, if the table has no row for one of them.
        """
        keys = frozenset(keys)
        for key in keys:
            self[key]
        return replace(
            self,
            added=tuple(row for row in self.added if row.key not in keys),
            edits={k: v for k, v in self.edits.items() if k not in keys},
            removed=self.removed | keys.intersection(row.key for row in self.scan),
        )

    def reset(self, key: str, *columns: str) -> RowTable:
        """
        Forget the edits of a row, and that it was removed, if it was. Given
        ``columns``, forget only the edits of those cells.
        """
        return self.reset_each([key], *columns)

    def reset_each(self, keys: Iterable[str], *columns: str) -> RowTable:
        """:meth:`reset` of each of ``keys`` in one pass."""
        keys = frozenset(keys)
        edits = {}
        for key, cells in self.edits.items():
            if key not in keys:
                edits[key] = cells
            elif columns and (
                kept := {c: v for c, v in cells.items() if c not in columns}
            ):
                edits[key] = kept
        removed = self.removed if columns else self.removed - keys
        return replace(self, edits=edits, removed=removed)
