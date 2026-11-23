# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The definition of batches kept in a folder workspace, opened twice."""

import json
import re
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from ess.apps import Client, Selector, SubmitError, Template
from ess.apps.batch import (
    Batch,
    BatchManager,
    CorrectedFiles,
    FileInfo,
    FolderDefinitions,
    Header,
    Intake,
    NotKept,
    RowSource,
)
from ess.apps.batch.definition import (
    CORRECTIONS,
    FORMAT,
    FromSource,
    Key,
    header_data,
    header_key,
    header_of,
    rows_key,
    settings_key,
    source_data,
    source_key,
    source_of,
)
from ess.apps.batch.table import KEY, frame_of
from ess.apps.batch.templates import load_template
from ess.apps.folder import folder_files, open_folder
from ess.spec import Binding

from .toys import PLAIN, Measure, edit, rows


def first_transmission(files: Sequence[FileInfo]) -> pd.DataFrame:
    """A row per sample run, each with the first transmission run listed."""
    transmissions = [str(f.ref) for f in files if f.metadata['role'] == 'transmission']
    return pd.DataFrame.from_records(
        [
            {
                KEY: str(f.metadata['run']),
                'run': f.metadata['run'],
                'sample': str(f.ref),
                'transmission': transmissions[0],
            }
            for f in files
            if f.metadata['role'] == 'sample'
        ]
    ).set_index(KEY)


FIRST_TRANSMISSION = RowSource(first_transmission, fills=('sample', 'transmission'))

type Opened = Callable[[], AbstractContextManager[tuple[Intake, CorrectedFiles]]]


@pytest.fixture
def opened(folder: Path, folder_bindings: list[Binding]) -> Opened:
    """Opens the folder: an intake of its batches, and the file source."""

    @contextmanager
    def opened() -> Iterator[tuple[Intake, CorrectedFiles]]:
        store = FolderDefinitions(folder)
        files = CorrectedFiles(folder_files(folder), definitions=store)
        with open_folder(folder, folder_bindings, files=files) as client:
            sources = {'first': FIRST_TRANSMISSION}
            yield (
                Intake(BatchManager(client, definitions=store), sources=sources),
                files,
            )

    return opened


@pytest.fixture
def made(opened: Opened) -> str:
    """A batch ``b`` of PLAIN of the rows of samples 1, 2 and 3, kept in the folder."""
    with opened() as (intake, _):
        return intake.manager.create(
            PLAIN.name, title='B', rows=intake.rows_of('first'), label='b'
        ).label


def test_a_batch_opened_again_has_the_table_and_the_settings_it_had(
    opened: Opened, made: str
) -> None:
    with opened() as (intake, _):
        batch = intake.manager.batch(made)
        batch.settings = {'binning': {'bins': 3}}
        frame = batch.frame().drop('3').assign(thickness=[0.1 + 0.2, None])
        frame.loc['mine', 'sample'] = 'run:2'
        frame['note'] = pd.Series([['a', 1], {'b': None}, 2**53 + 1], index=frame.index)
        batch.put(frame)
        before = batch.frame()

    with opened() as (intake, _):
        batch = intake.manager.batch(made)

        pd.testing.assert_frame_equal(batch.frame(), before)
        assert batch.settings == {'binning': {'bins': 3}}


def test_a_table_kept_stays_when_its_files_go(
    opened: Opened, made: str, folder: Path
) -> None:
    with opened() as (intake, _):
        before = intake.manager.batch(made).frame()
    files = folder / 'files.csv'
    files.write_text(files.read_text().replace('run-1001.json,1001,transmission', ''))

    with opened() as (intake, _):
        pd.testing.assert_frame_equal(intake.manager.batch(made).frame(), before)
        assert intake.manager.batch(made).state()['1'].problems != ()


def test_the_corrections_of_the_files_are_there_after_a_restart(
    opened: Opened,
) -> None:
    with opened() as (_, files):
        (bad,) = files.list(Selector(role='bad'))
        files.correct(bad, role='sample')

    with opened() as (intake, files):
        assert files.corrections(bad) == {'role': 'sample'}
        assert '4' in intake.rows_of('first').index


def test_the_settings_kept_are_a_template_of_all_settings_that_the_command_reads(
    opened: Opened, made: str, folder: Path
) -> None:
    settings = {'binning': {'bins': 3}, 'thickness': 2.0}  # thickness: a column
    with opened() as (intake, _):
        batch = intake.manager.batch(made)
        batch.settings = settings
        batch.make_column('thickness')

    path = FolderDefinitions(folder).path(settings_key(made))

    assert load_template(path) == Template(batch.spec, params=settings)
    with opened() as (intake, _):
        assert intake.manager.batch(made).settings == settings


@pytest.mark.parametrize(
    ('data', 'entry'),
    [
        ({'data': []}, 'not a table'),
        ({'schema': {'fields': [{'name': 'x'}]}, 'data': []}, "schema: no field 'key'"),
        (
            {
                'schema': {'fields': [{'name': 'key'}, {'name': 'thickness'}]},
                'data': [{'key': '1', 'thickness': 'thick'}],
            },
            "row '1', column 'thickness': 'thick' is not a number",
        ),
    ],
)
def test_rows_that_cannot_be_read_are_refused_naming_the_file_and_the_entry(
    opened: Opened, made: str, folder: Path, data: Any, entry: str
) -> None:
    FolderDefinitions(folder).save(rows_key(made), data)

    with opened() as (intake, _):
        with pytest.raises(ValueError, match=rf'b/rows\.json: {re.escape(entry)}'):
            intake.manager.batch(made)


def test_a_file_that_is_not_json_is_refused_naming_it(
    opened: Opened, folder: Path
) -> None:
    path = FolderDefinitions(folder).path(CORRECTIONS)
    path.parent.mkdir(parents=True)
    path.write_text('{"x": ')

    with pytest.raises(ValueError, match=r'definitions/corrections\.json: '), opened():
        pass


def test_corrections_that_cannot_be_read_are_refused_naming_the_entry(
    folder: Path,
) -> None:
    definitions = FolderDefinitions(folder)
    definitions.save(CORRECTIONS, {'path:run-1.json': 'sample'})

    with pytest.raises(ValueError, match=r'corrections\.json: path:run-1\.json: '):
        CorrectedFiles(folder_files(folder), definitions=definitions)


def test_settings_of_another_version_are_taken_as_they_are(
    opened: Opened, made: str, folder: Path
) -> None:
    kept = {'spec': {'name': 'plain', 'version': 9}, 'params': {'gone': 1}}
    FolderDefinitions(folder).save(settings_key(made), kept)

    with opened() as (intake, _):
        batch = intake.manager.batch(made)
        assert batch.settings == {'gone': 1}
        with pytest.raises(SubmitError, match='gone'):
            batch.state()


def test_a_folder_that_cannot_be_written_raises_that_the_table_was_not_kept(
    tmp_path: Path,
) -> None:
    (tmp_path / 'definitions').write_text('a file where the folder should be')

    with pytest.raises(NotKept, match='definitions'):
        FolderDefinitions(tmp_path).save(CORRECTIONS, {})


def test_a_label_is_kept_in_a_folder_of_its_own(tmp_path: Path) -> None:
    store = FolderDefinitions(tmp_path)
    labels = ['auto/iofq', 'auto%2Fiofq', '..', '.', 'labels']
    paths = [store.path(rows_key(label)) for label in labels]

    assert len(set(paths)) == len(labels)
    assert all(p.parent.parent == tmp_path / 'definitions' / 'labels' for p in paths)


@pytest.mark.parametrize('label', ['../..', 'a/../b', '%2E%2E', 'x.json'])
def test_a_label_that_looks_like_a_path_keeps_its_rows_inside_the_definitions(
    client: Client, sample: Measure, folder: Path, label: str
) -> None:
    sample(1, 'water')
    store = FolderDefinitions(folder)
    batch = Batch(client, PLAIN, label=label, rows=rows(client), definitions=store)

    edit(batch, '1', thickness=2.0)

    assert store.load(rows_key(label), frame_of).loc['1', 'thickness'] == 2.0
    written = [p for p in folder.rglob('*.json') if p.name == 'rows.json']
    assert [p.parent.parent for p in written] == [folder / 'definitions' / 'labels']


def test_a_label_too_long_to_name_a_file_is_refused_when_the_batch_is_made(
    opened: Opened, folder: Path
) -> None:
    with opened() as (intake, _):
        with pytest.raises(ValueError, match='too long to name a file'):
            intake.manager.create(PLAIN.name, title='B', label='Å' * 120)

    assert FolderDefinitions(folder).labels() == []


def test_a_file_left_by_a_write_cut_off_is_not_read_and_not_removed(
    opened: Opened, made: str, folder: Path
) -> None:
    with opened() as (intake, _):
        edit(intake.manager.batch(made), '1', thickness=2.0)
    path = FolderDefinitions(folder).path(rows_key(made))
    # A reader cannot tell it from the file of a write of another process.
    left = path.parent / f'.{path.name}.x1y2z3'
    left.write_text('{"half')

    with opened() as (intake, _):
        assert intake.manager.batch(made).frame().loc['1', 'thickness'] == 2.0

    assert left.exists()


class Failing:
    """Definitions that hold nothing, and fail to keep anything."""

    def load[T](self, key: Key, read: Callable[[Any], T]) -> T | None:
        return None

    def save(self, key: Key, data: Any) -> None:
        raise NotKept('disk full')

    def delete(self, key: Key) -> None:
        raise NotKept('disk full')

    def labels(self) -> list[str]:
        return []


def test_a_change_that_cannot_be_kept_raises_and_changes_nothing(
    client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    batch = Batch(client, PLAIN, label='f', rows=rows(client), definitions=Failing())
    files = CorrectedFiles(client.files, definitions=Failing())
    before = batch.frame()

    with pytest.raises(NotKept, match='disk full'):
        edit(batch, '1', thickness=2.0)
    with pytest.raises(NotKept, match='disk full'):
        batch.settings = {'binning': {'bins': 3}}
    with pytest.raises(NotKept, match='disk full'):
        files.correct_each(list(client.files.list(Selector())), role='bad')

    pd.testing.assert_frame_equal(batch.frame(), before)
    assert batch.settings == {}
    assert [files.corrections(r) for r in client.files.list(Selector())] == [{}, {}]


def test_the_labels_are_those_with_a_part_kept_written_back_as_given(
    folder: Path,
) -> None:
    store = FolderDefinitions(folder)
    for label in ('b', 'a.x', 'auto/iofq'):
        store.save(rows_key(label), {})
    cut_off = store.path(rows_key('c')).parent
    cut_off.mkdir(parents=True)
    (cut_off / '.rows.json.k3j2h1g0').write_text('{')

    assert store.labels() == ['a.x', 'auto/iofq', 'b']


def test_no_labels_before_anything_is_kept(folder: Path) -> None:
    assert FolderDefinitions(folder).labels() == []


def test_a_part_deleted_is_gone_and_its_label_with_its_last_part(
    folder: Path,
) -> None:
    store = FolderDefinitions(folder)
    store.save(rows_key('a'), {})
    store.save(settings_key('a'), {'x': 1})

    store.delete(rows_key('a'))
    assert store.load(rows_key('a'), lambda data: data) is None
    assert store.labels() == ['a']

    store.delete(settings_key('a'))
    assert store.labels() == []


def test_deleting_a_part_never_kept_does_nothing(folder: Path) -> None:
    store = FolderDefinitions(folder)
    store.save(rows_key('a'), {})

    store.delete(rows_key('b'))
    store.delete(source_key('a'))

    assert store.labels() == ['a']


def test_a_header_is_kept_and_read_back(folder: Path) -> None:
    store = FolderDefinitions(folder)
    header = Header('Sample runs', 'those of May')

    store.save(header_key('a'), header_data(header))

    assert store.load(header_key('a'), header_of) == header
    assert store.load(header_key('b'), header_of) is None


def test_a_header_that_cannot_be_read_is_refused_naming_the_file_and_the_entry(
    folder: Path,
) -> None:
    store = FolderDefinitions(folder)
    store.save(header_key('a'), {'title': 3})

    with pytest.raises(ValueError, match=r'header\.json: title: '):
        store.load(header_key('a'), header_of)


def test_the_source_of_a_batch_is_kept_with_its_keys_sorted(folder: Path) -> None:
    store = FolderDefinitions(folder)
    made = FromSource('pairs', frozenset({'60340', '0342', '60339'}), follows=True)
    key = source_key('a')

    store.save(key, source_data(made))

    assert source_data(made)['considered'] == ['0342', '60339', '60340']
    assert store.load(key, source_of) == made
    store.save(key, {'source': 'pairs', 'considered': '60339', 'follows': False})
    with pytest.raises(ValueError, match=r'source\.json: considered: '):
        store.load(key, source_of)
    store.save(key, {'source': 'pairs', 'considered': ['60339']})
    with pytest.raises(ValueError, match=r'source\.json: follows: Field required'):
        store.load(key, source_of)


def test_a_folder_gets_the_format_with_its_first_definitions(folder: Path) -> None:
    store = FolderDefinitions(folder)
    store.save(rows_key('a'), {})

    format_file = folder / 'definitions' / 'format.json'
    assert json.loads(format_file.read_text()) == {'format': FORMAT}
    assert FolderDefinitions(folder).labels() == ['a']


@pytest.mark.parametrize(('kept', 'said'), [(None, 'none'), (FORMAT + 1, FORMAT + 1)])
def test_a_folder_of_definitions_of_another_format_is_refused(
    folder: Path, kept: int | None, said: object
) -> None:
    FolderDefinitions(folder).save(rows_key('a'), {})
    format_file = folder / 'definitions' / 'format.json'
    if kept is None:
        format_file.unlink()
    else:
        format_file.write_text(json.dumps({'format': kept}))

    with pytest.raises(ValueError, match=f'format {said}.*make a new folder'):
        FolderDefinitions(folder)
