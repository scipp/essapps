# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""What a client keeps: its stages, its accumulators, and the values of its records."""

import copy
import gc
import operator
import re
import threading
import time
import weakref
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from concurrent.futures import Future
from pathlib import Path
from typing import Any, NewType, Self

import numpy as np
import pytest
import scipp as sc
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ess.dispatch import (
    Accumulator,
    Backend,
    Client,
    ClientEnded,
    Record,
    Request,
    Selector,
    Status,
    SubmitError,
    Template,
    dataset,
    local,
)
from ess.dispatch.backend import Entry
from ess.dispatch.testing import FakeDatasets, StagedFunction, kept_rows, read_dataset
from ess.spec import (
    AccumulatorRef,
    DriverBinding,
    WorkflowSpec,
    function_binding,
)

type Spec = WorkflowSpec[Any, Any]

# The toy specs over floats. A reference must fit the field it is given to,
# and two NewTypes of float do not fit each other. So a field that takes any
# float, as SHIFT's source does, is typed float, and SHIFT's output is a
# NewType; other fields are NewTypes, which the plain float outputs fit.

SampleFile = NewType('SampleFile', Path)
RunFile = NewType('RunFile', Path)
Shifted = NewType('Shifted', float)
Extra = NewType('Extra', float)
Part = NewType('Part', float)
Offset = NewType('Offset', float)
Low = NewType('Low', float)
High = NewType('High', float)
Spread = NewType('Spread', float)


class Shift(BaseModel):
    offset: float = 0.0


class Factor(BaseModel):
    factor: float = 1.0


class Sample(BaseModel):
    sample: SampleFile


class Value(BaseModel):
    value: float


class LoadOutputs(BaseModel):
    value: float
    extra: Extra


class ShiftParams(BaseModel):
    shift: Shift = Field(default_factory=Shift)
    source: float


class ShiftOutputs(BaseModel):
    value: Shifted


class ScaleParams(BaseModel):
    scaling: Factor = Field(default_factory=Factor)
    sample: SampleFile


class Parts(BaseModel):
    parts: list[Part] = Field(min_length=1)


def _spec(
    name: str,
    *,
    params: type[BaseModel],
    outputs: type[BaseModel] = Value,
    tables: Any = (),
) -> Spec:
    return WorkflowSpec(
        name=name,
        version=1,
        title=name,
        description=name,
        params=params,
        outputs=outputs,
        tables=tables,
    )


LOAD = _spec('load', params=Sample, outputs=LoadOutputs)
SHIFT = _spec('shift', params=ShiftParams, outputs=ShiftOutputs)
SCALE = _spec('scale', params=ScaleParams)
TOTAL = _spec('total', params=Parts, tables=('parts',))
DIGITS = _spec('digits', params=Parts, tables=('parts',))


class Labels(BaseModel):
    tags: Any = None


class Tag(BaseModel):
    tags: Any = None


class SumParams(BaseModel):
    scale: Factor = Field(default_factory=Factor)
    labels: Labels = Field(default_factory=Labels)
    tag: list[Tag | None]
    runs: list[RunFile] = Field(default_factory=list)
    offset: Offset | None = None


SUM = _spec('sum', params=SumParams, tables={'runs': ('tag',)})


class Weight(BaseModel):
    value: float


class WeightedParams(BaseModel):
    weight: list[Weight]
    values: list[Part] = Field(min_length=1)


WEIGHTED = _spec(
    'weighted',
    params=WeightedParams,
    tables={'values': ('weight',)},
)


class MeanAndSpread(BaseModel):
    mean: float
    spread: Spread | None = None


SPREAD = _spec('spread', params=Parts, outputs=MeanAndSpread, tables=('parts',))


class Reference(BaseModel):
    total: float


class StitchParams(BaseModel):
    reference: Reference
    low: list[Low] = Field(default_factory=list)
    high: list[High] = Field(default_factory=list)


class Curve(BaseModel):
    curve: list[float]


STITCH = _spec(
    'stitch',
    params=StitchParams,
    outputs=Curve,
    tables=('low', 'high'),
)


class Step(BaseModel):
    size: float


class PositiveScale(BaseModel):
    factor: float = 1.0

    @field_validator('factor')
    @classmethod
    def _positive(cls, factor: float) -> float:
        return abs(factor)


class StepsParams(BaseModel):
    """A params model with a table: its index is a list of groups."""

    scale: PositiveScale = Field(default_factory=PositiveScale)
    steps: list[Step] = Field(min_length=1)


class WalkParams(StepsParams):
    steps: list[Step] = Field(min_length=2)


WALK = _spec('walk', params=WalkParams, tables=('steps',))


class Unchecked(StepsParams):
    """Its model validator always raises, which the framework never calls."""

    @model_validator(mode='after')
    def _raises(self) -> Self:
        raise ValueError('a model validator was called')


UNCHECKED_PLAIN = _spec('unchecked-plain', params=Unchecked, tables=('steps',))
UNCHECKED_STAGED = _spec('unchecked-staged', params=Unchecked, tables=('steps',))
UNCHECKED_ROWS = _spec('unchecked-rows', params=Unchecked, tables=('steps',))


def load_run(params: Sample) -> LoadOutputs:
    run = read_dataset(params.sample)
    return LoadOutputs(value=run, extra=-run)


def shift(params: ShiftParams) -> ShiftOutputs:
    return ShiftOutputs(value=params.source + params.shift.offset)


def scale(params: ScaleParams) -> Value:
    return Value(value=read_dataset(params.sample) * params.scaling.factor)


def weighted(params: Any) -> Value:
    """WEIGHTED as a plain function: each value times its weight, summed."""
    pairs = zip(params.weight, params.values, strict=True)
    return Value(value=sum(w.value * x for w, x in pairs))


def stitch(params: StitchParams) -> Curve:
    """
    STITCH as a plain function, a joint computation over both tables: their
    values, low ones first, scaled together so that they sum to the reference
    total.
    """
    values = [*params.low, *params.high]
    total = params.reference.total
    return Curve(curve=[total * value / sum(values) for value in values])


def append_digit(number: float, digit: float) -> float:
    """An order-sensitive combination, slow enough for pushes to overlap."""
    time.sleep(0.001)
    return number * 10 + digit


class Fold:
    """
    A driver that combines the index values of one table into the spec's one
    output with ``operation``, as in ``operation(operation(a, b), c)``, in
    push order. The first value is copied, so that combining in place never
    changes the output it came from.
    """

    def __init__(self, spec: Spec, operation: Callable[[Any, Any], Any]) -> None:
        self._outputs = spec.outputs
        (self._name,) = spec.outputs.model_fields
        self._operation = operation
        self._total: Any = None

    def push(self, table: str, columns: Mapping[str, Sequence[Any]]) -> None:
        for value in columns[table]:
            if self._total is None:
                self._total = copy.deepcopy(value)
            else:
                self._total = self._operation(self._total, value)

    def outputs(self) -> BaseModel:
        return self._outputs(**{self._name: self._total})


def folding(spec: Spec, operation: Callable[[Any, Any], Any]) -> DriverBinding:
    """A binding of a spec over one table, whose output is its index values folded."""
    return DriverBinding(spec, lambda fixed: Fold(spec, operation))


class Averaging:
    """
    The driver of SPREAD, which holds a sum and a count rather than a mean,
    and leaves out the optional spread.
    """

    def __init__(self, fixed: Mapping[str, Any]) -> None:
        self._total = 0.0
        self._count = 0

    def push(self, table: str, columns: Mapping[str, Sequence[Any]]) -> None:
        self._total += sum(columns[table])
        self._count += len(columns[table])

    def outputs(self) -> MeanAndSpread:
        return MeanAndSpread(mean=self._total / self._count)


class _ScaledTotal:
    """``offset`` plus each run times ``scale``; ``tags`` lists the tags pushed."""

    def __init__(self, fixed: Mapping[str, Any]) -> None:
        self._factor = fixed['scale'].factor
        self.total = fixed['offset'] or 0.0
        self.tags: list[Any] = []

    def push(self, table: str, columns: Mapping[str, Sequence[Any]]) -> None:
        self.tags.extend(columns['tag'])
        for path in columns['runs']:
            self.total += self._factor * read_dataset(path)

    def outputs(self) -> Value:
        return Value(value=self.total)


class ScaledSum:
    """
    Makes the drivers of SUM: ``offset`` plus each run times ``scale``.
    ``opened`` lists the fixed values of each driver made, for an accumulator
    or a plain request, and ``held`` the drivers.
    """

    def __init__(self) -> None:
        self.opened: list[dict[str, Any]] = []
        self.held: list[_ScaledTotal] = []

    def __call__(self, fixed: Mapping[str, Any]) -> _ScaledTotal:
        self.opened.append(dict(fixed))
        self.held.append(_ScaledTotal(fixed))
        return self.held[-1]


class Walking:
    """The driver of the specs over steps: the sum of the step sizes, scaled."""

    def __init__(self, fixed: Mapping[str, Any]) -> None:
        self._factor = fixed['scale'].factor
        self._total = 0.0

    def push(self, table: str, columns: Mapping[str, Sequence[Any]]) -> None:
        self._total += sum(step.size for step in columns['steps'])

    def outputs(self) -> Value:
        return Value(value=self._factor * self._total)


def _eventually(condition: Callable[[], bool]) -> bool:
    """Whether ``condition`` holds within five seconds, as other threads go on."""
    deadline = time.monotonic() + 5
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


def _start(call: Callable[[], Any]) -> Future[Any]:
    """
    Run ``call`` on a thread of its own; the future returns or raises as it does.

    The thread is a daemon, so that a call that never returns fails its test at
    the future's timeout instead of holding up the run.
    """
    future: Future[Any] = Future()

    def run() -> None:
        try:
            future.set_result(call())
        except Exception as error:
            future.set_exception(error)

    threading.Thread(target=run, daemon=True).start()
    return future


def _load(n: int) -> Request:
    return Request(LOAD, params={'sample': dataset(run=n)})


def _read(accumulator: Accumulator | Record, output: str = 'value') -> Request:
    """A request that reads an output of an accumulator or a record: SHIFT by 0."""
    return Request(SHIFT, params={'source': accumulator.ref(output)})


def _state(client: Client, reader: Record) -> Record:
    """The record of the state of an accumulator that ``reader`` reads as its source."""
    ref = reader.request.params['source']
    (state,) = [r for r in client.records() if r.id == ref.record]
    return state


@pytest.fixture
def shifting() -> StagedFunction:
    return StagedFunction(SHIFT, shift)


@pytest.fixture
def scaling() -> StagedFunction:
    return StagedFunction(SCALE, scale)


@pytest.fixture
def loading() -> threading.Event:
    """Set to let LOAD finish."""
    event = threading.Event()
    event.set()
    return event


@pytest.fixture
def datasets(tmp_path: Path) -> FakeDatasets:
    datasets = FakeDatasets(proposal='p1', directory=tmp_path)
    for n in (1, 2):
        datasets.measure(n, float(n))
    return datasets


@pytest.fixture
def scaled_sum() -> ScaledSum:
    return ScaledSum()


@pytest.fixture
def backend(
    datasets: FakeDatasets,
    shifting: StagedFunction,
    scaling: StagedFunction,
    scaled_sum: ScaledSum,
    loading: threading.Event,
) -> Iterator[Backend]:
    def load(params: Sample) -> LoadOutputs:
        loading.wait(timeout=5)
        return load_run(params)

    backend = Backend(
        datasets,
        [
            function_binding(LOAD, load),
            shifting,
            scaling,
            folding(TOTAL, operator.add),
            folding(DIGITS, append_digit),
            DriverBinding(SUM, scaled_sum),
            kept_rows(WEIGHTED, weighted),
            DriverBinding(SPREAD, Averaging),
            kept_rows(STITCH, stitch),
            DriverBinding(WALK, Walking),
        ],
    )
    yield backend
    backend.close()


@pytest.fixture
def client(backend: Backend) -> Client:
    return Client(backend, proposal='p1', submitter='anna')


def _stage_shift(client: Client, record: Record) -> Any:
    """A stage of SHIFT of the record's value, with the shift left open."""
    return client.stage(
        Template(SHIFT, params={'source': record.ref('value')}, blanks=('shift',))
    )


def _shift(client: Client, stage: Any, offset: float) -> Record:
    return client.submit(stage, params={'shift': Shift(offset=offset)})


def test_calls_through_a_stage_are_staged_once(
    client: Client, shifting: StagedFunction
) -> None:
    load = client.submit(_load(1))
    stage = _stage_shift(client, load)
    shifted = [_shift(client, stage, x) for x in (0.5, 1.5)]
    plain = client.compute(
        SHIFT,
        params={'shift': Shift(offset=1.5), 'source': load.ref('value')},
    )

    assert [client.output(r, 'value') for r in shifted] == [1.5, 2.5]
    assert shifted[1].request == plain.request
    assert shifting.staged == [('shift',)]  # the plain request makes no stage


def test_concurrent_calls_through_a_stage_stage_it_once(
    client: Client, shifting: StagedFunction, loading: threading.Event
) -> None:
    loading.clear()
    stage = _stage_shift(client, client.submit(_load(1)))
    pending = [_shift(client, stage, x) for x in (1.0, 2.0, 3.0, 4.0)]
    loading.set()
    client.wait(pending)

    assert [client.output(r, 'value') for r in pending] == [2.0, 3.0, 4.0, 5.0]
    assert shifting.staged == [('shift',)]


def test_a_released_stage_takes_no_calls_and_runs_those_made_before(
    client: Client, shifting: StagedFunction, loading: threading.Event
) -> None:
    loading.clear()
    stage = _stage_shift(client, client.submit(_load(2)))
    pending = _shift(client, stage, 1.0)
    client.release(stage)
    loading.set()

    with pytest.raises(SubmitError, match='the stage was released'):
        _shift(client, stage, 2.0)
    assert client.wait(pending) is Status.COMPLETED
    assert client.output(pending, 'value') == 3.0
    assert shifting.staged == [('shift',)]


def test_a_stage_keeps_what_its_template_references_until_it_has_staged(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    load = client.submit(_load(2))
    stage = _stage_shift(client, load)
    client.release(load)
    loading.set()

    shifted = [_shift(client, stage, x) for x in (1.0, 2.0)]
    client.wait(shifted)
    assert [client.output(r, 'value') for r in shifted] == [3.0, 4.0]
    with pytest.raises(SubmitError, match='not kept by this client'):
        client.submit(_read(load))


def test_releasing_a_stage_lets_go_of_what_its_template_references(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    load = client.submit(_load(2))
    stage = _stage_shift(client, load)
    client.release([load, stage])
    loading.set()

    assert client.status(load) is Status.CANCELLED


def test_a_stage_keeps_the_dataset_its_name_resolved_to_when_it_was_made(
    client: Client, scaling: StagedFunction, datasets: FakeDatasets
) -> None:
    run = datasets.resolve(dataset(run=1))
    stage = client.stage(
        Template(SCALE, params={'sample': dataset(run=1)}, blanks=('scaling',))
    )
    twice = {'scaling': Factor(factor=2.0)}
    before = client.compute(stage, params=twice)
    datasets.correct(run, run=99)
    datasets.correct(datasets.measure(7, 5.0), run=1)
    after = client.compute(stage, params=twice)

    assert datasets.resolve(dataset(run=1)) != run
    assert stage.template.params == {'sample': run}
    assert after.request == before.request
    assert [client.output(r, 'value') for r in (before, after)] == [2.0, 2.0]
    assert scaling.staged == [('scaling',)]


def test_a_stage_whose_staging_fails_stops(
    client: Client, scaling: StagedFunction
) -> None:
    scaling.fail_next = True
    stage = client.stage(
        Template(SCALE, params={'sample': dataset(run=2)}, blanks=('scaling',))
    )
    twice = {'scaling': Factor(factor=2.0)}
    failed = client.compute(stage, params=twice)

    assert client.status(failed) is Status.FAILED
    assert client.failure(failed) == 'staging failed'
    with pytest.raises(SubmitError, match='the stage stopped: staging failed'):
        client.submit(stage, params=twice)
    assert scaling.staged == []


def test_the_calls_waiting_for_a_stage_whose_staging_fails_fail_with_it(
    client: Client, shifting: StagedFunction, loading: threading.Event
) -> None:
    loading.clear()
    stage = _stage_shift(client, client.submit(_load(1)))
    shifting.fail_next = True
    pending = [_shift(client, stage, x) for x in (1.0, 2.0, 3.0)]
    loading.set()
    client.wait(pending)

    assert sorted(client.failure(r) for r in pending) == [
        'staging failed',
        *['the stage stopped: staging failed: staging failed'] * 2,
    ]
    assert shifting.staged == []


def test_a_request_through_a_stage_must_have_the_stage_s_values(
    backend: Backend,
) -> None:
    client = backend.open_client('p1', 'anna')
    (load,) = backend.submit([Entry(_load(1))], client=client)
    stage, template = backend.open_stage(
        Template(SCALE, params={'sample': dataset(run=1)}, blanks=('scaling',)),
        client=client,
    )
    twice = {'scaling': {'factor': 2.0}}
    refused = {
        'the stage holds scale': _read(load),
        r"\['sample'\]: differ from the stage's values": Request(
            SCALE, params={**twice, 'sample': dataset(run=2)}
        ),
    }
    for reason, request in refused.items():
        with pytest.raises(SubmitError, match=reason):
            backend.submit([Entry(request, stage=stage)], client=client)

    (record,) = backend.submit(
        [Entry(template.fill(params=twice), stage=stage)], client=client
    )
    assert record.request.params['sample'] == template.params['sample']


def test_the_stages_and_accumulators_of_another_client_are_refused(
    backend: Backend, client: Client
) -> None:
    theirs = Client(backend, proposal='p1', submitter='bob')
    load = client.compute(_load(1))
    stage = _stage_shift(client, load)
    total = client.accumulator(Template(TOTAL, blanks=('parts',)))
    total.push(parts=[load.ref('value')])

    with pytest.raises(SubmitError, match='the stage was released or is unknown'):
        _shift(theirs, stage, 2.0)
    with pytest.raises(SubmitError, match='the accumulator was released or is'):
        theirs.submit(_read(total))


def test_a_stage_refuses_a_template_that_a_request_would_refuse(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    cancelled = client.submit(_load(1))
    client.cancel(cancelled)
    loading.set()
    released = client.compute(_load(1))
    client.release(released)
    refused = {
        'sample: unknown dataset run:9': Template(
            SCALE, params={'sample': dataset(run=9)}, blanks=('scaling',)
        ),
        r'source: dataset .* does not fit float': Template(
            SHIFT, params={'source': dataset(run=1)}, blanks=('shift',)
        ),
        f'source: record {cancelled.id} cancelled': Template(
            SHIFT, params={'source': cancelled.ref('value')}, blanks=('shift',)
        ),
        f'source: record {released.id} is not kept by this client': Template(
            SHIFT, params={'source': released.ref('value')}, blanks=('shift',)
        ),
    }
    for reason, template in refused.items():
        with pytest.raises(SubmitError, match=reason):
            client.stage(template)


def test_a_stage_needs_a_binding_that_makes_stages(client: Client) -> None:
    with pytest.raises(SubmitError, match=r'^total/v1: its binding makes no stages$'):
        client.stage(Template(TOTAL, blanks=('parts',)))
    with pytest.raises(SubmitError, match=r'^total/v1: its binding makes no stages$'):
        client.stage(Template(TOTAL, params={'speed': 1.0}, blanks=('parts',)))


def test_bind_is_a_list_of_bindings(datasets: FakeDatasets) -> None:
    for bind in ({LOAD: load_run}, [load_run]):
        with pytest.raises(TypeError, match='bind is a list of bindings'):
            Backend(datasets, bind)  # type: ignore[arg-type]


def test_values_go_with_a_spec_or_a_stage(client: Client) -> None:
    with pytest.raises(TypeError, match='params go with a spec or a stage'):
        client.submit(_load(1), params={'sample': dataset(run=2)})


def test_a_params_model_makes_the_request_of_its_dict() -> None:
    steps = [Step(size=1.0), Step(size=2.0)]
    by_model = Request(WALK, params=WalkParams(steps=steps))
    by_dict = Request(WALK, params={'steps': steps})
    scaled = Request(
        WALK, params=WalkParams(scale=PositiveScale(factor=2.0), steps=steps)
    )

    assert by_model == by_dict  # defaults left to the backend's spec
    assert scaled.params['scale'] == {'factor': 2.0}


def test_the_framework_never_calls_a_model_validator(datasets: FakeDatasets) -> None:
    def sizes(params: StepsParams) -> Value:
        return Value(value=params.scale.factor * sum(s.size for s in params.steps))

    bind = [
        function_binding(UNCHECKED_PLAIN, sizes),
        StagedFunction(UNCHECKED_STAGED, sizes),
        kept_rows(UNCHECKED_ROWS, sizes),
    ]
    with local(proposal='p1', datasets=datasets, bind=bind) as client:
        plain = client.compute(UNCHECKED_PLAIN, params={'steps': [Step(size=1.0)]})
        stage = client.stage(
            Template(
                UNCHECKED_STAGED, params={'steps': [Step(size=2.0)]}, blanks=('scale',)
            )
        )
        staged = client.compute(stage, params={'scale': {'factor': 2.0}})
        rows = client.accumulator(Template(UNCHECKED_ROWS, blanks=('steps',)))
        rows.push(steps=[Step(size=3.0)])

        assert client.output(plain, 'value') == 1.0
        assert client.output(staged, 'value') == 4.0
        assert client.output(rows, 'value') == 3.0


def test_a_template_and_its_calls_take_models(client: Client) -> None:
    load = client.submit(_load(1))
    half = Shift(offset=0.5)
    stage = client.stage(
        Template(SHIFT, params={'shift': half, 'source': load.ref('value')})
    )
    tuned = client.compute(_stage_shift(client, load), params={'shift': half})

    assert client.output(client.compute(stage), 'value') == 1.5
    assert client.output(tuned, 'value') == 1.5


def test_a_group_with_a_field_its_model_lacks_is_refused(client: Client) -> None:
    acc = client.accumulator(Template(SUM, blanks=('runs',)))

    with pytest.raises(SubmitError, match=r'scaling\.speed: Extra inputs are not'):
        client.submit(
            SCALE,
            params={'scaling': {'factor': 2.0, 'speed': 2.0}, 'sample': dataset(run=1)},
        )
    with pytest.raises(SubmitError, match=r'tag\.0\.speed: Extra inputs are not'):
        acc.push(runs=[dataset(run=1)], tag=[{'speed': 2.0}])


# What a client keeps


def test_a_released_value_is_not_kept_and_the_record_stays(client: Client) -> None:
    load = client.compute(_load(1))
    client.release(load)

    not_kept = f'record {load.id} is not kept by this client'
    with pytest.raises(LookupError, match=not_kept):
        client.output(load, 'value')
    with pytest.raises(SubmitError, match=rf'^source: {not_kept}$'):
        client.submit(_read(load))
    assert client.records() == [load]
    assert client.status(load) is Status.COMPLETED


def test_a_pending_request_reads_a_value_released_after_its_submission(
    client: Client, loading: threading.Event
) -> None:
    first = client.compute(_load(1))
    loading.clear()
    second = client.submit(_load(2))
    total = client.submit(
        TOTAL, params={'parts': [first.ref('value'), second.ref('value')]}
    )
    client.release(first)
    loading.set()

    assert client.output(total, 'value') == 3.0
    with pytest.raises(LookupError, match='not kept'):  # read, then dropped
        client.output(first, 'value')


def test_a_released_pending_record_that_nothing_reads_is_cancelled(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    load = client.submit(_load(1))
    client.release(load)
    loading.set()

    assert client.status(load) is Status.CANCELLED
    assert client.failure(load) == 'nothing keeps its outputs'
    with pytest.raises(SubmitError, match=rf'^source: record {load.id} cancelled$'):
        client.submit(_read(load))


def test_a_released_pending_record_is_refused_and_still_runs_for_its_readers(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    load = client.submit(_load(1))
    shifted = client.submit(
        SHIFT,
        params={'shift': Shift(offset=1.0), 'source': load.ref('value')},
    )
    client.release(load)

    not_kept = rf'^source: record {load.id} is not kept by this client$'
    with pytest.raises(SubmitError, match=not_kept):  # however far it has run
        client.submit(_read(load))
    loading.set()
    assert client.output(shifted, 'value') == 2.0
    assert client.status(load) is Status.COMPLETED


def test_cancelling_work_that_nothing_keeps_passes_along_a_chain(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    load = client.submit(_load(1))
    shifted = client.submit(_read(load))
    again = client.submit(_read(shifted))
    client.release([load, shifted])
    assert client.status([load, shifted]) == [Status.PENDING] * 2  # again reads them

    client.release(again)
    loading.set()
    assert client.status([load, shifted, again]) == [Status.CANCELLED] * 3


def test_cancelling_passes_along_a_chain_of_any_length(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    chain = [client.submit(_load(1))]
    for _ in range(1000):
        chain.append(client.submit(_read(chain[-1])))
    client.release(chain)  # the last one releases the whole chain
    loading.set()

    assert set(client.status(chain)) == {Status.CANCELLED}


def test_a_client_reads_and_references_only_what_it_keeps(
    backend: Backend, client: Client, loading: threading.Event
) -> None:
    other = Client(backend, proposal='p1', submitter='bob')
    done = client.compute(_load(1))
    loading.clear()
    pending = client.submit(_load(2))

    for record in (done, pending):
        not_kept = f'record {record.id} is not kept by this client'
        with pytest.raises(LookupError, match=not_kept):
            other.output(record, 'value')
        with pytest.raises(SubmitError, match=rf'^source: {not_kept}$'):
            other.submit(_read(record))
    loading.set()
    assert other.wait(pending) is Status.COMPLETED  # its status is the proposal's


def test_a_client_releases_only_what_it_keeps(backend: Backend, client: Client) -> None:
    load = client.compute(_load(1))
    Client(backend, proposal='p1', submitter='bob').release(load)

    assert client.output(load, 'value') == 1.0


def test_every_call_of_a_closed_client_raises_client_ended(client: Client) -> None:
    load = client.compute(_load(1))
    stage = _stage_shift(client, load)
    total = client.accumulator(Template(TOTAL, blanks=('parts',)))
    total.push(parts=[load.ref('value')])
    client.close()

    calls = [
        lambda: client.submit(_load(1)),
        lambda: _shift(client, stage, 1.0),
        lambda: client.submit(_read(total)),
        lambda: total.push(parts=[load.ref('value')]),
        lambda: client.status(load),
        lambda: client.output(load, 'value'),
        lambda: client.output(total),
        lambda: client.provenance(total),
        lambda: client.records(),
        lambda: client.stage(Template(SCALE, blanks=('speed',))),
        lambda: client.accumulator(Template(TOTAL, blanks=('parts',))),
        lambda: client.release(load),
        lambda: client.datasets.list(Selector()),
    ]
    for call in calls:
        with pytest.raises(ClientEnded):
            call()


def test_closing_a_client_cancels_the_work_that_nothing_else_keeps(
    backend: Backend, client: Client, loading: threading.Event
) -> None:
    other = Client(backend, proposal='p1', submitter='bob')
    loading.clear()
    pending = client.submit(_load(2))
    shifted = client.submit(_read(pending))
    client.close()
    loading.set()

    assert other.status([pending, shifted]) == [Status.CANCELLED] * 2


def test_a_with_block_closes_the_client(backend: Backend) -> None:
    with Client(backend, proposal='p1', submitter='anna') as client:
        load = client.compute(_load(1))
    client.close()  # closing again does nothing

    with pytest.raises(ClientEnded):
        client.output(load, 'value')


def test_closing_a_local_client_closes_its_backend_once_its_workflows_return(
    datasets: FakeDatasets, loading: threading.Event
) -> None:
    started = threading.Event()
    loaded = []

    def load(params: Sample) -> LoadOutputs:
        started.set()
        loading.wait(timeout=5)
        loaded.append(read_dataset(params.sample))
        return load_run(params)

    loading.clear()
    with local(
        proposal='p1', datasets=datasets, bind=[function_binding(LOAD, load)]
    ) as client:
        client.submit(_load(1))
        started.wait(timeout=5)
        threading.Timer(0.05, loading.set).start()

    assert loaded == [1.0]  # cancelled when the client closed, but it ran


# Accumulators


def _total(client: Client) -> Accumulator:
    """An accumulator of TOTAL, the sum of a table of values."""
    return client.accumulator(Template(TOTAL, blanks=('parts',)))


def _parts(*records: Record) -> dict[str, list[Any]]:
    """The params of TOTAL over the values of ``records``."""
    return {'parts': [r.ref('value') for r in records]}


def test_a_reference_names_the_record_of_the_state_pinned_at_submission(
    client: Client,
) -> None:
    loads = client.compute([_load(n) for n in (1, 2, 1)])
    total = _total(client)
    total.push(parts=[loads[0].ref('value')])
    total.push(parts=[loads[1].ref('value')])
    first = client.compute(_read(total))
    total.push(parts=[loads[2].ref('value')])
    second = client.compute(_read(total))

    states = [_state(client, r) for r in (first, second)]
    assert [client.output(r, 'value') for r in (first, second)] == [3.0, 4.0]
    assert [r.request.params['source'] for r in (first, second)] == [
        state.ref('value') for state in states
    ]
    assert [state.request for state in states] == [
        Request(TOTAL, params=_parts(*loads[:n])) for n in (2, 3)
    ]
    assert client.provenance(first).records() == [states[0], *loads[:2]]
    assert client.provenance(second).records() == [states[1], *loads]


def test_a_submission_makes_one_record_of_each_state_its_requests_read(
    client: Client,
) -> None:
    load = client.compute(_load(1))
    total = _total(client)
    total.push(parts=[load.ref('value')])
    reads = client.compute(
        {
            'a': _read(total),
            'b': Request(
                SHIFT,
                params={'shift': Shift(offset=1.0), 'source': total.ref('value')},
            ),
        },
        label='shifted',
    )
    state = _state(client, reads['a'])

    assert client.records() == [load, state, *reads.values()]
    assert {r.request.params['source'] for r in reads.values()} == {state.ref('value')}
    assert state.request == Request(TOTAL, params=_parts(load))
    assert (state.label, state.member, state.submitter) == (None, None, 'anna')
    assert client.records(label='shifted') == list(reads.values())
    assert [client.output(r, 'value') for r in reads.values()] == [1.0, 2.0]


def test_an_accumulator_waits_for_its_fixed_values_and_reads_them_once(
    client: Client,
    scaled_sum: ScaledSum,
    datasets: FakeDatasets,
    loading: threading.Event,
) -> None:
    loading.clear()
    offset = client.submit(_load(2))
    threading.Timer(0.05, loading.set).start()
    fixed = {'offset': offset.ref('value')}  # and the default scale and labels
    total = client.accumulator(Template(SUM, params=fixed, blanks=('runs',)))
    runs = [dataset(run=n) for n in (1, 2)]
    for run in runs:
        total.push(runs=[run])
    read = client.compute(_read(total))
    plain = client.compute(SUM, params={'runs': runs, **fixed})

    assert client.output(read, 'value') == client.output(plain, 'value') == 5.0
    opened = {'scale': Factor(), 'labels': Labels(), 'offset': 2.0}
    assert scaled_sum.opened == [opened] * 2  # the accumulator and the plain request
    assert total.template == Template(SUM, params=fixed, blanks=('runs',))
    state = _state(client, read)
    assert state.request == plain.request
    assert client.provenance(read).records() == [state, offset]
    resolved = {datasets.resolve(run) for run in runs}
    assert set(client.provenance(read).datasets()) == resolved  # offset's run too
    provenance = client.provenance(total)  # of the state after the pushes so far
    assert provenance == client.provenance(plain)
    assert provenance.records() == [offset]
    assert set(provenance.datasets()) == resolved


def test_an_accumulator_may_output_other_fields_than_it_takes(client: Client) -> None:
    loads = [client.submit(_load(n)) for n in (1, 2)]
    spread = client.accumulator(Template(SPREAD, blanks=('parts',)))
    for load in loads:
        spread.push(parts=[load.ref('value')])
    read = client.compute(_read(spread, 'mean'))
    plain = client.compute(SPREAD, params=_parts(*loads))

    assert client.output(read, 'value') == client.output(plain, 'mean') == 1.5


def test_a_field_validator_gives_the_driver_the_plain_request_s_values(
    client: Client,
) -> None:
    flipped = {'scale': {'factor': -2.0}}
    steps = [Step(size=1.0), Step(size=2.0)]
    walk = client.accumulator(Template(WALK, params=flipped, blanks=('steps',)))
    walk.push(steps=steps)
    plain = client.compute(WALK, params={**flipped, 'steps': steps})

    assert client.output(walk, 'value') == client.output(plain, 'value') == 6.0
    assert plain.request.params['scale'] == {'factor': 2.0}  # made positive


def test_field_validators_apply_at_the_opening_and_table_rules_at_reads(
    client: Client,
) -> None:
    flipped = {'scale': {'factor': -2.0}}
    walk = client.accumulator(Template(WALK, params=flipped, blanks=('steps',)))
    walk.push(steps=[Step(size=1.0)])
    with pytest.raises(SubmitError, match='steps needs at least 2') as refused:
        client.submit(WALK, params={**flipped, 'steps': [Step(size=1.0)]})
    reason = f'^{re.escape(str(refused.value))}$'
    with pytest.raises(LookupError, match=reason):
        client.output(walk)
    with pytest.raises(SubmitError, match=reason):
        client.freeze(walk)
    walk.push(steps=[Step(size=2.0)])

    assert client.output(walk, 'value') == 6.0


def test_a_table_that_needs_two_rows_takes_them_one_push_at_a_time(
    datasets: FakeDatasets,
) -> None:
    bind = [DriverBinding(WALK, Walking)]
    with local(proposal='p1', datasets=datasets, bind=bind) as client:
        walk = client.accumulator(Template(WALK, blanks=('steps',)))
        walk.push(steps=[Step(size=1.0)])
        with pytest.raises(LookupError, match='steps needs at least 2'):
            client.output(walk)
        walk.push(steps=[Step(size=2.0)])

        assert client.output(walk, 'value') == 3.0


def test_an_accumulator_s_values_reach_the_driver_as_the_log_holds_them(
    client: Client, scaled_sum: ScaledSum
) -> None:
    acc = client.accumulator(
        Template(SUM, params={'labels': Labels(tags=('a', 'b'))}, blanks=('runs',))
    )
    acc.push(runs=[dataset(run=1)], tag=[Tag(tags=('c', 'd'))])
    read = client.compute(_read(acc))

    assert client.output(read, 'value') == 1.0
    assert acc.template.params == {'labels': {'tags': ['a', 'b']}}  # JSON has no tuple
    assert _state(client, read).request.params['tag'] == [{'tags': ['c', 'd']}]
    assert scaled_sum.opened[0]['labels'] == Labels(tags=['a', 'b'])
    assert scaled_sum.held[0].tags == [Tag(tags=['c', 'd'])]


def test_an_optional_output_left_out_is_not_read(client: Client) -> None:
    parts = _parts(*client.compute([_load(n) for n in (1, 2)]))
    record = client.compute(SPREAD, params=parts)
    spread = client.accumulator(Template(SPREAD, blanks=('parts',)))
    spread.push(**parts)
    reading = client.compute(_read(spread, 'spread'))

    assert client.output(record) == client.output(spread) == {'mean': 1.5}
    for read in (
        lambda: client.output(record, 'spread'),
        lambda: client.output(spread, 'spread'),
    ):
        with pytest.raises(LookupError, match='the workflow did not return it'):
            read()
    left_out = f'record {record.id} output spread: the workflow did not return it'
    with pytest.raises(SubmitError, match=rf'^source: {left_out}$'):
        client.submit(_read(record, 'spread'))
    state = _state(client, reading)
    assert client.failure(reading) == (
        f'record {state.id} output spread: the workflow did not return it'
    )


def test_a_state_with_nothing_pushed_is_read_as_the_plain_request_over_no_rows(
    client: Client,
) -> None:
    summed = client.accumulator(Template(SUM, blanks=('runs',)))
    total = _total(client)
    plain = client.compute(SUM)
    with pytest.raises(SubmitError, match='parts: the table has no row') as refused:
        client.submit(TOTAL, params={'parts': []})

    assert client.output(summed, 'value') == client.output(plain, 'value') == 0.0
    assert client.provenance(summed).request == plain.request
    with pytest.raises(LookupError, match=f'^{re.escape(str(refused.value))}$'):
        client.output(total)


def test_every_output_of_a_record_or_an_accumulator_is_read_without_a_name(
    client: Client,
) -> None:
    load = client.compute(_load(1))
    total = _total(client)
    total.push(parts=[load.ref('value')])

    assert client.output(load) == {'value': 1.0, 'extra': -1.0}
    assert client.output(total) == {'value': 1.0}
    assert load.refs() == {name: load.ref(name) for name in ('value', 'extra')}
    assert total.refs() == {'value': total.ref('value')}


def test_an_accumulator_is_read_through_a_reference(client: Client) -> None:
    with pytest.raises(TypeError, match=r'accumulator\.ref\(output\)'):
        client.submit(_total(client))


def test_the_blanks_of_an_accumulator_are_its_tables_by_their_indexes(
    client: Client,
) -> None:
    refused = {
        r"by their indexes \['runs'\], not \[\]$": Template(SUM),
        r"not \['scale'\]$": Template(SUM, blanks=('scale',)),
        r"not \['runs', 'scale'\]$": Template(SUM, blanks=('runs', 'scale')),
        r"not \['tag'\]$": Template(SUM, blanks=('tag',)),
        r"by their indexes \['high', 'low'\], not \[\]$": Template(STITCH),
        r"not \['reference'\]$": Template(STITCH, blanks=('reference',)),
    }
    for reason, template in refused.items():
        with pytest.raises(SubmitError, match=reason):
            client.accumulator(template)


def test_an_accumulator_needs_a_binding_that_makes_accumulators(
    client: Client,
) -> None:
    refused = r'^scale/v1: its binding makes no accumulators$'
    with pytest.raises(SubmitError, match=refused):
        client.accumulator(Template(SCALE, blanks=('scaling',)))


def test_an_accumulator_of_a_plain_function_reads_as_the_plain_request(
    client: Client,
) -> None:
    loads = client.compute([_load(n) for n in (1, 2)])
    reference = {'reference': Reference(total=30.0)}
    stitched = client.accumulator(
        Template(STITCH, params=reference, blanks=('low', 'high'))
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
        plain = client.compute(STITCH, params={**reference, **tables})
        reads.append(client.output(stitched, 'curve'))
        plains.append(client.output(plain, 'curve'))
    client.release(loads)  # the accumulator keeps the values of its rows

    assert reads == plains
    assert reads == [[30.0], [10.0, 20.0], [6.0, 12.0, 12.0], [5.0, 10.0, 10.0, 5.0]]
    assert client.output(stitched, 'curve') == [5.0, 10.0, 10.0, 5.0]
    assert client.provenance(stitched).request == plain.request


def test_an_accumulator_refuses_a_template_that_a_request_would_refuse(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    cancelled = client.submit(_load(1))
    client.cancel(cancelled)
    loading.set()
    released = client.compute(_load(1))
    client.release(released)
    refused = {
        r"\['speed'\]: not parameters": {'params': {'speed': {}}},
        'scale.factor: Input should be a valid number': {
            'params': {'scale': {'factor': 'x'}}
        },
        f'offset: record {cancelled.id} cancelled': {
            'params': {'offset': cancelled.ref('value')}
        },
        f'offset: record {released.id} is not kept by this client': {
            'params': {'offset': released.ref('value')}
        },
    }
    for reason, values in refused.items():
        with pytest.raises(SubmitError, match=reason):
            client.accumulator(Template(SUM, blanks=('runs',), **values))


def test_a_template_that_fixes_a_repeated_index_value_is_refused(
    client: Client,
) -> None:
    load = client.compute(_load(1))
    fixed = Template(
        STITCH,
        params={'reference': Reference(total=1.0), 'high': [load.ref('value')] * 2},
        blanks=('low',),
    )

    with pytest.raises(
        SubmitError, match=r'^high\[1\]: repeats high\[0\], {"@ref":"output"'
    ):
        client.accumulator(fixed)


# Pushes


def test_a_push_takes_an_index_and_its_columns_as_a_request_does(
    client: Client,
) -> None:
    loads = [client.submit(_load(n)) for n in (1, 2)]
    weighted = client.accumulator(Template(WEIGHTED, blanks=('values',)))
    weighted.push(values=[loads[0].ref('value')], weight=[Weight(value=3.0)])
    weighted.push(values=[loads[1].ref('value')], weight=[{'value': 1.0}])
    read = client.compute(_read(weighted))
    plain = client.compute(
        WEIGHTED,
        params={
            'weight': [Weight(value=3.0), Weight(value=1.0)],
            'values': [x.ref('value') for x in loads],
        },
    )

    assert client.output(read, 'value') == client.output(plain, 'value') == 5.0
    state = _state(client, read)
    assert state.request == plain.request
    assert client.provenance(read).records() == [state, *loads]


def test_a_push_is_refused_as_the_request_over_it_alone(client: Client) -> None:
    load = client.compute(_load(1))
    value, weight = [load.ref('value')], [Weight(value=1.0)]
    pushes = [
        {'values': value},  # a column whose element type is not optional
        {'weight': weight * 2, 'values': value},  # columns of unequal length
        {'weight': weight, 'values': [1.0]},  # a value where a reference goes
        {'weight': [{'value': 'x'}], 'values': value},  # an invalid literal
        {'weight': weight, 'values': [dataset(run=1)]},  # a dataset, no file
        {'weight': weight, 'values': [{'@ref': 'run'}]},  # no kind of reference
    ]
    weighted = client.accumulator(Template(WEIGHTED, blanks=('values',)))
    for columns in pushes:
        with pytest.raises(SubmitError) as refused:
            client.submit(WEIGHTED, params=columns)
        with pytest.raises(SubmitError) as pushed:
            weighted.push(**columns)
        assert str(pushed.value) == str(refused.value)


def test_a_push_takes_lists_of_values(client: Client) -> None:
    load = client.compute(_load(1))
    total = _total(client)
    plain = {'@ref': 'output', 'record': load.id, 'output': 'value'}
    for column in (load.ref('value'), plain, 'x'):
        with pytest.raises(TypeError, match='parts: a column is a list of values'):
            total.push(parts=column)  # type: ignore[arg-type]


def test_a_reference_in_a_push_must_fit_its_column(client: Client) -> None:
    load = client.submit(_load(1))
    summed = client.accumulator(Template(SUM, blanks=('runs',)))

    with pytest.raises(SubmitError, match=r'^runs\[0\]: .*, of type float, does not'):
        summed.push(runs=[load.ref('value')])


def test_a_reference_to_an_accumulator_is_checked_as_one_to_a_record(
    backend: Backend, client: Client
) -> None:
    total = _total(client)
    load = client.compute(_load(1))
    total.push(parts=[load.ref('value')])
    elsewhere = Client(backend, proposal='p2', submitter='carl')
    extra = AccumulatorRef(accumulator=total.id, output='extra')

    for read in (lambda: total.ref('extra'), lambda: client.output(total, 'extra')):
        with pytest.raises(KeyError, match="total/v1 has no output 'extra'"):
            read()
    with pytest.raises(SubmitError, match=r"^source: total/v1 has no output 'extra'$"):
        client.submit(SHIFT, params={'source': extra})
    with pytest.raises(SubmitError, match=r'^runs\[0\]: .* does not fit'):
        client.submit(SUM, params={'runs': [total.ref('value')]})
    with pytest.raises(SubmitError, match=r'^source: the accumulator was released or'):
        elsewhere.submit(_read(total))


def test_only_a_request_reads_an_accumulator_and_no_client_its_state(
    client: Client, loading: threading.Event
) -> None:
    load = client.compute(_load(1))
    total, other = _total(client), _total(client)
    total.push(parts=[load.ref('value')])
    loading.clear()
    pending = client.submit(_load(2))
    parts = [total.ref('value'), pending.ref('value')]
    read = client.submit(TOTAL, params={'parts': parts})  # holds the state until run
    state = read.request.params['parts'][0]

    refusals = {
        total.ref('value'): 'only a request may reference an accumulator',
        state: f'record {state.record} is not kept by this client',
    }
    for ref, refused in refusals.items():
        for accumulator in (total, other):
            with pytest.raises(SubmitError, match=rf'^parts\[0\]: {refused}'):
                accumulator.push(parts=[ref])
        with pytest.raises(SubmitError, match=rf'^source: {refused}'):
            client.stage(Template(SHIFT, params={'source': ref}, blanks=('shift',)))
        with pytest.raises(SubmitError, match=rf'^offset: {refused}'):
            client.accumulator(Template(SUM, params={'offset': ref}, blanks=('runs',)))
    with pytest.raises(SubmitError, match=rf'^source: {refusals[state]}'):
        client.submit(SHIFT, params={'source': state})
    loading.set()
    assert client.output(read, 'value') == 3.0


def test_a_push_takes_a_pending_record_and_refuses_one_that_did_not_complete(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    pending, cancelled = (client.submit(_load(n)) for n in (1, 2))
    client.cancel(cancelled)
    total = _total(client)
    with pytest.raises(SubmitError, match=f'{cancelled.id} cancelled'):
        total.push(parts=[cancelled.ref('value')])
    total.push(parts=[pending.ref('value')])  # added once pending completes
    read = client.submit(_read(total))
    loading.set()

    state = _state(client, read)
    assert [r.id for r in client.provenance(read).records()] == [state.id, pending.id]
    assert client.output(read, 'value') == 1.0


def test_a_record_that_a_push_waits_for_and_that_fails_stops_the_accumulator(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    pending = client.submit(_load(1))
    total = _total(client)
    total.push(parts=[pending.ref('value')])
    read = client.submit(_read(total))
    client.cancel(pending)
    loading.set()

    stopped = f'the accumulator stopped: push 0: input {pending.id} cancelled'
    state = _state(client, read)
    assert client.wait(read) is Status.FAILED
    assert client.failure([state, read]) == [stopped, f'input {state.id} failed']
    with pytest.raises(SubmitError, match=f'^{stopped}$'):
        total.push(parts=[pending.ref('value')])


def test_concurrent_pushes_add_in_the_order_they_are_logged(
    client: Client, datasets: FakeDatasets
) -> None:
    runs = [datasets.measure(n, float(n)) for n in range(3, 10)]
    loads = client.compute([Request(LOAD, params={'sample': run}) for run in runs])
    digits = client.accumulator(Template(DIGITS, blanks=('parts',)))
    pushes = [
        threading.Thread(target=lambda x=x: digits.push(parts=[x.ref('value')]))
        for x in loads
    ]
    for push in pushes:
        push.start()
    for push in pushes:
        push.join()
    read = client.compute(_read(digits))
    pushed = _state(client, read).request.params  # in the order they were logged
    plain = client.compute(DIGITS, params=pushed)

    assert client.output(read, 'value') == client.output(plain, 'value')


def test_a_push_is_added_after_a_request_that_has_not_started(
    client: Client, loading: threading.Event
) -> None:
    loads = client.compute([_load(n) for n in (1, 2)])
    total = _total(client)
    total.push(parts=[loads[0].ref('value')])
    loading.clear()
    pending = client.submit(_load(2))
    parts = [total.ref('value'), pending.ref('value')]
    summed = client.submit(TOTAL, params={'parts': parts})  # once pending completes
    total.push(parts=[loads[1].ref('value')])  # returns at once
    read = _start(lambda: client.output(total, 'value'))

    with pytest.raises(TimeoutError):
        read.result(timeout=0.05)  # the push waits for summed
    loading.set()
    assert read.result(timeout=5) == 3.0
    assert client.output(summed, 'value') == 3.0  # read 1.0 before the push


def test_cancelling_a_request_that_has_not_started_lets_the_push_be_added(
    client: Client, loading: threading.Event
) -> None:
    loads = client.compute([_load(n) for n in (1, 2)])
    total = _total(client)
    total.push(parts=[loads[0].ref('value')])
    loading.clear()
    pending = client.submit(_load(2))
    parts = [total.ref('value'), pending.ref('value')]
    summed = client.submit(TOTAL, params={'parts': parts})
    total.push(parts=[loads[1].ref('value')])
    read = _start(lambda: client.output(total, 'value'))

    with pytest.raises(TimeoutError):
        read.result(timeout=0.05)
    client.cancel(summed)
    assert read.result(timeout=5) == 3.0
    assert client.status([pending, summed]) == [Status.PENDING, Status.CANCELLED]
    loading.set()


# Adding pushes, failing to add, and dropping pushes


class Sum:
    """
    Makes drivers that sum like ``Fold(TOTAL, operator.add)``, and counts the
    pushes they add.

    A push is added while ``go`` is set; ``adding`` says a push has started.
    With ``failing`` set, the next push fails to add.
    """

    def __init__(self) -> None:
        self.pushed = 0
        self.adding = threading.Event()
        self.failing = False
        self.go = threading.Event()
        self.go.set()

    def __call__(self, fixed: Mapping[str, Any]) -> '_Summing':
        return _Summing(self)


class _Summing:
    def __init__(self, owner: Sum) -> None:
        self._owner = owner
        self._fold = Fold(TOTAL, operator.add)

    def push(self, table: str, columns: Mapping[str, Sequence[Any]]) -> None:
        self._owner.adding.set()
        self._owner.go.wait(timeout=5)
        if self._owner.failing:
            self._owner.failing = False
            raise ValueError('cannot add')
        self._owner.pushed += 1
        self._fold.push(table, columns)

    def outputs(self) -> BaseModel:
        return self._fold.outputs()


@pytest.fixture
def summing() -> Sum:
    return Sum()


@pytest.fixture
def summed_backend(datasets: FakeDatasets, summing: Sum) -> Iterator[Backend]:
    """A backend that sums TOTAL with ``summing``."""
    backend = Backend(
        datasets,
        [
            function_binding(LOAD, load_run),
            function_binding(SHIFT, shift),
            DriverBinding(TOTAL, summing),
        ],
    )
    yield backend
    backend.close()


@pytest.fixture
def summed(summed_backend: Backend) -> Client:
    """A client of ``summed_backend``."""
    return Client(summed_backend, proposal='p1', submitter='anna')


@pytest.fixture
def summed_loads(summed: Client) -> list[Record]:
    """LOAD of runs 1, 2, and 1 again."""
    return summed.compute([_load(n) for n in (1, 2, 1)])


def test_an_accumulator_adds_each_row_once(summed: Client, summing: Sum) -> None:
    client = summed
    total = _total(client)
    values = []
    for n in (1, 2, 1, 2):
        total.push(parts=[client.compute(_load(n)).ref('value')])
        values.append(client.output(client.submit(_read(total)), 'value'))

    assert values == [1.0, 3.0, 4.0, 6.0]
    assert summing.pushed == 4


def test_a_push_that_fails_to_add_stops_the_accumulator(
    summed: Client, summed_loads: list[Record], summing: Sum
) -> None:
    client = summed
    total = _total(client)
    total.push(parts=[summed_loads[0].ref('value')])
    before = client.compute(_read(total))
    summing.failing = True
    summing.go.clear()
    total.push(parts=[summed_loads[1].ref('value')])  # logged; fails once it adds
    after = client.submit(_read(total))
    summing.go.set()

    stopped = 'the accumulator stopped: push 1 failed: cannot add'
    state = _state(client, after)
    assert client.wait(after) is Status.FAILED
    assert client.failure([state, after]) == [stopped, f'input {state.id} failed']
    assert len(state.request.params['parts']) == 2  # the push is in the log
    with pytest.raises(SubmitError, match=f'^{stopped}$'):
        total.push(parts=[summed_loads[2].ref('value')])
    with pytest.raises(SubmitError, match=rf'^source: {stopped}$'):
        client.submit(_read(total))  # it may be half added
    for read in (lambda: client.output(total), lambda: client.provenance(total)):
        with pytest.raises(LookupError, match=f'^{stopped}$'):
            read()
    assert client.output(before, 'value') == 1.0


def _refuse(fixed: Mapping[str, Any]) -> Fold:
    """Fails to make a driver."""
    raise ValueError('cannot open')


def test_an_accumulator_returns_at_once_and_stops_if_it_fails_to_open(
    datasets: FakeDatasets,
) -> None:
    with local(
        proposal='p1', datasets=datasets, bind=[DriverBinding(SUM, _refuse)]
    ) as client:
        summed = client.accumulator(Template(SUM, blanks=('runs',)))  # returns first

        stopped = 'the accumulator stopped: opening failed: cannot open'
        with pytest.raises(LookupError, match=f'^{stopped}$'):
            client.output(summed)
        with pytest.raises(SubmitError, match=f'^{stopped}$'):
            summed.push(runs=[dataset(run=1)])


def test_closing_a_client_drops_the_pushes_that_no_read_needs(
    summed_backend: Backend, summed: Client, summed_loads: list[Record], summing: Sum
) -> None:
    summing.go.clear()
    total = _total(summed)
    for load in summed_loads[:2]:
        total.push(parts=[load.ref('value')])
    summing.adding.wait(timeout=5)
    summed.close()
    summing.go.set()
    summed_backend.close()

    assert summing.pushed == 1  # the push being added when the client closed


def test_ending_a_client_cancels_the_reads_of_its_accumulator(
    summed_backend: Backend, summed: Client, summed_loads: list[Record], summing: Sum
) -> None:
    other = Client(summed_backend, proposal='p1', submitter='bob')
    summing.go.clear()
    total = _total(summed)
    total.push(parts=[summed_loads[0].ref('value')])
    shifted = summed.submit(_read(total))
    state = _state(summed, shifted)
    summed.close()
    summing.go.set()

    assert other.status([state, shifted]) == [Status.CANCELLED] * 2


def test_a_released_accumulator_drops_the_pushes_once_their_reader_is_cancelled(
    summed_backend: Backend, summed: Client, summed_loads: list[Record], summing: Sum
) -> None:
    client = summed
    summing.go.clear()
    total = _total(client)
    for load in summed_loads:
        total.push(parts=[load.ref('value')])
    read = client.submit(_read(total))  # pins three pushes
    summing.adding.wait(timeout=5)
    client.release(total)  # the read keeps the pushes
    client.release(read)  # and no longer once it is cancelled
    summing.go.set()
    summed_backend.close()

    assert client.status(read) is Status.CANCELLED
    assert summing.pushed == 1  # the push being added when the read was cancelled


def test_a_released_accumulator_adds_the_pushes_up_to_the_last_state_read(
    summed: Client, summed_loads: list[Record], summing: Sum
) -> None:
    client = summed
    summing.go.clear()
    total = _total(client)
    total.push(parts=[summed_loads[0].ref('value')])
    total.push(parts=[summed_loads[1].ref('value')])
    read = client.submit(_read(total))
    total.push(parts=[summed_loads[2].ref('value')])
    client.release(total)
    summing.go.set()

    assert client.output(read, 'value') == 3.0
    assert summing.pushed == 2  # the last push was dropped


def test_an_accumulator_value_that_does_not_fit_the_spec_fails_the_reader(
    datasets: FakeDatasets,
) -> None:
    bind = [
        function_binding(LOAD, load_run),
        function_binding(SHIFT, shift),
        DriverBinding(TOTAL, Averaging),  # returns MeanAndSpread
    ]
    with local(proposal='p1', datasets=datasets, bind=bind) as client:
        total = _total(client)
        load = client.compute(_load(1))
        total.push(parts=[load.ref('value')])
        read = client.compute(_read(total))
        state = _state(client, read)

        assert client.failure([state, read]) == [
            'the workflow of total/v1 returned MeanAndSpread, not Value',
            f'input {state.id} failed',
        ]


# The toy specs over arrays


class Data(BaseModel):
    """Base of the models over arrays."""

    model_config = ConfigDict(arbitrary_types_allowed=True)


ArrayPart = NewType('ArrayPart', sc.Variable)
SampleArray = NewType('SampleArray', sc.Variable)
BackgroundArray = NewType('BackgroundArray', sc.Variable)
Copied = NewType('Copied', sc.Variable)
SidesTotal = NewType('SidesTotal', sc.Variable)


class ArrayValue(Data):
    value: sc.Variable


class ArrayParts(Data):
    parts: list[ArrayPart] = Field(min_length=1)


class CopyParams(Data):
    source: sc.Variable


class CopyOutputs(Data):
    value: Copied


class SidesParams(Data):
    samples: list[SampleArray] = Field(default_factory=list)
    backgrounds: list[BackgroundArray] = Field(default_factory=list)


class BothParams(Data):
    samples: list[SampleArray] = Field(min_length=1)
    backgrounds: list[BackgroundArray] = Field(min_length=1)


class Sides(Data):
    difference: sc.Variable
    total: SidesTotal


ARRAY = _spec('array', params=Sample, outputs=ArrayValue)
ARRAY_TOTAL = _spec(
    'array-total', params=ArrayParts, outputs=ArrayValue, tables=('parts',)
)
COPY = _spec('copy', params=CopyParams, outputs=CopyOutputs)
SIDES = _spec(
    'sides', params=SidesParams, outputs=Sides, tables=('samples', 'backgrounds')
)
BOTH = _spec(
    'both', params=BothParams, outputs=Sides, tables=('samples', 'backgrounds')
)


def array(params: Sample) -> ArrayValue:
    """A run as an array of one value."""
    return ArrayValue(value=sc.array(dims=['x'], values=[read_dataset(params.sample)]))


def _listed(value: sc.Variable) -> list[float]:
    return value.values.tolist()


class Copying:
    """
    COPY, which copies its source while ``go`` is set; ``started`` and
    ``finished`` say when it runs.
    """

    def __init__(self) -> None:
        self.started = threading.Event()
        self.finished = threading.Event()
        self.go = threading.Event()
        self.go.set()

    def __call__(self, params: CopyParams) -> CopyOutputs:
        self.started.set()
        self.go.wait(timeout=5)
        self.finished.set()
        return CopyOutputs(value=params.source.copy())


class Subtracting:
    """
    Makes the drivers of SIDES and BOTH, for accumulators and plain requests:
    the sum of the samples minus that of the backgrounds, and the sum of both.
    ``computed`` counts the ``outputs`` calls that computed. A push sets
    ``adding``, and adds while ``go`` is set. An ``outputs`` call sets
    ``computing``, and computes while ``compute`` is set. With ``refusing``
    set, the next push fails, and with ``failing`` set, the next ``outputs``
    call. ``held`` refers weakly to each driver made, and ``returned`` to the
    difference that each ``outputs`` call returned.
    """

    def __init__(self) -> None:
        self.computed = 0
        self.adding = threading.Event()
        self.go = threading.Event()
        self.go.set()
        self.computing = threading.Event()
        self.compute = threading.Event()
        self.compute.set()
        self.refusing = False
        self.failing = False
        self.held: list[weakref.ref[_Sides]] = []
        self.returned: list[weakref.ref[sc.Variable]] = []

    def __call__(self, fixed: Mapping[str, Any]) -> '_Sides':
        driver = _Sides(self)
        self.held.append(weakref.ref(driver))
        return driver


class _Sides:
    def __init__(self, owner: Subtracting) -> None:
        self._owner = owner
        self._sums: dict[str, Any] = {'samples': 0.0, 'backgrounds': 0.0}

    def push(self, table: str, columns: Mapping[str, Sequence[Any]]) -> None:
        self._owner.adding.set()
        self._owner.go.wait(timeout=5)
        if self._owner.refusing:
            self._owner.refusing = False
            raise ValueError('cannot add')
        for value in columns[table]:
            self._sums[table] += value  # in place from the second row

    def outputs(self) -> Sides:
        self._owner.computing.set()
        self._owner.compute.wait(timeout=5)
        if self._owner.failing:
            self._owner.failing = False
            raise ValueError('cannot compute')
        self._owner.computed += 1
        outputs = _subtract(**self._sums)
        self._owner.returned.append(weakref.ref(outputs.difference))
        return outputs


def _subtract(samples: Any, backgrounds: Any) -> Sides:
    return Sides(difference=samples - backgrounds, total=samples + backgrounds)


@pytest.fixture
def copying() -> Copying:
    return Copying()


@pytest.fixture
def subtracting() -> Subtracting:
    return Subtracting()


@pytest.fixture
def in_place(
    datasets: FakeDatasets, copying: Copying, subtracting: Subtracting
) -> Iterator[Client]:
    """A client of a backend whose ARRAY_TOTAL and SIDES add arrays in place."""
    bind = [
        function_binding(ARRAY, array),
        folding(ARRAY_TOTAL, operator.iadd),
        StagedFunction(COPY, copying),
        DriverBinding(SIDES, subtracting),
        DriverBinding(BOTH, subtracting),
    ]
    backend = Backend(datasets, bind)
    yield Client(backend, proposal='p1', submitter='anna')
    copying.go.set()
    subtracting.go.set()
    subtracting.compute.set()
    backend.close()


@pytest.fixture
def loads(in_place: Client) -> list[Record]:
    """The arrays of runs 1 and 2."""
    return in_place.compute(
        [Request(ARRAY, params={'sample': dataset(run=n)}) for n in (1, 2)]
    )


def _array_total(client: Client) -> Accumulator:
    """An accumulator of ARRAY_TOTAL, the sum of a table of arrays."""
    return client.accumulator(Template(ARRAY_TOTAL, blanks=('parts',)))


def _copy(client: Client, accumulator: Accumulator, output: str = 'value') -> Record:
    return client.submit(COPY, params={'source': accumulator.ref(output)})


# A stage that lets go of its template's values


def test_a_stage_lets_go_of_what_its_template_references_once_staged(
    datasets: FakeDatasets,
) -> None:
    class KeepsTheSum:
        """Stages COPY keeping only the sum of the source, not the source."""

        spec = COPY

        def compute(self, params: CopyParams) -> CopyOutputs:
            return self.make_stage({'source': params.source}, ())({})

        def make_stage(
            self, fixed: Mapping[str, Any], blanks: Collection[str]
        ) -> Callable[[Mapping[str, Any]], CopyOutputs]:
            total = CopyOutputs(value=fixed['source'].sum())
            return lambda given: total

    bind = [function_binding(ARRAY, array), KeepsTheSum()]
    with local(proposal='p1', datasets=datasets, bind=bind) as client:
        loaded = client.compute(ARRAY, params={'sample': dataset(run=2)})
        value = weakref.ref(client.output(loaded, 'value'))
        stage = client.stage(Template(COPY, params={'source': loaded.ref('value')}))
        client.release(loaded)
        assert value() is not None

        first = client.compute(stage)
        assert value() is None
        again = client.compute(stage)
        assert [client.output(r, 'value').value for r in (first, again)] == [2.0] * 2


# Accumulators that add in place


def test_adding_in_place_leaves_the_outputs_pushed_unchanged(
    in_place: Client, loads: list[Record]
) -> None:
    client = in_place
    total = _array_total(client)
    for load in loads:
        total.push(parts=[load.ref('value')])
    plain = client.compute(ARRAY_TOTAL, params=_parts(*loads))

    assert _listed(client.output(_copy(client, total), 'value')) == [3.0]
    assert _listed(client.output(plain, 'value')) == [3.0]
    assert [_listed(client.output(x, 'value')) for x in loads] == [[1.0], [2.0]]


def test_no_client_reads_or_references_the_record_of_a_state(
    in_place: Client, loads: list[Record], copying: Copying
) -> None:
    client = in_place
    copying.go.clear()
    total = _array_total(client)
    total.push(parts=[loads[0].ref('value')])
    copied = _copy(client, total)  # holds the state until it has run
    state = _state(client, copied)

    not_kept = f'record {state.id} is not kept by this client'
    with pytest.raises(LookupError, match=f'^{not_kept}$'):
        client.output(state, 'value')
    for refuse in (
        lambda: client.submit(COPY, params={'source': state.ref('value')}),
        lambda: client.stage(Template(COPY, params={'source': state.ref('value')})),
        lambda: total.push(parts=[state.ref('value')]),
    ):
        with pytest.raises(SubmitError, match=f': {not_kept}$'):
            refuse()
    copying.go.set()
    total.push(parts=[loads[1].ref('value')])
    assert _listed(client.output(total, 'value')) == [3.0]
    assert _listed(client.output(copied, 'value')) == [1.0]


def test_cancelling_the_record_of_a_state_fails_its_readers_and_frees_the_push(
    in_place: Client,
    loads: list[Record],
    copying: Copying,
    subtracting: Subtracting,
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push(samples=[loads[0].ref('value')])
    subtracting.go.clear()
    sides.push(backgrounds=[loads[1].ref('value')])  # adds once go is set
    copying.go.clear()
    copied = _copy(client, sides, 'difference')  # its state waits for that push
    sides.push(samples=[loads[1].ref('value')])  # waits for the readers
    state = _state(client, copied)
    assert state in client.records()
    client.cancel(state)
    subtracting.go.set()

    read = _start(lambda: client.output(sides, 'difference'))
    assert _listed(read.result(timeout=2)) == [1.0]  # copying.go is still clear
    assert client.failure(copied) == f'input {state.id} cancelled'


def test_two_submissions_that_read_one_state_make_two_records_computed_once(
    in_place: Client, loads: list[Record], subtracting: Subtracting
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push(samples=[loads[0].ref('value')])
    first = _copy(client, sides, 'difference')
    client.wait(first)
    second = _copy(client, sides, 'difference')
    client.wait(second)

    assert _state(client, first).id != _state(client, second).id
    assert _state(client, first).request == _state(client, second).request
    assert subtracting.computed == 1


def test_a_push_is_added_once_the_requests_that_read_the_state_before_it_have_run(
    in_place: Client, loads: list[Record], copying: Copying
) -> None:
    client = in_place
    copying.go.clear()
    total = _array_total(client)
    total.push(parts=[loads[0].ref('value')])
    copied = _copy(client, total)
    copying.started.wait(timeout=5)  # the copy runs, and reads the value
    total.push(parts=[loads[1].ref('value')])  # returns at once
    read = _start(lambda: client.output(total, 'value'))

    with pytest.raises(TimeoutError):
        read.result(timeout=0.05)
    copying.go.set()
    assert _listed(read.result(timeout=5)) == [3.0]
    assert _listed(client.output(copied, 'value')) == [1.0]


def test_a_push_is_added_once_a_cancelled_request_that_still_runs_returns(
    in_place: Client, loads: list[Record], copying: Copying
) -> None:
    client = in_place
    copying.go.clear()
    total = _array_total(client)
    total.push(parts=[loads[0].ref('value')])
    copied = _copy(client, total)
    copying.started.wait(timeout=5)
    client.cancel(copied)
    total.push(parts=[loads[1].ref('value')])
    read = _start(lambda: client.output(total, 'value'))

    with pytest.raises(TimeoutError):
        read.result(timeout=0.05)
    copying.go.set()
    assert _listed(read.result(timeout=5)) == [3.0]
    assert client.status(copied) is Status.CANCELLED


def test_references_in_one_submission_pin_one_state_while_rows_are_pushed(
    in_place: Client, datasets: FakeDatasets
) -> None:
    client = in_place
    runs = [datasets.measure(n, float(n)) for n in range(3, 23)]
    loads = client.compute([Request(ARRAY, params={'sample': run}) for run in runs])
    total = _array_total(client)
    total.push(parts=[loads[0].ref('value')])
    pushing = threading.Thread(
        target=lambda: [total.push(parts=[x.ref('value')]) for x in loads[1:]]
    )
    pushing.start()
    copy_twice = [Request(COPY, params={'source': total.ref('value')})] * 2
    pairs = [client.compute(copy_twice) for _ in range(20)]
    pushing.join()

    for pair in pairs:
        assert len({r.request.params['source'] for r in pair}) == 1  # one state
        upto = len(_state(client, pair[0]).request.params['parts'])
        expected = float(sum(range(3, 3 + upto)))
        assert [_listed(client.output(r, 'value')) for r in pair] == [[expected]] * 2


def test_a_request_reads_several_accumulators(
    in_place: Client, loads: list[Record]
) -> None:
    client = in_place
    first, second = _array_total(client), _array_total(client)
    first.push(parts=[loads[0].ref('value')])
    second.push(parts=[loads[1].ref('value')])

    both = client.compute(ARRAY_TOTAL, params=_parts(first, second))
    assert _listed(client.output(both, 'value')) == [3.0]
    twice = client.compute(
        SIDES,
        params={'samples': [first.ref('value')], 'backgrounds': [first.ref('value')]},
    )
    assert _listed(client.output(twice, 'difference')) == [0.0]


def test_a_submission_pins_states_whose_pushes_wait_and_returns_at_once(
    in_place: Client, loads: list[Record], copying: Copying
) -> None:
    client = in_place
    copying.go.clear()
    first, second = _array_total(client), _array_total(client)
    for total in (first, second):
        total.push(parts=[loads[0].ref('value')])
        _copy(client, total)  # holds back the next push
    for total in (first, second):
        total.push(parts=[loads[1].ref('value')])
    reads = client.submit(
        [Request(COPY, params={'source': t.ref('value')}) for t in (first, second)]
    )

    assert [len(_state(client, r).request.params['parts']) for r in reads] == [2, 2]
    assert client.status(reads) == [Status.PENDING] * 2
    copying.go.set()
    assert [_listed(client.output(r, 'value')) for r in reads] == [[3.0]] * 2


def test_a_released_accumulator_still_adds_the_pushes_logged(
    in_place: Client, loads: list[Record], copying: Copying
) -> None:
    client = in_place
    copying.go.clear()
    total = _array_total(client)
    total.push(parts=[loads[0].ref('value')])
    first = _copy(client, total)
    copying.started.wait(timeout=5)
    total.push(parts=[loads[1].ref('value')])  # waits for the first copy
    second = _copy(client, total)
    client.release(total)  # the copies are still readers of their states
    copying.go.set()

    assert [_listed(client.output(r, 'value')) for r in (first, second)] == [
        [1.0],
        [3.0],
    ]


def test_a_released_accumulator_takes_no_pushes_or_references_and_is_still_read(
    in_place: Client, loads: list[Record], copying: Copying
) -> None:
    client = in_place
    copying.go.clear()
    total = _array_total(client)
    total.push(parts=[loads[0].ref('value')])
    copied = _copy(client, total)
    client.release(total)  # the copy is still a reader of its state

    released = 'the accumulator was released or is unknown'
    with pytest.raises(SubmitError, match=f'^{released}$'):
        total.push(parts=[loads[1].ref('value')])
    with pytest.raises(SubmitError, match=rf'^source: {released}$'):
        _copy(client, total)
    for read in (
        lambda: client.output(total, 'value'),
        lambda: client.provenance(total),
    ):
        with pytest.raises(LookupError, match=f'^{released}$'):
            read()
    copying.go.set()
    assert _listed(client.output(copied, 'value')) == [1.0]  # read after the release


def test_a_read_is_a_copy_that_a_later_push_leaves_unchanged(
    in_place: Client, loads: list[Record]
) -> None:
    client = in_place
    total = _array_total(client)
    total.push(parts=[loads[0].ref('value')])
    first = client.output(total, 'value')
    total.push(parts=[loads[1].ref('value')])

    assert _listed(first) == [1.0]
    assert _listed(client.output(total, 'value')) == [3.0]


# Several tables, and the outputs of a state


def _sides_of(client: Client) -> Accumulator:
    """An accumulator of SIDES, which fills both tables."""
    return client.accumulator(Template(SIDES, blanks=('samples', 'backgrounds')))


def _sides(outputs: Mapping[str, sc.Variable]) -> dict[str, list[float]]:
    return {name: _listed(value) for name, value in outputs.items()}


def test_pushes_into_several_tables_give_the_plain_request_over_them(
    in_place: Client, loads: list[Record]
) -> None:
    client = in_place
    sides = _sides_of(client)
    pushes = [('backgrounds', loads[0]), ('samples', loads[1]), ('samples', loads[0])]
    reads, plains = [], []
    for n, (table, load) in enumerate(pushes, start=1):
        sides.push(**{table: [load.ref('value')]})
        tables = {
            t: [x.ref('value') for u, x in pushes[:n] if u == t]
            for t in ('samples', 'backgrounds')
        }
        plain = client.compute(SIDES, params=tables)
        reads.append(_sides(client.output(sides)))
        plains.append(_sides(client.output(plain)))

    assert reads == plains
    assert [r['difference'] for r in reads] == [[-1.0], [1.0], [2.0]]
    assert client.provenance(sides).request == plain.request
    assert {r.id for r in client.provenance(sides).records()} == {x.id for x in loads}


def test_readers_of_one_state_compute_its_outputs_once(
    in_place: Client, loads: list[Record], copying: Copying, subtracting: Subtracting
) -> None:
    client = in_place
    copying.go.clear()
    sides = _sides_of(client)
    sides.push(samples=[loads[0].ref('value')])
    copied = _copy(client, sides, 'difference')
    copying.started.wait(timeout=5)  # it has read 'difference', and holds the state
    both = client.output(sides)
    again = client.output(sides, 'total')
    copying.go.set()
    client.wait(copied)
    sides.push(backgrounds=[loads[1].ref('value')])
    after = client.output(sides, 'difference')

    assert subtracting.computed == 2  # once per state
    assert _sides(both) == {'difference': [1.0], 'total': [1.0]}
    assert [_listed(again), _listed(after)] == [[1.0], [-1.0]]


def test_the_outputs_of_a_state_are_kept_until_the_next_push(
    in_place: Client, loads: list[Record], subtracting: Subtracting
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push(samples=[loads[0].ref('value')])
    reads = [client.output(sides, name) for name in ('difference', 'total')]

    assert subtracting.computed == 1  # the first read was done before the second
    assert [_listed(r) for r in reads] == [[1.0], [1.0]]
    sides.push(backgrounds=[loads[1].ref('value')])
    assert _listed(client.output(sides, 'difference')) == [-1.0]
    assert subtracting.computed == 2


def test_a_read_while_a_push_adds_pins_the_state_after_it(
    in_place: Client, loads: list[Record], subtracting: Subtracting
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push(samples=[loads[0].ref('value')])
    client.output(sides)  # the first push is added
    subtracting.adding.clear()
    subtracting.go.clear()
    sides.push(backgrounds=[loads[1].ref('value')])
    subtracting.adding.wait(timeout=5)  # the push adds
    read = _start(lambda: client.output(sides, 'difference'))
    copied = _copy(client, sides, 'difference')

    assert _state(client, copied).request == client.provenance(sides).request
    assert len(client.provenance(sides).request.params['backgrounds']) == 1
    with pytest.raises(TimeoutError):
        read.result(timeout=0.05)  # waits for the state, as for a record
    subtracting.go.set()
    assert _listed(read.result(timeout=5)) == [-1.0]
    assert _listed(client.output(copied, 'value')) == [-1.0]


def test_a_push_of_several_rows_gives_one_state(
    in_place: Client, loads: list[Record]
) -> None:
    client = in_place
    sides = _sides_of(client)
    rows = [x.ref('value') for x in loads]
    sides.push(samples=rows)
    copied = _copy(client, sides, 'difference')
    plain = client.compute(SIDES, params={'samples': rows})

    assert _state(client, copied).request == plain.request
    assert _listed(client.output(copied, 'value')) == [3.0]
    assert _sides(client.output(sides)) == _sides(client.output(plain))


def test_a_state_is_read_once_its_plain_request_would_be_accepted(
    in_place: Client, loads: list[Record]
) -> None:
    client = in_place
    both = client.accumulator(Template(BOTH, blanks=('samples', 'backgrounds')))
    both.push(samples=[loads[0].ref('value')])
    tables = {'samples': [loads[0].ref('value')], 'backgrounds': []}
    with pytest.raises(SubmitError, match='backgrounds: the table has') as refused:
        client.submit(BOTH, params=tables)
    reason = re.escape(str(refused.value))

    with pytest.raises(SubmitError, match=rf'^source: {reason}$'):
        _copy(client, both, 'difference')
    for read in (lambda: client.output(both), lambda: client.provenance(both)):
        with pytest.raises(LookupError, match=f'^{reason}$'):
            read()
    both.push(backgrounds=[loads[1].ref('value')])
    tables['backgrounds'] = [loads[1].ref('value')]
    plain = client.compute(BOTH, params=tables)
    assert _sides(client.output(both)) == {'difference': [-1.0], 'total': [3.0]}
    assert _sides(client.output(plain)) == _sides(client.output(both))
    assert client.provenance(both).request == plain.request


def test_a_push_is_added_once_a_read_of_the_outputs_in_progress_is_done(
    in_place: Client, loads: list[Record], subtracting: Subtracting
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push(samples=[loads[0].ref('value')])
    subtracting.compute.clear()
    read = _start(lambda: client.output(sides, 'difference'))
    subtracting.computing.wait(timeout=5)  # the read computes the output
    subtracting.adding.clear()
    sides.push(backgrounds=[loads[1].ref('value')])  # returns at once

    assert not subtracting.adding.wait(timeout=0.05)
    subtracting.compute.set()
    assert _listed(read.result(timeout=5)) == [1.0]
    assert _listed(client.output(sides, 'difference')) == [-1.0]


def test_a_push_proceeds_after_readers_whose_outputs_failed(
    in_place: Client, loads: list[Record], subtracting: Subtracting
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push(samples=[loads[0].ref('value')])
    subtracting.failing = True
    copied = _copy(client, sides, 'difference')
    client.wait(copied)
    subtracting.failing = True
    with pytest.raises(ValueError, match=r'^cannot compute$'):
        client.output(sides, 'difference')

    _start(lambda: sides.push(backgrounds=[loads[1].ref('value')])).result(5)
    state = _state(client, copied)
    assert client.failure([state, copied]) == [
        'cannot compute',
        f'input {state.id} failed',
    ]
    assert _listed(client.output(sides, 'difference')) == [-1.0]


def test_concurrent_readers_of_one_state_compute_its_outputs_once(
    in_place: Client, loads: list[Record], subtracting: Subtracting
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push(samples=[loads[0].ref('value')])
    subtracting.compute.clear()
    read = _start(lambda: client.output(sides, 'difference'))
    subtracting.computing.wait(timeout=5)  # the read computes, and holds the state
    copies = client.submit(
        [Request(COPY, params={'source': sides.ref('difference')})] * 2
    )
    subtracting.compute.set()
    assert _listed(read.result(timeout=5)) == [1.0]
    assert [_listed(client.output(c, 'value')) for c in copies] == [[1.0]] * 2
    assert subtracting.computed == 1


def test_readers_of_a_state_of_kept_rows_compute_the_plain_request_once(
    datasets: FakeDatasets, copying: Copying
) -> None:
    calls: list[int] = []

    def sides(params: SidesParams) -> Sides:
        calls.append(len(params.samples))
        return _subtract(sum(params.samples, 0.0), sum(params.backgrounds, 0.0))

    bind = [
        function_binding(ARRAY, array),
        function_binding(COPY, copying),
        kept_rows(SIDES, sides),
    ]
    with local(proposal='p1', datasets=datasets, bind=bind) as client:
        load = client.compute(ARRAY, params={'sample': dataset(run=1)})
        kept = client.accumulator(
            Template(SIDES, params={'backgrounds': []}, blanks=('samples',))
        )
        kept.push(samples=[load.ref('value')])
        copying.go.clear()
        copied = _copy(client, kept, 'difference')
        copying.started.wait(timeout=5)  # it has read 'difference', and holds the state
        total = client.output(kept, 'total')
        copying.go.set()

        assert _listed(client.output(copied, 'value')) == _listed(total) == [1.0]
        assert calls == [1]


# Freeze


def test_a_freeze_is_the_record_of_the_last_state_computed_once(
    in_place: Client, loads: list[Record], subtracting: Subtracting
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push(samples=[loads[0].ref('value')])
    sides.push(backgrounds=[loads[1].ref('value')])
    looked = client.output(sides, 'difference')
    plain = client.provenance(sides).request
    total = client.freeze(sides)

    assert total.request == plain
    assert _listed(client.output(total, 'difference')) == _listed(looked) == [-1.0]
    assert subtracting.computed == 1  # the look and the freeze read one state
    assert client.output(total, 'difference') is subtracting.returned[0]()  # no copy
    assert total in client.records()


def test_a_push_after_a_freeze_is_refused_while_its_record_is_pending(
    in_place: Client, loads: list[Record], subtracting: Subtracting
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push(samples=[loads[0].ref('value')])
    subtracting.compute.clear()
    total = client.freeze(sides)

    with pytest.raises(SubmitError, match=f'frozen as record {total.id}'):
        sides.push(samples=[loads[1].ref('value')])
    with pytest.raises(SubmitError, match=f'frozen as record {total.id}'):
        client.freeze(sides)
    subtracting.compute.set()
    assert _listed(client.output(total, 'total')) == [1.0]


def test_a_frozen_accumulator_is_read_through_its_record(
    in_place: Client, loads: list[Record]
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push(samples=[loads[0].ref('value')])
    total = client.freeze(sides)
    client.wait(total)

    with pytest.raises(LookupError, match=f'read record {total.id} instead'):
        client.output(sides)
    with pytest.raises(SubmitError, match=f'read record {total.id} instead'):
        _copy(client, sides, 'difference')
    with pytest.raises(LookupError, match=f'read record {total.id} instead'):
        client.provenance(sides)
    copied = client.compute(COPY, params={'source': total.ref('difference')})
    assert _listed(client.output(copied, 'value')) == [1.0]


def test_templates_and_pushes_reference_a_frozen_record(
    in_place: Client, loads: list[Record]
) -> None:
    client = in_place
    first = _array_total(client)
    first.push(parts=[x.ref('value') for x in loads])
    frozen = client.freeze(first)
    second = _array_total(client)
    second.push(parts=[frozen.ref('value')])
    second.push(parts=[loads[0].ref('value')])
    staged = client.stage(Template(COPY, params={'source': frozen.ref('value')}))

    assert _listed(client.output(second, 'value')) == [4.0]
    assert _listed(client.output(client.compute(staged), 'value')) == [3.0]


def test_a_freeze_whose_plain_request_would_be_refused_is_refused(
    in_place: Client, loads: list[Record]
) -> None:
    client = in_place
    both = client.accumulator(Template(BOTH, blanks=('samples', 'backgrounds')))
    both.push(samples=[loads[0].ref('value')])

    with pytest.raises(SubmitError, match='backgrounds: the table has no row'):
        client.freeze(both)
    both.push(backgrounds=[loads[1].ref('value')])  # takes pushes again
    total = client.freeze(both)
    assert _listed(client.output(total, 'difference')) == [-1.0]


def test_a_cancelled_freeze_leaves_the_accumulator_readable_and_shut(
    in_place: Client, loads: list[Record], subtracting: Subtracting
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push(samples=[loads[0].ref('value')])
    subtracting.compute.clear()
    cancelled = client.freeze(sides)
    subtracting.computing.wait(timeout=5)  # it runs, and holds the state
    client.release(cancelled)  # nothing keeps it any more
    subtracting.compute.set()

    assert client.wait(cancelled) == Status.CANCELLED
    # no push comes to drop the outputs computed for the cancelled freeze
    assert _eventually(
        lambda: gc.collect() >= 0 and [r() for r in subtracting.returned] == [None]
    )
    with pytest.raises(SubmitError, match=f'frozen as record {cancelled.id}'):
        sides.push(backgrounds=[loads[1].ref('value')])
    assert _listed(client.output(sides, 'total')) == [1.0]
    again = client.freeze(sides)
    assert _listed(client.output(again, 'total')) == [1.0]


def test_a_failed_freeze_leaves_the_accumulator_readable_and_shut(
    in_place: Client, loads: list[Record], subtracting: Subtracting
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push(samples=[loads[0].ref('value')])
    subtracting.failing = True
    failed = client.freeze(sides)

    assert client.wait(failed) == Status.FAILED
    with pytest.raises(SubmitError, match=f'frozen as record {failed.id}'):
        sides.push(backgrounds=[loads[1].ref('value')])
    assert _listed(client.output(sides, 'total')) == [1.0]
    again = client.freeze(sides)
    assert _listed(client.output(again, 'total')) == [1.0]


def test_a_frozen_driver_is_dropped_once_its_readers_are_done(
    in_place: Client,
    loads: list[Record],
    copying: Copying,
    subtracting: Subtracting,
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push(samples=[loads[0].ref('value')])
    copying.go.clear()
    copied = _copy(client, sides, 'difference')  # pins the same state
    copying.started.wait(timeout=5)
    total = client.freeze(sides)
    client.wait(total)

    assert subtracting.held[0]() is not None  # the copy still reads the state
    copying.go.set()
    client.wait(copied)
    client.release(copied)
    gc.collect()
    assert subtracting.held[0]() is None
    assert _listed(client.output(total, 'difference')) == [1.0]


def test_a_stopped_driver_is_dropped_before_the_accumulator_is_released(
    in_place: Client, loads: list[Record], subtracting: Subtracting
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push(samples=[loads[0].ref('value')])
    client.output(sides)  # the first push is added
    subtracting.refusing = True
    sides.push(samples=[loads[1].ref('value')])  # fails to add

    with pytest.raises(LookupError, match='push 1 failed: cannot add'):
        client.output(sides, 'difference')
    gc.collect()
    assert subtracting.held[0]() is None  # nothing reads it any more


# Selections


class Grid(Data):
    value: sc.Variable
    text: str


GRID = _spec('grid', params=Sample, outputs=Grid)


def grid(params: Sample) -> Grid:
    values = np.arange(6.0).reshape(3, 2) * read_dataset(params.sample)
    return Grid(value=sc.array(dims=['x', 'y'], values=values), text='a')


@pytest.fixture
def grids(datasets: FakeDatasets) -> Iterator[Client]:
    """A client of a backend with GRID, a 3x2 array per run, and ARRAY_TOTAL."""
    bind = [function_binding(GRID, grid), folding(ARRAY_TOTAL, operator.add)]
    with local(proposal='p1', datasets=datasets, bind=bind) as client:
        yield client


def _grid(client: Client, n: int) -> Record:
    return client.compute(GRID, params={'sample': dataset(run=n)})


def test_a_selection_copies_part_of_an_output_and_makes_no_record(
    grids: Client,
) -> None:
    client = grids
    record = _grid(client, 1)
    row = client.output(record, 'value', select={'x': 1})
    corner = client.output(record, 'value', select={'x': slice(0, 2), 'y': 1})
    row.values[0] = 99.0

    assert row.dims == ('y',)
    assert _listed(corner) == [1.0, 3.0]
    assert _listed(client.output(record, 'value')['x', 1]) == [2.0, 3.0]
    assert client.records() == [record]


def test_a_selection_of_an_accumulator_copies_part_of_its_current_state(
    grids: Client,
) -> None:
    client = grids
    runs = [_grid(client, n) for n in (1, 2)]
    total = _array_total(client)
    cuts = []
    for run in runs:
        total.push(parts=[run.ref('value')])
        cuts.append(client.output(total, 'value', select={'y': 1}))

    assert [_listed(c) for c in cuts] == [[1.0, 3.0, 5.0], [3.0, 9.0, 15.0]]
    assert client.records() == runs


def test_a_selection_is_refused_for_an_output_not_declared_as_an_array(
    grids: Client,
) -> None:
    client = grids
    record = _grid(client, 1)

    with pytest.raises(ValueError, match='needs an array'):
        client.output(record, 'text', select={'x': 0})
    with pytest.raises(ValueError, match='name of an output'):
        client.output(record, select={'x': 0})


def test_a_selection_the_output_cannot_take_raises_what_the_slice_raises(
    grids: Client,
) -> None:
    client = grids
    record, other = _grid(client, 1), _grid(client, 2)
    total = _array_total(client)
    total.push(parts=[record.ref('value')])

    with pytest.raises(IndexError):
        client.output(record, 'value', select={'x': 3})
    with pytest.raises(IndexError):
        client.output(total, 'value', select={'x': 3})
    with pytest.raises(sc.DimensionError):
        client.output(total, 'value', select={'z': 0})
    total.push(parts=[other.ref('value')])  # the reads let go of the state
    assert _listed(client.output(total, 'value', select={'x': 2})) == [12.0, 15.0]
