# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Names of files, and a file source over the files of a folder.

A person names a file by a run number, a path, or a PID. When a request is
submitted, the workspace resolves the name to the file's identity (see
:meth:`~ess.apps.protocol.FileSource.resolve`).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from ess.spec import File, FileRef


def file(
    *, run: int | None = None, path: Path | str | None = None, pid: str | None = None
) -> FileRef:
    """
    The name of a file in a request, by run number, path, or SciCat PID::

        file(run=60339)
        file(path='/home/user/data/run1.h5')
        file(pid='20.500.12269/vanadium')

    A relative path is relative to the working directory and is made absolute
    here. To name a file by its path relative to the workspace's folder, write
    its identity: ``FileRef(file='path:masks/main.xml')``.

    A record holds no metadata of the file. :meth:`ess.apps.Files.metadata` returns it.
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
    The files whose metadata has the given values::

        client.files.list(Selector(role='sample'))

    A file has a kind, such as raw, mask, or calibration. A file without
    ``kind`` is raw, and a selector without ``kind`` matches raw files.
    :class:`~ess.spec.File` lists the keys and their values.
    """

    def __init__(self, kind: str = 'raw', **fields: Any) -> None:
        self._fields = tuple(sorted({'kind': kind, **fields}.items()))

    @classmethod
    def of(cls, marker: File) -> Selector:
        """
        The files that may fill a field with ``marker``.

        These are the files with each key and value of the marker's metadata,
        except ``role`` for raw files. The workspace rejects any other file.

        A request states what a run is used for by the field it fills, and one
        run can be both the transmission run of a can and the empty beam. So at
        a raw file field the role only narrows what a form offers (see
        :meth:`ess.apps.Listing.files`). At a file of another kind, such as a
        calibration, the role states what the file holds, as the kind does.
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
    The PID of the raw file with ``uuid`` in ``entry/entry_identifier_uuid``.

    ``catalogue_identifier('abc')`` returns ``'20.500.12269/abc'``.
    """
    return f'20.500.12269/{uuid}'


def readable(metadata: dict[str, Any], proposal: str) -> bool:
    """Whether requests of ``proposal`` may read a file with this metadata."""
    return metadata.get('proposal', proposal) == proposal


class FolderFiles:
    """
    A file source over the files of a folder, described by a table.

    The table has a row per file: ``file``, its path in the folder, and its
    metadata, such as ``run``, ``kind``, ``role``, and ``pid``. The rows are in
    the order of measurement::

        FolderFiles('/data/test', [
            {'file': 'run-1.nxs', 'run': 1, 'role': 'sample'},
            {'file': 'masks/main.xml', 'kind': 'mask'},
        ], root='/data/test')

    A file with a ``pid`` is identified by it, so that every file source that
    lists the file returns one identity. Any other file is identified by its
    real path, relative to the real path of ``root`` if it is inside it::

        pid:20.500.12269/<uuid>     a row with pid 20.500.12269/<uuid>
        path:masks/main.xml         a file inside root
        path:/data/other/run-1.nxs  a file outside root, or without root

    ``root`` is the folder of a workspace, so that moving the folder keeps the
    identity of each file in it. A file is named by its identity, its ``run``,
    its ``pid``, or its absolute path, also through a symbolic link.

    Raises
    ------
    FileNotFoundError
        If the folder lacks a file of the table.
    ValueError
        If two rows have one file, one run, or one PID, or a PID lacks its
        prefix, as in ``<uuid>`` for ``20.500.12269/<uuid>``.
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
        """``ref``, if a row of the table has this identity."""
        if ref not in self._paths:
            raise KeyError(f'the file source does not know the file {ref.file}')
        return ref

    def list(self, selector: Selector) -> list[FileRef]:
        return [r for r, m in self._metadata.items() if selector.matches(m)]


def replace_at_once(path: Path, write: Callable[[Path], None]) -> None:
    """
    Replaces ``path`` with the file that ``write`` writes, at once.

    ``write`` writes a file with the same suffix next to ``path``, which is then
    renamed to ``path``. A reader in another process sees the old file or the
    new one, never half of one.
    """
    partial = path.with_name(f'.{path.stem}.partial{path.suffix}')
    try:
        write(partial)
        partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)


def _written_in_full(pid: str) -> bool:
    """Whether ``pid`` has the form ``<prefix>/<suffix>`` of a catalogue identifier."""
    prefix, slash, suffix = pid.partition('/')
    return bool(prefix and slash and suffix)
