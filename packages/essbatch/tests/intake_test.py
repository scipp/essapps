# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Rows of a row source brought into batches by an intake, and Follow."""

from collections.abc import Callable
from typing import Any

import pandas as pd
import pytest
from ess.batch import (
    BatchManager,
    FolderDefinitions,
    Intake,
    NotKept,
    Range,
    RowSource,
)
from ess.batch.definition import (
    HEADER,
    SETTINGS,
    SOURCE,
    Key,
)

from ess.apps import Client
from ess.apps.testing import FakeFiles

from .toys import PLAIN, SAMPLES, SOURCES, Measure, Refusing, edit, from_pairs


def _again(client: Client, definitions: FolderDefinitions) -> Intake:
    """
    The intake of the fixture made again on ``definitions``, with a manager
    made again, as after a restart.
    """
    return Intake(BatchManager(client, definitions=definitions), sources=SOURCES)


@pytest.fixture
def made(intake: Intake, sample: Measure) -> Intake:
    """
    ``intake`` with runs 1 to 4 measured, run 5 without its transmission
    run, and the batch ``a`` of the rows from run 3 on, and a row ``mine``
    added by hand.
    """
    for run, name in enumerate(['water', 'sds', 'dmso', 'd2o'], start=1):
        sample(run, name)
    sample(5, 'oil', partner=False)
    batch = intake.create(
        PLAIN.name,
        title='A',
        source='pairs',
        choose=lambda given: given[given.run >= 3],
        label='a',
    )
    edit(batch, 'mine', sample='run:1')
    return intake


def test_a_batch_made_from_a_source_takes_and_considers_its_filled_rows(
    made: Intake, definitions: FolderDefinitions
) -> None:
    kept = SOURCE.load(definitions, 'a')

    assert list(made.manager.batch('a').frame().index) == ['3', '4', 'mine']
    assert made.source('a') == 'pairs'
    assert kept.considered == {'1', '2', '3', '4'}  # not 5, which waits


def test_a_batch_made_from_a_source_keeps_its_fills_when_read_again(
    intake: Intake, client: Client, definitions: FolderDefinitions
) -> None:
    from_pairs(intake, 'plain', title='A', label='a')

    again = BatchManager(client, definitions=definitions).batch('a')

    assert again.fills == {'sample', 'transmission'}


def test_a_source_json_with_fills_is_refused_when_read(
    intake: Intake, client: Client, definitions: FolderDefinitions
) -> None:
    from_pairs(intake, 'plain', title='A', label='a')
    kept = definitions.load(SOURCE.key('a'), dict)
    definitions.save(SOURCE.key('a'), {**kept, 'fills': ['sample']})

    with pytest.raises(ValueError, match=r'source\.json: fills: Unexpected'):
        _again(client, definitions).source('a')


def test_choose_may_give_columns_that_the_source_does_not(
    intake: Intake, sample: Measure
) -> None:
    sample(1, 'water')

    batch = intake.create(
        PLAIN.name,
        title='A',
        source='pairs',
        choose=lambda given: given.assign(thickness=2.0),
    )

    assert batch.frame()['thickness'].tolist() == [2.0]


def test_columns_given_are_empty_and_a_column_of_the_table_keeps_its_cells(
    intake: Intake, sample: Measure
) -> None:
    sample(1, 'water')

    batch = intake.create(
        PLAIN.name,
        title='A',
        source='pairs',
        parameter_columns=['sample', 'thickness'],
    )

    frame = batch.frame()
    assert frame['sample'].tolist() == ['uuid:run-1']
    assert frame['thickness'].isna().tolist() == [True]


@pytest.fixture
def four(intake: Intake, sample: Measure) -> Intake:
    """``intake`` with runs 1, 2 and 4 measured, and run 3 without its partner."""
    for run, name in [(1, 'water'), (2, 'sds'), (4, 'ethanol')]:
        sample(run, name)
    sample(3, 'oil', partner=False)
    return intake


@pytest.mark.parametrize(
    ('chosen', 'taken'),
    [
        (Range(first='2'), ['2', '4']),
        (Range(last='2'), ['1', '2']),
        (Range('2', '2'), ['2']),
        (Range(later=True), []),
    ],
    ids=['from', 'to', 'one', 'later'],
)
def test_a_range_takes_the_filled_rows_between_its_ends_and_considers_all(
    four: Intake,
    definitions: FolderDefinitions,
    chosen: Range,
    taken: list[str],
) -> None:
    batch = four.create(PLAIN.name, title='A', source='pairs', choose=chosen)

    assert list(batch.frame().index) == taken
    considered = SOURCE.load(definitions, batch.label).considered
    assert considered == {'1', '2', '4'}  # not 3, which waits


@pytest.mark.parametrize(
    ('chosen', 'said'),
    [
        (Range(first='3'), '3 is not a filled row'),
        (Range('2', '1'), 'the row 2 comes after the row 1'),
    ],
    ids=['waits', 'reversed'],
)
def test_a_range_that_takes_no_row_of_its_ends_is_refused_and_nothing_is_kept(
    four: Intake, definitions: FolderDefinitions, chosen: Range, said: str
) -> None:
    with pytest.raises(ValueError, match=f'^{said}$'):
        four.create(PLAIN.name, title='A', source='pairs', choose=chosen)

    assert (four.manager.headers(), definitions.labels()) == ({}, [])


def test_a_range_of_the_rows_that_arrive_later_has_no_end() -> None:
    with pytest.raises(ValueError, match='arrive later has no first or last'):
        Range(first='1', later=True)


def test_a_batch_of_the_rows_that_arrive_later_that_follows_gets_only_those(
    four: Intake, fake: FakeFiles, sample: Measure
) -> None:
    batch = four.create(
        'plain', title='A', source='pairs', choose=Range(later=True), follow=True
    )
    sample(5, 'dmso')
    fake.measure(1003, 1.0, role='transmission', sample='oil')  # 3 waited

    four.step()

    assert list(batch.frame().index) == ['3', '5']


def test_a_row_that_waits_when_a_batch_is_made_is_added_by_follow_once_filled(
    intake: Intake, fake: FakeFiles, sample: Measure
) -> None:
    sample(1, 'water')
    sample(2, 'oil', partner=False)
    batch = intake.create('plain', title='A', source='pairs', follow=True)
    fake.measure(1002, 1.0, role='transmission', sample='oil')

    intake.step()

    assert list(batch.frame().index) == ['1', '2']


def test_a_batch_made_without_a_source_has_none(intake: Intake) -> None:
    intake.manager.create('plain', title='A', label='a')

    assert intake.source('a') is None


def _not_registered(name: str, registered: str) -> str:
    """The pattern of the refusal of the source ``name``, which is not registered."""
    return rf'^the source {name} is not registered; registered: {registered}$'


def test_a_source_that_is_not_registered_is_refused(
    intake: Intake, definitions: FolderDefinitions
) -> None:
    with pytest.raises(ValueError, match=_not_registered('runs', 'pairs')):
        intake.create('plain', title='Morning', source='runs')
    with pytest.raises(ValueError, match=_not_registered('runs', 'pairs')):
        intake.rows_of('runs')
    assert definitions.labels() == []


def test_a_row_chosen_that_the_source_does_not_give_is_refused_and_nothing_is_kept(
    intake: Intake, definitions: FolderDefinitions, sample: Measure
) -> None:
    sample(1, 'water')

    def choose(given: pd.DataFrame) -> pd.DataFrame:
        return given.rename({'1': '9'})

    with pytest.raises(ValueError, match=r"choose gives rows \['9'\] that the"):
        intake.create('plain', title='A', source='pairs', choose=choose)
    assert intake.manager.headers() == {}
    assert definitions.labels() == []


@pytest.mark.parametrize(
    'call',
    [
        lambda i: i.source('b'),
        lambda i: i.offered('b'),
        lambda i: i.take('b', []),
        lambda i: i.follow('b'),
    ],
    ids=['source', 'offered', 'take', 'follow'],
)
def test_a_label_that_is_no_batch_is_refused(
    intake: Intake, call: Callable[[Intake], Any]
) -> None:
    with pytest.raises(KeyError):
        call(intake)


def test_the_rows_offered_are_those_of_the_source_that_the_table_lacks(
    made: Intake,
) -> None:
    batch = made.manager.batch('a')
    batch.put(batch.frame().drop('4'))

    offered = made.offered('a')

    assert offered['waits'].to_dict() == {
        '1': False,
        '2': False,
        '4': False,
        '5': True,
    }
    assert list(offered.columns) == [*made.rows_of('pairs').columns, 'waits']


def test_a_row_of_the_source_that_waits_is_offered_as_waiting(
    intake: Intake, sample: Measure
) -> None:
    sample(1, 'water')
    intake.create('plain', title='A', source='pairs', label='a')
    sample(2, 'oil', partner=False)

    assert intake.offered('a')['waits'].to_dict() == {'2': True}


def test_rows_taken_are_put_as_the_source_makes_them_and_considered(
    intake: Intake,
    fake: FakeFiles,
    sample: Measure,
    definitions: FolderDefinitions,
) -> None:
    sample(1, 'water')
    batch = intake.create('plain', title='A', source='pairs', label='a', follow=True)
    sample(2, 'oil', partner=False)

    intake.take('a', ['2'])
    taken = batch.frame().loc['2']
    batch.put(batch.frame().drop('2'))
    fake.measure(1002, 1.0, role='transmission', sample='oil')
    intake.step()

    assert taken['sample'] == 'uuid:run-2'
    assert pd.isna(taken['transmission'])
    assert list(batch.frame().index) == ['1']
    assert SOURCE.load(definitions, 'a').considered == {'1', '2'}


@pytest.mark.parametrize('added_by', ['step', 'take'])
def test_a_row_added_after_make_column_takes_the_setting(
    intake: Intake, client: Client, sample: Measure, added_by: str
) -> None:
    sample(1, 'water')
    settings = {'thickness': 2.0}
    batch = intake.create(
        'plain', title='A', source='pairs', label='a', settings=settings, follow=True
    )
    client.wait(batch.reduce(['1']))
    batch.make_column('thickness')
    sample(2, 'oil')

    if added_by == 'step':
        client.wait(list(intake.step().records.values()))
    else:
        intake.take('a', ['2'])
        client.wait(batch.reduce(['2']))

    assert pd.isna(batch.frame().loc['2', 'thickness'])
    assert batch.state()['2'].record.request.params['thickness'] == 2.0
    assert batch.state()['1'].text == 'done'


def test_a_waiting_row_taken_or_stepped_waits_whatever_the_settings_kept(
    intake: Intake,
    client: Client,
    sample: Measure,
    definitions: FolderDefinitions,
) -> None:
    sample(1, 'water')
    from_pairs(intake, 'plain', title='A', label='a', follow=True)
    sample(2, 'oil', partner=False)
    kept = definitions.load(SETTINGS.key('a'), dict)
    stale = {**kept['params'], 'transmission': 'run:1001'}
    definitions.save(SETTINGS.key('a'), {**kept, 'params': stale})
    again = _again(client, definitions)  # as after a restart

    again.take('a', ['2'])
    done = again.step()
    state = again.manager.batch('a').state()['2']

    assert again.manager.batch('a').state()['1'].text != 'incomplete'
    assert done is not None
    assert '2' not in done.records
    assert [(e.loc, e.msg) for e in state.problems] == [(('transmission',), 'empty')]


def test_a_source_that_no_longer_gives_a_column_of_the_fills_kept_is_named(
    intake: Intake, client: Client, definitions: FolderDefinitions
) -> None:
    from_pairs(intake, 'plain', title='A', label='a', follow=True)
    sources = {'pairs': SAMPLES}  # its code now fills sample only
    again = Intake(BatchManager(client, definitions=definitions), sources=sources)
    said = 'the source pairs: it no longer gives the column transmission'

    with pytest.raises(ValueError, match=said):
        again.offered('a')
    with pytest.raises(ValueError, match=said):
        again.step()


def test_a_step_whose_source_is_not_registered_is_refused_and_keeps_nothing(
    client: Client, sample: Measure, definitions: FolderDefinitions
) -> None:
    sample(1, 'water')
    sources = dict(SOURCES)
    intake = Intake(BatchManager(client, definitions=definitions), sources=sources)
    batch = intake.create('plain', title='A', source='pairs', label='a', follow=True)
    typed = batch.frame().rename(index={'1': 'typed'})
    batch.put(pd.concat([batch.frame(), typed]))
    del sources['pairs']

    with pytest.raises(ValueError, match=_not_registered('pairs', 'none')):
        intake.step()

    # A key typed by hand is considered only by a step that reads the source.
    assert SOURCE.load(definitions, 'a').considered == {'1'}


@pytest.mark.parametrize(
    ('keys', 'said'),
    [
        (['9'], r"makes no rows \['9'\]"),
        (['3'], r"rows \['3'\] are in the batch already"),
    ],
)
def test_rows_that_cannot_be_taken_are_refused_and_nothing_changes(
    made: Intake, keys: list[str], said: str
) -> None:
    before = made.manager.batch('a').frame()

    with pytest.raises(ValueError, match=said):
        made.take('a', keys)

    pd.testing.assert_frame_equal(made.manager.batch('a').frame(), before)


def test_a_batch_made_without_a_source_offers_and_takes_no_rows_and_cannot_follow(
    intake: Intake,
) -> None:
    intake.manager.create('plain', title='A', label='a')

    with pytest.raises(ValueError, match='a was not made from a row source'):
        intake.offered('a')
    with pytest.raises(ValueError, match='a was not made from a row source'):
        intake.take('a', ['1'])
    with pytest.raises(ValueError, match='a was not made from a row source'):
        intake.follow('a')
    assert intake.following is None


class _SourceRefused(Refusing):
    """:class:`Refusing` that refuses to save ``source.json`` only."""

    def save(self, key: Key, data: Any) -> None:
        if key[-1] == 'source':
            raise NotKept(f'{key}: refused')
        super().save(key, data)


def test_a_batch_whose_source_is_refused_is_removed_again(
    client: Client, definitions: FolderDefinitions, sample: Measure
) -> None:
    sample(1, 'water')
    refusing = _SourceRefused(definitions)
    intake = Intake(BatchManager(client, definitions=refusing), sources=SOURCES)

    with pytest.raises(NotKept):
        intake.create('plain', title='A', source='pairs', label='a', follow=True)

    assert intake.manager.headers() == {}
    assert definitions.labels() == []
    assert intake.following is None


def _broken(files: Any) -> pd.DataFrame:
    raise AssertionError('the source is listed')


@pytest.mark.parametrize(
    ('spec', 'title', 'label', 'said'),
    [
        ('iofq', 'A', None, 'no spec iofq'),
        ('plain', ' ', None, 'a title that is not empty'),
        ('plain', 'A', 'a/b', 'a label is'),
    ],
)
def test_a_spec_title_or_label_is_refused_before_the_source_is_looked_up(
    manager: BatchManager, spec: str, title: str, label: str | None, said: str
) -> None:
    intake = Intake(manager, sources={'broken': RowSource(_broken, fills=())})

    for source in ('broken', 'nope'):
        with pytest.raises(ValueError, match=said):
            intake.create(spec, title=title, source=source, label=label)


@pytest.mark.parametrize(
    ('spec', 'lacked'),
    [
        ('find', 'parameter transmission'),
        ('repeated', 'parameters sample, transmission'),
    ],
)
def test_a_spec_that_lacks_a_parameter_the_source_fills_is_refused(
    intake: Intake, definitions: FolderDefinitions, spec: str, lacked: str
) -> None:
    said = f'the spec {spec} has no {lacked}, which the source pairs fills'

    with pytest.raises(ValueError, match=f'^{said}$'):
        intake.create(spec, title='A', source='pairs', label='a')

    assert definitions.labels() == []


# Which batch follows


@pytest.fixture
def two(intake: Intake) -> Intake:
    """
    ``intake`` with two batches of a source: ``a``, which follows, and ``b``,
    made without Follow.
    """
    intake.create('plain', title='A', source='pairs', label='a', follow=True)
    intake.create('plain', title='B', source='pairs', label='b')
    return intake


def _follows(definitions: FolderDefinitions) -> list[str]:
    """The labels whose ``source.json`` says that they follow."""
    return [
        label
        for label in definitions.labels()
        if (made := SOURCE.load(definitions, label)) is not None and made.follows
    ]


def test_a_source_json_that_cannot_be_read_hides_neither_follow_nor_other_batches(
    two: Intake, client: Client, definitions: FolderDefinitions
) -> None:
    two.create('plain', title='C', source='pairs', label='c', follow=True)
    definitions.save(SOURCE.key('a'), {'source': 'pairs'})
    again = _again(client, definitions)

    assert again.following == 'c'
    assert again.source('b') == 'pairs'
    with pytest.raises(ValueError, match=r'source\.json: considered: '):
        again.source('a')


def test_a_batch_made_to_follow_ends_follow_on_the_one_before_then_writes_its_own(
    two: Intake, refusing: Refusing, definitions: FolderDefinitions
) -> None:
    two.create('plain', title='C', source='pairs', label='c', follow=True)

    assert refusing.saved[-3:] == [HEADER.key('c'), SOURCE.key('a'), SOURCE.key('c')]
    assert _follows(definitions) == ['c']


def test_a_batch_made_to_follow_follows_in_place_of_the_one_before(
    two: Intake, client: Client, definitions: FolderDefinitions
) -> None:
    assert two.following == 'a'  # b was made without Follow

    two.create('plain', title='C', source='pairs', label='c', follow=True)

    assert two.following == 'c'
    assert _again(client, definitions).following == 'c'


def test_a_batch_that_follows_with_a_last_row_is_refused_and_nothing_changes(
    two: Intake, definitions: FolderDefinitions, sample: Measure
) -> None:
    sample(1, 'water')

    with pytest.raises(ValueError, match='so it has no last row'):
        two.create(
            'plain',
            title='C',
            source='pairs',
            choose=Range(last='1'),
            label='c',
            follow=True,
        )

    assert (list(two.manager.headers()), two.following) == (['a', 'b'], 'a')
    assert 'c' not in definitions.labels()


def test_after_a_stop_no_batch_follows_and_an_intake_made_again_has_none(
    two: Intake, client: Client, definitions: FolderDefinitions, refusing: Refusing
) -> None:
    two.stop()

    assert two.following is None
    assert two.step() is None
    assert _again(client, definitions).following is None
    saved = list(refusing.saved)
    two.stop()  # none follows: nothing is done
    assert refusing.saved == saved


def test_a_stopped_batch_gets_no_rows_at_a_step(
    intake: Intake, sample: Measure
) -> None:
    sample(1, 'water')
    batch = intake.create('plain', title='A', source='pairs', label='a', follow=True)
    intake.stop()
    sample(2, 'oil')

    intake.step()

    assert list(batch.frame().index) == ['1']
    assert intake.offered('a')['waits'].to_dict() == {'2': False}


def test_a_stopped_batch_follows_again_and_its_step_adds_the_rows_filled_since(
    intake: Intake, sample: Measure
) -> None:
    sample(1, 'water')
    sample(2, 'sds')
    batch = intake.create(
        'plain', title='A', source='pairs', choose=Range(first='2'), label='a'
    )
    intake.stop()
    sample(3, 'oil')

    intake.follow('a')
    intake.step()

    assert intake.following == 'a'
    assert list(batch.frame().index) == ['2', '3']  # 1 was left out at making


def test_a_batch_that_follows_again_adds_the_rows_that_another_took_meanwhile(
    two: Intake, sample: Measure
) -> None:
    sample(1, 'water')
    two.follow('b')
    two.step()

    two.follow('a')
    two.step()

    assert list(two.manager.batch('a').frame().index) == ['1']
    assert list(two.manager.batch('b').frame().index) == ['1']


def test_a_batch_told_to_follow_ends_follow_on_the_one_before_then_writes_its_own(
    two: Intake, refusing: Refusing, definitions: FolderDefinitions
) -> None:
    two.follow('b')

    assert refusing.saved[-2:] == [SOURCE.key('a'), SOURCE.key('b')]
    assert _follows(definitions) == ['b']
    assert two.following == 'b'


def test_a_batch_told_to_follow_follows_after_a_restart(
    two: Intake, client: Client, definitions: FolderDefinitions
) -> None:
    two.stop()

    two.follow('b')

    assert _again(client, definitions).following == 'b'


def test_a_batch_that_follows_already_is_left_as_it_is(
    two: Intake, refusing: Refusing
) -> None:
    saved = list(refusing.saved)

    two.follow('a')

    assert (two.following, refusing.saved) == ('a', saved)


def test_a_batch_whose_source_is_not_registered_cannot_follow(
    two: Intake, client: Client, definitions: FolderDefinitions
) -> None:
    again = Intake(BatchManager(client, definitions=definitions), sources={})

    with pytest.raises(ValueError, match=_not_registered('pairs', 'none')):
        again.follow('b')
    assert again.following == 'a'


def test_a_batch_refused_to_follow_leaves_the_one_before_following(
    two: Intake, refusing: Refusing, client: Client, definitions: FolderDefinitions
) -> None:
    refusing.part = SOURCE.key('b')

    with pytest.raises(NotKept):
        two.follow('b')

    assert two.following == 'a'
    assert _follows(definitions) == ['a']
    assert _again(client, definitions).following == 'a'


def test_a_batch_removed_through_the_manager_follows_no_longer(
    two: Intake, client: Client, definitions: FolderDefinitions
) -> None:
    two.manager.remove('a')

    assert two.following is None
    assert two.step() is None
    assert _again(client, definitions).following is None


@pytest.mark.parametrize(
    'remake',
    [
        lambda i: i.manager.create('plain', title='A again', label='a'),
        lambda i: i.create('plain', title='A again', source='pairs', label='a'),
    ],
    ids=['manager', 'intake'],
)
def test_a_label_removed_and_made_again_without_follow_does_not_follow(
    two: Intake,
    client: Client,
    definitions: FolderDefinitions,
    remake: Callable[[Intake], object],
) -> None:
    two.manager.remove('a')
    remake(two)

    assert two.following is None
    assert _again(client, definitions).following is None


class _Counting(Refusing):
    """:class:`Refusing` that counts the tables loaded."""

    def __init__(self, definitions: FolderDefinitions) -> None:
        super().__init__(definitions)
        self.loads = 0

    def load[T](self, key: Key, read: Callable[[Any], T]) -> T | None:
        self.loads += 1
        return super().load(key, read)


def test_which_batch_follows_is_read_again_only_after_a_change_of_the_headers(
    two: Intake, client: Client, definitions: FolderDefinitions
) -> None:
    counting = _Counting(definitions)
    intake = Intake(BatchManager(client, definitions=counting), sources=SOURCES)
    assert intake.following == 'a'
    loads = counting.loads

    assert intake.following == 'a'
    assert counting.loads == loads
    intake.manager.change('b', title='B again')
    assert intake.following == 'a'
    assert counting.loads > loads


def test_a_kept_label_without_a_header_follows_not(
    two: Intake, client: Client, definitions: FolderDefinitions
) -> None:
    definitions.delete(('labels', 'a', 'header'))

    assert _again(client, definitions).following is None


def test_which_batch_follows_stays_if_the_definitions_refuse_a_change(
    two: Intake, refusing: Refusing
) -> None:
    refusing.part = SOURCE.key('a')

    with pytest.raises(NotKept):
        two.create('plain', title='C', source='pairs', label='c', follow=True)
    with pytest.raises(NotKept):
        two.stop()
    assert (list(two.manager.headers()), two.following) == (['a', 'b'], 'a')
    assert 'c' not in refusing.labels()
