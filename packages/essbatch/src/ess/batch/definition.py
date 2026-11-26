# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Where the definition of batches is kept: what a person makes of them, so that a
batch made again, after a restart or in another process, has it.

The definition is these entries, each with one writer. The settings and the
rows of a batch are written by its ``Batch``, which the manager makes:

    corrections   metadata of a file set by hand, by file     CorrectedFiles
    header        title, description                          BatchManager
    settings      the template: spec, settings, fills         Batch.settings
    rows          the batch table                             Batch.put
    source        the row source a batch was made from, the  Intake
                  keys of it that the batch has considered,
                  and whether the batch follows
    form          the format of the files that its results    Writing
                  are written as, and what a person says of
                  them
    publish       the records published to the catalogue      Publisher

The corrections are about files, and every batch of a workspace reads the
files through one :class:`~.corrections.CorrectedFiles`, so a workspace has
one entry of them. The others are of a label, so removing a label removes
them all (:data:`LABEL_ENTRIES`), also whether its batch follows. A
:class:`DefinitionStore`, *the definitions*, keeps each entry as plain data
under a key::

    ('corrections',)
    ('labels', label, 'header')
    ('labels', label, 'settings')
    ('labels', label, 'rows')
    ('labels', label, 'source')
    ('labels', label, 'form')
    ('labels', label, 'publish')

and its writer saves it whole at each change. It can forget an entry
(``delete``) and list the labels that have an entry (``labels``). Only
:class:`FolderDefinitions` knows that the definitions are files. A batch and a
file source without definitions keep their entries in memory.

The plain data of each entry, as its :class:`Entry` below writes and reads it
(:data:`CORRECTIONS` with :func:`corrections_data` and
:func:`corrections_of`)::

    corrections   {'pid:20.500.12269/abc': {'role': 'sample'}}
    header        {'title': 'Sample runs', 'description': ''}
    settings      {'spec': ..., 'params': {...}, 'blanks': ['sample']}: a template
    rows          {'schema': ..., 'data': [{'key': '60339', ...}, ...]}
    source        {'source': 'sample-runs', 'considered': ['60337', '60338'],
                   'follows': True}
    form          {'format': 'scipp-hdf5', 'outputs': ['iofq'],
                   'name': '{title} {name}', ...}
    publish       {'published': [{'key': '60339', 'record': ..., 'pid': ...}]}

The settings are a template in the format of the file that a person saves
from a page: its params are the settings of the batch, also those at the
columns, which each row with an empty cell takes; its blanks are the
parameters that the row source of the batch fills
(:attr:`~.reduction.Batch.fills`), which take no setting and which each row
must fill. The template that the rows fill has other blanks, all the
parameter columns of the rows (:meth:`~.reduction.Batch.template`).
The rows are :func:`~.table.data_of` of the batch table. Each entry is checked when
read: data of another shape raises ``ValueError`` naming the field, and
:class:`FolderDefinitions` adds the file.
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

from .table import data_of, frame_of

type Key = tuple[str, ...]
"""Names an entry: ``('corrections',)``, or ``('labels', label, part)``."""

CORRECTIONS: Key = ('corrections',)


class NotKept(OSError):
    """A :class:`DefinitionStore` could not keep an entry; it holds what it did."""


class DefinitionStore(Protocol):
    """
    Keeps each entry of the definition as plain data, under its :data:`Key`.

    ``save`` takes data that :func:`pydantic_core.to_json` writes, and raises
    :class:`NotKept` if it cannot keep it. ``load`` gives what ``read`` makes
    of the data in its JSON form, or ``None`` if nothing was saved under the
    key; a ``ValueError`` of ``read`` is raised naming where the data is kept.
    """

    def load[T](self, key: Key, read: Callable[[Any], T]) -> T | None: ...

    def save(self, key: Key, data: Any) -> None: ...

    def delete(self, key: Key) -> None:
        """Forget the entry of ``key``; nothing if none is kept."""
        ...

    def labels(self) -> list[str]:
        """The labels that have at least one entry kept, sorted."""
        ...


_NAME_MAX = 255
"""The bytes of a file name, on the file systems of Linux and macOS."""

# Moves with every change of what the kept files mean, such as where the fills
# of a batch are kept, so that a folder of another format is refused, not read
# wrong. No folder is migrated.
FORMAT = 1
"""The format of the files of :class:`FolderDefinitions`, in ``format.json``."""

_FORMAT_KEY: Key = ('format',)


class _Format(TypedDict):
    format: int


_FORMAT = TypeAdapter(_Format)


class FolderDefinitions:
    """
    A :class:`DefinitionStore` of JSON files in ``definitions/`` of a
    workspace's folder (see :mod:`ess.apps.folder`)::

        definitions/format.json
        definitions/corrections.json
        definitions/labels/auto%2Fiofq/header.json
        definitions/labels/auto%2Fiofq/settings.json
        definitions/labels/auto%2Fiofq/rows.json
        definitions/labels/auto%2Fiofq/source.json

    Each name of a key is a folder or a file, with ``/``, ``%`` and ``.``
    written as ``%2F``, ``%25`` and ``%2E``, so that no label reaches out of
    its folder. A name that is too long for a file name so written raises
    ``ValueError``. A file is written whole: to a temporary file next to it,
    which then replaces it, so a reader never sees half of one. A temporary
    file left by a write that was cut off stays, a few bytes, until its entry
    is deleted: a reader cannot tell it from that of a write of another
    process that runs.

    ``format.json`` holds ``{"format": FORMAT}`` (:data:`FORMAT`), written
    before the first entry of a folder without labels. Made on a folder with
    labels and no ``format.json``, or another format, it raises
    ``ValueError``: what the files mean may have changed, and no folder is
    migrated.
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
        """Write ``data`` to ``path`` whole; :class:`NotKept` if refused."""
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
            raise NotKept(f'{path}: {error.strerror or error}') from error

    def delete(self, key: Key) -> None:
        path = self.path(key)
        try:
            path.unlink(missing_ok=True)
            for left in path.parent.glob(f'.{path.name}.*'):
                left.unlink(missing_ok=True)
        except OSError as error:
            raise NotKept(f'{path}: {error.strerror or error}') from error
        if path.parent != self._root:
            with suppress(OSError):  # not empty: another part is kept
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
    """``data`` as ``adapter`` validates it; ``ValueError`` names each field."""
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
    """
    What a batch is called. Raises ``ValueError`` for a title that is empty
    or only spaces.

    Parameters
    ----------
    title:
        What the batch is called.
    description:
        What a person says of the batch.
    """

    title: str
    description: str = ''

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError('a batch has a title that is not empty')


@dataclass(frozen=True)
class FromSource:
    """
    What a batch made from a row source keeps of it: the name of the source;
    the keys of its rows that the batch has considered, those put into the
    batch from the source or left out when the batch was made; and whether
    the batch follows its source. Data with other fields, or without one of
    these, is refused when read.
    """

    __pydantic_config__ = ConfigDict(extra='forbid')

    source: str
    considered: frozenset[str]
    follows: bool


class Form(BaseModel, frozen=True):
    """
    How the results of a batch are written, once for all its rows, and what a
    person says of them (see :mod:`.writing`).

    ``format`` is the name of the writer (:class:`ess.apps.writers.Writer`).
    ``outputs`` are the outputs of the spec that a writer which writes the
    outputs chosen writes; a writer of fixed outputs ignores them. ``name``
    names the result of a row; it may hold ``{title}``, the title of the
    batch, ``{name}``, the name of the row, and ``{key}``, its key, each
    without a format. Another field in braces, or a name of only spaces,
    raises ``ValueError``. The other fields are those of
    :class:`ess.apps.writers.About`. It is also the body of
    ``POST api/output/form`` of the page.
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


_NAME_FIELDS = frozenset({'title', 'name', 'key'})


class Published(BaseModel, frozen=True):
    """One upload: the row, its result record, and the dataset's identifier."""

    model_config = ConfigDict(extra='forbid')

    key: str
    record: str
    pid: str


class Publishing(BaseModel, frozen=True):
    """What a batch keeps of the catalogue: each upload, in order."""

    model_config = ConfigDict(extra='forbid')

    published: tuple[Published, ...] = ()


@dataclass(frozen=True)
class Entry[T]:
    """
    An entry of a label, kept under ``('labels', label, part)``: how its value
    is written as plain data (``dump``) and read from it (``read``, which
    raises ``ValueError`` naming the field of data of another shape).
    """

    part: str
    dump: Callable[[T], Any]
    read: Callable[[Any], T]

    def key(self, label: str) -> Key:
        """The key of the entry of ``label``."""
        return ('labels', label, self.part)

    def load(self, definitions: DefinitionStore, label: str) -> T | None:
        """The value kept of ``label``; ``None`` if none, as the store loads it."""
        return definitions.load(self.key(label), self.read)

    def save(self, definitions: DefinitionStore, label: str, value: T) -> None:
        """Keep ``value`` as the entry of ``label``; :class:`NotKept` if refused."""
        definitions.save(self.key(label), self.dump(value))


def _adapted[T](part: str, kind: type[T]) -> Entry[T]:
    """The entry ``part`` of a value of ``kind``, as pydantic writes it as JSON."""
    adapter = TypeAdapter(kind)
    return Entry(
        part, partial(adapter.dump_python, mode='json'), partial(_checked, adapter)
    )


_TEMPLATE = TypeAdapter(Template)
_CORRECTIONS = TypeAdapter(dict[str, dict[str, Any]])


def template_data(template: Template) -> Any:
    """``template``, as plain data that a template file holds."""
    return _TEMPLATE.dump_python(template, mode='json')


def template_of(data: Any) -> Template:
    """The template of :func:`template_data`; ``ValueError`` names a field."""
    return _checked(_TEMPLATE, data)


def _source_data(made: FromSource) -> dict[str, Any]:
    """What a batch keeps of its source, as plain data; the keys sorted."""
    return {
        'source': made.source,
        'considered': sorted(made.considered),
        'follows': made.follows,
    }


HEADER = _adapted('header', Header)
SETTINGS = Entry('settings', template_data, template_of)
ROWS = Entry('rows', data_of, frame_of)
"""Read as the rows are kept; :meth:`.manager.BatchManager.batch` types them."""
SOURCE = Entry('source', _source_data, partial(_checked, TypeAdapter(FromSource)))
FORM = _adapted('form', Form)
PUBLISH = _adapted('publish', Publishing)

LABEL_ENTRIES = (HEADER, SOURCE, ROWS, SETTINGS, FORM, PUBLISH)
"""Every entry of a label: the header first, then the others."""


def published(definitions: DefinitionStore, label: str) -> dict[str, str]:
    """
    The catalogue identifier of each result of ``label`` published, by record
    ID; empty if none. Here, not in :mod:`.publish`, so that reading it needs
    no catalogue; only :class:`~.publish.Publisher` writes it.
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
    The corrections of :func:`corrections_data`, by file. Raises
    ``ValueError`` naming the field of data of another shape.
    """
    return {
        FileRef(file=f): fields for f, fields in _checked(_CORRECTIONS, data).items()
    }
