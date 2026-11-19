# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The batches of a workspace, made, kept, changed and removed by a manager."""

import re
from collections.abc import Callable
from contextlib import closing
from typing import Any

import pytest

from ess.apps import Client, Stage, Workspace
from ess.apps.batch import (
    Batch,
    BatchManager,
    CorrectedFiles,
    Defaults,
    FolderDefinitions,
    Header,
    NotKept,
    Row,
    Source,
)
from ess.apps.testing import FakeStore, StagedFunction
from ess.spec import function_binding

from .toys import (
    COLUMNS,
    DEFAULTS,
    FIND,
    PLAIN,
    SOURCES,
    STAGED,
    Measure,
    Refusing,
    pair,
)


def _again(client: Client, definitions: FolderDefinitions) -> BatchManager:
    """The manager of the fixture made again on ``definitions``, as after a restart."""
    return BatchManager(
        client, sources=SOURCES, definitions=definitions, defaults=DEFAULTS
    )


def test_a_batch_made_is_listed_with_its_header(
    manager: BatchManager, client: Client, definitions: FolderDefinitions
) -> None:
    label = manager.create(
        'plain', title='Morning', source='pairs', description='2 mm cells', label='b'
    )

    header = Header('Morning', '2 mm cells', 'pairs')
    assert label == 'b'
    assert manager.batches() == {'b': header}
    assert manager.source('b') is SOURCES['pairs']
    assert _again(client, definitions).batches() == {'b': header}


def test_a_batch_made_keeps_its_spec_and_columns_before_any_settings_are_applied(
    manager: BatchManager, client: Client, definitions: FolderDefinitions
) -> None:
    label = manager.create(
        'plain', title='Morning', source='pairs', columns=('thickness',)
    )

    again = _again(client, definitions).batch(label)
    assert again.spec == PLAIN
    assert again.columns == ('sample', 'transmission', 'thickness')


def test_a_manager_made_again_on_the_definitions_hands_out_the_batch_as_it_was(
    manager: BatchManager,
    client: Client,
    definitions: FolderDefinitions,
    sample: Measure,
) -> None:
    water = sample(1, 'water')
    label = manager.create(
        'staged', title='Morning', source='pairs', columns=('thickness',), label='b'
    )
    batch = manager.batch(label)
    batch.settings = {'binning': {'bins': 3}}
    batch.rows = batch.rows.edit('1', {'thickness': 2.0})
    batch.put([Row('mine', {'sample': water})])

    again = _again(client, definitions).batch(label)

    assert again.spec == STAGED
    assert again.columns == ('sample', 'transmission', 'thickness')
    assert again.settings == batch.settings
    assert list(again.rows) == list(batch.rows)


def test_the_columns_of_a_batch_are_those_of_its_source_and_those_the_person_adds(
    manager: BatchManager,
) -> None:
    label = manager.create(
        'plain', title='Morning', source='pairs', columns=('thickness', 'sample')
    )

    assert manager.batch(label).columns == ('sample', 'transmission', 'thickness')


@pytest.fixture
def with_defaults(client: Client, definitions: FolderDefinitions) -> BatchManager:
    """A manager that registers settings for the spec ``plain``."""
    settings = {'thickness': 2.0, 'binning': {'bins': 3}}
    return BatchManager(
        client,
        sources=SOURCES,
        definitions=definitions,
        defaults={'plain': Defaults(settings=settings)},
    )


def test_a_batch_starts_with_the_settings_registered_for_its_spec_but_its_columns(
    with_defaults: BatchManager,
) -> None:
    label = with_defaults.create('plain', title='Morning', columns=COLUMNS)

    assert with_defaults.batch(label).settings == {'binning': {'bins': 3}}


def test_settings_given_to_create_replace_those_registered_but_its_columns(
    with_defaults: BatchManager,
) -> None:
    settings = {'thickness': 3.0, 'offset': {'value': 1.0}}
    label = with_defaults.create(
        'plain', title='Morning', columns=COLUMNS, settings=settings
    )

    assert with_defaults.batch(label).settings == {'offset': {'value': 1.0}}


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
        manager = BatchManager(client, sources=SOURCES, definitions=definitions)

        label = manager.create('plain', title='Morning', columns=COLUMNS)

        assert manager.batch(label).spec == newer


def _with_definitions(client: Client, definitions: FolderDefinitions) -> None:
    batch = Batch(client, PLAIN, columns=COLUMNS, label='b', definitions=definitions)
    batch.settings = {}


def _with_records(client: Client, definitions: FolderDefinitions) -> None:
    batch = Batch(client, PLAIN, columns=COLUMNS, label='b', source=pair)
    batch.scan()
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
        manager.create('plain', title='Morning', columns=COLUMNS, label='b')
    assert manager.batches() == {}


@pytest.mark.parametrize('label', ['auto/iofq', '.b', '..'])
def test_a_label_with_a_slash_or_a_leading_dot_is_refused(
    manager: BatchManager, label: str
) -> None:
    with pytest.raises(ValueError, match='a label is'):
        manager.create('plain', title='Morning', columns=COLUMNS, label=label)
    assert manager.batches() == {}


def test_a_source_that_is_not_registered_is_refused(manager: BatchManager) -> None:
    with pytest.raises(KeyError, match='runs'):
        manager.create('plain', title='Morning', source='runs')
    assert manager.batches() == {}


def test_a_source_whose_columns_the_spec_lacks_is_refused(
    manager: BatchManager,
) -> None:
    with pytest.raises(ValueError, match=r"Find has no parameters \['transmission'\]"):
        manager.create(FIND.name, title='Morning', source='pairs')
    assert manager.batches() == {}


def test_a_spec_that_the_workspace_does_not_bind_is_refused(
    manager: BatchManager,
) -> None:
    with pytest.raises(KeyError, match='no spec iofq'):
        manager.create('iofq', title='Morning')
    assert manager.batches() == {}


def test_generated_labels_are_the_time_of_making_and_differ(
    manager: BatchManager,
) -> None:
    first = manager.create('plain', title='Morning', columns=COLUMNS)
    second = manager.create('plain', title='Afternoon', columns=COLUMNS)

    assert re.fullmatch(r'\d{8}-\d{6}', first)
    # made in the same second, or in the next
    assert second == f'{first}-2' or (
        re.fullmatch(r'\d{8}-\d{6}', second) and second > first
    )


def test_the_batch_of_a_label_is_one_object(manager: BatchManager) -> None:
    label = manager.create('plain', title='Morning', source='pairs')

    assert manager.batch(label) is manager.batch(label)


def test_a_batch_whose_source_fills_a_column_it_lacks_is_refused(
    client: Client, definitions: FolderDefinitions
) -> None:
    narrow = {'pairs': Source(pair, columns=('sample',))}
    manager = BatchManager(client, sources=narrow, definitions=definitions)
    manager.create('plain', title='Morning', source='pairs', label='b')

    with pytest.raises(ValueError, match=r"b: its source fills \['transmission'\]"):
        _again(client, definitions).batch('b')


@pytest.mark.parametrize(
    'call',
    [
        lambda m: m.batch('b'),
        lambda m: m.source('b'),
        lambda m: m.edit('b', title='Afternoon'),
        lambda m: m.remove('b'),
    ],
    ids=['batch', 'source', 'edit', 'remove'],
)
def test_a_label_that_is_no_batch_is_refused(
    manager: BatchManager, call: Callable[[BatchManager], Any]
) -> None:
    with pytest.raises(KeyError):
        call(manager)


def test_a_title_and_description_changed_are_kept(
    manager: BatchManager, client: Client, definitions: FolderDefinitions
) -> None:
    label = manager.create('plain', title='Morning', source='pairs', description='a')

    header = manager.edit(label, title='Afternoon')
    header = manager.edit(label, description='b')

    assert header == Header('Afternoon', 'b', 'pairs')
    assert _again(client, definitions).batches() == {label: header}


def test_a_batch_removed_is_gone_with_its_definitions_and_its_records_stay(
    manager: BatchManager,
    client: Client,
    definitions: FolderDefinitions,
    sample: Measure,
) -> None:
    sample(1, 'water')
    label = manager.create('plain', title='Morning', source='pairs', columns=COLUMNS)
    batch = manager.batch(label)
    batch.rows = batch.rows.edit('1', {'thickness': 2.0})
    client.wait(batch.reduce(['1']))

    manager.remove(label)

    assert manager.batches() == {}
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
        sources=SOURCES,
        definitions=definitions,
        defaults=DEFAULTS,
    )
    sample(1, 'water')
    label = manager.create('staged', title='Morning', source='pairs')
    client.wait(manager.batch(label).reduce(['1']))

    manager.remove(label)

    assert [type(s) for s in releases.released] == [Stage]


def test_a_batch_whose_header_is_refused_is_not_made_and_leaves_its_label_free(
    manager: BatchManager, refusing: Refusing, definitions: FolderDefinitions
) -> None:
    refusing.part = 'header'

    with pytest.raises(NotKept):
        manager.create('plain', title='Morning', columns=COLUMNS, label='b')
    assert manager.batches() == {}
    assert definitions.labels() == []
    refusing.part = None
    assert manager.create('plain', title='Morning', columns=COLUMNS, label='b') == 'b'


def test_a_header_change_that_is_refused_changes_nothing(
    manager: BatchManager, refusing: Refusing
) -> None:
    manager.create('plain', title='Morning', columns=COLUMNS, label='b')
    refusing.part = 'header'

    with pytest.raises(NotKept):
        manager.edit('b', title='Afternoon')
    assert manager.batches() == {'b': Header('Morning')}


def test_a_batch_whose_removal_is_refused_after_its_header_is_gone_keeps_its_label(
    manager: BatchManager,
    refusing: Refusing,
    client: Client,
    definitions: FolderDefinitions,
) -> None:
    manager.create('plain', title='Morning', columns=COLUMNS, label='b')
    refusing.part = 'settings'

    with pytest.raises(NotKept):
        manager.remove('b')
    refusing.part = None
    assert manager.batches() == {}
    assert _again(client, definitions).batches() == {}
    with pytest.raises(ValueError, match='has definitions or records'):
        manager.create('plain', title='Morning', columns=COLUMNS, label='b')


def test_a_batch_without_a_source_has_only_rows_added_by_hand(
    manager: BatchManager, sample: Measure
) -> None:
    sample(1, 'water')
    label = manager.create('plain', title='Morning', columns=COLUMNS)
    batch = manager.batch(label)

    assert manager.source(label) is None
    assert list(batch.rows) == []
    batch.put([Row('mine', {'sample': sample(2, 'oil')})])
    assert [r.key for r in manager.batch(label).rows] == ['mine']
