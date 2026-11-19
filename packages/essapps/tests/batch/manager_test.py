# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The batches of a workspace, made, kept, changed and removed by a manager."""

import re
from collections.abc import Callable
from contextlib import closing
from typing import Any

import pandas as pd
import pytest

from ess.apps import Client, Stage, Workspace
from ess.apps.batch import (
    Batch,
    BatchManager,
    CorrectedFiles,
    FolderDefinitions,
    Header,
    NotKept,
)
from ess.apps.batch.definition import source_key, source_of
from ess.apps.testing import FakeFiles, FakeStore, StagedFunction
from ess.spec import function_binding

from .toys import (
    COLUMNS,
    PAIRS,
    PLAIN,
    SOURCES,
    STAGED,
    STAGED_NAMES,
    Measure,
    Refusing,
    edit,
    rows,
)


def _again(client: Client, definitions: FolderDefinitions) -> BatchManager:
    """The manager of the fixture made again on ``definitions``, as after a restart."""
    return BatchManager(
        client, sources=SOURCES, definitions=definitions, staged=STAGED_NAMES
    )


def test_a_batch_made_is_listed_with_its_header(
    manager: BatchManager, client: Client, definitions: FolderDefinitions
) -> None:
    label = manager.create(
        'plain', title='Morning', rows=rows(client), description='2 mm cells', label='b'
    )

    header = Header('Morning', '2 mm cells')
    assert label == 'b'
    assert manager.headers() == {'b': header}
    assert _again(client, definitions).headers() == {'b': header}


def test_a_manager_made_again_on_the_definitions_hands_out_the_batch_as_it_was(
    manager: BatchManager,
    client: Client,
    definitions: FolderDefinitions,
    sample: Measure,
) -> None:
    sample(1, 'water')
    label = manager.create('staged', title='Morning', rows=rows(client), label='b')
    batch = manager.batch(label)
    batch.settings = {'binning': {'bins': 3}}
    edit(batch, '1', thickness=2.0)
    edit(batch, 'mine', sample='run:1')

    again = _again(client, definitions).batch(label)

    assert again.spec == STAGED
    assert again.columns == COLUMNS
    assert again.settings == batch.settings
    pd.testing.assert_frame_equal(again.frame(), batch.frame())


def test_the_columns_of_a_batch_are_the_parameter_columns_of_its_rows(
    manager: BatchManager, client: Client
) -> None:
    given = rows(client)[['thickness', 'transmission', 'name', 'sample']]
    chosen = manager.create('plain', title='Evening', rows=given)
    empty = manager.create('plain', title='Morning')

    assert manager.batch(chosen).columns == ('thickness', 'transmission', 'sample')
    assert manager.batch(empty).columns == ()


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
    return BatchManager(
        client,
        sources=SOURCES,
        definitions=definitions,
        settings={'plain': settings},
    )


def test_a_batch_starts_with_the_settings_registered_for_its_spec_but_its_columns(
    with_defaults: BatchManager, client: Client
) -> None:
    label = with_defaults.create('plain', title='Morning', rows=rows(client))

    assert with_defaults.batch(label).settings == {'binning': {'bins': 3}}


def test_settings_given_to_create_replace_those_registered_but_its_columns(
    with_defaults: BatchManager, client: Client
) -> None:
    settings = {'thickness': 3.0, 'offset': {'value': 1.0}}
    label = with_defaults.create(
        'plain', title='Morning', rows=rows(client), settings=settings
    )

    assert with_defaults.batch(label).settings == {'offset': {'value': 1.0}}


def test_settings_registered_later_leave_the_settings_of_a_batch_made_before(
    with_defaults: BatchManager, client: Client, definitions: FolderDefinitions
) -> None:
    label = with_defaults.create('plain', title='Morning', rows=rows(client))
    later = {'plain': {'binning': {'bins': 7}}}

    again = BatchManager(
        client, sources=SOURCES, definitions=definitions, settings=later
    )

    assert again.batch(label).settings == {'binning': {'bins': 3}}


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

        label = manager.create('plain', title='Morning')

        assert manager.batch(label).spec == newer


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


def test_a_source_that_is_not_registered_is_refused(
    manager: BatchManager, definitions: FolderDefinitions
) -> None:
    with pytest.raises(ValueError, match='the source runs is not registered'):
        manager.create('plain', title='Morning', source='runs')
    with pytest.raises(ValueError, match='the source runs is not registered'):
        manager.rows_of('runs')
    assert definitions.labels() == []


def test_a_spec_that_the_workspace_does_not_bind_is_refused(
    manager: BatchManager,
) -> None:
    with pytest.raises(ValueError, match='no spec iofq'):
        manager.create('iofq', title='Morning')
    assert manager.headers() == {}


def test_generated_labels_are_the_time_of_making_and_differ(
    manager: BatchManager,
) -> None:
    first = manager.create('plain', title='Morning')
    second = manager.create('plain', title='Afternoon')

    assert re.fullmatch(r'\d{8}-\d{6}', first)
    # made in the same second, or in the next
    assert second == f'{first}-2' or (
        re.fullmatch(r'\d{8}-\d{6}', second) and second > first
    )


def test_the_batch_of_a_label_is_one_object(manager: BatchManager) -> None:
    label = manager.create('plain', title='Morning', source='pairs')

    assert manager.batch(label) is manager.batch(label)


@pytest.mark.parametrize(
    'call',
    [
        lambda m: m.batch('b'),
        lambda m: m.change('b', title='Afternoon'),
        lambda m: m.remove('b'),
        lambda m: m.offered('b'),
        lambda m: m.take('b', []),
    ],
    ids=['batch', 'change', 'remove', 'offered', 'take'],
)
def test_a_label_that_is_no_batch_is_refused(
    manager: BatchManager, call: Callable[[BatchManager], Any]
) -> None:
    with pytest.raises(KeyError):
        call(manager)


def test_a_title_and_description_changed_are_kept(
    manager: BatchManager, client: Client, definitions: FolderDefinitions
) -> None:
    label = manager.create('plain', title='Morning', description='a')

    header = manager.change(label, title='Afternoon')
    header = manager.change(label, description='b')

    assert header == Header('Afternoon', 'b')
    assert _again(client, definitions).headers() == {label: header}


def test_a_batch_removed_is_gone_with_its_definitions_and_its_records_stay(
    manager: BatchManager,
    client: Client,
    definitions: FolderDefinitions,
    sample: Measure,
) -> None:
    sample(1, 'water')
    label = manager.create('plain', title='Morning', rows=rows(client), source='pairs')
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
        sources=SOURCES,
        definitions=definitions,
        staged=STAGED_NAMES,
    )
    sample(1, 'water')
    label = manager.create('staged', title='Morning', rows=rows(client))
    client.wait(manager.batch(label).reduce(['1']))

    manager.remove(label)

    assert [type(s) for s in releases.released] == [Stage]


def test_a_batch_whose_header_is_refused_is_not_made(
    manager: BatchManager, refusing: Refusing
) -> None:
    refusing.part = 'header'

    with pytest.raises(NotKept):
        manager.create('plain', title='Morning', label='b')
    assert manager.headers() == {}


@pytest.mark.parametrize('title', ['', '   '])
def test_an_empty_or_blank_title_is_refused(
    manager: BatchManager, definitions: FolderDefinitions, title: str
) -> None:
    with pytest.raises(ValueError, match='a title that is not empty'):
        manager.create('plain', title=title)
    assert definitions.labels() == []
    label = manager.create('plain', title='Morning')

    with pytest.raises(ValueError, match='a title that is not empty'):
        manager.change(label, title=title)
    assert manager.headers()[label].title == 'Morning'


def test_a_batch_whose_removal_is_refused_is_kept(
    manager: BatchManager, refusing: Refusing
) -> None:
    manager.create('plain', title='Morning', label='b')
    batch = manager.batch('b')
    refusing.part = 'header'

    with pytest.raises(NotKept):
        manager.remove('b')
    assert list(manager.headers()) == ['b']
    assert manager.batch('b') is batch


def test_a_batch_made_without_rows_takes_the_rows_put(
    manager: BatchManager, client: Client, definitions: FolderDefinitions
) -> None:
    label = manager.create('plain', title='Morning')
    batch = manager.batch(label)
    assert batch.frame().empty

    batch.put(pd.DataFrame({'sample': ['run:2']}, index=['mine']))

    assert list(_again(client, definitions).batch(label).frame().index) == ['mine']


# A batch made from a source


@pytest.fixture
def made(manager: BatchManager, sample: Measure) -> BatchManager:
    """
    ``manager`` with runs 1 to 4 measured, run 5 without its transmission
    run, and the batch ``a`` of the rows from run 3 on, and a row ``mine``.
    """
    for run, name in enumerate(['water', 'sds', 'dmso', 'd2o'], start=1):
        sample(run, name)
    sample(5, 'oil', partner=False)
    given = manager.rows_of('pairs')
    table = given[given.run.isin([3, 4])].copy()
    table.loc['mine', 'sample'] = 'run:1'
    manager.create(PLAIN.name, title='A', rows=table, source='pairs', label='a')
    return manager


def test_a_batch_made_from_a_source_considers_its_keys_and_those_of_its_rows(
    made: BatchManager, definitions: FolderDefinitions
) -> None:
    kept = definitions.load(source_key('a'), source_of)

    assert kept.source == 'pairs'
    assert kept.considered == {'1', '2', '3', '4', '5', 'mine'}


def test_the_rows_offered_are_those_of_the_source_that_the_table_lacks(
    made: BatchManager,
) -> None:
    batch = made.batch('a')
    batch.put(batch.frame().drop('4'))

    offered = made.offered('a')

    assert offered['waits'].to_dict() == {
        '1': False,
        '2': False,
        '4': False,
        '5': False,
    }
    assert list(offered.columns) == [*made.rows_of('pairs').columns, 'waits']


def test_a_row_of_the_source_that_waits_is_offered_as_waiting(
    manager: BatchManager, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    manager.create('plain', title='A', rows=rows(client), source='pairs', label='a')
    sample(2, 'oil', partner=False)

    assert manager.offered('a')['waits'].to_dict() == {'2': True}


def test_rows_taken_are_put_as_the_source_makes_them_and_considered(
    manager: BatchManager,
    client: Client,
    fake: FakeFiles,
    sample: Measure,
    definitions: FolderDefinitions,
) -> None:
    sample(1, 'water')
    manager.create('plain', title='A', rows=rows(client), source='pairs', label='a')
    manager.follow('a')
    sample(2, 'oil', partner=False)

    manager.take('a', ['2'])
    batch = manager.batch('a')
    taken = batch.frame().loc['2']
    batch.put(batch.frame().drop('2'))
    fake.measure(1002, 1.0, role='transmission', sample='oil')
    manager.step()

    assert taken['sample'] == 'uuid:run-2'
    assert pd.isna(taken['transmission'])
    assert list(batch.frame().index) == ['1']
    assert definitions.load(source_key('a'), source_of).considered == {'1', '2'}


@pytest.mark.parametrize(
    ('keys', 'said'),
    [(['9'], r"makes no rows \['9'\]"), (['3'], "the key '3' is given twice")],
)
def test_rows_that_cannot_be_taken_are_refused_and_nothing_changes(
    made: BatchManager, keys: list[str], said: str
) -> None:
    before = made.batch('a').frame()

    with pytest.raises(ValueError, match=said):
        made.take('a', keys)

    pd.testing.assert_frame_equal(made.batch('a').frame(), before)


def test_a_batch_made_without_a_source_offers_and_takes_no_rows(
    manager: BatchManager,
) -> None:
    manager.create('plain', title='A', label='a')

    with pytest.raises(ValueError, match='a was not made from a source'):
        manager.offered('a')
    with pytest.raises(ValueError, match='a was not made from a source'):
        manager.take('a', ['1'])


# Which batch follows


@pytest.fixture
def two(manager: BatchManager) -> BatchManager:
    """``manager`` with two batches of a source, ``a`` and ``b``."""
    manager.create('plain', title='A', source='pairs', label='a')
    manager.create('plain', title='B', source='pairs', label='b')
    return manager


def test_one_batch_follows_and_a_manager_made_again_has_it(
    two: BatchManager, client: Client, definitions: FolderDefinitions
) -> None:
    assert two.following is None
    assert two.step() is None

    two.follow('a')
    two.follow('b')

    assert two.following == 'b'
    assert _again(client, definitions).following == 'b'
    two.follow(None)
    assert _again(client, definitions).following is None


def test_a_batch_removed_follows_no_longer(
    two: BatchManager, client: Client, definitions: FolderDefinitions
) -> None:
    two.follow('a')

    two.remove('a')

    assert two.following is None
    assert _again(client, definitions).following is None


def test_a_kept_label_without_a_header_follows_not(
    two: BatchManager, client: Client, definitions: FolderDefinitions
) -> None:
    two.follow('a')
    definitions.delete(('labels', 'a', 'header'))

    assert _again(client, definitions).following is None


def test_a_batch_that_cannot_follow_is_refused_and_nothing_changes(
    two: BatchManager, client: Client, definitions: FolderDefinitions
) -> None:
    two.follow('a')
    two.create('plain', title='C', label='c')
    BatchManager(
        client, sources={**SOURCES, 'other': PAIRS}, definitions=definitions
    ).create('plain', title='D', source='other', label='d')
    again = _again(client, definitions)

    with pytest.raises(ValueError, match='c was not made from a source'):
        again.follow('c')
    with pytest.raises(ValueError, match='the source other is not registered'):
        again.follow('d')
    with pytest.raises(KeyError):
        again.follow('e')
    assert again.following == 'a'


def test_which_batch_follows_stays_if_the_definitions_refuse_a_change(
    two: BatchManager, refusing: Refusing
) -> None:
    two.follow('a')
    refusing.part = 'following'

    with pytest.raises(NotKept):
        two.follow('b')
    with pytest.raises(NotKept):
        two.follow(None)
    assert two.following == 'a'


def test_a_removal_that_the_definitions_refuse_leaves_the_batch_following(
    two: BatchManager, refusing: Refusing
) -> None:
    two.follow('a')
    refusing.part = 'header'

    with pytest.raises(NotKept):
        two.remove('a')

    assert list(two.headers()) == ['a', 'b']
    assert two.following == 'a'


def test_a_removal_whose_end_of_follow_is_refused_leaves_none_following(
    two: BatchManager,
    refusing: Refusing,
    client: Client,
    definitions: FolderDefinitions,
) -> None:
    two.follow('a')
    refusing.part = 'following'

    with pytest.raises(NotKept):
        two.remove('a')

    assert list(two.headers()) == ['b']
    assert two.following is None
    assert _again(client, definitions).following is None
