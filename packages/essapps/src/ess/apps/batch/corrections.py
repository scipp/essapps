# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""A file source that holds corrections to the metadata of files."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ess.spec import FileRef

from ..files import Selector
from ..protocol import FileSource
from . import definition
from .definition import DefinitionStore


class _Every(Selector):
    """Every file of any kind, whatever its metadata says now."""

    def matches(self, metadata: dict[str, Any]) -> bool:
        return True


class CorrectedFiles:
    """
    A file source over ``source``, with corrections a person makes.

    The workspace gets this source, so forms and sources of rows all see a file
    as corrected, as :class:`~ess.apps.protocol.FileSource` requires::

        files = CorrectedFiles(FolderFiles('/data', table))
        client = local(proposal='p1', files=files, bind=BINDINGS)
        files.correct(run, role='sample')

    Listing asks ``source`` for every file and selects by the corrected
    metadata, so the source must select through :meth:`Selector.matches`, as
    those that read a folder or a table do. A name such as ``file(run=1)`` is
    resolved by ``source``, so a correction of ``run`` does not rename a file.

    ``store`` keeps the corrections (:mod:`~.definition`): they are read here,
    and written at each change. Without it, they are kept in memory only. A
    correction of a file that ``source`` no longer lists stays, unused, and
    applies again if the file is listed again.
    """

    def __init__(
        self, source: FileSource, *, store: DefinitionStore | None = None
    ) -> None:
        self._source = source
        self._store = store
        self._corrections: dict[FileRef, dict[str, Any]] = {}
        if store is not None and (data := store.load(definition.CORRECTIONS)):
            self._corrections = definition.corrections_of(data)

    @property
    def source(self) -> FileSource:
        """The source corrected, to read what a file says without corrections."""
        return self._source

    def _write(self, corrections: dict[FileRef, dict[str, Any]]) -> None:
        if self._store is not None:
            data = definition.corrections_data(corrections)
            self._store.save(definition.CORRECTIONS, data)
        self._corrections = corrections

    def correct(self, ref: FileRef, **fields: Any) -> None:
        """Set metadata fields of a file, on top of what the source says."""
        self._write(
            {**self._corrections, ref: {**self._corrections.get(ref, {}), **fields}}
        )

    def corrections(self, ref: FileRef) -> Mapping[str, Any]:
        """The fields of a file set by :meth:`correct`, empty if none."""
        return MappingProxyType(self._corrections.get(ref, {}))

    def reset(self, ref: FileRef, *fields: str) -> None:
        """Forget the corrections of a file, or only those of ``fields``."""
        kept = {
            k: v
            for k, v in self._corrections.get(ref, {}).items()
            if fields and k not in fields
        }
        others = {k: v for k, v in self._corrections.items() if k != ref}
        self._write({**others, ref: kept} if kept else others)

    def resolve(self, name: FileRef) -> FileRef:
        return self._source.resolve(name)

    def path(self, ref: FileRef) -> Path:
        return self._source.path(ref)

    def metadata(self, ref: FileRef) -> dict[str, Any]:
        return {**self._source.metadata(ref), **self._corrections.get(ref, {})}

    def list(self, selector: Selector) -> list[FileRef]:
        return [
            ref
            for ref in self._source.list(_Every())
            if selector.matches(self.metadata(ref))
        ]
