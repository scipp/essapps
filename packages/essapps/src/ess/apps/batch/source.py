# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What an application gives a batch: a :class:`Source` makes a table from the
files.

A source is code of the application, registered by name with a
:class:`~.manager.BatchManager`. Its :data:`RowSource` takes the files with
their metadata and returns a frame, the form that ``Batch.put`` takes::

    key     run    name         sample      sample_transmission
    60339   60339  D2O buffer   run:60339   run:60305
    60340   60340  sample A     run:60340   <NA>

The key of each row as index, as text; a column per parameter that it fills,
each file as its name (``str(f.ref)``); and notes, such as ``run`` and
``name``. :func:`rows_of` is what a source makes from the files now. A batch
is made from it, and Follow adds its rows that are new and filled
(:mod:`~.auto`). A batch never asks its source again: how its table was made
is not part of it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
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
    """A file and its metadata, as a :data:`RowSource` is given it."""

    ref: FileRef
    metadata: Mapping[str, Any]


def file_infos(client: Client, selector: Selector | None = None) -> list[FileInfo]:
    """The files ``selector`` matches, raw files if not given, with their metadata."""
    refs = client.files.list(Selector() if selector is None else selector)
    return [
        FileInfo(*pair) for pair in zip(refs, client.files.metadata(refs), strict=True)
    ]


type RowSource = Callable[[Sequence[FileInfo]], pd.DataFrame]
"""
What an application knows of its technique: which files make which rows. It
takes plain data and returns a frame, so it is tested on its own.
"""


@dataclass(frozen=True)
class Source:
    """
    A source of rows: a :data:`RowSource`, the columns it fills, and the files
    it is given.

    Parameters
    ----------
    rows:
        Makes a table from files.
    fills:
        The columns that ``rows`` fills. Follow adds a row once it has a
        value in each of these that is a column of the batch.
    selector:
        The files that ``rows`` is given; raw files if not given.
    """

    rows: RowSource
    fills: tuple[str, ...]
    selector: Selector | None = None


def rows_of(client: Client, source: Source) -> pd.DataFrame:
    """
    The table that ``source`` makes from the files of ``client`` now, with an
    empty column for each of :attr:`Source.fills` that it lacks, so that a
    batch made from it has its columns also when no file is listed yet.

    Raises ``ValueError`` for a key that is not text, empty, or given twice,
    or a column that a table cannot hold (see :func:`~.table.typed`).
    """
    frame = source.rows(file_infos(client, source.selector))
    empty = {
        c: pd.Series(pd.NA, index=frame.index, dtype='string')
        for c in source.fills
        if c not in frame.columns
    }
    # Every column a note: the source knows no spec.
    return typed(frame.assign(**empty), BaseModel)
