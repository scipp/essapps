# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What an application gives a batch: a :class:`RowSource` makes rows from the
files.

A row source is code of the application, registered by name with an
:class:`~.intake.Intake`. Its :data:`RowFunction` takes the files with
their metadata and returns a frame, the form that ``Batch.put`` takes::

    key     run    name         sample      sample_transmission
    60339   60339  D2O buffer   run:60339   run:60305
    60340   60340  sample A     run:60340   <NA>

The key of each row as index, as text; a column per parameter that it fills,
each file as its cell (``cell_of(f.ref)``, :func:`~.table.cell_of`); and
notes, such as ``run`` and ``name``. :func:`rows_of` is what a source makes
from the files now. A row is *filled* (:func:`filled`) once it has a value
in each column that the source fills; until then it *waits*. A batch is made
from the filled rows, or from a range of them (:class:`Range`), and Follow
adds the rows that are new and filled (:mod:`~.auto`). A batch never asks
its source again: how its batch table was made is not part of it.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pandas as pd
from pydantic import BaseModel

from ess.spec import FileRef

from ..client import Client
from ..files import Selector
from .table import typed


@dataclass(frozen=True)
class FileInfo:
    """A file and its metadata, as a :data:`RowFunction` is given it."""

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
What an application knows of its technique: which files make which rows. It
takes plain data and returns a frame, so it is tested on its own.
"""


@dataclass(frozen=True)
class RowSource:
    """
    A row source: a :data:`RowFunction`, the columns it fills, and the files
    it is given.

    Parameters
    ----------
    rows:
        Makes the rows of the source from the files.
    fills:
        The columns that ``rows`` fills, each named as a parameter of the
        specs it fits (:func:`~.intake.fits`). A batch made from the source
        keeps them as parameter columns (:attr:`~.reduction.Batch.fills`),
        and Follow adds a row once it has a value in each.
    selector:
        The files that ``rows`` is given; raw files if not given.
    description:
        What a row is and how ``rows`` pairs the files, for a person who
        chooses the source or checks its rows, such as on ``essapps-batch
        sources``.
    """

    rows: RowFunction
    fills: tuple[str, ...]
    selector: Selector | None = None
    description: str = ''


def rows_of(client: Client, source: RowSource) -> pd.DataFrame:
    """
    The rows that ``source`` makes from the files of ``client`` now, with an
    empty column for each of :attr:`RowSource.fills` that it lacks, so that a
    batch made from it has its columns also when no file is listed yet.

    Raises ``ValueError`` for a key that is not text, empty, or given twice,
    or a column that a table cannot hold (see :func:`~.table.typed`).
    """
    frame = source.rows(_file_infos(client, source.selector))
    empty = {
        c: pd.Series(pd.NA, index=frame.index, dtype='string')
        for c in source.fills
        if c not in frame.columns
    }
    # Every column a note: the source knows no spec.
    return typed(frame.assign(**empty), BaseModel)


def filled(given: pd.DataFrame, columns: Collection[str]) -> pd.Series:
    """
    Whether each row of ``given`` has a value in each of ``columns``, such as
    the columns that a source fills: a row that has not waits. Raises
    ``ValueError`` if ``given`` lacks one of ``columns``, as when the code of
    a source no longer fills a column that a batch keeps as filled by it.
    """
    if lacking := [c for c in columns if c not in given.columns]:
        raise ValueError(
            f'it no longer gives the column {", ".join(sorted(lacking))} that '
            'the batch keeps as filled by it'
        )
    return given[list(columns)].notna().all(axis=1)


@dataclass(frozen=True)
class Range:
    """
    Which rows a new batch takes of those that its source has filled now, as
    a person picks them, given as ``choose`` to
    :meth:`~.intake.Intake.create` and
    :meth:`~.intake.Intake.marked`: called on the filled rows, it gives
    the rows from the row of key ``first`` to the row of key ``last``, both
    included, in their order; an end not given is open. With ``later``, it
    gives none, so that the batch gets only the rows added later, by Follow
    or by a person.

    Raises ``ValueError`` for ``later`` with an end; called, for an end that
    is not a key of the rows given, and for a ``first`` after the ``last``.
    ``create`` refuses a ``Range`` with a ``last`` and ``follow``: the rows
    filled between the last row and the making would be considered, so
    Follow would never add them, but would add the rows that arrive later.
    A ``choose`` that is a function may pick any rows, also with
    ``follow``: a person chose them in code, and the filled rows that it
    leaves out are considered, so Follow adds only what arrives later.
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
                raise ValueError(f'{end} is not a filled row')
        start = 0 if first is None else arrived.index.get_loc(first)
        stop = len(arrived) if last is None else arrived.index.get_loc(last) + 1
        if first is not None and last is not None and stop <= start:
            raise ValueError(f'the row {first} comes after the row {last}')
        return arrived.iloc[start:stop]
