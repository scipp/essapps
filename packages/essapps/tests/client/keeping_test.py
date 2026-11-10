# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)


"""What the workspace keeps and every client reads, and closing."""

import copy
import threading

import pytest

from ess.apps import (
    Accumulator,
    Client,
    NotReadable,
    Record,
    Selector,
    Status,
    SubmitError,
    Template,
    Workspace,
    local,
)
from ess.apps.log import Finished, Log
from ess.apps.testing import FakeFiles, FakeStore, read_file
from ess.spec import function_binding

from . import toys


def test_a_released_value_is_not_kept_and_the_record_stays(client: Client) -> None:
    load = client.compute(toys.load_request(1))
    client.release(load)

    not_kept = f'record {load.id} is not kept'
    with pytest.raises(NotReadable, match=not_kept):
        client.output(load, 'value')
    with pytest.raises(SubmitError, match=rf'^source: {not_kept}$'):
        client.submit(toys.read_request(load))
    assert client.records() == [load]
    assert client.status(load) is Status.COMPLETED


def test_a_pending_request_reads_a_value_released_after_its_submission(
    client: Client, loading: threading.Event
) -> None:
    first = client.compute(toys.load_request(1))
    loading.clear()
    second = client.submit(toys.load_request(2))
    total = client.submit(
        toys.TOTAL, params={'parts': [first.ref('value'), second.ref('value')]}
    )
    client.release(first)
    loading.set()

    assert client.output(total, 'value') == toys.Amount(value=3.0)


def test_a_released_pending_record_is_refused_and_still_runs_for_its_readers(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    load = client.submit(toys.load_request(1))
    shifted = client.submit(
        toys.SHIFT,
        params={'shift': toys.Shift(offset=1.0), 'source': load.ref('value')},
    )
    client.release(load)

    not_kept = rf'^source: record {load.id} is not kept$'
    with pytest.raises(SubmitError, match=not_kept):  # however far it has run
        client.submit(toys.read_request(load))
    loading.set()
    assert client.output(shifted, 'value') == toys.Amount(value=2.0)
    assert client.status(load) is Status.COMPLETED


def test_cancelling_work_that_nothing_keeps_passes_along_a_chain_of_any_length(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    chain = [client.submit(toys.load_request(1))]
    for _ in range(1000):
        chain.append(client.submit(toys.read_request(chain[-1])))
    client.release(chain[:-1])
    assert set(client.status(chain)) == {Status.PENDING}  # the last reads them

    client.release(chain[-1])  # now nothing keeps any of them
    loading.set()
    assert set(client.status(chain)) == {Status.CANCELLED}


def test_a_released_pending_record_that_nothing_reads_is_cancelled(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    load = client.submit(toys.load_request(1))
    client.release(load)
    loading.set()

    assert client.status(load) is Status.CANCELLED
    assert client.failure(load) == 'nothing keeps its outputs'
    with pytest.raises(SubmitError, match=rf'^source: record {load.id} cancelled$'):
        client.submit(toys.read_request(load))


def test_closing_the_workspace_cancels_the_reads_of_its_accumulator(
    summed_workspace: Workspace, summed: Client, summing: toys.Sum, log: Log
) -> None:
    client = summed
    load = client.compute(toys.load_request(1))
    summing.go.clear()
    total = toys.total_accumulator(client)
    total.push(parts=[load.ref('value')])
    shifted = client.submit(toys.read_request(total))
    state = toys.state_record(client, shifted)
    threading.Timer(0.05, summing.go.set).start()
    summed_workspace.close()

    finished = {e.record: e.status for e in log if isinstance(e, Finished)}
    assert [finished[state.id], finished[shifted.id]] == [Status.CANCELLED] * 2


def test_closing_the_workspace_writes_a_pending_persisted_freeze(
    summed_workspace: Workspace,
    summed: Client,
    summing: toys.Sum,
    log: Log,
    store: FakeStore,
) -> None:
    client = summed
    load = client.compute(toys.load_request(1))
    summing.go.clear()
    total = toys.total_accumulator(client)
    total.push(parts=[load.ref('value')])
    frozen = client.freeze(total, persist=True)
    threading.Timer(0.05, summing.go.set).start()
    summed_workspace.close()

    finished = {e.record: e.status for e in log if isinstance(e, Finished)}
    assert finished[frozen.id] is Status.COMPLETED
    assert store.read(frozen.id, 'value') == toys.Amount(value=1.0)


def test_another_client_reads_and_references_the_records_the_workspace_keeps(
    client: Client, other: Client, loading: threading.Event
) -> None:
    done = client.compute(toys.load_request(1))
    loading.clear()
    pending = client.submit(toys.load_request(2))

    reads = [other.submit(toys.read_request(r)) for r in (done, pending)]
    loading.set()
    assert other.output(done, 'value') == toys.Amount(value=1.0)
    assert other.output(pending, 'value') == toys.Amount(value=2.0)
    assert other.wait(reads) == [Status.COMPLETED] * 2


def test_another_client_goes_through_the_stages_and_reads_the_accumulators(
    client: Client, other: Client
) -> None:
    load = client.compute(toys.load_request(1))
    stage = toys.stage_shift(client, load)
    total = client.accumulator(Template(toys.TOTAL, blanks=('parts',)))
    total.push(parts=[load.ref('value')])

    shifted = toys.submit_shift(other, stage, 2.0)
    read = other.submit(toys.read_request(total))
    assert other.output(total) == {'value': toys.Amount(value=1.0)}
    assert other.wait([shifted, read]) == [Status.COMPLETED] * 2


def test_a_read_tells_what_is_unknown_from_what_cannot_be_read(
    client: Client,
) -> None:
    load = client.compute(toys.load_request(1))
    total = client.accumulator(Template(toys.TOTAL, blanks=('parts',)))
    both: list[Record | Accumulator] = [load, total]
    client.release(both)
    unknown_load = load.model_copy(update={'id': 'unknown'})
    unknown_total = copy.copy(total)
    unknown_total.id = 'unknown'

    for released in (
        lambda: client.output(load, 'value'),
        lambda: client.output(total),
    ):
        with pytest.raises(NotReadable, match=r'is not (kept|open)$'):
            released()
    with pytest.raises(SubmitError, match='is not open'):
        total.push(parts=[toys.Amount(value=1.0)])  # a push is refused, not read
    for unknown in (
        lambda: client.output(unknown_load, 'value'),
        lambda: client.output(unknown_total),
    ):
        with pytest.raises(KeyError, match=r"'no .* unknown in proposal p1'"):
            unknown()
    with pytest.raises(KeyError, match="load/v1 has no output 'nope'"):
        client.output(load, 'nope')


def test_another_client_releases_what_the_workspace_keeps(
    client: Client, other: Client
) -> None:
    load = client.compute(toys.load_request(1))
    stage = toys.stage_shift(client, load)
    total = toys.total_accumulator(client)
    other.release([load, stage, total])

    with pytest.raises(NotReadable, match=f'record {load.id} is not kept'):
        client.output(load, 'value')
    with pytest.raises(SubmitError, match='the stage was released'):
        toys.submit_shift(client, stage, 1.0)
    with pytest.raises(NotReadable, match=f'accumulator {total.id} is not open'):
        client.output(total)


def test_another_client_s_name_is_on_what_it_submits_and_freezes(
    client: Client, other: Client
) -> None:
    load = other.compute(toys.load_request(1))
    total = toys.total_accumulator(client)
    total.push(parts=[load.ref('value')])
    frozen = other.freeze(total)

    assert [r.submitter for r in client.records(ids=[load.id, frozen.id])] == [
        'bob',
        'bob',
    ]
    assert client.output(frozen, 'value') == toys.Amount(value=1.0)


def test_every_call_of_a_closed_workspace_raises(
    workspace: Workspace, client: Client
) -> None:
    load = client.compute(toys.load_request(1))
    stage = toys.stage_shift(client, load)
    total = client.accumulator(Template(toys.TOTAL, blanks=('parts',)))
    total.push(parts=[load.ref('value')])
    workspace.close()

    calls = [
        lambda: client.submit(toys.load_request(1)),
        lambda: client.freeze(total),
        lambda: client.cancel(load),
        lambda: client.changes(),
        lambda: client.wait(load),
        lambda: client.describe(load, 'value'),
        lambda: client.files.metadata(load.request.params['sample']),
        lambda: toys.submit_shift(client, stage, 1.0),
        lambda: client.submit(toys.read_request(total)),
        lambda: total.push(parts=[load.ref('value')]),
        lambda: client.status(load),
        lambda: client.output(load, 'value'),
        lambda: client.output(total),
        lambda: client.provenance(total),
        lambda: client.records(),
        lambda: client.readable(load),
        lambda: client.spec(load.spec),
        lambda: client.specs(),
        lambda: client.stage(Template(toys.SCALE, blanks=('speed',))),
        lambda: client.accumulator(Template(toys.TOTAL, blanks=('parts',))),
        lambda: client.release(load),
        lambda: client.files.list(Selector()),
    ]
    for call in calls:
        with pytest.raises(RuntimeError, match=r'^the workspace is closed$'):
            call()


def test_closing_a_client_releases_nothing(
    workspace: Workspace, other: Client, loading: threading.Event
) -> None:
    loading.clear()
    with Client(workspace, submitter='anna') as client:
        pending = client.submit(toys.load_request(2))
        shifted = client.submit(toys.read_request(pending))
    loading.set()

    assert other.wait([pending, shifted]) == [Status.COMPLETED] * 2
    assert other.output(pending, 'value') == toys.Amount(value=2.0)


def test_closing_a_local_client_closes_its_workspace_once_its_workflows_return(
    files: FakeFiles, loading: threading.Event
) -> None:
    started = threading.Event()
    loaded = []

    def load(params: toys.Sample) -> toys.LoadOutputs:
        started.set()
        loading.wait(timeout=5)
        loaded.append(read_file(params.sample))
        return toys.load_run(params)

    loading.clear()
    with local(
        proposal='p1', files=files, bind=[function_binding(toys.LOAD, load)]
    ) as client:
        client.submit(toys.load_request(1))
        started.wait(timeout=5)
        threading.Timer(0.05, loading.set).start()

    assert loaded == [1.0]  # cancelled when the client closed, but it ran


def test_every_output_of_a_record_or_an_accumulator_is_read_without_a_name(
    client: Client,
) -> None:
    load = client.compute(toys.load_request(1))
    total = toys.total_accumulator(client)
    total.push(parts=[load.ref('value')])

    one = toys.Amount(value=1.0)
    assert client.output(load) == {'value': one, 'extra': toys.Amount(value=-1.0)}
    assert client.output(total) == {'value': one}
    assert load.refs() == {name: load.ref(name) for name in ('value', 'extra')}
    assert total.refs() == {'value': total.ref('value')}


def test_on_close_is_called_by_each_close(workspace: Workspace) -> None:
    closed: list[bool] = []
    with Client(workspace, submitter='anna', on_close=lambda: closed.append(True)) as c:
        c.close()

    assert closed == [True, True]


def test_closing_a_local_client_cancels_what_has_not_started_before_it_waits(
    files: FakeFiles, loading: threading.Event
) -> None:
    started = threading.Event()
    shifted = []

    def load(params: toys.Sample) -> toys.LoadOutputs:
        started.set()
        loading.wait(timeout=5)
        return toys.load_run(params)

    def shift(params: toys.ShiftParams) -> toys.ShiftOutputs:
        shifted.append(params)
        return toys.shift(params)

    loading.clear()
    bind = [function_binding(toys.LOAD, load), function_binding(toys.SHIFT, shift)]
    with local(proposal='p1', files=files, bind=bind) as client:
        first = client.submit(toys.load_request(1))
        client.submit(toys.SHIFT, params={'source': first.ref('value')})
        started.wait(timeout=5)
        threading.Timer(0.05, loading.set).start()

    assert shifted == []
