# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""A file source that holds corrections to the metadata of files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ess.spec import FileRef

from ..files import Selector
from ..protocol import FileSource


class _Every(Selector):
    """Every file of any kind, whatever its metadata says now."""

    def matches(self, metadata: dict[str, Any]) -> bool:
        return True


class CorrectedFiles:
    """
    A file source over ``source``, with corrections a person makes.

    The workspace gets this source, so forms, rules, and sources of rows all see
    a file as corrected, as :class:`~ess.apps.protocol.FileSource` requires::

        files = CorrectedFiles(FolderFiles('/data', table))
        client = local(proposal='p1', files=files, bind=BINDINGS)
        files.correct(run, role='sample')

    Listing asks ``source`` for every file and selects by the corrected
    metadata, so the source must select through :meth:`Selector.matches`, as
    those that read a folder or a table do. A name such as ``file(run=1)`` is
    resolved by ``source``, so a correction of ``run`` does not rename a file.
    """

    def __init__(self, source: FileSource) -> None:
        self._source = source
        self._corrections: dict[FileRef, dict[str, Any]] = {}

    def correct(self, ref: FileRef, **fields: Any) -> None:
        """Set metadata fields of a file, on top of what the source says."""
        self._corrections = {
            **self._corrections,
            ref: {**self._corrections.get(ref, {}), **fields},
        }

    def reset(self, ref: FileRef) -> None:
        """Forget the corrections of a file."""
        self._corrections = {k: v for k, v in self._corrections.items() if k != ref}

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
