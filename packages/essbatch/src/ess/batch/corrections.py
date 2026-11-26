# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
A file source that applies corrections to the metadata of files, and a
workspace folder opened with it (:func:`open_workspace`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ess.apps import Client
from ess.apps.files import Selector
from ess.apps.folder import ArrivingFiles, open_folder
from ess.apps.protocol import FileSource
from ess.spec import Binding, FileRef

from . import definition
from .definition import DefinitionStore, FolderDefinitions


class _Every(Selector):
    """Matches every file, whatever its metadata."""

    def matches(self, metadata: dict[str, Any]) -> bool:
        return True


class CorrectedFiles:
    """
    A file source that applies, on top of ``source``, the corrections a person
    makes.

    Give the workspace this source, so that forms and row sources see the
    corrected metadata::

        files = CorrectedFiles(FolderFiles('/data', table))
        client = local(proposal='p1', files=files, bind=BINDINGS)
        files.correct(run, role='sample')

    :meth:`list` lists every file of ``source`` and selects by the corrected
    metadata. So ``source`` must select through :meth:`Selector.matches`, as
    the sources of a folder and of a files table do. ``source`` resolves a
    name such as ``file(run=1)``, so a correction of ``run`` does not rename a
    file.

    With ``definitions``, the corrections are read once, here, and stored at
    each change. A change that cannot be stored is not made. Without
    ``definitions``, the corrections are held in memory only. A correction of
    a file that ``source`` no longer lists stays, and applies again when the
    file is listed again.

    Raises ``ValueError`` if ``definitions`` hold corrections that cannot be
    read.
    """

    def __init__(
        self, source: FileSource, *, definitions: DefinitionStore | None = None
    ) -> None:
        self._source = source
        self._definitions = definitions
        self._corrections: dict[FileRef, dict[str, Any]] = {}
        if definitions is not None:
            kept = definitions.load(definition.CORRECTIONS, definition.corrections_of)
            self._corrections = kept or {}

    @property
    def source(self) -> FileSource:
        """The source without the corrections."""
        return self._source

    def _write(self, corrections: dict[FileRef, dict[str, Any]]) -> None:
        if self._definitions is not None:
            data = definition.corrections_data(corrections)
            self._definitions.save(definition.CORRECTIONS, data)
        self._corrections = corrections

    def correct(self, ref: FileRef, **fields: Any) -> None:
        """Sets metadata fields of a file, over those of the source."""
        self.correct_each([ref], **fields)

    def correct_each(self, refs: Iterable[FileRef], **fields: Any) -> None:
        """Sets metadata fields of each of ``refs`` in one write: all or none."""
        corrections = dict(self._corrections)
        for ref in refs:
            corrections[ref] = {**corrections.get(ref, {}), **fields}
        self._write(corrections)

    def corrections(self, ref: FileRef) -> Mapping[str, Any]:
        """The fields of a file set by :meth:`correct`, empty if none."""
        return MappingProxyType(self._corrections.get(ref, {}))

    def reset(self, ref: FileRef, *fields: str) -> None:
        """Removes the corrections of a file, or only those of ``fields``."""
        self.reset_each([ref], *fields)

    def reset_each(self, refs: Iterable[FileRef], *fields: str) -> None:
        """
        Removes the corrections of each of ``refs``, or only those of
        ``fields``, in one write: all or none.
        """
        corrections = dict(self._corrections)
        for ref in refs:
            kept = {
                k: v
                for k, v in corrections.pop(ref, {}).items()
                if fields and k not in fields
            }
            if kept:
                corrections[ref] = kept
        self._write(corrections)

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


def open_workspace(
    directory: Path,
    bind: Sequence[Binding],
    *,
    source: FileSource | None = None,
    proposal: str = 'local',
) -> tuple[Client, CorrectedFiles, FolderDefinitions]:
    """
    A client of the workspace folder ``directory``, its corrected file source,
    and the folder's definitions.

    The corrections are stored in the folder. The file source corrects
    ``source``, or by default an :class:`~ess.apps.folder.ArrivingFiles`,
    which reads ``files.csv`` again when it is replaced: a file that arrives
    while the client is open is listed.

    Raises as :func:`ess.apps.folder.open_folder` does, and ``ValueError`` if
    the definitions hold corrections that cannot be read.
    """
    definitions = FolderDefinitions(directory)
    files = CorrectedFiles(
        ArrivingFiles(directory) if source is None else source,
        definitions=definitions,
    )
    client = open_folder(directory, bind, files=files, proposal=proposal)
    return client, files, definitions
