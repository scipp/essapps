# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The files that a person takes away from a batch. It needs pandas, scipp, and h5py.

:func:`export_table` takes the table that :func:`.frames.reduce_table` returned,
indexed by key, and writes the result of each completed row to a folder named
by its key, with a :data:`RowWriter`: by the form of the batch
(:meth:`.writing.Writing.by_form`), or with one writer (:func:`by_writer`),
such as scipp HDF5 for a table that names no batch. It finds a row's
result by the ``record`` column, so it exports what the table says, also after
later reductions under the same label.
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
"""Writes the result of the row of a key, its record, into a new folder."""


def by_writer(client: Client, writer: Writer = SCIPP_HDF5) -> RowWriter:
    """
    Writes the result of a row with ``writer``, named by the key of the row;
    a writer that writes the outputs chosen writes each output that the
    record returned.
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
    Write the result of each row of ``table``, indexed by key, whose record
    completed to ``directory / key`` with ``write``, replacing that folder
    whole: each row is written into a temporary folder in ``directory``
    first, so that a row that fails leaves the folder of its key as it was.

    Raises ``ValueError`` if the table has no ``record`` column, names a record
    that the workspace lacks, has a key twice, or a key that is not the name of
    a folder, such as one with ``/``; nothing is written.
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
