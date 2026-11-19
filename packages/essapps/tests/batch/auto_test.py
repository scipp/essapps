# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Automatic reduction: which rows a step adds and reduces, and why a row did not
run; what a source makes.
"""

import functools
import threading
from collections.abc import Sequence
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from ess.apps import Client, Workspace
from ess.apps.batch import (
    Batch,
    BatchManager,
    CorrectedFiles,
    FileInfo,
    FolderDefinitions,
    RowSource,
    Source,
    rows_of,
)
from ess.apps.batch.auto import Automatic, added, every
from ess.apps.batch.definition import source_key, source_of
from ess.apps.batch.table import KEY, cell_of
from ess.apps.folder import open_folder
from ess.apps.testing import FakeFiles, StagedFunction
from ess.spec import Binding, FileRef, function_binding

from .toys import (
    FIND,
    PAIRS,
    PLAIN,
    REPEATED,
    SAMPLES,
    STAGED,
    Measure,
    edit,
    pair,
    rows,
)

SETTINGS: dict[str, Any] = {'binning': {'bins': 2}}


@pytest.fixture
def loop(batch: Batch) -> Automatic:
    return Automatic(batch, PAIRS)


def _loop(client: Any, make: RowSource = pair) -> Automatic:
    """A loop on ``client``, which may be a fake that wraps a client."""
    batch = Batch(client, PLAIN, label='scan', rows=rows(client))
    return Automatic(batch, replace(PAIRS, rows=make))


def _changed(make: RowSource, **cells: Any) -> RowSource:
    """``make``, with ``cells`` set in each row that it makes, by key."""

    def changed(files: Sequence[FileInfo]) -> pd.DataFrame:
        frame = make(files)
        for key, row in cells.items():
            for column, cell in row.items():
                if column not in frame.columns:
                    frame[column] = pd.Series(None, index=frame.index, dtype=object)
                if key in frame.index:
                    frame.loc[key, column] = cell
        return frame

    return changed


# What a source makes


def test_a_source_makes_a_table_with_a_column_for_each_it_fills(
    client: Client, sample: Measure
) -> None:
    empty = rows_of(client, SAMPLES)
    sample(1, 'water')

    made = rows_of(client, replace(PAIRS, fills=(*PAIRS.fills, 'offset')))

    assert (empty.index.name, list(empty.columns)) == (KEY, ['sample'])
    assert list(made.index) == ['1']
    assert list(made.columns) == ['run', 'name', 'sample', 'transmission', 'offset']
    assert made.loc['1', 'transmission'] == 'uuid:run-1001'
    assert made['offset'].isna().all()


def test_a_source_that_makes_a_key_twice_is_refused(
    client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    twice = Source(lambda files: pd.concat([pair(files)] * 2), fills=PAIRS.fills)

    with pytest.raises(ValueError, match="the key '1' is given twice"):
        rows_of(client, twice)


# Which rows a step adds


def table(**rows: dict[str, Any]) -> pd.DataFrame:
    frame = pd.DataFrame.from_dict(rows, orient='index')
    return frame.rename_axis(KEY)


def test_a_step_adds_the_rows_new_and_filled_with_the_columns_of_the_table() -> None:
    held = table(a={'sample': 's', 'transmission': 't'})
    given = table(
        a={'sample': 's2', 'transmission': 't', 'note': 1},
        b={'sample': 's', 'transmission': 't', 'note': 2},
        c={'sample': 's', 'transmission': None, 'note': 3},
        d={'sample': 's', 'transmission': 't', 'note': 4},
    )

    new, waiting = added(held, given, ('sample', 'transmission', 'offset'), {'d'})

    assert new.to_dict('index') == {'b': {'sample': 's', 'transmission': 't'}}
    assert list(waiting) == ['c']


def test_a_row_waits_until_it_is_filled_and_is_not_in_the_table(
    client: Client, fake: FakeFiles
) -> None:
    loop = _repeated(client)
    fake.measure(1, 1.0, role='sample', sample='1')

    assert loop.step() == {}
    assert loop.waiting == ('1',)
    assert list(loop.batch.frame().index) == []


def _per_sample(files: Sequence[FileInfo]) -> pd.DataFrame:
    """
    A row per sample, with its sample and transmission runs so far, in run
    order; ``trans_runs`` is empty until a transmission run arrives.
    """
    runs: dict[str, dict[str, list[str]]] = {}
    for f in sorted(files, key=lambda f: f.metadata['run']):
        column = 'trans_runs' if f.metadata['role'] == 'transmission' else 'sample_runs'
        runs.setdefault(f.metadata['sample'], {}).setdefault(column, []).append(
            str(f.ref)
        )
    frame = pd.DataFrame.from_dict(runs, orient='index')
    return frame.rename_axis(KEY)


def _repeated(client: Client) -> Automatic:
    source = Source(_per_sample, fills=('sample_runs', 'trans_runs'))
    batch = Batch(client, REPEATED, label='repeated', rows=rows_of(client, source))
    return Automatic(batch, source)


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


def test_a_row_is_added_once_filled_and_not_changed_when_its_files_change(
    client: Client, fake: FakeFiles
) -> None:
    loop = _repeated(client)
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

    assert {name: rows for name, rows in seen.items() if rows} == {
        't1': {'1': (['s1'], ['t1'])},
        't2': {'2': (['s2'], ['t2'])},
    }
    state = loop.batch.state()
    assert {key: s.text for key, s in state.items()} == {
        '1': 'completed',
        '2': 'completed',
    }


def test_each_new_row_is_reduced_once(
    loop: Automatic, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    sample(2, 'sds')
    first = loop.step()
    client.wait(first)
    sample(3, 'water', partner=False)

    assert list(first) == ['1', '2']
    assert list(loop.step()) == ['3']
    assert loop.step() == {}
    assert loop.considered == {'1', '2', '3'}


def test_a_step_adds_no_row_that_is_considered(client: Client, sample: Measure) -> None:
    sample(1, 'water')
    sample(2, 'sds')
    batch = Batch(client, PLAIN, label='scan', rows=rows(client).drop('1'))
    loop = Automatic(batch, PAIRS, considered={'1'})

    assert list(loop.step()) == ['2']
    assert list(batch.frame().index) == ['2']


@pytest.mark.parametrize(
    ('change', 'text'),
    [
        ('a newer transmission run', 'completed'),
        ('a cell set by a person', 'outdated'),
        ('its runs arrive again', 'completed'),
        ('its sample named by run', 'completed'),
        ('its reduction failed', 'failed'),
    ],
)
def test_a_row_with_a_result_is_left_to_a_person(
    client: Client, sample: Measure, fake: FakeFiles, change: str, text: str
) -> None:
    by_run = _changed(pair, **{'1': {'sample': 'run:1'}})
    loop = _loop(client, by_run if change == 'its sample named by run' else pair)
    run = sample(1, 'water')
    if change == 'its reduction failed':
        fake.corrupt(run)
    client.wait(loop.step())

    if change == 'a newer transmission run':
        fake.measure(1005, 2.0, role='transmission', sample='water')
    elif change == 'a cell set by a person':
        other = fake.measure(77, 7.0, role='sample', sample='other')
        edit(loop.batch, '1', sample=cell_of(other))
    elif change == 'its runs arrive again':
        sample(1, 'water')

    assert loop.step() == {}
    assert loop.batch.state()['1'].text == text


def test_a_new_loop_of_any_submitter_reduces_only_the_rows_without_a_result(
    loop: Automatic, client: Client, other: Client, sample: Measure
) -> None:
    sample(1, 'water')
    client.wait(loop.step())
    sample(2, 'sds')

    batch = Batch(other, STAGED, label='scan', rows=loop.batch.frame())
    restarted = Automatic(batch, PAIRS)

    assert list(restarted.step()) == ['2']


def _with_first_transmission(files: Sequence[FileInfo]) -> pd.DataFrame:
    """A row per sample run, each with the first transmission run."""
    transmission = next(f.ref for f in files if f.metadata['role'] == 'transmission')
    return (
        pd.DataFrame.from_records(
            [
                {KEY: str(f.metadata['run']), 'sample': str(f.ref)}
                for f in files
                if f.metadata['role'] == 'sample'
            ]
        )
        .set_index(KEY)
        .assign(transmission=str(transmission))
    )


def test_a_restart_on_the_same_folder_reduces_nothing_again(
    folder: Path, folder_bindings: list[Binding]
) -> None:
    source = replace(PAIRS, rows=_with_first_transmission)

    def loop(client: Client) -> Automatic:
        table = rows_of(client, source)
        batch = Batch(client, PLAIN, label='auto', rows=table, persist=True)
        return Automatic(batch, source)

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
    with_offset = _changed(
        pair, **{k: {'offset': cell_of(r)} for k, r in offsets.items()}
    )
    batch = Batch(client, PLAIN, label='scan', rows=rows(client).assign(offset=None))
    loop = Automatic(batch, replace(PAIRS, rows=with_offset))
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
        batch = Batch(submits, PLAIN, label='scan', rows=rows(submits), persist=True)  # type: ignore[arg-type]
        loop = Automatic(batch, PAIRS)
        sample(1, 'water')
        sample(2, 'sds')

        assert loop.step() == {}
        assert loop.refused == dict.fromkeys(
            ['1', '2'], 'persisting needs a store, which this workspace lacks'
        )
        assert submits.calls == 1


def test_a_row_whose_cell_is_refused_waits_for_a_person_to_set_it(
    client: Client, sample: Measure
) -> None:
    loop = _loop(client, _changed(pair, **{'2': {'thickness': -1.0}}))
    sample(1, 'water')
    sample(2, 'sds')

    assert list(loop.step()) == ['1']
    assert loop.refused == {}
    assert loop.batch.state()['2'].text == 'incomplete'
    assert loop.step() == {}

    edit(loop.batch, '2', thickness=1.0)

    assert list(loop.step()) == ['2']


def test_a_row_added_by_a_person_is_reduced_by_the_next_step(
    client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    loop = _loop(client)
    edit(loop.batch, 'mine', sample='run:1', transmission='run:1001')

    assert list(loop.step()) == ['1', 'mine']


def test_a_row_whose_key_changes_is_added_and_reduced_under_its_new_key(
    client: Client, sample: Measure, files: CorrectedFiles
) -> None:
    def by_name(files: Sequence[FileInfo]) -> pd.DataFrame:
        return pair(files).set_index('name', drop=False).rename_axis(KEY)

    loop = _loop(client, by_name)
    run = sample(1, 'water')
    client.wait(loop.step())

    files.correct(run, sample='oil')
    files.correct(FileRef(file='uuid:run-1001'), sample='oil')

    assert list(loop.step()) == ['oil']
    assert list(loop.batch.frame().index) == ['water', 'oil']


def test_refused_settings_refuse_every_ready_row_without_a_result_until_they_change(
    loop: Automatic, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    client.wait(loop.step())
    sample(2, 'sds')
    sample(3, 'dmso', partner=False)
    sample(4, 'd2o')

    loop.batch.settings = {'binning': {'bins': 0}}
    assert loop.step() == {}
    assert loop.refused == dict.fromkeys(
        ['2', '4'], 'binning.bins: Input should be greater than or equal to 1'
    )

    loop.batch.settings = SETTINGS
    assert list(loop.step()) == ['2', '4']
    assert loop.refused == {}


def test_a_changed_setting_reduces_no_row_again_and_its_rows_read_outdated(
    loop: Automatic, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    client.wait(loop.step())

    loop.batch.settings = {'binning': {'bins': 3}}
    sample(2, 'sds')

    (record,) = loop.step().values()
    assert record.member == '2'
    assert record.request.params['binning'] == {'bins': 3}
    old = loop.batch.state()['1']
    assert (old.text, old.changed) == ('outdated', {'binning'})


def test_a_row_reads_the_record_of_its_row_in_a_batch_stepped_before(
    client: Client, sample: Measure
) -> None:
    find = Batch(client, FIND, label='find', rows=rows_of(client, SAMPLES))

    def with_offset(files: Sequence[FileInfo]) -> pd.DataFrame:
        found = {k: s.record for k, s in find.state().items() if s.record}
        offsets = {k: {'offset': cell_of(r.ref('offset'))} for k, r in found.items()}
        return _changed(pair, **offsets)(files)

    source = Source(with_offset, fills=(*PAIRS.fills, 'offset'))
    reduce = Batch(client, PLAIN, label='reduce', rows=rows_of(client, source))
    loops = [Automatic(find, SAMPLES), Automatic(reduce, source)]
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


def test_two_loops_on_one_label_submit_a_file_once(
    client: Client, sample: Measure
) -> None:
    first, second = _loop(client), _loop(client)
    sample(1, 'water')

    assert list(first.step()) == ['1']
    assert second.step() == {}


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


# The example of ADR 0013: a batch made from a source with the rows from run 3
# on, which follows.


@pytest.fixture
def following(manager: BatchManager, client: Client, sample: Measure) -> BatchManager:
    """
    ``manager`` with runs 1 to 4 measured, each with its transmission run,
    and the batch ``a`` of the rows from run 3 on, which follows.
    """
    for run, name in enumerate(['water', 'sds', 'dmso', 'd2o'], start=1):
        sample(run, name)
    given = manager.rows_of('pairs')
    manager.create(
        PLAIN.name, title='A', rows=given[given.run >= 3], source='pairs', label='a'
    )
    manager.follow('a')
    return manager


def considered(definitions: FolderDefinitions) -> frozenset[str]:
    return definitions.load(source_key('a'), source_of).considered


def test_a_batch_made_from_a_source_considers_every_row_it_gave(
    following: BatchManager, client: Client, definitions: FolderDefinitions
) -> None:
    following.step()

    client.wait(client.records(label='a'))
    assert list(following.batch('a').frame().index) == ['3', '4']
    assert {r.member for r in client.records(label='a')} == {'3', '4'}
    assert considered(definitions) == {'1', '2', '3', '4'}


def test_a_row_of_a_sample_measured_before_its_transmission_waits_then_is_added(
    following: BatchManager,
    client: Client,
    fake: FakeFiles,
    sample: Measure,
    definitions: FolderDefinitions,
) -> None:
    sample(5, 'oil', partner=False)

    waits = following.step()
    fake.measure(1005, 1.0, role='transmission', sample='oil')
    adds = following.step()

    assert isinstance(waits, Automatic)
    assert isinstance(adds, Automatic)
    assert (waits.waiting, adds.waiting) == (('5',), ())
    assert list(following.batch('a').frame().index) == ['3', '4', '5']
    assert '5' in {r.member for r in client.records(label='a')}
    assert considered(definitions) == {'1', '2', '3', '4', '5'}


def test_a_row_that_a_person_removes_stays_away(following: BatchManager) -> None:
    batch = following.batch('a')
    batch.put(batch.frame().drop('4'))

    following.step()

    assert list(batch.frame().index) == ['3']


def test_a_step_after_a_restart_adds_the_rows_that_arrived_meanwhile(
    following: BatchManager,
    client: Client,
    definitions: FolderDefinitions,
    sample: Measure,
) -> None:
    following.step()
    sample(6, 'oil')
    again = BatchManager(client, sources=following.sources, definitions=definitions)

    again.step()

    assert again.following == 'a'
    assert list(again.batch('a').frame().index) == ['3', '4', '6']


def test_a_transmission_measured_again_changes_no_row(
    following: BatchManager, fake: FakeFiles
) -> None:
    following.step()
    fake.measure(2003, 1.0, role='transmission', sample='dmso')

    following.step()

    assert following.batch('a').frame().loc['3', 'transmission'] == 'uuid:run-1003'
    assert following.rows_of('pairs').loc['3', 'transmission'] == 'uuid:run-2003'
