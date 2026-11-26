# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The definitions of the batches of a workspace: what persons make of them,
stored so that a batch read again after a restart is the same.

A :class:`DefinitionStore` stores each entry as plain data under a
:data:`Key`. Each entry has one writer, which saves it whole at each change.
The corrections are of files, which all batches share, so they are one entry
of the workspace. All other entries are of a label (:data:`LABEL_ENTRIES`).
Each entry is validated when read: data of another shape raises ``ValueError``
naming the field.

The entries, their content and their writers: guide
``packages/essbatch/docs/batch.md``, "What is kept on disk".
"""

from __future__ import annotations

import os
import string
import tempfile
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Protocol, TypedDict
from urllib.parse import quote, unquote

from pydantic import (
    BaseModel,
    ConfigDict,
    TypeAdapter,
    ValidationError,
    field_validator,
)
from pydantic_core import from_json, to_json

from ess.apps.records import Template
from ess.spec import FileRef

from .table import data_of, table_of

type Key = tuple[str, ...]
"""Names an entry: ``('corrections',)``, or ``('labels', label, part)``."""

CORRECTIONS: Key = ('corrections',)


class DefinitionWriteError(OSError):
    """A :class:`DefinitionStore` could not write or delete an entry."""


class DefinitionStore(Protocol):
    """
    Stores each entry of the definitions as plain data, under its :data:`Key`.

    ``save`` accepts data that :func:`pydantic_core.to_json` can write, and
    raises :class:`DefinitionWriteError` if it cannot store it. ``load``
    returns ``read`` applied to the stored data in its JSON form, or ``None``
    if nothing is stored under the key. It raises a ``ValueError`` of ``read``
    with the place of the data added.
    """

    def load[T](self, key: Key, read: Callable[[Any], T]) -> T | None: ...

    def save(self, key: Key, data: Any) -> None: ...

    def delete(self, key: Key) -> None:
        """Delete the entry of ``key``, if any."""
        ...

    def labels(self) -> list[str]:
        """The labels with at least one entry, sorted."""
        ...


_NAME_MAX = 255
"""The bytes of a file name, on the file systems of Linux and macOS."""

# Moves with every change of what the files mean, such as where the source
# columns of a batch are stored, so that a folder of another format is rejected,
# not misread. No folder is migrated.
FORMAT = 2
"""The format of the files of :class:`FolderDefinitions`, in ``format.json``."""

_FORMAT_KEY: Key = ('format',)


class _Format(TypedDict):
    format: int


_FORMAT = TypeAdapter(_Format)


class FolderDefinitions:
    """
    A :class:`DefinitionStore` of JSON files in ``definitions/`` of a workspace
    folder (:mod:`ess.apps.folder`)::

        definitions/format.json
        definitions/corrections.json
        definitions/labels/auto%2Fiofq/header.json
        definitions/labels/auto%2Fiofq/table.json

    Each name of a key is a folder or a file, with ``/``, ``%`` and ``.``
    written as ``%2F``, ``%25`` and ``%2E``, so that no label reaches out of
    its folder. A file is written to a temporary file next to it, which then
    replaces it, so a reader never sees half of one. A temporary file left by
    an interrupted write stays until its entry is deleted, since it cannot be
    told from that of a write of another process.

    ``format.json`` holds :data:`FORMAT`, written before the first entry.
    Raises ``ValueError`` for a folder with labels and no ``format.json``, or
    of another format.
    """

    def __init__(self, directory: Path) -> None:
        self._root = directory / 'definitions'
        self._formatted = False  # format.json is written before the first entry
        if not self.labels():
            return
        found = self.load(_FORMAT_KEY, lambda data: _checked(_FORMAT, data)['format'])
        if found != FORMAT:
            given = 'none (no format.json)' if found is None else found
            raise ValueError(
                f'{self._root}: the definitions are of format {given}, and this '
                f'version reads format {FORMAT}; make a new folder'
            )
        self._formatted = True

    def __str__(self) -> str:
        return str(self._root)

    def path(self, key: Key) -> Path:
        """The file that holds the entry of ``key``, written or not."""
        if not key or '' in key:
            raise ValueError(f'{key}: a key is names that are not empty')
        names = [quote(name, safe='').replace('.', '%2E') for name in key]
        names[-1] = f'{names[-1]}.json'
        for name, written in zip(key, names, strict=True):
            # the temporary file of a write adds a dot and a suffix of 8
            if len(written.encode()) + 10 > _NAME_MAX:
                raise ValueError(
                    f'{name!r} is too long to name a file of the definitions: '
                    f'{len(written.encode())} bytes written as {written[:20]}...'
                )
        return self._root.joinpath(*names)

    def load[T](self, key: Key, read: Callable[[Any], T]) -> T | None:
        path = self.path(key)
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            return None
        try:
            return read(from_json(content))
        except ValueError as error:
            raise ValueError(f'{path}: {error}') from None

    def save(self, key: Key, data: Any) -> None:
        if not self._formatted:
            self._write(self.path(_FORMAT_KEY), {'format': FORMAT})
            self._formatted = True
        self._write(self.path(key), data)

    def _write(self, path: Path, data: Any) -> None:
        """Write ``data`` to ``path`` whole."""
        content = to_json(data, indent=1)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
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
        except OSError as error:
            raise DefinitionWriteError(f'{path}: {error.strerror or error}') from error

    def delete(self, key: Key) -> None:
        path = self.path(key)
        try:
            path.unlink(missing_ok=True)
            for left in path.parent.glob(f'.{path.name}.*'):
                left.unlink(missing_ok=True)
        except OSError as error:
            raise DefinitionWriteError(f'{path}: {error.strerror or error}') from error
        if path.parent != self._root:
            with suppress(OSError):  # not empty: the label has another entry
                path.parent.rmdir()

    def labels(self) -> list[str]:
        folder = self._root / 'labels'
        if not folder.is_dir():
            return []
        return sorted(
            unquote(p.name)
            for p in folder.iterdir()
            if any(not f.name.startswith('.') for f in p.iterdir())
        )


def _checked[T](adapter: TypeAdapter[T], data: Any) -> T:
    """``data`` validated by ``adapter``; the ``ValueError`` names each bad field."""
    try:
        return adapter.validate_python(data)
    except ValidationError as error:
        said = [
            f'{".".join(map(str, e["loc"])) or "the whole"}: {e["msg"]}'
            for e in error.errors()
        ]
        raise ValueError('; '.join(said)) from None


@dataclass(frozen=True)
class Header:
    """The title and description of a batch. ``ValueError`` for an empty title."""

    title: str
    description: str = ''

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError('a batch has a title that is not empty')


@dataclass(frozen=True)
class SourceEntry:
    """
    What a batch made from a row source stores of it, in ``source.json``.

    ``seen_keys`` are the keys of the rows of the source that the batch has
    seen (guide, "Follow").
    """

    __pydantic_config__ = ConfigDict(extra='forbid')

    row_source: str
    seen_keys: frozenset[str]
    follows: bool


class OutputOptions(BaseModel, frozen=True):
    """
    How the results of a batch are written (:mod:`.writing`).

    ``format`` names a writer (:class:`ess.apps.writers.Writer`). ``outputs``
    are the outputs of the spec to write; a writer of fixed outputs ignores
    them. ``name`` names the result of a row. It may hold ``{title}`` of the
    batch, and ``{name}`` and ``{key}`` of the row, without a format spec. The
    other fields are those of :class:`ess.apps.writers.DatasetMetadata`.
    """

    model_config = ConfigDict(extra='forbid')

    format: str
    outputs: tuple[str, ...] = ()
    name: str = '{title} {name}'
    description: str = ''
    keywords: tuple[str, ...] = ()
    owner: str = ''
    contact_email: str = ''

    @field_validator('name')
    @classmethod
    def _pattern(cls, name: str) -> str:
        try:
            fields = [
                (field, spec, conversion)
                for _, field, spec, conversion in string.Formatter().parse(name)
                if field is not None
            ]
        except ValueError as error:  # an unmatched brace
            raise ValueError(f'a name with braces that do not match: {error}') from None
        if bad := [
            f for f, spec, conv in fields if f not in _NAME_FIELDS or spec or conv
        ]:
            raise ValueError(
                f'a name may hold {{title}}, {{name}} and {{key}}, not {{{bad[0]}}}'
            )
        if not name.strip():
            raise ValueError('a name is not empty')
        return name

    @property
    def lacking(self) -> list[str]:
        """
        What publishing needs that these options lack: ``'owner'``,
        ``'contact email'``, or neither.
        """
        given = [('owner', self.owner), ('contact email', self.contact_email)]
        return [what for what, text in given if not text]


_NAME_FIELDS = frozenset({'title', 'name', 'key'})


class Published(BaseModel, frozen=True):
    """One upload: the row, its result record, and the dataset's identifier."""

    model_config = ConfigDict(extra='forbid')

    key: str
    record: str
    pid: str


class Publishing(BaseModel, frozen=True):
    """What a batch stores of the catalogue: each upload, in order."""

    model_config = ConfigDict(extra='forbid')

    published: tuple[Published, ...] = ()


@dataclass(frozen=True)
class Entry[T]:
    """
    An entry of a label, stored under ``('labels', label, part)``.

    ``dump`` converts a value to plain data. ``read`` converts it back, and
    raises ``ValueError`` naming the field of data of another shape.
    """

    part: str
    dump: Callable[[T], Any]
    read: Callable[[Any], T]

    def key(self, label: str) -> Key:
        """The key of the entry of ``label``."""
        return ('labels', label, self.part)

    def load(self, definitions: DefinitionStore, label: str) -> T | None:
        """The stored value of ``label``, or ``None``."""
        return definitions.load(self.key(label), self.read)

    def save(self, definitions: DefinitionStore, label: str, value: T) -> None:
        """Store ``value`` as the entry of ``label``."""
        definitions.save(self.key(label), self.dump(value))


def _adapted[T](part: str, kind: type[T]) -> Entry[T]:
    """The entry ``part`` of a value of ``kind``, as pydantic writes it to JSON."""
    adapter = TypeAdapter(kind)
    return Entry(
        part, partial(adapter.dump_python, mode='json'), partial(_checked, adapter)
    )


_TEMPLATE = TypeAdapter(Template)
_CORRECTIONS = TypeAdapter(dict[str, dict[str, Any]])


def template_data(template: Template) -> Any:
    """``template`` as the plain data of a template file."""
    return _TEMPLATE.dump_python(template, mode='json')


def template_of(data: Any) -> Template:
    """The template of :func:`template_data`; ``ValueError`` names a field."""
    return _checked(_TEMPLATE, data)


def _source_data(made: SourceEntry) -> dict[str, Any]:
    """``made`` as plain data, with the keys sorted."""
    return {
        'row_source': made.row_source,
        'seen_keys': sorted(made.seen_keys),
        'follows': made.follows,
    }


HEADER = _adapted('header', Header)
SETTINGS = Entry('settings', template_data, template_of)
TABLE = Entry('table', data_of, table_of)
"""Read as stored; :meth:`.manager.BatchManager.batch` types it for the spec."""
SOURCE = Entry('source', _source_data, partial(_checked, TypeAdapter(SourceEntry)))
OUTPUT_OPTIONS = _adapted('output_options', OutputOptions)
PUBLISH = _adapted('publish', Publishing)

LABEL_ENTRIES = (HEADER, SOURCE, TABLE, SETTINGS, OUTPUT_OPTIONS, PUBLISH)
"""Every entry of a label, the header first."""


def published(definitions: DefinitionStore, label: str) -> dict[str, str]:
    """
    The catalogue identifier of each published result of ``label``, by record
    ID.

    Defined here, not in :mod:`.publish`, so that reading it needs no
    catalogue package.
    """
    kept = PUBLISH.load(definitions, label)
    return {} if kept is None else {p.record: p.pid for p in kept.published}


def corrections_data(
    corrections: Mapping[FileRef, Mapping[str, Any]],
) -> dict[str, Any]:
    """The corrections, by file, as plain data."""
    return {ref.file: dict(fields) for ref, fields in corrections.items()}


def corrections_of(data: Any) -> dict[FileRef, dict[str, Any]]:
    """
    The corrections of :func:`corrections_data`, by file. Raises ``ValueError``
    naming the field of data of another shape.
    """
    return {
        FileRef(file=f): fields for f, fields in _checked(_CORRECTIONS, data).items()
    }
