# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The files that a person takes away from a batch. It needs pandas, scipp, and h5py.

:func:`export_table` takes the table that :func:`.frames.reduce_table` returned,
and writes the outputs of each completed row to a folder named by its key. It
finds a row's outputs by the ``record`` column, so it exports what the table
says, also after later reductions under the same label.

The files are those of :mod:`ess.apps.hdf5`, a placeholder for the formats of
the techniques, such as NXcanSAS, each of which needs a writer for the type of
its output.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .. import hdf5
from ..client import Client
from ..records import Record, Status
from .frames import RESULT

RECORD_FILE = '_record.json'
"""The file of the record in the folder of a row; no output is named so."""


def write_outputs(client: Client, record: Record, directory: Path) -> dict[str, Path]:
    """
    Write each output that a completed ``record`` returned to ``directory``, and
    the record itself, which names the files and the values they were computed
    from. It names a file by its identity: its catalogue identifier, or its
    path, relative to the workspace's folder if the file is in it::

        iofq.h5            what scipp.io.load_hdf5 reads (see ess.apps.hdf5)
        _record.json       the record

    Returns the path of each output's file by its name.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / RECORD_FILE).write_text(record.model_dump_json(indent=2))
    paths = {}
    for name, value in client.output(record).items():
        paths[name] = directory / f'{name}.h5'
        hdf5.write(value, paths[name])
    return paths


def export_table(client: Client, table: pd.DataFrame, directory: Path) -> pd.DataFrame:
    """
    Write the outputs of each row of ``table`` whose record completed to
    ``directory / key`` (see :func:`write_outputs`), and return the table with a
    column for each output, which holds the path of its file, empty for a row
    that has none. A column of the table that is named as an output is replaced,
    as one of an earlier export is.

    Raises ``ValueError`` if the table has no ``record`` column, names a record
    that the workspace lacks, has a key twice, a key that is not the name of a
    folder, such as one with ``/``, or an output named as the key column or a
    column that reduce adds; nothing is written.
    """
    if 'record' not in table:
        raise ValueError('the table has no column record: reduce it first')
    keys = table[table.columns[0]].astype(str)
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
    names = dict.fromkeys(n for r in records for n in r.outputs)
    if taken := sorted(names.keys() & {table.columns[0], *RESULT}):
        raise ValueError(f'an output is named as a column: {", ".join(taken)}')
    paths = {
        key: write_outputs(client, by_id[i], directory / key)
        for key, i in ids.items()
        if i in by_id
    }
    added = {n: [str(paths.get(k, {}).get(n, '')) for k in keys] for n in names}
    return table.drop(columns=added.keys(), errors='ignore').assign(**added)
