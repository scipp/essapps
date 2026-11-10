# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)


"""Accumulators: states, reads, release, drivers, several tables."""

import gc
import re
import threading
from collections.abc import Mapping
from typing import Any

import pytest

from ess.apps import (
    Client,
    NotReadable,
    Record,
    Request,
    Status,
    SubmitError,
    Template,
    Workspace,
    file,
    local,
)
from ess.apps.testing import FakeFiles
from ess.spec import DriverBinding, OutputRef, function_binding

from . import toys


def _parts(*records: Record) -> dict[str, list[Any]]:
    """The params of TOTAL over the values of ``records``."""
    return {'parts': [r.ref('value') for r in records]}


def _refuse(fixed: Mapping[str, Any]) -> toys.Fold:
    """Fails to make a driver."""
    raise ValueError('cannot open')


def test_a_template_that_fixes_a_repeated_row_is_refused(
    client: Client,
) -> None:
    load = client.compute(toys.load_request(1))
    fixed = Template(
        toys.STITCH,
        params={
            'reference': toys.Reference(total=1.0),
            'high': [load.ref('value')] * 2,
        },
        blanks=('low',),
    )

    with pytest.raises(SubmitError, match=r'^high\[1\]: repeats high\[0\]'):
        client.accumulator(fixed)


def test_an_accumulator_value_that_does_not_fit_the_spec_fails_the_reader(
    files: FakeFiles, loading: threading.Event
) -> None:
    def load(params: toys.Sample) -> toys.LoadOutputs:
        loading.wait(timeout=5)
        return toys.load_run(params)

    bind = [
        function_binding(toys.LOAD, load),
        function_binding(toys.SHIFT, toys.shift),
        DriverBinding(toys.TOTAL, toys.Averaging),  # returns MeanAndSpread
    ]
    with local(proposal='p1', files=files, bind=bind) as client:
        total = toys.total_accumulator(client)
        loading.clear()
        pending = client.submit(toys.load_request(1))
        total.push(parts=[pending.ref('value')])
        frozen = client.freeze(total)
        read = client.submit(toys.read_request(frozen))  # the freeze is pending
        loading.set()
        client.wait([frozen, read])

        assert client.failure([frozen, read]) == [
            'the workflow of total/v1 returned MeanAndSpread, not Value',
            f'record {frozen.id} failed',
        ]


def test_an_accumulator_is_not_submitted(client: Client) -> None:
    with pytest.raises(TypeError, match='freeze it and reference the record'):
        client.submit(toys.total_accumulator(client))  # type: ignore[call-overload]


def test_an_accumulator_waits_for_its_fixed_values_and_reads_them_once(
    client: Client,
    scaled_sum: toys.ScaledSum,
    files: FakeFiles,
    loading: threading.Event,
) -> None:
    loading.clear()
    offset = client.submit(toys.load_request(2))
    threading.Timer(0.05, loading.set).start()
    fixed = {'offset': offset.ref('value')}  # and the default scale and labels
    total = client.accumulator(Template(toys.SUM, params=fixed, blanks=('runs',)))
    runs = [file(run=n) for n in (1, 2)]
    for run in runs:
        total.push(runs=[run])
    value = client.output(total, 'value')
    provenance = client.provenance(total)  # of the state after the pushes so far
    frozen = client.freeze(total)
    read = client.compute(toys.read_request(frozen))
    plain = client.compute(toys.SUM, params={'runs': runs, **fixed})

    assert (
        value
        == client.output(read, 'value')
        == client.output(plain, 'value')
        == toys.Amount(value=5.0)
    )
    opened = {
        'scale': toys.Factor(),
        'labels': toys.Labels(),
        'offset': toys.Amount(value=2.0),
    }
    assert scaled_sum.opened == [opened] * 2  # the accumulator and the plain request
    assert total.template == Template(toys.SUM, params=fixed, blanks=('runs',))
    assert client.provenance(read).records() == [frozen, offset]
    resolved = {files.resolve(run) for run in runs}
    assert set(client.provenance(read).files()) == resolved  # offset's run too
    assert provenance == client.provenance(plain)
    assert provenance.records() == [offset]
    assert set(provenance.files()) == resolved


def test_an_accumulator_s_values_reach_the_driver_as_the_log_holds_them(
    client: Client, scaled_sum: toys.ScaledSum, files: FakeFiles
) -> None:
    acc = client.accumulator(
        Template(
            toys.SUM, params={'labels': toys.Labels(tags=['a', 'b'])}, blanks=('runs',)
        )
    )
    acc.push(runs=[file(run=1)])

    assert client.output(acc, 'value') == toys.Amount(value=1.0)
    assert acc.template.params == {'labels': {'tags': ['a', 'b']}}
    (run,) = client.provenance(acc).request.params['runs']
    assert run == files.resolve(file(run=1))
    assert scaled_sum.opened[0]['labels'] == toys.Labels(tags=['a', 'b'])
    (held,) = scaled_sum.held[0].runs
    assert held == files.path(run)


def test_an_accumulator_of_a_plain_function_reads_as_the_plain_request(
    client: Client,
) -> None:
    loads = client.compute([toys.load_request(n) for n in (1, 2)])
    reference = {'reference': toys.Reference(total=30.0)}
    stitched = client.accumulator(
        Template(toys.STITCH, params=reference, blanks=('low', 'high'))
    )
    pushes = [('low', loads[0]), ('high', loads[1]), ('low', loads[1])]
    pushes.append(('high', loads[0]))
    reads, plains = [], []
    for n, (table, load) in enumerate(pushes, start=1):
        stitched.push(**{table: [load.ref('value')]})
        tables = {
            t: [x.ref('value') for u, x in pushes[:n] if u == t]
            for t in ('low', 'high')
        }
        plain = client.compute(toys.STITCH, params={**reference, **tables})
        reads.append(client.output(stitched, 'curve'))
        plains.append(client.output(plain, 'curve'))
    client.release(loads)  # the accumulator keeps the values of its rows

    assert reads == plains
    assert reads == [[30.0], [10.0, 20.0], [6.0, 12.0, 12.0], [5.0, 10.0, 10.0, 5.0]]
    assert client.output(stitched, 'curve') == [5.0, 10.0, 10.0, 5.0]


def test_a_read_is_a_copy_that_a_later_push_leaves_unchanged(
    in_place: Client, loads: list[Record]
) -> None:
    client = in_place
    total = toys.array_total_accumulator(client)
    total.push(parts=[loads[0].ref('value')])
    first = client.output(total, 'value')
    total.push(parts=[loads[1].ref('value')])

    assert toys.listed(first) == [1.0]
    assert toys.listed(client.output(total, 'value')) == [3.0]


def test_pushes_into_several_tables_give_the_plain_request_over_them(
    in_place: Client, loads: list[Record]
) -> None:
    client = in_place
    sides = toys.sides_accumulator(client)
    pushes = [('backgrounds', loads[0]), ('samples', loads[1]), ('samples', loads[0])]
    reads, plains = [], []
    for n, (table, load) in enumerate(pushes, start=1):
        sides.push(**{table: [load.ref('value')]})
        tables = {
            t: [x.ref('value') for u, x in pushes[:n] if u == t]
            for t in ('samples', 'backgrounds')
        }
        plain = client.compute(toys.SIDES, params=tables)
        reads.append(toys.listed_sides(client.output(sides)))
        plains.append(toys.listed_sides(client.output(plain)))

    assert reads == plains
    assert [r['difference'] for r in reads] == [[-1.0], [1.0], [2.0]]
    assert client.provenance(sides).request == plain.request
    assert {r.id for r in client.provenance(sides).records()} == {x.id for x in loads}


def test_readers_of_one_state_compute_its_outputs_once(
    in_place: Client, loads: list[Record], subtracting: toys.Subtracting
) -> None:
    client = in_place
    sides = toys.sides_accumulator(client)
    sides.push(samples=[loads[0].ref('value')])
    both = client.output(sides)
    again = client.output(sides, 'total')  # no reader holds the state
    sides.push(backgrounds=[loads[1].ref('value')])
    after = client.output(sides, 'difference')

    assert subtracting.computed == 2  # once per state
    assert toys.listed_sides(both) == {'difference': [1.0], 'total': [1.0]}
    assert [toys.listed(again), toys.listed(after)] == [[1.0], [-1.0]]


def test_a_state_is_read_once_its_plain_request_would_be_accepted(
    in_place: Client, loads: list[Record]
) -> None:
    client = in_place
    both = client.accumulator(Template(toys.BOTH, blanks=('samples', 'backgrounds')))
    both.push(samples=[loads[0].ref('value')])
    tables = {'samples': [loads[0].ref('value')], 'backgrounds': []}
    with pytest.raises(SubmitError, match='backgrounds: the table has') as refused:
        client.submit(toys.BOTH, params=tables)
    reason = re.escape(str(refused.value))

    for read in (lambda: client.output(both), lambda: client.provenance(both)):
        with pytest.raises(NotReadable, match=f'^{reason}$'):
            read()
    both.push(backgrounds=[loads[1].ref('value')])
    tables['backgrounds'] = [loads[1].ref('value')]
    plain = client.compute(toys.BOTH, params=tables)
    assert toys.listed_sides(client.output(both)) == {
        'difference': [-1.0],
        'total': [3.0],
    }
    assert toys.listed_sides(client.output(plain)) == toys.listed_sides(
        client.output(both)
    )
    assert client.provenance(both).request == plain.request


def test_a_state_with_nothing_pushed_is_read_as_the_plain_request_over_no_rows(
    client: Client,
) -> None:
    summed = client.accumulator(Template(toys.SUM, blanks=('runs',)))
    total = toys.total_accumulator(client)
    plain = client.compute(toys.SUM, params={'runs': []})
    with pytest.raises(SubmitError, match='parts: the table has no row') as refused:
        client.submit(toys.TOTAL, params={'parts': []})

    assert (
        client.output(summed, 'value')
        == client.output(plain, 'value')
        == toys.Amount(value=0.0)
    )
    assert client.provenance(summed).request == plain.request
    with pytest.raises(NotReadable, match=f'^{re.escape(str(refused.value))}$'):
        client.output(total)


def test_the_blanks_of_an_accumulator_are_its_tables(client: Client) -> None:
    refused = {
        r"tables, \['runs'\], not \[\]$": Template(toys.SUM),
        r"not \['scale'\]$": Template(toys.SUM, blanks=('scale',)),
        r"not \['runs', 'scale'\]$": Template(toys.SUM, blanks=('runs', 'scale')),
        r"not \['size'\]$": Template(toys.WALK, blanks=('size',)),  # a field of a row
        r"tables, \['low', 'high'\], not \[\]$": Template(toys.STITCH),
        r"not \['reference'\]$": Template(toys.STITCH, blanks=('reference',)),
    }
    for reason, template in refused.items():
        with pytest.raises(SubmitError, match=reason):
            client.accumulator(template)


def test_an_accumulator_needs_a_binding_that_makes_accumulators(
    client: Client,
) -> None:
    refused = r'^scale/v1: its binding makes no accumulators$'
    with pytest.raises(SubmitError, match=refused):
        client.accumulator(Template(toys.SCALE, blanks=('scaling',)))


def test_an_accumulator_refuses_a_template_that_a_request_would_refuse(
    client: Client,
) -> None:
    released = client.compute(toys.load_request(1))
    client.release(released)
    # No case of an unknown file: no accumulator here fixes a file field, and
    # the template of an accumulator is checked as that of a stage is.
    refused: list[dict[str, Any]] = [
        {'scale': {'factor': 'x'}},  # request_types refuses it
        {'offset': released.ref('value')},  # a record not kept
    ]
    for params in refused:
        with pytest.raises(SubmitError) as requested:
            client.submit(toys.SUM, params={**params, 'runs': []})
        with pytest.raises(SubmitError) as opened:
            client.accumulator(Template(toys.SUM, params=params, blanks=('runs',)))
        assert str(opened.value) == str(requested.value)


def test_field_validators_apply_at_the_opening_and_table_rules_at_reads(
    client: Client,
) -> None:
    flipped = {'scale': {'factor': -2.0}}
    walk = client.accumulator(Template(toys.WALK, params=flipped, blanks=('steps',)))
    walk.push(steps=[toys.Step(size=1.0)])
    with pytest.raises(
        SubmitError, match='steps: the table has one row; it needs at least 2'
    ) as refused:
        client.submit(toys.WALK, params={**flipped, 'steps': [toys.Step(size=1.0)]})
    reason = f'^{re.escape(str(refused.value))}$'
    with pytest.raises(NotReadable, match=reason):
        client.output(walk)
    walk.push(steps=[toys.Step(size=2.0)])

    assert client.output(walk, 'value') == toys.Amount(value=6.0)


def test_an_accumulator_adds_each_row_once(
    summed: Client, summing: toys.Sum, files: FakeFiles
) -> None:
    client = summed
    for n in (3, 4):
        files.measure(n, float(n))
    total = toys.total_accumulator(client)
    sums = []
    for n in (1, 2, 3, 4):
        total.push(parts=[client.compute(toys.load_request(n)).ref('value')])
        sums.append(client.output(total, 'value'))

    assert sums == [toys.Amount(value=v) for v in (1.0, 3.0, 6.0, 10.0)]
    assert summing.pushed == 4


def test_no_request_template_or_push_references_an_accumulator(
    client: Client,
) -> None:
    load = client.compute(toys.load_request(1))
    total, other = toys.total_accumulator(client), toys.total_accumulator(client)
    total.push(parts=[load.ref('value')])
    refused = re.escape(
        f'accumulator {total.id}: an accumulator cannot be referenced; '
        'freeze it first, and reference the record of the freeze'
    )
    written = {'@ref': 'output', 'record': total.id, 'output': 'value'}
    for ref in (OutputRef(record=total.id, output='value'), written):
        with pytest.raises(SubmitError, match=rf'^source: {refused}$'):
            client.submit(toys.SHIFT, params={'source': ref})
        for accumulator in (total, other):
            with pytest.raises(SubmitError, match=rf'^parts\[0\]: {refused}$'):
                accumulator.push(parts=[ref])
        with pytest.raises(SubmitError, match=rf'^source: {refused}$'):
            client.stage(
                Template(toys.SHIFT, params={'source': ref}, blanks=('shift',))
            )
        with pytest.raises(SubmitError, match=rf'^offset: {refused}$'):
            client.accumulator(
                Template(toys.SUM, params={'offset': ref}, blanks=('runs',))
            )
    frozen = client.freeze(total)
    with pytest.raises(SubmitError, match=rf'^source: {refused}$'):
        client.submit(toys.SHIFT, params={'source': written})
    shifted = client.compute(toys.read_request(frozen))
    assert client.output(shifted, 'value') == toys.Amount(value=1.0)


def test_reads_while_rows_are_pushed_each_see_one_state(
    in_place: Client, files: FakeFiles
) -> None:
    client = in_place
    runs = [files.measure(n, float(n)) for n in range(3, 23)]
    loads = client.compute(
        [Request(toys.ARRAY, params={'sample': run}) for run in runs]
    )
    total = toys.array_total_accumulator(client)
    total.push(parts=[loads[0].ref('value')])
    pushing = threading.Thread(
        target=lambda: [total.push(parts=[x.ref('value')]) for x in loads[1:]]
    )
    pushing.start()
    reads = [toys.listed(client.output(total, 'value')) for _ in range(20)]
    pushing.join()

    sums = [[float(sum(range(3, 3 + n)))] for n in range(1, 21)]
    assert all(read in sums for read in reads)  # the sum over a whole push each
    assert reads == sorted(reads)  # a later read sees a later state


def test_an_output_the_spec_lacks_is_not_read_from_an_accumulator(
    client: Client,
) -> None:
    total = toys.total_accumulator(client)
    total.push(parts=[toys.Amount(value=1.0)])

    with pytest.raises(KeyError, match="total/v1 has no output 'extra'"):
        client.output(total, 'extra')


def test_an_optional_output_left_out_is_not_read(client: Client) -> None:
    parts = _parts(*client.compute([toys.load_request(n) for n in (1, 2)]))
    record = client.compute(toys.SPREAD, params=parts)
    spread = client.accumulator(Template(toys.SPREAD, blanks=('parts',)))
    spread.push(**parts)
    read_live = client.output(spread)
    with pytest.raises(NotReadable, match='the workflow did not return it'):
        client.output(spread, 'spread')
    frozen = client.freeze(spread)
    client.wait(frozen)

    assert (
        client.output(record)
        == read_live
        == client.output(frozen)
        == {'mean': toys.Amount(value=1.5)}
    )
    for left_out in (record, frozen):
        with pytest.raises(NotReadable, match='the workflow did not return it'):
            client.output(left_out, 'spread')
        reason = f'record {left_out.id} output spread: the workflow did not return it'
        with pytest.raises(SubmitError, match=rf'^source: {reason}$'):
            client.submit(toys.read_request(left_out, 'spread'))


def test_concurrent_readers_of_one_state_compute_its_outputs_once(
    in_place: Client, loads: list[Record], subtracting: toys.Subtracting
) -> None:
    client = in_place
    sides = toys.sides_accumulator(client)
    sides.push(samples=[loads[0].ref('value')])
    subtracting.compute.clear()
    read = toys.start_in_thread(lambda: client.output(sides, 'difference'))
    subtracting.computing.wait(timeout=5)  # the read computes, and holds the state
    other = toys.start_in_thread(lambda: client.output(sides, 'total'))
    frozen = client.freeze(sides)
    subtracting.compute.set()
    assert toys.listed(read.result(timeout=5)) == [1.0]
    assert toys.listed(other.result(timeout=5)) == [1.0]
    assert toys.listed(client.output(frozen, 'difference')) == [1.0]
    assert subtracting.computed == 1


def test_reading_an_accumulator_whose_driver_returns_no_outputs_instance_raises(
    client: Client,
) -> None:
    untyped = client.accumulator(Template(toys.UNTYPED, blanks=('parts',)))
    untyped.push(parts=[toys.Amount(value=1.0)])

    returned = 'the workflow of untyped/v1 returned dict, not Value'
    for _ in range(2):  # computed again at each read
        with pytest.raises(NotReadable, match=f'^{returned}$'):
            client.output(untyped)


def test_a_read_while_a_push_adds_pins_the_state_after_it(
    in_place: Client, loads: list[Record], subtracting: toys.Subtracting
) -> None:
    client = in_place
    sides = toys.sides_accumulator(client)
    sides.push(samples=[loads[0].ref('value')])
    client.output(sides)  # the first push is added
    subtracting.adding.clear()
    subtracting.go.clear()
    sides.push(backgrounds=[loads[1].ref('value')])
    subtracting.adding.wait(timeout=5)  # the push adds
    read = toys.start_in_thread(lambda: client.output(sides, 'difference'))
    plain = client.provenance(sides).request
    frozen = client.freeze(sides)  # returns at once, and pins the same state

    assert frozen.request == plain
    assert len(plain.params['backgrounds']) == 1
    with pytest.raises(TimeoutError):
        read.result(timeout=0.05)  # waits for the state, as for a record
    subtracting.go.set()
    assert toys.listed(read.result(timeout=5)) == [-1.0]
    assert toys.listed(client.output(frozen, 'difference')) == [-1.0]


def test_a_record_that_a_push_waits_for_and_that_fails_stops_the_accumulator(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    pending = client.submit(toys.load_request(1))
    total = toys.total_accumulator(client)
    total.push(parts=[pending.ref('value')])
    frozen = client.freeze(total)  # waits for the state
    read = client.submit(toys.read_request(frozen))
    client.cancel(pending)
    loading.set()

    stopped = f'the accumulator stopped: push 0: record {pending.id} cancelled'
    assert client.wait(read) is Status.FAILED
    assert client.failure([frozen, read]) == [stopped, f'record {frozen.id} failed']
    with pytest.raises(SubmitError, match=f'^{stopped}$'):
        total.push(parts=[pending.ref('value')])


def test_a_push_that_fails_to_add_stops_the_accumulator(
    summed: Client, summed_loads: list[Record], summing: toys.Sum
) -> None:
    client = summed
    total = toys.total_accumulator(client)
    total.push(parts=[summed_loads[0].ref('value')])
    before = client.output(total, 'value')
    summing.failing = True
    summing.go.clear()
    total.push(parts=[summed_loads[1].ref('value')])  # logged; fails once it adds
    frozen = client.freeze(total)
    summing.go.set()

    stopped = 'the accumulator stopped: push 1 failed: ValueError: cannot add'
    assert client.wait(frozen) is Status.FAILED
    assert client.failure(frozen) == stopped
    assert len(frozen.request.params['parts']) == 2  # the push is in the log
    with pytest.raises(SubmitError, match=f'^{stopped}$'):
        total.push(parts=[summed_loads[2].ref('value')])
    with pytest.raises(SubmitError, match=f'^{stopped}$'):
        client.freeze(total)  # it may be half added
    for read in (lambda: client.output(total), lambda: client.provenance(total)):
        with pytest.raises(NotReadable, match=f'^{stopped}$'):
            read()
    assert before == toys.Amount(value=1.0)


def test_an_accumulator_returns_at_once_and_stops_if_it_fails_to_open(
    files: FakeFiles,
) -> None:
    with local(
        proposal='p1', files=files, bind=[DriverBinding(toys.SUM, _refuse)]
    ) as client:
        summed = client.accumulator(
            Template(toys.SUM, blanks=('runs',))
        )  # returns first

        stopped = 'the accumulator stopped: opening failed: ValueError: cannot open'
        with pytest.raises(NotReadable, match=f'^{stopped}$'):
            client.output(summed)
        with pytest.raises(SubmitError, match=f'^{stopped}$'):
            summed.push(runs=[file(run=1)])


def test_a_stopped_driver_is_dropped_before_the_accumulator_is_released(
    in_place: Client, loads: list[Record], subtracting: toys.Subtracting
) -> None:
    client = in_place
    sides = toys.sides_accumulator(client)
    sides.push(samples=[loads[0].ref('value')])
    client.output(sides)  # the first push is added
    subtracting.refusing = True
    sides.push(samples=[loads[1].ref('value')])  # fails to add

    with pytest.raises(NotReadable, match='push 1 failed: ValueError: cannot add'):
        client.output(sides, 'difference')
    gc.collect()
    assert subtracting.held[0]() is None  # nothing reads it any more


def test_closing_the_workspace_drops_the_pushes_that_no_read_needs(
    summed_workspace: Workspace,
    summed: Client,
    summed_loads: list[Record],
    summing: toys.Sum,
) -> None:
    summing.go.clear()
    total = toys.total_accumulator(summed)
    for load in summed_loads[:2]:
        total.push(parts=[load.ref('value')])
    summing.adding.wait(timeout=5)
    threading.Timer(0.05, summing.go.set).start()  # so that closing can return
    summed_workspace.close()

    assert summing.pushed == 1  # the push being added when the workspace closed


def test_a_released_accumulator_drops_the_pushes_once_their_reader_is_cancelled(
    summed_workspace: Workspace,
    summed: Client,
    summed_loads: list[Record],
    summing: toys.Sum,
) -> None:
    client = summed
    summing.go.clear()
    total = toys.total_accumulator(client)
    for load in summed_loads:
        total.push(parts=[load.ref('value')])
    frozen = client.freeze(total)  # pins three pushes
    summing.adding.wait(timeout=5)
    client.release(total)  # the freeze keeps the pushes
    client.release(frozen)  # and no longer once it is cancelled
    assert client.status(frozen) is Status.CANCELLED
    summing.go.set()
    summed_workspace.close()

    assert summing.pushed == 1  # the push being added when the freeze was cancelled


def test_a_released_accumulator_adds_the_pushes_up_to_the_last_state_frozen(
    summed: Client, summed_loads: list[Record], summing: toys.Sum
) -> None:
    client = summed
    summing.go.clear()
    total = toys.total_accumulator(client)
    total.push(parts=[summed_loads[0].ref('value')])
    total.push(parts=[summed_loads[1].ref('value')])
    frozen = client.freeze(total)
    client.release(total)
    summing.go.set()

    assert client.output(frozen, 'value') == toys.Amount(value=3.0)
    assert summing.pushed == 2


def test_a_released_accumulator_drops_the_pushes_after_the_last_state_read(
    in_place_workspace: Workspace,
    in_place: Client,
    loads: list[Record],
    subtracting: toys.Subtracting,
) -> None:
    client = in_place
    sides = toys.sides_accumulator(client)
    sides.push(samples=[loads[0].ref('value')])
    subtracting.compute.clear()
    read = toys.start_in_thread(lambda: client.output(sides, 'difference'))
    subtracting.computing.wait(timeout=5)  # the read holds the state of one push
    subtracting.adding.clear()
    sides.push(backgrounds=[loads[1].ref('value')])  # waits for the read
    client.release(sides)
    subtracting.compute.set()

    assert toys.listed(read.result(timeout=5)) == [1.0]
    in_place_workspace.close()
    assert not subtracting.adding.is_set()  # the last push was dropped


def test_a_released_accumulator_still_adds_the_pushes_logged(
    in_place: Client, loads: list[Record], subtracting: toys.Subtracting
) -> None:
    client = in_place
    sides = toys.sides_accumulator(client)
    sides.push(samples=[loads[0].ref('value')])
    subtracting.compute.clear()
    read = toys.start_in_thread(lambda: client.output(sides, 'difference'))
    subtracting.computing.wait(timeout=5)  # the read holds the state of one push
    sides.push(backgrounds=[loads[1].ref('value')])  # waits for the read
    frozen = client.freeze(sides)
    client.release(sides)  # the read and the freeze are still readers of their states
    subtracting.compute.set()

    assert toys.listed(read.result(timeout=5)) == [1.0]
    assert toys.listed(client.output(frozen, 'difference')) == [-1.0]


def test_a_released_accumulator_takes_no_pushes_or_reads_and_a_read_returns(
    in_place: Client, loads: list[Record], subtracting: toys.Subtracting
) -> None:
    client = in_place
    sides = toys.sides_accumulator(client)
    sides.push(samples=[loads[0].ref('value')])
    subtracting.compute.clear()
    read = toys.start_in_thread(lambda: client.output(sides, 'difference'))
    subtracting.computing.wait(timeout=5)
    client.release(sides)  # the read is still a reader of its state

    released = f'accumulator {sides.id} is not open'
    with pytest.raises(SubmitError, match=f'^{released}$'):
        sides.push(backgrounds=[loads[1].ref('value')])
    with pytest.raises(SubmitError, match=f'^{released}$'):
        client.freeze(sides)
    for reading in (
        lambda: client.output(sides, 'difference'),
        lambda: client.provenance(sides),
    ):
        with pytest.raises(NotReadable, match=f'^{released}$'):
            reading()
    subtracting.compute.set()
    assert toys.listed(read.result(timeout=5)) == [1.0]  # read after the release


def test_a_driver_that_fails_to_give_outputs_makes_its_state_not_readable(
    files: FakeFiles,
) -> None:
    class Failing:
        def push(self, table: str, rows: Any) -> None:
            pass

        def outputs(self) -> Any:
            raise RuntimeError('out of memory')

    bind = [DriverBinding(toys.TOTAL, lambda params: Failing())]
    with local(proposal='p1', files=files, bind=bind) as client:
        total = toys.total_accumulator(client)
        total.push(parts=[toys.Amount(value=1.0)])
        with pytest.raises(NotReadable, match='RuntimeError: out of memory'):
            client.output(total)
