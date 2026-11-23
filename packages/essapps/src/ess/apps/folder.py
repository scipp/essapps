# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A workspace kept in a folder, for one person on one machine. It needs scipp,
h5py and pandas (the extra ``files``).

The folder holds what a workspace needs to go on after its process ended::

    files.csv     the files table of FolderFiles: file, run, pid, kind, role, ...
                  (written by hand, or started with scan_folder, and
                  described by a person's run table)
    log.jsonl     history: a header naming the format of the log and of
                  the store, then one event per line (see ess.apps.log)
    store/        persisted outputs, <record>/<output>.h5
    definitions/  what a person made of the batches on the folder: the
                  corrections of files, and the header, settings, rows and
                  source of each label (see ess.apps.batch.definition)

So a command that runs and ends, and the next one, are clients of one workspace:
the second reads the records of the first, and references their outputs if they
were persisted. One process holds the folder at a time, since a log has one
writer.

A file of the store is named by the ID of its record.
:mod:`ess.apps.batch.export` writes the files of a batch to folders named by
the keys of its rows.
"""

from __future__ import annotations

import csv
import io
import threading
import warnings
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ess.spec import Binding, FileRef

from . import hdf5
from .client import Client
from .files import FolderFiles, Selector, replace_at_once
from .log import Log
from .protocol import FileSource
from .workspace import Workspace


class FolderStore:
    """
    A store of one scipp HDF5 file per output in ``directory``,
    ``<record>/<output>.h5`` (see :mod:`ess.apps.hdf5` for the format, and for
    the types of outputs that it takes).
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def path(self, record: str, output: str) -> Path:
        """The file of an output, written or not."""
        return self._directory / record / f'{output}.h5'

    def write(self, record: str, output: str, value: Any) -> None:
        path = self.path(record, output)
        path.parent.mkdir(parents=True, exist_ok=True)
        hdf5.write(value, path)

    def read(self, record: str, output: str, tp: Any | None) -> Any:
        path = self.path(record, output)
        if not path.is_file():
            raise LookupError(f'the store holds no {output} of {record}')
        try:
            return hdf5.read(tp, path)
        except ValueError as error:  # does not fit tp
            raise LookupError(str(error)) from None


_OWN = ('files.csv', 'log.jsonl', 'store', 'definitions')
"""What the workspace keeps in its folder, which no request names."""


def _measured(row: Mapping[str, Any]) -> tuple[bool, int, str]:
    """Sorts the runs with a whole number first, by it, and the rest by path."""
    run = str(row.get('run', ''))
    return (not run.isdigit(), int(run) if run.isdigit() else 0, row['file'])


def scan_folder(
    directory: Path, read: Callable[[Path], Mapping[str, Any] | None]
) -> list[dict[str, Any]]:
    """
    A row of ``files.csv`` for each file in ``directory``, at any depth, that
    ``read`` knows: the runs by their number, which is the order they were
    measured in, then the other files by path.

    ``read`` takes the path of a file and returns its metadata, such as
    ``{'kind': 'raw', 'run': 60339}``, or ``None`` for a file that is none of
    the workspace's. It knows the files of one instrument, so an instrument
    package provides it. What no file says of itself, such as the role of a
    run, a person adds to the table.

    A file that cannot be read is left out, with a warning. So is a folder
    that is a link, with all it holds, but without one.

    Raises ``ValueError`` if the rows are not a table of ``FolderFiles``, such
    as two files of one run.
    """
    rows = []
    for path in sorted(p for p in directory.rglob('*') if p.is_file()):
        relative = path.relative_to(directory)
        if relative.parts[0] in _OWN:
            continue
        try:
            metadata = read(path)
        except OSError as error:
            warnings.warn(f'{relative} is left out: {error}', stacklevel=2)
            continue
        if metadata is not None:
            rows.append({'file': str(relative), **metadata})
    rows.sort(key=_measured)
    FolderFiles(directory, rows)
    return rows


def _empty(cell: Any) -> bool:
    """Whether a cell holds nothing: no text, or what pandas reads a gap as."""
    if cell is None or cell is pd.NA:
        return True
    return isinstance(cell, str | float) and (cell == '' or cell != cell)


def _text(cell: Any) -> str:
    """A cell that names a run or a file as text; ``1.0`` as ``'1'``."""
    cell = cell.item() if isinstance(cell, np.generic) else cell
    # pandas holds whole numbers next to a gap as floats
    return str(int(cell) if isinstance(cell, float) and cell.is_integer() else cell)


def described(files: pd.DataFrame, run_table: pd.DataFrame) -> pd.DataFrame:
    """
    The files table (see :func:`scan_folder`) with what a person's run
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
        key = _text(record[column])
        if key in by[column]:
            raise ValueError(f'two rows of the run table describe the {column} {key}')
        by[column][key] = {
            name: cell
            for name, cell in record.items()
            if name not in by and not _empty(cell)
        }
    rows = [
        record
        | by['file'].get(_text(record['file']), {})
        | ({} if _empty(run := record.get('run')) else by['run'].get(_text(run), {}))
        for record in files.to_dict('records')
    ]
    columns = dict.fromkeys(
        [*files.columns, *run_table.columns.drop(list(by), 'ignore')]
    )
    return pd.DataFrame(rows, columns=list(columns)).fillna('')


def folder_files(directory: Path) -> FolderFiles:
    """
    The files that ``files.csv`` in ``directory`` lists. A path in it is
    relative to ``directory``, and an empty cell is metadata that the file
    lacks. A file in ``directory`` without ``pid`` is identified by its path
    relative to it, so moving the folder keeps every identity (see
    :class:`~ess.apps.FolderFiles`). Raises ``FileNotFoundError`` without
    ``files.csv``.
    """
    with (directory / 'files.csv').open(newline='') as f:
        table = [{k: v for k, v in row.items() if v != ''} for row in csv.DictReader(f)]
    return FolderFiles(directory, table, root=directory)


def write_files_table(directory: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    """
    Replace ``files.csv`` in ``directory`` with a table of ``rows`` at once
    (:func:`~ess.apps.files.replace_at_once`), so that no reader, such as
    :class:`ArrivingFiles`, sees half of it. The columns are the keys of the
    rows, in the order they first appear; ``None`` is an empty cell.
    """
    columns = list(dict.fromkeys(key for row in rows for key in row))
    text = io.StringIO()
    writer = csv.DictWriter(text, columns, lineterminator='\n')
    writer.writeheader()
    for row in rows:
        writer.writerow({k: '' if row.get(k) is None else row[k] for k in columns})
    replace_at_once(directory / 'files.csv', lambda p: p.write_text(text.getvalue()))


class ArrivingFiles:
    """
    The files that ``files.csv`` in ``directory`` lists (:func:`folder_files`),
    read again whenever the file was replaced or changed, so that files
    registered while a client is open are found. Replace ``files.csv`` at once,
    with :func:`write_files_table`, so that no read sees half of it.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._lock = threading.Lock()
        self._stamp: tuple[int, int] | None = None
        self._files: FolderFiles | None = None

    def _current(self) -> FolderFiles:
        stat = (self._directory / 'files.csv').stat()
        stamp = (stat.st_ino, stat.st_mtime_ns)
        with self._lock:
            if self._files is None or stamp != self._stamp:
                self._files = folder_files(self._directory)
                self._stamp = stamp
            return self._files

    def resolve(self, name: FileRef) -> FileRef:
        return self._current().resolve(name)

    def path(self, ref: FileRef) -> Path:
        return self._current().path(ref)

    def metadata(self, ref: FileRef) -> dict[str, Any]:
        return self._current().metadata(ref)

    def list(self, selector: Selector) -> list[FileRef]:
        return self._current().list(selector)


def open_folder(
    directory: Path,
    bind: Sequence[Binding],
    *,
    files: FileSource | None = None,
    proposal: str = 'local',
    submitter: str = 'user',
    workers: int = 1,
) -> Client:
    """
    A client of the workspace kept in ``directory`` (see the module), running the
    workflows of ``bind`` in this process. Closing the client closes the
    workspace.

    ``files`` is the file source, :func:`folder_files` if not given. Give a
    wrapper of that, such as :class:`~ess.apps.batch.CorrectedFiles`, to
    correct what ``files.csv`` says while the client is open. Raises
    ``FileNotFoundError`` without ``files.csv``, and ``RuntimeError`` if another
    process holds the folder. ``workers`` is the number of threads that run
    requests (see :class:`~ess.apps.Workspace`).
    """
    workspace = Workspace(
        proposal=proposal,
        files=folder_files(directory) if files is None else files,
        bind=bind,
        workers=workers,
        log=Log(directory / 'log.jsonl'),
        store=FolderStore(directory / 'store'),
    )
    return Client(workspace, submitter=submitter, on_close=workspace.close)
