# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The batch table as a frame: its dtypes, its cells, ``rows.json``, and table
files.

A batch table is one ``DataFrame``: the key of each row as index, named
:data:`KEY`, a parameter column per parameter of the spec that the rows give,
and notes, any other column::

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
``convert_dtypes()``, which must be one of these five. A note with no value
keeps its dtype if it is ``string``, ``Float64``, ``Int64`` or ``boolean``,
and is ``string`` otherwise: pandas makes a column ``float64`` or ``object``
when it fills it with empty cells, as ``concat`` does. At a parameter that
takes an output, text that :func:`as_request` reads as neither an output nor a
file is refused too, since the workspace would refuse it: a cell names an
output as ``RECORD.OUTPUT``. The table owns the
lists and groups in its ``object`` cells: :func:`typed` copies them. An empty
cell is
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

A table file, ``.csv``, ``.tsv`` or ``.xlsx``, is the frame as text
(:func:`write_table`), with the key as its column ``key``; :func:`read_table`
reads its parameter columns back equal. A list or a group is JSON text, as is
a cell of an ``object`` column that would read back as something else
(:func:`_write_cell`)::

    key,sample,thickness,binning,name
    60339,run:60339,1.5,"{""bins"": 4}",water
    60340,run:60340,,,NA

Empty is the only missing marker, so a note ``NA`` is text. A note has no
type in a file of text, so it reads back as text; only ``rows.json`` keeps the
dtype of every column.
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

from ess.spec import FileRef, OutputRef, field_types

from ..files import file, replace_at_once

KEY = 'key'
"""The name of the index of a batch table: the key of each row."""

NAME = 'name'
"""The note that names a row, as the page shows it and a published dataset
is named; a row without one is named by its key."""


def names(frame: pd.DataFrame, columns: Collection[str]) -> dict[str, str]:
    """
    The name of each row of ``frame``, a table whose parameter columns are
    ``columns``, by key: its note :data:`NAME`, else its key.
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
    ``frame`` as a batch holds it: the index of text named :data:`KEY`, each
    column of a parameter of ``model`` of its dtype (see :func:`dtypes`), and
    each note of the dtype of ``convert_dtypes()``; a note with no value keeps
    its dtype, or is ``string`` if that is ``float64`` or ``object``. A list
    or a group in a cell is copied.

    Raises ``ValueError``, naming each key, column or cell refused: a key that
    is not text, empty, or given twice; a column named :data:`KEY`, not named
    by text, or given twice; a note of another dtype than the five of a table;
    a cell that is not of the kind of its column; text that names no output
    at a parameter that takes one.
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
    if parameter.file and isinstance(cell, int) and not isinstance(cell, bool):
        return file(run=cell)
    if not isinstance(cell, str):
        return cell
    if parameter.file and cell and (not parameter.output or _KIND.match(cell)):
        return FileRef(file=cell)
    if parameter.output and (found := _OUTPUT.fullmatch(cell)):
        return OutputRef(**found.groupdict())
    return cell


_NOT_AN_OUTPUT = (
    'names no output: a cell names one as RECORD.OUTPUT, such as r17.centre'
)
"""What a message says of text in a cell that names no output where one is taken."""


def _names_no_output(parameter: _Parameter, cell: Any) -> bool:
    """
    Whether ``cell`` is text, or a list with text, at a parameter that takes
    an output, which :func:`as_request` leaves as text: it names no output and
    no file, and the parameter takes no text.
    """
    cells = cell if parameter.many and isinstance(cell, list) else [cell]
    return parameter.output and any(
        isinstance(c, str) and c and isinstance(_request_value(parameter, c), str)
        for c in cells
    )


def not_outputs(model: type[BaseModel], values: Mapping[str, Any]) -> list[str]:
    """
    The names of ``values``, by the name of a parameter of ``model``, that are
    text at a parameter that takes an output and name none: values that
    :func:`as_request` leaves as text, which the workspace refuses.
    """
    fields = model.model_fields
    return [
        name
        for name, value in values.items()
        if name in fields and _names_no_output(_parameter(name, fields[name]), value)
    ]


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


RESULT = ('status', 'record', 'failure')
"""The columns of what came of each row, which a table file may hold."""

_READERS: dict[str, Callable[..., pd.DataFrame]] = {
    '.csv': pd.read_csv,
    '.tsv': partial(pd.read_csv, sep='\t'),
    '.xlsx': pd.read_excel,
}

SUFFIXES = tuple(_READERS)
"""The suffixes of the files that hold a table."""


def _reader(path: Path) -> Callable[..., pd.DataFrame]:
    if (reader := _READERS.get(path.suffix)) is None:
        raise ValueError(f'{path}: a table is one of {", ".join(SUFFIXES)}')
    return reader


def _read_cell(text: str) -> Any:
    """The value of a cell of an ``object`` column: its JSON value, or its text."""
    try:
        return json.loads(text)
    except ValueError:
        return text


def _write_cell(value: Any) -> str:
    """The text that :func:`_read_cell` reads as ``value``, plain data."""
    if isinstance(value, str) and _read_cell(value) == value:
        return value
    return to_json(value).decode()


def read_text(path: Path) -> pd.DataFrame:
    """
    The table in a file of one of :data:`SUFFIXES`, each cell as text, an
    empty one as ``''``: a table that is not of a batch, such as a run table,
    or one with the columns of :data:`RESULT`.
    """
    return _reader(path)(path, dtype=str, keep_default_na=False)


def read_table(path: Path, model: type[BaseModel]) -> pd.DataFrame:
    """
    The table in a file of one of :data:`SUFFIXES`, as :func:`typed` gives it
    for the params model ``model``. The file has a column :data:`KEY`. A column
    of a parameter is read with its dtype, an ``object`` column cell by cell
    with :func:`_read_cell`; the columns of :data:`RESULT` are dropped. Only an
    empty cell is missing.

    The type of a note is never guessed from text. A note of a ``.csv`` or
    ``.tsv`` file is text, so ``0342`` stays ``'0342'``. A cell of a ``.xlsx``
    file has a type, so a note of one keeps the dtype of its cells if that is
    one of the five of a table, and is text otherwise, such as for dates or
    for cells of several types. Only ``rows.json`` keeps the dtype of every
    column (:func:`frame_of`). A ``.xlsx`` file keeps 15 significant digits
    of a number, as Excel does: ``0.1 + 0.2`` reads back as ``0.3``.

    Raises ``ValueError``, naming the file, if the file has no column
    :data:`KEY`, a cell is not of the kind of its column, or :func:`typed`
    refuses the table.
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
    # A note: each cell as the file holds it, text if the file is text.
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
    """
    A note of a ``.xlsx`` file: of the dtype of its cells, or text. A note
    without a value in any row is text, as in a ``.csv`` file.
    """
    typed_column = column.convert_dtypes()
    if column.notna().any() and str(typed_column.dtype) in _WORDS.keys() - {'object'}:
        return typed_column
    cells = [None if _empty(v) else str(v) for v in column]
    return pd.Series(cells, index=column.index, dtype='string')


def write_table(frame: pd.DataFrame, out: Path | TextIO) -> None:
    """
    Write ``frame`` as :func:`read_table` reads it back: to a file of one of
    :data:`SUFFIXES`, by its suffix, replaced at once
    (:func:`~ess.apps.files.replace_at_once`), or as CSV to a stream. A named index,
    such as :data:`KEY`, is the first column, and each cell of an ``object``
    column is :func:`_write_cell` of it.
    """
    suffix = '.csv'
    if isinstance(out, Path):
        _reader(out)  # refuses another suffix before anything is written
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
