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
    Counts,
    Defaults,
    FolderDefinitions,
    Header,
    NotKept,
    Row,
    Scope,
    Source,
    status_texts,
)
from ess.apps.batch.definition import settings_key
from ess.apps.testing import FakeStore, StagedFunction
from ess.spec import function_binding

from .toys import (
    COLUMNS,
    DEFAULTS,
    FIND,
    PLAIN,
    SOURCES,
    STAGED,
    FindParams,
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
        'plain',
        title='Morning',
        source='pairs',
        columns=COLUMNS,
        description='2 mm cells',
        label='b',
    )

    header = Header(
        spec='plain',
        columns=COLUMNS,
        source='pairs',
        title='Morning',
        description='2 mm cells',
    )
    assert label == 'b'
    assert manager.headers() == {'b': header}
    assert manager.source('b') is SOURCES['pairs']
    assert _again(client, definitions).headers() == {'b': header}


def test_a_manager_made_again_on_the_definitions_hands_out_the_batch_as_it_was(
    manager: BatchManager,
    client: Client,
    definitions: FolderDefinitions,
    sample: Measure,
) -> None:
    water = sample(1, 'water')
    label = manager.create(
        'staged', title='Morning', source='pairs', columns=COLUMNS, label='b'
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


def test_the_columns_of_a_batch_are_those_given_else_those_its_source_fills(
    manager: BatchManager,
) -> None:
    given = ('thickness', 'transmission', 'sample')
    filled = manager.create('plain', title='Morning', source='pairs')
    chosen = manager.create('plain', title='Evening', source='pairs', columns=given)

    assert manager.headers()[filled].columns == ('sample', 'transmission')
    assert manager.batch(filled).columns == ('sample', 'transmission')
    assert manager.headers()[chosen].columns == given
    assert manager.batch(chosen).columns == given


def test_columns_that_lack_one_the_source_fills_are_refused(
    manager: BatchManager, definitions: FolderDefinitions
) -> None:
    with pytest.raises(ValueError, match=r"pairs fills \['transmission'\]"):
        manager.create(
            'plain', title='Morning', source='pairs', columns=('sample', 'thickness')
        )
    assert manager.headers() == {}
    assert definitions.labels() == []


def test_a_column_named_twice_is_refused(
    manager: BatchManager, definitions: FolderDefinitions
) -> None:
    with pytest.raises(ValueError, match='a column is named twice'):
        manager.create(
            'plain', title='Morning', columns=('sample', 'transmission', 'sample')
        )
    assert definitions.labels() == []


def test_settings_kept_at_a_column_of_the_batch_are_refused_naming_the_file(
    manager: BatchManager, client: Client, definitions: FolderDefinitions
) -> None:
    manager.create('plain', title='Morning', source='pairs', columns=COLUMNS, label='b')
    narrow = Batch(
        client,
        PLAIN,
        columns=('sample', 'transmission'),
        label='b',
        definitions=definitions,
    )
    narrow.settings = {'thickness': 2.0}

    with pytest.raises(
        ValueError, match=r"b/settings\.json: \['thickness'\]: columns of the batch"
    ):
        _again(client, definitions).batch('b')


@pytest.mark.parametrize(
    ('bound', 'said'),
    [
        ('another spec', 'binds no spec plain'),
        (
            'fewer parameters',
            r"Plain has no parameters \['thickness', 'transmission'\]",
        ),
    ],
)
def test_a_batch_whose_spec_is_no_longer_bound_as_it_was_made_is_refused(
    manager: BatchManager,
    files: CorrectedFiles,
    staged_binding: StagedFunction,
    definitions: FolderDefinitions,
    bound: str,
    said: str,
) -> None:
    manager.create('plain', title='Morning', columns=COLUMNS, label='b')
    spec = (
        STAGED
        if bound == 'another spec'
        else PLAIN.model_copy(update={'version': 2, 'params': FindParams})
    )
    bind = [function_binding(spec, staged_binding.function)]
    workspace = Workspace(proposal='p1', files=files, bind=bind, store=FakeStore())
    with closing(workspace):
        client = Client(workspace, submitter='anna')

        with pytest.raises(KeyError, match=said):
            _again(client, definitions).batch('b')


def test_a_second_batch_on_the_label_with_other_columns_changes_no_header(
    manager: BatchManager, client: Client, definitions: FolderDefinitions
) -> None:
    manager.create('plain', title='Morning', source='pairs', label='b')
    other = Batch(client, PLAIN, columns=COLUMNS, label='b', definitions=definitions)
    other.settings = {}

    again = _again(client, definitions)

    assert again.headers()['b'].columns == ('sample', 'transmission')
    assert again.batch('b').columns == ('sample', 'transmission')


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


def test_settings_registered_later_leave_the_settings_of_a_batch_made_before(
    with_defaults: BatchManager, client: Client, definitions: FolderDefinitions
) -> None:
    label = with_defaults.create('plain', title='Morning', columns=COLUMNS)
    later = {'plain': Defaults(settings={'binning': {'bins': 7}})}

    again = BatchManager(
        client, sources=SOURCES, definitions=definitions, defaults=later
    )

    assert again.batch(label).settings == {'binning': {'bins': 3}}


def test_a_batch_whose_settings_are_not_kept_has_the_settings_registered(
    with_defaults: BatchManager, definitions: FolderDefinitions
) -> None:
    label = with_defaults.create(
        'plain', title='Morning', columns=COLUMNS, settings={'offset': {'value': 1.0}}
    )

    definitions.delete(settings_key(label))

    assert with_defaults.batch(label).settings == {'binning': {'bins': 3}}


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
    assert manager.headers() == {}


@pytest.mark.parametrize('label', ['auto/iofq', '.b', '..'])
def test_a_label_with_a_slash_or_a_leading_dot_is_refused(
    manager: BatchManager, label: str
) -> None:
    with pytest.raises(ValueError, match='a label is'):
        manager.create('plain', title='Morning', columns=COLUMNS, label=label)
    assert manager.headers() == {}


def test_a_source_that_is_not_registered_is_refused(manager: BatchManager) -> None:
    with pytest.raises(KeyError, match='runs'):
        manager.create('plain', title='Morning', source='runs')
    assert manager.headers() == {}


def test_a_source_whose_columns_the_spec_lacks_is_refused(
    manager: BatchManager,
) -> None:
    with pytest.raises(ValueError, match=r"Find has no parameters \['transmission'\]"):
        manager.create(FIND.name, title='Morning', source='pairs')
    assert manager.headers() == {}


def test_a_spec_that_the_workspace_does_not_bind_is_refused(
    manager: BatchManager,
) -> None:
    with pytest.raises(KeyError, match='no spec iofq'):
        manager.create('iofq', title='Morning')
    assert manager.headers() == {}


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
    narrow = {'pairs': Source(pair, fills=('sample',))}
    manager = BatchManager(client, sources=narrow, definitions=definitions)
    manager.create('plain', title='Morning', source='pairs', label='b')

    with pytest.raises(ValueError, match=r"b: its source fills \['transmission'\]"):
        _again(client, definitions).batch('b')


@pytest.mark.parametrize(
    'call',
    [
        lambda m: m.batch('b'),
        lambda m: m.source('b'),
        lambda m: m.change('b', title='Afternoon'),
        lambda m: m.remove('b'),
    ],
    ids=['batch', 'source', 'change', 'remove'],
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

    header = manager.change(label, title='Afternoon')
    header = manager.change(label, description='b')

    assert header == Header(
        spec='plain',
        columns=('sample', 'transmission'),
        source='pairs',
        title='Afternoon',
        description='b',
    )
    assert _again(client, definitions).headers() == {label: header}


def test_a_scope_given_at_making_and_changed_is_kept(
    manager: BatchManager,
    client: Client,
    definitions: FolderDefinitions,
    sample: Measure,
) -> None:
    for run, name in enumerate(['water', 'oil', 'sds'], start=1):
        sample(run, name)
    label = manager.create(
        'plain', title='Morning', source='pairs', scope=Scope(start=2)
    )
    batch = manager.batch(label)
    assert [row.key for row in batch.rows] == ['2', '3']

    manager.change(label, scope=Scope(start=2, stop=3))

    assert [row.key for row in batch.rows] == ['2']
    again = _again(client, definitions)
    assert again.headers()[label].scope == Scope(start=2, stop=3)
    assert [row.key for row in again.batch(label).rows] == ['2']


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
        sources=SOURCES,
        definitions=definitions,
        defaults=DEFAULTS,
    )
    sample(1, 'water')
    label = manager.create('staged', title='Morning', source='pairs')
    client.wait(manager.batch(label).reduce(['1']))

    manager.remove(label)

    assert [type(s) for s in releases.released] == [Stage]


@pytest.mark.parametrize('part', ['settings', 'header'])
def test_a_batch_whose_settings_or_header_is_refused_is_not_made_and_its_label_free(
    manager: BatchManager,
    refusing: Refusing,
    definitions: FolderDefinitions,
    part: str,
) -> None:
    refusing.part = part

    with pytest.raises(NotKept):
        manager.create('plain', title='Morning', columns=COLUMNS, label='b')
    assert manager.headers() == {}
    assert definitions.labels() == []
    refusing.part = None
    assert manager.create('plain', title='Morning', columns=COLUMNS, label='b') == 'b'


def test_a_header_change_that_is_refused_changes_nothing(
    manager: BatchManager, refusing: Refusing
) -> None:
    manager.create('plain', title='Morning', columns=COLUMNS, label='b')
    refusing.part = 'header'

    with pytest.raises(NotKept):
        manager.change('b', title='Afternoon')
    assert manager.headers() == {
        'b': Header(spec='plain', columns=COLUMNS, title='Morning')
    }


def test_an_empty_title_is_refused(
    manager: BatchManager, definitions: FolderDefinitions
) -> None:
    with pytest.raises(ValueError, match='a title that is not empty'):
        manager.create('plain', title='', columns=COLUMNS)
    assert definitions.labels() == []
    label = manager.create('plain', title='Morning', columns=COLUMNS)

    with pytest.raises(ValueError, match='a title that is not empty'):
        manager.change(label, title='')
    assert manager.headers()[label].title == 'Morning'


def test_a_scope_change_that_is_refused_leaves_the_rows_of_the_batch(
    manager: BatchManager, refusing: Refusing, sample: Measure
) -> None:
    sample(1, 'water')
    sample(2, 'oil')
    label = manager.create('plain', title='Morning', source='pairs')
    batch = manager.batch(label)
    refusing.part = 'header'

    with pytest.raises(NotKept):
        manager.change(label, scope=Scope(start=2))
    assert batch.scope == Scope()
    assert [row.key for row in batch.rows] == ['1', '2']
    assert manager.headers()[label].scope == Scope()


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
    assert manager.headers() == {}
    assert _again(client, definitions).headers() == {}
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


def statuses(manager: BatchManager, counts: Counts) -> dict[str, dict[str, str]]:
    """The status of each row of each batch counted, as a table names it."""
    return {
        label: status_texts(states, manager.batch(label).columns)
        for label, states in counts.states.items()
    }


def test_the_counts_give_the_state_of_each_row_of_each_batch_after_a_scan(
    manager: BatchManager, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    sample(2, 'oil', partner=False)
    manager.create('plain', title='Morning', source='pairs', label='a')
    manager.create('plain', title='Afternoon', source='pairs', label='b')
    batch = manager.batch('a')
    client.wait(batch.reduce(['1']))
    batch.settings = {'binning': {'bins': 3}}
    sample(3, 'sds')

    counts = manager.counts()

    assert statuses(manager, counts) == {
        'a': {
            '1': 'completed, earlier settings',
            '2': 'incomplete',
            '3': 'not reduced',
        },
        'b': {'1': 'not reduced', '2': 'incomplete', '3': 'not reduced'},
    }
    assert counts.failed == {}


def test_a_row_counts_as_shown_if_any_batch_of_its_source_shows_it(
    manager: BatchManager, sample: Measure
) -> None:
    for run, name in enumerate(['water', 'oil', 'sds', 'salt'], start=1):
        sample(run, name)
    for label, removed in [('a', ['2', '3']), ('b', ['3'])]:
        manager.create('plain', title=label, source='pairs', label=label)
        batch = manager.batch(label)
        batch.rows = batch.rows.remove_each(removed).add(Row(f'mine-{label}', {}))

    assert manager.counts().outside == {'pairs': 1}  # row 3, removed in both


def test_a_row_outside_the_scope_of_every_batch_of_its_source_counts_in_no_batch(
    manager: BatchManager, sample: Measure
) -> None:
    for run, name in enumerate(['water', 'oil', 'sds', 'salt'], start=1):
        sample(run, name)
    manager.create('plain', title='Morning', source='pairs', scope=Scope(stop=2))
    manager.create('plain', title='Evening', source='pairs', scope=Scope(start=4))

    assert manager.counts().outside == {'pairs': 2}  # rows 2 and 3


@pytest.mark.parametrize('broken', ['not registered', 'raises'])
def test_a_batch_that_cannot_be_made_has_its_reason_and_its_source_no_outside(
    client: Client, definitions: FolderDefinitions, sample: Measure, broken: str
) -> None:
    sample(1, 'water')
    sources = {**SOURCES, 'other': SOURCES['pairs']}
    first = BatchManager(client, sources=sources, definitions=definitions)
    first.create('plain', title='Fine', source='pairs', label='a')
    first.create('plain', title='Broken', source='other', label='b')

    def raising(files: Any) -> list[Row]:
        raise OSError('half written')

    if broken == 'not registered':
        sources = SOURCES
    else:
        sources = {
            **SOURCES,
            'other': Source(raising, fills=('sample', 'transmission')),
        }
    manager = BatchManager(client, sources=sources, definitions=definitions)

    counts = manager.counts()

    reason = {
        'not registered': 'b: the source other is not registered',
        'raises': 'half written',
    }[broken]
    assert list(counts.states) == ['a']
    assert counts.failed == {'b': reason}
    assert counts.outside == {'pairs': 0}


def test_a_count_calls_a_source_once_for_all_its_batches(
    client: Client, definitions: FolderDefinitions, sample: Measure
) -> None:
    sample(1, 'water')
    calls = []

    def counted(files: Any) -> list[Row]:
        calls.append(1)
        return pair(files)

    sources = {'pairs': Source(counted, fills=('sample', 'transmission'))}
    manager = BatchManager(client, sources=sources, definitions=definitions)
    for label in ('a', 'b'):
        manager.create('plain', title=label, source='pairs', label=label)

    manager.counts()  # makes both batches, which scans each once
    first = len(calls)
    manager.counts()

    assert (first, len(calls)) == (2, 4)
