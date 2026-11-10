# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A workspace kept in a folder, for one person on one machine.

The folder holds what a workspace needs to go on after its process ended::

    files.csv     the table of FolderFiles: file, run, kind, role, ...
    log.jsonl     history (see ess.apps.log)
    store/        persisted outputs, <record>/<output>.pickle

So a command that runs and ends, and the next one, are clients of one workspace:
the second reads the records of the first, and references their outputs if they
were persisted. One process holds the folder at a time, since a log has one
writer.

A file of the store is not for people: it is a pickle, which only the same
versions of the packages read back. :mod:`ess.apps.batch.export` writes the files
that a person keeps.
"""

from __future__ import annotations

import csv
import io
import pickle
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ess.spec import Binding

from .client import Client
from .files import FolderFiles
from .log import Log
from .workspace import Workspace

_ARRAYS = ('Variable', 'DataArray', 'Dataset')


def _from_hdf5(data: bytes) -> Any:
    import scipp as sc

    return sc.io.load_hdf5(io.BytesIO(data))


class _Pickler(pickle.Pickler):
    """
    Pickles a scipp array, which has no pickle of its own, as its HDF5 file.
    Any other value, a ``DataGroup`` among them, is pickled as it is.
    """

    def reducer_override(self, obj: Any) -> Any:
        tp = type(obj)
        if tp.__module__.partition('.')[0] != 'scipp' or tp.__name__ not in _ARRAYS:
            return NotImplemented
        data = io.BytesIO()
        obj.save_hdf5(data)
        return _from_hdf5, (data.getvalue(),)


class FolderStore:
    """
    A store of one file per output in ``directory``, ``<record>/<output>.pickle``.

    A value is read back as the workflow returned it, whatever its type, by the
    versions of the packages that wrote it.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def _path(self, record: str, output: str) -> Path:
        return self._directory / record / f'{output}.pickle'

    def write(self, record: str, output: str, value: Any) -> None:
        path = self._path(record, output)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('wb') as f:
            _Pickler(f).dump(value)

    def read(self, record: str, output: str) -> Any:
        try:
            with self._path(record, output).open('rb') as f:
                return pickle.load(f)  # noqa: S301
        except FileNotFoundError:
            raise LookupError(f'the store holds no {output} of {record}') from None


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
