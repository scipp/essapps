# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The table of a batch as a frame: its dtypes, its cells, and ``rows.json``.

A table is one ``DataFrame``: the key of each row as index, named
:data:`KEY`, a column per parameter of the spec that the rows give, and notes,
any other column::

    key     run    name         sample      sample_transmission
    60339   60339  D2O buffer   run:60339   run:60305
    water   <NA>   by hand      run:60012   <NA>

A key is text, not empty, and unique, and no column is named :data:`KEY`.
A column of a parameter has the dtype that :func:`dtypes` reads from the type
of the parameter, with ``field_types.unwrapped`` and ``field_types.split``::

    the parameter takes                                   dtype
    a file, an output, a file or an output                string
    text, a Literal or an enum of text, a time, a date    string
    a number                                              Float64
    a whole number                                        Int64
    a truth value                                         boolean
    a list, a group, a value or an output, anything else  object
    (a Literal of numbers, for one)

``| None`` does not change the dtype. A parameter that takes text or an output
is refused: a cell cannot tell its text from an output.

A cell must be of the kind of its column, with no conversion by pandas:
``string`` takes text, ``Float64`` an ``int`` or a ``float``, ``Int64`` an
``int`` or a whole ``float`` (``1.0`` is kept as ``1``: pandas gives whole
numbers with a gap as floats), ``boolean`` a ``bool``, and ``object`` plain
data, the values of JSON as Python gives them. A note takes the dtype of
``convert_dtypes()``, which must be one of these five. An empty cell is
``None``, ``NaN`` or ``pd.NA`` as given; in the table it is ``pd.NA``, or
``None`` in an ``object`` column, and in a row read with ``to_dict('index')``
and in ``rows.json`` it is ``None``.

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

:func:`cell_of` gives the JSON form of anything else, such as a group, a time,
or a file inside a group (``{'@ref': 'file', 'file': 'run:1'}``).

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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any, Literal, get_args, get_origin

import pandas as pd
from pandas.io.json import build_table_schema
from pydantic import BaseModel
from pydantic.fields import FieldInfo
from pydantic_core import to_jsonable_python

from ess.spec import FileRef, OutputRef, field_types

from ..files import file

KEY = 'key'
"""The name of the index of a table: the key of each row."""

_WORDS = {
    'string': 'text',
    'Float64': 'a number',
    'Int64': 'a whole number',
    'boolean': 'true or false',
    'object': 'plain data',
}
"""What a cell of each dtype holds, as a message says it."""

_KIND = re.compile(r'[a-z]+:')
# What OutputRef.__str__ writes, given that a record ID (uuid4().hex) and an
# output name (a field name) contain none of '.', '[', ']', ':'.
_OUTPUT = re.compile(
    r'(?P<record>[^.\[\]:]+)\.(?P<output>[^.\[\]]+)(?:\[(?P<key>.*)\])?'
)


@dataclass(frozen=True)
class _Parameter:
    """What a parameter takes, as a cell gives it."""

    many: bool  # a list of what the other fields say
    inline: Any  # the part that a request writes, None if only an output
    file: bool
    output: bool


def _text(tp: Any) -> bool:
    """Whether a value of ``tp`` is text in its JSON form."""
    if get_origin(tp) is Literal:
        return all(type(a) is str for a in get_args(tp))
    if isinstance(tp, type) and issubclass(tp, Enum):
        return all(type(m.value) is str for m in tp)
    return tp is str or tp is date or tp in field_types.TIMES


def _parameter(name: str, info: FieldInfo) -> _Parameter:
    metadata = field_types.field_metadata(info)
    tp, _, metadata = field_types.unwrapped(info.annotation, metadata)
    many = get_origin(tp) is list
    if many:
        tp, _, metadata = field_types.unwrapped(get_args(tp)[0])
    inline, inline_metadata, referenced = field_types.split(tp, *metadata)
    if referenced and _text(inline):
        raise ValueError(
            f'{name}: takes text or an output, and a cell cannot tell the two apart'
        )
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
    return 'string' if _text(tp) else 'object'


def dtypes(model: type[BaseModel]) -> dict[str, str]:
    """
    The dtype of the column of each parameter of ``model``, by name. Raises
    ``ValueError`` for a parameter that takes text or an output.
    """
    return {n: _dtype(_parameter(n, i)) for n, i in model.model_fields.items()}


def _empty(value: Any) -> bool:
    """Whether a cell is empty: ``None``, ``NaN`` or ``pd.NA``, not a list."""
    return value is None or (pd.api.types.is_scalar(value) and bool(pd.isna(value)))


def _plain(value: Any) -> bool:
    """Whether ``value`` is plain data: what JSON holds, as Python gives it."""
    if isinstance(value, list):
        return all(_plain(v) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _plain(v) for k, v in value.items())
    return value is None or type(value) in (str, int, float, bool)


def _fits(value: Any, dtype: str) -> bool:
    """Whether a cell that is not empty is of the kind of a column of ``dtype``."""
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
    """``index`` as a table's index; raises ``ValueError`` for a key refused."""
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
    """The problems of the names of the columns of a table, each as a message."""
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
    """
    ``column`` as a column of ``dtype``, an empty cell as ``pd.NA`` or ``None``;
    and each cell that is not of its kind, as a message.
    """
    values = [None if _empty(v) else v for v in column.tolist()]
    refused = [
        f'row {k!r}, column {column.name!r}: {v!r} is not {_WORDS[dtype]}'
        for k, v in zip(column.index, values, strict=True)
        if v is not None and not _fits(v, dtype)
    ]
    if dtype == 'object':
        return pd.Series(values, index=column.index, dtype=object), refused
    if refused:
        return column, refused
    if dtype == 'Int64':  # a whole float, as pandas gives a column with a gap
        values = [None if v is None else int(v) for v in values]
    return pd.Series(pd.array(values, dtype=dtype), index=column.index), []


def typed(frame: pd.DataFrame, model: type[BaseModel]) -> pd.DataFrame:
    """
    ``frame`` as a batch holds it: the index of text named :data:`KEY`, each
    column of a parameter of ``model`` of its dtype (see :func:`dtypes`), and
    each note of the dtype of ``convert_dtypes()``.

    Raises ``ValueError``, naming each key, column or cell refused: a key that
    is not text, empty, or given twice; a column named :data:`KEY`, not named
    by text, or given twice; a note of another dtype than the five of a table;
    a cell that is not of the kind of its column.
    """
    index = _keys(frame.index)
    if problems := _names(frame.columns):
        raise ValueError('; '.join(problems))
    types = dtypes(model)
    columns = {}
    for name, column in frame.set_axis(index).items():
        if name in types:
            dtype = types[name]
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
    if problems:
        raise ValueError('; '.join(problems))
    return pd.DataFrame(columns, index=index, columns=list(frame.columns))


def _request_value(parameter: _Parameter, cell: Any) -> Any:
    if parameter.file and isinstance(cell, int) and not isinstance(cell, bool):
        return file(run=cell)
    if not isinstance(cell, str):
        return cell
    if parameter.file and cell and (not parameter.output or _KIND.match(cell)):
        return FileRef(file=cell)
    if parameter.output and (found := _OUTPUT.fullmatch(cell)):
        return OutputRef(**found.groupdict())
    return cell


def as_request(model: type[BaseModel], cells: Mapping[str, Any]) -> dict[str, Any]:
    """
    ``cells``, by the name of a parameter of ``model``, as a request takes
    them: at a parameter that takes a file, text that is not empty is the file
    of that name and a whole number a run; at one that takes an output, text
    such as ``'r17.centre'`` or ``'r18.slices[2]'`` is that output; at one that
    takes either, text with a kind, such as ``run:``, is a file, and other text
    an output. In a list, each element is converted so. Any other cell is
    returned as it is, for the request to refuse if it does not fit.
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
    The cell of ``value`` of a request, the inverse of :func:`as_request`:
    a file is its name, such as ``'run:1'``, an output its text, such as
    ``'r17.centre'``, a list holds the cell of each element, and anything else
    is its JSON form, as plain data.
    """
    if isinstance(value, list):
        return [cell_of(v) for v in value]
    if isinstance(value, FileRef | OutputRef):
        return str(value)
    return to_jsonable_python(value)


def data_of(frame: pd.DataFrame) -> dict[str, Any]:
    """
    The plain data of ``frame``, a table as :func:`typed` gives it, as
    ``rows.json`` holds it: ``{'schema': ..., 'data': [{'key': ..., ...}]}``,
    with ``None`` in each empty cell. Written as JSON by a writer of Python
    values, it keeps each value exact.
    """
    if frame.index.name != KEY:
        raise ValueError(f'a table has an index named {KEY!r}: see typed()')
    records = [{KEY: key, **row} for key, row in frame.to_dict('index').items()]
    return {'schema': build_table_schema(frame, version=False), 'data': records}


def frame_of(data: Any) -> pd.DataFrame:
    """
    The table of the plain data of :func:`data_of`, equal to the frame it was
    made of. Raises ``ValueError``, naming the entry, row or column, for data
    that are not such a table, or a cell that is not of the kind of its column.
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
