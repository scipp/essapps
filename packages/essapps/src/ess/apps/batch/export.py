# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The files that a person takes away from a batch. It needs pandas, scipp, and h5py.

:func:`export_table` takes the table that :func:`.tables.reduce_table` returned,
and writes the outputs of each completed row to a folder named by its key. It
finds a row's outputs by the ``record`` column, so it exports what the table
says, also after later reductions under the same label.

The format is generic. A format of a technique, such as NXcanSAS, needs a
writer for the output's type, which this module does not offer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import scipp as sc
from pydantic import BaseModel
from pydantic_core import PydanticSerializationError, to_json

from ..client import Client
from ..records import Record, Status

RECORD_FILE = '_record.json'
"""The file of the record in the folder of a row; no output is named so."""


def _as_scipp(value: Any) -> Any:
    """
    ``value`` with each group, dict, and list in it a ``DataGroup``, a list by
    the index of each element, without the values that are ``None``.
    """
    if isinstance(value, BaseModel):
        value = {name: getattr(value, name) for name in type(value).model_fields}
    if isinstance(value, list | tuple):
        value = {str(i): v for i, v in enumerate(value)}
    if isinstance(value, dict):
        return sc.DataGroup(
            {k: _as_scipp(v) for k, v in value.items() if v is not None}
        )
    return value


def _write(value: Any, stem: Path) -> Path:
    """Write ``value`` as JSON if it has a JSON form, or else as HDF5."""
    try:
        data = to_json(value, indent=2)
    except PydanticSerializationError:
        path = stem.with_name(f'{stem.name}.h5')
        sc.io.save_hdf5(_as_scipp(value), path)
    else:
        path = stem.with_name(f'{stem.name}.json')
        path.write_bytes(data)
    return path


def write_outputs(client: Client, record: Record, directory: Path) -> dict[str, Path]:
    """
    Write each output that a completed ``record`` returned to ``directory``, and
    the record itself, which names the files and the values they were computed
    from::

        beam_centre.json   an output that has a JSON form
        iofq.h5            any other: what scipp.io.load_hdf5 reads, a group as a
                           DataGroup of its fields, without those that are None
        _record.json       the record

    Returns the path of each output's file by its name.
    """
    directory.mkdir(parents=True, exist_ok=True)
    (directory / RECORD_FILE).write_text(record.model_dump_json(indent=2))
    return {
        name: _write(value, directory / name)
        for name, value in client.output(record).items()
    }


def export_table(client: Client, table: pd.DataFrame, directory: Path) -> pd.DataFrame:
    """
    Write the outputs of each row of ``table`` whose record completed to
    ``directory / key`` (see :func:`write_outputs`), and return the table with a
    column for each output, which holds the path of its file, empty for a row
    that has none.

    Raises ``ValueError`` if the table has no ``record`` column, or a key is not
    the name of a folder, such as one with ``/``; nothing is written.
    """
    if 'record' not in table:
        raise ValueError('the table has no column record: reduce it first')
    keys = table[table.columns[0]].astype(str)
    if bad := sorted(k for k in keys if k in ('', '.', '..') or Path(k).name != k):
        raise ValueError(f'not the name of a folder: {", ".join(map(repr, bad))}')
    ids = dict(zip(keys, table['record'].astype(str), strict=True))
    records = client.records(ids=sorted(i for i in ids.values() if i))
    status = dict(zip((r.id for r in records), client.status(records), strict=True))
    by_id = {r.id: r for r in records if status[r.id] is Status.COMPLETED}
    paths = {
        key: write_outputs(client, by_id[i], directory / key)
        for key, i in ids.items()
        if i in by_id
    }
    names = dict.fromkeys(n for r in records for n in r.outputs)
    added = {n: [str(paths.get(k, {}).get(n, '')) for k in keys] for n in names}
    return table.drop(columns=added.keys(), errors='ignore').assign(**added)
