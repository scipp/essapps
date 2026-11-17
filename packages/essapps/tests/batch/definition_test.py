# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The definition of batches kept in a folder workspace, opened twice."""

from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

import pytest

from ess.apps import Selector, file
from ess.apps.batch import (
    EMPTY,
    Batch,
    CorrectedFiles,
    FileInfo,
    FolderDefinitions,
    Row,
)
from ess.apps.batch.definition import Key, edits_key, settings_key
from ess.apps.batch.text import load_template
from ess.apps.folder import folder_files, open_folder
from ess.spec import Binding

from .toys import COLUMNS, PLAIN


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
        columns: Sequence[str] = COLUMNS, settings: dict[str, Any] | None = None
    ) -> Iterator[tuple[Batch, CorrectedFiles]]:
        store = FolderDefinitions(folder)
        files = CorrectedFiles(folder_files(folder), store=store)
        with open_folder(folder, folder_bindings, files=files) as client:
            batch = Batch(
                client,
                PLAIN,
                columns=columns,
                label='auto/plain',
                source=first_transmission,
                settings=settings,
                store=store,
            )
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


def test_edits_that_cannot_be_read_are_refused_naming_the_row(
    opened: Opened, folder: Path
) -> None:
    edits = {'edited': {'2': {'sample': {'@ref': 'nothing'}}}}
    FolderDefinitions(folder).save(edits_key('auto/plain'), edits)

    with (
        pytest.raises(ValueError, match=r'auto/plain in .*definitions: row 2: '),
        opened(),
    ):
        pass


def test_a_label_is_kept_in_a_folder_of_its_own(tmp_path: Path) -> None:
    store = FolderDefinitions(tmp_path)
    labels = ['auto/iofq', 'auto%2Fiofq', '..', '.', 'labels']
    paths = [store.path(edits_key(label)) for label in labels]

    assert len(set(paths)) == len(labels)
    assert all(p.parent.parent == tmp_path / 'definitions' / 'labels' for p in paths)


class Failing:
    """A store that holds nothing, and fails to keep anything."""

    def load(self, key: Key) -> Any | None:
        return None

    def save(self, key: Key, data: Any) -> None:
        raise OSError('disk full')


def test_a_change_that_cannot_be_kept_raises_and_changes_nothing(
    opened: Opened,
) -> None:
    with opened() as (batch, _):
        failing = Batch(
            batch.client, PLAIN, columns=COLUMNS, label='f', store=Failing()
        )
        failing.rows = failing.rows.rescan(batch.rows)

        with pytest.raises(OSError, match='disk full'):
            failing.rows = failing.rows.edit('1', {'thickness': 2.0})
        with pytest.raises(OSError, match='disk full'):
            failing.settings = {'binning': {'bins': 3}}

        assert failing.rows['1'].values == batch.rows['1'].values
        assert failing.settings == {}
