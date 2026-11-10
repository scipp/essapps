# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The rows of a batch as a table of text, a pandas ``DataFrame``, which a person
writes in a spreadsheet and a program writes to a file. It needs pandas.

A table read from a file holds text; one made in a notebook may hold numbers,
gaps (``None``, ``NaN``), and any value that a request takes.

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

from collections.abc import Collection, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel

from ..client import Client
from ..records import Status, Template
from .reduction import Batch
from .state import RowState
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


def _empty(cell: Any) -> bool:
    """Whether a cell holds nothing: no text, or what pandas reads a gap as."""
    if cell is None or cell is pd.NA:
        return True
    return isinstance(cell, str | float) and (cell == '' or cell != cell)


def _value(cell: Any) -> Any:
    """A cell of text as :func:`.text.read_cell` reads it; any other as it is."""
    if isinstance(cell, str):
        return read_cell(cell)
    cell = cell.item() if isinstance(cell, np.generic) else cell
    # pandas holds whole numbers next to a gap as floats
    return int(cell) if isinstance(cell, float) and cell.is_integer() else cell


def rows_of(
    table: pd.DataFrame, model: type[BaseModel], columns: Collection[str]
) -> list[Row]:
    """
    A row of a batch for each row of ``table``, keyed by its first column.

    ``columns`` are the columns of the batch, parameters of ``model``. An empty
    cell is left out of the row's values. A cell that is not text, as in a table
    made in a notebook, is its value. The other columns are info, but for
    ``key``, which :func:`table_of` writes. Raises ``ValueError`` if the table
    lacks one of ``columns``, so that a misspelt header is not read as info, or
    has a column of another parameter, whose cells no row would use.
    """
    if missing := sorted(set(columns) - set(table.columns)):
        raise ValueError(f'the table has no column {", ".join(missing)}')
    fixed = set(table.columns) & model.model_fields.keys() - set(columns)
    if fixed:
        raise ValueError(
            f'the column {", ".join(sorted(fixed))} is a parameter, but not a '
            'blank of the template: name it as a blank, or rename the column'
        )
    rows = []
    for record in table.to_dict('records'):
        cells = {name: cell for name, cell in record.items() if not _empty(cell)}
        values = {c: _value(cells[c]) for c in columns if c in cells}
        info = {n: str(c) for n, c in cells.items() if n not in {*columns, 'key'}}
        key = str(_value(record[table.columns[0]]))
        rows.append(Row(key, with_named_files(model, values), info))
    return rows


def table_of(rows: Iterable[Row], columns: Sequence[str] = ()) -> pd.DataFrame:
    """
    The table that :func:`rows_of` reads as ``rows``: key, values, then info.
    It has a column for each of ``columns``, also one that no row has a value
    of. Raises ``ValueError`` if a row's info has the name of a value or
    ``key``, since a table has one column of a name.
    """
    rows = list(rows)
    names = {'key', *columns, *(name for row in rows for name in row.values)}
    records = []
    for row in rows:
        if twice := sorted(row.info.keys() & names):
            raise ValueError(f'{row.key}: info named as a column: {", ".join(twice)}')
        cells = {name: write_cell(value) for name, value in row.values.items()}
        records.append(
            {'key': row.key, **dict.fromkeys(columns, ''), **cells, **row.info}
        )
    return pd.DataFrame(records, dtype=str).fillna('')


def described(files: pd.DataFrame, run_table: pd.DataFrame) -> pd.DataFrame:
    """
    The table of files (see :func:`..folder.scan_folder`) with what a person's
    table says of each: its role, its sample, or any other column.

    A row of ``run_table`` describes the file of its ``run``, or, if that cell
    is empty or there is no such column, the file at its ``file``. The result
    has the rows of ``files``: a row of ``run_table`` that describes no file is
    ignored, and a file that no row describes keeps what it has. In a column of
    both tables, a cell of ``run_table`` that is not empty replaces what the
    file says of itself.

    Raises ``ValueError`` if a row names neither a run nor a file, or two rows
    describe one.
    """
    by: dict[str, dict[str, dict[str, Any]]] = {'run': {}, 'file': {}}
    for record in run_table.to_dict('records'):
        column = 'file' if _empty(record.get('run')) else 'run'
        if _empty(record.get(column)):
            raise ValueError('a row of the run table names neither a run nor a file')
        key = str(_value(record[column]))
        if key in by[column]:
            raise ValueError(f'two rows of the run table describe the {column} {key}')
        by[column][key] = {
            name: cell
            for name, cell in record.items()
            if name not in by and not _empty(cell)
        }
    rows = [
        record
        | by['file'].get(str(_value(record['file'])), {})
        | (
            {}
            if _empty(run := record.get('run'))
            else by['run'].get(str(_value(run)), {})
        )
        for record in files.to_dict('records')
    ]
    columns = dict.fromkeys(
        [*files.columns, *run_table.columns.drop(list(by), 'ignore')]
    )
    return pd.DataFrame(rows, columns=list(columns)).fillna('')


RESULT = ('status', 'record', 'failure')
"""The columns that :func:`reduce_table` and :func:`with_results` add, or replace."""


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
    return with_results(client, batch.state(), table)


def with_results(
    client: Client, states: Mapping[str, RowState], table: pd.DataFrame
) -> pd.DataFrame:
    """
    ``table`` with the columns of :data:`RESULT`, replaced if it has them: what
    ``states`` say of the row of each key in its first column. ``status`` is the
    row's :attr:`~.state.RowState.text`.
    """
    keys = table[table.columns[0]].astype(str)
    records = [states[key].record for key in keys]
    found = [r for r in records if r is not None]
    failure = dict(zip((r.id for r in found), client.failure(found), strict=True))
    return table.drop(columns=list(RESULT), errors='ignore').assign(
        status=[states[key].text for key in keys],
        record=['' if r is None else r.id for r in records],
        failure=['' if r is None else failure[r.id] or '' for r in records],
    )
