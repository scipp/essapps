# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The rows of a batch: as an immutable value of two layers (:class:`RowTable`),
and as a frame (:data:`KEY` and the functions after it).

A :class:`RowTable` holds two layers: what the last scan gave, and what was set
by hand since, a person's edit or a program's. A row's cells are the scan's
with the edits on top, worked out when asked for. So a new scan replaces only
its own layer, and no merge is needed: each rule below follows from which layer
an operation writes. The table keeps every row of the scan and shows those in
its scope, so a change of scope needs no new scan. The cells of a
:class:`RowTable` are opaque; it knows nothing of specs, files, or widgets.

The frame form
--------------

A table is one ``DataFrame``: the key of each row as index, named
:data:`KEY`, a column per parameter of the spec that the rows give, and notes,
any other column::

    key     run    name         sample      sample_transmission
    60339   60339  D2O buffer   run:60339   run:60305
    water   <NA>   by hand      run:60012   <NA>

A key is text, not empty, and unique. A column of a parameter has the dtype
that :func:`dtypes` reads from the type of the parameter, with
``field_types.unwrapped`` and ``field_types.split``::

    the parameter takes                                   dtype
    a file, an output, a file or an output                string
    text, a Literal of text, a time                       string
    a number                                              Float64
    a whole number                                        Int64
    a truth value                                         boolean
    a list, a group, a value or an output, anything else  object

``| None`` does not change the dtype: empty is ``None``. :func:`typed` casts a
frame to these dtypes, and refuses what pandas refuses and a value that is not
text in a ``string`` column, which pandas would turn into text, ``60339.0``
into ``'60339.0'``. Notes take the dtypes of ``convert_dtypes()``. So a table
has one kind of empty per dtype: ``pd.NA``, and ``None`` in an ``object``
column.

A cell is plain data. A file and an output are text, which a request does not
take, so :func:`as_request` converts the cells of a row before
``Template.fill``, and :func:`cell_of` converts a value of a request back::

    # at a parameter that takes a file
    'run:60339'      -> FileRef(file='run:60339')
    60339            -> FileRef(file='run:60339')   # a run; in a template, or a list
    # at a parameter that takes an output
    'r17.centre'     -> OutputRef(record='r17', output='centre')
    'r18.slices[2]'  -> OutputRef(record='r18', output='slices', key='2')
    # at a parameter that takes a file or an output
    'run:1'          -> FileRef(file='run:1')       # text with a kind, [a-z]+:
    'r17.centre'     -> OutputRef(record='r17', output='centre')
    # at a parameter that takes a list of these: each element
    ['run:1', 2]     -> [FileRef(file='run:1'), FileRef(file='run:2')]
    # anything else, a file inside a group too: the cell
    {'bins': 4}      -> {'bins': 4}

``rows.json`` holds :func:`data_of` of the frame, and :func:`frame_of` reads it
back equal, dtypes included. Its shape is that of ``to_json(orient='table')``:
a table schema, by ``build_table_schema``, and one record per row, of Python
values. pandas' own ``to_json`` and ``read_json`` round floats, and integers
above 2**53, so they neither write nor read the records. A JSON writer of
Python values keeps each float and integer exact, and :func:`frame_of` builds
each column with ``pd.array(values, dtype=extDtype)``.
A row is read as ``frame.to_dict('index')``, which gives Python values of each
column's type, never a NumPy scalar or a ``Float64`` in place of an ``Int64``.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from functools import cached_property
from typing import Any, Literal, get_args, get_origin

import pandas as pd
from pandas.io.json import build_table_schema
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from ess.spec import FileRef, OutputRef, field_types

from ..errors import FieldError, SubmitError
from ..files import file


class _Empty:
    def __repr__(self) -> str:
        return 'EMPTY'


EMPTY: Any = _Empty()
"""Written to a cell by :meth:`RowTable.edit` to clear it, whatever the scan gave."""

type Index = int | float | str
"""A value that orders rows, such as the run number. One source gives one type."""


@dataclass(frozen=True)
class Row:
    """
    One row: ``values`` by column, and ``info``, read-only text to show with it.

    A column left out of ``values`` is an empty cell; ``None`` is a value.
    ``key`` must stay the same from one scan to the next, so it is best derived
    from something that does not change, such as the run number, with the name
    of the sample in ``info``. ``index`` orders the rows for a :class:`Scope`;
    the source gives it, and a row added by hand has none.

    Raises ``TypeError`` if ``key`` is not a string.
    """

    key: str
    values: Mapping[str, Any] = field(default_factory=dict)
    info: Mapping[str, str] = field(default_factory=dict)
    index: Index | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, str):
            raise TypeError(f'the key of a row is a string, not {self.key!r}')


@dataclass(frozen=True)
class Scope:
    """
    Which rows of the scan a table shows: a slice of index values. ``start``
    is included and ``stop`` excluded, as in a Python slice; ``None`` is an
    open end. ``Scope()`` holds every row, also one without an index; a
    slice with an end holds no row without one, and a ``stop`` equal to the
    ``start`` holds no row. The manager converts the ends to the type of the
    index of a source, and refuses a ``stop`` below the ``start``.

    Raises ``TypeError`` for an end that is not an ``int``, ``float`` or
    ``str``, since only these are kept as they are in the definitions.
    """

    start: Index | None = None
    stop: Index | None = None

    def __post_init__(self) -> None:
        for end in (self.start, self.stop):
            if end is not None and (
                isinstance(end, bool) or not isinstance(end, int | float | str)
            ):
                raise TypeError(
                    f'an end of a scope is an int, a float or a str, not {end!r}'
                )

    def holds(self, row: Row) -> bool:
        """Whether ``row`` is in the scope."""
        if self.start is None and self.stop is None:
            return True
        if row.index is None:
            return False
        return (self.start is None or self.start <= row.index) and (
            self.stop is None or row.index < self.stop
        )


def _unique(rows: Iterable[Row]) -> tuple[Row, ...]:
    """``rows``; raises ``SubmitError`` for a key given twice."""
    rows = tuple(rows)
    count = Counter(row.key for row in rows)
    if twice := sorted(key for key, n in count.items() if n > 1):
        raise SubmitError(FieldError(k, (), 'two rows have this key') for k in twice)
    return rows


@dataclass(frozen=True)
class RowTable:
    """
    Rows by key: those of the scan in ``scope``, then those added by hand.
    Each method returns a new table. See ``packages/essapps/docs/batch.md``
    for the rules.
    """

    scan: tuple[Row, ...] = ()
    added: tuple[Row, ...] = ()
    edits: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    removed: frozenset[str] = frozenset()
    scope: Scope = field(default_factory=Scope)

    @cached_property
    def _rows(self) -> dict[str, Row]:
        hidden = self.removed | {row.key for row in self.added}
        shown = [r for r in self.scan if r.key not in hidden and self.scope.holds(r)]
        return {row.key: self._with_edits(row) for row in (*shown, *self.added)}

    @property
    def removed_in_scope(self) -> frozenset[str]:
        """
        The keys of the rows removed by hand that the scan gives in the scope:
        those a person sees as removed, and can bring back.
        """
        return frozenset(
            r.key for r in self.scan if r.key in self.removed and self.scope.holds(r)
        )

    def _refuse_outside(self, keys: Iterable[str]) -> None:
        """
        Raise ``SubmitError`` for each of ``keys`` that the table does not
        show, since the scan gives it outside the scope: added by hand, it
        would no longer take its cells from the scan.
        """
        outside = {r.key for r in self.scan if not self.scope.holds(r)}
        if refused := [k for k in keys if k in outside and k not in self]:
            raise SubmitError(
                FieldError(k, (), 'the source makes this row outside the scope')
                for k in refused
            )

    def _with_edits(self, row: Row) -> Row:
        edits = self.edits.get(row.key)
        if edits is None and not any(v is EMPTY for v in row.values.values()):
            return row
        cells = {**row.values, **(edits or {})}
        return replace(
            row, values={column: v for column, v in cells.items() if v is not EMPTY}
        )

    def __iter__(self) -> Iterator[Row]:
        return iter(self._rows.values())

    def __len__(self) -> int:
        return len(self._rows)

    def __contains__(self, key: str) -> bool:
        return key in self._rows

    def __getitem__(self, key: str) -> Row:
        return self._rows[key]

    def rescan(self, rows: Iterable[Row]) -> RowTable:
        """
        Replace the scan's layer. What was set by hand stays: an edit, a row
        added, and a row removed. Raises ``SubmitError`` for a key given twice.
        """
        return replace(self, scan=_unique(rows))

    def put(self, rows: Iterable[Row], columns: Iterable[str]) -> RowTable:
        """
        Set ``rows`` by hand, as a table of them gives them: a row of a key
        that the table does not show is added, but one of the scan that was
        removed is brought back; and of a row shown, or brought back, each of
        ``columns`` whose cell differs is set, or cleared if the row given
        leaves it empty. A cell that does not differ is left as it is, so one
        from the scan keeps following the scan. Raises ``SubmitError``, and
        sets none, for a key given twice, and for a key of a row that the scan
        gives outside the scope.
        """
        columns = tuple(columns)
        rows = _unique(rows)
        self._refuse_outside(row.key for row in rows)
        added = list(self.added)
        edits = dict(self.edits)
        removed = set(self.removed)
        scan = {row.key: row for row in self.scan if row.key in removed}
        for row in rows:
            if row.key in scan:
                removed.discard(row.key)
                shown = self._with_edits(scan[row.key]).values
            elif row.key in self:
                shown = self[row.key].values
            else:
                added.append(row)
                edits.pop(row.key, None)  # as :meth:`add`
                continue
            changed = {
                c: row.values.get(c, EMPTY)
                for c in columns
                if (c in row.values, row.values.get(c)) != (c in shown, shown.get(c))
            }
            if changed:
                edits[row.key] = {**edits.get(row.key, {}), **changed}
        return replace(
            self, added=tuple(added), edits=edits, removed=frozenset(removed)
        )

    def add(self, row: Row) -> RowTable:
        """
        Add a row by hand; it stays whatever a scan gives or the scope holds.
        Raises ``SubmitError`` if the table has a row of that key, or the scan
        gives one outside the scope.
        """
        if row.key in self:
            raise SubmitError([FieldError(row.key, (), 'a row has this key already')])
        self._refuse_outside([row.key])
        edits = {k: v for k, v in self.edits.items() if k != row.key}
        return replace(self, added=(*self.added, row), edits=edits)

    def edit(self, key: str, values: Mapping[str, Any]) -> RowTable:
        """
        Set cells of a row by hand, :data:`EMPTY` to clear one. They stay
        until set again or :meth:`reset`. Raises ``KeyError`` if there is no such
        row.
        """
        return self.edit_each([key], values)

    def edit_each(self, keys: Iterable[str], values: Mapping[str, Any]) -> RowTable:
        """
        :meth:`edit` of each of ``keys`` in one pass. Raises ``KeyError``, and
        edits none, if the table has no row for one of them.
        """
        keys = tuple(keys)
        for key in keys:
            self[key]
        edits = dict(self.edits)
        for key in keys:
            edits[key] = {**edits.get(key, {}), **values}
        return replace(self, edits=edits)

    def remove(self, key: str) -> RowTable:
        """
        Remove a row. One from the scan stays removed after the next scan,
        until :meth:`reset`. Raises ``KeyError`` if there is no such row.
        """
        return self.remove_each([key])

    def remove_each(self, keys: Iterable[str]) -> RowTable:
        """
        :meth:`remove` of each of ``keys`` in one pass. Raises ``KeyError``, and
        removes none, if the table has no row for one of them.
        """
        keys = frozenset(keys)
        for key in keys:
            self[key]
        return replace(
            self,
            added=tuple(row for row in self.added if row.key not in keys),
            edits={k: v for k, v in self.edits.items() if k not in keys},
            removed=self.removed | keys.intersection(row.key for row in self.scan),
        )

    def reset(self, key: str, *columns: str) -> RowTable:
        """
        Forget the edits of a row, and that it was removed, if it was. Given
        ``columns``, forget only the edits of those cells.
        """
        return self.reset_each([key], *columns)

    def reset_each(self, keys: Iterable[str], *columns: str) -> RowTable:
        """:meth:`reset` of each of ``keys`` in one pass."""
        keys = frozenset(keys)
        edits = {}
        for key, cells in self.edits.items():
            if key not in keys:
                edits[key] = cells
            elif columns and (
                kept := {c: v for c, v in cells.items() if c not in columns}
            ):
                edits[key] = kept
        removed = self.removed if columns else self.removed - keys
        return replace(self, edits=edits, removed=removed)


KEY = 'key'
"""The name of the index of a table: the key of each row."""

_WORDS = {
    'string': 'text',
    'Float64': 'a number',
    'Int64': 'a whole number',
    'boolean': 'true or false',
}
"""What a cell of each dtype but ``object`` holds, as a message says it."""

_KIND = re.compile(r'[a-z]+:')
_OUTPUT = re.compile(
    r'(?P<record>[^.\[\]]+)\.(?P<output>[^.\[\]]+)(?:\[(?P<key>.*)\])?'
)


@dataclass(frozen=True)
class _Parameter:
    """What a parameter takes, as a cell gives it."""

    many: bool  # a list of what the other fields say
    inline: Any  # the part that a request writes, None if only an output
    file: bool
    output: bool


def _parameter(info: FieldInfo) -> _Parameter:
    metadata = field_types.field_metadata(info)
    tp, _, metadata = field_types.unwrapped(info.annotation, metadata)
    many = get_origin(tp) is list
    if many:
        tp, _, metadata = field_types.unwrapped(get_args(tp)[0])
    inline, inline_metadata, referenced = field_types.split(tp, *metadata)
    is_file = (
        inline is not None
        and field_types.file_marker(inline, *inline_metadata) is not None
    )
    return _Parameter(many, inline, is_file, bool(referenced))


def _dtype(parameter: _Parameter) -> str:
    tp = parameter.inline
    if parameter.many:
        return 'object'
    if parameter.file or tp is None:
        return 'string'
    if parameter.output:
        return 'object'
    if tp is bool:
        return 'boolean'
    if tp is int:
        return 'Int64'
    if tp is float:
        return 'Float64'
    if (
        tp is str
        or tp in field_types.TIMES
        or (get_origin(tp) is Literal and all(type(a) is str for a in get_args(tp)))
    ):
        return 'string'
    return 'object'


def dtypes(model: type[BaseModel]) -> dict[str, str]:
    """The dtype of the column of each parameter of ``model``, by name."""
    return {n: _dtype(_parameter(i)) for n, i in model.model_fields.items()}


def _empty(value: Any) -> bool:
    """Whether a cell is empty: ``None``, ``NaN`` or ``pd.NA``, not a list."""
    return value is None or (pd.api.types.is_scalar(value) and bool(pd.isna(value)))


def _keys(index: pd.Index) -> pd.Index:
    """``index`` as a table's index; raises ``ValueError`` for a key refused."""
    if pd.api.types.is_numeric_dtype(index) and len(index):
        raise ValueError(
            f'a key is text, not a number such as {index[0]}; make the keys '
            'text with frame.index = frame.index.astype(str)'
        )
    problems = [
        f'the key {k!r} is not text'
        for k in index
        if not _empty(k) and not isinstance(k, str)
    ]
    if any(_empty(k) or k == '' for k in index):
        problems.append('a key is empty')
    problems += [
        f'the key {k!r} is given twice' for k, n in Counter(index).items() if n > 1
    ]
    if problems:
        raise ValueError('; '.join(problems))
    return pd.Index(index, name=KEY, dtype='string')


def _objects(column: pd.Series) -> pd.Series:
    """``column`` as an ``object`` column, ``None`` in each empty cell."""
    return pd.Series(
        [None if _empty(v) else v for v in column], index=column.index, dtype=object
    )


def _fits(value: Any, dtype: str) -> bool:
    """Whether a cell that is not empty fits a column of ``dtype``."""
    if dtype == 'string':
        return isinstance(value, str)
    try:
        pd.array([value], dtype=dtype)
    except (TypeError, ValueError):
        return False
    return True


def _cast(column: pd.Series, dtype: str) -> tuple[pd.Series, list[str]]:
    """``column`` cast to ``dtype``, and the cells refused, each as a message."""
    if dtype == 'object':
        return _objects(column), []
    if dtype != 'string' or all(_empty(v) or isinstance(v, str) for v in column):
        try:
            return column.astype(dtype), []
        except (TypeError, ValueError):
            pass
    refused = [
        f'row {k!r}, column {column.name!r}: {v!r} is not {_WORDS[dtype]}'
        for k, v in zip(column.index, column.tolist(), strict=True)
        if not _empty(v) and not _fits(v, dtype)
    ]
    return column, refused or [
        f'column {column.name!r}: not {_WORDS[dtype]} in each cell'
    ]


def typed(frame: pd.DataFrame, model: type[BaseModel]) -> pd.DataFrame:
    """
    ``frame`` as a batch holds it: the index of text named :data:`KEY`, each
    column of a parameter of ``model`` cast to its dtype (see :func:`dtypes`),
    and each note to the dtype of ``convert_dtypes()``. An empty cell of an
    ``object`` column is ``None``.

    Raises ``ValueError``, naming each key, column or cell refused: a key
    that is not text, empty, or given twice; a column given twice; a cell
    that is not of the dtype of its column.
    """
    index = _keys(frame.index)
    problems = [
        f'the column {c!r} is given twice'
        for c, n in Counter(frame.columns).items()
        if n > 1
    ]
    if problems:
        raise ValueError('; '.join(problems))
    types = dtypes(model)
    columns = {}
    for name, column in frame.set_axis(index).items():
        if name in types:
            columns[name], refused = _cast(column, types[name])
            problems += refused
        else:
            note = column.convert_dtypes()
            columns[name] = _objects(note) if note.dtype == object else note
    if problems:
        raise ValueError('; '.join(problems))
    return pd.DataFrame(columns, index=index, columns=frame.columns)


def _request_value(parameter: _Parameter, cell: Any) -> Any:
    if parameter.file and isinstance(cell, int) and not isinstance(cell, bool):
        return file(run=cell)
    if not isinstance(cell, str):
        return cell
    if parameter.file and (not parameter.output or _KIND.match(cell)):
        return FileRef(file=cell)
    if parameter.output and (found := _OUTPUT.fullmatch(cell)):
        return OutputRef(**found.groupdict())
    return cell


def as_request(model: type[BaseModel], cells: Mapping[str, Any]) -> dict[str, Any]:
    """
    ``cells``, by the name of a parameter of ``model``, as a request takes
    them: at a parameter that takes a file, text is the file of that name and
    a whole number a run; at one that takes an output, text such as
    ``'r17.centre'`` or ``'r18.slices[2]'`` is that output; at one that takes
    either, text with a kind, such as ``run:``, is a file, and other text an
    output. In a list, each element is converted so. Any other cell is
    returned as it is, for the request to refuse if it does not fit.
    """
    fields = model.model_fields
    converted = {}
    for name, cell in cells.items():
        if name not in fields:
            converted[name] = cell
            continue
        parameter = _parameter(fields[name])
        if parameter.many and isinstance(cell, list):
            converted[name] = [_request_value(parameter, c) for c in cell]
        else:
            converted[name] = _request_value(parameter, cell)
    return converted


def cell_of(value: Any) -> Any:
    """
    The cell of ``value`` of a request, the inverse of :func:`as_request`:
    a file is its name, such as ``'run:1'``, an output its text, such as
    ``'r17.centre'``, and a list holds the cell of each element.
    """
    if isinstance(value, list):
        return [cell_of(v) for v in value]
    if isinstance(value, FileRef | OutputRef):
        return str(value)
    return value


def data_of(frame: pd.DataFrame) -> dict[str, Any]:
    """
    The plain data of ``frame``, a table as :func:`typed` gives it, as
    ``rows.json`` holds it: ``{'schema': ..., 'data': [{'key': ..., ...}]}``,
    with ``None`` in each empty cell. Written as JSON by a writer of Python
    values, it keeps each value exact.
    """
    records = [
        {KEY: key, **{c: None if _empty(v) else v for c, v in row.items()}}
        for key, row in frame.to_dict('index').items()
    ]
    return {'schema': build_table_schema(frame, version=False), 'data': records}


def frame_of(data: Any) -> pd.DataFrame:
    """
    The table of the plain data of :func:`data_of`, equal to the frame it was
    made of. Raises ``ValueError``, naming the entry, for data that are not
    such a table.
    """
    try:
        fields = {
            f['name']: f.get('extDtype', 'object') for f in data['schema']['fields']
        }
        records = list(data['data'])
    except (KeyError, TypeError, AttributeError):
        raise ValueError(
            "not a table: {'schema': {'fields': [...]}, 'data': [...]}"
        ) from None
    if fields.pop(KEY, None) is None:
        raise ValueError(f'schema: no field {KEY!r}')
    for i, record in enumerate(records):
        if not isinstance(record, dict) or KEY not in record:
            raise ValueError(f'data[{i}]: not an object with a {KEY!r}')
        if unknown := sorted(record.keys() - fields.keys() - {KEY}):
            raise ValueError(f'data[{i}]: no field {", ".join(unknown)} in the schema')
    index = _keys(pd.Index([r[KEY] for r in records], dtype=object))
    columns = {}
    for name, dtype in fields.items():
        values = [r.get(name) for r in records]
        try:
            columns[name] = (
                pd.Series(values, index=index, dtype=object)
                if dtype == 'object'
                else pd.Series(pd.array(values, dtype=dtype), index=index)
            )
        except (TypeError, ValueError) as error:
            raise ValueError(f'column {name!r}: {error}') from None
    return pd.DataFrame(columns, index=index, columns=list(fields))
