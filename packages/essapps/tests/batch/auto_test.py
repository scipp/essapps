# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Automatic reduction: which rows a step reduces, and why a row did not run."""

import functools
import threading
from collections.abc import Sequence
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ess.apps import Client, Workspace, file
from ess.apps.batch import (
    EMPTY,
    Batch,
    BatchManager,
    CorrectedFiles,
    DefinitionStore,
    FileInfo,
    FolderDefinitions,
    NotKept,
    Row,
    RowSource,
)
from ess.apps.batch.auto import Automatic, Follow, every
from ess.apps.folder import open_folder
from ess.apps.testing import FakeFiles, StagedFunction
from ess.spec import Binding, FileRef, function_binding

from .toys import (
    COLUMNS,
    FIND,
    PLAIN,
    REPEATED,
    SERIES,
    STAGED,
    Gate,
    Measure,
    Refusing,
    pair,
)

SETTINGS: dict[str, Any] = {'binning': {'bins': 2}}


@pytest.fixture
def loop(batch: Batch) -> Automatic:
    return Automatic(batch, at={'sample'})


def _loop(
    client: Any, source: RowSource = pair, definitions: DefinitionStore | None = None
) -> Automatic:
    """A loop on ``client``, which may be a fake that wraps a client."""
    batch = Batch(
        client,
        PLAIN,
        columns=COLUMNS,
        label='scan',
        source=source,
        definitions=definitions,
    )
    return Automatic(batch, at={'sample'})


def _angles(files: Sequence[FileInfo]) -> list[Row]:
    """A row per sample: all its runs so far, in run order."""
    runs: dict[str, list[Any]] = {}
    for f in sorted(files, key=lambda f: f.metadata['run']):
        runs.setdefault(f.metadata['sample'], []).append(f.ref)
    return [Row(name, {'runs': refs}) for name, refs in runs.items()]


@pytest.fixture
def series(client: Client) -> Automatic:
    batch = Batch(client, SERIES, columns=('runs',), label='series', source=_angles)
    return Automatic(batch, at={'runs'})


def _per_sample(files: Sequence[FileInfo]) -> list[Row]:
    """
    A row per sample, with its sample and transmission runs so far, in run
    order; ``trans_runs`` is left out until a transmission run arrives.
    """
    runs: dict[str, dict[str, list[FileRef]]] = {}
    for f in sorted(files, key=lambda f: f.metadata['run']):
        column = 'trans_runs' if f.metadata['role'] == 'transmission' else 'sample_runs'
        runs.setdefault(f.metadata['sample'], {}).setdefault(column, []).append(f.ref)
    return [Row(name, cells) for name, cells in runs.items()]


def _repeated(client: Client, at: set[str]) -> Automatic:
    batch = Batch(
        client,
        REPEATED,
        columns=('sample_runs', 'trans_runs'),
        label='repeated',
        source=_per_sample,
    )
    return Automatic(batch, at=at)


# The files of the section "The model: tables and one cycle" of the guide, in
# the order they arrive: name, run, role, and sample.
ARRIVALS = [
    ('s1', 1, 'sample', '1'),
    ('t1', 2, 'transmission', '1'),
    ("s1'", 3, 'sample', '1'),
    ("t1'", 4, 'transmission', '1'),
    ('s2', 5, 'sample', '2'),
    ('t2', 6, 'transmission', '2'),
]


@pytest.mark.parametrize(
    ('at', 'reduced', 'texts'),
    [
        (
            {'sample_runs', 'trans_runs'},
            {
                't1': {'1': (['s1'], ['t1'])},
                "s1'": {'1': (['s1', "s1'"], ['t1'])},
                "t1'": {'1': (['s1', "s1'"], ['t1', "t1'"])},
                't2': {'2': (['s2'], ['t2'])},
            },
            {'1': 'completed', '2': 'completed'},
        ),
        (
            {'sample_runs'},
            {
                't1': {'1': (['s1'], ['t1'])},
                "s1'": {'1': (['s1', "s1'"], ['t1'])},
                't2': {'2': (['s2'], ['t2'])},
            },
            {'1': 'outdated', '2': 'completed'},
        ),
        (
            set(),
            {'t1': {'1': (['s1'], ['t1'])}, 't2': {'2': (['s2'], ['t2'])}},
            {'1': 'outdated', '2': 'completed'},
        ),
    ],
)
def test_a_row_is_reduced_again_for_a_change_at_a_column_of_at(
    client: Client,
    fake: FakeFiles,
    at: set[str],
    reduced: dict[str, dict[str, tuple[list[str], list[str]]]],
    texts: dict[str, str],
) -> None:
    loop = _repeated(client, at)
    names: dict[FileRef, str] = {}
    seen = {}
    for name, run, role, sample in ARRIVALS:
        names[fake.measure(run, float(run), role=role, sample=sample)] = name
        records = loop.step()
        client.wait(list(records.values()))
        seen[name] = {
            key: tuple(
                [names[f] for f in r.request.params[column]]
                for column in ('sample_runs', 'trans_runs')
            )
            for key, r in records.items()
        }
        assert loop.refused == {}

    assert {name: rows for name, rows in seen.items() if rows} == reduced
    state = loop.batch.state()
    assert {key: s.text for key, s in state.items()} == texts


def test_a_row_with_an_empty_cell_waits_and_its_state_says_why(
    client: Client, fake: FakeFiles
) -> None:
    loop = _repeated(client, {'sample_runs', 'trans_runs'})
    fake.measure(1, 1.0, role='sample', sample='1')

    assert loop.step() == {}
    assert loop.refused == {}
    (problem,) = loop.batch.state()['1'].problems
    assert str(problem) == '1: trans_runs: empty'


def test_each_new_row_is_reduced_once(
    loop: Automatic, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    sample(2, 'sds')
    first = loop.step()
    client.wait(first)
    sample(3, 'water')

    assert list(first) == ['1', '2']
    assert list(loop.step()) == ['3']
    assert loop.step() == {}


@pytest.mark.parametrize('at', [{'offset', 'sample'}, {'offset'}])
def test_every_element_of_at_must_be_a_column(batch: Batch, at: set[str]) -> None:
    with pytest.raises(KeyError, match='offset'):
        Automatic(batch, at=at)


def test_at_is_a_set_not_a_column_name(batch: Batch) -> None:
    with pytest.raises(TypeError, match='a set of columns'):
        Automatic(batch, at='sample')  # type: ignore[arg-type]


def test_the_batch_must_have_a_source(client: Client) -> None:
    batch = Batch(client, PLAIN, columns=COLUMNS, label='scan')

    with pytest.raises(ValueError, match='no source'):
        Automatic(batch, at={'sample'})


def test_a_failed_row_is_reduced_again_only_for_a_change_at_at(
    series: Automatic, client: Client, fake: FakeFiles
) -> None:
    fake.corrupt(fake.measure(1, 1.0, sample='si'))
    assert client.wait(series.step()) == {'si': 'failed'}
    assert series.step() == {}

    fake.measure(2, 2.0, sample='si')

    assert list(series.step()) == ['si']


def test_a_file_that_arrives_again_is_not_reduced_again(
    loop: Automatic, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    client.wait(loop.step())

    sample(1, 'water')

    assert loop.step() == {}


def test_a_cell_that_names_a_file_by_run_is_reduced_once(
    client: Client, sample: Measure
) -> None:
    def by_run(files: Sequence[FileInfo]) -> list[Row]:
        return [
            replace(row, values={**row.values, 'sample': file(run=int(row.key))})
            for row in pair(files)
        ]

    loop = _loop(client, by_run)
    run = sample(1, 'water')

    (record,) = loop.step().values()
    client.wait(record)

    assert record.request.params['sample'] == run
    assert loop.step() == {}


def test_a_new_loop_of_any_submitter_reduces_only_the_rows_without_a_result(
    loop: Automatic, client: Client, other: Client, sample: Measure
) -> None:
    sample(1, 'water')
    client.wait(loop.step())
    sample(2, 'sds')

    batch = Batch(other, STAGED, columns=COLUMNS, label='scan', source=pair)
    restarted = Automatic(batch, at={'sample'})

    assert list(restarted.step()) == ['2']


def _with_first_transmission(files: Sequence[FileInfo]) -> list[Row]:
    """A row per sample run, each with the first transmission run."""
    transmission = next(f.ref for f in files if f.metadata['role'] == 'transmission')
    return [
        Row(str(f.metadata['run']), {'sample': f.ref, 'transmission': transmission})
        for f in files
        if f.metadata['role'] == 'sample'
    ]


def test_a_restart_on_the_same_folder_reduces_nothing_again(
    folder: Path, folder_bindings: list[Binding]
) -> None:
    def loop(client: Client) -> Automatic:
        batch = Batch(
            client,
            PLAIN,
            columns=COLUMNS,
            label='auto',
            source=_with_first_transmission,
            persist=True,
        )
        return Automatic(batch, at=set(COLUMNS))

    with open_folder(folder, folder_bindings) as first:
        reduced = loop(first).step()
        first.wait(list(reduced.values()))
    assert sorted(reduced) == ['1', '2', '3']

    with open_folder(folder, folder_bindings) as second:
        assert loop(second).step() == {}


def test_refusals_that_only_submit_finds_are_tried_again_at_every_step(
    client: Client, sample: Measure
) -> None:
    found = client.compute(FIND, params={'sample': sample(1, 'water')})
    client.release(found)
    unknown = found.ref('offset').model_copy(update={'record': '0' * 32})
    offsets = {'2': found.ref('offset'), '3': unknown}

    def with_offset(files: Sequence[FileInfo]) -> list[Row]:
        return [
            replace(row, values={**row.values, 'offset': offsets[row.key]})
            if row.key in offsets
            else row
            for row in pair(files)
        ]

    batch = Batch(
        client, PLAIN, columns=(*COLUMNS, 'offset'), label='scan', source=with_offset
    )
    loop = Automatic(batch, at={'sample'})
    for run, name in [(2, 'sds'), (3, 'dmso'), (4, 'd2o')]:
        sample(run, name)
    refused = {
        '2': f'2: offset: record {found.id} is not kept',
        '3': f'3: offset: unknown record or accumulator {unknown.record}',
    }

    assert list(loop.step()) == ['1', '4']
    assert loop.refused == refused
    assert batch.state()['2'].text == 'not reduced'
    assert loop.step() == {}
    assert loop.refused == refused


class Submits:
    """Delegates to a client and counts the calls of ``submit``, at most three."""

    def __init__(self, client: Client) -> None:
        self._client = client
        self.calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def submit(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        if self.calls > 3:
            raise RuntimeError('submitted again and again')
        return self._client.submit(*args, **kwargs)


def test_a_refusal_that_names_no_row_refuses_every_row_of_the_step(
    files: CorrectedFiles, staged_binding: StagedFunction, sample: Measure
) -> None:
    bind = [function_binding(PLAIN, staged_binding.function)]
    with closing(Workspace(proposal='p1', files=files, bind=bind)) as storeless:
        submits = Submits(Client(storeless, submitter='anna'))
        batch = Batch(
            submits, PLAIN, columns=COLUMNS, label='scan', source=pair, persist=True
        )
        loop = Automatic(batch, at={'sample'})
        sample(1, 'water')
        sample(2, 'sds')

        assert loop.step() == {}
        assert loop.refused == dict.fromkeys(
            ['1', '2'], 'persisting needs a store, which this workspace lacks'
        )
        assert submits.calls == 1


def test_a_cell_the_workspace_does_not_accept_makes_its_row_wait(
    client: Client, sample: Measure
) -> None:
    thickness = -1.0

    def thick(files: Sequence[FileInfo]) -> list[Row]:
        return [
            replace(row, values={**row.values, 'thickness': thickness})
            if row.key == '2'
            else row
            for row in pair(files)
        ]

    loop = _loop(client, thick)
    sample(1, 'water')
    sample(2, 'sds')

    assert list(loop.step()) == ['1']
    assert loop.refused == {}
    assert loop.batch.state()['2'].text == 'incomplete'

    thickness = 1.0

    assert list(loop.step()) == ['2']


def test_a_row_changed_by_hand_waits_for_a_person_then_follows_the_files(
    client: Client, sample: Measure, fake: FakeFiles
) -> None:
    sample(1, 'water')
    other = fake.measure(77, 7.0, role='sample', sample='other')
    loop = Automatic(_loop(client).batch, at=set(COLUMNS))
    batch = loop.batch
    client.wait(loop.step())
    batch.rows = batch.rows.edit('1', {'sample': other})

    assert loop.step() == {}
    assert batch.state()['1'].text == 'outdated'

    client.wait(batch.reduce(['1']))
    transmission = fake.measure(1005, 2.0, role='transmission', sample='water')
    (record,) = loop.step().values()

    assert record.request.params['sample'] == other
    assert record.request.params['transmission'] == transmission


@pytest.mark.parametrize(
    ('by_hand', 'reduced'),
    [
        (lambda rows: rows.edit('2', {'sample': rows['2'].values['sample']}), ['1']),
        (lambda rows: rows.edit('2', {'thickness': EMPTY}), ['1']),
        (lambda rows: rows.add(Row('3', dict(rows['2'].values))), ['1', '2']),
    ],
    ids=['the value of the scan', 'cleared', 'added'],
)
def test_a_row_set_by_hand_without_a_result_waits_for_a_person(
    client: Client, sample: Measure, by_hand: Any, reduced: list[str]
) -> None:
    loop = _loop(client)
    sample(1, 'water')
    sample(2, 'oil')
    loop.batch.scan()
    loop.batch.rows = by_hand(loop.batch.rows)

    assert list(loop.step()) == reduced


def test_a_row_reverted_to_the_scan_follows_the_files_again(
    client: Client, sample: Measure, fake: FakeFiles
) -> None:
    run = sample(1, 'water')
    other = fake.measure(77, 7.0, role='sample', sample='other')
    loop = _loop(client)
    batch = loop.batch
    batch.scan()
    batch.rows = batch.rows.edit('1', {'sample': other})
    client.wait(batch.reduce(['1']))
    assert loop.step() == {}

    batch.rows = batch.rows.reset('1')
    (record,) = loop.step().values()

    assert record.request.params['sample'] == run


def test_a_record_of_another_batch_on_the_label_is_replaced_if_it_differs_at_at(
    loop: Automatic, client: Client, sample: Measure, fake: FakeFiles
) -> None:
    run = sample(1, 'water')
    client.wait(loop.step())
    other = fake.measure(77, 7.0, role='sample', sample='other')
    page = Batch(client, STAGED, columns=COLUMNS, label='scan', source=pair)
    page.scan()
    page.rows = page.rows.edit('1', {'sample': other})
    client.wait(page.reduce(['1']))

    (record,) = loop.step().values()

    assert record.request.params['sample'] == run


def test_without_a_store_a_row_edited_at_at_is_reduced_again_after_a_restart(
    client: Client, sample: Measure, fake: FakeFiles
) -> None:
    run = sample(1, 'water')
    other = fake.measure(77, 7.0, role='sample', sample='other')
    loop = _loop(client)
    client.wait(loop.step())
    loop.batch.rows = loop.batch.rows.edit('1', {'sample': other})
    (edited,) = loop.batch.reduce(['1']).values()
    client.wait(edited)

    (record,) = _loop(client).step().values()

    assert edited.request.params['sample'] == other
    assert record.request.params['sample'] == run


def test_with_a_store_a_row_edited_at_at_keeps_its_edit_after_a_restart(
    client: Client, sample: Measure, fake: FakeFiles, tmp_path: Path
) -> None:
    store = FolderDefinitions(tmp_path)
    sample(1, 'water')
    other = fake.measure(77, 7.0, role='sample', sample='other')
    loop = _loop(client, definitions=store)
    client.wait(loop.step())
    loop.batch.rows = loop.batch.rows.edit('1', {'sample': other})
    client.wait(loop.batch.reduce(['1']))

    restarted = _loop(client, definitions=store)

    assert restarted.step() == {}
    assert restarted.batch.rows['1'].values['sample'] == other


def test_a_row_whose_key_changes_is_reduced_under_its_new_key(
    client: Client, sample: Measure, files: CorrectedFiles
) -> None:
    def by_name(files: Sequence[FileInfo]) -> list[Row]:
        return [replace(row, key=row.info['name']) for row in pair(files)]

    loop = _loop(client, by_name)
    run = sample(1, 'water')
    client.wait(loop.step())

    files.correct(run, sample='oil')
    files.correct(FileRef(file='uuid:run-1001'), sample='oil')

    assert list(loop.step()) == ['oil']


def test_a_list_at_at_in_another_order_reduces_the_row_again(
    client: Client, fake: FakeFiles
) -> None:
    reverse = False

    def runs(files: Sequence[FileInfo]) -> list[Row]:
        ordered = sorted(files, key=lambda f: f.metadata['run'], reverse=reverse)
        return [Row('si', {'runs': [f.ref for f in ordered]})]

    batch = Batch(client, SERIES, columns=('runs',), label='series', source=runs)
    loop = Automatic(batch, at={'runs'})
    fake.measure(1, 1.0, sample='si')
    fake.measure(2, 2.0, sample='si')
    client.wait(loop.step())
    assert loop.step() == {}

    reverse = True

    assert list(loop.step()) == ['si']


def test_refused_settings_refuse_every_ready_row_until_they_change(
    batch: Batch, client: Client, sample: Measure
) -> None:
    loop = Automatic(batch, at={'sample'})
    sample(1, 'water')
    client.wait(loop.step())
    sample(2, 'sds')
    sample(3, 'dmso', partner=False)
    sample(4, 'd2o')
    batch.scan()
    batch.rows = batch.rows.edit('4', {'thickness': 2.0})  # left to a person

    batch.settings = {'binning': {'bins': 0}}
    assert loop.step() == {}
    assert loop.refused == dict.fromkeys(
        ['1', '2'], 'binning.bins: Input should be greater than or equal to 1'
    )

    batch.settings = SETTINGS
    assert list(loop.step()) == ['2']
    assert loop.refused == {}


def test_a_changed_setting_reduces_no_row_again_and_its_rows_read_outdated(
    batch: Batch, client: Client, sample: Measure
) -> None:
    loop = Automatic(batch, at={'sample'})
    sample(1, 'water')
    client.wait(loop.step())

    batch.settings = {'binning': {'bins': 3}}
    sample(2, 'sds')

    (record,) = loop.step().values()
    assert record.member == '2'
    assert record.request.params['binning'] == {'bins': 3}
    old = batch.state()['1']
    assert (old.text, old.changed) == ('outdated', {'binning'})


def test_a_pending_row_is_reduced_once_its_record_finishes_if_a_file_arrived(
    series: Automatic, client: Client, fake: FakeFiles, gate: Gate
) -> None:
    gate.close()
    fake.measure(1, 1.0, sample='si')
    first = series.step()
    fake.measure(2, 2.0, sample='si')
    assert series.step() == {}
    assert series.refused == {}

    gate.open()
    client.wait(first)

    (record,) = series.step().values()
    assert len(record.request.params['runs']) == 2


@pytest.mark.parametrize(
    ('at', 'reduced'), [({'sample'}, []), ({'sample', 'transmission'}, ['1'])]
)
def test_a_result_of_another_spec_counts_at_the_columns_it_holds(
    batch: Batch, client: Client, sample: Measure, at: set[str], reduced: list[str]
) -> None:
    run = sample(1, 'water')
    client.wait(client.submit(FIND, params={'sample': run}, label='scan', member='1'))
    batch.scan()

    state = batch.state()['1']
    assert (state.text, state.spec_changed) == ('outdated', True)
    assert state.changed == {'binning', 'transmission', 'thickness', 'offset'}
    loop = Automatic(batch, at=at)
    assert list(loop.step()) == reduced


def _samples(files: Sequence[FileInfo]) -> list[Row]:
    return [
        Row(str(f.metadata['run']), {'sample': f.ref})
        for f in files
        if f.metadata.get('role') == 'sample'
    ]


def test_a_row_reads_the_record_of_its_row_in_a_batch_stepped_before(
    client: Client, sample: Measure
) -> None:
    find = Batch(client, FIND, columns=('sample',), label='find', source=_samples)

    def with_offset(files: Sequence[FileInfo]) -> list[Row]:
        found = {k: s.record for k, s in find.state().items() if s.record}
        return [
            replace(row, values={**row.values, 'offset': found[row.key].ref('offset')})
            if row.key in found
            else row
            for row in pair(files)
        ]

    reduce = Batch(
        client, PLAIN, columns=(*COLUMNS, 'offset'), label='reduce', source=with_offset
    )
    loops = [
        Automatic(find, at={'sample'}),
        Automatic(reduce, at={'sample'}),
    ]
    sample(1, 'water')

    found, reduced = (loop.step() for loop in loops)

    assert reduced['1'].request.params['offset'] == found['1'].ref('offset')
    assert client.wait(reduced['1']) == 'completed'
    assert client.output(reduced['1'], 'total') == 2.0


class Flaky:
    """A client whose next ``fail`` submissions raise, as a lost connection does."""

    def __init__(self, client: Client) -> None:
        self._client = client
        self.fail = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def submit(self, *args: Any, **kwargs: Any) -> Any:
        if self.fail:
            self.fail -= 1
            raise ConnectionError('lost')
        return self._client.submit(*args, **kwargs)


def test_a_row_whose_submission_raised_runs_at_the_next_step(
    client: Client, sample: Measure
) -> None:
    flaky = Flaky(client)
    loop = _loop(flaky)
    sample(1, 'water')
    flaky.fail = 1
    with pytest.raises(ConnectionError):
        loop.step()

    assert list(loop.step()) == ['1']
    assert loop.step() == {}


class Raising:
    """Calls ``function``, or raises while ``on`` is set."""

    def __init__(self, function: Any) -> None:
        self.function = function
        self.on = False

    def __call__(self, *args: Any) -> Any:
        if self.on:
            raise RuntimeError('unreadable')
        return self.function(*args)


def test_a_step_that_raises_as_a_running_record_finishes_loses_nothing(
    client: Client, fake: FakeFiles, gate: Gate
) -> None:
    source = Raising(_angles)
    batch = Batch(client, SERIES, columns=('runs',), label='series', source=source)
    loop = Automatic(batch, at={'runs'})
    gate.close()
    fake.measure(1, 1.0, sample='si')
    first = loop.step()
    fake.measure(2, 2.0, sample='si')
    assert loop.step() == {}  # still running
    gate.open()
    client.wait(first)

    source.on = True
    with pytest.raises(RuntimeError):
        loop.step()
    source.on = False

    (record,) = loop.step().values()
    assert len(record.request.params['runs']) == 2


def test_two_loops_on_one_label_submit_a_file_once(
    client: Client, sample: Measure
) -> None:
    first, second = _loop(client), _loop(client)
    sample(1, 'water')

    assert list(first.step()) == ['1']
    assert second.step() == {}


def test_reduce_without_a_scan_reduces_the_rows_that_arrived_at_the_last_scan(
    loop: Automatic, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    loop.batch.scan()
    sample(2, 'oil')

    records = loop.reduce()

    assert list(records) == ['1']
    client.wait(list(records.values()))


# One loop of Follow


@pytest.fixture
def follow(manager: BatchManager, refusing: Refusing) -> Follow:
    manager.create('plain', title='A', source='pairs', label='a')
    manager.create('plain', title='B', source='pairs', label='b')
    return Follow(manager, refusing)


def _reduced(client: Client) -> list[tuple[str | None, str | None]]:
    return [(r.label, r.member) for r in client.records()]


def test_follow_steps_only_the_batch_that_follows(
    follow: Follow, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    follow.on('a')

    client.wait(follow.step())

    assert _reduced(client) == [('a', '1')]


def test_turning_follow_on_for_one_batch_turns_it_off_for_the_other(
    follow: Follow, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    follow.on('a')
    follow.on('b')

    client.wait(follow.step())

    assert follow.label == 'b'
    assert _reduced(client) == [('b', '1')]


def test_the_batch_that_follows_is_kept_and_a_follow_made_again_resumes(
    follow: Follow,
    manager: BatchManager,
    definitions: FolderDefinitions,
    client: Client,
    sample: Measure,
) -> None:
    follow.on('b')
    sample(1, 'water')

    again = Follow(manager, definitions)
    client.wait(again.step())

    assert again.label == 'b'
    assert _reduced(client) == [('b', '1')]


def test_follow_turns_itself_off_when_its_batch_is_removed(
    follow: Follow,
    manager: BatchManager,
    definitions: FolderDefinitions,
    sample: Measure,
) -> None:
    follow.on('a')
    manager.remove('a')
    sample(1, 'water')

    assert follow.step() == {}
    assert follow.label is None
    assert Follow(manager, definitions).label is None


def test_follow_refuses_a_batch_without_a_source(
    follow: Follow, manager: BatchManager
) -> None:
    follow.on('a')
    manager.create('plain', title='C', columns=COLUMNS, label='c')

    with pytest.raises(ValueError, match='no source'):
        follow.on('c')
    with pytest.raises(KeyError):
        follow.on('d')
    assert follow.label == 'a'


def test_follow_keeps_its_label_when_the_definitions_refuse_a_change(
    follow: Follow, refusing: Refusing
) -> None:
    follow.on('a')
    refusing.part = 'following'

    with pytest.raises(NotKept):
        follow.on('b')
    with pytest.raises(NotKept):
        follow.off()
    assert follow.label == 'a'


def test_follow_says_why_rows_of_its_last_step_were_refused_until_switched(
    follow: Follow, manager: BatchManager, sample: Measure
) -> None:
    sample(1, 'water')
    follow.on('a')
    manager.batch('a').settings = {'binning': {'bins': 0}}

    assert follow.step() == {}
    assert list(follow.refused) == ['1']
    follow.on('a')
    assert follow.refused == {}
    follow.step()
    follow.off()
    assert follow.refused == {}


def test_follow_off_steps_nothing(
    follow: Follow, client: Client, sample: Measure
) -> None:
    follow.on('a')
    follow.off()
    sample(1, 'water')

    assert follow.step() == {}
    assert follow.label is None
    assert _reduced(client) == []


def test_every_calls_the_actions_in_turn_until_the_block_ends() -> None:
    called: list[int] = []
    all_called = threading.Event()

    def call(i: int) -> None:
        called.append(i)
        if i == 2:
            all_called.set()

    with every(0, [functools.partial(call, i) for i in range(3)]):
        assert all_called.wait(10)
    with every(60, [functools.partial(call, 3)]):
        pass

    assert called == [0, 1, 2]
