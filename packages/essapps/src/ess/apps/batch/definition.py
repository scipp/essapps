# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Where the definition of batches is kept: what a person makes of them, so that a
batch made again, after a restart or in another process, has it.

The definition is three tables, each with one writer:

    corrections   metadata of a file set by hand, by file     CorrectedFiles
    edits         cells set, rows added or removed, by key    Batch.rows
    settings      the parameters that are not columns         Batch.settings

The corrections are about files, and every batch of a workspace reads the
files through one :class:`~.corrections.CorrectedFiles`, so a workspace has
one table of them. The edits and the settings are of a label. A
:class:`DefinitionStore` keeps each table as plain data under a key::

    ('corrections',)
    ('labels', label, 'edits')
    ('labels', label, 'settings')

and its writer saves it whole at each change. Only :class:`FolderDefinitions`
knows that the store is files. A batch and a file source without a store keep
their tables in memory.

The plain data of each table, as the functions below give and read it::

    corrections   {'pid:20.500.12269/abc': {'role': 'sample'}}
    edits         {'added': [{'key': 'mine', 'values': {...}, 'info': {...}}],
                   'edited': {'60339': {'thickness': 2.0}},
                   'cleared': {'60339': ['sample_transmission']},
                   'removed': ['60340']}
    settings      the template of the settings, as a template file holds it

A value is in the JSON form that a record holds, a file as
``{'@ref': 'file', 'file': 'pid:...'}``. The edits hold only what was set by
hand, so a cell from the scan keeps following the scan. The settings are a
template so that the command ``essapps-batch`` reads them as one.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from pydantic import TypeAdapter
from pydantic_core import from_json, to_json

from ess.spec import FileRef
from ess.spec.refs import ParamsError, map_refs

from ..records import Template
from .table import EMPTY, Row, RowTable

type Key = tuple[str, ...]
"""Names a table: ``('corrections',)``, or ``('labels', label, part)``."""

CORRECTIONS: Key = ('corrections',)


def edits_key(label: str) -> Key:
    return ('labels', label, 'edits')


def settings_key(label: str) -> Key:
    return ('labels', label, 'settings')


class DefinitionStore(Protocol):
    """
    Keeps each table of the definition as plain data, under its :data:`Key`.

    ``save`` takes data that :func:`pydantic_core.to_json` writes, and raises
    if it cannot keep it. ``load`` gives it back in its JSON form, or ``None``
    if nothing was saved under the key.
    """

    def load(self, key: Key) -> Any | None: ...

    def save(self, key: Key, data: Any) -> None: ...


class FolderDefinitions:
    """
    A :class:`DefinitionStore` of JSON files in ``definitions/`` of a
    workspace's folder (see :mod:`ess.apps.folder`)::

        definitions/corrections.json
        definitions/labels/auto%2Fiofq/edits.json
        definitions/labels/auto%2Fiofq/settings.json

    Each name of a key is a folder or a file, with ``/``, ``%`` and ``.``
    written as ``%2F``, ``%25`` and ``%2E``, so that no label reaches out of
    its folder. A file is written whole: to a temporary file, which then
    replaces it, so a reader never sees half of one.
    """

    def __init__(self, directory: Path) -> None:
        self._root = directory / 'definitions'

    def __str__(self) -> str:
        return str(self._root)

    def path(self, key: Key) -> Path:
        """The file that holds the table of ``key``, written or not."""
        if not key or '' in key:
            raise ValueError(f'{key}: a key is names that are not empty')
        names = [quote(name, safe='').replace('.', '%2E') for name in key]
        return self._root.joinpath(*names[:-1], f'{names[-1]}.json')

    def load(self, key: Key) -> Any | None:
        path = self.path(key)
        try:
            return from_json(path.read_bytes())
        except FileNotFoundError:
            return None
        except ValueError as error:
            raise ValueError(f'{path}: {error}') from None

    def save(self, key: Key, data: Any) -> None:
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = to_json(data, indent=1)
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f'.{path.name}.', delete=False
        ) as f:
            try:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            except BaseException:
                os.unlink(f.name)
                raise
        os.replace(f.name, path)


def _values(values: Mapping[str, Any]) -> dict[str, Any]:
    """Cells in their JSON form as the values a row holds: each file a FileRef."""
    return map_refs(values, lambda ref: ref)


def edits_data(rows: RowTable) -> dict[str, Any]:
    """What was set by hand on ``rows``, as plain data (see the module)."""
    edited: dict[str, Mapping[str, Any]] = {}
    cleared: dict[str, list[str]] = {}
    for key, cells in rows.edits.items():
        if not any(v is EMPTY for v in cells.values()):
            edited[key] = cells  # most edits clear nothing
            continue
        if given := {c: v for c, v in cells.items() if v is not EMPTY}:
            edited[key] = given
        cleared[key] = [c for c, v in cells.items() if v is EMPTY]
    return {
        'added': [
            {'key': r.key, 'values': dict(r.values), 'info': dict(r.info)}
            for r in rows.added
        ],
        'edited': edited,
        'cleared': cleared,
        'removed': sorted(rows.removed),
    }


def edits_of(data: Mapping[str, Any]) -> RowTable:
    """
    The table, without a scan, of the edits of :func:`edits_data`. Raises
    ``ValueError`` naming the row of a value that is not one.
    """
    entry = ''
    try:
        added = []
        for row in data.get('added', []):
            entry = f'row {row["key"]} added'
            added.append(Row(row['key'], _values(row['values']), row['info']))
        edits: dict[str, dict[str, Any]] = {}
        for key, cells in data.get('edited', {}).items():
            entry = f'row {key}'
            edits[key] = _values(cells)
        for key, columns in data.get('cleared', {}).items():
            edits[key] = {**edits.get(key, {}), **dict.fromkeys(columns, EMPTY)}
        return RowTable(
            added=tuple(added), edits=edits, removed=frozenset(data.get('removed', []))
        )
    except (KeyError, TypeError, ParamsError) as error:
        why = f'no {error}' if isinstance(error, KeyError) else str(error)
        raise ValueError(f'{entry or "the edits"}: {why}') from None


def settings_data(template: Template) -> Any:
    """The template of the settings, as plain data that a template file holds."""
    return TypeAdapter(Template).dump_python(template, mode='json')


def settings_of(data: Any) -> dict[str, Any]:
    """
    The settings in the template of :func:`settings_data`, of whatever spec
    and blanks it names: a value that the spec bound now does not take is
    refused when the settings are resolved, as any other.
    """
    return dict(TypeAdapter(Template).validate_python(data).params)


def corrections_data(
    corrections: Mapping[FileRef, Mapping[str, Any]],
) -> dict[str, Any]:
    """The corrections, by file, as plain data."""
    return {ref.file: dict(fields) for ref, fields in corrections.items()}


def corrections_of(data: Mapping[str, Any]) -> dict[FileRef, dict[str, Any]]:
    """The corrections of :func:`corrections_data`, by file."""
    return {FileRef(file=f): dict(fields) for f, fields in data.items()}
