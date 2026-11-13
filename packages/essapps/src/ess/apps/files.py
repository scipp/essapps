# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Naming files, and the file source that knows them.

A person names a file by what they know: a run number, a path, or a PID.
The workspace asks its file source to resolve such a name to the file's
identity when a request is submitted, so a record names the file itself.
A file the catalogue lists is identified by its catalogue identifier, any
other file by its path (see :class:`FolderFiles`).
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from ess.spec import File, FileRef


def file(
    *, run: int | None = None, path: Path | str | None = None, pid: str | None = None
) -> FileRef:
    """
    A file by its run number, its path, or the PID of the SciCat dataset
    that holds it, as a request writes it. The workspace resolves it to the
    file's identity when the request is submitted, and the record holds that
    identity in place of the name: ``pid:...`` for a file the catalogue lists,
    and ``path:...`` for any other (see :class:`FolderFiles`). Exactly one of
    the three is given::

        file(run=60339)
        file(path='/home/user/data/run1.h5')
        file(pid='20.500.12269/vanadium')

    A relative path is relative to the working directory, and is made absolute
    here. A path relative to the workspace's folder is written only as the
    identity a record holds, ``FileRef(file='path:masks/main.xml')``.

    A string where a file field is expected is refused: write ``file(path=...)``.
    The record holds no metadata; read it with :meth:`ess.apps.Files.metadata`.
    """
    given = {'run': run, 'path': path, 'pid': pid}
    named = {kind: value for kind, value in given.items() if value is not None}
    if len(named) != 1:
        raise ValueError(f'name a file by exactly one of {list(given)}')
    ((kind, value),) = named.items()
    if kind == 'path':
        return FileRef.at(Path(value).absolute())
    return FileRef(file=f'{kind}:{value}')


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
        value of its metadata, but for raw files not ``role``. The workspace refuses
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


def catalogue_identifier(uuid: str) -> str:
    """
    The PID under which the catalogue lists the raw file that carries ``uuid``
    in ``entry/entry_identifier_uuid``: ``20.500.12269/<uuid>``.
    """
    return f'20.500.12269/{uuid}'


def readable(metadata: dict[str, Any], proposal: str) -> bool:
    """Whether requests of ``proposal`` may read a file with this metadata."""
    return metadata.get('proposal', proposal) == proposal


class FolderFiles:
    """
    A file source over the files of a folder, with a table a person fills.

    The table has a row per file: ``file``, its name in the folder, and its
    metadata, such as ``run``, ``kind``, ``role``, and ``pid``. A row without
    ``kind`` is a raw file (see :class:`Selector`), and without ``proposal`` is
    open to all. The order of the rows is the order of measurement::

        FolderFiles('/data/test', [
            {'file': 'run-1.nxs', 'run': 1, 'role': 'sample'},
            {'file': 'masks/main.xml', 'kind': 'mask'},
        ], root='/data/test')

    A file with a ``pid``, one the catalogue lists, is identified by it, written
    in full, ``<prefix>/<suffix>``, so every source that lists the file gives it
    one identity. Any other file is identified by its real path, with symbolic
    links resolved: relative to the real path of ``root`` if it is inside it,
    and absolute if not or without ``root``::

        pid:20.500.12269/<uuid>     a row with pid 20.500.12269/<uuid>
        path:masks/main.xml         a file inside root
        path:/data/other/run-1.nxs  a file outside root

    ``root`` is the folder of a workspace (see :func:`ess.apps.folder.folder_files`),
    so that moving the folder keeps the identity of each file in it. A file is
    named by its identity, its ``run``, its ``pid``, or its absolute path, also
    one through a symbolic link.

    Raises
    ------
    FileNotFoundError
        If the folder lacks a file of the table.
    ValueError
        If two rows name one file, give one run or one PID, or a PID is not
        written in full.
    """

    def __init__(
        self,
        directory: Path | str,
        table: Iterable[Mapping[str, Any]],
        *,
        root: Path | str | None = None,
    ) -> None:
        root = None if root is None else Path(os.path.realpath(root))
        self._paths: dict[FileRef, Path] = {}
        self._metadata: dict[FileRef, dict[str, Any]] = {}
        self._names: dict[FileRef, FileRef] = {}
        for row in table:
            metadata = dict(row)
            path = Path(os.path.realpath(Path(directory, metadata.pop('file'))))
            if not path.is_file():
                raise FileNotFoundError(path)
            if FileRef.at(path) in self._names:
                raise ValueError(f'two rows name the file {path}')
            if root is not None and path.is_relative_to(root):
                local = FileRef.at(path.relative_to(root))
            else:
                local = FileRef.at(path)
            pid = metadata.get('pid')
            if pid is not None and not _written_in_full(str(pid)):
                raise ValueError(
                    f'the pid {pid!r} of {path} is not written in full: a catalogue '
                    'identifier is written with its prefix, such as '
                    '20.500.12269/<uuid>'
                )
            ref = local if pid is None else FileRef(file=f'pid:{pid}')
            names = {ref, local, FileRef.at(path)}
            if 'run' in metadata:
                names.add(FileRef(file=f'run:{metadata["run"]}'))
            for name in names:
                if name in self._names:
                    raise ValueError(f'two rows give {name}')
                self._names[name] = ref
            self._paths[ref] = path
            self._metadata[ref] = metadata

    def resolve(self, name: FileRef) -> FileRef:
        kind, _, value = name.file.partition(':')
        if kind == 'path' and os.path.isabs(value):
            name = FileRef.at(os.path.realpath(value))
        return self._names[name]

    def path(self, ref: FileRef) -> Path:
        return self._paths[self._known(ref)]

    def metadata(self, ref: FileRef) -> dict[str, Any]:
        return dict(self._metadata[self._known(ref)])

    def _known(self, ref: FileRef) -> FileRef:
        """
        ``ref``, if it is the identity of a file of the table. A record may hold
        one that is not, of a file moved or given a ``pid`` since it was made.
        """
        if ref not in self._paths:
            raise KeyError(f'the file source does not know the file {ref.file}')
        return ref

    def list(self, selector: Selector) -> list[FileRef]:
        return [r for r, m in self._metadata.items() if selector.matches(m)]


def _written_in_full(pid: str) -> bool:
    """Whether ``pid`` has the form ``<prefix>/<suffix>`` of a catalogue identifier."""
    prefix, slash, suffix = pid.partition('/')
    return bool(prefix and slash and suffix)
