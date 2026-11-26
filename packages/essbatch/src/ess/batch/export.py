# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The files that a person takes away from a batch: one folder per row.

A :data:`RowWriter` writes by the output options of the batch
(:meth:`.writing.Writing.by_options`), or with one writer (:func:`by_writer`),
such as scipp HDF5 for a table of no batch.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

import pandas as pd

from ess.apps import writers
from ess.apps.client import Client
from ess.apps.records import Record, Status
from ess.apps.writers import SCIPP_HDF5, DatasetMetadata, Writer

type RowWriter = Callable[[str, Record, Path], None]
"""Writes the result of a row, given its key and record, into a new folder."""


def by_writer(client: Client, writer: Writer = SCIPP_HDF5) -> RowWriter:
    """
    A :data:`RowWriter` that writes with ``writer``, naming the dataset by the
    key of the row.

    A writer of chosen outputs writes each output that the record returned.
    """
    return lambda key, record, directory: writers.write(
        client,
        record,
        writer,
        DatasetMetadata(name=key),
        directory,
        client.readable([record])[0],
    )


def export_table(
    client: Client, table: pd.DataFrame, directory: Path, write: RowWriter
) -> None:
    """
    Writes the result of each row whose record completed to ``directory /
    key``, with ``write``.

    ``table`` is indexed by key. The record of a row is the one in the column
    ``record``, so a later reduction under the same label does not change what
    is exported. Each folder is replaced whole. A row is written to a
    temporary folder in ``directory`` first, so a row whose writing fails
    leaves the folder of its key as it was.

    Raises ``ValueError`` before anything is written if ``table`` cannot be
    exported, such as a table without a column ``record``.
    """
    if 'record' not in table:
        raise ValueError('the table has no column record: reduce it first')
    keys = [str(k) for k in table.index]
    if bad := sorted(k for k in keys if k in ('', '.', '..') or Path(k).name != k):
        raise ValueError(f'not the name of a folder: {", ".join(map(repr, bad))}')
    if len(set(keys)) < len(keys):
        raise ValueError('two rows of the table have one key')
    ids = dict(zip(keys, table['record'].astype(str), strict=True))
    try:
        records = client.records(ids=sorted(i for i in ids.values() if i))
    except KeyError as error:
        raise ValueError(f'the workspace has no record {error.args[0]}') from None
    status = dict(zip((r.id for r in records), client.status(records), strict=True))
    by_id = {r.id: r for r in records if status[r.id] is Status.COMPLETED}
    directory.mkdir(parents=True, exist_ok=True)
    for key, i in ids.items():
        if i in by_id:
            with tempfile.TemporaryDirectory(dir=directory) as temporary:
                written = Path(temporary) / key
                write(key, by_id[i], written)
                if (directory / key).exists():
                    shutil.rmtree(directory / key)
                written.rename(directory / key)
