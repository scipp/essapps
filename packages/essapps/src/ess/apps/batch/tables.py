# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The rows of a batch as a table of text, a pandas ``DataFrame``, which a person
writes in a spreadsheet and a program writes to a file. It needs pandas.

The first column holds the key of each row. A column named as a parameter holds
the cells of that column of the batch, and any other column is ``info``. The key
column may itself be a parameter, such as the sample run::

    sample,sample_transmission,name
    60339,60394,water
    60340,60395,sds

:mod:`.text` says how a cell is read. :func:`reduce_table` gives the same table
with what came of each row, so the table that comes back is the table that went
in.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from pathlib import Path

import pandas as pd
from pydantic import BaseModel

from ..client import Client
from ..records import Status, Template
from .reduction import Batch
from .table import Row
from .text import read_cell, with_named_files, write_cell

_READERS = {
    '.csv': lambda path: pd.read_csv(path, dtype=str, keep_default_na=False),
    '.tsv': lambda path: pd.read_csv(path, dtype=str, keep_default_na=False, sep='\t'),
    '.xlsx': lambda path: pd.read_excel(path, dtype=str, keep_default_na=False),
}


SUFFIXES = tuple(_READERS)
"""The suffixes of the files that hold a table."""


def read_table(path: Path) -> pd.DataFrame:
    """The table in a ``.csv``, ``.tsv``, or ``.xlsx`` file, each cell as text."""
    if (reader := _READERS.get(path.suffix)) is None:
        raise ValueError(f'{path}: a table is one of {", ".join(_READERS)}')
    return reader(path)


def write_table(table: pd.DataFrame, path: Path) -> None:
    """Write ``table`` as :func:`read_table` reads it, by the suffix of ``path``."""
    if path.suffix == '.xlsx':
        table.to_excel(path, index=False)
    elif path.suffix in _READERS:
        table.to_csv(path, index=False, sep='\t' if path.suffix == '.tsv' else ',')
    else:
        raise ValueError(f'{path}: a table is one of {", ".join(_READERS)}')


def rows_of(
    table: pd.DataFrame, model: type[BaseModel], columns: Collection[str]
) -> list[Row]:
    """
    A row of a batch for each row of ``table``, keyed by its first column.

    ``columns`` are the columns of the batch, parameters of ``model``. An empty
    cell is left out of the row's values. Raises ``ValueError`` if the table
    lacks one of ``columns``, so that a misspelt header is not read as info, or
    has a column of another parameter, whose cells no row would use.
    """
    if missing := sorted(set(columns) - set(table.columns)):
        raise ValueError(f'the table has no column {", ".join(missing)}')
    fixed = set(table.columns) & model.model_fields.keys() - set(columns)
    if fixed:
        raise ValueError(
            f'the column {", ".join(sorted(fixed))} is a parameter that the template '
            'fixes: name it as a blank, or rename the column'
        )
    rows = []
    for record in table.to_dict('records'):
        cells = {name: str(text) for name, text in record.items() if text != ''}
        values = {c: read_cell(cells[c]) for c in columns if c in cells}
        info = {name: text for name, text in cells.items() if name not in columns}
        key = str(record[table.columns[0]])
        rows.append(Row(key, with_named_files(model, values), info))
    return rows


def table_of(rows: Iterable[Row]) -> pd.DataFrame:
    """
    The table that :func:`rows_of` reads as ``rows``: key, values, then info.
    Raises ``ValueError`` if a row's info has the name of a value or ``key``,
    since a table has one column of a name.
    """
    records = []
    for row in rows:
        if twice := sorted(row.info.keys() & {'key', *row.values}):
            raise ValueError(f'{row.key}: info named as a column: {", ".join(twice)}')
        cells = {name: write_cell(value) for name, value in row.values.items()}
        records.append({'key': row.key, **cells, **row.info})
    return pd.DataFrame(records, dtype=str).fillna('')


RESULT = ('status', 'record', 'failure')
"""The columns that :func:`reduce_table` adds, or replaces."""


def reduce_table(
    client: Client,
    template: Template,
    table: pd.DataFrame,
    *,
    label: str,
    staged: bool = False,
    persist: bool | Sequence[str] = False,
    remaining: bool = False,
) -> pd.DataFrame:
    """
    Reduce each row of ``table`` with ``template``, wait, and return the table
    with what came of each row: its ``status``, the ID of its ``record``, and
    why it failed in ``failure``.

    Parameters
    ----------
    client:
        The client that submits.
    template:
        The settings, and the blanks, each a column of ``table``. A file in it
        may be named as in a table (see :mod:`.text`).
    table:
        The rows, as :func:`rows_of` reads them.
    label:
        The label of the records. A batch is found again by it, so each batch
        has its own.
    staged:
        Run the rows through a stage.
    persist:
        As :meth:`~ess.apps.Client.submit` takes it.
    remaining:
        Leave out the rows whose newest record under ``label`` completed,
        whatever its cells and settings were.

    Raises
    ------
    SubmitError
        If a row cannot run; nothing is submitted.
    ValueError
        If the table does not fit the template (see :func:`rows_of`), or a
        blank is named as one of :data:`RESULT`.
    """
    if taken := sorted(set(template.blanks) & set(RESULT)):
        raise ValueError(f'a table cannot hold the blank {", ".join(taken)}')
    spec = client.spec(template.spec)
    settings = with_named_files(spec.params, template.params)
    batch = Batch(
        client,
        spec,
        columns=template.blanks,
        label=label,
        staged=staged,
        persist=persist,
    )
    batch.rows = batch.rows.rescan(rows_of(table, spec.params, template.blanks))
    try:
        # Records that a workspace went on with after a restart finish first.
        client.wait([s.record for s in batch.state().values() if s.record])
        keys = [
            key
            for key, state in batch.state().items()
            if not (remaining and state.status is Status.COMPLETED)
        ]
        client.wait(batch.reduce(settings, keys))
    except KeyboardInterrupt:
        batch.cancel()
        raise
    finally:
        batch.close()
    states = batch.state()
    found = [s.record for s in states.values() if s.record is not None]
    failure = dict(zip((r.id for r in found), client.failure(found), strict=True))
    keys = table[table.columns[0]].astype(str)
    records = [states[key].record for key in keys]
    return table.drop(columns=list(RESULT), errors='ignore').assign(
        status=[states[key].text for key in keys],
        record=['' if r is None else r.id for r in records],
        failure=['' if r is None else failure[r.id] or '' for r in records],
    )
