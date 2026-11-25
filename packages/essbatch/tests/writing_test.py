# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Writing the results of a batch by its form."""

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest
from ess.batch import Batch, BatchManager, FolderDefinitions
from ess.batch.definition import Form
from ess.batch.writing import Writing, load_form

from ess.apps import Client
from ess.apps.writers import Person, Writer

from .toys import TEXT

ADA = Person(name='Ada Lovelace', email='ada@example.org')


@pytest.fixture
def writing(manager: BatchManager) -> Writing:
    return Writing(manager, {'plain': [TEXT]}, ADA)


def test_until_set_the_form_is_of_the_first_writer_every_output_and_the_contact(
    writing: Writing, done_batch: Batch
) -> None:
    assert writing.form(done_batch.label) == Form(
        format='text',
        outputs=('total',),
        description='2 mm cells',
        owner='Ada Lovelace',
        contact_email='ada@example.org',
    )
    assert [w.name for w in writing.writers(done_batch.label)] == [
        'text',
        'scipp-hdf5',
    ]


def test_without_writers_or_contact_the_form_is_scipp_hdf5_and_names_no_owner(
    manager: BatchManager, done_batch: Batch
) -> None:
    form = Writing(manager, {}).form(done_batch.label)

    assert (form.format, form.owner, form.contact_email) == ('scipp-hdf5', '', '')


def test_a_form_set_is_kept_with_the_batch(
    writing: Writing, done_batch: Batch, client: Client, definitions: FolderDefinitions
) -> None:
    form = Form(format='scipp-hdf5', outputs=('total',), name='{key}')

    writing.set_form(done_batch.label, form)

    again = Writing(BatchManager(client, definitions=definitions), {})
    assert again.form(done_batch.label) == form


@pytest.mark.parametrize(
    ('form', 'match'),
    [
        (Form(format='xye', outputs=('total',)), 'no writer of the format xye'),
        (Form(format='text', outputs=('iofq',)), 'plain has no output iofq'),
        (Form(format='scipp-hdf5'), 'scipp HDF5 writes the outputs chosen'),
    ],
)
def test_a_form_of_another_format_or_output_or_none_chosen_is_refused(
    writing: Writing, done_batch: Batch, form: Form, match: str
) -> None:
    before = writing.form(done_batch.label)

    with pytest.raises(ValueError, match=match):
        writing.set_form(done_batch.label, form)
    assert writing.form(done_batch.label) == before


@pytest.mark.parametrize(
    ('name', 'match'),
    [
        ('{title} {sample}', 'not {sample}'),
        ('{title:>99}', 'not {title}'),
        ('{title!r}', 'not {title}'),
        ('{title.upper}', 'not {title.upper}'),
        ('{title', 'do not match'),
        (' ', 'not empty'),
    ],
)
def test_a_name_with_another_field_a_format_or_nothing_is_refused(
    name: str, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        Form(format='text', name=name)


def test_a_row_is_written_by_the_form_with_its_record(
    writing: Writing, done_batch: Batch, tmp_path: Path
) -> None:
    record = done_batch.state(['1'])['1'].record
    assert record is not None

    writing.by_form(done_batch.label)[0]('1', record, tmp_path / '1')

    assert sorted(p.name for p in (tmp_path / '1').iterdir()) == [
        '_record.json',
        'total.txt',
    ]
    assert (tmp_path / '1' / 'total.txt').read_text() == 'Morning water: 1.0'
    written = json.loads((tmp_path / '1' / '_record.json').read_text())
    assert written['id'] == record.id


def test_a_kept_form_of_a_format_this_process_lacks_is_refused_naming_it(
    writing: Writing, manager: BatchManager, done_batch: Batch
) -> None:
    writing.set_form(done_batch.label, Form(format='text'))

    with pytest.raises(ValueError, match='no writer of the format text'):
        Writing(manager, {}).by_form(done_batch.label)


@pytest.mark.parametrize(
    ('writers', 'match'),
    [
        ({'plain': [replace(TEXT, outputs=('iofq',))]}, 'reads iofq'),
        ({'nope': [TEXT]}, 'nope'),
        ({'plain': [TEXT, TEXT]}, 'one format'),
    ],
)
def test_writers_of_unknown_specs_outputs_or_twice_one_format_are_refused(
    manager: BatchManager, writers: Mapping[str, list[Writer]], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        Writing(manager, writers)


def test_the_form_text_is_read_back(
    writing: Writing, done_batch: Batch, tmp_path: Path
) -> None:
    path = tmp_path / 'form.yaml'
    path.write_text(writing.form_text(done_batch.label))

    assert load_form(path) == writing.form(done_batch.label)


def test_removing_the_batch_forgets_its_form(
    writing: Writing, done_batch: Batch, manager: BatchManager
) -> None:
    writing.set_form(done_batch.label, Form(format='text'))

    manager.remove(done_batch.label)

    assert manager.definitions.labels() == []
