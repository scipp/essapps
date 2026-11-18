# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""A batch on a real in-process workspace with fake workflows."""

from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field, StrictInt

from ess.apps import Client, Status, SubmitError, Workspace, file, local
from ess.apps.batch import (
    EMPTY,
    Batch,
    CorrectedFiles,
    FileInfo,
    Row,
    RowTable,
    Scope,
    earlier_settings,
    left_to_reduce,
    ready,
)
from ess.apps.log import Log
from ess.apps.testing import CountingClient, FakeFiles, FakeStore, StagedFunction
from ess.spec import (
    FileRef,
    OutputRef,
    RunFile,
    SampleRunFile,
    WorkflowSpec,
    function_binding,
)

from .toys import (
    COLUMNS,
    FIND,
    PAIRS,
    PLAIN,
    SAMPLES,
    STAGED,
    Gate,
    Measure,
    Outputs,
    pair,
)

PLAIN_V2 = PLAIN.model_copy(update={'version': 2})


class StrictParams(BaseModel):
    sample: RunFile
    count: StrictInt = 1
    number: int = 1
    counts: list[StrictInt] = Field(default_factory=lambda: [1])


STRICT = WorkflowSpec(
    name='strict',
    version=1,
    title='Strict',
    description='A toy whose parameters tell 1, 1.0 and True apart.',
    params=StrictParams,
    outputs=Outputs,
)

SETTINGS: dict[str, Any] = {'binning': {'bins': 2}}
OTHER_SETTINGS: dict[str, Any] = {'binning': {'bins': 3}}


@pytest.fixture
def scanned(batch: Batch, sample: Measure) -> Batch:
    sample(1, 'water')
    sample(2, 'sds')
    batch.scan()
    batch.settings = SETTINGS
    return batch


def test_a_scan_makes_rows_from_the_files(scanned: Batch) -> None:
    assert [row.key for row in scanned.rows] == ['1', '2']
    assert scanned.rows['1'].info == {'name': 'water'}


def test_rows_are_reduced_and_found_by_their_key(
    scanned: Batch, client: Client
) -> None:
    records = scanned.reduce(['1', '2'])
    client.wait(records)

    assert client.output(records['2'], 'total') == 2.0
    assert {r.member for r in client.records(label='scan')} == {'1', '2'}
    state = scanned.state()
    assert state['1'].record == records['1']
    assert state['1'].status is Status.COMPLETED


def test_the_state_of_chosen_rows_asks_the_status_of_their_records_only(
    client: Client, sample: Measure
) -> None:
    counting = CountingClient(client)
    batch = Batch(
        counting,  # type: ignore[arg-type]
        STAGED,
        columns=COLUMNS,
        label='scan',
        source=PAIRS,
    )
    sample(1, 'water')
    sample(2, 'sds')
    batch.scan()
    client.wait(batch.reduce(['1']))
    counting.reads.clear()

    assert list(batch.state(['2'])) == ['2']
    assert counting.reads['status'] == 0


def test_a_scan_returns_the_files_its_source_got(
    client: Client, sample: Measure
) -> None:
    got: list[FileInfo] = []

    def source(files: Sequence[FileInfo]) -> list[Row]:
        got.extend(files)
        return pair(files)

    batch = Batch(
        client, PLAIN, columns=COLUMNS, label='scan', source=replace(PAIRS, rows=source)
    )
    run = sample(1, 'water')

    assert batch.scan() == got
    assert run in [f.ref for f in got]


def test_a_batch_whose_source_fills_a_column_it_lacks_is_refused(
    client: Client,
) -> None:
    with pytest.raises(ValueError, match=r"the source fills \['transmission'\]"):
        Batch(client, PLAIN, columns=('sample',), label='scan', source=PAIRS)


def test_a_scope_set_on_a_batch_changes_its_rows_without_a_scan(
    scanned: Batch, sample: Measure
) -> None:
    sample(3, 'oil')

    scanned.scope = Scope(start=2)
    assert [row.key for row in scanned.rows] == ['2']
    scanned.scope = Scope()
    assert [row.key for row in scanned.rows] == ['1', '2']


def test_a_table_assigned_keeps_the_scope_of_the_batch(scanned: Batch) -> None:
    scanned.scope = Scope(start=2)

    scanned.rows = RowTable(scan=scanned.rows.scan)

    assert scanned.scope == Scope(start=2)
    assert [row.key for row in scanned.rows] == ['2']


def test_a_file_listed_late_lands_in_the_batch_by_its_index(
    client: Client, sample: Measure
) -> None:
    batch = Batch(
        client,
        PLAIN,
        columns=COLUMNS,
        label='scan',
        source=PAIRS,
        scope=Scope(start=2, stop=10),
    )
    sample(5, 'water')
    batch.scan()

    sample(3, 'oil')
    sample(12, 'sds')
    batch.scan()

    assert sorted(row.key for row in batch.rows) == ['3', '5']


def test_a_row_in_the_scope_takes_a_partner_measured_before_it(
    client: Client, fake: FakeFiles, sample: Measure
) -> None:
    before = fake.measure(1, 1.0, role='transmission', sample='water')
    sample(5, 'water', partner=False)
    batch = Batch(
        client, PLAIN, columns=COLUMNS, label='scan', source=PAIRS, scope=Scope(start=5)
    )

    batch.scan()

    assert [row.key for row in batch.rows] == ['5']
    assert batch.rows['5'].values['transmission'] == before


def test_columns_must_be_parameters(client: Client) -> None:
    with pytest.raises(KeyError, match='nope'):
        Batch(client, STAGED, columns=('nope',), label='scan')


def test_a_batch_without_a_source_cannot_scan(client: Client) -> None:
    with pytest.raises(ValueError, match='no source'):
        Batch(client, STAGED, columns=COLUMNS, label='scan').scan()


def test_a_batch_starts_with_a_copy_of_the_settings_given(
    client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    given = {'binning': {'bins': 3}}
    batch = Batch(
        client, PLAIN, columns=COLUMNS, label='s', source=PAIRS, settings=given
    )
    batch.scan()
    given['binning']['bins'] = 4

    records = batch.reduce(['1'])

    assert records['1'].request.params['binning'] == {'bins': 3}


def test_settings_that_name_a_column_are_refused(scanned: Batch) -> None:
    with pytest.raises(ValueError, match='thickness'):
        scanned.settings = {**SETTINGS, 'thickness': 4.0}
    with pytest.raises(ValueError, match='thickness'):
        Batch(
            scanned.client,
            PLAIN,
            columns=COLUMNS,
            label='s',
            settings={'thickness': 4.0},
        )

    assert scanned.settings == SETTINGS


def test_a_reduction_compares_no_row_and_a_state_resolves_the_settings_once(
    client: Client, sample: Measure
) -> None:
    counting = CountingClient(client)
    batch = Batch(
        counting,  # type: ignore[arg-type]
        PLAIN,
        columns=COLUMNS,
        label='scan',
        source=PAIRS,
    )
    sample(1, 'water')
    batch.scan()
    client.wait(batch.reduce(['1']))
    batch.settings = OTHER_SETTINGS

    client.wait(batch.reduce(['1']))
    batch.results()
    reduced = counting.reads['resolve']
    batch.state()

    assert (reduced, counting.reads['resolve']) == (0, 1)


def test_the_settings_change_only_by_an_assignment_of_all_of_them(
    scanned: Batch,
) -> None:
    given = {'binning': {'bins': 3}}
    scanned.settings = given
    given['binning']['bins'] = 4
    scanned.settings['binning']['bins'] = 5

    with pytest.raises(TypeError):
        scanned.settings['binning'] = {'bins': 6}  # type: ignore[index]
    assert scanned.settings == {'binning': {'bins': 3}}


def test_an_empty_cell_of_a_column_with_a_default_gets_it(
    scanned: Batch, client: Client
) -> None:
    records = scanned.reduce(['1'])

    assert records['1'].request.params['thickness'] == 1.0


def test_a_person_can_add_a_row_by_run_number(
    batch: Batch, client: Client, sample: Measure
) -> None:
    sample(7, 'water')
    batch.rows = batch.rows.add(
        Row('mine', {'sample': file(run=7), 'transmission': file(run=1007)})
    )

    records = batch.reduce(['mine'])
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
        batch.reduce(['2'])
    assert [(e.entry, e.loc) for e in error.value.errors] == [('2', ('transmission',))]


def test_every_refused_row_is_named_and_none_is_submitted(
    scanned: Batch, client: Client
) -> None:
    scanned.rows = scanned.rows.edit('1', {'thickness': -1.0}).edit(
        '2', {'thickness': -2.0}
    )

    with pytest.raises(SubmitError) as error:
        scanned.reduce(['1', '2'])

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
        client, spec, columns=COLUMNS, label='s', source=PAIRS, staged=spec is STAGED
    )
    batch.scan()

    batch.settings = {'binning': {'bins': 0}}

    with pytest.raises(SubmitError) as error:
        batch.reduce(['1', '2'])

    assert [(e.entry, e.loc[0]) for e in error.value.errors] == [(None, 'binning')]
    batch.close()


def test_a_binding_without_stages_works_with_staged_off(
    client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    batch = Batch(client, PLAIN, columns=COLUMNS, label='plain', source=PAIRS)
    batch.scan()

    records = batch.reduce(['1'])

    assert client.wait(records) == {'1': 'completed'}


def test_the_stage_is_reused_while_the_settings_stay(
    scanned: Batch, client: Client, staged_binding: StagedFunction
) -> None:
    client.wait(scanned.reduce(['1']))
    client.wait(scanned.reduce(['2']))

    assert len(staged_binding.staged) == 1


def test_changed_settings_open_a_new_stage(
    scanned: Batch, client: Client, staged_binding: StagedFunction
) -> None:
    client.wait(scanned.reduce(['1']))
    scanned.settings = OTHER_SETTINGS
    client.wait(scanned.reduce(['1']))

    assert len(staged_binding.staged) == 2


def test_a_row_refused_leaves_the_stage_alone(
    scanned: Batch, client: Client, staged_binding: StagedFunction
) -> None:
    client.wait(scanned.reduce(['1']))
    scanned.rows = scanned.rows.edit('2', {'thickness': -1.0})
    with pytest.raises(SubmitError):
        scanned.reduce(['2'])
    scanned.rows = scanned.rows.reset('2')

    client.wait(scanned.reduce(['2']))

    assert len(staged_binding.staged) == 1


def test_a_stage_that_failed_is_replaced_within_one_reduce(
    scanned: Batch, client: Client, staged_binding: StagedFunction
) -> None:
    staged_binding.fail_next = True
    assert client.wait(scanned.reduce(['1'])) == {'1': 'failed'}

    records = scanned.reduce(['1', '2'])

    assert client.wait(records) == {'1': 'completed', '2': 'completed'}


def test_a_rerun_becomes_the_result(scanned: Batch, client: Client) -> None:
    scanned.settings = OTHER_SETTINGS
    client.wait(scanned.reduce(['1']))
    scanned.settings = SETTINGS
    again = scanned.reduce(['1'])
    client.wait(again)

    assert scanned.state()['1'].record == again['1']


def test_an_edited_row_is_outdated_and_left_to_do(
    scanned: Batch, client: Client
) -> None:
    client.wait(scanned.reduce(['1', '2']))
    scanned.rows = scanned.rows.edit('1', {'thickness': 2.0})

    assert left_to_reduce(scanned.state(), COLUMNS) == ['1']


def test_changed_settings_make_every_result_outdated(
    scanned: Batch, client: Client
) -> None:
    client.wait(scanned.reduce(['1', '2']))

    scanned.settings = OTHER_SETTINGS

    states = scanned.state()
    assert [states[k].outdated for k in ('1', '2')] == [True, True]
    assert earlier_settings(states, COLUMNS) == ['1', '2']
    assert left_to_reduce(states, COLUMNS) == []


def test_changed_names_the_cell_and_the_setting_that_differ(
    scanned: Batch, client: Client
) -> None:
    client.wait(scanned.reduce(['1', '2']))
    scanned.rows = scanned.rows.edit('1', {'thickness': 2.0})

    scanned.settings = OTHER_SETTINGS

    changed = {k: s.changed for k, s in scanned.state().items()}

    assert changed == {'1': {'binning', 'thickness'}, '2': {'binning'}}


def test_a_second_batch_on_the_label_reads_the_same_changed(
    scanned: Batch, client: Client
) -> None:
    client.wait(scanned.reduce(['1', '2']))
    second = Batch(client, STAGED, columns=COLUMNS, label='scan', source=PAIRS)
    second.scan()
    for batch in (scanned, second):
        batch.rows = batch.rows.edit('1', {'thickness': 2.0})

    def changed(batch: Batch) -> dict[str, frozenset[str]]:
        return {k: s.changed for k, s in batch.state().items()}

    assert changed(second) == changed(scanned) == {'1': {'thickness'}, '2': set()}


def test_a_default_written_out_and_a_file_by_name_do_not_change_a_row(
    scanned: Batch, client: Client
) -> None:
    scanned.settings = {}
    client.wait(scanned.reduce(['1', '2']))
    by_name = {'sample': file(run=1), 'transmission': file(run=1001), 'thickness': 1.0}
    scanned.rows = scanned.rows.edit('1', by_name)
    scanned.settings = {'binning': {'bins': 2}, 'offset': None}

    states = scanned.state()

    assert {k: s.changed for k, s in states.items()} == {'1': set(), '2': set()}
    assert left_to_reduce(states, COLUMNS) == []


def test_a_cleared_cell_is_one_problem(scanned: Batch, client: Client) -> None:
    client.wait(scanned.reduce(['1']))
    scanned.rows = scanned.rows.edit('1', {'sample': EMPTY})

    state = scanned.state()['1']

    assert [(e.loc, e.msg) for e in state.problems] == [(('sample',), 'empty')]


def test_a_file_named_before_it_is_measured_is_a_problem_until_it_is(
    scanned: Batch, sample: Measure
) -> None:
    row = Row('9', {'sample': file(run=9), 'transmission': file(run=1001)})
    scanned.rows = scanned.rows.add(row)

    before = scanned.state()
    sample(9, 'oil')
    after = scanned.state()

    assert [e.loc for e in before['9'].problems] == [('sample',)]
    assert ready(before) == ['1', '2']
    assert scanned.results()['9'].problems == ()
    assert (after['9'].problems, after['9'].text) == ((), 'not reduced')


def test_an_older_record_that_ends_later_is_not_the_result(
    scanned: Batch, client: Client, gate: Gate
) -> None:
    gate.close()
    older = scanned.reduce(['1'])['1']
    scanned.state()
    cells = {**scanned.rows['1'].values, 'thickness': 1.0}
    request = scanned.template().fill(params=cells)
    newer = client.submit(request, label='scan', member='1')
    client.cancel([older])

    assert scanned.state()['1'].record == newer
    gate.open()
    client.wait(newer)


def test_a_record_under_another_label_is_not_a_rows_result(
    scanned: Batch, client: Client
) -> None:
    mine = scanned.reduce(['1'])['1']
    client.wait(mine)
    scanned.state()
    cells = {**scanned.rows['1'].values, 'thickness': 1.0}
    request = scanned.template().fill(params=cells)

    client.compute(request, label='other', member='1')

    assert scanned.state()['1'].record == mine


def test_a_cell_refused_is_a_problem_of_its_row_and_not_changed(
    scanned: Batch, client: Client
) -> None:
    client.wait(scanned.reduce(['1', '2']))
    scanned.rows = scanned.rows.edit('1', {'sample': file(run=99)})

    state = scanned.state()['1']

    assert [e.loc for e in state.problems] == [('sample',)]
    assert state.changed == frozenset()
    assert state.text == 'incomplete'


class Changing:
    """
    A file source whose names change: a name in ``names`` resolves to the
    identity there, or to no file for ``None``.
    """

    def __init__(self, files: CorrectedFiles) -> None:
        self.files = files
        self.names: dict[FileRef, FileRef | None] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self.files, name)

    def resolve(self, name: FileRef) -> FileRef:
        if name not in self.names:
            return self.files.resolve(name)
        if (ref := self.names[name]) is None:
            raise KeyError(name)
        return ref

    def metadata(self, ref: FileRef) -> dict[str, Any]:
        return self.files.metadata(self._underlying(ref))

    def path(self, ref: FileRef) -> Path:
        return self.files.path(self._underlying(ref))

    def _underlying(self, ref: FileRef) -> FileRef:
        """The identity that ``files`` gives the file of ``ref``."""
        names = [n for n, r in self.names.items() if r == ref]
        return self.files.resolve(names[0]) if names else ref


@pytest.fixture
def bound_twice(
    files: CorrectedFiles, sample: Measure
) -> Iterator[tuple[Client, Changing]]:
    """A client that runs PLAIN and PLAIN_V2, of rows 1 and 2 reduced by PLAIN."""
    changing = Changing(files)
    bind = [
        function_binding(spec, lambda _: Outputs(total=1.0))
        for spec in (PLAIN, PLAIN_V2)
    ]
    sample(1, 'water')
    sample(2, 'sds')
    with local(proposal='p1', files=changing, bind=bind) as client:  # type: ignore[arg-type]
        old = Batch(client, PLAIN, columns=COLUMNS, label='s', source=PAIRS)
        old.scan()
        client.wait(old.reduce(['1', '2']))
        yield client, changing


@pytest.mark.parametrize('spec', [PLAIN, PLAIN_V2])
def test_settings_refused_are_raised_naming_no_row(
    bound_twice: tuple[Client, Changing], sample: Measure, spec: Any
) -> None:
    client, _ = bound_twice
    sample(3, 'oil')
    batch = Batch(client, spec, columns=COLUMNS, label='s', source=PAIRS)
    batch.scan()

    batch.settings = {'binning': {'bins': 0}}

    with pytest.raises(SubmitError) as error:
        batch.state()

    assert [(e.entry, e.loc[0]) for e in error.value.errors] == [(None, 'binning')]
    assert batch.results()['3'].problems == ()


def test_a_changed_setting_is_found_when_no_result_s_files_are_known(
    bound_twice: tuple[Client, Changing],
) -> None:
    client, changing = bound_twice
    batch = Batch(client, PLAIN, columns=COLUMNS, label='s', source=PAIRS)
    batch.scan()
    changing.names |= dict.fromkeys(r.values['sample'] for r in batch.rows)
    batch.settings = OTHER_SETTINGS

    changed = {k: s.changed for k, s in batch.state().items()}

    assert changed == {'1': {'binning'}, '2': {'binning'}}


@pytest.fixture
def strict(files: CorrectedFiles, sample: Measure) -> Iterator[Client]:
    """A client that runs STRICT, with sample run 1."""
    sample(1, 'water')
    bind = [function_binding(STRICT, lambda _: Outputs(total=1.0))]
    with local(proposal='p1', files=files, bind=bind) as client:
        yield client


def problems(batch: Batch) -> dict[str, list[tuple[str | int, ...]]]:
    return {k: [e.loc for e in s.problems] for k, s in batch.state().items()}


def strict_batch(client: Client, rows: dict[str, dict[str, Any]]) -> Batch:
    batch = Batch(
        client, STRICT, columns=('sample', 'count', 'number', 'counts'), label='s'
    )
    batch.rows = batch.rows.rescan(
        [Row(k, {'sample': file(run=1), **cells}) for k, cells in rows.items()]
    )
    return batch


@pytest.mark.parametrize(
    ('column', 'kept', 'given'),
    [
        ('count', 1, True),
        ('count', 1, 1.0),
        ('counts', [1], [True]),
        ('counts', (1,), (True,)),
    ],
)
def test_a_value_kept_is_not_taken_for_an_equal_one_of_another_type(
    strict: Client, column: str, kept: Any, given: Any
) -> None:
    cached = strict_batch(strict, {'a': {column: kept}})
    problems(cached)

    cached.rows = cached.rows.edit('a', {column: given})

    assert problems(cached)['a'] != []
    assert problems(cached) == problems(strict_batch(strict, {'a': {column: given}}))


def test_a_value_kept_at_one_column_is_not_taken_at_another(strict: Client) -> None:
    rows = {'a': {'number': True}, 'b': {'count': True}}
    cached = strict_batch(strict, {'a': rows['a']})
    problems(cached)

    cached.rows = cached.rows.add(Row('b', {'sample': file(run=1), **rows['b']}))

    assert problems(cached) == problems(strict_batch(strict, rows))
    assert problems(cached) == {'a': [], 'b': [('count',)]}


def test_a_batch_reads_a_file_by_run_as_a_new_one_does_after_a_scan(
    bound_twice: tuple[Client, Changing],
) -> None:
    client, changing = bound_twice

    def by_run() -> Batch:
        batch = Batch(client, PLAIN, columns=COLUMNS, label='s', source=PAIRS)
        batch.scan()
        batch.rows = batch.rows.edit('1', {'sample': file(run=1)})
        return batch

    def changed(batch: Batch) -> frozenset[str]:
        return batch.state()['1'].changed

    batch = by_run()
    before = changed(batch)
    changing.names[file(run=1)] = FileRef(file='pid:20.500.12269/water')
    kept = changed(batch)
    batch.scan()

    assert (before, kept) == (set(), set())
    assert changed(batch) == changed(by_run()) == {'sample'}


def test_a_row_naming_its_file_by_run_is_changed_only_once_its_cell_is(
    scanned: Batch, client: Client
) -> None:
    scanned.rows = scanned.rows.edit('1', {'sample': file(run=1)})
    client.wait(scanned.reduce(['1']))

    first, again = (scanned.state()['1'].changed for _ in range(2))
    scanned.rows = scanned.rows.edit('1', {'sample': file(run=2)})

    assert (first, again) == (set(), set())
    assert scanned.state()['1'].changed == {'sample'}


def test_a_result_of_another_spec_version_is_the_rows_result_and_outdated(
    bound_twice: tuple[Client, Changing],
) -> None:
    client, _ = bound_twice
    new = Batch(client, PLAIN_V2, columns=COLUMNS, label='s', source=PAIRS)
    new.scan()
    client.wait(new.reduce(['2']))

    states = new.state()

    assert [r.member for r in client.records(label='s')] == ['1', '2', '2']
    assert states['1'].record == client.records(label='s')[0]
    assert (states['1'].spec_changed, states['2'].spec_changed) == (True, False)
    assert left_to_reduce(states, COLUMNS) == ['1']


class Redefined(BaseModel):
    """The params of ``PLAIN`` with another definition of its binning."""

    count: int = 2
    sample: SampleRunFile
    transmission: SampleRunFile
    thickness: float = 1.0


def test_a_result_of_a_spec_of_the_same_version_but_another_definition_is_outdated(
    files: CorrectedFiles, sample: Measure, tmp_path: Path
) -> None:
    redefined = PLAIN.model_copy(update={'params': Redefined})
    sample(1, 'water')
    kept = tmp_path / 'log'
    reduced = Workspace(
        proposal='p1',
        files=files,
        bind=[function_binding(PLAIN, lambda _: Outputs(total=1.0))],
        log=Log(kept),
    )
    with Client(reduced, submitter='anna', on_close=reduced.close) as client:
        old = Batch(client, PLAIN, columns=COLUMNS, label='s', source=PAIRS)
        old.scan()
        client.wait(old.reduce(['1']))
    again = Workspace(
        proposal='p1',
        files=files,
        bind=[function_binding(redefined, lambda _: Outputs(total=1.0))],
        log=Log(kept),
    )
    with Client(again, submitter='anna', on_close=again.close) as client:
        new = Batch(client, redefined, columns=COLUMNS, label='s', source=PAIRS)
        new.scan()

        state = new.state()['1']

        assert (state.status, state.spec_changed) == (Status.COMPLETED, True)
        assert state.text == 'outdated'


def test_the_state_of_chosen_rows_reads_only_the_records_that_changed(
    client: Client, sample: Measure
) -> None:
    counting = CountingClient(client)
    batch = Batch(
        counting,  # type: ignore[arg-type]
        STAGED,
        columns=COLUMNS,
        label='scan',
        source=PAIRS,
    )
    sample(1, 'water')
    sample(2, 'sds')
    batch.scan()
    client.wait(batch.reduce(['1', '2']))
    batch.state()
    counting.reads.clear()

    batch.state(['2'])

    assert counting.reads['records'] == 0


def test_a_row_running_is_not_submitted_twice(
    scanned: Batch, client: Client, gate: Gate
) -> None:
    gate.close()
    first = scanned.reduce(ready(scanned.state()))

    assert ready(scanned.state()) == []
    gate.open()
    client.wait(first)


def test_cancel_reaches_what_was_just_submitted(
    scanned: Batch, client: Client, gate: Gate
) -> None:
    gate.close()
    records = scanned.reduce(['1', '2'])

    scanned.cancel()
    gate.open()

    assert client.wait(records) == {'1': 'cancelled', '2': 'cancelled'}
    assert left_to_reduce(scanned.state(), COLUMNS) == ['1', '2']


def test_cancel_leaves_other_rows_when_given_keys(
    scanned: Batch, client: Client, gate: Gate
) -> None:
    gate.close()
    records = scanned.reduce(['1', '2'])

    scanned.cancel(['1'])

    assert client.status(records['1']) is Status.CANCELLED
    assert client.status(records['2']) is Status.PENDING
    gate.open()


@pytest.fixture
def chain(client: Client, sample: Measure) -> tuple[Batch, Batch]:
    """A batch that finds an offset per sample, and one that reads it per row."""
    sample(1, 'water')
    sample(2, 'sds')
    find = Batch(client, FIND, columns=('sample',), label='find', source=SAMPLES)
    reduce = Batch(
        client, PLAIN, columns=(*COLUMNS, 'offset'), label='reduce', source=PAIRS
    )
    find.scan()
    reduce.scan()
    return find, reduce


def test_a_row_can_read_the_record_of_its_row_in_another_batch(
    chain: tuple[Batch, Batch], client: Client
) -> None:
    find, reduce = chain
    found = find.reduce(['1', '2'])  # the second batch need not wait
    for key, record in found.items():
        reduce.rows = reduce.rows.edit(key, {'offset': record.ref('offset')})

    records = reduce.reduce(['1', '2'])
    client.wait(records)

    assert client.output(records['2'], 'total') == 4.0
    reduce.scan()
    assert reduce.rows['2'].values['offset'] == found['2'].ref('offset')


def test_a_cell_reading_a_failed_record_stops_only_its_row(
    chain: tuple[Batch, Batch], client: Client
) -> None:
    _, reduce = chain
    reduce.settings = {'binning': {'bins': 1}}
    failed = reduce.reduce(['1'])
    client.wait(failed)
    reduce.rows = reduce.rows.edit('2', {'offset': failed['1'].ref('total')})

    assert ready(reduce.state()) == ['1']


def test_a_cell_reading_a_cancelled_record_stops_only_its_row(
    chain: tuple[Batch, Batch], client: Client, gate: Gate
) -> None:
    _, reduce = chain
    gate.close()
    cancelled = reduce.reduce(['1'])
    reduce.cancel(['1'])
    gate.open()
    client.wait(cancelled)
    reduce.rows = reduce.rows.edit('2', {'offset': cancelled['1'].ref('total')})

    states = reduce.state()

    assert ready(states) == ['1']
    assert [e.msg for e in states['2'].problems] == ['the record it reads is cancelled']


def test_an_unknown_record_in_a_cell_touches_only_its_row(
    chain: tuple[Batch, Batch], client: Client
) -> None:
    _, reduce = chain
    reduce.settings = {'binning': {'bins': 1}}
    failed = reduce.reduce(['1'])
    client.wait(failed)
    reduce.rows = reduce.rows.edit(
        '1', {'offset': OutputRef(record='nope', output='x')}
    )
    reduce.rows = reduce.rows.edit('2', {'offset': failed['1'].ref('total')})

    whole, chosen = reduce.state()['2'], reduce.state(['2'])['2']

    assert [e.msg for e in whole.problems] == ['the record it reads is failed']
    assert chosen.problems == whole.problems


def test_a_record_of_another_submitter_is_a_rows_result(
    scanned: Batch, client: Client, other: Client
) -> None:
    client.wait(scanned.reduce(['1']))
    row = {k: scanned.rows['1'].values[k] for k in ('sample', 'transmission')}

    theirs = other.compute(STAGED, params={**row, **SETTINGS}, label='scan', member='1')

    assert scanned.state()['1'].record == theirs


def test_persisting_goes_with_the_batch(files: CorrectedFiles, sample: Measure) -> None:
    store = FakeStore()
    binding = function_binding(PLAIN, lambda _: Outputs(total=1.0))
    sample(1, 'water')
    with local(proposal='p1', files=files, bind=[binding], store=store) as client:
        batch = Batch(
            client, PLAIN, columns=COLUMNS, label='s', source=PAIRS, persist=True
        )
        batch.scan()
        records = batch.reduce(['1'])
        client.wait(records)

        assert (records['1'].id, 'total') in store


def test_a_row_running_is_refused_when_picked(
    scanned: Batch, client: Client, gate: Gate
) -> None:
    gate.close()
    first = scanned.reduce(['1'])

    with pytest.raises(SubmitError, match='1: still running'):
        scanned.reduce(['1'])
    gate.open()
    client.wait(first)
    assert len(client.records(label='scan')) == 1


def test_cancel_reaches_a_row_removed_since(
    scanned: Batch, client: Client, gate: Gate
) -> None:
    gate.close()
    records = scanned.reduce(['1'])
    scanned.rows = scanned.rows.remove('1')

    scanned.cancel()
    gate.open()

    assert client.wait(records) == {'1': 'cancelled'}
