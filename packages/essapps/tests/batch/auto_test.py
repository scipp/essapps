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
    Row,
    RowSource,
    Scope,
    Source,
)
from ess.apps.batch.auto import Automatic, every
from ess.apps.folder import open_folder
from ess.apps.testing import FakeFiles, StagedFunction
from ess.spec import Binding, FileRef, function_binding

from .toys import (
    COLUMNS,
    FIND,
    PAIRS,
    PLAIN,
    REPEATED,
    SAMPLES,
    SERIES,
    STAGED,
    Gate,
    Measure,
    pair,
)

SETTINGS: dict[str, Any] = {'binning': {'bins': 2}}


@pytest.fixture
def loop(batch: Batch) -> Automatic:
    return Automatic(batch)


def _loop(
    client: Any, rows: RowSource = pair, definitions: DefinitionStore | None = None
) -> Automatic:
    """A loop on ``client``, which may be a fake that wraps a client."""
    batch = Batch(
        client,
        PLAIN,
        columns=COLUMNS,
        label='scan',
        source=replace(PAIRS, rows=rows),
        definitions=definitions,
    )
    return Automatic(batch)


def _angles(files: Sequence[FileInfo]) -> list[Row]:
    """A row per sample: all its runs so far, in run order."""
    runs: dict[str, list[Any]] = {}
    for f in sorted(files, key=lambda f: f.metadata['run']):
        runs.setdefault(f.metadata['sample'], []).append(f.ref)
    return [Row(name, {'runs': refs}) for name, refs in runs.items()]


ANGLES = Source(_angles, fills=('runs',))


@pytest.fixture
def series(client: Client) -> Automatic:
    batch = Batch(client, SERIES, columns=('runs',), label='series', source=ANGLES)
    return Automatic(batch)


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


def _repeated(client: Client) -> Automatic:
    batch = Batch(
        client,
        REPEATED,
        columns=('sample_runs', 'trans_runs'),
        label='repeated',
        source=Source(_per_sample, fills=('sample_runs', 'trans_runs')),
    )
    return Automatic(batch)


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


def test_a_row_is_reduced_once_and_not_again_when_its_files_change(
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
        '1': 'outdated',
        '2': 'completed',
    }


def test_a_row_with_an_empty_cell_waits_and_its_state_says_why(
    client: Client, fake: FakeFiles
) -> None:
    loop = _repeated(client)
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
    sample(3, 'water', partner=False)

    assert list(first) == ['1', '2']
    assert list(loop.step()) == ['3']
    assert loop.step() == {}


def test_the_batch_must_have_a_source(client: Client) -> None:
    batch = Batch(client, PLAIN, columns=COLUMNS, label='scan')

    with pytest.raises(ValueError, match='no source'):
        Automatic(batch)


def test_a_failed_row_is_left_to_a_person_also_when_its_files_change(
    series: Automatic, client: Client, fake: FakeFiles
) -> None:
    fake.corrupt(fake.measure(1, 1.0, sample='si'))
    assert client.wait(series.step()) == {'si': 'failed'}

    fake.measure(2, 2.0, sample='si')

    assert series.step() == {}


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

    batch = Batch(other, STAGED, columns=COLUMNS, label='scan', source=PAIRS)
    restarted = Automatic(batch)

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
            source=replace(PAIRS, rows=_with_first_transmission),
            persist=True,
        )
        return Automatic(batch)

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
        client,
        PLAIN,
        columns=(*COLUMNS, 'offset'),
        label='scan',
        source=Source(with_offset, fills=(*PAIRS.fills, 'offset')),
    )
    loop = Automatic(batch)
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
            submits, PLAIN, columns=COLUMNS, label='scan', source=PAIRS, persist=True
        )
        loop = Automatic(batch)
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


@pytest.mark.parametrize(
    ('by_hand', 'reduced'),
    [
        (
            lambda rows: rows.edit('2', {'sample': rows['2'].values['sample']}),
            ['1', '2'],
        ),
        (lambda rows: rows.edit('2', {'thickness': EMPTY}), ['1', '2']),
        (lambda rows: rows.add(Row('3', dict(rows['2'].values))), ['1', '2', '3']),
    ],
    ids=['the value of the scan', 'cleared', 'added'],
)
def test_a_row_set_by_hand_without_a_result_is_reduced_by_the_next_step(
    client: Client, sample: Measure, by_hand: Any, reduced: list[str]
) -> None:
    loop = _loop(client)
    sample(1, 'water')
    sample(2, 'oil')
    loop.batch.scan()
    loop.batch.rows = by_hand(loop.batch.rows)

    assert list(loop.step()) == reduced


def test_a_row_with_a_result_is_left_to_a_person_also_when_set_by_hand(
    client: Client, sample: Measure, fake: FakeFiles
) -> None:
    sample(1, 'water')
    other = fake.measure(77, 7.0, role='sample', sample='other')
    loop = _loop(client)
    batch = loop.batch
    client.wait(loop.step())
    batch.rows = batch.rows.edit('1', {'sample': other})

    assert loop.step() == {}
    assert batch.state()['1'].text == 'outdated'


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


def test_refused_settings_refuse_every_new_ready_row_until_they_change(
    batch: Batch, client: Client, sample: Measure
) -> None:
    loop = Automatic(batch)
    sample(1, 'water')
    client.wait(loop.step())
    sample(2, 'sds')
    sample(3, 'dmso', partner=False)
    sample(4, 'd2o')

    batch.settings = {'binning': {'bins': 0}}
    assert loop.step() == {}
    assert loop.refused == dict.fromkeys(
        ['2', '4'], 'binning.bins: Input should be greater than or equal to 1'
    )

    batch.settings = SETTINGS
    assert list(loop.step()) == ['2', '4']
    assert loop.refused == {}


def test_a_changed_setting_reduces_no_row_again_and_its_rows_read_outdated(
    batch: Batch, client: Client, sample: Measure
) -> None:
    loop = Automatic(batch)
    sample(1, 'water')
    client.wait(loop.step())

    batch.settings = {'binning': {'bins': 3}}
    sample(2, 'sds')

    (record,) = loop.step().values()
    assert record.member == '2'
    assert record.request.params['binning'] == {'bins': 3}
    old = batch.state()['1']
    assert (old.text, old.changed) == ('outdated', {'binning'})


def test_a_row_reads_the_record_of_its_row_in_a_batch_stepped_before(
    client: Client, sample: Measure
) -> None:
    find = Batch(client, FIND, columns=('sample',), label='find', source=SAMPLES)

    def with_offset(files: Sequence[FileInfo]) -> list[Row]:
        found = {k: s.record for k, s in find.state().items() if s.record}
        return [
            replace(row, values={**row.values, 'offset': found[row.key].ref('offset')})
            if row.key in found
            else row
            for row in pair(files)
        ]

    reduce = Batch(
        client,
        PLAIN,
        columns=(*COLUMNS, 'offset'),
        label='reduce',
        source=Source(with_offset, fills=(*PAIRS.fills, 'offset')),
    )
    loops = [Automatic(find), Automatic(reduce)]
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
    batch = Batch(
        client,
        SERIES,
        columns=('runs',),
        label='series',
        source=replace(ANGLES, rows=source),
    )
    loop = Automatic(batch)
    gate.close()
    fake.measure(1, 1.0, sample='si')
    first = loop.step()
    fake.measure(2, 2.0, sample='ge')
    gate.open()
    client.wait(first)

    source.on = True
    with pytest.raises(RuntimeError):
        loop.step()
    source.on = False

    assert list(loop.step()) == ['ge']


def test_two_loops_on_one_label_submit_a_file_once(
    client: Client, sample: Measure
) -> None:
    first, second = _loop(client), _loop(client)
    sample(1, 'water')

    assert list(first.step()) == ['1']
    assert second.step() == {}


def test_a_loop_reduces_the_rows_of_the_scope_and_those_arriving_in_it(
    client: Client, sample: Measure
) -> None:
    batch = Batch(
        client, PLAIN, columns=COLUMNS, label='scan', source=PAIRS, scope=Scope(start=2)
    )
    loop = Automatic(batch)
    sample(1, 'water')
    sample(2, 'oil')

    first = loop.step()
    sample(3, 'sds')
    second = loop.step()

    assert (list(first), list(second)) == (['2'], ['3'])


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


def test_widening_the_scope_of_a_batch_of_the_manager_reduces_the_rows_it_now_holds(
    manager: BatchManager, client: Client, sample: Measure
) -> None:
    for run, name in enumerate(['water', 'oil', 'sds'], start=1):
        sample(run, name)
    manager.create('plain', title='A', source='pairs', scope=Scope(start=3), label='a')
    loop = Automatic(manager.batch('a'))
    client.wait(loop.step())

    manager.change('a', scope=Scope())
    records = loop.step()

    client.wait(records)
    assert sorted(records) == ['1', '2']
