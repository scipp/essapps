# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""How a person writes the settings of a batch in a template."""

from typing import Any

import pytest

from ess.apps import Client, SpecId, Template, file
from ess.apps.batch import Batch
from ess.apps.batch.text import checked_settings, with_latest
from ess.spec import FileRef

from . import toys

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

    assert checked_settings(batch, template) == {
        'binning': {'bins': 3},
        'offset': found.ref('offset'),
    }


def test_a_file_of_a_template_is_named_as_in_a_cell(client: Client) -> None:
    batch = Batch(client, toys.SERIES, label='series')
    template = Template(toys.SERIES, params={'runs': [60339, 'path:mask.xml']})

    assert checked_settings(batch, template) == {
        'runs': [file(run=60339), FileRef(file='path:mask.xml')]
    }


def test_a_setting_of_a_template_at_a_column_of_the_batch_is_left_out(
    batch: Batch,
) -> None:
    params = {'thickness': 2.0, 'binning': {'bins': 3}}
    template = Template(toys.STAGED, params=params, blanks=())

    assert checked_settings(batch, template) == {'binning': {'bins': 3}}


@pytest.mark.parametrize(
    ('spec', 'message'),
    [
        (toys.PLAIN, 'it holds the settings of plain, not staged'),
        (
            SpecId(name='staged', version=2),
            'it holds the settings of version 2 of staged, not of version 1',
        ),
    ],
    ids=['other-spec', 'other-version'],
)
def test_a_template_of_another_spec_or_version_is_refused_saying_why(
    batch: Batch, spec: Any, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        checked_settings(batch, Template(spec, blanks=toys.COLUMNS))
