# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The batch table as a ``DataFrame``: its dtypes, its cells, ``table.json``, and
table files.

:func:`typed` checks a frame and converts it to a batch table. An empty cell is
``pd.NA`` in the table, or ``None`` in an ``object`` column. It is ``None`` in
a row read with ``to_dict('index')`` and in ``table.json``.

``table.json`` has the shape of ``to_json(orient='table')``. Its records are
written and read as Python values, since pandas' ``to_json`` and ``read_json``
round floats, and integers above 2**53.

Guide: ``packages/essbatch/docs/batch.md``, "Cells and their dtypes" and
"Table files".
"""

from __future__ import annotations

import copy
import json
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from enum import Enum
from functools import partial
from pathlib import Path
from typing import Any, Literal, TextIO, get_args, get_origin

import pandas as pd
from pandas.io.json import build_table_schema
from pydantic import BaseModel
from pydantic.fields import FieldInfo
from pydantic_core import to_json, to_jsonable_python

from ess.apps.files import file, replace_at_once
from ess.spec import FileRef, OutputRef, field_types
from ess.spec.field_types import Choice, FileKind, Group, List, Scalar

KEY = 'key'
"""The name of the index of a batch table."""

NAME = 'name'
"""The note column that names a row on the page and in a published dataset."""


def row_names(frame: pd.DataFrame, columns: Collection[str]) -> dict[str, str]:
    """
    The name of each row of ``frame``, by key: its cell at :data:`NAME`, else
    its key. ``columns`` are the parameter columns of ``frame``.
    """
    if NAME not in frame.columns or NAME in columns:
        return {key: key for key in frame.index}
    return {
        key: key if pd.isna(name) else str(name) for key, name in frame[NAME].items()
    }


_WORDS = {
    'string': 'text',
    'Float64': 'a number',
    'Int64': 'a whole number',
    'boolean': 'true or false',
    'object': 'plain data',
}
"""What a cell of each dtype holds, in the words of an error message."""

_KIND = re.compile(r'[a-z]+:')
# What OutputRef.__str__ writes, given that a record ID (uuid4().hex) and an
# output name (a field name) contain none of '.', '[', ']', ':'.
_OUTPUT = re.compile(
    r'(?P<record>[^.\[\]:]+)\.(?P<output>[^.\[\]]+)(?:\[(?P<key>.*)\])?'
)


@dataclass(frozen=True)
class _Parameter:
    """What a parameter accepts, as a cell holds it."""

    many: bool  # a list of what the other fields describe
    inline: Scalar | Group | Choice | FileKind | None  # None if only an output
    output: bool


def _text(tp: Any) -> bool:
    """Whether a value of ``tp`` is text in its JSON form."""
    if get_origin(tp) is Literal:
        return all(type(a) is str for a in get_args(tp))
    if isinstance(tp, type) and issubclass(tp, Enum):
        return all(type(m.value) is str for m in tp)
    return tp is str or tp is date or tp in field_types.TIMES


def _parameter(name: str, info: FieldInfo) -> _Parameter:
    field = field_types.shape(info.annotation, field_types.field_metadata(info))
    many = False
    if isinstance(field.inline, List):
        many, field = True, field_types.shape(field.inline.element)
    inline = field.inline
    if field.referenced and isinstance(inline, Scalar) and _text(inline.type):
        raise ValueError(
            f'{name}: takes text or an output, and a cell cannot tell the two apart'
        )
    return _Parameter(many, inline, bool(field.referenced))


def _dtype(parameter: _Parameter) -> str:
    if parameter.many:
        return 'object'
    match parameter.inline:
        case FileKind() | None:
            return 'string'
        case Scalar(tp) if not parameter.output:
            if tp is bool:
                return 'boolean'
            if tp is int:
                return 'Int64'
            if tp is float:
                return 'Float64'
            return 'string' if _text(tp) else 'object'
    return 'object'


def dtypes(model: type[BaseModel]) -> dict[str, str]:
    """
    The dtype of the column of each parameter of ``model``, by name.

    Raises ``ValueError`` for a parameter that accepts text or an output.
    """
    return {n: _dtype(_parameter(n, i)) for n, i in model.model_fields.items()}


def _empty(value: Any) -> bool:
    """Whether a cell is empty: ``None``, ``NaN`` or ``pd.NA``."""
    return value is None or (pd.api.types.is_scalar(value) and bool(pd.isna(value)))


def _plain(value: Any) -> bool:
    """Whether ``value`` is plain data: a JSON value as Python represents it."""
    if isinstance(value, list):
        return all(_plain(v) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _plain(v) for k, v in value.items())
    return value is None or type(value) in (str, int, float, bool)


def _fits(value: Any, dtype: str) -> bool:
    """Whether a cell that is not empty fits a column of ``dtype``."""
    if dtype == 'object':
        return _plain(value)
    if isinstance(value, bool):
        return dtype == 'boolean'
    if dtype == 'Int64':
        whole = isinstance(value, int) or (
            isinstance(value, float) and value.is_integer()
        )
        return whole and -(2**63) <= value < 2**63
    kinds = {'string': str, 'Float64': (int, float), 'boolean': bool}
    return isinstance(value, kinds[dtype])


def _keys(index: pd.Index) -> pd.Index:
    """``index`` as the index of a batch table; ``ValueError`` for a bad key."""
    if pd.api.types.is_numeric_dtype(index) and len(index):
        raise ValueError(f'a key is text, not a number such as {index[0]}')
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


def _names(names: Iterable[Any]) -> list[str]:
    """An error message for each problem with the column names of a table."""
    names = list(names)
    problems = [f'the column {c!r} is not text' for c in names if type(c) is not str]
    problems += [
        f'the column {c!r} is given twice' for c, n in Counter(names).items() if n > 1
    ]
    if KEY in names:
        problems.append(
            f'a column is named {KEY!r}, the name of the index: make it the '
            f'index with frame.set_index({KEY!r})'
        )
    return problems


def _cast(column: pd.Series, dtype: str) -> tuple[pd.Series, list[str]]:
    """``column`` cast to ``dtype``, and a message for each cell that does not fit."""
    values = [None if _empty(v) else v for v in column.tolist()]
    refused = [
        f'row {k!r}, column {column.name!r}: {v!r} is not {_WORDS[dtype]}'
        for k, v in zip(column.index, values, strict=True)
        if v is not None and not _fits(v, dtype)
    ]
    if dtype == 'object':  # the table owns its lists and groups
        return pd.Series(
            copy.deepcopy(values), index=column.index, dtype=object
        ), refused
    if refused:
        return column, refused
    if dtype == 'Int64':  # a whole float, as pandas gives a column with a gap
        values = [None if v is None else int(v) for v in values]
    return pd.Series(pd.array(values, dtype=dtype), index=column.index), []


def typed(frame: pd.DataFrame, model: type[BaseModel]) -> pd.DataFrame:
    """
    ``frame`` as a batch table for the params model ``model``.

    The index is text, named :data:`KEY`. Each parameter column has the dtype
    of :func:`dtypes`. Each note column has the dtype of ``convert_dtypes()``.
    A note column without values keeps its dtype, or becomes ``string`` if it
    is ``float64`` or ``object``. Lists and dicts in cells are copied.

    Raises ``ValueError`` naming each bad key, column or cell.
    """
    index = _keys(frame.index)
    if problems := _names(frame.columns):
        raise ValueError('; '.join(problems))
    parameters = {n: _parameter(n, i) for n, i in model.model_fields.items()}
    types = {n: _dtype(p) for n, p in parameters.items()}
    columns = {}
    for name, column in frame.set_axis(index).items():
        if name in types:
            dtype = types[name]
        elif column.isna().all():  # pandas makes it float or object when it lost it
            kept = str(column.dtype) in _WORDS.keys() - {'object'}
            dtype = str(column.dtype) if kept else 'string'
        else:
            column = column.convert_dtypes()
            dtype = str(column.dtype)
            if dtype not in _WORDS:
                problems.append(
                    f'the note {name!r} is {dtype}, not text, a number, true or '
                    f'false, or plain data: make it text, with '
                    f"frame[{name!r}].astype('string')"
                )
                continue
        columns[name], refused = _cast(column, dtype)
        problems += refused
        if name in parameters and not refused:
            problems += [
                f'row {k!r}, column {name!r}: {v!r} {_NOT_AN_OUTPUT}'
                for k, v in columns[name].items()
                if not _empty(v) and _names_no_output(parameters[name], v)
            ]
    if problems:
        raise ValueError('; '.join(problems))
    return pd.DataFrame(columns, index=index, columns=list(frame.columns))


def _request_value(parameter: _Parameter, cell: Any) -> Any:
    is_file = isinstance(parameter.inline, FileKind)
    if is_file and isinstance(cell, int) and not isinstance(cell, bool):
        return file(run=cell)
    if not isinstance(cell, str):
        return cell
    if is_file and cell and (not parameter.output or _KIND.match(cell)):
        return FileRef(file=cell)
    if parameter.output and (found := _OUTPUT.fullmatch(cell)):
        return OutputRef(**found.groupdict())
    return cell


_NOT_AN_OUTPUT = (
    'names no output: a cell names one as RECORD.OUTPUT, such as r17.centre'
)


def _names_no_output(parameter: _Parameter, cell: Any) -> bool:
    """
    Whether ``cell`` holds text that refers to neither an output nor a file,
    at a parameter that accepts an output.
    """
    cells = cell if parameter.many and isinstance(cell, list) else [cell]
    return parameter.output and any(
        isinstance(c, str) and c and isinstance(_request_value(parameter, c), str)
        for c in cells
    )


def not_outputs(model: type[BaseModel], values: Mapping[str, Any]) -> list[str]:
    """
    The names in ``values`` whose value is text that refers to no output, at a
    parameter of ``model`` that accepts an output. :func:`as_request` leaves
    such text as it is, and the workspace rejects it.
    """
    fields = model.model_fields
    return [
        name
        for name, value in values.items()
        if name in fields and _names_no_output(_parameter(name, fields[name]), value)
    ]


def as_request(model: type[BaseModel], cells: Mapping[str, Any]) -> dict[str, Any]:
    """
    ``cells``, by parameter name, as values of a request of ``model``.

    The conversions are in the guide, "Cells and their dtypes". A cell that
    none applies to is returned as it is, for the request to reject if it does
    not fit.
    """
    fields = model.model_fields
    converted = {}
    for name, cell in cells.items():
        if name not in fields:
            converted[name] = cell
            continue
        parameter = _parameter(name, fields[name])
        if parameter.many and isinstance(cell, list):
            converted[name] = [_request_value(parameter, c) for c in cell]
        else:
            converted[name] = _request_value(parameter, cell)
    return converted


def cell_of(value: Any) -> Any:
    """
    The cell of a value of a request: the inverse of :func:`as_request`.

    A file or an output becomes its text, such as ``'run:1'`` or
    ``'r17.centre'``. A list holds the cell of each element. Any other value
    becomes its JSON form, such as ``{'@ref': 'file', 'file': 'run:1'}`` for a
    file inside a group.
    """
    if isinstance(value, list):
        return [cell_of(v) for v in value]
    if isinstance(value, FileRef | OutputRef):
        return str(value)
    return to_jsonable_python(value)


def data_of(frame: pd.DataFrame) -> dict[str, Any]:
    """
    ``frame``, a batch table, as the plain data of ``table.json``::

        {'schema': ..., 'data': [{'key': '60339', 'sample': 'run:60339', ...}]}
    """
    if frame.index.name != KEY:
        raise ValueError(f'a table has an index named {KEY!r}: see typed()')
    records = [{KEY: key, **row} for key, row in frame.to_dict('index').items()]
    return {'schema': build_table_schema(frame, version=False), 'data': records}


def table_of(data: Any) -> pd.DataFrame:
    """
    The batch table of the plain data of :func:`data_of`, equal to the
    original, dtypes included.

    Raises ``ValueError`` naming the entry, row or column of bad data.
    """
    try:
        fields = [
            (f['name'], f.get('extDtype', 'object')) for f in data['schema']['fields']
        ]
        records = data['data']
    except (KeyError, TypeError):
        raise ValueError(
            "not a table: {'schema': {'fields': [{'name': ...}, ...]}, 'data': [...]}"
        ) from None
    if not isinstance(records, list):
        raise ValueError('data: not a list of rows')
    names = [n for n, _ in fields if n != KEY]
    if len(names) == len(fields):
        raise ValueError(f'schema: no field {KEY!r}')
    if problems := _names(names):
        raise ValueError('; '.join(problems))
    types = dict(fields)
    if wrong := [
        f'{n!r} is {t}' for n, t in types.items() if t not in _WORDS and n != KEY
    ]:
        raise ValueError(f'schema: not a dtype of a table: {", ".join(wrong)}')
    for i, record in enumerate(records):
        if not isinstance(record, dict) or KEY not in record:
            raise ValueError(f'data[{i}]: not an object with a {KEY!r}')
        if unknown := sorted(record.keys() - types.keys()):
            raise ValueError(f'data[{i}]: no field {", ".join(unknown)} in the schema')
    index = _keys(pd.Index([r[KEY] for r in records], dtype=object))
    columns = {}
    for name in names:
        values = pd.Series([r.get(name) for r in records], index=index, dtype=object)
        columns[name], refused = _cast(values.rename(name), types[name])
        problems += refused
    if problems:
        raise ValueError('; '.join(problems))
    return pd.DataFrame(columns, index=index, columns=names)


RESULT = ('status', 'record', 'failure', 'published')
"""The result columns, which a table file may hold."""

_READERS: dict[str, Callable[..., pd.DataFrame]] = {
    '.csv': pd.read_csv,
    '.tsv': partial(pd.read_csv, sep='\t'),
    '.xlsx': pd.read_excel,
}

SUFFIXES = tuple(_READERS)
"""The suffixes of table files."""


def _reader(path: Path) -> Callable[..., pd.DataFrame]:
    if (reader := _READERS.get(path.suffix)) is None:
        raise ValueError(f'{path}: a table is one of {", ".join(SUFFIXES)}')
    return reader


def _read_cell(text: str) -> Any:
    """The value of a cell of an ``object`` column: its JSON value, else its text."""
    try:
        return json.loads(text)
    except ValueError:
        return text


def _write_cell(value: Any) -> str:
    """The text that :func:`_read_cell` reads back as ``value``."""
    if isinstance(value, str) and _read_cell(value) == value:
        return value
    return to_json(value).decode()


def read_text(path: Path) -> pd.DataFrame:
    """
    The table in a table file, each cell as text and an empty cell as ``''``.

    For a table that is not a batch table, such as a run table.
    """
    return _reader(path)(path, dtype=str, keep_default_na=False)


def read_table(path: Path, model: type[BaseModel]) -> pd.DataFrame:
    """
    The batch table in a table file, for the params model ``model``.

    How the columns are read is in the guide, "Table files". A ``.xlsx`` file
    keeps 15 significant digits, as Excel does: ``0.1 + 0.2`` reads back as
    ``0.3``.

    Raises ``ValueError`` naming the file.
    """
    reader = _reader(path)
    try:
        return _read_table(reader, path, model)
    except (TypeError, ValueError) as error:
        # TypeError: what pandas raises for 2.5 in an Int64 column
        raise ValueError(f'{path}: {error}') from None


def _read_table(
    reader: Callable[..., pd.DataFrame], path: Path, model: type[BaseModel]
) -> pd.DataFrame:
    types = dtypes(model)
    xlsx = path.suffix == '.xlsx'
    read_as: dict[str, Any] = {
        n: 'string' if t == 'object' else t for n, t in types.items()
    }
    if xlsx:
        # pandas reads an empty cell of .xlsx as the text 'nan' for a boolean
        # dtype; a cell of .xlsx is a bool already, which typed() checks.
        read_as = {n: object if t == 'boolean' else t for n, t in read_as.items()}
    # A note column: each cell as the file holds it, so text in a text file.
    notes = object if xlsx else 'string'
    frame = reader(
        path,
        dtype=defaultdict(lambda: notes, {**read_as, KEY: 'string'}),
        keep_default_na=False,
        na_values=[''],
    )
    if KEY not in frame.columns:
        raise ValueError(f'a table has a column {KEY!r}, the key of each row')
    frame = frame.drop(columns=list(RESULT), errors='ignore').set_index(KEY)
    for name, column in frame.items():
        if types.get(name) == 'object':
            frame[name] = pd.Series(
                [None if pd.isna(v) else _read_cell(v) for v in column],
                index=frame.index,
                dtype=object,
            )
        elif name not in types and xlsx:
            frame[name] = _note(column)
    return typed(frame, model)


def _note(column: pd.Series) -> pd.Series:
    """A note column of a ``.xlsx`` file: of the dtype of its cells, else text."""
    typed_column = column.convert_dtypes()
    if column.notna().any() and str(typed_column.dtype) in _WORDS.keys() - {'object'}:
        return typed_column
    cells = [None if _empty(v) else str(v) for v in column]
    return pd.Series(cells, index=column.index, dtype='string')


def write_table(frame: pd.DataFrame, out: Path | TextIO) -> None:
    """
    Write ``frame`` as :func:`read_table` reads it back.

    To a table file, by its suffix, replaced at once, or as CSV to a stream.
    A named index, such as :data:`KEY`, becomes the first column.
    """
    suffix = '.csv'
    if isinstance(out, Path):
        _reader(out)  # rejects another suffix before anything is written
        suffix = out.suffix
    if frame.index.name is not None:
        frame = frame.reset_index()
    frame = frame.assign(
        **{
            name: [None if _empty(v) else _write_cell(v) for v in column]
            for name, column in frame.items()
            if column.dtype == object
        }
    )
    if isinstance(out, Path):
        replace_at_once(out, lambda path: _write_frame(frame, path, suffix))
    else:
        _write_frame(frame, out, suffix)


def _write_frame(frame: pd.DataFrame, out: Path | TextIO, suffix: str) -> None:
    if suffix == '.xlsx':
        frame.to_excel(out, index=False)
    else:
        frame.to_csv(out, index=False, sep='\t' if suffix == '.tsv' else ',')
