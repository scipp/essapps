# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Scheduling and checking in the backend, through the client."""

import threading
from collections.abc import Iterator
from itertools import islice
from pathlib import Path
from typing import Any, NewType

import pytest
from pydantic import BaseModel, Field

from ess.dispatch import (
    Backend,
    Client,
    Request,
    Selector,
    Status,
    SubmitError,
    dataset,
)
from ess.dispatch.log import Event, Log
from ess.dispatch.testing import FakeDatasets, read_dataset
from ess.spec import NoParams, WorkflowSpec, function_binding

SampleFile = NewType('SampleFile', Path)
A = NewType('A', float)
B = NewType('B', float)


class LoadInputs(BaseModel):
    sample: SampleFile


class ValueOutputs(BaseModel):
    value: float


class Shift(BaseModel):
    offset: float = 0.0


class AddParams(BaseModel):
    shift: Shift = Field(default_factory=Shift)


class AddInputs(BaseModel):
    a: A
    b: B


def _spec(
    name: str,
    *,
    params: type[BaseModel] = NoParams,
    inputs: type[BaseModel] = LoadInputs,
    outputs: type[BaseModel] = ValueOutputs,
) -> WorkflowSpec[Any, Any, Any]:
    return WorkflowSpec(
        name=name,
        version=1,
        title=name,
        description=name,
        params=params,
        inputs=inputs,
        outputs=outputs,
    )


LOAD = _spec('load')
ADD = _spec('add', params=AddParams, inputs=AddInputs)
NOTHING = _spec('nothing')  # its workflow returns None as a required output
UNTYPED = _spec('untyped')  # its workflow returns a dict


class Scale(BaseModel):
    factor: float = 1.0


class ScaledParams(BaseModel):
    scale: Scale | None = None


class PartsOutputs(BaseModel):
    parts: dict[str, float]


SPLIT = _spec('split', outputs=PartsOutputs)
SCALED = _spec('scaled', params=ScaledParams)
SUM = SCALED.tabulate(name='sum', version=1, tables={'sample': ('scale',)})


def add(params: AddParams, inputs: AddInputs) -> ValueOutputs:
    return ValueOutputs(value=inputs.a + inputs.b + params.shift.offset)


def total(params: Any, inputs: Any) -> ValueOutputs:
    factors = [1.0 if s is None else s.factor for s in params.scale]
    loaded = [read_dataset(path) for path in inputs.sample]
    return ValueOutputs(value=sum(f * x for f, x in zip(factors, loaded, strict=True)))


def split(params: NoParams, inputs: LoadInputs) -> PartsOutputs:
    value = read_dataset(inputs.sample)
    return PartsOutputs(parts={'whole': value, 'half': value / 2})


class Gate:
    """Holds every LOAD until opened, so that its dependents stay pending."""

    def __init__(self) -> None:
        self.open = threading.Event()

    def load(self, params: NoParams, inputs: LoadInputs) -> ValueOutputs:
        run = read_dataset(inputs.sample)
        self.open.wait(timeout=5)
        if run < 0:
            raise ValueError('negative run')
        return ValueOutputs(value=run)


@pytest.fixture
def gate() -> Gate:
    return Gate()


@pytest.fixture
def datasets(tmp_path: Path) -> FakeDatasets:
    datasets = FakeDatasets(proposal='p1', directory=tmp_path)
    for n, value in ((1, 1.0), (2, 2.0), (3, -1.0)):
        datasets.measure(n, value)
    return datasets


@pytest.fixture
def backend(datasets: FakeDatasets, gate: Gate) -> Iterator[Backend]:
    bindings = [
        function_binding(LOAD, gate.load),
        function_binding(ADD, add),
        function_binding(
            NOTHING, lambda p, i: ValueOutputs.model_construct(value=None)
        ),
        function_binding(UNTYPED, lambda p, i: {'value': 1.0}),
        function_binding(SUM, total),
        function_binding(SPLIT, split),
    ]
    backend = Backend(datasets, bindings)
    yield backend
    gate.open.set()
    backend.close()


@pytest.fixture
def client(backend: Backend) -> Client:
    return Client(backend, proposal='p1', submitter='anna')


def load(n: int) -> Request:
    return Request(LOAD, inputs={'sample': dataset(run=n)})


def operands(a: Any, b: Any) -> dict[str, Any]:
    return {'a': a, 'b': b}


def test_a_request_waits_for_its_pending_inputs(client: Client, gate: Gate) -> None:
    a, b = client.submit([load(1), load(2)])
    total = client.submit(ADD, inputs=operands(a.ref('value'), b.ref('value')))

    assert client.status(total) is Status.PENDING
    gate.open.set()
    assert client.output(total, 'value') == 3.0


def test_closing_waits_until_no_record_is_pending(
    backend: Backend, client: Client, gate: Gate
) -> None:
    a = client.submit(load(1))
    total = client.submit(ADD, inputs=operands(a.ref('value'), a.ref('value')))
    threading.Timer(0.05, gate.open.set).start()
    backend.close()

    assert client.status([a, total]) == [Status.COMPLETED, Status.COMPLETED]


class FullDisk(Log):
    """A log in memory whose appends fail once the disk is full."""

    def __init__(self) -> None:
        super().__init__()
        self.full = False

    def append(self, event: Event) -> Event:
        if self.full:
            raise OSError('no space left on device')
        return super().append(event)


def test_a_failed_append_stops_the_backend(datasets: FakeDatasets, gate: Gate) -> None:
    log = FullDisk()
    backend = Backend(datasets, [function_binding(LOAD, gate.load)], log=log)
    client = Client(backend, proposal='p1', submitter='anna')
    record = client.submit(load(1))
    log.full = True
    threading.Timer(0.05, gate.open.set).start()  # once the wait below has started

    with pytest.raises(RuntimeError, match='no space left on device'):
        client.wait(record)  # its Finished event was not appended
    with pytest.raises(RuntimeError, match='no space left on device'):
        client.submit(load(2))
    with pytest.raises(RuntimeError, match='no space left on device'):
        backend.close()


def test_a_failed_input_fails_everything_downstream(client: Client, gate: Gate) -> None:
    bad, good = client.submit([load(3), load(1)])
    first = client.submit(ADD, inputs=operands(bad.ref('value'), good.ref('value')))
    second = client.submit(ADD, inputs=operands(first.ref('value'), good.ref('value')))
    gate.open.set()

    assert client.wait([bad, good, first, second]) == [
        Status.FAILED,
        Status.COMPLETED,
        Status.FAILED,
        Status.FAILED,
    ]
    assert client.failure(bad) == 'negative run'
    assert client.failure(first) == f'input {bad.id} failed'
    assert client.failure(second) == f'input {first.id} failed'


def test_cancel_ends_what_has_not_started(client: Client, gate: Gate) -> None:
    a, b = client.submit([load(1), load(2)])
    total = client.submit(ADD, inputs=operands(a.ref('value'), b.ref('value')))
    client.cancel(total)
    gate.open.set()

    assert client.wait([a, b, total]) == ['completed', 'completed', 'cancelled']


def test_one_refused_request_refuses_the_submission(client: Client) -> None:
    a = client.submit(load(1))
    typo = Request(
        ADD,
        params={'shift': {'offset': 'x'}},
        inputs=operands(a.ref('value'), a.ref('value')),
    )

    with pytest.raises(SubmitError, match='offset'):
        client.submit([load(2), typo])
    assert client.records() == [a]


def test_a_value_that_cannot_be_stored_is_refused(datasets: FakeDatasets) -> None:
    class Item(BaseModel):
        value: Any

    class AnyParams(BaseModel):
        item: Item

    spec = _spec('anything', params=AnyParams)
    binding = function_binding(spec, lambda p, i: ValueOutputs(value=0.0))
    backend = Backend(datasets, [binding])
    client = Client(backend, proposal='p1', submitter='anna')
    sample = {'sample': dataset(run=1)}
    try:
        with pytest.raises(SubmitError, match=r'^second: item: cannot be stored'):
            client.submit(
                {
                    'first': Request(
                        spec, params={'item': {'value': 1}}, inputs=sample
                    ),
                    'second': Request(
                        spec, params={'item': {'value': len}}, inputs=sample
                    ),
                }
            )
        assert client.records() == []
    finally:
        backend.close()


def test_a_reference_to_an_unknown_record_is_refused(client: Client) -> None:
    missing = {'record': 'missing', 'output': 'value'}

    with pytest.raises(SubmitError, match='a: unknown record missing'):
        client.submit(ADD, inputs=operands(missing, missing))


def test_a_reference_to_a_missing_output_is_refused(client: Client, gate: Gate) -> None:
    gate.open.set()
    loaded = client.compute(load(1))
    missing = {'record': loaded.id, 'output': 'total'}

    with pytest.raises(SubmitError, match="b: load/v1 has no output 'total'"):
        client.submit(ADD, inputs=operands(loaded.ref('value'), missing))


def test_a_reference_to_another_proposal_is_refused(
    backend: Backend, gate: Gate
) -> None:
    gate.open.set()
    theirs = Client(backend, proposal='p2', submitter='eve')
    mine = Client(backend, proposal='p1', submitter='anna')
    loaded = mine.compute(load(1))

    with pytest.raises(SubmitError, match='belongs to proposal p1'):
        theirs.submit(ADD, inputs=operands(loaded.ref('value'), loaded.ref('value')))


def test_an_unknown_dataset_is_refused(client: Client) -> None:
    with pytest.raises(SubmitError, match='unknown dataset run:9'):
        client.submit(load(9))


def test_a_file_given_as_a_string_is_refused(client: Client) -> None:
    with pytest.raises(SubmitError, match=r'sample: .* is not a reference'):
        client.submit(LOAD, inputs={'sample': '/data/run-1.json'})


def test_a_file_given_by_path_is_the_dataset_of_that_path(
    client: Client, datasets: FakeDatasets, gate: Gate
) -> None:
    gate.open.set()
    path = datasets.path(datasets.resolve(dataset(run=1)))
    by_path = client.compute(LOAD, inputs={'sample': dataset(path=path)})

    assert by_path.request == client.compute(load(1)).request
    assert client.output(by_path, 'value') == 1.0


def test_a_dataset_fits_a_file_field_only(client: Client) -> None:
    with pytest.raises(SubmitError, match=r'a: dataset .* does not fit A'):
        client.submit(ADD, inputs=operands(dataset(run=1), dataset(run=2)))


def test_an_output_that_does_not_fit_its_field_is_refused(client: Client) -> None:
    loaded = client.submit(load(1))

    with pytest.raises(SubmitError, match=r'sample: .*, of type float, does not fit'):
        client.submit(LOAD, inputs={'sample': loaded.ref('value')})


def test_an_input_given_as_a_parameter_is_refused(client: Client) -> None:
    with pytest.raises(SubmitError, match='sample: an input of load/v1, given in'):
        client.submit(LOAD, params={'sample': dataset(run=1)})


def test_a_required_input_left_out_is_refused(client: Client) -> None:
    with pytest.raises(SubmitError, match=r"\['sample'\]: required inputs"):
        client.submit(LOAD)


def test_a_client_sees_only_the_datasets_of_its_proposal(
    backend: Backend, datasets: FakeDatasets
) -> None:
    mine = Client(backend, proposal='p1', submitter='anna')
    theirs = Client(backend, proposal='p2', submitter='eve')
    other = datasets.measure(4, 4.0, proposal='p2')
    later = datasets.measure(5, 5.0)

    assert mine.datasets.list(Selector()) == [
        datasets.resolve(dataset(run=n)) for n in (1, 2, 3, 5)
    ]
    assert theirs.datasets.list(Selector()) == [other]
    assert list(islice(mine.datasets.watch(Selector()), 4))[-1] == later
    assert theirs.datasets.metadata(other)['run'] == 4
    with pytest.raises(KeyError, match='in proposal p1'):
        mine.datasets.metadata(other)


def test_the_keys_of_a_dict_become_members(client: Client, gate: Gate) -> None:
    gate.open.set()
    records = client.compute({'250K': load(1), '260K': load(2)}, label='scan')

    assert {r.member: r for r in client.records(label='scan')} == records
    assert client.latest('scan', member='260K') == records['260K']


def test_latest_without_a_member_is_the_newest_record_of_any_member(
    client: Client, gate: Gate
) -> None:
    gate.open.set()
    cold = client.submit(load(1), label='scan', member='250K')
    warm = client.submit(load(2), label='scan', member='260K')

    assert client.latest('scan') == warm
    assert client.latest('scan', member='250K') == cold
    with pytest.raises(KeyError, match="'270K'"):
        client.latest('scan', member='270K')


def test_reading_an_output_of_a_failed_record_raises(
    client: Client, gate: Gate
) -> None:
    gate.open.set()
    failed = client.compute(load(3))

    with pytest.raises(RuntimeError, match='negative run'):
        client.output(failed, 'value')


def test_an_unknown_parameter_is_refused(client: Client) -> None:
    with pytest.raises(SubmitError, match=r"\['scale'\]: not parameters or inputs"):
        client.submit(LOAD, params={'scale': {}}, inputs={'sample': dataset(run=1)})


def test_a_workflow_that_returns_none_as_a_required_output_fails(
    client: Client,
) -> None:
    nothing = client.compute(NOTHING, inputs={'sample': dataset(run=1)})

    assert (
        client.failure(nothing) == 'the workflow of nothing/v1 returned None as value'
    )


def test_a_workflow_that_returns_no_outputs_instance_fails(client: Client) -> None:
    untyped = client.compute(UNTYPED, inputs={'sample': dataset(run=1)})

    assert client.failure(untyped) == (
        'the workflow of untyped/v1 returned dict, not ValueOutputs'
    )


def test_a_long_chain_fails_as_a_whole(client: Client, gate: Gate) -> None:
    chain = [client.submit(load(3))]
    for _ in range(1500):
        a, b = chain[-1].ref('value'), chain[0].ref('value')
        chain.append(client.submit(ADD, inputs=operands(a, b)))
    gate.open.set()

    assert set(client.wait(chain)) == {Status.FAILED}


def test_the_same_request_twice_makes_two_records(client: Client, gate: Gate) -> None:
    gate.open.set()
    request = load(1)
    first, second = client.compute([request, request])

    assert first.id != second.id
    assert first.request == second.request
    assert client.records() == [first, second]


def test_a_record_holds_its_request_with_defaults_filled_in(
    client: Client, gate: Gate
) -> None:
    gate.open.set()
    a = client.compute(load(1))
    total = client.compute(ADD, inputs=operands(a.ref('value'), a.ref('value')))

    assert total.request.params == {'shift': {'offset': 0.0}}
    assert total.request.inputs == operands(a.ref('value'), a.ref('value'))
    assert total.request.upstream() == [a.ref('value'), a.ref('value')]


def test_a_reference_with_a_key_reads_an_element_of_an_output(client: Client) -> None:
    split = client.submit(SPLIT, inputs={'sample': dataset(run=2)})
    whole, half = (
        split.ref('parts').model_copy(update={'key': k}) for k in ('whole', 'half')
    )
    total = client.compute(ADD, inputs=operands(whole, half))

    assert client.output(total, 'value') == 3.0


def test_a_reference_to_an_element_of_an_output_without_elements_is_refused(
    client: Client,
) -> None:
    a = client.submit(load(1))
    element = {'record': a.id, 'output': 'value', 'key': '0'}

    with pytest.raises(SubmitError, match='element of an output without elements'):
        client.submit(ADD, inputs=operands(element, a.ref('value')))


def test_cancel_drops_what_is_running(client: Client, gate: Gate) -> None:
    running = client.submit(load(1))
    client.cancel(running)
    gate.open.set()

    assert client.wait(running) is Status.CANCELLED
    with pytest.raises(RuntimeError, match='cancelled'):
        client.output(running, 'value')


def test_a_reference_to_a_failed_record_is_refused(client: Client, gate: Gate) -> None:
    gate.open.set()
    failed = client.compute(load(3))

    with pytest.raises(SubmitError, match=f'a: record {failed.id} failed'):
        client.submit(ADD, inputs=operands(failed.ref('value'), failed.ref('value')))


def test_a_refusal_names_the_request_and_the_field(client: Client) -> None:
    with pytest.raises(SubmitError, match=r'^second: sample: unknown dataset run:9$'):
        client.submit({'first': load(1), 'second': load(9)})


# Tables in a plain request


def test_a_column_left_out_is_none_in_each_row(client: Client) -> None:
    runs = [dataset(run=1), dataset(run=2)]
    summed = client.compute(SUM, inputs={'sample': runs})

    assert summed.request.params == {'scale': [None, None]}
    assert client.output(summed, 'value') == 3.0


def test_a_table_takes_a_column_attached_to_its_index(client: Client) -> None:
    summed = client.compute(
        SUM,
        params={'scale': [Scale(factor=10.0), None]},
        inputs={'sample': [dataset(run=1), dataset(run=2)]},
    )

    assert client.output(summed, 'value') == 12.0


def test_the_columns_of_a_table_have_equal_length(client: Client) -> None:
    with pytest.raises(SubmitError, match='scale: 1 rows, but sample has 2'):
        client.submit(
            SUM,
            params={'scale': [Scale()]},
            inputs={'sample': [dataset(run=1), dataset(run=2)]},
        )


def test_a_table_with_a_required_index_has_a_row(client: Client) -> None:
    with pytest.raises(SubmitError, match='sample'):
        client.submit(SUM, inputs={'sample': []})


def test_an_index_value_given_twice_is_refused(
    client: Client, datasets: FakeDatasets
) -> None:
    path = datasets.path(datasets.resolve(dataset(run=1)))

    with pytest.raises(SubmitError, match=r'sample\[1\]: repeats sample\[0\]'):
        client.submit(SUM, inputs={'sample': [dataset(run=1), dataset(path=path)]})


def test_a_column_without_its_index_is_refused(client: Client) -> None:
    with pytest.raises(SubmitError, match="columns of the table of sample"):
        client.submit(SUM, params={'scale': [Scale()]})
