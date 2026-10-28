# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Scheduling and checking in the backend, through the client."""

import threading
from collections.abc import Iterator
from enum import Enum
from itertools import islice
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field

from ess.apps import (
    Backend,
    Client,
    FieldError,
    RecordFailed,
    Request,
    Selector,
    Status,
    SubmitError,
    Template,
    dataset,
)
from ess.apps.log import Event, Log
from ess.apps.testing import FakeDatasets, StagedFunction, kept_rows, read_dataset
from ess.spec import DatasetModel, WorkflowSpec, function_binding


class SampleRun(DatasetModel): ...


class Amount(BaseModel):
    """A value one workflow returns and another takes by reference."""

    value: float


class LoadParams(BaseModel):
    sample: SampleRun


class ValueOutputs(BaseModel):
    value: Amount


class Shift(BaseModel):
    offset: float = 0.0


class AddParams(BaseModel):
    shift: Shift = Field(default_factory=Shift)
    a: Amount
    b: Amount


def _spec(
    name: str,
    *,
    params: type[BaseModel] = LoadParams,
    outputs: type[BaseModel] = ValueOutputs,
    tables: tuple[str, ...] = (),
) -> WorkflowSpec[Any, Any]:
    return WorkflowSpec(
        name=name,
        version=1,
        title=name,
        description=name,
        params=params,
        outputs=outputs,
        tables=tables,
    )


LOAD = _spec('load')
ADD = _spec('add', params=AddParams)
NOTHING = _spec('nothing')  # its workflow returns None as a required output
UNTYPED = _spec('untyped')  # its workflow returns a dict
COUNTS = _spec('counts')  # its workflow looks up a key that is not there


class MaskFile(DatasetModel, kind='mask'): ...


class LokiMask(DatasetModel, kind='mask', instrument='loki'): ...


class MaskParams(BaseModel):
    mask: MaskFile


class LokiMaskParams(BaseModel):
    mask: LokiMask


class RunsParams(BaseModel):
    runs: list[SampleRun] = Field(min_length=1)


class Term(BaseModel):
    value: float
    factor: float = 1.0


class SumParams(BaseModel):
    terms: list[Term] = Field(min_length=1)


class PartsOutputs(BaseModel):
    parts: dict[str, Amount]


class PiecesOutputs(BaseModel):
    pieces: list[Amount]


class Box(BaseModel):
    value: float = 0.0


class BoxParams(BaseModel):
    box: Box = Field(default_factory=Box)
    sample: SampleRun


class Values(BaseModel):
    values: list[float] = []


class Mode(Enum):
    fast = 'fast'
    slow = 'slow'


class Strict(BaseModel, strict=True):
    mode: Mode = Mode.fast
    factor: float = 1.0


class ModeParams(BaseModel):
    values: Values = Field(default_factory=Values)
    strict: Strict = Field(default_factory=Strict)
    sample: SampleRun


def slow(params: ModeParams) -> ValueOutputs:
    strict = params.strict
    value = float(strict.mode is Mode.slow) * strict.factor
    return ValueOutputs(value=Amount(value=value))


SPLIT = _spec('split', outputs=PartsOutputs)
PIECES = _spec('pieces', outputs=PiecesOutputs)
BOXED = _spec('boxed', params=BoxParams)  # a box of a number
MASKED = _spec('masked', params=MaskParams)
LOKI_MASKED = _spec('loki-masked', params=LokiMaskParams)
RUNS = _spec('runs', params=RunsParams, tables=('runs',))
MODES = _spec('modes', params=ModeParams)
SUM = _spec('sum', params=SumParams, tables=('terms',))


def add(params: AddParams) -> ValueOutputs:
    value = params.a.value + params.b.value + params.shift.offset
    return ValueOutputs(value=Amount(value=value))


def total(params: SumParams) -> ValueOutputs:
    value = sum(term.factor * term.value for term in params.terms)
    return ValueOutputs(value=Amount(value=value))


def _read(dataset: DatasetModel) -> Amount:
    return Amount(value=read_dataset(dataset.path))


def runs(params: RunsParams) -> ValueOutputs:
    value = sum(read_dataset(run.path) for run in params.runs)
    return ValueOutputs(value=Amount(value=value))


def split(params: LoadParams) -> PartsOutputs:
    value = read_dataset(params.sample.path)
    return PartsOutputs(
        parts={'whole': Amount(value=value), 'half': Amount(value=value / 2)}
    )


def pieces(params: LoadParams) -> PiecesOutputs:
    value = read_dataset(params.sample.path)
    return PiecesOutputs(pieces=[Amount(value=value), Amount(value=value / 2)])


class Gate:
    """Holds every LOAD until opened, so that its dependents stay pending."""

    def __init__(self) -> None:
        self.open = threading.Event()

    def load(self, params: LoadParams) -> ValueOutputs:
        run = read_dataset(params.sample.path)
        self.open.wait(timeout=5)
        if run < 0:
            raise ValueError('negative run')
        return ValueOutputs(value=Amount(value=run))


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
        function_binding(NOTHING, lambda p: ValueOutputs.model_construct(value=None)),
        function_binding(UNTYPED, lambda p: {'value': 1.0}),
        function_binding(COUNTS, lambda p: {}['counts']),
        function_binding(SUM, total),
        function_binding(SPLIT, split),
        function_binding(PIECES, pieces),
        function_binding(BOXED, lambda p: ValueOutputs(value=Amount(value=0.0))),
        StagedFunction(MODES, slow),
        function_binding(MASKED, lambda p: ValueOutputs(value=_read(p.mask))),
        function_binding(LOKI_MASKED, lambda p: ValueOutputs(value=_read(p.mask))),
        kept_rows(RUNS, runs),
    ]
    backend = Backend(datasets, bindings)
    yield backend
    gate.open.set()
    backend.close()


@pytest.fixture
def client(backend: Backend) -> Client:
    return Client(backend, proposal='p1', submitter='anna')


def load(n: int) -> Request:
    return Request(LOAD, params={'sample': dataset(run=n)})


def operands(a: Any, b: Any) -> dict[str, Any]:
    return {'a': a, 'b': b}


def test_a_request_waits_for_its_pending_references(client: Client, gate: Gate) -> None:
    a, b = client.submit([load(1), load(2)])
    total = client.submit(ADD, params=operands(a.ref('value'), b.ref('value')))

    assert client.status(total) is Status.PENDING
    gate.open.set()
    assert client.output(total, 'value') == Amount(value=3.0)


def test_closing_waits_until_no_record_is_pending(
    backend: Backend, client: Client, gate: Gate
) -> None:
    a = client.submit(load(1))
    total = client.submit(ADD, params=operands(a.ref('value'), a.ref('value')))
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
    first = client.submit(ADD, params=operands(bad.ref('value'), good.ref('value')))
    second = client.submit(ADD, params=operands(first.ref('value'), good.ref('value')))
    gate.open.set()

    assert client.wait([bad, good, first, second]) == [
        Status.FAILED,
        Status.COMPLETED,
        Status.FAILED,
        Status.FAILED,
    ]
    assert client.failure(bad) == 'ValueError: negative run'
    assert client.failure(first) == f'record {bad.id} failed'
    assert client.failure(second) == f'record {first.id} failed'


def test_cancel_ends_what_has_not_started(client: Client, gate: Gate) -> None:
    a, b = client.submit([load(1), load(2)])
    total = client.submit(ADD, params=operands(a.ref('value'), b.ref('value')))
    client.cancel(total)
    gate.open.set()

    assert client.wait([a, b, total]) == ['completed', 'completed', 'cancelled']


def test_one_refused_request_refuses_the_submission(client: Client) -> None:
    a = client.submit(load(1))
    typo = Request(
        ADD,
        params={'shift': {'offset': 'x'}, **operands(a.ref('value'), a.ref('value'))},
    )

    with pytest.raises(SubmitError, match='offset'):
        client.submit([load(2), typo])
    assert client.records() == [a]


def _boxed(value: Any) -> Request:
    return Request(
        BOXED,
        params={'box': {'value': value}, 'sample': dataset(run=1)},
    )


def test_a_value_that_cannot_be_stored_is_refused(client: Client) -> None:
    with pytest.raises(SubmitError, match=r'^second: box: cannot be stored as JSON'):
        client.submit({'first': _boxed(1), 'second': _boxed(len)})
    assert client.records() == []


@pytest.mark.parametrize('value', [float('inf'), float('-inf'), float('nan')])
def test_a_value_whose_json_form_reads_back_as_another_is_refused(
    client: Client, value: float
) -> None:
    with pytest.raises(
        SubmitError,
        match=r'^box\.value: has no JSON form that reads back as the value$',
    ):
        client.submit(_boxed(value))


def test_a_number_that_is_not_finite_is_refused_in_a_list(client: Client) -> None:
    with pytest.raises(
        SubmitError, match=r'^values\.values\[1\]: has no JSON form that reads'
    ):
        client.submit(
            MODES,
            params={
                'values': {'values': [1.0, float('inf')]},
                'sample': dataset(run=1),
            },
        )


def test_a_strict_group_is_validated_from_its_json_form(client: Client) -> None:
    strict = {'strict': Strict(mode=Mode.slow)}
    plain = client.compute(MODES, params={**strict, 'sample': dataset(run=1)})
    stage = client.stage(Template(MODES, params=strict, blanks=('sample',)))
    staged = client.compute(stage, params={'sample': dataset(run=1)})

    assert plain.request.params['strict'] == {'mode': 'slow', 'factor': 1.0}
    assert (
        client.output(plain, 'value')
        == client.output(staged, 'value')
        == Amount(value=1.0)
    )


def test_a_field_a_group_lacks_is_refused(client: Client) -> None:
    typo = Request(BOXED, params={'box': {'unknown': 1}, 'sample': dataset(run=1)})

    with pytest.raises(
        SubmitError, match=r"box\.unknown: Extra inputs are not permitted"
    ):
        client.submit(typo)


def test_a_reference_to_an_unknown_record_is_refused(client: Client) -> None:
    missing = {'@ref': 'output', 'record': 'missing', 'output': 'value'}

    with pytest.raises(SubmitError, match='a: unknown record missing'):
        client.submit(ADD, params=operands(missing, missing))


def test_a_reference_of_an_unknown_kind_is_refused(client: Client) -> None:
    unknown = {'@ref': 'record', 'record': 'r1', 'output': 'value'}

    with pytest.raises(
        SubmitError, match=r"^a: .*'record' is not a kind of reference"
    ) as raised:
        client.submit(ADD, params=operands(unknown, unknown))
    assert [e.loc for e in raised.value.errors] == [('a',)]


def test_a_malformed_reference_is_refused(client: Client) -> None:
    for malformed, problem in [
        ({'@ref': 'output', 'record': 'r1'}, 'output: Field required'),
        ({'@ref': 'dataset', 'dataset': 'run:1', 'run': 1}, 'run: Extra inputs'),
    ]:
        with pytest.raises(
            SubmitError, match=rf'^b: .*: not a reference to .*{problem}'
        ):
            client.submit(ADD, params=operands(dataset(run=1), malformed))


def test_a_reference_to_a_missing_output_is_refused(client: Client, gate: Gate) -> None:
    gate.open.set()
    loaded = client.compute(load(1))
    missing = {'@ref': 'output', 'record': loaded.id, 'output': 'total'}

    with pytest.raises(SubmitError, match="b: load/v1 has no output 'total'"):
        client.submit(ADD, params=operands(loaded.ref('value'), missing))


def test_a_reference_to_another_proposal_is_refused(
    backend: Backend, gate: Gate
) -> None:
    gate.open.set()
    theirs = Client(backend, proposal='p2', submitter='eve')
    mine = Client(backend, proposal='p1', submitter='anna')
    loaded = mine.compute(load(1))

    with pytest.raises(SubmitError, match='belongs to proposal p1'):
        theirs.submit(ADD, params=operands(loaded.ref('value'), loaded.ref('value')))


def test_an_unknown_dataset_is_refused(client: Client) -> None:
    with pytest.raises(SubmitError, match='unknown dataset run:9'):
        client.submit(load(9))


def test_a_dataset_of_another_proposal_is_refused(
    client: Client, datasets: FakeDatasets
) -> None:
    datasets.measure(4, 4.0, proposal='p2')

    with pytest.raises(
        SubmitError, match=r'^sample: dataset run:4 belongs to proposal p2$'
    ):
        client.submit(load(4))


def test_a_dataset_fits_a_dataset_model_by_its_kind(
    client: Client, datasets: FakeDatasets, gate: Gate
) -> None:
    gate.open.set()
    datasets.measure(5, 5.0, kind='mask')

    with pytest.raises(
        SubmitError,
        match=r"^mask: dataset run:1 does not fit MaskFile, which takes datasets "
        r"with \{'kind': 'mask'\}$",
    ):
        client.submit(MASKED, params={'mask': dataset(run=1)})
    with pytest.raises(
        SubmitError,
        match=r"^sample: dataset run:5 does not fit SampleRun, which takes datasets "
        r"with \{'kind': 'raw'\}$",
    ):
        client.submit(load(5))
    masked = client.compute(MASKED, params={'mask': dataset(run=5)})
    assert client.output(masked, 'value') == Amount(value=5.0)
    assert client.output(client.compute(load(1)), 'value') == Amount(value=1.0)


def test_a_dataset_fits_a_dataset_model_by_each_key_of_its_metadata(
    client: Client, datasets: FakeDatasets
) -> None:
    datasets.measure(5, 5.0, kind='mask', instrument='dream')
    datasets.measure(6, 6.0, kind='mask', instrument='loki')

    with pytest.raises(
        SubmitError,
        match=r"^mask: dataset run:5 does not fit LokiMask, which takes datasets "
        r"with \{'instrument': 'loki', 'kind': 'mask'\}$",
    ):
        client.submit(LOKI_MASKED, params={'mask': dataset(run=5)})
    masked = client.compute(LOKI_MASKED, params={'mask': dataset(run=6)})
    assert client.output(masked, 'value') == Amount(value=6.0)


def test_a_table_takes_a_list_of_rows(client: Client) -> None:
    with pytest.raises(SubmitError, match=r'^terms: .* is not a list$'):
        client.submit(SUM, params={'terms': {'value': 1.0}})


def test_a_dataset_model_given_inline_is_refused(client: Client) -> None:
    refused = 'is not a dataset; SampleRun is given by reference to a dataset'

    with pytest.raises(SubmitError, match=rf"^sample: '/data/run-1.json' {refused}"):
        client.submit(LOAD, params={'sample': '/data/run-1.json'})
    with pytest.raises(SubmitError, match=rf'^sample: .* {refused}'):
        client.submit(LOAD, params={'sample': {'path': '/data/run-1.json'}})


def test_a_file_given_by_path_is_the_dataset_of_that_path(
    client: Client, datasets: FakeDatasets, gate: Gate
) -> None:
    gate.open.set()
    path = datasets.path(datasets.resolve(dataset(run=1)))
    by_path = client.compute(LOAD, params={'sample': dataset(path=path)})

    assert by_path.request == client.compute(load(1)).request
    assert client.output(by_path, 'value') == Amount(value=1.0)


def test_a_dataset_fits_a_dataset_model_only(client: Client) -> None:
    with pytest.raises(
        SubmitError,
        match=r'^a: dataset run:1 does not fit Amount, which is not a dataset model$',
    ):
        client.submit(ADD, params=operands(dataset(run=1), dataset(run=2)))


def test_a_dataset_model_takes_a_reference_to_a_dataset_only(client: Client) -> None:
    loaded = client.submit(load(1))
    refused = 'is not a dataset; SampleRun is given by reference to a dataset'
    by_ref = loaded.ref('value')

    with pytest.raises(SubmitError, match=rf'^sample: {loaded.id}.value {refused}$'):
        client.submit(LOAD, params={'sample': by_ref})
    with pytest.raises(SubmitError, match=rf'^runs\[0\]: .* {refused}$'):
        client.submit(RUNS, params={'runs': [by_ref]})
    with pytest.raises(SubmitError, match=rf'^sample: .* {refused}$'):
        client.stage(Template(MODES, params={'sample': by_ref}, blanks=('values',)))


def test_a_spec_whose_output_holds_a_dataset_model_is_refused() -> None:
    class RunOutputs(BaseModel):
        run: SampleRun

    with pytest.raises(ValueError, match='SampleRun is a dataset model; no output'):
        _spec('echo', outputs=RunOutputs)


def test_a_required_parameter_left_out_is_refused(client: Client) -> None:
    with pytest.raises(
        SubmitError, match=r'^sample: a required parameter, not given$'
    ) as raised:
        client.submit(LOAD)
    assert raised.value.errors == (
        FieldError(None, ('sample',), 'a required parameter, not given'),
    )


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

    with pytest.raises(RecordFailed, match='negative run') as raised:
        client.output(failed, 'value')
    assert raised.value.record == failed.id
    assert raised.value.status is Status.FAILED
    assert raised.value.reason == client.failure(failed)


def test_an_unknown_parameter_is_refused(client: Client) -> None:
    with pytest.raises(SubmitError, match='scale: not a parameter of'):
        client.submit(LOAD, params={'scale': {}, 'sample': dataset(run=1)})


def test_a_failure_names_the_type_of_the_workflow_s_error(client: Client) -> None:
    failed = client.compute(COUNTS, params={'sample': dataset(run=1)})

    assert client.failure(failed) == "KeyError: 'counts'"


def test_a_workflow_that_returns_none_as_a_required_output_fails(
    client: Client,
) -> None:
    nothing = client.compute(NOTHING, params={'sample': dataset(run=1)})

    assert (
        client.failure(nothing) == 'the workflow of nothing/v1 returned None as value'
    )


def test_a_workflow_that_returns_no_outputs_instance_fails(client: Client) -> None:
    untyped = client.compute(UNTYPED, params={'sample': dataset(run=1)})

    assert client.failure(untyped) == (
        'the workflow of untyped/v1 returned dict, not ValueOutputs'
    )


def test_a_long_chain_fails_as_a_whole(client: Client, gate: Gate) -> None:
    chain = [client.submit(load(3))]
    for _ in range(1500):
        a, b = chain[-1].ref('value'), chain[0].ref('value')
        chain.append(client.submit(ADD, params=operands(a, b)))
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
    total = client.compute(ADD, params=operands(a.ref('value'), a.ref('value')))

    assert total.request.params == {
        'shift': {'offset': 0.0},
        **operands(a.ref('value'), a.ref('value')),
    }
    assert total.request.upstream() == [a.ref('value'), a.ref('value')]


def test_a_reference_with_a_key_reads_an_element_of_an_output(client: Client) -> None:
    split = client.submit(SPLIT, params={'sample': dataset(run=2)})
    whole, half = (
        split.ref('parts').model_copy(update={'key': k}) for k in ('whole', 'half')
    )
    total = client.compute(ADD, params=operands(whole, half))

    assert client.output(total, 'value') == Amount(value=3.0)


def test_a_reference_to_an_element_of_an_output_without_elements_is_refused(
    client: Client,
) -> None:
    a = client.submit(load(1))
    with pytest.raises(SubmitError, match='element of an output without elements'):
        client.submit(ADD, params=operands(a.ref('value', key='0'), a.ref('value')))


def test_a_reference_with_a_key_into_a_list_is_by_integer(client: Client) -> None:
    split = client.submit(PIECES, params={'sample': dataset(run=2)})
    whole, half = (split.ref('pieces', key=k) for k in ('0', '1'))

    assert client.output(client.compute(ADD, params=operands(whole, half))) == {
        'value': Amount(value=3.0)
    }
    for key in ('half', '01', '+1', '-1', ' 1'):
        named = split.ref('pieces', key=key)
        with pytest.raises(SubmitError, match=r'^a: .*: the key of an element of'):
            client.submit(ADD, params=operands(named, half))


def test_cancel_drops_what_is_running(client: Client, gate: Gate) -> None:
    running = client.submit(load(1))
    client.cancel(running)
    gate.open.set()

    assert client.wait(running) is Status.CANCELLED
    with pytest.raises(
        RecordFailed, match=f'^record {running.id} cancelled$'
    ) as raised:
        client.output(running, 'value')
    assert (raised.value.status, raised.value.reason) == (Status.CANCELLED, None)


def test_a_reference_to_a_failed_record_is_refused(client: Client, gate: Gate) -> None:
    gate.open.set()
    failed = client.compute(load(3))

    with pytest.raises(SubmitError, match=f'a: record {failed.id} failed'):
        client.submit(ADD, params=operands(failed.ref('value'), failed.ref('value')))


def test_a_refusal_names_the_request_and_the_field(client: Client) -> None:
    with pytest.raises(SubmitError, match=r'^second: sample: unknown dataset run:9$'):
        client.submit({'first': load(1), 'second': load(9)})
    with pytest.raises(SubmitError) as raised:
        client.submit([load(1), load(9)])
    assert raised.value.errors == (FieldError(1, ('sample',), 'unknown dataset run:9'),)
    assert str(raised.value) == '[1]: sample: unknown dataset run:9'


def test_a_refusal_lists_each_problem_of_a_value(client: Client) -> None:
    typo = Request(
        BOXED,
        params={
            'box': {'valeu': 1, 'other': 2},
            'sample': dataset(run=1),
        },
    )

    with pytest.raises(SubmitError) as raised:
        client.submit({'typo': typo})
    assert [(e.entry, e.loc) for e in raised.value.errors] == [
        ('typo', ('box', 'valeu')),
        ('typo', ('box', 'other')),
    ]
    assert str(raised.value).count('typo: box.') == 2


# Tables in a plain request


def test_a_row_is_a_group(client: Client) -> None:
    summed = client.compute(
        SUM, params={'terms': [{'value': 1.0, 'factor': 10.0}, Term(value=2.0)]}
    )

    assert client.output(summed, 'value') == Amount(value=12.0)


def test_a_table_that_needs_a_row_is_refused_without_one(client: Client) -> None:
    with pytest.raises(SubmitError, match='terms'):
        client.submit(SUM, params={'terms': []})


def test_a_row_given_twice_is_refused(client: Client, datasets: FakeDatasets) -> None:
    path = datasets.path(datasets.resolve(dataset(run=1)))

    with pytest.raises(SubmitError, match=r'runs\[1\]: repeats runs\[0\]'):
        client.submit(RUNS, params={'runs': [dataset(run=1), dataset(path=path)]})


def test_rows_are_distinct_as_a_whole(client: Client) -> None:
    scaled = {'value': 1.0, 'factor': 2.0}
    summed = client.compute(SUM, params={'terms': [{'value': 1.0}, scaled]})

    assert client.output(summed, 'value') == Amount(value=3.0)
    with pytest.raises(SubmitError, match=r'terms\[1\]: repeats terms\[0\]'):
        client.submit(SUM, params={'terms': [scaled, dict(scaled)]})


def test_rows_of_dataset_models_are_distinct_by_identity_not_by_path(
    client: Client, datasets: FakeDatasets
) -> None:
    # Two datasets of one run, by PID, share the run's file.
    a = datasets.measure(7, 1.0, pid='x')
    b = datasets.measure(7, 2.0, pid='y')
    assert datasets.path(a) == datasets.path(b)

    summed = client.compute(RUNS, params={'runs': [a, b]})
    assert client.output(summed, 'value') == Amount(value=4.0)
    with pytest.raises(SubmitError, match=r'^runs\[1\]: repeats runs\[0\]'):
        client.submit(RUNS, params={'runs': [a, dataset(pid='x')]})

    acc = client.accumulator(Template(RUNS, blanks=('runs',)))
    acc.push(runs=[a])
    acc.push(runs=[b])
    assert client.output(acc, 'value') == Amount(value=4.0)
    with pytest.raises(SubmitError, match=r'^runs\[2\]: repeats runs\[0\]'):
        acc.push(runs=[dataset(pid='x')])
