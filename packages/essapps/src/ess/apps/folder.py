# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A workspace stored in a folder, for one person on one machine.

It needs scipp, h5py and pandas (the extra ``files``). The folder holds what a
workspace needs to continue after its process ended::

    files.csv     the table of FolderFiles: file, run, pid, kind, role, ...
    log.jsonl     history, one event per line (see ess.apps.log)
    store/        persisted outputs, <record>/<output>.h5
    definitions/  the batches of the folder (see ess.batch.definition)

So two commands, one after the other, are clients of one workspace. The second
reads the records of the first, and references their outputs if they were
persisted. One process holds the folder at a time, since a log has one writer.
"""

from __future__ import annotations

import csv
import io
import shutil
import threading
import warnings
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
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
    A store of one scipp HDF5 file per output, ``<directory>/<record>/<output>.h5``.

    :mod:`ess.apps.hdf5` describes the format and the types it accepts.
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

    def delete(self, record: str) -> None:
        with suppress(FileNotFoundError):
            shutil.rmtree(self._directory / record)

    def read(self, record: str, output: str, tp: Any | None) -> Any:
        try:
            return hdf5.read(tp, self.path(record, output))
        except FileNotFoundError:  # never written, or deleted by a clear
            raise LookupError(f'the store holds no {output} of {record}') from None
        except ValueError as error:  # does not fit tp
            raise LookupError(str(error)) from None


_OWN = ('files.csv', 'log.jsonl', 'store', 'definitions')
"""The entries of a workspace folder that hold no data files, which a scan skips."""


def _measured(row: Mapping[str, Any]) -> tuple[bool, int, str]:
    """Sorts the runs with a whole number first, by it, and the rest by path."""
    run = str(row.get('run', ''))
    return (not run.isdigit(), int(run) if run.isdigit() else 0, row['file'])


def scan_folder(
    directory: Path, read: Callable[[Path], Mapping[str, Any] | None]
) -> list[dict[str, Any]]:
    """
    The rows of ``files.csv`` for the files in ``directory`` that ``read`` knows.

    ``read`` returns the metadata of a file, such as ``{'kind': 'raw', 'run': 1}``,
    or ``None`` for a file that is not the workspace's. The rows hold the runs
    by number, which is the order of measurement, then the other files by path.
    A file that cannot be read is left out with a warning. A folder that is a
    symbolic link is left out without one.

    Raises ``ValueError`` if the rows are not a table of ``FolderFiles``, such
    as for two files of one run.
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
    """Whether a cell is empty: no text, or what pandas reads for a gap."""
    if cell is None or cell is pd.NA:
        return True
    return isinstance(cell, str | float) and (cell == '' or cell != cell)


def _text(cell: Any) -> str:
    """A cell that refers to a run or a file, as text: ``1.0`` as ``'1'``."""
    cell = cell.item() if isinstance(cell, np.generic) else cell
    # pandas holds whole numbers next to a gap as floats
    return str(int(cell) if isinstance(cell, float) and cell.is_integer() else cell)


def described(files: pd.DataFrame, run_table: pd.DataFrame) -> pd.DataFrame:
    """
    ``files``, the rows of ``files.csv``, with the columns of a person's ``run_table``.

    A row of ``run_table`` describes the file of its ``run``. If that cell is
    empty or the column is missing, the row describes the file at its ``file``.
    The result has the rows of ``files``, and ignores a row of ``run_table``
    that describes no file. A cell of ``run_table`` that is not empty replaces
    the cell of the file in the same column.

    Raises ``ValueError`` if a row refers to neither a run nor a file, or two
    rows describe one file.
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
    The file source of ``files.csv`` in ``directory``.

    A path in ``files.csv`` is relative to ``directory``. An empty cell is
    metadata that the file lacks. ``directory`` is the ``root`` of the
    :class:`~ess.apps.FolderFiles`. Raises ``FileNotFoundError`` if
    ``files.csv`` is missing.
    """
    with (directory / 'files.csv').open(newline='') as f:
        table = [{k: v for k, v in row.items() if v != ''} for row in csv.DictReader(f)]
    return FolderFiles(directory, table, root=directory)


def write_files_table(directory: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    """
    Replaces ``files.csv`` in ``directory`` with ``rows``, at once.

    A reader such as :class:`ArrivingFiles` never sees half of the file. The
    columns are the keys of the rows, in the order they first appear. ``None``
    is an empty cell.
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
    The file source of ``files.csv`` in ``directory``, read again when the file changes.

    A file added while a client is open is found. Write ``files.csv`` with
    :func:`write_files_table`, so that no read sees half of it. Without
    ``files.csv``, it lists no file.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._lock = threading.Lock()
        self._stamp: tuple[int, int] | None = None
        self._files: FolderFiles | None = None

    def _current(self) -> FolderFiles:
        try:
            stat = (self._directory / 'files.csv').stat()
        except FileNotFoundError:
            return FolderFiles(self._directory, [], root=self._directory)
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
    A client of the workspace in ``directory``, running the workflows of ``bind``.

    The workflows run in ``workers`` threads of this process. Closing the client
    closes the workspace. ``files`` is ``folder_files(directory)`` if ``None``.
    A wrapper of it, such as :class:`~ess.batch.CorrectedFiles`, corrects the
    metadata in ``files.csv`` while the client is open.

    Raises ``FileNotFoundError`` if ``files`` is ``None`` and ``files.csv`` is
    missing, and
    ``RuntimeError`` if another process holds the folder.
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
