# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""A batch on a real in-process workspace with fake workflows."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from pydantic import BaseModel, Field, StrictInt

from ess.apps import Client, Status, SubmitError, Workspace, file, local
from ess.apps.batch import (
    Batch,
    CorrectedFiles,
    FolderDefinitions,
    NotKept,
    earlier_settings,
    left_to_reduce,
    ready,
    rows_of,
)
from ess.apps.batch.definition import settings_key, template_of
from ess.apps.batch.table import as_request, cell_of
from ess.apps.log import Log
from ess.apps.testing import CountingClient, FakeStore, StagedFunction
from ess.spec import (
    FileRef,
    RunFile,
    SampleRunFile,
    WorkflowSpec,
    function_binding,
)

from .toys import (
    COLUMNS,
    FIND,
    PLAIN,
    SAMPLES,
    STAGED,
    Gate,
    Measure,
    Outputs,
    Refusing,
    edit,
    rows,
)

PLAIN_V2 = PLAIN.model_copy(update={'version': 2})


class StrictParams(BaseModel):
    sample: RunFile
    counts: list[StrictInt] = Field(default_factory=lambda: [1])
    numbers: list[int] = Field(default_factory=lambda: [1])


STRICT = WorkflowSpec(
    name='strict',
    version=1,
    title='Strict',
    description='A toy whose parameters tell 1 and True apart.',
    params=StrictParams,
    outputs=Outputs,
)


class OptionalParams(BaseModel):
    sample: RunFile
    transmission: RunFile | None


OPTIONAL = WorkflowSpec(
    name='optional',
    version=1,
    title='Optional',
    description='A toy whose transmission run may be none, with no default.',
    params=OptionalParams,
    outputs=Outputs,
)


class NumberParams(BaseModel):
    bins: StrictInt
    scale: float


NUMBERS = WorkflowSpec(
    name='numbers',
    version=1,
    title='Numbers',
    description='A toy whose parameters are all numbers, one a strict int.',
    params=NumberParams,
    outputs=Outputs,
)

SETTINGS: dict[str, Any] = {'binning': {'bins': 2}}
OTHER_SETTINGS: dict[str, Any] = {'binning': {'bins': 3}}


@pytest.fixture
def measured(batch: Batch, client: Client, sample: Measure) -> Batch:
    """``batch`` with the rows of samples 1 and 2, and settings."""
    sample(1, 'water')
    sample(2, 'sds')
    batch.put(rows(client))
    batch.settings = SETTINGS
    return batch


def test_the_table_put_is_the_table_and_its_parameter_columns_the_blanks(
    batch: Batch, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    table = rows(client).assign(note='a')

    batch.put(table)

    frame = batch.frame()
    assert list(frame.index) == ['1']
    assert list(frame.columns) == ['run', 'name', *COLUMNS, 'note']
    assert (frame.loc['1', 'name'], frame.loc['1', 'sample']) == ('water', 'uuid:run-1')
    assert batch.columns == batch.template().blanks == COLUMNS
    assert batch.settings_fields == ('binning', 'offset')


def test_a_table_that_is_refused_changes_nothing(measured: Batch) -> None:
    before = measured.frame()
    twice = pd.concat([before, before.loc[['1']]])
    thick = before.assign(thickness='thick')

    for frame in (twice, thick):
        with pytest.raises(ValueError, match="'1'"):
            measured.put(frame)

    pd.testing.assert_frame_equal(measured.frame(), before)


def test_a_table_put_again_writes_nothing(
    client: Client, sample: Measure, refusing: Refusing
) -> None:
    sample(1, 'water')
    batch = Batch(client, PLAIN, label='s', rows=rows(client), definitions=refusing)
    batch.put(batch.frame().assign(thickness=2.0))
    refusing.part = 'rows'

    batch.put(batch.frame())

    with pytest.raises(NotKept):
        batch.put(batch.frame().assign(thickness=3.0))
    assert batch.frame().loc['1', 'thickness'] == 2.0


def test_a_setting_made_a_column_loses_its_value_and_a_column_dropped_has_none(
    client: Client, sample: Measure, definitions: FolderDefinitions
) -> None:
    sample(1, 'water')
    settings = {**SETTINGS, 'offset': {'value': 1.0}}
    batch = Batch(
        client,
        PLAIN,
        label='s',
        rows=rows(client),
        settings=settings,
        definitions=definitions,
    )

    batch.put(batch.frame().assign(offset=None))
    as_column = (batch.settings, batch.columns)
    batch.put(batch.frame().drop(columns=['offset', 'thickness']))

    assert as_column == (SETTINGS, (*COLUMNS, 'offset'))
    assert (batch.settings, batch.columns) == (SETTINGS, ('sample', 'transmission'))
    kept = definitions.load(settings_key('s'), template_of)
    assert kept == batch.template()
    assert kept.blanks == ('sample', 'transmission')


def test_rows_are_reduced_and_found_by_their_key(
    measured: Batch, client: Client
) -> None:
    records = measured.reduce(['1', '2'])
    client.wait(records)

    assert client.output(records['2'], 'total') == 2.0
    assert {r.member for r in client.records(label='scan')} == {'1', '2'}
    state = measured.state()
    assert state['1'].record == records['1']
    assert state['1'].status is Status.COMPLETED


def test_the_state_of_chosen_rows_asks_the_status_of_their_records_only(
    client: Client, sample: Measure
) -> None:
    counting = CountingClient(client)
    sample(1, 'water')
    sample(2, 'sds')
    batch = Batch(counting, STAGED, label='scan', rows=rows(client))  # type: ignore[arg-type]
    client.wait(batch.reduce(['1']))
    counting.reads.clear()

    assert list(batch.state(['2'])) == ['2']
    assert counting.reads['status'] == 0


def test_a_batch_starts_with_a_copy_of_the_settings_given(
    client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    given = {'binning': {'bins': 3}}
    batch = Batch(client, PLAIN, label='s', rows=rows(client), settings=given)
    given['binning']['bins'] = 4

    records = batch.reduce(['1'])

    assert records['1'].request.params['binning'] == {'bins': 3}


def test_settings_at_a_column_are_refused_and_given_at_making_left_out(
    measured: Batch,
) -> None:
    with pytest.raises(ValueError, match='thickness'):
        measured.settings = {**SETTINGS, 'thickness': 4.0}
    made = Batch(
        measured.client,
        PLAIN,
        label='s',
        rows=measured.frame(),
        settings={**SETTINGS, 'thickness': 4.0},
    )

    assert measured.settings == made.settings == SETTINGS


def test_a_reduction_compares_no_row_and_a_state_resolves_the_settings_once(
    client: Client, sample: Measure
) -> None:
    counting = CountingClient(client)
    sample(1, 'water')
    batch = Batch(counting, PLAIN, label='scan', rows=rows(client))  # type: ignore[arg-type]
    client.wait(batch.reduce(['1']))
    batch.settings = OTHER_SETTINGS

    client.wait(batch.reduce(['1']))
    batch.state(compare=False)
    reduced = counting.reads['resolve']
    batch.state()

    assert (reduced, counting.reads['resolve']) == (0, 1)


def test_the_settings_change_only_by_an_assignment_of_all_of_them(
    measured: Batch,
) -> None:
    given = {'binning': {'bins': 3}}
    measured.settings = given
    given['binning']['bins'] = 4
    measured.settings['binning']['bins'] = 5
    measured.template().params['binning']['bins'] = 7

    with pytest.raises(TypeError):
        measured.settings['binning'] = {'bins': 6}  # type: ignore[index]
    assert measured.settings == {'binning': {'bins': 3}}


def test_an_empty_cell_of_a_column_with_a_default_gets_it(measured: Batch) -> None:
    records = measured.reduce(['1'])

    assert records['1'].request.params['thickness'] == 1.0


def test_an_empty_cell_of_a_parameter_that_takes_none_gives_none(
    files: CorrectedFiles, sample: Measure
) -> None:
    sample(1, 'water')
    bind = [function_binding(OPTIONAL, lambda _: Outputs(total=1.0))]
    with local(proposal='p1', files=files, bind=bind) as client:
        table = pd.DataFrame({'sample': ['run:1'], 'transmission': [None]}, index=['1'])
        batch = Batch(client, OPTIONAL, label='s', rows=table)

        assert batch.state()['1'].text == 'ready'
        records = batch.reduce(['1'])

    assert records['1'].request.params['transmission'] is None


def test_a_whole_number_is_an_int_in_the_request_where_all_columns_are_numbers(
    files: CorrectedFiles,
) -> None:
    bind = [function_binding(NUMBERS, lambda _: Outputs(total=1.0))]
    with local(proposal='p1', files=files, bind=bind) as client:
        table = pd.DataFrame({'bins': [3], 'scale': [0.5]}, index=['1'])
        records = Batch(client, NUMBERS, label='s', rows=table).reduce(['1'])

    assert type(records['1'].request.params['bins']) is int


def test_a_list_in_a_cell_changes_only_by_put(strict: Client) -> None:
    table = pd.DataFrame({'sample': ['run:1'], 'counts': [[1]]}, index=['a'])
    batch = Batch(strict, STRICT, label='s', rows=table)
    table.loc['a', 'counts'].append(2)
    frame = batch.frame()
    frame.loc['a', 'counts'].append(3)

    assert batch.frame().loc['a', 'counts'] == [1]
    batch.put(frame)
    frame.loc['a', 'counts'].append(4)
    assert batch.frame().loc['a', 'counts'] == [1, 3]


def test_a_person_can_add_a_row_by_run_number(
    measured: Batch, client: Client, sample: Measure
) -> None:
    sample(7, 'water')
    edit(measured, 'mine', sample='run:7', transmission='run:1007')

    records = measured.reduce(['mine'])
    client.wait(records)

    assert client.output(records['mine'], 'total') == 7.0


def test_a_row_without_a_partner_is_skipped_by_ready_and_refused_when_picked(
    batch: Batch, client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    sample(2, 'sds', partner=False)
    batch.put(rows(client))

    assert ready(batch.state()) == ['1']
    with pytest.raises(SubmitError) as error:
        batch.reduce(['2'])
    assert [(e.entry, e.loc) for e in error.value.errors] == [('2', ('transmission',))]


def test_every_refused_row_is_named_and_none_is_submitted(
    measured: Batch, client: Client
) -> None:
    measured.put(measured.frame().assign(thickness=[-1.0, -2.0]))

    with pytest.raises(SubmitError) as error:
        measured.reduce(['1', '2'])

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
    batch = Batch(client, spec, label='s', rows=rows(client), staged=spec is STAGED)

    batch.settings = {'binning': {'bins': 0}}

    with pytest.raises(SubmitError) as error:
        batch.reduce(['1', '2'])

    assert [(e.entry, e.loc[0]) for e in error.value.errors] == [(None, 'binning')]
    batch.close()


def test_a_binding_without_stages_works_with_staged_off(
    client: Client, sample: Measure
) -> None:
    sample(1, 'water')
    batch = Batch(client, PLAIN, label='plain', rows=rows(client))

    records = batch.reduce(['1'])

    assert client.wait(records) == {'1': 'completed'}


def test_the_stage_is_reused_while_the_settings_stay(
    measured: Batch, client: Client, staged_binding: StagedFunction
) -> None:
    client.wait(measured.reduce(['1']))
    client.wait(measured.reduce(['2']))

    assert len(staged_binding.staged) == 1


def test_changed_settings_open_a_new_stage(
    measured: Batch, client: Client, staged_binding: StagedFunction
) -> None:
    client.wait(measured.reduce(['1']))
    measured.settings = OTHER_SETTINGS
    client.wait(measured.reduce(['1']))

    assert len(staged_binding.staged) == 2


def test_a_row_refused_leaves_the_stage_alone(
    measured: Batch, client: Client, staged_binding: StagedFunction
) -> None:
    client.wait(measured.reduce(['1']))
    edit(measured, '2', thickness=-1.0)
    with pytest.raises(SubmitError):
        measured.reduce(['2'])
    edit(measured, '2', thickness=pd.NA)

    client.wait(measured.reduce(['2']))

    assert len(staged_binding.staged) == 1


def test_a_stage_that_failed_is_replaced_within_one_reduce(
    measured: Batch, client: Client, staged_binding: StagedFunction
) -> None:
    staged_binding.fail_next = True
    assert client.wait(measured.reduce(['1'])) == {'1': 'failed'}

    records = measured.reduce(['1', '2'])

    assert client.wait(records) == {'1': 'completed', '2': 'completed'}


def test_a_rerun_becomes_the_result(measured: Batch, client: Client) -> None:
    measured.settings = OTHER_SETTINGS
    client.wait(measured.reduce(['1']))
    measured.settings = SETTINGS
    again = measured.reduce(['1'])
    client.wait(again)

    assert measured.state()['1'].record == again['1']


def test_an_edited_row_is_outdated_and_left_to_do(
    measured: Batch, client: Client
) -> None:
    client.wait(measured.reduce(['1', '2']))
    edit(measured, '1', thickness=2.0)

    assert left_to_reduce(measured.state()) == ['1']


def test_changed_settings_make_every_result_outdated(
    measured: Batch, client: Client
) -> None:
    client.wait(measured.reduce(['1', '2']))

    measured.settings = OTHER_SETTINGS

    states = measured.state()
    assert [states[k].outdated for k in ('1', '2')] == [True, True]
    assert earlier_settings(states) == ['1', '2']
    assert left_to_reduce(states) == []


def test_changed_names_the_cell_and_the_setting_that_differ(
    measured: Batch, client: Client
) -> None:
    client.wait(measured.reduce(['1', '2']))
    edit(measured, '1', thickness=2.0)

    measured.settings = OTHER_SETTINGS

    changed = {k: s.changed for k, s in measured.state().items()}

    assert changed == {'1': {'binning', 'thickness'}, '2': {'binning'}}


def test_a_column_made_with_the_value_of_its_setting_changes_no_row(
    measured: Batch, client: Client
) -> None:
    measured.settings = {**SETTINGS, 'offset': {'value': 0.5}}
    client.wait(measured.reduce(['1', '2']))
    offset = pd.Series([{'value': 0.5}] * 2, index=measured.frame().index, dtype=object)

    measured.put(measured.frame().assign(offset=offset))

    assert {k: s.changed for k, s in measured.state().items()} == {
        '1': set(),
        '2': set(),
    }


def test_a_second_batch_on_the_label_reads_the_same_changed(
    measured: Batch, client: Client
) -> None:
    client.wait(measured.reduce(['1', '2']))
    second = Batch(client, STAGED, label='scan', rows=measured.frame())
    for batch in (measured, second):
        edit(batch, '1', thickness=2.0)

    def changed(batch: Batch) -> dict[str, frozenset[str]]:
        return {k: s.changed for k, s in batch.state().items()}

    assert changed(second) == changed(measured) == {'1': {'thickness'}, '2': set()}


def test_a_default_written_out_and_a_file_by_name_do_not_change_a_row(
    measured: Batch, client: Client
) -> None:
    measured.settings = {}
    client.wait(measured.reduce(['1', '2']))
    edit(measured, '1', sample='run:1', transmission='run:1001', thickness=1.0)
    measured.settings = {'binning': {'bins': 2}, 'offset': None}

    states = measured.state()

    assert {k: s.changed for k, s in states.items()} == {'1': set(), '2': set()}
    assert left_to_reduce(states) == []


def test_a_cell_emptied_is_one_problem(measured: Batch, client: Client) -> None:
    client.wait(measured.reduce(['1']))
    edit(measured, '1', sample=pd.NA)

    state = measured.state()['1']

    assert [(e.loc, e.msg) for e in state.problems] == [(('sample',), 'empty')]


def test_a_file_named_before_it_is_measured_is_a_problem_until_it_is(
    measured: Batch, sample: Measure
) -> None:
    edit(measured, '9', sample='run:9', transmission='run:1001')

    before = measured.state()
    sample(9, 'oil')
    after = measured.state()

    assert [e.loc for e in before['9'].problems] == [('sample',)]
    assert ready(before) == ['1', '2']
    assert measured.state(compare=False)['9'].problems == ()
    assert (after['9'].problems, after['9'].text) == ((), 'ready')


def _request_of(batch: Batch, key: str) -> Any:
    """The request of the row of ``key``, its empty thickness given."""
    cells = batch.frame()[['sample', 'transmission']].to_dict('index')[key]
    params = {**as_request(batch.spec.params, cells), 'thickness': 1.0}
    return batch.template().fill(params=params)


def test_an_older_record_that_ends_later_is_not_the_result(
    measured: Batch, client: Client, gate: Gate
) -> None:
    gate.close()
    older = measured.reduce(['1'])['1']
    measured.state()
    newer = client.submit(_request_of(measured, '1'), label='scan', member='1')
    client.cancel([older])

    assert measured.state()['1'].record == newer
    gate.open()
    client.wait(newer)


def test_a_record_under_another_label_is_not_a_rows_result(
    measured: Batch, client: Client
) -> None:
    mine = measured.reduce(['1'])['1']
    client.wait(mine)
    measured.state()

    client.compute(_request_of(measured, '1'), label='other', member='1')

    assert measured.state()['1'].record == mine


def test_a_cell_refused_is_a_problem_of_its_row_and_not_changed(
    measured: Batch, client: Client
) -> None:
    client.wait(measured.reduce(['1', '2']))
    edit(measured, '1', sample='run:99')

    state = measured.state()['1']

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
        old = Batch(client, PLAIN, label='s', rows=rows(client))
        client.wait(old.reduce(['1', '2']))
        yield client, changing


@pytest.mark.parametrize('spec', [PLAIN, PLAIN_V2])
def test_settings_refused_are_raised_naming_no_row(
    bound_twice: tuple[Client, Changing], sample: Measure, spec: Any
) -> None:
    client, _ = bound_twice
    sample(3, 'oil')
    batch = Batch(client, spec, label='s', rows=rows(client))

    batch.settings = {'binning': {'bins': 0}}

    with pytest.raises(SubmitError) as error:
        batch.state()

    assert [(e.entry, e.loc[0]) for e in error.value.errors] == [(None, 'binning')]
    assert batch.state(compare=False)['3'].problems == ()


def test_a_changed_setting_is_found_when_no_result_s_files_are_known(
    bound_twice: tuple[Client, Changing],
) -> None:
    client, changing = bound_twice
    batch = Batch(client, PLAIN, label='s', rows=rows(client))
    changing.names |= dict.fromkeys(FileRef(file=f) for f in batch.frame()['sample'])
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


def strict_batch(client: Client, cells: dict[str, dict[str, Any]]) -> Batch:
    table = pd.DataFrame.from_dict(
        {
            k: {'sample': 'run:1', 'counts': None, 'numbers': None, **c}
            for k, c in cells.items()
        },
        orient='index',
    )
    return Batch(client, STRICT, label='s', rows=table)


def test_a_value_kept_is_not_taken_for_an_equal_one_of_another_type(
    strict: Client,
) -> None:
    cached = strict_batch(strict, {'a': {'counts': [1]}})
    problems(cached)

    frame = cached.frame()
    frame.at['a', 'counts'] = [True]
    cached.put(frame)

    assert problems(cached)['a'] != []
    assert problems(cached) == problems(strict_batch(strict, {'a': {'counts': [True]}}))


def test_a_value_kept_at_one_column_is_not_taken_at_another(strict: Client) -> None:
    cells = {'a': {'numbers': [True]}, 'b': {'counts': [True]}}
    cached = strict_batch(strict, {'a': cells['a']})
    problems(cached)

    frame = cached.frame()
    frame.loc['b'] = pd.Series({'sample': 'run:1', 'counts': [True]}, dtype=object)
    cached.put(frame)

    assert problems(cached) == problems(strict_batch(strict, cells))
    assert problems(cached) == {'a': [], 'b': [('counts', 0)]}


def test_a_batch_reads_a_file_by_run_as_a_new_one_does_after_a_put(
    bound_twice: tuple[Client, Changing],
) -> None:
    client, changing = bound_twice

    def by_run() -> Batch:
        batch = Batch(client, PLAIN, label='s', rows=rows(client))
        edit(batch, '1', sample='run:1')
        return batch

    def changed(batch: Batch) -> frozenset[str]:
        return batch.state()['1'].changed

    batch = by_run()
    before = changed(batch)
    changing.names[file(run=1)] = FileRef(file='pid:20.500.12269/water')
    kept = changed(batch)
    batch.put(batch.frame())

    assert (before, kept) == (set(), set())
    assert changed(batch) == changed(by_run()) == {'sample'}


def test_a_row_naming_its_file_by_run_is_changed_only_once_its_cell_is(
    measured: Batch, client: Client
) -> None:
    edit(measured, '1', sample='run:1')
    client.wait(measured.reduce(['1']))

    first, again = (measured.state()['1'].changed for _ in range(2))
    edit(measured, '1', sample='run:2')

    assert (first, again) == (set(), set())
    assert measured.state()['1'].changed == {'sample'}


def test_a_result_of_another_spec_version_is_the_rows_result_and_outdated(
    bound_twice: tuple[Client, Changing],
) -> None:
    client, _ = bound_twice
    new = Batch(client, PLAIN_V2, label='s', rows=rows(client))
    client.wait(new.reduce(['2']))

    states = new.state()

    assert [r.member for r in client.records(label='s')] == ['1', '2', '2']
    assert states['1'].record == client.records(label='s')[0]
    assert (states['1'].spec_changed, states['2'].spec_changed) == (True, False)
    assert left_to_reduce(states) == ['1']


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
        old = Batch(client, PLAIN, label='s', rows=rows(client))
        client.wait(old.reduce(['1']))
    again = Workspace(
        proposal='p1',
        files=files,
        bind=[function_binding(redefined, lambda _: Outputs(total=1.0))],
        log=Log(kept),
    )
    with Client(again, submitter='anna', on_close=again.close) as client:
        new = Batch(client, redefined, label='s', rows=rows(client))

        state = new.state()['1']

        assert (state.status, state.spec_changed) == (Status.COMPLETED, True)
        assert state.text == 'outdated'


def test_the_state_of_chosen_rows_reads_only_the_records_that_changed(
    client: Client, sample: Measure
) -> None:
    counting = CountingClient(client)
    sample(1, 'water')
    sample(2, 'sds')
    batch = Batch(counting, STAGED, label='scan', rows=rows(client))  # type: ignore[arg-type]
    client.wait(batch.reduce(['1', '2']))
    batch.state()
    counting.reads.clear()

    batch.state(['2'])

    assert counting.reads['records'] == 0


def test_a_row_running_is_not_submitted_twice(
    measured: Batch, client: Client, gate: Gate
) -> None:
    gate.close()
    first = measured.reduce(ready(measured.state()))

    assert ready(measured.state()) == []
    gate.open()
    client.wait(first)


def test_cancel_reaches_what_was_just_submitted(
    measured: Batch, client: Client, gate: Gate
) -> None:
    gate.close()
    records = measured.reduce(['1', '2'])

    measured.cancel()
    gate.open()

    assert client.wait(records) == {'1': 'cancelled', '2': 'cancelled'}
    assert left_to_reduce(measured.state()) == ['1', '2']


def test_cancel_leaves_other_rows_when_given_keys(
    measured: Batch, client: Client, gate: Gate
) -> None:
    gate.close()
    records = measured.reduce(['1', '2'])

    measured.cancel(['1'])

    assert client.status(records['1']) is Status.CANCELLED
    assert client.status(records['2']) is Status.PENDING
    gate.open()


@pytest.fixture
def chain(client: Client, sample: Measure) -> tuple[Batch, Batch]:
    """A batch that finds an offset per sample, and one that reads it per row."""
    sample(1, 'water')
    sample(2, 'sds')
    find = Batch(client, FIND, label='find', rows=rows_of(client, SAMPLES))
    reduce = Batch(client, PLAIN, label='reduce', rows=rows(client).assign(offset=None))
    return find, reduce


def test_a_row_can_read_the_record_of_its_row_in_another_batch(
    chain: tuple[Batch, Batch], client: Client
) -> None:
    find, reduce = chain
    found = find.reduce(['1', '2'])  # the second batch need not wait
    for key, record in found.items():
        edit(reduce, key, offset=cell_of(record.ref('offset')))

    records = reduce.reduce(['1', '2'])
    client.wait(records)

    assert client.output(records['2'], 'total') == 4.0
    assert records['2'].request.params['offset'] == found['2'].ref('offset')


def test_a_cell_reading_a_failed_record_stops_only_its_row(
    chain: tuple[Batch, Batch], client: Client
) -> None:
    _, reduce = chain
    reduce.settings = {'binning': {'bins': 1}}
    failed = reduce.reduce(['1'])
    client.wait(failed)
    edit(reduce, '2', offset=cell_of(failed['1'].ref('total')))

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
    edit(reduce, '2', offset=cell_of(cancelled['1'].ref('total')))

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
    edit(reduce, '1', offset='nope.x')
    edit(reduce, '2', offset=cell_of(failed['1'].ref('total')))

    whole, chosen = reduce.state()['2'], reduce.state(['2'])['2']

    assert [e.msg for e in whole.problems] == ['the record it reads is failed']
    assert chosen.problems == whole.problems


def test_a_record_of_another_submitter_is_a_rows_result(
    measured: Batch, client: Client, other: Client
) -> None:
    client.wait(measured.reduce(['1']))

    theirs = other.compute(_request_of(measured, '1'), label='scan', member='1')

    assert measured.state()['1'].record == theirs


def test_persisting_goes_with_the_batch(files: CorrectedFiles, sample: Measure) -> None:
    store = FakeStore()
    binding = function_binding(PLAIN, lambda _: Outputs(total=1.0))
    sample(1, 'water')
    with local(proposal='p1', files=files, bind=[binding], store=store) as client:
        batch = Batch(client, PLAIN, label='s', rows=rows(client), persist=True)
        records = batch.reduce(['1'])
        client.wait(records)

        assert (records['1'].id, 'total') in store


def test_a_row_running_is_refused_when_picked(
    measured: Batch, client: Client, gate: Gate
) -> None:
    gate.close()
    first = measured.reduce(['1'])

    with pytest.raises(SubmitError, match='1: still running'):
        measured.reduce(['1'])
    gate.open()
    client.wait(first)
    assert len(client.records(label='scan')) == 1


def test_cancel_reaches_a_row_removed_since(
    measured: Batch, client: Client, gate: Gate
) -> None:
    gate.close()
    records = measured.reduce(['1'])
    measured.put(measured.frame().drop('1'))

    measured.cancel()
    gate.open()

    assert client.wait(records) == {'1': 'cancelled'}
