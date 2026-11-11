# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""How a person writes params in a cell of a table or a YAML file."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from ess.apps import Client, SpecId, Template, file
from ess.apps.batch import Batch
from ess.apps.batch.text import (
    named_file,
    read_cell,
    settings_of,
    with_latest,
    with_named_files,
    write_cell,
)
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


# @latest


def _find(client: Client, run: int, *, label: str, member: str | None = None):
    record = client.submit(
        toys.FIND, params={'sample': file(run=run)}, label=label, member=member
    )
    client.wait([record])
    return record


def test_latest_becomes_the_reference_to_the_output_of_the_newest_record(
    folder_client: Client,
) -> None:
    _find(folder_client, 1, label='offset')
    newest = _find(folder_client, 2, label='offset')

    value = with_latest(folder_client, {'@latest': 'offset', 'output': 'offset'})

    assert value == newest.ref('offset')


def test_latest_with_a_member_selects_among_the_members_of_the_label(
    folder_client: Client,
) -> None:
    wanted = _find(folder_client, 1, label='offset', member='a')
    _find(folder_client, 2, label='offset', member='b')

    value = with_latest(
        folder_client, {'@latest': 'offset', 'member': 'a', 'output': 'offset'}
    )

    assert value == wanted.ref('offset')


def test_latest_is_found_inside_a_dict_and_a_list(folder_client: Client) -> None:
    record = _find(folder_client, 1, label='offset')
    latest = {'@latest': 'offset', 'output': 'offset'}

    value = with_latest(folder_client, {'a': {'b': [latest, 1]}, 'c': 2})

    assert value == {'a': {'b': [record.ref('offset'), 1]}, 'c': 2}


def test_latest_of_a_label_without_records_is_refused(folder_client: Client) -> None:
    with pytest.raises(ValueError, match='nothing'):
        with_latest(folder_client, {'@latest': 'nothing', 'output': 'offset'})


def test_latest_of_an_output_that_the_record_lacks_is_refused(
    folder_client: Client,
) -> None:
    _find(folder_client, 1, label='offset')

    with pytest.raises(ValueError, match='nothing'):
        with_latest(folder_client, {'@latest': 'offset', 'output': 'nothing'})


def test_latest_with_another_key_next_to_it_is_refused(folder_client: Client) -> None:
    _find(folder_client, 1, label='offset')

    with pytest.raises(ValueError, match='member and output only'):
        with_latest(
            folder_client, {'@latest': 'offset', 'output': 'offset', 'extra': 1}
        )


def test_latest_with_a_label_that_is_not_text_is_refused(
    folder_client: Client,
) -> None:
    with pytest.raises(ValueError, match='as text'):
        with_latest(folder_client, {'@latest': False, 'output': 'offset'})


def test_a_value_without_latest_comes_back_equal(folder_client: Client) -> None:
    value = {'a': [1, {'b': 'x'}], 'c': None, 'd': 2.5}

    assert with_latest(folder_client, value) == value


# The settings of a batch


def test_the_settings_of_a_template_have_latest_read(
    batch: Batch, client: Client, sample: toys.Measure
) -> None:
    sample(1, 'water')
    found = _find(client, 1, label='offset')
    params = {
        'binning': {'bins': 3},
        'offset': {'@latest': 'offset', 'output': 'offset'},
    }
    template = Template(toys.STAGED, params=params, blanks=toys.COLUMNS)

    assert settings_of(batch, template) == {
        'binning': {'bins': 3},
        'offset': found.ref('offset'),
    }


@pytest.mark.parametrize(
    ('spec', 'blanks', 'message'),
    [
        (toys.PLAIN, toys.COLUMNS, 'it holds the settings of plain, not staged'),
        (
            SpecId(name='staged', version=2),
            toys.COLUMNS,
            'it holds the settings of version 2 of staged, not of version 1',
        ),
        (
            toys.STAGED,
            ('sample',),
            'its columns are sample, not sample, transmission, thickness',
        ),
        (toys.STAGED, (), 'its columns are none, not sample, transmission'),
    ],
    ids=['other-spec', 'other-version', 'other-columns', 'no-columns'],
)
def test_a_template_that_does_not_fit_the_batch_is_refused_saying_why(
    batch: Batch, spec: Any, blanks: tuple[str, ...], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        settings_of(batch, Template(spec, blanks=blanks))
