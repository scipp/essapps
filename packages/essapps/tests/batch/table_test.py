# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The table of a batch as a frame: dtypes, cells, and rows.json."""

from collections.abc import Callable
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal

import pandas as pd
import pytest
from pydantic import BaseModel
from pydantic_core import from_json, to_json

from ess.apps.batch.table import (
    KEY,
    as_request,
    cell_of,
    data_of,
    dtypes,
    frame_of,
    read_cell,
    read_table,
    typed,
    write_cell,
    write_table,
)
from ess.spec import FileRef, OutputRef, Referenced, RunFile
from ess.spec.request_types import request_model

from . import toys


class Colour(Enum):
    RED = 'red'
    BLUE = 'blue'


class Kinds(BaseModel):
    count: int = 1
    flag: bool = False
    word: str = ''
    choice: Literal['a', 'b'] = 'a'
    level: Literal[1, 2] = 1
    when: datetime | None = None
    day: date | None = None
    colour: Colour = Colour.RED
    scale: float | None = None
    either: int | str = 0
    centre: Referenced[toys.Offset] | None = None
    source: RunFile | Referenced[toys.Offset] | None = None


def test_a_column_has_the_dtype_of_its_parameter() -> None:
    assert dtypes(Kinds) == {
        'count': 'Int64',
        'flag': 'boolean',
        'word': 'string',
        'choice': 'string',
        'level': 'object',
        'when': 'string',
        'day': 'string',
        'colour': 'string',
        'scale': 'Float64',
        'either': 'object',
        'centre': 'string',
        'source': 'string',
    }


def test_the_columns_of_the_toy_have_the_dtypes_of_their_parameters() -> None:
    assert dtypes(toys.Params) == {
        'binning': 'object',
        'sample': 'string',
        'transmission': 'string',
        'thickness': 'Float64',
        'offset': 'object',
    }
    assert dtypes(toys.SeriesParams) == {'runs': 'object'}


class Ambiguous(BaseModel):
    label: str | Referenced[toys.Offset]


def test_a_parameter_of_text_or_an_output_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match='label: takes text or an output'):
        dtypes(Ambiguous)


def test_typed_names_the_index_and_casts_parameters_and_notes() -> None:
    frame = pd.DataFrame(
        {
            'sample': ['run:1', None],
            'thickness': [2, None],
            'binning': [{'bins': 3}, float('nan')],
            'run': [1.0, None],
            'name': ['water', None],
        },
        index=['0342', 'b'],
    )

    table = typed(frame, toys.Params)

    assert table.index.name == KEY
    assert list(table.index) == ['0342', 'b']
    assert table.index.dtype == 'string'
    assert table.dtypes.astype(str).to_dict() == {
        'sample': 'string',
        'thickness': 'Float64',
        'binning': 'object',
        'run': 'Int64',
        'name': 'string',
    }
    assert table.loc['b', 'binning'] is None


@pytest.mark.parametrize(
    ('index', 'columns', 'message'),
    [
        (['a', 'a'], {}, "the key 'a' is given twice"),
        (['a', ''], {}, 'a key is empty'),
        ([60339.0], {}, '^a key is text, not a number such as 60339.0$'),
        (['a', 'b'], {'scale': [1.0, 'thick']}, "row 'b', column 'scale': 'thick'"),
        (['a'], {'scale': ['2']}, "row 'a', column 'scale': '2' is not a number"),
        (['a'], {'scale': [True]}, "row 'a', column 'scale': True is not a number"),
        (['a', 'b'], {'word': ['run:1', 60339.0]}, "row 'b', column 'word': 60339.0"),
        (['a'], {'count': [2.5]}, "row 'a', column 'count': 2.5 is not a whole"),
        (['a'], {'count': [float('inf')]}, "row 'a', column 'count': inf is not a"),
        (['a'], {'count': [True]}, "row 'a', column 'count': True is not a whole"),
        (['a'], {'count': [2**63]}, f"row 'a', column 'count': {2**63} is not a"),
        (['a'], {'flag': ['yes']}, "row 'a', column 'flag': 'yes' is not true"),
        (['a'], {'flag': [1]}, "row 'a', column 'flag': 1 is not true or false"),
        (['a'], {'level': [(1, 2)]}, r"row 'a', column 'level': \(1, 2\) is not plain"),
        (['a'], {'key': ['x']}, "a column is named 'key'.*set_index"),
        (['a'], {0: ['x']}, 'the column 0 is not text'),
        (
            ['a'],
            {'when': [datetime(2026, 1, 1, tzinfo=UTC)]},
            r"column 'when': Timestamp.* is not text",
        ),
    ],
)
def test_typed_refuses_a_key_a_column_or_a_cell_and_names_it(
    index: list[Any], columns: dict[Any, list[Any]], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        typed(pd.DataFrame(columns, index=index), Kinds)


def test_typed_refuses_a_column_given_twice() -> None:
    frame = pd.DataFrame([['a', 'b']], columns=['word', 'word'], index=['r'])

    with pytest.raises(ValueError, match="the column 'word' is given twice"):
        typed(frame, Kinds)


def test_typed_refuses_a_note_of_another_dtype_by_name() -> None:
    frame = pd.DataFrame({'start': [pd.Timestamp('2026-11-19')]}, index=['a'])

    with pytest.raises(ValueError, match=r"the note 'start' is datetime64.*text"):
        typed(frame, Kinds)


def test_a_whole_float_in_a_column_of_whole_numbers_is_kept_as_an_int() -> None:
    frame = pd.DataFrame({'count': [1, None]}, index=['a', 'b'])

    table = typed(frame, Kinds)

    assert table['count'].dtype == 'Int64'
    assert table['count'].tolist() == [1, pd.NA]
    assert type(table.to_dict('index')['a']['count']) is int


def test_a_float_nan_in_a_column_of_numbers_is_an_empty_cell() -> None:
    frame = pd.DataFrame({'scale': [float('nan'), 1.0]}, index=['a', 'b'])

    table = typed(frame, Kinds)

    assert table['scale'].isna().tolist() == [True, False]
    pd.testing.assert_frame_equal(frame_of(data_of(table)), table)


@pytest.fixture
def exact() -> pd.DataFrame:
    key = pd.Index(['a', 'b', '0342'], name=KEY, dtype='string')
    return pd.DataFrame(
        {
            'x': pd.array([0.1 + 0.2, None, 6.02214076e23], dtype='Float64'),
            'n': pd.array([2**53 + 1, None, 1], dtype='Int64'),
            'f': pd.array([True, None, False], dtype='boolean'),
            's': pd.array(['run:1', None, 'r17.centre'], dtype='string'),
            'o': pd.Series([['run:1'], None, {'v': 0.1 + 0.2}], index=key),
        },
        index=key,
    )


def test_the_data_of_a_table_read_back_as_json_give_the_table_exactly(
    exact: pd.DataFrame,
) -> None:
    back = frame_of(from_json(to_json(data_of(exact))))

    pd.testing.assert_frame_equal(back, exact)
    assert back.loc['a', 'x'] == 0.1 + 0.2
    assert back.loc['a', 'n'] == 2**53 + 1


def test_the_data_of_a_table_without_rows_give_its_columns_and_dtypes(
    exact: pd.DataFrame,
) -> None:
    empty = exact.iloc[:0]

    pd.testing.assert_frame_equal(frame_of(data_of(empty)), empty)


def test_the_data_of_a_table_without_columns_give_its_keys() -> None:
    table = typed(pd.DataFrame(index=['a', 'b']), Kinds)

    pd.testing.assert_frame_equal(frame_of(data_of(table)), table)


@pytest.mark.parametrize(
    ('change', 'message'),
    [
        (lambda d: d.pop('schema'), 'not a table'),
        (lambda d: d.update(data={'a': 1}), 'data: not a list of rows'),
        (lambda d: d['data'][1].pop(KEY), r"data\[1\]: not an object with a 'key'"),
        (lambda d: d['data'][2].update(y=1), r'data\[2\]: no field y'),
        (lambda d: d['data'][0].update(n=2.5), "row 'a', column 'n': 2.5 is not"),
        (lambda d: d['data'][0].update(s=1.0), "row 'a', column 's': 1.0 is not"),
        (lambda d: d['data'][0].update(n=True), "row 'a', column 'n': True is not"),
        (lambda d: d['data'][1].update(key='a'), "the key 'a' is given twice"),
        (lambda d: d['schema']['fields'].append({'name': 'n'}), "'n' is given twice"),
    ],
)
def test_data_that_are_not_a_table_are_refused_by_entry(
    exact: pd.DataFrame, change: Callable[[dict[str, Any]], object], message: str
) -> None:
    data = from_json(to_json(data_of(exact)))
    change(data)

    with pytest.raises(ValueError, match=message):
        frame_of(data)


class Either(BaseModel):
    source: RunFile | Referenced[toys.Offset]
    centre: Referenced[toys.Offset]


def output(record: str, name: str, key: str | None = None) -> OutputRef:
    return OutputRef(record=record, output=name, key=key)


@pytest.mark.parametrize(
    ('model', 'cells', 'request_values'),
    [
        (toys.Params, {'sample': 'run:60339'}, {'sample': FileRef(file='run:60339')}),
        (toys.Params, {'sample': 'path:a.nxs'}, {'sample': FileRef(file='path:a.nxs')}),
        (toys.Params, {'sample': ''}, {'sample': ''}),
        (toys.Params, {'offset': 'r17.offset'}, {'offset': output('r17', 'offset')}),
        (toys.Params, {'binning': {'bins': 3}}, {'binning': {'bins': 3}}),
        (Either, {'centre': 'r18.slices[2]'}, {'centre': output('r18', 'slices', '2')}),
        (Either, {'source': 'run:1'}, {'source': FileRef(file='run:1')}),
        (Either, {'source': 'r17.offset'}, {'source': output('r17', 'offset')}),
        (
            toys.SeriesParams,
            {'runs': ['run:1', 'run:2']},
            {'runs': [FileRef(file='run:1'), FileRef(file='run:2')]},
        ),
    ],
)
def test_a_cell_is_converted_to_what_a_request_takes_and_back(
    model: type[BaseModel], cells: dict[str, Any], request_values: dict[str, Any]
) -> None:
    converted = as_request(model, cells)

    assert converted == request_values
    assert {name: cell_of(v) for name, v in converted.items()} == cells


def test_a_whole_number_at_a_file_is_a_run() -> None:
    assert as_request(toys.SeriesParams, {'runs': [60339, 'run:2']}) == {
        'runs': [FileRef(file='run:60339'), FileRef(file='run:2')]
    }


class Masked(BaseModel):
    mask: RunFile


class Grouped(BaseModel):
    masked: Masked
    when: datetime


def test_the_cell_of_a_group_or_a_time_is_its_json_form_which_a_request_takes() -> None:
    values = {
        'masked': Masked.model_construct(mask=FileRef(file='run:1')),
        'when': datetime(2026, 11, 19, 12, tzinfo=UTC),
    }

    cells = {name: cell_of(v) for name, v in values.items()}

    assert cells == {
        'masked': {'mask': {'@ref': 'file', 'file': 'run:1'}},
        'when': '2026-11-19T12:00:00Z',
    }
    request_model(Grouped).model_validate(as_request(Grouped, cells))


# A table file


class Filed(BaseModel):
    x: float | None = None
    n: int | None = None
    f: bool | None = None
    s: RunFile | None = None
    o: list[Any] | None = None


@pytest.fixture
def filed() -> pd.DataFrame:
    """A table of each dtype, with notes, a key that a number would lose, and gaps."""
    key = pd.Index(['a', '0342', 'b'], name=KEY, dtype='string')
    return typed(
        pd.DataFrame(
            {
                'x': [0.1 + 0.2, None, 6.02214076e23],
                'n': [2**40 + 1, None, 1],
                'f': [True, None, False],
                's': ['run:1', None, 'path:a b, "c".nxs'],
                'o': [['run:1', 2], None, {'v': 1.5}],
                'name': ['NA', None, 'water'],
                'run': [60339, 60340, None],
            },
            index=key,
        ).astype({'o': object}),
        Filed,
    )


@pytest.mark.parametrize('suffix', ['.csv', '.tsv', '.xlsx'])
def test_a_table_written_to_a_file_is_read_back_equal(
    tmp_path: Path, filed: pd.DataFrame, suffix: str
) -> None:
    path = tmp_path / f'table{suffix}'

    write_table(filed, path)

    pd.testing.assert_frame_equal(read_table(path, Filed), filed)


def test_a_list_or_a_group_is_json_text_in_a_table_file(
    tmp_path: Path, filed: pd.DataFrame
) -> None:
    path = tmp_path / 'table.csv'

    write_table(filed, path)

    assert path.read_text().splitlines()[1] == (
        'a,0.30000000000000004,1099511627777,True,run:1,"[""run:1"",2]",NA,60339'
    )


def test_the_columns_of_what_came_of_each_row_are_dropped_when_read(
    tmp_path: Path, filed: pd.DataFrame
) -> None:
    path = tmp_path / 'done.csv'
    write_table(filed.assign(status='completed', record='r1', failure=''), path)

    pd.testing.assert_frame_equal(read_table(path, Filed), filed)


@pytest.mark.parametrize(
    ('text', 'message'),
    [
        ('sample,x\nrun:1,1\n', "a table has a column 'key'"),
        ('key,x\na,thick\n', 'thick'),
        ('key,n\na,2.5\n', 'cast'),
        ('key,f\na,yes\n', 'yes'),
        ('key,x\na,1\na,2\n', "the key 'a' is given twice"),
    ],
    ids=['no-key', 'not-a-number', 'not-whole', 'not-true-or-false', 'key-twice'],
)
def test_a_table_file_that_is_not_a_table_is_refused_saying_why(
    tmp_path: Path, text: str, message: str
) -> None:
    path = tmp_path / 'table.csv'
    path.write_text(text)

    with pytest.raises(ValueError, match=message):
        read_table(path, Filed)


@pytest.mark.parametrize(
    'function',
    [lambda p: read_table(p, Filed), lambda p: write_table(pd.DataFrame(), p)],
    ids=['read', 'write'],
)
def test_a_table_file_of_an_unknown_suffix_is_refused(
    tmp_path: Path, function: Callable[[Path], object]
) -> None:
    with pytest.raises(ValueError, match='a table is one of'):
        function(tmp_path / 'table.json')


@pytest.mark.parametrize(
    ('text', 'value'),
    [
        ('1.5', 1.5),
        ('null', None),
        ('{"x": 0}', {'x': 0}),
        ('[1, 2]', [1, 2]),
        ('water', 'water'),
    ],
)
def test_a_cell_of_plain_data_is_read_as_json_if_it_is_json_and_else_as_text(
    text: str, value: Any
) -> None:
    assert read_cell(text) == value


@pytest.mark.parametrize(
    'value',
    ['1', 'null', 'water', {'x': [1, 2]}, None, 2.5, True, [1, 2], [{'x': None}]],
)
def test_a_cell_of_plain_data_written_is_read_back(value: Any) -> None:
    assert read_cell(write_cell(value)) == value
