# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Row sources: code of an application that makes rows of a batch table from
the files.

Guide: ``packages/essbatch/docs/batch.md``, "Writing a row source".
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pandas as pd
from pydantic import BaseModel

from ess.apps.client import Client
from ess.apps.files import Selector
from ess.spec import FileRef

from .table import typed


@dataclass(frozen=True)
class FileInfo:
    """A file and its metadata, as a :data:`RowFunction` receives it."""

    ref: FileRef
    metadata: Mapping[str, Any]


def _file_infos(client: Client, selector: Selector | None = None) -> list[FileInfo]:
    """The files ``selector`` matches, raw files if not given, with their metadata."""
    refs = client.files.list(Selector() if selector is None else selector)
    return [
        FileInfo(*pair) for pair in zip(refs, client.files.metadata(refs), strict=True)
    ]


type RowFunction = Callable[[Sequence[FileInfo]], pd.DataFrame]
"""
A function from the files to the rows of a source.

It returns the key of each row as index, as text; a column per source column,
a file as its name (:func:`~.table.cell_of`); and other columns as notes, such
as ``run`` and ``name``.
"""


@dataclass(frozen=True)
class RowSource:
    """
    A :data:`RowFunction`, the columns it fills, and the files it receives.

    Parameters
    ----------
    source_columns:
        The columns that ``rows`` fills, each named as a parameter of the
        specs it fits (:func:`~.intake.fits`). Follow adds a row only once it
        has a value in each.
    selector:
        The files that ``rows`` receives. Raw files if ``None``.
    description:
        For a person: what a row is and how ``rows`` pairs the files.
        ``essbatch sources`` shows it.
    """

    rows: RowFunction
    source_columns: tuple[str, ...]
    selector: Selector | None = None
    description: str = ''


def rows_of(client: Client, source: RowSource) -> pd.DataFrame:
    """
    The rows that ``source`` makes from the files listed now.

    It adds an empty column for each source column that the rows lack, so
    that a batch made before any file is listed has its columns.

    Raises
    ------
    ValueError
        For a key that is not text, empty, or repeated, or a column that a
        table cannot hold (:func:`~.table.typed`).
    """
    frame = source.rows(_file_infos(client, source.selector))
    empty = {
        c: pd.Series(pd.NA, index=frame.index, dtype='string')
        for c in source.source_columns
        if c not in frame.columns
    }
    # Every column is a note: a source knows no spec.
    return typed(frame.assign(**empty), BaseModel)


def complete(given: pd.DataFrame, columns: Collection[str]) -> pd.Series:
    """
    Whether each row of ``given`` has a value in each of ``columns``.

    A row of a source is *complete* once it has a value in each source column,
    and *incomplete* until then.

    Raises
    ------
    ValueError
        If ``given`` lacks one of ``columns``, such as when the code of a source
        no longer returns a column that a batch stores as a source column.
    """
    if lacking := [c for c in columns if c not in given.columns]:
        raise ValueError(
            f'it no longer gives the column {", ".join(sorted(lacking))} that '
            'the batch keeps as a source column'
        )
    return given[list(columns)].notna().all(axis=1)


@dataclass(frozen=True)
class Range:
    """
    The rows of a new batch, picked from the complete rows of its source.

    Passed as ``choose`` to :meth:`~.intake.Intake.create` and
    :meth:`~.intake.Intake.marked`. Called on the complete rows, it returns
    those from key ``first`` to key ``last``, both included. An end not given
    is open::

        Range(first='60339')(arrived)   # rows 60339 to the end
        Range(later=True)(arrived)      # no rows

    With ``later``, the batch gets only rows added later, by Follow or by a
    person.

    Raises
    ------
    ValueError
        For ``later`` with an end. When called, for an end that is not a key of
        the rows, or a ``first`` after ``last``.
    """

    first: str | None = None
    last: str | None = None
    later: bool = False

    def __post_init__(self) -> None:
        if self.later and (self.first is not None or self.last is not None):
            raise ValueError(
                'a batch of the rows that arrive later has no first or last'
            )

    def __call__(self, arrived: pd.DataFrame) -> pd.DataFrame:
        if self.later:
            return arrived.iloc[:0]
        first, last = self.first, self.last
        for end in (first, last):
            if end is not None and end not in arrived.index:
                raise ValueError(f'{end} is not a complete row')
        start = 0 if first is None else arrived.index.get_loc(first)
        stop = len(arrived) if last is None else arrived.index.get_loc(last) + 1
        if first is not None and last is not None and stop <= start:
            raise ValueError(f'the row {first} comes after the row {last}')
        return arrived.iloc[start:stop]
