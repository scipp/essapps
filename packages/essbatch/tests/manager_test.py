# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The batches of a workspace, made, kept, changed and removed by a manager."""

import itertools
import re
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from ess.batch import (
    Batch,
    BatchManager,
    CorrectedFiles,
    DefinitionWriteError,
    FolderDefinitions,
    Header,
    Intake,
)
from ess.batch.definition import (
    HEADER,
    ROWS,
    SETTINGS,
    Key,
)

from ess.apps import Client, Stage, Workspace
from ess.apps.testing import FakeStore, StagedFunction
from ess.spec import function_binding

from .toys import (
    COLUMNS,
    PLAIN,
    SOURCES,
    STAGED,
    STAGED_NAMES,
    Measure,
    Refusing,
    edit,
    from_pairs,
    rows,
)


def _again(client: Client, definitions: FolderDefinitions) -> BatchManager:
    """The manager of the fixture made again on ``definitions``, as after a restart."""
    return BatchManager(client, definitions=definitions, staged=STAGED_NAMES)


def test_a_batch_made_is_listed_with_its_header(
    manager: BatchManager, client: Client, definitions: FolderDefinitions
) -> None:
    batch = manager.create(
        'plain', title='Morning', rows=rows(client), description='2 mm cells', label='b'
    )

    header = Header('Morning', '2 mm cells')
    assert batch.label == 'b'
    assert manager.headers() == {'b': header}
    assert _again(client, definitions).headers() == {'b': header}


def test_a_manager_made_again_on_the_definitions_hands_out_the_batch_as_it_was(
    manager: BatchManager,
    client: Client,
    definitions: FolderDefinitions,
    sample: Measure,
) -> None:
    sample(1, 'water')
    batch = manager.create('staged', title='Morning', rows=rows(client), label='b')
    batch.settings = {'binning': {'bins': 3}, 'thickness': 3.0}  # thickness: a column
    edit(batch, '1', thickness=2.0)
    edit(batch, 'mine', sample='run:1')

    again = _again(client, definitions).batch(batch.label)

    assert again.spec == STAGED
    assert again.parameter_columns == COLUMNS
    assert again.settings == batch.settings
    assert again.cells() == batch.cells()
    pd.testing.assert_frame_equal(again.frame(), batch.frame())


def test_the_columns_of_a_batch_are_the_parameter_columns_of_its_rows(
    manager: BatchManager, client: Client
) -> None:
    given = rows(client)[['thickness', 'transmission', 'name', 'sample']]
    chosen = manager.create('plain', title='Evening', rows=given)
    empty = manager.create('plain', title='Morning')

    assert chosen.parameter_columns == ('thickness', 'transmission', 'sample')
    assert empty.parameter_columns == ()


@pytest.mark.parametrize(
    ('table', 'said'),
    [
        (pd.DataFrame({'sample': ['run:1']}, index=[1]), 'a key is text'),
        (
            pd.DataFrame([['a', 'b']], columns=['sample', 'sample'], index=['1']),
            'twice',
        ),
        (pd.DataFrame({'thickness': ['thick']}, index=['1']), 'is not a number'),
    ],
)
def test_rows_that_a_batch_refuses_are_refused_and_nothing_is_kept(
    manager: BatchManager, definitions: FolderDefinitions, table: Any, said: str
) -> None:
    with pytest.raises(ValueError, match=said):
        manager.create('plain', title='Morning', rows=table)
    assert manager.headers() == {}
    assert definitions.labels() == []


def test_a_batch_whose_spec_is_no_longer_bound_is_refused(
    manager: BatchManager,
    files: CorrectedFiles,
    staged_binding: StagedFunction,
    definitions: FolderDefinitions,
) -> None:
    manager.create('plain', title='Morning', label='b')
    bind = [function_binding(STAGED, staged_binding.function)]
    workspace = Workspace(proposal='p1', files=files, bind=bind, store=FakeStore())
    with closing(workspace):
        client = Client(workspace, submitter='anna')

        with pytest.raises(ValueError, match='binds no spec plain'):
            _again(client, definitions).batch('b')


@pytest.fixture
def with_defaults(client: Client, definitions: FolderDefinitions) -> BatchManager:
    """A manager that registers settings for the spec ``plain``."""
    settings = {'thickness': 2.0, 'binning': {'bins': 3}}
    return BatchManager(client, definitions=definitions, settings={'plain': settings})


def test_a_batch_starts_with_the_settings_registered_for_its_spec(
    with_defaults: BatchManager, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    batch = with_defaults.create('plain', title='Morning', rows=rows(client))

    assert batch.settings == {'thickness': 2.0, 'binning': {'bins': 3}}
    assert batch.cells()['1']['thickness'] == 2.0  # the column's empty cell


def test_settings_given_to_create_replace_those_registered(
    with_defaults: BatchManager, client: Client
) -> None:
    settings = {'thickness': 3.0, 'offset': {'value': 1.0}}
    batch = with_defaults.create(
        'plain', title='Morning', rows=rows(client), settings=settings
    )

    assert batch.settings == settings


def test_a_batch_has_no_setting_at_a_column_of_its_fills(
    with_defaults: BatchManager, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    settings = {'thickness': 3.0, 'transmission': 'run:1001'}
    fills = ('sample', 'transmission')
    batch = with_defaults.create(
        'plain', title='Morning', rows=rows(client), fills=fills, settings=settings
    )

    assert batch.settings == {'thickness': 3.0}


def test_the_fills_of_a_batch_are_kept_as_the_blanks_of_its_settings(
    manager: BatchManager,
    client: Client,
    definitions: FolderDefinitions,
    sample: Measure,
) -> None:
    sample(1, 'water')
    fills = ('transmission', 'sample')
    manager.create('plain', title='Morning', rows=rows(client), fills=fills, label='b')

    kept = SETTINGS.load(definitions, 'b')

    assert kept.blanks == ('sample', 'transmission')
    assert _again(client, definitions).batch('b').fills == {'sample', 'transmission'}


def test_a_table_without_a_column_of_its_fills_is_refused_and_nothing_is_kept(
    manager: BatchManager, client: Client, definitions: FolderDefinitions
) -> None:
    table = rows(client).drop(columns='transmission')

    with pytest.raises(ValueError, match='fills transmission: a row waits'):
        manager.create('plain', title='M', rows=table, fills=['transmission'])
    assert definitions.labels() == []


def test_settings_registered_later_leave_the_settings_of_a_batch_made_before(
    with_defaults: BatchManager, client: Client, definitions: FolderDefinitions
) -> None:
    batch = with_defaults.create('plain', title='Morning', rows=rows(client))
    later = {'plain': {'binning': {'bins': 7}}}

    again = BatchManager(client, definitions=definitions, settings=later)

    assert again.batch(batch.label).settings == {
        'thickness': 2.0,
        'binning': {'bins': 3},
    }


def test_a_batch_is_of_the_highest_version_of_its_spec_that_the_workspace_binds(
    files: CorrectedFiles,
    staged_binding: StagedFunction,
    definitions: FolderDefinitions,
) -> None:
    newer = PLAIN.model_copy(update={'version': 2})
    bind = [function_binding(s, staged_binding.function) for s in (newer, PLAIN)]
    workspace = Workspace(proposal='p1', files=files, bind=bind, store=FakeStore())
    with closing(workspace):
        client = Client(workspace, submitter='anna')
        manager = BatchManager(client, definitions=definitions)

        batch = manager.create('plain', title='Morning')

        assert batch.spec == newer


def _with_definitions(client: Client, definitions: FolderDefinitions) -> None:
    batch = Batch(client, PLAIN, label='b', definitions=definitions)
    batch.settings = {}


def _with_records(client: Client, definitions: FolderDefinitions) -> None:
    batch = Batch(client, PLAIN, label='b', rows=rows(client))
    client.wait(batch.reduce(['1']))


@pytest.mark.parametrize('taken', [_with_definitions, _with_records])
def test_a_label_that_has_definitions_or_records_is_refused(
    manager: BatchManager,
    client: Client,
    definitions: FolderDefinitions,
    sample: Measure,
    taken: Callable[[Client, FolderDefinitions], None],
) -> None:
    sample(1, 'water')
    taken(client, definitions)

    with pytest.raises(ValueError, match='has definitions or records'):
        manager.create('plain', title='Morning', label='b')
    assert manager.headers() == {}


@pytest.mark.parametrize('label', ['auto/iofq', '.b', '..'])
def test_a_label_with_a_slash_or_a_leading_dot_is_refused(
    manager: BatchManager, label: str
) -> None:
    with pytest.raises(ValueError, match='a label is'):
        manager.create('plain', title='Morning', label=label)
    assert manager.headers() == {}


def test_a_spec_that_the_workspace_does_not_bind_is_refused(
    manager: BatchManager,
) -> None:
    with pytest.raises(ValueError, match='no spec iofq'):
        manager.create('iofq', title='Morning')
    assert manager.headers() == {}


def test_generated_labels_are_the_time_of_making_and_differ(
    manager: BatchManager,
) -> None:
    first = manager.create('plain', title='Morning').label
    second = manager.create('plain', title='Afternoon').label

    assert re.fullmatch(r'\d{8}-\d{6}', first)
    # made in the same second, or in the next
    assert second == f'{first}-2' or (
        re.fullmatch(r'\d{8}-\d{6}', second) and second > first
    )


def test_the_batch_of_a_label_is_one_object(manager: BatchManager) -> None:
    batch = manager.create('plain', title='Morning')

    assert manager.batch(batch.label) is batch


@pytest.mark.parametrize(
    'call',
    [
        lambda m: m.batch('b'),
        lambda m: m.change('b', title='Afternoon'),
        lambda m: m.remove('b'),
    ],
    ids=['batch', 'change', 'remove'],
)
def test_a_label_that_is_no_batch_is_refused(
    manager: BatchManager, call: Callable[[BatchManager], Any]
) -> None:
    with pytest.raises(KeyError):
        call(manager)


def test_a_title_and_description_changed_are_kept(
    manager: BatchManager, client: Client, definitions: FolderDefinitions
) -> None:
    label = manager.create('plain', title='Morning', description='a').label

    manager.change(label, title='Afternoon')
    manager.change(label, description='1 mm cells\n2 mm cells')

    header = Header('Afternoon', '1 mm cells\n2 mm cells')
    assert manager.headers() == {label: header}
    assert _again(client, definitions).headers() == {label: header}


def test_a_batch_removed_is_gone_with_its_definitions_and_its_records_stay(
    intake: Intake,
    client: Client,
    definitions: FolderDefinitions,
    sample: Measure,
) -> None:
    manager = intake.manager
    sample(1, 'water')
    label = from_pairs(intake, 'plain', title='Morning')  # source.json too
    batch = manager.batch(label)
    edit(batch, '1', thickness=2.0)
    client.wait(batch.reduce(['1']))

    manager.remove(label)

    assert manager.headers() == {}
    assert definitions.labels() == []
    assert [r.member for r in client.records(label=label)] == ['1']
    with pytest.raises(KeyError):
        manager.batch(label)


class Releases:
    """Delegates to a client and keeps what it releases."""

    def __init__(self, client: Client) -> None:
        self._client = client
        self.released: list[Any] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def release(self, what: Any) -> None:
        self.released.append(what)
        self._client.release(what)


def test_a_staged_batch_removed_releases_its_stage(
    client: Client, definitions: FolderDefinitions, sample: Measure
) -> None:
    releases = Releases(client)
    manager = BatchManager(
        releases,  # type: ignore[arg-type]
        definitions=definitions,
        staged=STAGED_NAMES,
    )
    sample(1, 'water')
    batch = manager.create('staged', title='Morning', rows=rows(client))
    client.wait(batch.reduce(['1']))

    manager.remove(batch.label)

    assert [type(s) for s in releases.released] == [Stage]


def test_a_batch_whose_header_is_refused_is_not_made(
    manager: BatchManager, client: Client, refusing: Refusing
) -> None:
    refusing.part = 'header'

    with pytest.raises(DefinitionWriteError):
        manager.create('plain', title='Morning', rows=rows(client), label='b')
    assert manager.headers() == {}
    assert refusing.labels() == []
    refusing.part = None
    assert manager.create('plain', title='Morning', label='c').label == 'c'


def test_the_header_of_a_batch_is_written_after_its_tables(
    manager: BatchManager, client: Client, refusing: Refusing, sample: Measure
) -> None:
    sample(1, 'water')
    manager.create('plain', title='Morning', rows=rows(client), label='b')

    assert refusing.saved == [ROWS.key('b'), SETTINGS.key('b'), HEADER.key('b')]


def test_a_setting_keeps_its_value_if_the_table_that_makes_it_a_column_is_refused(
    manager: BatchManager,
    client: Client,
    definitions: FolderDefinitions,
    refusing: Refusing,
    sample: Measure,
) -> None:
    sample(1, 'water')
    table = rows(client).drop(columns='thickness')
    batch = manager.create('plain', title='M', rows=table, settings={'thickness': 2.0})
    refusing.part = 'rows'

    with pytest.raises(DefinitionWriteError):
        batch.put(batch.frame().assign(thickness=3.0))

    assert _again(client, definitions).batch(batch.label).settings == {'thickness': 2.0}


def test_rows_reduced_are_not_outdated_in_a_manager_made_again(
    manager: BatchManager,
    client: Client,
    definitions: FolderDefinitions,
    sample: Measure,
) -> None:
    sample(1, 'water')
    batch = manager.create('plain', title='Morning', rows=rows(client))
    batch.settings = {'binning': {'bins': 3}}
    edit(batch, '1', thickness=1 / 3)
    client.wait(list(batch.reduce(['1']).values()))

    state = _again(client, definitions).batch(batch.label).state()['1']

    assert (state.status, state.outdated) == ('done', False)


@pytest.mark.parametrize('title', ['', '   '])
def test_an_empty_or_blank_title_is_refused(
    manager: BatchManager, definitions: FolderDefinitions, title: str
) -> None:
    with pytest.raises(ValueError, match='a title that is not empty'):
        manager.create('plain', title=title)
    assert definitions.labels() == []
    label = manager.create('plain', title='Morning').label

    with pytest.raises(ValueError, match='a title that is not empty'):
        manager.change(label, title=title)
    assert manager.headers()[label].title == 'Morning'


def test_a_batch_whose_removal_is_refused_is_kept(
    manager: BatchManager, refusing: Refusing
) -> None:
    batch = manager.create('plain', title='Morning', label='b')
    refusing.part = 'header'

    with pytest.raises(DefinitionWriteError):
        manager.remove('b')
    assert list(manager.headers()) == ['b']
    assert manager.batch('b') is batch


def test_a_batch_made_without_rows_takes_the_rows_put(
    manager: BatchManager, client: Client, definitions: FolderDefinitions
) -> None:
    batch = manager.create('plain', title='Morning')
    assert batch.frame().empty

    batch.put(pd.DataFrame({'sample': ['run:2']}, index=['mine']))

    again = _again(client, definitions).batch(batch.label)
    assert list(again.frame().index) == ['mine']


@pytest.mark.parametrize(
    'change',
    [
        lambda m: m.create('plain', title='C', label='c'),
        lambda m: m.remove('b'),
        lambda m: m.change('b', title='Other'),
    ],
    ids=['create', 'remove', 'change'],
)
def test_the_version_moves_at_each_change_of_the_headers(
    manager: BatchManager, change: Callable[[BatchManager], object]
) -> None:
    manager.create('plain', title='B', label='b')
    before = manager.revision

    change(manager)

    assert manager.revision != before


# A create or remove cut off


class Stopping:
    """
    Definitions that refuse the ``at``-th write, a save or a delete, and every
    write after it, as a process that stops there; ``writes`` counts them.
    """

    def __init__(self, definitions: FolderDefinitions, at: int) -> None:
        self.at = at
        self.writes = 0
        self._definitions = definitions

    def load[T](self, key: Key, read: Callable[[Any], T]) -> T | None:
        return self._definitions.load(key, read)

    def save(self, key: Key, data: Any) -> None:
        self._write()
        self._definitions.save(key, data)

    def delete(self, key: Key) -> None:
        self._write()
        self._definitions.delete(key)

    def labels(self) -> list[str]:
        return self._definitions.labels()

    def _write(self) -> None:
        self.writes += 1
        if self.writes >= self.at:
            raise DefinitionWriteError('stopped')


def _create(intake: Intake, *, follow: bool) -> None:
    """Make the batch ``a`` with every table: settings, rows and source."""
    intake.create(
        'plain',
        title='A',
        source='pairs',
        settings={'thickness': 2.0},
        label='a',
        follow=follow,
    )


def _intake(client: Client, definitions: Any) -> Intake:
    """An intake of a manager made again on ``definitions``, as after a restart."""
    return Intake(BatchManager(client, definitions=definitions), sources=SOURCES)


def _made_again(
    client: Client, definitions: FolderDefinitions, *, follow: bool
) -> None:
    """
    As after a restart: remove what is kept of ``a``, if anything, then make
    it again, following if ``follow``; with ``follow``, it is the batch that
    follows, and else none follows.
    """
    again = _intake(client, definitions)
    if 'a' in definitions.labels():
        again.manager.remove('a')
    _create(again, follow=follow)
    assert 'a' in again.manager.headers()
    assert _intake(client, definitions).following == ('a' if follow else None)


def test_a_create_cut_off_at_any_write_leaves_what_remove_removes(
    client: Client, sample: Measure, tmp_path: Path
) -> None:
    sample(1, 'water')
    for at in itertools.count(1):
        definitions = FolderDefinitions(tmp_path / str(at))
        _intake(client, definitions).create(
            'plain', title='Z', source='pairs', label='z', follow=True
        )
        stopping = Stopping(definitions, at)
        try:
            _create(_intake(client, stopping), follow=True)
        except DefinitionWriteError:
            # No batch a, or one made without a source; z follows still, or
            # none does if the cut came after Follow on z ended.
            again = _intake(client, definitions)
            assert set(again.manager.headers()) - {'z'} <= {'a'}
            assert again.following in ('z', None)
            _made_again(client, definitions, follow=True)
        else:
            break
    assert at > 5  # rows, settings, header, source of z, source of a


def test_a_remove_cut_off_at_any_write_leaves_what_remove_removes(
    client: Client, sample: Measure, tmp_path: Path
) -> None:
    sample(1, 'water')
    for at in itertools.count(1):
        definitions = FolderDefinitions(tmp_path / str(at))
        _create(_intake(client, definitions), follow=True)
        stopping = Stopping(definitions, at)
        manager = BatchManager(client, definitions=stopping)
        try:
            manager.remove('a')
        except DefinitionWriteError:
            _made_again(client, definitions, follow=False)
        else:
            break
    assert at > 4  # header, source, rows, settings
