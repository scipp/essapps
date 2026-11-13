# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Automatic reduction: which rows a step reduces, and why a row did not run."""

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import pytest

from ess.apps import Client, SubmitError, file
from ess.apps.batch import FileInfo, Row
from one import Automatic, arrived
from support import Batch
from ess.apps.testing import CountingClient, FakeFiles

from toys import (
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
def auto(batch: Batch) -> Automatic:
    return Automatic(batch, runs=arrived('sample'), settings=lambda: SETTINGS)


def _angles(files: Sequence[FileInfo]) -> list[Row]:
    """A row per sample: all its runs so far, in run order."""
    runs: dict[str, list[Any]] = {}
    for f in sorted(files, key=lambda f: f.metadata['run']):
        runs.setdefault(f.metadata['sample'], []).append(f.ref)
    return [Row(name, {'runs': refs}) for name, refs in runs.items()]


@pytest.fixture
def series(client: Client) -> Automatic:
    batch = Batch(client, SERIES, columns=('runs',), label='series', source=_angles)
    return Automatic(batch, runs=arrived('runs'), settings=dict)


def test_each_new_row_is_reduced_once(
    auto: Automatic, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    sample(2, 'sds')
    first = auto.step()
    client.wait(first)
    sample(3, 'water')

    assert list(first) == ['1', '2']
    assert list(auto.step()) == ['3']
    assert auto.step() == {}


def test_the_batch_must_have_a_source(client: Client) -> None:
    batch = Batch(client, PLAIN, columns=COLUMNS, label='scan')

    with pytest.raises(ValueError, match='no source'):
        Automatic(batch, runs=arrived('sample'), settings=dict)


def test_a_row_whose_record_failed_is_not_reduced_again(
    auto: Automatic, client: Client, sample: Measure, fake: FakeFiles
) -> None:
    fake.corrupt(sample(1, 'water'))
    assert client.wait(auto.step()) == {'1': 'failed'}

    assert auto.step() == {}
    assert auto.refused == {}


def test_a_new_partner_does_not_reduce_a_row_again(
    auto: Automatic, client: Client, sample: Measure, fake: FakeFiles
) -> None:
    sample(1, 'water')
    client.wait(auto.step())

    later = fake.measure(1002, 2.0, role='transmission', sample='water')

    assert auto.batch.rows['1'].values['transmission'] != later  # before the scan
    assert auto.step() == {}
    assert auto.batch.rows['1'].values['transmission'] == later


def test_a_file_that_arrives_again_is_not_reduced_again(
    auto: Automatic, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    client.wait(auto.step())

    sample(1, 'water')

    assert auto.step() == {}


def test_a_new_loop_of_any_submitter_reduces_only_what_no_record_holds(
    auto: Automatic, client: Client, other: Client, sample: Measure
) -> None:
    sample(1, 'water')
    client.wait(auto.step())
    sample(2, 'sds')

    batch = Batch(other, STAGED, columns=COLUMNS, label='scan', source=pair)
    restarted = Automatic(batch, runs=arrived('sample'), settings=lambda: SETTINGS)

    assert list(restarted.step()) == ['2']


def test_a_refused_row_does_not_hold_back_the_others(
    auto: Automatic, batch: Batch, sample: Measure
) -> None:
    for run, name in [(1, 'water'), (2, 'sds'), (3, 'dmso')]:
        sample(run, name)
    batch.scan()
    batch.rows = batch.rows.edit('2', {'thickness': -1.0})

    assert list(auto.step()) == ['1', '3']
    assert auto.refused == {
        '2': '2: thickness: Input should be greater than or equal to 0'
    }


def test_a_waiting_row_runs_once_its_partner_arrives(
    auto: Automatic, batch: Batch, sample: Measure, fake: FakeFiles
) -> None:
    sample(1, 'water', partner=False)
    assert auto.step() == {}
    # CHANGED: why a row waits is its state, as on a person's page
    assert str(SubmitError(batch.state()['1'].problems)) == '1: transmission: empty'
    assert auto.refused == {}

    fake.measure(1001, 1.0, role='transmission', sample='water')

    assert list(auto.step()) == ['1']
    assert auto.refused == {}


def test_rows_refused_for_the_settings_run_once_the_settings_change(
    batch: Batch, sample: Measure
) -> None:
    settings = {'binning': {'bins': 0}}
    auto = Automatic(batch, runs=arrived('sample'), settings=lambda: settings)
    sample(1, 'water')
    sample(2, 'sds')
    assert auto.step() == {}
    assert auto.refused == dict.fromkeys(
        ['1', '2'], 'binning.bins: Input should be greater than or equal to 1'
    )

    settings = SETTINGS

    assert list(auto.step()) == ['1', '2']
    assert auto.refused == {}


def test_a_changed_setting_applies_to_new_rows_only(
    batch: Batch, client: Client, sample: Measure
) -> None:
    settings = SETTINGS
    auto = Automatic(batch, runs=arrived('sample'), settings=lambda: settings)
    sample(1, 'water')
    client.wait(auto.step())

    settings = {'binning': {'bins': 3}}
    sample(2, 'sds')

    (record,) = auto.step().values()
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
    # CHANGED: a running row is 'pending' in its state, not in refused
    assert series.batch.state()['si'].text == 'pending'
    assert series.refused == {}

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
    autos = [
        Automatic(find, runs=arrived('sample'), settings=dict),
        Automatic(reduce, runs=arrived('sample'), settings=lambda: SETTINGS),
    ]
    sample(1, 'water')

    found, reduced = (auto.step() for auto in autos)

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
    batch = Batch(
        flaky,  # type: ignore[arg-type]
        PLAIN,
        columns=COLUMNS,
        label='scan',
        source=pair,
    )
    auto = Automatic(batch, runs=arrived('sample'), settings=lambda: SETTINGS)
    sample(1, 'water')
    flaky.fail = 1
    with pytest.raises(ConnectionError):
        auto.step()

    assert list(auto.step()) == ['1']
    assert auto.step() == {}


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
    auto = Automatic(batch, runs=arrived('runs'), settings=settings)
    gate.close()
    fake.measure(1, 1.0, sample='si')
    first = auto.step()
    fake.measure(2, 2.0, sample='si')
    assert auto.step() == {}  # still running
    gate.open()
    client.wait(first)

    (settings if raising == 'settings' else source).on = True
    with pytest.raises(RuntimeError):
        auto.step()
    settings.on = source.on = False

    (record,) = auto.step().values()
    assert len(record.request.params['runs']) == 2


def test_a_record_holding_the_file_at_another_parameter_does_not_count(
    auto: Automatic, client: Client, sample: Measure, fake: FakeFiles
) -> None:
    run = sample(1, 'water')
    other = fake.measure(5000, 1.0, role='transmission', sample='other')
    params = {'sample': other, 'transmission': run, **SETTINGS}
    client.wait(client.submit(PLAIN, params=params, label='scan', member='1'))

    assert list(auto.step()) == ['1']


def test_a_file_inside_a_group_at_the_trigger_column_counts(
    client: Client, fake: FakeFiles
) -> None:
    def grouped(files: Sequence[FileInfo]) -> list[Row]:
        return [Row(str(f.metadata['run']), {'group': {'run': f.ref}}) for f in files]

    batch = Batch(client, GROUPED, columns=('group',), label='g', source=grouped)
    auto = Automatic(batch, runs=arrived('group'), settings=dict)
    fake.measure(1, 1.0)

    # CHANGED: was "does not count", and the row never ran. Any difference at
    # the parameter counts, so the row runs once.
    client.wait(first := auto.step())
    assert list(first) == ['1']
    assert auto.step() == {}


def test_a_record_of_another_spec_with_the_rows_key_counts(
    auto: Automatic, client: Client, sample: Measure
) -> None:
    run = sample(1, 'water')
    client.wait(client.submit(FIND, params={'sample': run}, label='scan', member='1'))

    assert auto.step() == {}


def test_settings_changed_in_place_count_as_changed(
    batch: Batch, sample: Measure
) -> None:
    settings = {'binning': {'bins': 0}}
    auto = Automatic(batch, runs=arrived('sample'), settings=lambda: settings)
    sample(1, 'water')
    assert auto.step() == {}

    settings['binning']['bins'] = 2

    assert list(auto.step()) == ['1']


def test_an_idle_step_reads_no_records(client: Client, sample: Measure) -> None:
    counting = CountingClient(client)
    batch = Batch(
        counting,  # type: ignore[arg-type]
        PLAIN,
        columns=COLUMNS,
        label='scan',
        source=pair,
    )
    auto = Automatic(batch, runs=arrived('sample'), settings=lambda: SETTINGS)
    sample(1, 'water')
    client.wait(auto.step())
    auto.step()  # reads the record that finished
    counting.reads.clear()

    assert auto.step() == {}
    assert sum(counting.reads.values()) == 0


def test_two_loops_on_one_label_submit_a_file_once(
    client: Client, sample: Measure
) -> None:
    first, second = (
        Automatic(
            Batch(client, PLAIN, columns=COLUMNS, label='scan', source=pair),
            runs=arrived('sample'),
            settings=lambda: SETTINGS,
        )
        for _ in range(2)
    )
    sample(1, 'water')

    assert list(first.step()) == ['1']
    assert second.step() == {}


def test_a_cell_that_names_a_file_is_reduced_once(
    client: Client, sample: Measure
) -> None:
    def by_run(files: Sequence[FileInfo]) -> list[Row]:
        return [
            replace(row, values={**row.values, 'sample': file(run=int(row.key))})
            for row in pair(files)
        ]

    batch = Batch(client, PLAIN, columns=COLUMNS, label='scan', source=by_run)
    auto = Automatic(batch, runs=arrived('sample'), settings=lambda: SETTINGS)
    sample(1, 'water')

    # CHANGED: was "stops the loop at every step". The cell differs from the
    # record's as given, is resolved, and is then the same file.
    client.wait(first := auto.step())
    assert list(first) == ['1']
    assert auto.step() == {}
    assert len(client.records(label='scan')) == 1
