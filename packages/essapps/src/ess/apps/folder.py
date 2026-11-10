# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A workspace kept in a folder, for one person on one machine. It needs scipp
and h5py.

The folder holds what a workspace needs to go on after its process ended::

    files.csv     the table of FolderFiles: file, run, kind, role, ...
                  (written by hand, or started with scan_folder)
    log.jsonl     history (see ess.apps.log)
    store/        persisted outputs, <record>/<output>.h5

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
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ess.spec import Binding

from . import hdf5
from .client import Client
from .files import FolderFiles
from .log import Log
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

    def read(self, record: str, output: str, tp: Any) -> Any:
        path = self.path(record, output)
        if not path.is_file():
            raise LookupError(f'the store holds no {output} of {record}')
        return hdf5.read(tp, path)


_OWN = ('files.csv', 'log.jsonl', 'store')
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

    Raises ``ValueError`` if the rows are not a table of ``FolderFiles``, such
    as two files of one run.
    """
    rows = []
    for path in sorted(p for p in directory.rglob('*') if p.is_file()):
        relative = path.relative_to(directory)
        if relative.parts[0] in _OWN:
            continue
        if (metadata := read(path)) is not None:
            rows.append({'file': str(relative), **metadata})
    rows.sort(key=_measured)
    FolderFiles(directory, rows)
    return rows


def open_folder(
    directory: Path,
    bind: Sequence[Binding],
    *,
    proposal: str = 'local',
    submitter: str = 'user',
) -> Client:
    """
    A client of the workspace kept in ``directory`` (see the module), running the
    workflows of ``bind`` in this process. Closing the client closes the
    workspace.

    A path in ``files.csv`` is relative to ``directory``, and an empty cell is
    metadata that the file lacks. Raises ``FileNotFoundError`` without
    ``files.csv``, and ``RuntimeError`` if another process holds the folder.
    """
    with (directory / 'files.csv').open(newline='') as f:
        table = [{k: v for k, v in row.items() if v != ''} for row in csv.DictReader(f)]
    workspace = Workspace(
        proposal=proposal,
        files=FolderFiles(directory, table),
        bind=bind,
        log=Log(directory / 'log.jsonl'),
        store=FolderStore(directory / 'store'),
    )
    return Client(workspace, submitter=submitter, on_close=workspace.close)
