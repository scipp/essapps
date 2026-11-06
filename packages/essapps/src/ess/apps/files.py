# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Naming files, and the file source that knows them.

A person names a file by what they know: a run number, a path, or a PID.
The backend asks its file source to resolve such a name to the file's
identity when a request is submitted, so a record names the file itself.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from ess.spec import File, FileRef


def file(
    *, run: int | None = None, path: Path | str | None = None, pid: str | None = None
) -> FileRef:
    """
    A file by its run number, its path, or the PID of the SciCat dataset
    that holds it, as a request writes it. The backend resolves it to the
    file's identity when the request is submitted, and the record holds that
    identity in place of the name. Exactly one of the three is given::

        file(run=60339)
        file(path='/home/user/data/run1.h5')
        file(pid='20.500.12269/vanadium')

    A string where a file field is expected is refused: write ``file(path=...)``.
    The record holds no metadata; read it with :meth:`ess.apps.Files.metadata`.
    """
    given = {'run': run, 'path': path, 'pid': pid}
    named = {kind: value for kind, value in given.items() if value is not None}
    if len(named) != 1:
        raise ValueError(f'name a file by exactly one of {list(given)}')
    ((kind, value),) = named.items()
    return FileRef.at(value) if kind == 'path' else FileRef(file=f'{kind}:{value}')


class Selector:
    """
    Which files to take: those whose metadata has the given values.

    A file has a kind, such as raw, derived, mask, or calibration; a
    selector matches raw files unless it names another kind. A run has a
    role, such as sample or background (see :class:`~ess.spec.File` for
    the keys and their values). Each given value must equal the file's::

        client.files.list(Selector(role='sample'))
    """

    def __init__(self, kind: str = 'raw', **fields: Any) -> None:
        self._fields = tuple(sorted({'kind': kind, **fields}.items()))

    @classmethod
    def of(cls, marker: File) -> Selector:
        """
        The files that may fill a field with ``marker``: those with each key and
        value of its metadata, but for raw files not ``role``. The backend refuses
        any other file.

        The role of a run says what the run is used for, which a request states by
        the field it fills: one run can be the transmission run of a can and the
        empty beam. So at a raw file field the role only narrows what a form offers
        (see :meth:`ess.apps.Listing.files`). At a file of another kind, such as a
        calibration, a role says what the file holds, as a kind does.
        """
        metadata = marker.metadata
        if metadata['kind'] == 'raw':
            metadata.pop('role', None)
        return cls(**metadata)

    @property
    def fields(self) -> dict[str, Any]:
        return dict(self._fields)

    def matches(self, metadata: dict[str, Any]) -> bool:
        metadata = {'kind': 'raw', **metadata}
        return all(metadata.get(k) == v for k, v in self._fields)

    def __repr__(self) -> str:
        return f'Selector({self.fields})'


def readable(metadata: dict[str, Any], proposal: str) -> bool:
    """Whether requests of ``proposal`` may read a file with this metadata."""
    return metadata.get('proposal', proposal) == proposal


class FolderFiles:
    """
    A file source over the files of a folder, with a table a person fills.

    The table has a row per file: ``file``, its name in the folder, and its
    metadata, such as ``run``, ``kind``, and ``role``. A row without ``kind`` is
    a raw file (see :class:`Selector`), and without ``proposal`` is open to all.
    The order of the rows is the order of measurement::

        FolderFiles('/data/test', [
            {'file': 'run-1.nxs', 'run': 1, 'role': 'sample'},
            {'file': 'mask.xml', 'kind': 'mask'},
        ])

    A file is identified by its path, and named by its path, its ``run``, or its
    ``pid``.

    Raises
    ------
    FileNotFoundError
        If the folder lacks a file of the table.
    ValueError
        If two rows name one file, or give one run or one PID.
    """

    def __init__(
        self, directory: Path | str, table: Iterable[Mapping[str, Any]]
    ) -> None:
        self._metadata: dict[FileRef, dict[str, Any]] = {}
        self._names: dict[FileRef, FileRef] = {}
        for row in table:
            metadata = dict(row)
            path = Path(directory).absolute() / metadata.pop('file')
            if not path.is_file():
                raise FileNotFoundError(path)
            ref = FileRef.at(path)
            if ref in self._metadata:
                raise ValueError(f'two rows name the file {path}')
            self._metadata[ref] = metadata
            for kind in ('run', 'pid'):
                if kind in metadata:
                    name = FileRef(file=f'{kind}:{metadata[kind]}')
                    if name in self._names:
                        raise ValueError(f'two rows give {name}')
                    self._names[name] = ref

    def resolve(self, name: FileRef) -> FileRef:
        if name in self._metadata:
            return name
        return self._names[name]

    def path(self, ref: FileRef) -> Path:
        return Path(ref.file.removeprefix('path:'))

    def metadata(self, ref: FileRef) -> dict[str, Any]:
        return dict(self._metadata[ref])

    def list(self, selector: Selector) -> list[FileRef]:
        return [r for r, m in self._metadata.items() if selector.matches(m)]
