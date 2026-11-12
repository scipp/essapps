# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""A batch on a real in-process workspace with fake workflows."""

from collections.abc import Sequence
from typing import Any

import pytest

from ess.apps import Client, Status, SubmitError, file, local
from ess.apps.batch import Batch, CorrectedFiles, FileInfo, Row, ready, remaining
from ess.apps.testing import FakeStore, StagedFunction
from ess.spec import function_binding

from .toys import COLUMNS, FIND, PLAIN, STAGED, Gate, Measure, Outputs, pair

SETTINGS: dict[str, Any] = {'binning': {'bins': 2}}
OTHER_SETTINGS: dict[str, Any] = {'binning': {'bins': 3}}


@pytest.fixture
def scanned(batch: Batch, sample: Measure) -> Batch:
    sample(1, 'water')
    sample(2, 'sds')
    batch.scan()
    return batch


def test_a_scan_makes_rows_from_the_files(scanned: Batch) -> None:
    assert [row.key for row in scanned.rows] == ['1', '2']
    assert scanned.rows['1'].info == {'name': 'water'}


def test_rows_are_reduced_and_found_by_their_key(
    scanned: Batch, client: Client
) -> None:
    records = scanned.reduce(SETTINGS, ['1', '2'])
    client.wait(records)

    assert client.output(records['2'], 'total') == 2.0
    assert {r.member for r in client.records(label='scan')} == {'1', '2'}
    state = scanned.state()
    assert state['1'].record == records['1']
    assert state['1'].status is Status.COMPLETED


def test_columns_must_be_parameters(client: Client) -> None:
    with pytest.raises(KeyError, match='nope'):
        Batch(client, STAGED, columns=('nope',), label='scan')


def test_a_batch_without_a_source_cannot_scan(client: Client) -> None:
    with pytest.raises(ValueError, match='no source'):
        Batch(client, STAGED, columns=COLUMNS, label='scan').scan()


def test_an_empty_cell_of_a_column_with_a_default_gets_it(
    scanned: Batch, client: Client
) -> None:
    records = scanned.reduce(SETTINGS, ['1'])

    assert records['1'].request.params['thickness'] == 1.0


def test_a_person_can_add_a_row_by_run_number(
    batch: Batch, client: Client, sample: Measure
) -> None:
    sample(7, 'water')
    batch.rows = batch.rows.add(
        Row('mine', {'sample': file(run=7), 'transmission': file(run=1007)})
    )

    records = batch.reduce(SETTINGS, ['mine'])
    client.wait(records)

    assert client.output(records['mine'], 'total') == 7.0


def test_a_row_without_a_partner_is_skipped_by_ready_and_refused_when_picked(
    batch: Batch, sample: Measure
) -> None:
    sample(1, 'water')
    sample(2, 'sds', partner=False)
    batch.scan()

    assert ready(batch.state()) == ['1']
    with pytest.raises(SubmitError) as error:
        batch.reduce(SETTINGS, ['2'])
    assert [(e.entry, e.loc) for e in error.value.errors] == [('2', ('transmission',))]


def test_every_refused_row_is_named_and_none_is_submitted(
    scanned: Batch, client: Client
) -> None:
    scanned.rows = scanned.rows.edit('1', {'thickness': -1.0}).edit(
        '2', {'thickness': -2.0}
    )

    with pytest.raises(SubmitError) as error:
        scanned.reduce(SETTINGS, ['1', '2'])

    assert [(e.entry, e.loc) for e in error.value.errors] == [
        ('1', ('thickness',)),
        ('2', ('thickness',)),
    ]
    assert client.records(label='scan') == []


@pytest.mark.parametrize('spec', [STAGED, PLAIN])
def test_a_reason_about_the_settings_is_named_once_without_a_key(
    client: Client, sample: Measure, spec: Any
) -> None:
    sample(1, 'water')
    sample(2, 'sds')
    batch = Batch(
        client, spec, columns=COLUMNS, label='s', source=pair, staged=spec is STAGED
    )
    batch.scan()

    with pytest.raises(SubmitError) as error:
        batch.reduce({'binning': {'bins': 0}}, ['1', '2'])

    assert [(e.entry, e.loc[0]) for e in error.value.errors] == [(None, 'binning')]
    batch.close()


def test_a_binding_without_stages_works_with_staged_off(
    client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    batch = Batch(client, PLAIN, columns=COLUMNS, label='plain', source=pair)
    batch.scan()

    records = batch.reduce(SETTINGS, ['1'])

    assert client.wait(records) == {'1': 'completed'}


def test_the_stage_is_reused_while_the_settings_stay(
    scanned: Batch, client: Client, staged_binding: StagedFunction
) -> None:
    client.wait(scanned.reduce(SETTINGS, ['1']))
    client.wait(scanned.reduce(dict(SETTINGS), ['2']))

    assert len(staged_binding.staged) == 1


def test_changed_settings_open_a_new_stage(
    scanned: Batch, client: Client, staged_binding: StagedFunction
) -> None:
    client.wait(scanned.reduce(SETTINGS, ['1']))
    client.wait(scanned.reduce(OTHER_SETTINGS, ['1']))

    assert len(staged_binding.staged) == 2


def test_a_row_refused_leaves_the_stage_alone(
    scanned: Batch, client: Client, staged_binding: StagedFunction
) -> None:
    client.wait(scanned.reduce(SETTINGS, ['1']))
    scanned.rows = scanned.rows.edit('2', {'thickness': -1.0})
    with pytest.raises(SubmitError):
        scanned.reduce(SETTINGS, ['2'])
    scanned.rows = scanned.rows.reset('2')

    client.wait(scanned.reduce(SETTINGS, ['2']))

    assert len(staged_binding.staged) == 1


def test_a_stage_that_failed_is_replaced_within_one_reduce(
    scanned: Batch, client: Client, staged_binding: StagedFunction
) -> None:
    staged_binding.fail_next = True
    assert client.wait(scanned.reduce(SETTINGS, ['1'])) == {'1': 'failed'}

    records = scanned.reduce(SETTINGS, ['1', '2'])

    assert client.wait(records) == {'1': 'completed', '2': 'completed'}


def test_a_rerun_becomes_the_result(scanned: Batch, client: Client) -> None:
    client.wait(scanned.reduce(OTHER_SETTINGS, ['1']))
    again = scanned.reduce(SETTINGS, ['1'])
    client.wait(again)

    assert scanned.state()['1'].record == again['1']


def test_an_edited_row_is_stale_and_left_to_do(scanned: Batch, client: Client) -> None:
    client.wait(scanned.reduce(SETTINGS, ['1', '2']))
    scanned.rows = scanned.rows.edit('1', {'thickness': 2.0})

    assert remaining(scanned.state(SETTINGS)) == ['1']


def test_changed_settings_make_every_result_stale(
    scanned: Batch, client: Client
) -> None:
    client.wait(scanned.reduce(SETTINGS, ['1', '2']))

    assert remaining(scanned.state(SETTINGS)) == []
    assert remaining(scanned.state(OTHER_SETTINGS)) == ['1', '2']


def test_a_row_running_is_not_submitted_twice(
    scanned: Batch, client: Client, gate: Gate
) -> None:
    gate.close()
    first = scanned.reduce(SETTINGS, ready(scanned.state()))

    assert ready(scanned.state()) == []
    gate.open()
    client.wait(first)


def test_cancel_reaches_what_was_just_submitted(
    scanned: Batch, client: Client, gate: Gate
) -> None:
    gate.close()
    records = scanned.reduce(SETTINGS, ['1', '2'])

    scanned.cancel()
    gate.open()

    assert client.wait(records) == {'1': 'cancelled', '2': 'cancelled'}
    assert remaining(scanned.state(SETTINGS)) == ['1', '2']


def test_cancel_leaves_other_rows_when_given_keys(
    scanned: Batch, client: Client, gate: Gate
) -> None:
    gate.close()
    records = scanned.reduce(SETTINGS, ['1', '2'])

    scanned.cancel(['1'])

    assert client.status(records['1']) is Status.CANCELLED
    assert client.status(records['2']) is Status.PENDING
    gate.open()


def test_a_tweak_is_the_rows_result_and_not_stale(
    scanned: Batch, client: Client
) -> None:
    client.wait(scanned.reduce(SETTINGS, ['1']))
    row = scanned.rows['1'].values

    tweaked = scanned.tweak('1', {**OTHER_SETTINGS, **row})
    client.wait(tweaked)

    assert scanned.state()['1'].record == tweaked
    assert remaining(scanned.state(SETTINGS)) == ['2']


def test_a_row_is_tweaked_until_it_is_reduced_with_the_settings(
    scanned: Batch, client: Client
) -> None:
    client.wait(scanned.tweak('1', {**OTHER_SETTINGS, **scanned.rows['1'].values}))
    tweaked = {k: s.tweaked for k, s in scanned.state().items()}

    client.wait(scanned.reduce(SETTINGS, ['1']))

    assert tweaked == {'1': True, '2': False}
    assert not scanned.state()['1'].tweaked


def test_a_tweak_starts_from_the_rows_cells_and_its_result_or_else_the_settings(
    scanned: Batch, client: Client
) -> None:
    cells = scanned.rows['1'].values
    record = scanned.reduce(SETTINGS, ['1'])['1']
    client.wait(record)
    with_cells = {**OTHER_SETTINGS, 'sample': 'ignored', 'thickness': 9.0}

    assert scanned.tweak_params('1', None, with_cells) == {**OTHER_SETTINGS, **cells}
    given = record.request.params
    settings = {k: v for k, v in given.items() if k not in scanned.columns}
    assert scanned.tweak_params('1', record, OTHER_SETTINGS) == {**settings, **cells}


@pytest.fixture
def chain(client: Client, sample: Measure) -> tuple[Batch, Batch]:
    """A batch that finds an offset per sample, and one that reads it per row."""
    sample(1, 'water')
    sample(2, 'sds')
    find = Batch(client, FIND, columns=('sample',), label='find', source=_samples)
    reduce = Batch(
        client, PLAIN, columns=(*COLUMNS, 'offset'), label='reduce', source=pair
    )
    find.scan()
    reduce.scan()
    return find, reduce


def _samples(files: Sequence[FileInfo]) -> list[Row]:
    return [
        Row(str(f.metadata['run']), {'sample': f.ref})
        for f in files
        if f.metadata.get('role') == 'sample'
    ]


def test_a_row_can_read_the_record_of_its_row_in_another_batch(
    chain: tuple[Batch, Batch], client: Client
) -> None:
    find, reduce = chain
    found = find.reduce({}, ['1', '2'])  # the second batch need not wait
    for key, record in found.items():
        reduce.rows = reduce.rows.edit(key, {'offset': record.ref('offset')})

    records = reduce.reduce(SETTINGS, ['1', '2'])
    client.wait(records)

    assert client.output(records['2'], 'total') == 4.0
    reduce.scan()
    assert reduce.rows['2'].values['offset'] == found['2'].ref('offset')


def test_a_cell_reading_a_failed_record_stops_only_its_row(
    chain: tuple[Batch, Batch], client: Client
) -> None:
    _, reduce = chain
    failed = reduce.reduce({'binning': {'bins': 1}}, ['1'])
    client.wait(failed)
    reduce.rows = reduce.rows.edit('2', {'offset': failed['1'].ref('total')})

    assert ready(reduce.state()) == ['1']


def test_results_of_another_submitter_are_not_a_rows_result(
    scanned: Batch, client: Client, other: Client
) -> None:
    client.wait(scanned.reduce(SETTINGS, ['1']))
    mine = scanned.state()['1'].record
    row = {k: scanned.rows['1'].values[k] for k in ('sample', 'transmission')}

    other.compute(STAGED, params={**row, **SETTINGS}, label='scan', member='1')

    assert scanned.state()['1'].record == mine


def test_persisting_goes_with_the_batch(files: CorrectedFiles, sample: Measure) -> None:
    store = FakeStore()
    binding = function_binding(PLAIN, lambda _: Outputs(total=1.0))
    sample(1, 'water')
    with local(proposal='p1', files=files, bind=[binding], store=store) as client:
        batch = Batch(
            client, PLAIN, columns=COLUMNS, label='s', source=pair, persist=True
        )
        batch.scan()
        records = batch.reduce(SETTINGS, ['1'])
        client.wait(records)

        assert (records['1'].id, 'total') in store


def test_a_row_running_is_refused_when_picked(
    scanned: Batch, client: Client, gate: Gate
) -> None:
    gate.close()
    first = scanned.reduce(SETTINGS, ['1'])

    with pytest.raises(SubmitError, match='1: still running'):
        scanned.reduce(SETTINGS, ['1'])
    with pytest.raises(SubmitError, match='1: still running'):
        scanned.tweak('1', {**SETTINGS, **scanned.rows['1'].values})
    gate.open()
    client.wait(first)
    assert len(client.records(label='scan')) == 1


def test_a_cell_edited_after_a_tweak_makes_the_row_stale(
    scanned: Batch, client: Client
) -> None:
    client.wait(scanned.reduce(SETTINGS, ['1']))
    client.wait(scanned.tweak('1', {**OTHER_SETTINGS, **scanned.rows['1'].values}))

    scanned.rows = scanned.rows.edit('1', {'thickness': 2.0})

    assert scanned.state(SETTINGS)['1'].stale


def test_cancel_reaches_a_row_removed_since(
    scanned: Batch, client: Client, gate: Gate
) -> None:
    gate.close()
    records = scanned.reduce(SETTINGS, ['1'])
    scanned.rows = scanned.rows.remove('1')

    scanned.cancel()
    gate.open()

    assert client.wait(records) == {'1': 'cancelled'}
