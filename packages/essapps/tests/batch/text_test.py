# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""How a person writes params in a cell of a table or a YAML file."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from ess.apps import file
from ess.apps.batch.text import named_file, read_cell, with_named_files, write_cell
from ess.spec import FileRef, Referenced, RunFile

from . import toys


class Several(BaseModel):
    runs: list[RunFile]


class MaybeFile(BaseModel):
    sample: RunFile | None = None


class Either(BaseModel):
    sample: RunFile | Referenced[toys.Offset]


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
def test_a_cell_is_read_as_json_if_it_is_json_and_else_as_text(
    text: str, value: Any
) -> None:
    assert read_cell(text) == value


@pytest.mark.parametrize(
    'value',
    ['1', 'null', 'water', {'x': [1, 2]}, None, 2.5, [1, 2], [{'x': None}]],
)
def test_a_value_written_is_read_back(value: Any) -> None:
    assert read_cell(write_cell(value)) == value


@pytest.mark.parametrize(
    'ref',
    [
        file(run=60339),
        FileRef.at(Path('/data/mask.xml')),
        FileRef(file='pid:abc'),
        [file(run=1), FileRef(file='pid:abc')],
    ],
    ids=['run', 'path', 'kind', 'list'],
)
def test_a_file_written_is_read_back_as_the_file_named(ref: Any) -> None:
    value = read_cell(write_cell(ref))

    read = (
        [named_file(v) for v in value] if isinstance(value, list) else named_file(value)
    )

    assert read == ref


@pytest.mark.parametrize(
    ('name', 'ref'),
    [
        (60339, file(run=60339)),
        ('pid:abc', FileRef(file='pid:abc')),
        ('data/mask.xml', FileRef.at(Path('data/mask.xml').absolute())),
        (True, True),
        ({'@ref': 'file', 'file': 'run:1'}, {'@ref': 'file', 'file': 'run:1'}),
    ],
    ids=['run', 'kind', 'path', 'bool', 'group'],
)
def test_a_name_is_a_run_a_file_of_a_kind_or_a_path_and_anything_else_stays(
    name: Any, ref: Any
) -> None:
    assert named_file(name) == ref


@pytest.mark.parametrize(
    ('model', 'params', 'expected'),
    [
        (
            toys.Params,
            {'sample': 60339, 'transmission': 'pid:t', 'thickness': 2},
            {
                'sample': file(run=60339),
                'transmission': FileRef(file='pid:t'),
                'thickness': 2,
            },
        ),
        (
            Several,
            {'runs': [1, 'pid:b']},
            {'runs': [file(run=1), FileRef(file='pid:b')]},
        ),
        (MaybeFile, {'sample': None}, {'sample': None}),
        (
            Either,
            {'sample': {'@ref': 'output', 'record': 'abc', 'output': 'offset'}},
            {'sample': {'@ref': 'output', 'record': 'abc', 'output': 'offset'}},
        ),
    ],
    ids=['file-parameters-only', 'list', 'optional-none', 'reference'],
)
def test_names_at_file_parameters_become_files_and_other_values_stay(
    model: type[BaseModel], params: dict[str, Any], expected: dict[str, Any]
) -> None:
    assert with_named_files(model, params) == expected
