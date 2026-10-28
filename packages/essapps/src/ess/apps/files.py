# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Naming files, and the file source that knows them.

A person names a file by what they know: a run number, a path, or a PID.
The backend asks its file source to resolve such a name to the file's
identity when a request is submitted, so a record names the file itself.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, Protocol

from ess.spec import FileRef


def file(
    *, run: int | None = None, path: Path | str | None = None, pid: str | None = None
) -> FileRef:
    """A reference to a file by its run number, its path, or its PID."""
    given = {'run': run, 'path': path, 'pid': pid}
    named = {kind: value for kind, value in given.items() if value is not None}
    if len(named) != 1:
        raise ValueError(f'name a file by exactly one of {list(given)}')
    ((kind, value),) = named.items()
    return FileRef(file=f'{kind}:{value}')


class Selector:
    """
    Which files to take: those whose metadata has the given values.

    A file has a kind, such as raw, derived, mask, or calibration; a
    selector matches raw files unless it names another kind.
    """

    def __init__(self, kind: str = 'raw', **fields: Any) -> None:
        self._fields = tuple(sorted({'kind': kind, **fields}.items()))

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


class FileSource(Protocol):
    """
    Which files exist, and what they hold.

    A backend has one. It resolves names and finds files through it, and
    answers through it the queries of its clients: listing files, waiting
    for new ones, and reading their metadata. A client sees only the files
    its proposal may read. Tests use a fake (``testing.FakeFiles``).
    """

    def resolve(self, name: FileRef) -> FileRef:
        """The identity of the named file; raises ``KeyError`` if unknown."""
        ...

    def path(self, ref: FileRef) -> Path:
        """
        A local path to the file's file, for a workflow to read; raises
        ``OSError`` if the file cannot be had.
        """
        ...

    def metadata(self, ref: FileRef) -> dict[str, Any]:
        """
        The file's current metadata.

        The field ``proposal`` names the proposal whose requests may read the
        file; a file without it, such as published data, is open to all.
        """
        ...

    def list(self, selector: Selector) -> list[FileRef]:
        """The files the selector matches, in the order they were measured."""
        ...

    def watch(self, selector: Selector) -> Iterator[FileRef]:
        """Matching files: the existing ones first, then new ones, each once."""
        ...
