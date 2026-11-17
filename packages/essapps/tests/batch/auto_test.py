# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Automatic reduction: which rows a step reduces, and why a row did not run."""

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import pytest

from ess.apps import Client, file
from ess.apps.batch import Batch, FileInfo, Row, RowSource
from ess.apps.batch.auto import Automatic
from ess.apps.testing import CountingClient, FakeFiles

from .toys import (
    COLUMNS,
    FIND,
    GROUPED,
    PLAIN,
    SERIES,
    STAGED,
    Gate,
    Measure,
    pair,
)

SETTINGS: dict[str, Any] = {'binning': {'bins': 2}}


@pytest.fixture
def loop(batch: Batch) -> Automatic:
    return Automatic(batch, at='sample', settings=lambda: SETTINGS)


def _loop(client: Any, source: RowSource = pair) -> Automatic:
    """A loop on ``client``, which may be a fake that wraps a client."""
    batch = Batch(client, PLAIN, columns=COLUMNS, label='scan', source=source)
    return Automatic(batch, at='sample', settings=lambda: SETTINGS)


def _angles(files: Sequence[FileInfo]) -> list[Row]:
    """A row per sample: all its runs so far, in run order."""
    runs: dict[str, list[Any]] = {}
    for f in sorted(files, key=lambda f: f.metadata['run']):
        runs.setdefault(f.metadata['sample'], []).append(f.ref)
    return [Row(name, {'runs': refs}) for name, refs in runs.items()]


@pytest.fixture
def series(client: Client) -> Automatic:
    batch = Batch(client, SERIES, columns=('runs',), label='series', source=_angles)
    return Automatic(batch, at='runs', settings=dict)


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


def test_the_trigger_column_must_be_a_column(batch: Batch) -> None:
    with pytest.raises(KeyError, match='offset'):
        Automatic(batch, at='offset', settings=dict)


def test_the_batch_must_have_a_source(client: Client) -> None:
    batch = Batch(client, PLAIN, columns=COLUMNS, label='scan')

    with pytest.raises(ValueError, match='no source'):
        Automatic(batch, at='sample', settings=dict)


def test_a_row_whose_record_failed_is_not_reduced_again(
    loop: Automatic, client: Client, sample: Measure, fake: FakeFiles
) -> None:
    fake.corrupt(sample(1, 'water'))
    assert client.wait(loop.step()) == {'1': 'failed'}

    assert loop.step() == {}
    assert loop.refused == {}


def test_a_new_partner_does_not_reduce_a_row_again(
    loop: Automatic, client: Client, sample: Measure, fake: FakeFiles
) -> None:
    sample(1, 'water')
    client.wait(loop.step())

    later = fake.measure(1002, 2.0, role='transmission', sample='water')

    assert loop.batch.rows['1'].values['transmission'] != later  # before the scan
    assert loop.step() == {}
    assert loop.batch.rows['1'].values['transmission'] == later


def test_a_file_that_arrives_again_is_not_reduced_again(
    loop: Automatic, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    client.wait(loop.step())

    sample(1, 'water')

    assert loop.step() == {}


def test_a_new_loop_of_any_submitter_reduces_only_what_no_record_holds(
    loop: Automatic, client: Client, other: Client, sample: Measure
) -> None:
    sample(1, 'water')
    client.wait(loop.step())
    sample(2, 'sds')

    batch = Batch(other, STAGED, columns=COLUMNS, label='scan', source=pair)
    restarted = Automatic(batch, at='sample', settings=lambda: SETTINGS)

    assert list(restarted.step()) == ['2']


def test_a_refused_row_does_not_hold_back_the_others(
    loop: Automatic, batch: Batch, sample: Measure
) -> None:
    for run, name in [(1, 'water'), (2, 'sds'), (3, 'dmso')]:
        sample(run, name)
    batch.scan()
    batch.rows = batch.rows.edit('2', {'thickness': -1.0})

    assert list(loop.step()) == ['1', '3']
    assert loop.refused == {
        '2': '2: thickness: Input should be greater than or equal to 0'
    }


def test_a_waiting_row_runs_once_its_partner_arrives(
    loop: Automatic, sample: Measure, fake: FakeFiles
) -> None:
    sample(1, 'water', partner=False)
    assert loop.step() == {}
    assert loop.refused == {'1': '1: transmission: empty'}

    fake.measure(1001, 1.0, role='transmission', sample='water')

    assert list(loop.step()) == ['1']
    assert loop.refused == {}


def test_rows_refused_for_the_settings_run_once_the_settings_change(
    batch: Batch, sample: Measure
) -> None:
    settings = {'binning': {'bins': 0}}
    loop = Automatic(batch, at='sample', settings=lambda: settings)
    sample(1, 'water')
    sample(2, 'sds')
    assert loop.step() == {}
    assert loop.refused == dict.fromkeys(
        ['1', '2'], 'binning.bins: Input should be greater than or equal to 1'
    )

    settings = SETTINGS

    assert list(loop.step()) == ['1', '2']
    assert loop.refused == {}


def test_a_changed_setting_applies_to_new_rows_only(
    batch: Batch, client: Client, sample: Measure
) -> None:
    settings = SETTINGS
    loop = Automatic(batch, at='sample', settings=lambda: settings)
    sample(1, 'water')
    client.wait(loop.step())

    settings = {'binning': {'bins': 3}}
    sample(2, 'sds')

    (record,) = loop.step().values()
    assert record.member == '2'
    assert record.request.params['binning'] == {'bins': 3}


def test_a_new_file_in_the_list_at_the_trigger_column_reduces_the_row_again(
    series: Automatic, client: Client, fake: FakeFiles
) -> None:
    runs = [fake.measure(1, 1.0, sample='si')]
    client.wait(series.step())
    runs += [fake.measure(n, float(n), sample='si') for n in (3, 2)]

    (record,) = series.step().values()
    client.wait(record)

    assert record.request.params['runs'] == [runs[0], runs[2], runs[1]]
    assert client.output(record, 'total') == 6.0
    assert series.step() == {}


def test_a_running_row_with_a_new_file_runs_once_its_record_finishes(
    series: Automatic, client: Client, fake: FakeFiles, gate: Gate
) -> None:
    gate.close()
    fake.measure(1, 1.0, sample='si')
    first = series.step()
    fake.measure(2, 2.0, sample='si')
    assert series.step() == {}
    assert series.refused == {'si': 'si: still running'}

    gate.open()
    client.wait(first)

    (record,) = series.step().values()
    assert len(record.request.params['runs']) == 2


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
        Automatic(find, at='sample', settings=dict),
        Automatic(reduce, at='sample', settings=lambda: SETTINGS),
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


@pytest.mark.parametrize('raising', ['settings', 'source'])
def test_a_step_that_raises_as_a_running_record_finishes_loses_nothing(
    client: Client, fake: FakeFiles, gate: Gate, raising: str
) -> None:
    settings, source = Raising(dict), Raising(_angles)
    batch = Batch(client, SERIES, columns=('runs',), label='series', source=source)
    loop = Automatic(batch, at='runs', settings=settings)
    gate.close()
    fake.measure(1, 1.0, sample='si')
    first = loop.step()
    fake.measure(2, 2.0, sample='si')
    assert loop.step() == {}  # still running
    gate.open()
    client.wait(first)

    (settings if raising == 'settings' else source).on = True
    with pytest.raises(RuntimeError):
        loop.step()
    settings.on = source.on = False

    (record,) = loop.step().values()
    assert len(record.request.params['runs']) == 2


def test_a_record_holding_the_file_at_another_parameter_does_not_count(
    loop: Automatic, client: Client, sample: Measure, fake: FakeFiles
) -> None:
    run = sample(1, 'water')
    other = fake.measure(5000, 1.0, role='transmission', sample='other')
    params = {'sample': other, 'transmission': run, **SETTINGS}
    client.wait(client.submit(PLAIN, params=params, label='scan', member='1'))

    assert list(loop.step()) == ['1']


def test_a_file_inside_a_group_at_the_trigger_column_does_not_count(
    client: Client, fake: FakeFiles
) -> None:
    def grouped(files: Sequence[FileInfo]) -> list[Row]:
        return [Row(str(f.metadata['run']), {'group': {'run': f.ref}}) for f in files]

    batch = Batch(client, GROUPED, columns=('group',), label='g', source=grouped)
    loop = Automatic(batch, at='group', settings=dict)
    fake.measure(1, 1.0)

    assert loop.step() == {}
    assert loop.refused == {}


def test_a_record_of_another_spec_with_the_rows_key_counts(
    loop: Automatic, client: Client, sample: Measure
) -> None:
    run = sample(1, 'water')
    client.wait(client.submit(FIND, params={'sample': run}, label='scan', member='1'))

    assert loop.step() == {}


def test_settings_changed_in_place_count_as_changed(
    batch: Batch, sample: Measure
) -> None:
    settings = {'binning': {'bins': 0}}
    loop = Automatic(batch, at='sample', settings=lambda: settings)
    sample(1, 'water')
    assert loop.step() == {}

    settings['binning']['bins'] = 2

    assert list(loop.step()) == ['1']


def test_an_idle_step_reads_no_records(client: Client, sample: Measure) -> None:
    counting = CountingClient(client)
    loop = _loop(counting)
    sample(1, 'water')
    client.wait(loop.step())
    loop.step()  # reads the record that finished
    counting.reads.clear()

    assert loop.step() == {}
    assert sum(counting.reads.values()) == 0


def test_a_step_does_not_try_a_refused_row_again(
    client: Client, sample: Measure
) -> None:
    counting = CountingClient(client)
    loop = _loop(counting)
    sample(1, 'water')
    client.wait(loop.step())
    sample(2, 'sds', partner=False)
    loop.step()  # refuses row 2, and reads the record of row 1
    counting.reads.clear()

    assert loop.step() == {}
    assert loop.refused == {'2': '2: transmission: empty'}
    assert sum(counting.reads.values()) == 0


def test_two_loops_on_one_label_submit_a_file_once(
    client: Client, sample: Measure
) -> None:
    first, second = _loop(client), _loop(client)
    sample(1, 'water')

    assert list(first.step()) == ['1']
    assert second.step() == {}


def test_a_row_whose_trigger_cell_names_a_file_is_refused_until_it_holds_the_file(
    client: Client, sample: Measure
) -> None:
    named = True

    def source(files: Sequence[FileInfo]) -> list[Row]:
        return [
            replace(row, values={**row.values, 'sample': file(run=1)})
            if named and row.key == '1'
            else row
            for row in pair(files)
        ]

    loop = _loop(client, source)
    sample(1, 'water')
    sample(2, 'sds')

    assert list(loop.step()) == ['2']
    assert loop.refused['1'].startswith('1: sample: run:1 is not a file of the scan')

    named = False

    assert list(loop.step()) == ['1']
    assert loop.refused == {}
