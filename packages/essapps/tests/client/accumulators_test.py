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
    files: FakeFiles,
) -> None:
    bind = [
        function_binding(toys.LOAD, toys.load_run),
        function_binding(toys.SHIFT, toys.shift),
        DriverBinding(toys.TOTAL, toys.Averaging),  # returns MeanAndSpread
    ]
    with local(proposal='p1', files=files, bind=bind) as client:
        total = toys.total_accumulator(client)
        load = client.compute(toys.load_request(1))
        total.push(parts=[load.ref('value')])
        read = client.compute(toys.read_request(total))
        state = toys.state_record(client, read)

        assert client.failure([state, read]) == [
            'the workflow of total/v1 returned MeanAndSpread, not Value',
            f'record {state.id} failed',
        ]


def test_an_accumulator_is_read_through_a_reference(client: Client) -> None:
    with pytest.raises(TypeError, match=r'accumulator\.ref\(output\)'):
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
    read = client.compute(toys.read_request(total))
    plain = client.compute(toys.SUM, params={'runs': runs, **fixed})

    assert (
        client.output(read, 'value')
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
    assert client.provenance(read).records() == [
        toys.state_record(client, read),
        offset,
    ]
    resolved = {files.resolve(run) for run in runs}
    assert set(client.provenance(read).files()) == resolved  # offset's run too
    provenance = client.provenance(total)  # of the state after the pushes so far
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
    read = client.compute(toys.read_request(acc))

    assert client.output(read, 'value') == toys.Amount(value=1.0)
    assert acc.template.params == {'labels': {'tags': ['a', 'b']}}
    (run,) = toys.state_record(client, read).request.params['runs']
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
    in_place: Client,
    loads: list[Record],
    copying: toys.Copying,
    subtracting: toys.Subtracting,
) -> None:
    client = in_place
    copying.go.clear()
    sides = toys.sides_accumulator(client)
    sides.push(samples=[loads[0].ref('value')])
    copied = toys.submit_copy(client, sides, 'difference')
    copying.started.wait(timeout=5)  # it has read 'difference', and holds the state
    both = client.output(sides)
    copying.go.set()
    client.wait(copied)
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

    with pytest.raises(SubmitError, match=rf'^source: {reason}$'):
        toys.submit_copy(client, both, 'difference')
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
        sums.append(client.output(client.submit(toys.read_request(total)), 'value'))

    assert sums == [toys.Amount(value=v) for v in (1.0, 3.0, 6.0, 10.0)]
    assert summing.pushed == 4


def test_a_reference_names_the_record_of_the_state_pinned_at_submission(
    client: Client, files: FakeFiles
) -> None:
    files.measure(3, 3.0)
    loads = client.compute([toys.load_request(n) for n in (1, 2, 3)])
    total = toys.total_accumulator(client)
    total.push(parts=[loads[0].ref('value')])
    total.push(parts=[loads[1].ref('value')])
    first = client.compute(toys.read_request(total))
    total.push(parts=[loads[2].ref('value')])
    second = client.compute(toys.read_request(total))

    states = [toys.state_record(client, r) for r in (first, second)]
    assert [client.output(r, 'value') for r in (first, second)] == [
        toys.Amount(value=v) for v in (3.0, 6.0)
    ]
    assert [r.request.params['source'] for r in (first, second)] == [
        state.ref('value') for state in states
    ]
    assert [state.request for state in states] == [
        Request(toys.TOTAL, params=_parts(*loads[:n])) for n in (2, 3)
    ]
    assert client.provenance(first).records() == [states[0], *loads[:2]]
    assert client.provenance(second).records() == [states[1], *loads]


def test_a_submission_makes_one_record_of_each_state_its_requests_read(
    client: Client,
) -> None:
    load = client.compute(toys.load_request(1))
    total = toys.total_accumulator(client)
    total.push(parts=[load.ref('value')])
    reads = client.compute(
        {
            'a': toys.read_request(total),
            'b': Request(
                toys.SHIFT,
                params={'shift': toys.Shift(offset=1.0), 'source': total.ref('value')},
            ),
        },
        label='shifted',
    )
    state = toys.state_record(client, reads['a'])

    assert client.records() == [load, state, *reads.values()]
    assert {r.request.params['source'] for r in reads.values()} == {state.ref('value')}
    assert (state.label, state.member, state.submitter) == (None, None, 'anna')
    assert client.records(label='shifted') == list(reads.values())


def test_only_a_request_reads_an_accumulator_and_no_client_its_state(
    client: Client, loading: threading.Event
) -> None:
    load = client.compute(toys.load_request(1))
    total, other = toys.total_accumulator(client), toys.total_accumulator(client)
    total.push(parts=[load.ref('value')])
    loading.clear()
    pending = client.submit(toys.load_request(2))
    parts = [total.ref('value'), pending.ref('value')]
    read = client.submit(
        toys.TOTAL, params={'parts': parts}
    )  # holds the state until run
    state = read.request.params['parts'][0]

    refusals = {
        total.ref('value'): 'only a request may reference an accumulator',
        state: f'record {state.record} is not kept',
    }
    for ref, refused in refusals.items():
        for accumulator in (total, other):
            with pytest.raises(SubmitError, match=rf'^parts\[0\]: {refused}'):
                accumulator.push(parts=[ref])
        with pytest.raises(SubmitError, match=rf'^source: {refused}'):
            client.stage(
                Template(toys.SHIFT, params={'source': ref}, blanks=('shift',))
            )
        with pytest.raises(SubmitError, match=rf'^offset: {refused}'):
            client.accumulator(
                Template(toys.SUM, params={'offset': ref}, blanks=('runs',))
            )
    with pytest.raises(SubmitError, match=rf'^source: {refusals[state]}'):
        client.submit(toys.SHIFT, params={'source': state})
    loading.set()
    assert client.output(read, 'value') == toys.Amount(value=3.0)


def test_no_client_reads_the_record_of_a_state(
    in_place: Client, loads: list[Record], copying: toys.Copying
) -> None:
    client = in_place
    copying.go.clear()
    total = toys.array_total_accumulator(client)
    total.push(parts=[loads[0].ref('value')])
    copied = toys.submit_copy(client, total)  # holds the state until it has run
    state = toys.state_record(client, copied)

    with pytest.raises(NotReadable, match=f'^record {state.id} is not kept$'):
        client.output(state, 'value')
    copying.go.set()
    assert toys.listed(client.output(copied, 'value')) == [1.0]


def test_references_in_one_submission_pin_one_state_while_rows_are_pushed(
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
    copy_twice = [Request(toys.COPY, params={'source': total.ref('value')})] * 2
    pairs = [client.compute(copy_twice) for _ in range(20)]
    pushing.join()

    for pair in pairs:
        assert len({r.request.params['source'] for r in pair}) == 1  # one state
        upto = len(toys.state_record(client, pair[0]).request.params['parts'])
        expected = float(sum(range(3, 3 + upto)))
        assert [toys.listed(client.output(r, 'value')) for r in pair] == [
            [expected]
        ] * 2


def test_a_request_reads_several_accumulators(
    in_place: Client, loads: list[Record]
) -> None:
    client = in_place
    first, second = (
        toys.array_total_accumulator(client),
        toys.array_total_accumulator(client),
    )
    first.push(parts=[loads[0].ref('value')])
    second.push(parts=[loads[1].ref('value')])

    both = client.compute(toys.ARRAY_TOTAL, params=_parts(first, second))
    assert toys.listed(client.output(both, 'value')) == [3.0]


def test_two_submissions_that_read_one_state_make_two_records(
    in_place: Client, loads: list[Record]
) -> None:
    client = in_place
    sides = toys.sides_accumulator(client)
    sides.push(samples=[loads[0].ref('value')])
    first = toys.submit_copy(client, sides, 'difference')
    client.wait(first)
    second = toys.submit_copy(client, sides, 'difference')
    client.wait(second)

    assert toys.state_record(client, first).id != toys.state_record(client, second).id
    assert (
        toys.state_record(client, first).request
        == toys.state_record(client, second).request
    )


def test_cancelling_the_record_of_a_state_fails_its_readers_and_frees_the_push(
    in_place: Client,
    loads: list[Record],
    copying: toys.Copying,
    subtracting: toys.Subtracting,
) -> None:
    client = in_place
    sides = toys.sides_accumulator(client)
    sides.push(samples=[loads[0].ref('value')])
    subtracting.go.clear()
    sides.push(backgrounds=[loads[1].ref('value')])  # adds once go is set
    copying.go.clear()
    copied = toys.submit_copy(
        client, sides, 'difference'
    )  # its state waits for that push
    sides.push(samples=[loads[1].ref('value')])  # waits for the readers
    state = toys.state_record(client, copied)
    assert state in client.records()
    client.cancel(state)
    subtracting.go.set()

    read = toys.start_in_thread(lambda: client.output(sides, 'difference'))
    assert toys.listed(read.result(timeout=2)) == [1.0]  # copying.go is still clear
    assert client.failure(copied) == f'record {state.id} cancelled'


def test_a_reference_to_an_accumulator_is_checked_as_one_to_a_record(
    client: Client,
) -> None:
    total = toys.total_accumulator(client)
    load = client.compute(toys.load_request(1))
    total.push(parts=[load.ref('value')])
    extra = OutputRef(record=total.id, output='extra')

    for read in (lambda: total.ref('extra'), lambda: client.output(total, 'extra')):
        with pytest.raises(KeyError, match="total/v1 has no output 'extra'"):
            read()
    with pytest.raises(SubmitError, match=r"^source: total/v1 has no output 'extra'"):
        client.submit(toys.SHIFT, params={'source': extra})
    with pytest.raises(SubmitError, match=r'^shift: .* does not fit Shift'):
        client.submit(
            toys.SHIFT,
            params={'shift': total.ref('value'), 'source': toys.Amount(value=1.0)},
        )


def test_a_reference_to_an_accumulator_in_json_form_reads_its_state(
    client: Client,
) -> None:
    total = toys.total_accumulator(client)
    total.push(parts=[toys.Amount(value=2.0)])
    source = {'@ref': 'output', 'record': total.id, 'output': 'value'}
    shifted = client.compute(toys.SHIFT, params={'source': source})

    assert client.output(shifted, 'value') == toys.Amount(value=2.0)
    (ref,) = shifted.request.upstream()
    assert ref.record != total.id


def test_a_reference_with_a_key_reads_an_element_of_an_accumulators_output(
    client: Client,
) -> None:
    split = client.accumulator(Template(toys.SPLIT_TOTAL, blanks=('amounts',)))
    split.push(amounts=[toys.Amount(value=3.0), toys.Amount(value=1.0)])
    parts = [split.ref('parts', key='half'), split.ref('pieces', key='0')]
    total = client.compute(toys.TOTAL, params={'parts': parts})

    assert client.output(total, 'value') == toys.Amount(value=6.0)
    (state,) = {ref.record for ref in total.request.upstream()}
    assert total.request.upstream() == [
        OutputRef(record=state, output='parts', key='half'),
        OutputRef(record=state, output='pieces', key='0'),
    ]
    with pytest.raises(SubmitError, match=r'^parts\[0\]: .*: the key of an element'):
        client.submit(toys.TOTAL, params={'parts': [split.ref('pieces', key='a')]})


def test_an_optional_output_left_out_is_not_read(client: Client) -> None:
    parts = _parts(*client.compute([toys.load_request(n) for n in (1, 2)]))
    record = client.compute(toys.SPREAD, params=parts)
    spread = client.accumulator(Template(toys.SPREAD, blanks=('parts',)))
    spread.push(**parts)
    reading = client.compute(toys.read_request(spread, 'spread'))

    assert (
        client.output(record)
        == client.output(spread)
        == {'mean': toys.Amount(value=1.5)}
    )
    for read in (
        lambda: client.output(record, 'spread'),
        lambda: client.output(spread, 'spread'),
    ):
        with pytest.raises(NotReadable, match='the workflow did not return it'):
            read()
    left_out = f'record {record.id} output spread: the workflow did not return it'
    with pytest.raises(SubmitError, match=rf'^source: {left_out}$'):
        client.submit(toys.read_request(record, 'spread'))
    state = toys.state_record(client, reading)
    assert client.failure(reading) == (
        f'record {state.id} output spread: the workflow did not return it'
    )


def test_concurrent_readers_of_one_state_compute_its_outputs_once(
    in_place: Client, loads: list[Record], subtracting: toys.Subtracting
) -> None:
    client = in_place
    sides = toys.sides_accumulator(client)
    sides.push(samples=[loads[0].ref('value')])
    subtracting.compute.clear()
    read = toys.start_in_thread(lambda: client.output(sides, 'difference'))
    subtracting.computing.wait(timeout=5)  # the read computes, and holds the state
    copies = client.submit(
        [Request(toys.COPY, params={'source': sides.ref('difference')})] * 2
    )
    subtracting.compute.set()
    assert toys.listed(read.result(timeout=5)) == [1.0]
    assert [toys.listed(client.output(c, 'value')) for c in copies] == [[1.0]] * 2
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


def test_a_submission_pins_states_whose_pushes_wait_and_returns_at_once(
    in_place: Client, loads: list[Record], copying: toys.Copying
) -> None:
    client = in_place
    copying.go.clear()
    first, second = (
        toys.array_total_accumulator(client),
        toys.array_total_accumulator(client),
    )
    for total in (first, second):
        total.push(parts=[loads[0].ref('value')])
        toys.submit_copy(client, total)  # holds back the next push
    for total in (first, second):
        total.push(parts=[loads[1].ref('value')])
    reads = client.submit(
        [Request(toys.COPY, params={'source': t.ref('value')}) for t in (first, second)]
    )

    assert [
        len(toys.state_record(client, r).request.params['parts']) for r in reads
    ] == [2, 2]
    assert client.status(reads) == [Status.PENDING] * 2
    copying.go.set()
    assert [toys.listed(client.output(r, 'value')) for r in reads] == [[3.0]] * 2


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
    copied = toys.submit_copy(client, sides, 'difference')

    assert toys.state_record(client, copied).request == client.provenance(sides).request
    assert len(client.provenance(sides).request.params['backgrounds']) == 1
    with pytest.raises(TimeoutError):
        read.result(timeout=0.05)  # waits for the state, as for a record
    subtracting.go.set()
    assert toys.listed(read.result(timeout=5)) == [-1.0]
    assert toys.listed(client.output(copied, 'value')) == [-1.0]


def test_a_record_that_a_push_waits_for_and_that_fails_stops_the_accumulator(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    pending = client.submit(toys.load_request(1))
    total = toys.total_accumulator(client)
    total.push(parts=[pending.ref('value')])
    read = client.submit(toys.read_request(total))
    client.cancel(pending)
    loading.set()

    stopped = f'the accumulator stopped: push 0: record {pending.id} cancelled'
    state = toys.state_record(client, read)
    assert client.wait(read) is Status.FAILED
    assert client.failure([state, read]) == [stopped, f'record {state.id} failed']
    with pytest.raises(SubmitError, match=f'^{stopped}$'):
        total.push(parts=[pending.ref('value')])


def test_a_push_that_fails_to_add_stops_the_accumulator(
    summed: Client, summed_loads: list[Record], summing: toys.Sum
) -> None:
    client = summed
    total = toys.total_accumulator(client)
    total.push(parts=[summed_loads[0].ref('value')])
    before = client.compute(toys.read_request(total))
    summing.failing = True
    summing.go.clear()
    total.push(parts=[summed_loads[1].ref('value')])  # logged; fails once it adds
    after = client.submit(toys.read_request(total))
    summing.go.set()

    stopped = 'the accumulator stopped: push 1 failed: ValueError: cannot add'
    state = toys.state_record(client, after)
    assert client.wait(after) is Status.FAILED
    assert client.failure([state, after]) == [stopped, f'record {state.id} failed']
    assert len(state.request.params['parts']) == 2  # the push is in the log
    with pytest.raises(SubmitError, match=f'^{stopped}$'):
        total.push(parts=[summed_loads[2].ref('value')])
    with pytest.raises(SubmitError, match=rf'^source: {stopped}$'):
        client.submit(toys.read_request(total))  # it may be half added
    for read in (lambda: client.output(total), lambda: client.provenance(total)):
        with pytest.raises(NotReadable, match=f'^{stopped}$'):
            read()
    assert client.output(before, 'value') == toys.Amount(value=1.0)


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
    read = client.submit(toys.read_request(total))  # pins three pushes
    summing.adding.wait(timeout=5)
    client.release(total)  # the read keeps the pushes
    client.release(read)  # and no longer once it is cancelled
    assert client.status(read) is Status.CANCELLED
    summing.go.set()
    summed_workspace.close()

    assert summing.pushed == 1  # the push being added when the read was cancelled


def test_a_released_accumulator_adds_the_pushes_up_to_the_last_state_read(
    summed: Client, summed_loads: list[Record], summing: toys.Sum
) -> None:
    client = summed
    summing.go.clear()
    total = toys.total_accumulator(client)
    total.push(parts=[summed_loads[0].ref('value')])
    total.push(parts=[summed_loads[1].ref('value')])
    read = client.submit(toys.read_request(total))
    total.push(parts=[summed_loads[2].ref('value')])
    client.release(total)
    summing.go.set()

    assert client.output(read, 'value') == toys.Amount(value=3.0)
    assert summing.pushed == 2  # the last push was dropped


def test_a_released_accumulator_still_adds_the_pushes_logged(
    in_place: Client, loads: list[Record], copying: toys.Copying
) -> None:
    client = in_place
    copying.go.clear()
    total = toys.array_total_accumulator(client)
    total.push(parts=[loads[0].ref('value')])
    first = toys.submit_copy(client, total)
    copying.started.wait(timeout=5)
    total.push(parts=[loads[1].ref('value')])  # waits for the first copy
    second = toys.submit_copy(client, total)
    client.release(total)  # the copies are still readers of their states
    copying.go.set()

    assert [toys.listed(client.output(r, 'value')) for r in (first, second)] == [
        [1.0],
        [3.0],
    ]


def test_a_released_accumulator_takes_no_pushes_or_references_and_is_still_read(
    in_place: Client, loads: list[Record], copying: toys.Copying
) -> None:
    client = in_place
    copying.go.clear()
    total = toys.array_total_accumulator(client)
    total.push(parts=[loads[0].ref('value')])
    copied = toys.submit_copy(client, total)
    client.release(total)  # the copy is still a reader of its state

    released = f'accumulator {total.id} is not open'
    with pytest.raises(SubmitError, match=f'^{released}$'):
        total.push(parts=[loads[1].ref('value')])
    with pytest.raises(SubmitError, match=rf'^source: {released}$'):
        toys.submit_copy(client, total)
    for read in (
        lambda: client.output(total, 'value'),
        lambda: client.provenance(total),
    ):
        with pytest.raises(NotReadable, match=f'^{released}$'):
            read()
    copying.go.set()
    assert toys.listed(client.output(copied, 'value')) == [
        1.0
    ]  # read after the release


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
