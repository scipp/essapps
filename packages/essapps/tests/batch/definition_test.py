# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The definition of batches kept in a folder workspace, opened twice."""

import re
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

import pytest

from ess.apps import Client, Selector, SubmitError, file
from ess.apps.batch import (
    EMPTY,
    Batch,
    CorrectedFiles,
    FileInfo,
    FolderDefinitions,
    Row,
)
from ess.apps.batch.definition import CORRECTIONS, Key, NotKept, edits_key, settings_key
from ess.apps.batch.text import load_template
from ess.apps.folder import folder_files, open_folder
from ess.spec import Binding

from .toys import COLUMNS, PLAIN, Measure, pair


def first_transmission(files: Sequence[FileInfo]) -> list[Row]:
    """A row per sample run, each with the first transmission run listed."""
    transmissions = [f.ref for f in files if f.metadata['role'] == 'transmission']
    return [
        Row(str(f.metadata['run']), {'sample': f.ref, 'transmission': transmissions[0]})
        for f in files
        if f.metadata['role'] == 'sample'
    ]


type Opened = Callable[..., AbstractContextManager[tuple[Batch, CorrectedFiles]]]


@pytest.fixture
def opened(folder: Path, folder_bindings: list[Binding]) -> Opened:
    """Opens the folder, with a scanned batch, and the file source, on its store."""

    @contextmanager
    def opened(
        columns: Sequence[str] = COLUMNS,
        settings: dict[str, Any] | None = None,
        label: str = 'auto/plain',
        scan: bool = True,
    ) -> Iterator[tuple[Batch, CorrectedFiles]]:
        store = FolderDefinitions(folder)
        files = CorrectedFiles(folder_files(folder), definitions=store)
        with open_folder(folder, folder_bindings, files=files) as client:
            batch = Batch(
                client,
                PLAIN,
                columns=columns,
                label=label,
                source=first_transmission,
                settings=settings,
                definitions=store,
            )
            if scan:
                batch.scan()
            yield batch, files

    return opened


def shown(batch: Batch) -> list[tuple[str, dict[str, Any], dict[str, str]]]:
    return [(r.key, dict(r.values), dict(r.info)) for r in batch.rows]


def test_a_batch_opened_again_has_the_edits_it_had(opened: Opened) -> None:
    with opened() as (batch, _):
        batch.rows = (
            batch.rows.edit('1', {'thickness': 2.0})
            .edit('2', {'transmission': EMPTY})
            .remove('3')
            .add(Row('mine', {'sample': file(run=2)}, {'name': 'mine'}))
        )
        before = shown(batch)

    with opened() as (batch, _):
        assert shown(batch) == before


def test_a_cell_from_the_scan_follows_the_scan_after_a_restart(
    opened: Opened, folder: Path
) -> None:
    with opened() as (batch, _):
        batch.rows = batch.rows.edit('1', {'thickness': 2.0})
    files = folder / 'files.csv'
    files.write_text(files.read_text().replace('run-1001.json,1001,transmission', ''))

    with opened() as (batch, _):
        row = batch.rows['1'].values

    assert (row['transmission'].file, row['thickness']) == ('path:run-1002.json', 2.0)


def test_the_corrections_of_the_files_are_there_after_a_restart(
    opened: Opened,
) -> None:
    with opened() as (_, files):
        (bad,) = files.list(Selector(role='bad'))
        files.correct(bad, role='sample')

    with opened() as (batch, files):
        assert files.corrections(bad) == {'role': 'sample'}
        assert '4' in batch.rows


def test_the_settings_are_there_after_a_restart_in_place_of_those_given(
    opened: Opened,
) -> None:
    with opened(settings={'binning': {'bins': 5}}) as (batch, _):
        batch.settings = {'binning': {'bins': 3}}

    with opened(settings={'binning': {'bins': 5}}) as (batch, _):
        assert batch.settings == {'binning': {'bins': 3}}


def test_the_settings_kept_are_a_template_that_the_command_reads(
    opened: Opened, folder: Path
) -> None:
    with opened() as (batch, _):
        batch.settings = {'binning': {'bins': 3}}
        template = batch.template()

    path = FolderDefinitions(folder).path(settings_key('auto/plain'))

    assert load_template(path) == template


def test_an_edit_at_a_column_that_is_gone_makes_its_row_incomplete(
    opened: Opened,
) -> None:
    with opened() as (batch, _):
        batch.rows = batch.rows.edit('1', {'thickness': 2.0})

    with opened(columns=('sample', 'transmission')) as (batch, _):
        problems = batch.state()['1'].problems

    assert [(e.loc, e.msg) for e in problems] == [(('thickness',), 'not a column')]


@pytest.mark.parametrize(
    ('edits', 'entry'),
    [
        ({'edited': {'2': {'sample': {'@ref': 'nothing'}}}}, 'row 2: '),
        ({'removed': '3'}, 'removed: Input should be a valid list'),
        ({'cleared': {'1': 'thickness'}}, 'cleared.1: Input should be a valid list'),
        ({'added': [{'key': 'x', 'values': {}}]}, 'added.0.info: Field required'),
        (['not', 'a', 'table'], 'the whole: '),
    ],
)
def test_edits_that_cannot_be_read_are_refused_naming_the_file_and_the_entry(
    opened: Opened, folder: Path, edits: Any, entry: str
) -> None:
    FolderDefinitions(folder).save(edits_key('auto/plain'), edits)

    with (
        pytest.raises(
            ValueError, match=rf'auto%2Fplain/edits\.json: {re.escape(entry)}'
        ),
        opened(),
    ):
        pass


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


def test_settings_of_a_spec_of_another_name_are_refused_naming_both(
    opened: Opened, folder: Path
) -> None:
    kept = {'spec': {'name': 'other', 'version': 9}, 'params': {}}
    FolderDefinitions(folder).save(settings_key('auto/plain'), kept)

    with (
        pytest.raises(
            ValueError,
            match=r'settings\.json: settings of the spec other version 9, not of plain',
        ),
        opened(),
    ):
        pass


def test_settings_of_another_version_are_taken_as_they_are(
    opened: Opened, folder: Path
) -> None:
    kept = {'spec': {'name': 'plain', 'version': 9}, 'params': {'gone': 1}}
    FolderDefinitions(folder).save(settings_key('auto/plain'), kept)

    with opened() as (batch, _):
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
    paths = [store.path(edits_key(label)) for label in labels]

    assert len(set(paths)) == len(labels)
    assert all(p.parent.parent == tmp_path / 'definitions' / 'labels' for p in paths)


@pytest.mark.parametrize('label', ['../..', 'a/../b', '%2E%2E', 'x.json'])
def test_a_label_that_looks_like_a_path_keeps_its_edits_inside_the_definitions(
    opened: Opened, folder: Path, label: str
) -> None:
    with opened(label=label) as (batch, _):
        batch.rows = batch.rows.edit('1', {'thickness': 2.0})

    with opened(label=label) as (batch, _):
        assert batch.rows['1'].values['thickness'] == 2.0
    written = [p for p in folder.rglob('*.json') if p.name == 'edits.json']
    assert [p.parent.parent for p in written] == [folder / 'definitions' / 'labels']


def test_a_label_too_long_to_name_a_file_is_refused_when_the_batch_is_made(
    opened: Opened,
) -> None:
    with (
        pytest.raises(ValueError, match='too long to name a file'),
        opened(label='Å' * 120),
    ):
        pass


def test_a_file_left_by_a_write_cut_off_is_removed_when_read(
    opened: Opened, folder: Path
) -> None:
    with opened() as (batch, _):
        batch.rows = batch.rows.edit('1', {'thickness': 2.0})
    path = FolderDefinitions(folder).path(edits_key('auto/plain'))
    left = path.parent / f'.{path.name}.x1y2z3'
    left.write_text('{"half')

    with opened() as (batch, _):
        assert batch.rows['1'].values['thickness'] == 2.0

    assert not left.exists()


def test_a_cell_cleared_stays_empty_after_a_restart_whatever_the_scan_gives(
    opened: Opened, folder: Path
) -> None:
    with opened() as (batch, _):
        batch.rows = batch.rows.edit('1', {'transmission': EMPTY})
    files = folder / 'files.csv'
    files.write_text(files.read_text().replace('run-1001.json,1001,transmission', ''))

    with opened() as (batch, _):
        assert 'transmission' not in batch.rows['1'].values
        assert batch.rows['2'].values['transmission'].file == 'path:run-1002.json'


def test_a_table_put_brings_back_a_row_removed_and_is_kept(opened: Opened) -> None:
    with opened() as (batch, _):
        row = batch.rows['3']
        batch.rows = batch.rows.remove('3')
        batch.put([Row('3', {**row.values, 'thickness': 4.0})])

    with opened() as (batch, _):
        assert batch.rows['3'].values == {**row.values, 'thickness': 4.0}
        assert '3' not in batch.rows.added


def test_a_table_put_before_a_scan_is_refused(opened: Opened) -> None:
    with opened(scan=False) as (batch, _):
        with pytest.raises(ValueError, match='scan the files first'):
            batch.put([Row('1', {'thickness': 4.0})])


class Failing:
    """Definitions that hold nothing, and fail to keep anything."""

    def load[T](self, key: Key, read: Callable[[Any], T]) -> T | None:
        return None

    def save(self, key: Key, data: Any) -> None:
        raise NotKept('disk full')


def test_a_change_that_cannot_be_kept_raises_and_changes_nothing(
    client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    batch = Batch(
        client, PLAIN, columns=COLUMNS, label='f', source=pair, definitions=Failing()
    )
    files = CorrectedFiles(client.files, definitions=Failing())
    batch.scan()  # writes nothing
    before = batch.rows

    with pytest.raises(NotKept, match='disk full'):
        batch.rows = batch.rows.edit('1', {'thickness': 2.0})
    with pytest.raises(NotKept, match='disk full'):
        batch.settings = {'binning': {'bins': 3}}
    with pytest.raises(NotKept, match='disk full'):
        files.correct_each(list(client.files.list(Selector())), role='bad')

    assert batch.rows is before
    assert batch.settings == {}
    assert [files.corrections(r) for r in client.files.list(Selector())] == [{}, {}]
