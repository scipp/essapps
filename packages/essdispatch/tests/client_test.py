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
from typing import Annotated, Any, Self

import numpy as np
import pytest
import scipp as sc
from pydantic import BaseModel, Field, field_validator, model_validator

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
from ess.dispatch.log import Log
from ess.dispatch.testing import FakeDatasets, StagedFunction, kept_rows
from ess.spec import (
    REPEATED_ROWS,
    AccumulatorRef,
    Array,
    ArraySpec,
    DriverBinding,
    NexusFile,
    OpaqueFile,
    WorkflowSpec,
    function_binding,
)


class Run(BaseModel):
    file: NexusFile


class Parts(BaseModel):
    value: Array()  # type: ignore[valid-type]


class LoadOutputs(BaseModel):
    value: Array()  # type: ignore[valid-type]
    extra: Array()  # type: ignore[valid-type]


class LoadParams(BaseModel):
    sample: Run


class Shift(BaseModel):
    offset: float = 0.0


class ShiftParams(BaseModel):
    source: Parts
    shift: Shift = Field(default_factory=Shift)


class Factor(BaseModel):
    factor: float = 1.0


class ScaleParams(BaseModel):
    sample: Run
    scaling: Factor = Field(default_factory=Factor)


def _spec(name: str, params: type[BaseModel], outputs: type[BaseModel]) -> WorkflowSpec:
    return WorkflowSpec(
        name=name,
        version=1,
        title=name,
        description=name,
        params=params,
        outputs=outputs,
    )


LOAD = _spec('load', LoadParams, LoadOutputs)
SHIFT = _spec('shift', ShiftParams, Parts)
SCALE = _spec('scale', ScaleParams, Parts)


class PartsTable(BaseModel):
    parts: list[Parts]


class PairsTable(BaseModel):
    pairs: list[LoadOutputs]


TOTAL = _spec('total', PartsTable, Parts)
PAIRS = _spec('pairs', PairsTable, LoadOutputs)


class Reference(BaseModel):
    total: float


class StitchParams(BaseModel):
    reference: Reference
    low: list[Parts]
    high: list[Parts]


class Curve(BaseModel):
    curve: Array()  # type: ignore[valid-type]


STITCH = _spec('stitch', StitchParams, Curve)
DIGITS = _spec('digits', PartsTable, Parts)


class MeanParams(BaseModel):
    parts: list[Parts] = Field(min_length=1)


class Mean(BaseModel):
    mean: Array()  # type: ignore[valid-type]


MEAN = _spec('mean', MeanParams, Mean)


class PairsOrMoreParams(BaseModel):
    parts: list[Parts] = Field(min_length=2)


MEAN_OF_TWO = _spec('mean-of-two', PairsOrMoreParams, Mean)


class MeanAndSpread(BaseModel):
    mean: Array()  # type: ignore[valid-type]
    spread: Array() | None = None  # type: ignore[valid-type]


SPREAD = _spec('spread', MeanParams, MeanAndSpread)


class Averaging:
    """
    The driver of MEAN, which holds a sum and a count rather than a mean; also
    of MEAN_OF_TWO, and of SPREAD without its optional output.
    """

    def __init__(self, fixed: Mapping[str, Any]) -> None:
        self._total = 0.0
        self._count = 0

    def push(self, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
        for row in rows:
            self._total += row['value']
            self._count += 1

    def outputs(self) -> Mapping[str, Any]:
        return {'mean': self._total / self._count}


class Weighted(BaseModel):
    value: Array()  # type: ignore[valid-type]
    weight: float = 1.0


class WeightedTable(BaseModel):
    rows: list[Weighted]


WEIGHTED = _spec('weighted', WeightedTable, Parts)


class WeightedSum:
    """The driver of WEIGHTED: the sum of each value times its weight."""

    def __init__(self, fixed: Mapping[str, Any]) -> None:
        self._total = 0.0

    def push(self, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
        for row in rows:
            self._total += row['value'] * row['weight']

    def outputs(self) -> Mapping[str, Any]:
        return {'value': self._total}


class Scale(BaseModel):
    factor: float = 1.0


class SumParams(BaseModel):
    runs: list[Run]
    scale: Scale = Field(default_factory=Scale)
    offset: Parts | None = None


SUM = _spec('sum', SumParams, Parts)


class RepeatableParams(SumParams):
    runs: Annotated[list[Run], REPEATED_ROWS]


REPEATABLE = _spec('repeatable', RepeatableParams, Parts)


class PositiveScale(Scale):
    @field_validator('factor')
    @classmethod
    def _positive(cls, factor: float) -> float:
        return abs(factor)


class PositiveParams(SumParams):
    scale: PositiveScale = Field(default_factory=PositiveScale)


class MeanScale(BaseModel):
    factor: float | None = None

    @field_validator('factor')
    @classmethod
    def _positive(cls, factor: float | None) -> float | None:
        return None if factor is None else abs(factor)


class MeanSumParams(BaseModel):
    runs: list[Run]
    scale: MeanScale = Field(default_factory=MeanScale)
    offset: Parts | None = None

    @model_validator(mode='after')
    def _mean(self) -> Self:
        """Unless given, ``scale`` makes the sum a mean: one over the number of runs."""
        if not self.runs:
            raise ValueError('a mean needs runs')
        if self.scale.factor is None:
            self.scale.factor = 1 / len(self.runs)
        return self


class TaggedRun(Run):
    tag: Any = None


class Labels(BaseModel):
    tags: Any = None


class TaggedParams(SumParams):
    runs: list[TaggedRun]
    labels: Labels = Field(default_factory=Labels)


POSITIVE = _spec('positive', PositiveParams, Parts)
MEAN_SUM = _spec('mean-sum', MeanSumParams, Parts)
TAGGED = _spec('tagged', TaggedParams, Parts)


class _ScaledTotal:
    """``offset`` plus each run times ``scale``; ``rows`` lists the rows pushed."""

    def __init__(self, scale: Mapping[str, float], offset: Mapping[str, Any] | None):
        self._scale = scale['factor']
        self.total = offset['value'] if offset else 0.0
        self.rows: list[Mapping[str, Any]] = []

    def push(self, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
        for row in rows:
            self.rows.append(row)
            self.total += self._scale * row['file']

    def outputs(self) -> Mapping[str, Any]:
        return {'value': self.total}


class ScaledSum:
    """
    Makes the drivers of SUM and the specs with validators: ``offset`` plus
    each run times ``scale``. ``opened`` lists the fixed values of each driver
    made, for an accumulator or a plain request, and ``held`` the drivers.
    """

    def __init__(self) -> None:
        self.opened: list[dict[str, Any]] = []
        self.held: list[_ScaledTotal] = []

    def __call__(self, fixed: Mapping[str, Any]) -> _ScaledTotal:
        self.opened.append(dict(fixed))
        self.held.append(_ScaledTotal(fixed['scale'], fixed['offset']))
        return self.held[-1]


class Fold:
    """
    A driver that combines each field of one table's rows with ``operation``,
    as in ``operation(operation(a, b), c)``, in push order. The first row is
    copied, so that combining in place never changes the output it came from.
    With no rows it returns no outputs.
    """

    def __init__(self, operation: Callable[[Any, Any], Any]) -> None:
        self._operation = operation
        self._total: dict[str, Any] | None = None

    def push(self, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
        for row in rows:
            if self._total is None:
                self._total = copy.deepcopy(dict(row))
            else:
                for field, value in row.items():
                    self._total[field] = self._operation(self._total[field], value)

    def outputs(self) -> Mapping[str, Any]:
        return self._total or {}


def folding(spec: WorkflowSpec, operation: Callable[[Any, Any], Any]) -> DriverBinding:
    """A binding of a spec over one table, whose outputs are the rows' fields folded."""
    return DriverBinding(spec, lambda fixed: Fold(operation))


def pairs(pairs: list[dict[str, float]]) -> dict[str, float]:
    """PAIRS as a plain function over its table."""
    return {f: sum(row[f] for row in pairs) for f in ('value', 'extra')}


def stitch(
    reference: dict[str, float],
    low: list[dict[str, float]],
    high: list[dict[str, float]],
) -> dict[str, list[float]]:
    """
    STITCH as a plain function, a joint computation over both tables: their
    values, low ones first, scaled together so that they sum to the reference total.
    """
    values = [row['value'] for row in (*low, *high)]
    return {'curve': [reference['total'] * value / sum(values) for value in values]}


def append_digit(number: float, digit: float) -> float:
    """An order-sensitive combination, slow enough for pushes to overlap."""
    time.sleep(0.001)
    return number * 10 + digit


def _shift(source: dict[str, float], shift: dict[str, float]) -> dict[str, float]:
    """SHIFT as a plain function."""
    return {'value': source['value'] + shift['offset']}


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


@pytest.fixture
def shifting() -> StagedFunction:
    return StagedFunction(SHIFT, _shift)


@pytest.fixture
def scaling() -> StagedFunction:
    return StagedFunction(
        SCALE, lambda sample, scaling: {'value': sample['file'] * scaling['factor']}
    )


@pytest.fixture
def loading() -> threading.Event:
    """Set to let LOAD finish."""
    event = threading.Event()
    event.set()
    return event


@pytest.fixture
def datasets() -> FakeDatasets:
    datasets = FakeDatasets(proposal='p1')
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
    def load(sample: dict[str, float]) -> dict[str, Any]:
        loading.wait(timeout=5)
        run = sample['file']
        return {'value': run, 'extra': -run}

    backend = Backend(
        datasets,
        [
            function_binding(LOAD, load),
            shifting,
            scaling,
            folding(TOTAL, operator.add),
            function_binding(PAIRS, pairs),
            kept_rows(STITCH, stitch),
            folding(DIGITS, append_digit),
            DriverBinding(MEAN, Averaging),
            DriverBinding(MEAN_OF_TWO, Averaging),
            DriverBinding(SPREAD, Averaging),
            DriverBinding(WEIGHTED, WeightedSum),
            folding(FILES_SUM, operator.add),
            *(
                DriverBinding(spec, scaled_sum)
                for spec in (SUM, POSITIVE, REPEATABLE, MEAN_SUM, TAGGED)
            ),
        ],
    )
    yield backend
    backend.close()


@pytest.fixture
def client(backend: Backend) -> Client:
    return Client(backend, proposal='p1', submitter='anna')


def test_calls_through_a_stage_are_staged_once(
    client: Client, shifting: StagedFunction
) -> None:
    load = client.submit(LOAD, {'sample': {'file': dataset(run=1)}})
    shift = client.stage(
        Template(
            SHIFT, params={'source': {'value': load.ref('value')}}, blanks=('shift',)
        )
    )
    shifted = [client.compute(shift, {'shift': {'offset': x}}) for x in (0.5, 1.5)]
    plain = client.compute(
        SHIFT, {'source': {'value': load.ref('value')}, 'shift': {'offset': 1.5}}
    )

    assert [client.output(r, 'value') for r in shifted] == [1.5, 2.5]
    assert shifted[1].request == plain.request
    assert shifting.staged == [('shift',)]  # the plain request makes no stage


def test_concurrent_calls_through_a_stage_stage_it_once(
    client: Client, shifting: StagedFunction, loading: threading.Event
) -> None:
    loading.clear()
    load = client.submit(LOAD, {'sample': {'file': dataset(run=1)}})
    shift = client.stage(
        Template(
            SHIFT, params={'source': {'value': load.ref('value')}}, blanks=('shift',)
        )
    )
    pending = [
        client.submit(shift, {'shift': {'offset': x}}) for x in (1.0, 2.0, 3.0, 4.0)
    ]
    loading.set()
    client.wait(pending)

    assert [client.output(r, 'value') for r in pending] == [2.0, 3.0, 4.0, 5.0]
    assert shifting.staged == [('shift',)]


def test_a_released_stage_takes_no_calls_and_runs_those_made_before(
    client: Client, shifting: StagedFunction, loading: threading.Event
) -> None:
    loading.clear()
    load = client.submit(LOAD, {'sample': {'file': dataset(run=2)}})
    shift = client.stage(
        Template(
            SHIFT, params={'source': {'value': load.ref('value')}}, blanks=('shift',)
        )
    )
    pending = client.submit(shift, {'shift': {'offset': 1.0}})
    client.release(shift)
    loading.set()

    with pytest.raises(SubmitError, match='the stage was released'):
        client.submit(shift, {'shift': {'offset': 2.0}})
    assert client.wait(pending) is Status.COMPLETED
    assert client.output(pending, 'value') == 3.0
    assert shifting.staged == [('shift',)]


def test_a_stage_keeps_what_its_template_references_until_it_has_staged(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    load = client.submit(LOAD, {'sample': {'file': dataset(run=2)}})
    shift = client.stage(
        Template(
            SHIFT, params={'source': {'value': load.ref('value')}}, blanks=('shift',)
        )
    )
    client.release(load)
    loading.set()

    shifted = [client.compute(shift, {'shift': {'offset': x}}) for x in (1.0, 2.0)]
    assert [client.output(r, 'value') for r in shifted] == [3.0, 4.0]
    with pytest.raises(SubmitError, match='not kept by this client'):
        client.submit(
            SHIFT, {'source': {'value': load.ref('value')}, 'shift': {'offset': 1.0}}
        )


class KeepsTheSum:
    """Stages SHIFT keeping only the sum of the value, not the value."""

    spec = SHIFT

    def compute(self, params: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.make_stage({'source': params['source']}, ('shift',))(params)

    def make_stage(
        self, fixed: Mapping[str, Any], blanks: Collection[str]
    ) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
        total = float(np.sum(fixed['source']['value']))
        return lambda values: {'value': total + values['shift']['offset']}


def test_a_stage_lets_go_of_what_its_template_references_once_staged(
    datasets: FakeDatasets,
) -> None:
    def load(sample: dict[str, float]) -> dict[str, np.ndarray]:
        run = sample['file']
        return {'value': np.array([run]), 'extra': np.array([-run])}

    bind = [function_binding(LOAD, load), KeepsTheSum()]
    with local(proposal='p1', datasets=datasets, bind=bind) as client:
        loaded = client.compute(LOAD, {'sample': {'file': dataset(run=2)}})
        value = weakref.ref(client.output(loaded, 'value'))
        shift = client.stage(
            Template(
                SHIFT,
                params={'source': {'value': loaded.ref('value')}},
                blanks=('shift',),
            )
        )
        client.release(loaded)
        assert value() is not None

        shifted = client.compute(shift, {'shift': {'offset': 1.0}})
        assert value() is None
        again = client.compute(shift, {'shift': {'offset': 2.0}})
        assert [client.output(r, 'value') for r in (shifted, again)] == [3.0, 4.0]


def test_releasing_a_stage_lets_go_of_what_its_template_references(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    load = client.submit(LOAD, {'sample': {'file': dataset(run=2)}})
    shift = client.stage(
        Template(
            SHIFT, params={'source': {'value': load.ref('value')}}, blanks=('shift',)
        )
    )
    client.release([load, shift])
    loading.set()

    assert client.status(load) is Status.CANCELLED


def test_a_stage_keeps_the_dataset_its_name_resolved_to_when_it_was_made(
    client: Client, scaling: StagedFunction, datasets: FakeDatasets
) -> None:
    run = datasets.resolve(dataset(run=1))
    scale = client.stage(
        Template(
            SCALE, params={'sample': {'file': dataset(run=1)}}, blanks=('scaling',)
        )
    )
    before = client.compute(scale, {'scaling': {'factor': 2.0}})
    datasets.correct(run, run=99)
    datasets.measure(1, 5.0, pid='again')
    after = client.compute(scale, {'scaling': {'factor': 2.0}})

    assert scale.template.params == {'sample': {'file': run}}
    assert after.request == before.request
    assert [client.output(r, 'value') for r in (before, after)] == [2.0, 2.0]
    assert scaling.staged == [('scaling',)]


def test_a_stage_whose_staging_fails_stops(
    client: Client, scaling: StagedFunction
) -> None:
    scaling.fail_next = True
    scale = client.stage(
        Template(
            SCALE, params={'sample': {'file': dataset(run=2)}}, blanks=('scaling',)
        )
    )
    failed = client.compute(scale, {'scaling': {'factor': 2.0}})

    assert client.status(failed) is Status.FAILED
    assert client.failure(failed) == 'staging failed'
    with pytest.raises(SubmitError, match='the stage stopped: staging failed'):
        client.submit(scale, {'scaling': {'factor': 2.0}})
    assert scaling.staged == []


def test_the_calls_waiting_for_a_stage_whose_staging_fails_fail_with_it(
    client: Client, shifting: StagedFunction, loading: threading.Event
) -> None:
    loading.clear()
    load = client.submit(LOAD, {'sample': {'file': dataset(run=1)}})
    shift = client.stage(
        Template(
            SHIFT, params={'source': {'value': load.ref('value')}}, blanks=('shift',)
        )
    )
    shifting.fail_next = True
    pending = [client.submit(shift, {'shift': {'offset': x}}) for x in (1.0, 2.0, 3.0)]
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
    (load,) = backend.submit(
        [Entry(Request(LOAD, {'sample': {'file': dataset(run=1)}}))], client=client
    )
    scale, template = backend.open_stage(
        Template(
            SCALE, params={'sample': {'file': dataset(run=1)}}, blanks=('scaling',)
        ),
        client=client,
    )
    refused = {
        'the stage holds scale': Request(
            SHIFT, {'source': {'value': load.ref('value')}}
        ),
        r"\['sample'\]: differ from the stage's values": Request(
            SCALE,
            {'sample': {'file': dataset(run=2)}, 'scaling': {'factor': 2.0}},
        ),
    }
    for reason, request in refused.items():
        with pytest.raises(SubmitError, match=reason):
            backend.submit([Entry(request, stage=scale)], client=client)

    (record,) = backend.submit(
        [Entry(template.fill({'scaling': {'factor': 2.0}}), stage=scale)], client=client
    )
    assert record.request.params['sample'] == template.params['sample']


def test_the_stages_and_accumulators_of_another_client_are_refused(
    backend: Backend, client: Client
) -> None:
    theirs = Client(backend, proposal='p1', submitter='bob')
    load = client.compute(LOAD, {'sample': {'file': dataset(run=1)}})
    scale = client.stage(
        Template(
            SCALE, params={'sample': {'file': dataset(run=1)}}, blanks=('scaling',)
        )
    )
    total = client.accumulator(Template(TOTAL, blanks=('parts',)))
    total.push('parts', [load.refs('value')])

    with pytest.raises(SubmitError, match='the stage was released or is unknown'):
        theirs.submit(scale, {'scaling': {'factor': 2.0}})
    with pytest.raises(SubmitError, match='the accumulator was released or is'):
        theirs.submit(SHIFT, {'source': {'value': total.ref('value')}})


def test_a_stage_refuses_a_template_that_a_request_would_refuse(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    cancelled = client.submit(LOAD, {'sample': {'file': dataset(run=1)}})
    client.cancel(cancelled)
    loading.set()
    released = client.compute(LOAD, {'sample': {'file': dataset(run=1)}})
    client.release(released)
    refused = {
        r"\['speed'\]: not parameters": Template(SCALE, blanks=('speed',)),
        r"\['scale'\]: not parameters": Template(
            SCALE, params={'scale': 2.0}, blanks=('scaling',)
        ),
        'sample: Field required': Template(SCALE, blanks=('scaling',)),
        'scaling.factor: Input should be a valid number': Template(
            SCALE, params={'scaling': {'factor': 'x'}}, blanks=('sample',)
        ),
        'sample: unknown dataset run:9': Template(
            SCALE, params={'sample': {'file': dataset(run=9)}}, blanks=('scaling',)
        ),
        f'record {cancelled.id} cancelled': Template(
            SHIFT,
            params={'source': {'value': cancelled.ref('value')}},
            blanks=('shift',),
        ),
        f'record {released.id} is not kept by this client': Template(
            SHIFT,
            params={'source': {'value': released.ref('value')}},
            blanks=('shift',),
        ),
    }
    for reason, template in refused.items():
        with pytest.raises(SubmitError, match=reason):
            client.stage(template)


def test_a_stage_needs_a_binding_that_makes_stages(client: Client) -> None:
    with pytest.raises(SubmitError, match=r'^total/v1: its binding makes no stages$'):
        client.stage(Template(TOTAL, blanks=('parts',)))


# What a client keeps


def test_a_released_value_is_not_kept_and_the_record_stays(client: Client) -> None:
    load = client.compute(LOAD, {'sample': {'file': dataset(run=1)}})
    client.release(load)

    not_kept = f'record {load.id} is not kept by this client'
    with pytest.raises(LookupError, match=not_kept):
        client.output(load, 'value')
    with pytest.raises(SubmitError, match=rf'^source\.value: {not_kept}$'):
        client.submit(SHIFT, {'source': {'value': load.ref('value')}})
    assert client.records() == [load]
    assert client.status(load) is Status.COMPLETED


def test_a_pending_request_reads_a_value_released_after_its_submission(
    client: Client, loading: threading.Event
) -> None:
    first = client.compute(LOAD, {'sample': {'file': dataset(run=1)}})
    loading.clear()
    second = client.submit(LOAD, {'sample': {'file': dataset(run=2)}})
    total = client.submit(TOTAL, {'parts': [first.refs('value'), second.refs('value')]})
    client.release(first)
    loading.set()

    assert client.output(total, 'value') == 3.0
    with pytest.raises(LookupError, match='not kept'):  # read, then dropped
        client.output(first, 'value')


def test_a_released_pending_record_that_nothing_reads_is_cancelled(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    load = client.submit(LOAD, {'sample': {'file': dataset(run=1)}})
    client.release(load)
    loading.set()

    assert client.status(load) is Status.CANCELLED
    assert client.failure(load) == 'nothing keeps its outputs'
    with pytest.raises(
        SubmitError, match=rf'^source\.value: record {load.id} cancelled$'
    ):
        client.submit(SHIFT, {'source': {'value': load.ref('value')}})


def test_a_released_pending_record_is_refused_and_still_runs_for_its_readers(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    load = client.submit(LOAD, {'sample': {'file': dataset(run=1)}})
    shifted = client.submit(
        SHIFT, {'source': {'value': load.ref('value')}, 'shift': {'offset': 1.0}}
    )
    client.release(load)

    not_kept = rf'^source\.value: record {load.id} is not kept by this client$'
    with pytest.raises(SubmitError, match=not_kept):  # however far it has run
        client.submit(SHIFT, {'source': {'value': load.ref('value')}})
    loading.set()
    assert client.output(shifted, 'value') == 2.0
    assert client.status(load) is Status.COMPLETED


def test_cancelling_work_that_nothing_keeps_passes_along_a_chain(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    load = client.submit(LOAD, {'sample': {'file': dataset(run=1)}})
    shifted = client.submit(SHIFT, {'source': {'value': load.ref('value')}})
    scaled = client.submit(SHIFT, {'source': {'value': shifted.ref('value')}})
    client.release([load, shifted])
    assert client.status([load, shifted]) == [Status.PENDING] * 2  # scaled reads them

    client.release(scaled)
    loading.set()
    assert client.status([load, shifted, scaled]) == [Status.CANCELLED] * 3


def test_cancelling_passes_along_a_chain_of_any_length(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    chain = [client.submit(LOAD, {'sample': {'file': dataset(run=1)}})]
    for _ in range(1000):
        chain.append(
            client.submit(SHIFT, {'source': {'value': chain[-1].ref('value')}})
        )
    client.release(chain)  # the last one releases the whole chain
    loading.set()

    assert set(client.status(chain)) == {Status.CANCELLED}


def test_a_client_reads_and_references_only_what_it_keeps(
    backend: Backend, client: Client, loading: threading.Event
) -> None:
    other = Client(backend, proposal='p1', submitter='bob')
    done = client.compute(LOAD, {'sample': {'file': dataset(run=1)}})
    loading.clear()
    pending = client.submit(LOAD, {'sample': {'file': dataset(run=2)}})

    for record in (done, pending):
        not_kept = f'record {record.id} is not kept by this client'
        with pytest.raises(LookupError, match=not_kept):
            other.output(record, 'value')
        with pytest.raises(SubmitError, match=rf'^source\.value: {not_kept}$'):
            other.submit(SHIFT, {'source': {'value': record.ref('value')}})
    loading.set()
    assert other.wait(pending) is Status.COMPLETED  # its status is the proposal's


def test_a_client_releases_only_what_it_keeps(backend: Backend, client: Client) -> None:
    load = client.compute(LOAD, {'sample': {'file': dataset(run=1)}})
    Client(backend, proposal='p1', submitter='bob').release(load)

    assert client.output(load, 'value') == 1.0


def test_every_call_of_a_closed_client_raises_client_ended(client: Client) -> None:
    load = client.compute(LOAD, {'sample': {'file': dataset(run=1)}})
    shift = client.stage(
        Template(
            SHIFT, params={'source': {'value': load.ref('value')}}, blanks=('shift',)
        )
    )
    total = client.accumulator(Template(TOTAL, blanks=('parts',)))
    total.push('parts', [load.refs('value')])
    client.close()

    calls = [
        lambda: client.submit(LOAD, {'sample': {'file': dataset(run=1)}}),
        lambda: client.submit(shift, {'shift': {'offset': 1.0}}),
        lambda: client.submit(SHIFT, {'source': {'value': total.ref('value')}}),
        lambda: total.push('parts', [load.refs('value')]),
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
    pending = client.submit(LOAD, {'sample': {'file': dataset(run=2)}})
    shifted = client.submit(SHIFT, {'source': {'value': pending.ref('value')}})
    client.close()
    loading.set()

    assert other.status([pending, shifted]) == [Status.CANCELLED] * 2


def test_a_with_block_closes_the_client(backend: Backend) -> None:
    with Client(backend, proposal='p1', submitter='anna') as client:
        load = client.compute(LOAD, {'sample': {'file': dataset(run=1)}})
    client.close()  # closing again does nothing

    with pytest.raises(ClientEnded):
        client.output(load, 'value')


def test_closing_a_local_client_closes_its_backend_once_its_workflows_return(
    datasets: FakeDatasets, loading: threading.Event
) -> None:
    started = threading.Event()
    loaded = []

    def load(sample: dict[str, float]) -> dict[str, Any]:
        run = sample['file']
        started.set()
        loading.wait(timeout=5)
        loaded.append(run)
        return {'value': run, 'extra': -run}

    loading.clear()
    with local(
        proposal='p1', datasets=datasets, bind=[function_binding(LOAD, load)]
    ) as client:
        client.submit(LOAD, {'sample': {'file': dataset(run=1)}})
        started.wait(timeout=5)
        threading.Timer(0.05, loading.set).start()

    assert loaded == [1.0]  # cancelled when the client closed, but it ran


# Accumulators


def _total(client: Client) -> Accumulator:
    """An accumulator of TOTAL, the sum of a table of values."""
    return client.accumulator(Template(TOTAL, blanks=('parts',)))


def _state(client: Client, reader: Record, field: str = 'value') -> Record:
    """The record of the state of an accumulator that ``reader`` reads in ``field``."""
    ref = reader.request.params['source'][field]
    (state,) = [r for r in client.records() if r.id == ref.record]
    return state


def test_a_reference_names_the_record_of_the_state_pinned_at_submission(
    client: Client,
) -> None:
    loads = [
        client.submit(LOAD, {'sample': {'file': dataset(run=n)}}) for n in (1, 2, 1)
    ]
    client.wait(loads)
    total = _total(client)
    total.push('parts', [loads[0].refs('value')])
    total.push('parts', [loads[1].refs('value')])
    first = client.compute(SHIFT, {'source': {'value': total.ref('value')}})
    total.push('parts', [loads[2].refs('value')])
    second = client.compute(SHIFT, {'source': {'value': total.ref('value')}})

    states = [_state(client, r) for r in (first, second)]
    assert [client.output(r, 'value') for r in (first, second)] == [3.0, 4.0]
    assert [r.request.params['source']['value'] for r in (first, second)] == [
        state.ref('value') for state in states
    ]
    assert [state.request for state in states] == [
        Request(TOTAL, {'parts': [x.refs('value') for x in loads[:n]]}) for n in (2, 3)
    ]
    assert client.provenance(first).records() == [states[0], *loads[:2]]
    assert client.provenance(second).records() == [states[1], *loads]


def test_a_submission_makes_one_record_of_each_state_its_requests_read(
    client: Client,
) -> None:
    load = client.compute(LOAD, {'sample': {'file': dataset(run=1)}})
    total = _total(client)
    total.push('parts', [load.refs('value')])
    reads = client.compute(
        {
            'a': Request(SHIFT, {'source': {'value': total.ref('value')}}),
            'b': Request(
                SHIFT,
                {'source': {'value': total.ref('value')}, 'shift': {'offset': 1.0}},
            ),
        },
        label='shifted',
    )
    state = _state(client, reads['a'])

    assert client.records() == [load, state, *reads.values()]
    assert {r.request.params['source']['value'] for r in reads.values()} == {
        state.ref('value')
    }
    assert state.request == Request(TOTAL, {'parts': [load.refs('value')]})
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
    offset = client.submit(LOAD, {'sample': {'file': dataset(run=2)}})
    threading.Timer(0.05, loading.set).start()
    fixed = {'offset': {'value': offset.ref('value')}}  # and the default scale
    total = client.accumulator(Template(SUM, params=fixed, blanks=('runs',)))
    for n in (1, 2):
        total.push('runs', [{'file': dataset(run=n)}])
    read = client.compute(SHIFT, {'source': {'value': total.ref('value')}})
    runs = [{'file': dataset(run=n)} for n in (1, 2)]
    plain = client.compute(SUM, {'runs': runs, **fixed})

    assert client.output(read, 'value') == client.output(plain, 'value') == 5.0
    opened = {'scale': {'factor': 1.0}, 'offset': {'value': 2.0}}
    assert scaled_sum.opened == [opened] * 2  # the accumulator and the plain request
    assert total.template == Template(SUM, params=fixed, blanks=('runs',))
    state = _state(client, read)
    assert state.request == plain.request
    assert client.provenance(read).records() == [state, offset]
    assert set(client.provenance(read).datasets()) == {
        datasets.resolve(r['file']) for r in runs
    } | {datasets.resolve(dataset(run=2))}  # the runs, and offset's run
    provenance = client.provenance(total)  # of the state after the pushes so far
    assert provenance == client.provenance(plain)
    assert provenance.records() == [offset]
    assert set(provenance.datasets()) == {datasets.resolve(r['file']) for r in runs}


def test_a_push_may_take_an_output_named_unlike_the_field(client: Client) -> None:
    loads = [
        client.submit(LOAD, {'sample': {'file': {'dataset': f'run:{n}'}}})
        for n in (1, 2)
    ]
    total = _total(client)
    for load in loads:
        total.push('parts', [{'value': load.ref('extra')}])
    read = client.compute(SHIFT, {'source': {'value': total.ref('value')}})

    assert client.output(read, 'value') == -3.0


def test_an_accumulator_may_output_other_fields_than_it_takes(client: Client) -> None:
    loads = [client.submit(LOAD, {'sample': {'file': dataset(run=n)}}) for n in (1, 2)]
    mean = client.accumulator(Template(MEAN, blanks=('parts',)))
    for load in loads:
        mean.push('parts', [load.refs('value')])
    read = client.compute(SHIFT, {'source': {'value': mean.ref('mean')}})
    plain = client.compute(MEAN, {'parts': [x.refs('value') for x in loads]})

    assert client.output(read, 'value') == client.output(plain, 'mean') == 1.5


def test_an_accumulator_opens_over_a_table_that_a_request_needs_rows_in(
    client: Client,
) -> None:
    load = client.compute(LOAD, {'sample': {'file': dataset(run=2)}})
    with pytest.raises(SubmitError, match='parts: List should have at least 1 item'):
        client.submit(MEAN, {'parts': []})
    mean = client.accumulator(Template(MEAN, blanks=('parts',)))
    mean.push('parts', [load.refs('value')])

    assert client.output(mean, 'mean') == 2.0


def test_a_table_that_needs_two_rows_takes_them_one_push_at_a_time(
    client: Client,
) -> None:
    loads = [client.compute(LOAD, {'sample': {'file': dataset(run=n)}}) for n in (1, 2)]
    with pytest.raises(SubmitError, match='at least 2 items') as refused:
        client.submit(MEAN_OF_TWO, {'parts': [loads[0].refs('value')]})
    mean = client.accumulator(Template(MEAN_OF_TWO, blanks=('parts',)))
    mean.push('parts', [loads[0].refs('value')])

    with pytest.raises(LookupError, match=f'^{re.escape(str(refused.value))}$'):
        client.output(mean, 'mean')
    mean.push('parts', [loads[1].refs('value')])
    assert client.output(mean, 'mean') == 1.5


def test_a_row_equal_to_an_earlier_one_is_refused_at_the_push(client: Client) -> None:
    total = client.accumulator(Template(SUM, blanks=('runs',)))
    total.push('runs', [{'file': dataset(run=1)}])

    with pytest.raises(SubmitError, match=r'runs\[1\]: repeats runs\[0\]'):
        total.push('runs', [{'file': dataset(run=1)}])
    total.push('runs', [{'file': dataset(run=2)}])
    assert client.output(total, 'value') == 3.0


def test_a_dataset_is_the_same_value_by_any_name_that_resolves_to_it(
    client: Client,
) -> None:
    total = client.accumulator(Template(SUM, blanks=('runs',)))
    total.push('runs', [{'file': dataset(run=1)}])

    with pytest.raises(SubmitError, match=r'repeats'):
        total.push('runs', [{'file': {'dataset': 'run:1'}}])


def test_a_row_is_equal_only_if_every_field_is(client: Client) -> None:
    load = client.compute(LOAD, {'sample': {'file': dataset(run=1)}})
    rows = client.accumulator(Template(WEIGHTED, blanks=('rows',)))
    rows.push('rows', [{'value': load.ref('value'), 'weight': 1.0}])
    rows.push('rows', [{'value': load.ref('value'), 'weight': 2.0}])  # same data

    with pytest.raises(SubmitError, match=r'repeats'):
        rows.push('rows', [{'value': load.ref('value'), 'weight': 2.0}])


def test_a_plain_request_with_a_repeated_row_is_refused(client: Client) -> None:
    run = {'file': dataset(run=1)}

    with pytest.raises(SubmitError, match=r'runs\[1\]: repeats runs\[0\]'):
        client.submit(SUM, {'runs': [run, run]})


def test_a_template_that_fixes_a_repeated_row_is_refused(
    in_place: Client, loads: list[Record]
) -> None:
    client = in_place
    part = loads[0].refs('value')
    fixed = {'backgrounds': [part, part]}

    with pytest.raises(SubmitError, match=r'backgrounds\[1\]: repeats'):
        client.accumulator(Template(SIDES, params=fixed, blanks=('samples',)))


def test_a_table_may_allow_a_repeated_row(client: Client) -> None:
    run = {'file': dataset(run=1)}
    total = client.accumulator(Template(REPEATABLE, blanks=('runs',)))
    total.push('runs', [run])
    total.push('runs', [run])

    plain = client.compute(REPEATABLE, {'runs': [run, run]})
    assert client.output(total, 'value') == client.output(plain, 'value') == 2.0


def test_a_field_validator_gives_the_driver_the_plain_request_s_values(
    client: Client, scaled_sum: ScaledSum
) -> None:
    positive = client.accumulator(
        Template(POSITIVE, params={'scale': {'factor': -2.0}}, blanks=('runs',))
    )
    positive.push('runs', [{'file': dataset(run=1)}])
    plain = client.compute(
        POSITIVE, {'runs': [{'file': dataset(run=1)}], 'scale': {'factor': -2.0}}
    )

    assert client.output(positive, 'value') == client.output(plain, 'value') == 2.0
    assert plain.request.params['scale'] == {
        'factor': 2.0
    }  # the validator makes it positive
    # the drivers of the accumulator and of the plain request
    assert scaled_sum.opened == [{'scale': {'factor': 2.0}, 'offset': None}] * 2


def test_field_validators_apply_at_the_opening_and_model_validators_at_reads(
    client: Client, scaled_sum: ScaledSum
) -> None:
    mean = client.accumulator(
        Template(MEAN_SUM, params={'scale': {'factor': -2.0}}, blanks=('runs',))
    )
    with pytest.raises(SubmitError, match='a mean needs runs') as refused:
        client.submit(MEAN_SUM, {'runs': [], 'scale': {'factor': -2.0}})
    with pytest.raises(LookupError, match=f'^{re.escape(str(refused.value))}$'):
        client.output(mean)
    mean.push('runs', [{'file': dataset(run=1)}])

    assert client.output(mean, 'value') == 2.0  # once the driver is made
    assert scaled_sum.opened == [{'scale': {'factor': 2.0}, 'offset': None}]


class FaultyParams(BaseModel):
    """A table whose model validator has a bug: it raises at two rows."""

    parts: list[Parts]

    @model_validator(mode='after')
    def _faulty(self) -> Self:
        if len(self.parts) == 2:
            raise TypeError('a bug in a validator')
        return self


FAULTY = _spec('faulty', FaultyParams, Parts)


def test_a_read_whose_validator_raises_holds_back_no_push(
    datasets: FakeDatasets,
) -> None:
    bind = [
        function_binding(LOAD, lambda sample: {'value': sample['file'], 'extra': 0.0}),
        folding(FAULTY, operator.add),
    ]
    datasets.measure(3, 3.0)
    backend = Backend(datasets, bind)
    client = Client(backend, proposal='p1', submitter='anna')
    loads = [
        client.compute(LOAD, {'sample': {'file': dataset(run=n)}}) for n in (1, 2, 3)
    ]
    faulty = client.accumulator(Template(FAULTY, blanks=('parts',)))
    for load in loads[:2]:
        faulty.push('parts', [load.refs('value')])
    with pytest.raises(TypeError, match='a bug in a validator'):
        client.output(faulty, 'value')
    faulty.push('parts', [loads[2].refs('value')])

    assert _start(lambda: client.output(faulty, 'value')).result(timeout=5) == 6.0
    backend.close()  # not before: a reader left behind would hold it open


def test_an_accumulator_s_values_are_typed_as_the_log_holds_them(
    client: Client, scaled_sum: ScaledSum
) -> None:
    tagged = client.accumulator(
        Template(TAGGED, params={'labels': {'tags': ('a', 'b')}}, blanks=('runs',))
    )
    tagged.push('runs', [{'file': dataset(run=1)}])

    assert client.output(tagged, 'value') == 1.0
    assert tagged.template.params == {
        'labels': {'tags': ['a', 'b']}
    }  # JSON has no tuple
    assert scaled_sum.opened == [
        {'scale': {'factor': 1.0}, 'offset': None, 'labels': {'tags': ['a', 'b']}}
    ]


def test_a_pushed_row_reaches_the_driver_as_the_plain_request_holds_it(
    client: Client, scaled_sum: ScaledSum
) -> None:
    tagged = client.accumulator(Template(TAGGED, blanks=('runs',)))
    tagged.push('runs', [{'file': dataset(run=1), 'tag': ('a', 'b')}])
    read = client.compute(SHIFT, {'source': {'value': tagged.ref('value')}})

    assert client.output(read, 'value') == 1.0
    (row,) = _state(client, read).request.params['runs']
    assert row['tag'] == ['a', 'b']  # JSON has no tuple
    assert [pushed['tag'] for pushed in scaled_sum.held[0].rows] == [row['tag']]


def test_an_optional_output_left_out_is_not_read(client: Client) -> None:
    parts = [
        client.compute(LOAD, {'sample': {'file': dataset(run=n)}}).refs('value')
        for n in (1, 2)
    ]
    record = client.compute(SPREAD, {'parts': parts})
    spread = client.accumulator(Template(SPREAD, blanks=('parts',)))
    for part in parts:
        spread.push('parts', [part])
    reading = client.compute(SHIFT, {'source': {'value': spread.ref('spread')}})

    assert client.output(record) == client.output(spread) == {'mean': 1.5}
    for read in (
        lambda: client.output(record, 'spread'),
        lambda: client.output(spread, 'spread'),
    ):
        with pytest.raises(LookupError, match='the workflow did not return it'):
            read()
    left_out = f'record {record.id} output spread: the workflow did not return it'
    with pytest.raises(SubmitError, match=rf'^source\.value: {left_out}$'):
        client.submit(SHIFT, {'source': {'value': record.ref('spread')}})
    state = _state(client, reading)
    assert client.failure(reading) == (
        f'record {state.id} output spread: the workflow did not return it'
    )


def test_a_state_with_nothing_pushed_is_read_as_the_plain_request_over_no_rows(
    client: Client,
) -> None:
    summed = client.accumulator(Template(SUM, blanks=('runs',)))
    mean = client.accumulator(Template(MEAN, blanks=('parts',)))
    total = _total(client)
    plain = client.compute(SUM, {'runs': []})
    with pytest.raises(SubmitError, match='at least 1 item') as refused:
        client.submit(MEAN, {'parts': []})

    assert client.output(summed, 'value') == client.output(plain, 'value') == 0.0
    assert client.provenance(summed).request == plain.request
    with pytest.raises(LookupError, match=f'^{re.escape(str(refused.value))}$'):
        client.output(mean)
    missing = r"^the driver of total/v1 returned \[\]: missing \['value'\]$"
    with pytest.raises(ValueError, match=missing):  # the fold has nothing to fold
        client.output(total, 'value')


def test_every_output_of_a_record_or_an_accumulator_is_read_without_a_name(
    client: Client,
) -> None:
    load = client.compute(LOAD, {'sample': {'file': dataset(run=1)}})
    total = _total(client)
    total.push('parts', [load.refs('value')])
    shifted = client.compute(SHIFT, {'source': total.refs()})

    assert client.output(load) == {'value': 1.0, 'extra': -1.0}
    assert client.output(total) == {'value': 1.0}
    assert total.refs() == {'value': total.ref('value')}
    assert shifted.request.params['source']['value'] == _state(client, shifted).ref(
        'value'
    )


def test_an_accumulator_is_read_through_a_reference(client: Client) -> None:
    with pytest.raises(TypeError, match=r'accumulator\.ref\(output\)'):
        client.submit(_total(client))


def test_the_blanks_of_an_accumulator_are_table_fields(client: Client) -> None:
    refused = {
        r"are table fields of \['runs'\], not \[\]$": Template(SUM),
        r"not \['scale'\]$": Template(SUM, blanks=('scale',)),
        r"not \['runs', 'scale'\]$": Template(SUM, blanks=('runs', 'scale')),
        r"of \['high', 'low'\], not \[\]$": Template(STITCH),
        r"not \['reference'\]$": Template(STITCH, blanks=('reference',)),
    }
    for reason, template in refused.items():
        with pytest.raises(SubmitError, match=reason):
            client.accumulator(template)


def test_an_accumulator_needs_a_binding_that_makes_accumulators(
    client: Client,
) -> None:
    refused = r'^pairs/v1: its binding makes no accumulators$'
    with pytest.raises(SubmitError, match=refused):
        client.accumulator(Template(PAIRS, blanks=('pairs',)))


def test_an_accumulator_of_a_plain_function_reads_as_the_plain_request(
    client: Client,
) -> None:
    loads = [client.compute(LOAD, {'sample': {'file': dataset(run=n)}}) for n in (1, 2)]
    stitched = client.accumulator(
        Template(STITCH, params={'reference': {'total': 30.0}}, blanks=('low', 'high'))
    )
    pushes = [
        ('low', loads[0].refs('value')),
        ('high', loads[1].refs('value')),
        ('low', loads[1].refs('value')),
        ('high', loads[0].refs('value')),
    ]
    reads, plains = [], []
    for n, (table, row) in enumerate(pushes, start=1):
        stitched.push(table, [row])
        tables = {t: [r for u, r in pushes[:n] if u == t] for t in ('low', 'high')}
        plain = client.compute(STITCH, {'reference': {'total': 30.0}, **tables})
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
    cancelled = client.submit(LOAD, {'sample': {'file': dataset(run=1)}})
    client.cancel(cancelled)
    loading.set()
    released = client.compute(LOAD, {'sample': {'file': dataset(run=1)}})
    client.release(released)
    refused = {
        r"\['speed'\]: not parameters": {'speed': 2.0},
        'scale.factor: Input should be a valid number': {'scale': {'factor': 'x'}},
        f'record {cancelled.id} cancelled': {
            'offset': {'value': cancelled.ref('value')}
        },
        f'record {released.id} is not kept by this client': {
            'offset': {'value': released.ref('value')}
        },
    }
    for reason, params in refused.items():
        with pytest.raises(SubmitError, match=reason):
            client.accumulator(Template(SUM, params=params, blanks=('runs',)))


def test_a_push_of_more_fields_than_the_row_is_refused(client: Client) -> None:
    load = client.submit(LOAD, {'sample': {'file': dataset(run=1)}})
    total = _total(client)
    with pytest.raises(SubmitError, match='fields'):
        total.push('parts', [load.refs()])  # 'value' and 'extra'


def test_a_push_takes_values_and_defaults_as_the_request_does(client: Client) -> None:
    loads = [client.submit(LOAD, {'sample': {'file': dataset(run=n)}}) for n in (1, 2)]
    rows = [
        {'value': loads[0].ref('value'), 'weight': 3.0},
        {'value': loads[1].ref('value')},  # the default weight
    ]
    weighted = client.accumulator(Template(WEIGHTED, blanks=('rows',)))
    for row in rows:
        weighted.push('rows', [row])
    read = client.compute(SHIFT, {'source': {'value': weighted.ref('value')}})
    plain = client.compute(WEIGHTED, {'rows': rows})

    assert client.output(read, 'value') == client.output(plain, 'value') == 5.0
    state = _state(client, read)
    assert state.request == plain.request
    assert client.provenance(read).records() == [state, *loads]


def test_a_push_is_refused_as_the_request_over_it_alone(client: Client) -> None:
    load = client.compute(LOAD, {'sample': {'file': dataset(run=1)}})
    rows = [
        {'weight': 2.0},  # a required field left out
        {'value': load.ref('value'), 'scale': 2.0},  # a field the row lacks
        {'value': 1.0},  # a value where a reference goes
    ]
    weighted = client.accumulator(Template(WEIGHTED, blanks=('rows',)))
    for row in rows:
        with pytest.raises(SubmitError) as refused:
            client.submit(WEIGHTED, {'rows': [row]})
        with pytest.raises(SubmitError) as pushed:
            weighted.push('rows', [row])
        assert str(pushed.value) == str(refused.value)


def test_a_row_refused_in_a_push_is_located_by_its_index_in_the_push(
    client: Client,
) -> None:
    load = client.compute(LOAD, {'sample': {'file': dataset(run=1)}})
    rows = [{'value': load.ref('value')}, {'weight': 2.0}]
    weighted = client.accumulator(Template(WEIGHTED, blanks=('rows',)))
    with pytest.raises(SubmitError, match=r'rows\.1\.value') as refused:
        client.submit(WEIGHTED, {'rows': rows})
    with pytest.raises(SubmitError) as pushed:
        weighted.push('rows', rows)

    assert str(pushed.value) == str(refused.value)


class Files(BaseModel):
    value: OpaqueFile


class FilesTable(BaseModel):
    files: list[Files]


FILES_SUM = _spec('files-sum', FilesTable, Files)


def test_a_reference_in_a_row_must_fit_its_field(client: Client) -> None:
    load = client.submit(LOAD, {'sample': {'file': {'dataset': 'run:1'}}})

    with pytest.raises(SubmitError, match='does not fit'):
        client.submit(FILES_SUM, {'files': [{'value': load.ref('value')}]})


def test_a_reference_in_a_group_must_fit_its_field(client: Client) -> None:
    files = client.submit(FILES_SUM, {'files': [{'value': dataset(run=1)}]})

    with pytest.raises(SubmitError, match=r'^source\.value: .* does not fit'):
        client.submit(SHIFT, {'source': {'value': files.ref('value')}})


def test_each_row_of_a_table_needs_every_field(client: Client) -> None:
    loads = [
        client.submit(LOAD, {'sample': {'file': {'dataset': f'run:{n}'}}})
        for n in (1, 2)
    ]

    with pytest.raises(SubmitError, match=r'pairs\.1\.extra: Field required'):
        client.submit(PAIRS, {'pairs': [loads[0].refs(), loads[1].refs('value')]})


def test_a_params_model_makes_the_request_of_its_dict() -> None:
    by_model = Request(SCALE, ScaleParams(sample=Run(file=dataset(run=1))))
    by_dict = Request(SCALE, {'sample': {'file': dataset(run=1)}})

    assert by_model == by_dict  # defaults left to the backend's spec


def test_a_template_and_its_calls_take_models(client: Client) -> None:
    load = client.submit(LOAD, LoadParams(sample=Run(file=dataset(run=1))))
    params = ShiftParams(source=Parts(value=load.ref('value')))
    stage = client.stage(Template(SHIFT, params=params, blanks=('shift',)))
    shifted = client.compute(stage, {'shift': Shift(offset=0.5)})

    assert client.output(shifted, 'value') == 1.5


def test_a_push_takes_a_row_model(client: Client) -> None:
    loads = [client.submit(LOAD, {'sample': {'file': dataset(run=n)}}) for n in (1, 2)]
    mean = client.accumulator(Template(MEAN, blanks=('parts',)))
    for load in loads:
        mean.push('parts', [Parts(value=load.ref('value'))])

    assert client.output(mean, 'mean') == 1.5


def test_a_group_with_a_field_its_model_lacks_is_refused(client: Client) -> None:
    with pytest.raises(SubmitError, match=r"sample: \['speed'\]: not fields of"):
        client.submit(LOAD, {'sample': {'file': dataset(run=1), 'speed': 2.0}})


def test_a_row_that_does_not_fit_is_refused_at_the_push(client: Client) -> None:
    load = client.submit(LOAD, {'sample': {'file': dataset(run=1)}})
    files = client.accumulator(Template(FILES_SUM, blanks=('files',)))
    with pytest.raises(SubmitError, match='does not fit'):
        files.push('files', [{'value': load.ref('value')}])


def test_a_reference_to_an_accumulator_is_checked_as_one_to_a_record(
    backend: Backend, client: Client
) -> None:
    total = _total(client)
    load = client.compute(LOAD, {'sample': {'file': dataset(run=1)}})
    total.push('parts', [load.refs('value')])
    elsewhere = Client(backend, proposal='p2', submitter='carl')
    extra = AccumulatorRef(accumulator=total.id, output='extra')

    for read in (lambda: total.ref('extra'), lambda: client.output(total, 'extra')):
        with pytest.raises(KeyError, match="total/v1 has no output 'extra'"):
            read()
    with pytest.raises(
        SubmitError, match=r"^source\.value: total/v1 has no output 'extra'$"
    ):
        client.submit(SHIFT, {'source': {'value': extra}})
    with pytest.raises(SubmitError, match=r'^files\[0\]\.value: .* does not fit'):
        client.submit(FILES_SUM, {'files': [{'value': total.ref('value')}]})
    with pytest.raises(
        SubmitError, match=r'^source\.value: the accumulator was released or'
    ):
        elsewhere.submit(SHIFT, {'source': {'value': total.ref('value')}})


def test_only_a_request_reads_an_accumulator_and_no_client_its_state(
    client: Client, loading: threading.Event
) -> None:
    load = client.compute(LOAD, {'sample': {'file': dataset(run=1)}})
    total, other = _total(client), _total(client)
    total.push('parts', [load.refs('value')])
    loading.clear()
    pending = client.submit(LOAD, {'sample': {'file': dataset(run=2)}})
    parts = [{'value': total.ref('value')}, pending.refs('value')]
    read = client.submit(TOTAL, {'parts': parts})  # holds the state until it runs
    state = read.request.params['parts'][0]['value']

    refusals = {
        total.ref('value'): 'only a request may reference an accumulator',
        state: f'record {state.record} is not kept by this client',
    }
    for ref, refused in refusals.items():
        for accumulator in (total, other):
            with pytest.raises(SubmitError, match=rf'^parts\[0\]\.value: {refused}'):
                accumulator.push('parts', [{'value': ref}])
        with pytest.raises(SubmitError, match=rf'^source\.value: {refused}'):
            client.stage(
                Template(SHIFT, params={'source': {'value': ref}}, blanks=('shift',))
            )
        with pytest.raises(SubmitError, match=rf'^offset\.value: {refused}'):
            client.accumulator(
                Template(SUM, params={'offset': {'value': ref}}, blanks=('runs',))
            )
    later = rf'^source\.value: {refusals[state]}'
    with pytest.raises(SubmitError, match=later):
        client.submit(SHIFT, {'source': {'value': state}})
    loading.set()
    assert client.output(read, 'value') == 3.0


def test_a_push_takes_a_pending_record_and_refuses_one_that_did_not_complete(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    pending, cancelled = (
        client.submit(LOAD, {'sample': {'file': dataset(run=n)}}) for n in (1, 2)
    )
    client.cancel(cancelled)
    total = _total(client)
    with pytest.raises(SubmitError, match=f'{cancelled.id} cancelled'):
        total.push('parts', [cancelled.refs('value')])
    total.push('parts', [pending.refs('value')])  # added once pending completes
    read = client.submit(SHIFT, {'source': {'value': total.ref('value')}})
    loading.set()

    state = _state(client, read)
    assert [r.id for r in client.provenance(read).records()] == [state.id, pending.id]
    assert client.output(read, 'value') == 1.0


def test_a_record_that_a_push_waits_for_and_that_fails_stops_the_accumulator(
    client: Client, loading: threading.Event
) -> None:
    loading.clear()
    pending = client.submit(LOAD, {'sample': {'file': dataset(run=1)}})
    total = _total(client)
    total.push('parts', [pending.refs('value')])
    read = client.submit(SHIFT, {'source': {'value': total.ref('value')}})
    client.cancel(pending)
    loading.set()

    stopped = f'the accumulator stopped: push 0: input {pending.id} cancelled'
    state = _state(client, read)
    assert client.wait(read) is Status.FAILED
    assert client.failure([state, read]) == [stopped, f'input {state.id} failed']
    with pytest.raises(SubmitError, match=f'^{stopped}$'):
        total.push('parts', [pending.refs('value')])


def test_concurrent_pushes_add_in_the_order_they_are_logged(
    client: Client, datasets: FakeDatasets
) -> None:
    runs = [datasets.measure(n, float(n)) for n in range(3, 10)]
    loads = [client.submit(LOAD, {'sample': {'file': run}}) for run in runs]
    client.wait(loads)
    digits = client.accumulator(Template(DIGITS, blanks=('parts',)))
    pushes = [
        threading.Thread(target=digits.push, args=('parts', [x.refs('value')]))
        for x in loads
    ]
    for push in pushes:
        push.start()
    for push in pushes:
        push.join()
    read = client.compute(SHIFT, {'source': {'value': digits.ref('value')}})
    pushed = _state(client, read).request.params  # in the order they were logged
    plain = client.compute(DIGITS, pushed)

    assert client.output(read, 'value') == client.output(plain, 'value')


def test_a_push_is_added_after_a_request_that_has_not_started(
    client: Client, loading: threading.Event
) -> None:
    loads = [client.compute(LOAD, {'sample': {'file': dataset(run=n)}}) for n in (1, 2)]
    total = _total(client)
    total.push('parts', [loads[0].refs('value')])
    loading.clear()
    pending = client.submit(LOAD, {'sample': {'file': dataset(run=2)}})
    parts = [{'value': total.ref('value')}, pending.refs('value')]
    summed = client.submit(TOTAL, {'parts': parts})  # starts once pending completes
    total.push('parts', [loads[1].refs('value')])  # returns at once
    read = _start(lambda: client.output(total, 'value'))

    with pytest.raises(TimeoutError):
        read.result(timeout=0.05)  # the push waits for summed
    loading.set()
    assert read.result(timeout=5) == 3.0
    assert client.output(summed, 'value') == 3.0  # read 1.0 before the push


def test_cancelling_a_request_that_has_not_started_lets_the_push_be_added(
    client: Client, loading: threading.Event
) -> None:
    loads = [client.compute(LOAD, {'sample': {'file': dataset(run=n)}}) for n in (1, 2)]
    total = _total(client)
    total.push('parts', [loads[0].refs('value')])
    loading.clear()
    pending = client.submit(LOAD, {'sample': {'file': dataset(run=2)}})
    parts = [{'value': total.ref('value')}, pending.refs('value')]
    summed = client.submit(TOTAL, {'parts': parts})
    total.push('parts', [loads[1].refs('value')])
    read = _start(lambda: client.output(total, 'value'))

    with pytest.raises(TimeoutError):
        read.result(timeout=0.05)
    client.cancel(summed)
    assert read.result(timeout=5) == 3.0
    assert client.status([pending, summed]) == [Status.PENDING, Status.CANCELLED]
    loading.set()


class Sum:
    """
    Makes drivers that sum like ``Fold(operator.add)``, and counts the pushes
    they add.

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
        self._fold = Fold(operator.add)

    def push(self, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
        self._owner.adding.set()
        self._owner.go.wait(timeout=5)
        if self._owner.failing:
            self._owner.failing = False
            raise ValueError('cannot add')
        self._owner.pushed += 1
        self._fold.push(table, rows)

    def outputs(self) -> Mapping[str, Any]:
        return self._fold.outputs()


@pytest.fixture
def summing() -> Sum:
    return Sum()


@pytest.fixture
def log() -> Log:
    return Log()


@pytest.fixture
def summed_backend(datasets: FakeDatasets, summing: Sum, log: Log) -> Iterator[Backend]:
    """A backend that sums TOTAL with ``summing`` and logs to ``log``."""
    backend = Backend(
        datasets,
        [
            function_binding(
                LOAD, lambda sample: {'value': sample['file'], 'extra': 0.0}
            ),
            function_binding(SHIFT, _shift),
            DriverBinding(TOTAL, summing),
        ],
        log=log,
    )
    yield backend
    backend.close()


@pytest.fixture
def summed(summed_backend: Backend) -> Client:
    """A client of ``summed_backend``."""
    return Client(summed_backend, proposal='p1', submitter='anna')


def test_an_accumulator_adds_each_row_once(summed: Client, summing: Sum) -> None:
    client = summed
    total = _total(client)
    values = []
    for n in (1, 2, 1, 2):
        load = client.compute(LOAD, {'sample': {'file': dataset(run=n)}})
        total.push('parts', [load.refs('value')])
        read = client.submit(SHIFT, {'source': {'value': total.ref('value')}})
        values.append(client.output(read, 'value'))

    assert values == [1.0, 3.0, 4.0, 6.0]
    assert summing.pushed == 4


def test_a_push_that_fails_to_add_stops_the_accumulator(
    summed: Client, summing: Sum
) -> None:
    client = summed
    loads = [
        client.compute(LOAD, {'sample': {'file': dataset(run=n)}}) for n in (1, 2, 1)
    ]
    total = _total(client)
    total.push('parts', [loads[0].refs('value')])
    before = client.compute(SHIFT, {'source': {'value': total.ref('value')}})
    summing.failing = True
    summing.go.clear()
    total.push('parts', [loads[1].refs('value')])  # logged, and fails once it adds
    after = client.submit(SHIFT, {'source': {'value': total.ref('value')}})
    summing.go.set()

    stopped = 'the accumulator stopped: push 1 failed: cannot add'
    state = _state(client, after)
    assert client.wait(after) is Status.FAILED
    assert client.failure([state, after]) == [stopped, f'input {state.id} failed']
    assert len(state.request.params['parts']) == 2  # the push is in the log
    with pytest.raises(SubmitError, match=f'^{stopped}$'):
        total.push('parts', [loads[2].refs('value')])
    with pytest.raises(SubmitError, match=rf'^source\.value: {stopped}$'):
        client.submit(
            SHIFT, {'source': {'value': total.ref('value')}}
        )  # it may be half added
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
        proposal='p1', datasets=datasets, bind=[DriverBinding(TOTAL, _refuse)]
    ) as client:
        total = _total(client)  # returns before the driver is made

        stopped = 'the accumulator stopped: opening failed: cannot open'
        with pytest.raises(LookupError, match=f'^{stopped}$'):
            client.output(total)
        with pytest.raises(SubmitError, match=f'^{stopped}$'):
            total.push('parts', [{'value': 1.0}])


def test_closing_a_client_drops_the_pushes_that_no_read_needs(
    summed_backend: Backend, summed: Client, summing: Sum
) -> None:
    client = summed
    loads = client.compute(
        [Request(LOAD, {'sample': {'file': dataset(run=n)}}) for n in (1, 2)]
    )
    summing.go.clear()
    total = _total(client)
    for load in loads:
        total.push('parts', [load.refs('value')])
    summing.adding.wait(timeout=5)
    client.close()
    summing.go.set()
    summed_backend.close()

    assert summing.pushed == 1  # the push being added when the client closed


def test_ending_a_client_cancels_the_reads_of_its_accumulator(
    summed_backend: Backend, summed: Client, summing: Sum
) -> None:
    client = summed
    other = Client(summed_backend, proposal='p1', submitter='bob')
    load = client.compute(LOAD, {'sample': {'file': dataset(run=1)}})
    summing.go.clear()
    total = _total(client)
    total.push('parts', [load.refs('value')])
    shifted = client.submit(SHIFT, {'source': {'value': total.ref('value')}})
    state = _state(client, shifted)
    client.close()
    summing.go.set()

    assert other.status([state, shifted]) == [Status.CANCELLED] * 2


def test_a_released_accumulator_drops_the_pushes_once_their_reader_is_cancelled(
    summed_backend: Backend, summed: Client, summing: Sum
) -> None:
    client = summed
    loads = client.compute(
        [Request(LOAD, {'sample': {'file': dataset(run=n)}}) for n in (1, 2, 1)]
    )
    summing.go.clear()
    total = _total(client)
    for load in loads:
        total.push('parts', [load.refs('value')])
    read = client.submit(
        SHIFT, {'source': {'value': total.ref('value')}}
    )  # pins three pushes
    summing.adding.wait(timeout=5)
    client.release(total)  # the read keeps the pushes
    client.release(read)  # and no longer once it is cancelled
    summing.go.set()
    summed_backend.close()

    assert client.status(read) is Status.CANCELLED
    assert summing.pushed == 1  # the push being added when the read was cancelled


def test_a_released_accumulator_adds_the_pushes_up_to_the_last_state_read(
    summed: Client, summing: Sum
) -> None:
    client = summed
    loads = client.compute(
        [Request(LOAD, {'sample': {'file': dataset(run=n)}}) for n in (1, 2, 1)]
    )
    summing.go.clear()
    total = _total(client)
    total.push('parts', [loads[0].refs('value')])
    total.push('parts', [loads[1].refs('value')])
    read = client.submit(SHIFT, {'source': {'value': total.ref('value')}})
    total.push('parts', [loads[2].refs('value')])
    client.release(total)
    summing.go.set()

    assert client.output(read, 'value') == 3.0
    assert summing.pushed == 2  # the last push was dropped


def test_an_accumulator_value_that_does_not_fit_the_spec_fails_the_reader(
    datasets: FakeDatasets,
) -> None:
    bind = [
        function_binding(LOAD, lambda sample: {'value': sample['file'], 'extra': 0.0}),
        function_binding(SHIFT, _shift),
        DriverBinding(TOTAL, Averaging),  # returns 'mean', which TOTAL lacks
    ]
    with local(proposal='p1', datasets=datasets, bind=bind) as client:
        total = _total(client)
        load = client.compute(LOAD, {'sample': {'file': dataset(run=1)}})
        total.push('parts', [load.refs('value')])
        read = client.compute(SHIFT, {'source': {'value': total.ref('value')}})
        state = _state(client, read)

        assert client.failure([state, read]) == [
            "the driver of total/v1 returned ['mean']: missing ['value']",
            f'input {state.id} failed',
        ]


# Accumulators that add in place


class ValueParams(BaseModel):
    source: Parts


COPY = _spec('copy', ValueParams, Parts)


class Copying:
    """
    COPY, which copies its value while ``go`` is set; ``started`` and
    ``finished`` say when it runs.
    """

    def __init__(self) -> None:
        self.started = threading.Event()
        self.finished = threading.Event()
        self.go = threading.Event()
        self.go.set()

    def __call__(self, source: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        self.started.set()
        self.go.wait(timeout=5)
        self.finished.set()
        return {'value': source['value'].copy()}


class SidesParams(BaseModel):
    samples: list[Parts]
    backgrounds: list[Parts]


class Sides(BaseModel):
    difference: Array()  # type: ignore[valid-type]
    total: Array()  # type: ignore[valid-type]


SIDES = _spec('sides', SidesParams, Sides)


class BothSidesParams(BaseModel):
    samples: list[Parts] = Field(min_length=1)
    backgrounds: list[Parts] = Field(min_length=1)


BOTH = _spec('both', BothSidesParams, Sides)


class Subtracting:
    """
    Makes the drivers of SIDES and BOTH, for accumulators and plain requests:
    the sum of the samples minus that of the backgrounds, and the sum of both.
    ``computed`` counts the ``outputs`` calls that computed. A push sets
    ``adding``, and adds while ``go`` is set. An ``outputs`` call sets
    ``computing``, and computes while ``compute`` is set; with ``failing``
    set, the next one fails. ``held`` refers weakly to each driver made, and
    ``returned`` to the difference that each ``outputs`` call returned, if an
    array.
    """

    def __init__(self) -> None:
        self.computed = 0
        self.adding = threading.Event()
        self.go = threading.Event()
        self.go.set()
        self.computing = threading.Event()
        self.compute = threading.Event()
        self.compute.set()
        self.failing = False
        self.held: list[weakref.ref[_Sides]] = []
        self.returned: list[weakref.ref[np.ndarray]] = []

    def __call__(self, fixed: Mapping[str, Any]) -> '_Sides':
        driver = _Sides(self)
        self.held.append(weakref.ref(driver))
        return driver


class _Sides:
    def __init__(self, owner: Subtracting) -> None:
        self._owner = owner
        self._sums: dict[str, Any] = {'samples': 0.0, 'backgrounds': 0.0}

    def push(self, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
        self._owner.adding.set()
        self._owner.go.wait(timeout=5)
        for row in rows:
            self._sums[table] += row['value']  # in place from the second row

    def outputs(self) -> Mapping[str, Any]:
        self._owner.computing.set()
        self._owner.compute.wait(timeout=5)
        if self._owner.failing:
            self._owner.failing = False
            raise ValueError('cannot compute')
        self._owner.computed += 1
        outputs = _subtract(**self._sums)
        if isinstance(outputs['difference'], np.ndarray):
            self._owner.returned.append(weakref.ref(outputs['difference']))
        return outputs


def _subtract(samples: Any, backgrounds: Any) -> dict[str, Any]:
    return {'difference': samples - backgrounds, 'total': samples + backgrounds}


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
    """A client of a backend whose TOTAL and SIDES add arrays in place."""

    def load(sample: dict[str, float]) -> dict[str, np.ndarray]:
        run = sample['file']
        return {'value': np.array([run]), 'extra': np.array([-run])}

    bind = [
        function_binding(LOAD, load),
        folding(TOTAL, operator.iadd),
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
    return [
        in_place.compute(LOAD, {'sample': {'file': dataset(run=n)}}) for n in (1, 2)
    ]


def _copy(client: Client, accumulator: Accumulator, output: str = 'value') -> Record:
    return client.submit(COPY, {'source': {'value': accumulator.ref(output)}})


def test_adding_in_place_leaves_the_outputs_pushed_unchanged(
    in_place: Client, loads: list[Record]
) -> None:
    client = in_place
    total = _total(client)
    for load in loads:
        total.push('parts', [load.refs('value')])
    plain = client.compute(TOTAL, {'parts': [x.refs('value') for x in loads]})

    assert client.output(_copy(client, total), 'value').tolist() == [3.0]
    assert client.output(plain, 'value').tolist() == [3.0]
    assert [client.output(x, 'value').tolist() for x in loads] == [[1.0], [2.0]]


def test_no_client_reads_or_references_the_record_of_a_state(
    in_place: Client, loads: list[Record], copying: Copying
) -> None:
    client = in_place
    copying.go.clear()
    total = _total(client)
    total.push('parts', [loads[0].refs('value')])
    copied = _copy(client, total)  # holds the state until it has run
    state = _state(client, copied)

    not_kept = f'record {state.id} is not kept by this client'
    with pytest.raises(LookupError, match=f'^{not_kept}$'):
        client.output(state, 'value')
    for refuse in (
        lambda: client.submit(COPY, {'source': {'value': state.ref('value')}}),
        lambda: client.stage(
            Template(COPY, params={'source': {'value': state.ref('value')}})
        ),
        lambda: total.push('parts', [{'value': state.ref('value')}]),
    ):
        with pytest.raises(SubmitError, match=f'value: {not_kept}$'):
            refuse()
    copying.go.set()
    total.push('parts', [loads[1].refs('value')])
    assert client.output(total, 'value').tolist() == [3.0]
    assert client.output(copied, 'value').tolist() == [1.0]


def test_cancelling_the_record_of_a_state_fails_its_readers_and_frees_the_push(
    in_place: Client,
    loads: list[Record],
    copying: Copying,
    subtracting: Subtracting,
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push('samples', [loads[0].refs('value')])
    subtracting.go.clear()
    sides.push('backgrounds', [loads[1].refs('value')])  # adds once go is set
    copying.go.clear()
    copied = _copy(client, sides, 'difference')  # its state waits for that push
    sides.push('samples', [loads[1].refs('value')])  # waits for the readers
    state = _state(client, copied)
    assert state in client.records()
    client.cancel(state)
    subtracting.go.set()

    read = _start(lambda: client.output(sides, 'difference'))
    assert read.result(timeout=2).tolist() == [1.0]  # copying.go is still clear
    assert client.failure(copied) == f'input {state.id} cancelled'


def test_two_submissions_that_read_one_state_make_two_records_computed_once(
    in_place: Client, loads: list[Record], subtracting: Subtracting
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push('samples', [loads[0].refs('value')])
    first = client.compute(COPY, {'source': {'value': sides.ref('difference')}})
    second = client.compute(COPY, {'source': {'value': sides.ref('difference')}})

    assert _state(client, first).id != _state(client, second).id
    assert _state(client, first).request == _state(client, second).request
    assert subtracting.computed == 1


def test_a_push_is_added_once_the_requests_that_read_the_state_before_it_have_run(
    in_place: Client, loads: list[Record], copying: Copying
) -> None:
    client = in_place
    copying.go.clear()
    total = _total(client)
    total.push('parts', [loads[0].refs('value')])
    copied = _copy(client, total)
    copying.started.wait(timeout=5)  # the copy runs, and reads the value
    total.push('parts', [loads[1].refs('value')])  # returns at once
    read = _start(lambda: client.output(total, 'value'))

    with pytest.raises(TimeoutError):
        read.result(timeout=0.05)
    copying.go.set()
    assert read.result(timeout=5).tolist() == [3.0]
    assert client.output(copied, 'value').tolist() == [1.0]


def test_a_push_is_added_once_a_cancelled_request_that_still_runs_returns(
    in_place: Client, loads: list[Record], copying: Copying
) -> None:
    client = in_place
    copying.go.clear()
    total = _total(client)
    total.push('parts', [loads[0].refs('value')])
    copied = _copy(client, total)
    copying.started.wait(timeout=5)
    client.cancel(copied)
    total.push('parts', [loads[1].refs('value')])
    read = _start(lambda: client.output(total, 'value'))

    with pytest.raises(TimeoutError):
        read.result(timeout=0.05)
    copying.go.set()
    assert read.result(timeout=5).tolist() == [3.0]
    assert client.status(copied) is Status.CANCELLED


def test_references_in_one_submission_pin_one_state_while_rows_are_pushed(
    in_place: Client, datasets: FakeDatasets
) -> None:
    client = in_place
    runs = [datasets.measure(n, float(n)) for n in range(3, 23)]
    loads = client.compute([Request(LOAD, {'sample': {'file': run}}) for run in runs])
    total = _total(client)
    total.push('parts', [loads[0].refs('value')])
    pushing = threading.Thread(
        target=lambda: [total.push('parts', [x.refs('value')]) for x in loads[1:]]
    )
    pushing.start()
    pairs = [
        client.compute([Request(COPY, {'source': {'value': total.ref('value')}})] * 2)
        for _ in range(20)
    ]
    pushing.join()

    for pair in pairs:
        assert (
            len({r.request.params['source']['value'] for r in pair}) == 1
        )  # one state record
        upto = len(_state(client, pair[0]).request.params['parts'])
        expected = float(sum(range(3, 3 + upto)))
        assert [client.output(r, 'value').tolist() for r in pair] == [[expected]] * 2


def test_a_request_reads_several_accumulators(
    in_place: Client, loads: list[Record]
) -> None:
    client = in_place
    first, second = _total(client), _total(client)
    first.push('parts', [loads[0].refs('value')])
    second.push('parts', [loads[1].refs('value')])

    both = client.compute(TOTAL, {'parts': [first.refs(), second.refs()]})
    assert client.output(both, 'value').tolist() == [3.0]
    twice = client.compute(
        SIDES, {'samples': [first.refs()], 'backgrounds': [first.refs()]}
    )
    assert client.output(twice, 'difference').tolist() == [0.0]


def test_a_submission_pins_states_whose_pushes_wait_and_returns_at_once(
    in_place: Client, loads: list[Record], copying: Copying
) -> None:
    client = in_place
    copying.go.clear()
    first, second = _total(client), _total(client)
    for total in (first, second):
        total.push('parts', [loads[0].refs('value')])
        _copy(client, total)  # holds back the next push
    for total in (first, second):
        total.push('parts', [loads[1].refs('value')])
    reads = client.submit(
        [Request(COPY, {'source': {'value': t.ref('value')}}) for t in (first, second)]
    )

    assert [len(_state(client, r).request.params['parts']) for r in reads] == [2, 2]
    assert client.status(reads) == [Status.PENDING] * 2
    copying.go.set()
    assert [client.output(r, 'value').tolist() for r in reads] == [[3.0]] * 2


def test_a_released_accumulator_still_adds_the_pushes_logged(
    in_place: Client, loads: list[Record], copying: Copying
) -> None:
    client = in_place
    copying.go.clear()
    total = _total(client)
    total.push('parts', [loads[0].refs('value')])
    first = _copy(client, total)
    copying.started.wait(timeout=5)
    total.push('parts', [loads[1].refs('value')])  # waits for the first copy
    second = _copy(client, total)
    client.release(total)  # the copies are still readers of their states
    copying.go.set()

    assert [client.output(r, 'value').tolist() for r in (first, second)] == [
        [1.0],
        [3.0],
    ]


def test_a_released_accumulator_takes_no_pushes_or_references_and_is_still_read(
    in_place: Client, loads: list[Record], copying: Copying
) -> None:
    client = in_place
    copying.go.clear()
    total = _total(client)
    total.push('parts', [loads[0].refs('value')])
    copied = _copy(client, total)
    client.release(total)  # the copy is still a reader of its state

    released = 'the accumulator was released or is unknown'
    with pytest.raises(SubmitError, match=f'^{released}$'):
        total.push('parts', [loads[1].refs('value')])
    with pytest.raises(SubmitError, match=rf'^source\.value: {released}$'):
        _copy(client, total)
    for read in (
        lambda: client.output(total, 'value'),
        lambda: client.provenance(total),
    ):
        with pytest.raises(LookupError, match=f'^{released}$'):
            read()
    copying.go.set()
    assert client.output(copied, 'value').tolist() == [1.0]  # read after the release


def test_a_read_is_a_copy_that_a_later_push_leaves_unchanged(
    in_place: Client, loads: list[Record]
) -> None:
    client = in_place
    total = _total(client)
    total.push('parts', [loads[0].refs('value')])
    first = client.output(total, 'value')
    total.push('parts', [loads[1].refs('value')])

    assert first.tolist() == [1.0]
    assert client.output(total, 'value').tolist() == [3.0]


# Several tables, and the outputs of a state


def _sides_of(client: Client) -> Accumulator:
    """An accumulator of SIDES, which fills both tables."""
    return client.accumulator(Template(SIDES, blanks=('samples', 'backgrounds')))


def _listed(outputs: Mapping[str, np.ndarray]) -> dict[str, list[float]]:
    return {name: value.tolist() for name, value in outputs.items()}


def test_pushes_into_several_tables_give_the_plain_request_over_them(
    in_place: Client, loads: list[Record]
) -> None:
    client = in_place
    sides = _sides_of(client)
    pushes = [('backgrounds', loads[0]), ('samples', loads[1]), ('samples', loads[0])]
    reads, plains = [], []
    for n, (table, load) in enumerate(pushes, start=1):
        sides.push(table, [load.refs('value')])
        rows = {
            t: [x.refs('value') for u, x in pushes[:n] if u == t]
            for t in ('samples', 'backgrounds')
        }
        plain = client.compute(SIDES, rows)
        reads.append(_listed(client.output(sides)))
        plains.append(_listed(client.output(plain)))

    assert reads == plains
    assert [r['difference'] for r in reads] == [[-1.0], [1.0], [2.0]]
    assert client.provenance(sides).request == plain.request
    assert client.provenance(sides).records() == [loads[1], loads[0]]


def test_readers_of_one_state_compute_its_outputs_once(
    in_place: Client, loads: list[Record], copying: Copying, subtracting: Subtracting
) -> None:
    client = in_place
    copying.go.clear()
    sides = _sides_of(client)
    sides.push('samples', [loads[0].refs('value')])
    copied = client.submit(COPY, {'source': {'value': sides.ref('difference')}})
    copying.started.wait(timeout=5)  # it has read 'difference', and holds the state
    both = client.output(sides)
    again = client.output(sides, 'total')
    copying.go.set()
    client.wait(copied)
    sides.push('backgrounds', [loads[1].refs('value')])
    after = client.output(sides, 'difference')

    assert subtracting.computed == 2  # once per state
    assert _listed(both) == {'difference': [1.0], 'total': [1.0]}
    assert [again.tolist(), after.tolist()] == [[1.0], [-1.0]]


def test_the_outputs_of_a_state_are_kept_until_the_next_push(
    in_place: Client, loads: list[Record], subtracting: Subtracting
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push('samples', [loads[0].refs('value')])
    reads = [client.output(sides, name) for name in ('difference', 'total')]

    assert subtracting.computed == 1  # the first read was done before the second
    assert [r.tolist() for r in reads] == [[1.0], [1.0]]
    sides.push('backgrounds', [loads[1].refs('value')])
    assert client.output(sides, 'difference').tolist() == [-1.0]
    assert subtracting.computed == 2


def test_a_read_while_a_push_adds_pins_the_state_after_it(
    in_place: Client, loads: list[Record], subtracting: Subtracting
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push('samples', [loads[0].refs('value')])
    client.output(sides)  # the first push is added
    subtracting.adding.clear()
    subtracting.go.clear()
    sides.push('backgrounds', [loads[1].refs('value')])
    subtracting.adding.wait(timeout=5)  # the push adds
    read = _start(lambda: client.output(sides, 'difference'))
    copied = _copy(client, sides, 'difference')

    assert _state(client, copied).request == client.provenance(sides).request
    assert len(client.provenance(sides).request.params['backgrounds']) == 1
    with pytest.raises(TimeoutError):
        read.result(timeout=0.05)  # waits for the state, as for a record
    subtracting.go.set()
    assert read.result(timeout=5).tolist() == [-1.0]
    assert client.output(copied, 'value').tolist() == [-1.0]


def test_a_push_names_only_tables_the_accumulator_fills(
    in_place: Client, loads: list[Record]
) -> None:
    client = in_place
    sides = client.accumulator(
        Template(SIDES, params={'backgrounds': []}, blanks=('samples',))
    )
    row = loads[0].refs('value')

    refused = r"^a push names a table of the accumulator, \['samples'\], not "
    for table in ('backgrounds', 'other'):
        with pytest.raises(SubmitError, match=f"{refused}'{table}'$"):
            sides.push(table, [row])
    with pytest.raises(SubmitError, match=r'^a push into samples has no rows$'):
        sides.push('samples', [])
    sides.push('samples', [row])
    assert client.output(sides, 'difference').tolist() == [1.0]


def test_a_push_of_several_rows_gives_one_state(
    in_place: Client, loads: list[Record]
) -> None:
    client = in_place
    sides = _sides_of(client)
    rows = [x.refs('value') for x in loads]
    sides.push('samples', rows)
    copied = _copy(client, sides, 'difference')
    plain = client.compute(SIDES, {'samples': rows, 'backgrounds': []})

    assert _state(client, copied).request == plain.request
    assert client.output(copied, 'value').tolist() == [3.0]
    assert _listed(client.output(sides)) == _listed(client.output(plain))


def test_a_state_is_read_once_its_plain_request_would_be_accepted(
    in_place: Client, loads: list[Record]
) -> None:
    client = in_place
    both = client.accumulator(Template(BOTH, blanks=('samples', 'backgrounds')))
    both.push('samples', [loads[0].refs('value')])
    rows = {'samples': [loads[0].refs('value')], 'backgrounds': []}
    with pytest.raises(SubmitError, match='backgrounds: List should') as refused:
        client.submit(BOTH, rows)
    reason = re.escape(str(refused.value))

    with pytest.raises(SubmitError, match=rf'^source\.value: {reason}$'):
        _copy(client, both, 'difference')
    for read in (lambda: client.output(both), lambda: client.provenance(both)):
        with pytest.raises(LookupError, match=f'^{reason}$'):
            read()
    both.push('backgrounds', [loads[1].refs('value')])
    rows['backgrounds'] = [loads[1].refs('value')]
    plain = client.compute(BOTH, rows)
    assert _listed(client.output(both)) == {'difference': [-1.0], 'total': [3.0]}
    assert _listed(client.output(plain)) == _listed(client.output(both))
    assert client.provenance(both).request == plain.request


def test_a_push_is_added_once_a_read_of_the_outputs_in_progress_is_done(
    in_place: Client, loads: list[Record], subtracting: Subtracting
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push('samples', [loads[0].refs('value')])
    subtracting.compute.clear()
    read = _start(lambda: client.output(sides, 'difference'))
    subtracting.computing.wait(timeout=5)  # the read computes the output
    subtracting.adding.clear()
    sides.push('backgrounds', [loads[1].refs('value')])  # returns at once

    assert not subtracting.adding.wait(timeout=0.05)
    subtracting.compute.set()
    assert read.result(timeout=5).tolist() == [1.0]
    assert client.output(sides, 'difference').tolist() == [-1.0]


def test_a_push_proceeds_after_readers_whose_outputs_failed(
    in_place: Client, loads: list[Record], subtracting: Subtracting
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push('samples', [loads[0].refs('value')])
    subtracting.failing = True
    copied = client.compute(COPY, {'source': {'value': sides.ref('difference')}})
    subtracting.failing = True
    with pytest.raises(ValueError, match=r'^cannot compute$'):
        client.output(sides, 'difference')

    _start(lambda: sides.push('backgrounds', [loads[1].refs('value')])).result(5)
    state = _state(client, copied)
    assert client.failure([state, copied]) == [
        'cannot compute',
        f'input {state.id} failed',
    ]
    assert client.output(sides, 'difference').tolist() == [-1.0]


def test_concurrent_readers_of_one_state_compute_its_outputs_once(
    in_place: Client, loads: list[Record], subtracting: Subtracting
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push('samples', [loads[0].refs('value')])
    subtracting.compute.clear()
    read = _start(lambda: client.output(sides, 'difference'))
    subtracting.computing.wait(timeout=5)  # the read computes, and holds the state
    copies = client.submit(
        [Request(COPY, {'source': {'value': sides.ref('difference')}})] * 2
    )
    subtracting.compute.set()
    assert read.result(timeout=5).tolist() == [1.0]
    assert [client.output(c, 'value').tolist() for c in copies] == [[1.0]] * 2
    assert subtracting.computed == 1


def test_readers_of_a_state_of_kept_rows_compute_the_plain_request_once(
    datasets: FakeDatasets, copying: Copying
) -> None:
    calls: list[int] = []

    def sides(samples: list[Any], backgrounds: list[Any]) -> dict[str, Any]:
        calls.append(len(samples))
        return _subtract(
            samples=sum((row['value'] for row in samples), 0.0),
            backgrounds=sum((row['value'] for row in backgrounds), 0.0),
        )

    def load(sample: dict[str, float]) -> dict[str, np.ndarray]:
        run = sample['file']
        return {'value': np.array([run]), 'extra': np.array([-run])}

    bind = [
        function_binding(LOAD, load),
        function_binding(COPY, copying),
        kept_rows(SIDES, sides),
    ]
    with local(proposal='p1', datasets=datasets, bind=bind) as client:
        load_1 = client.compute(LOAD, {'sample': {'file': dataset(run=1)}})
        kept = client.accumulator(
            Template(SIDES, params={'backgrounds': []}, blanks=('samples',))
        )
        kept.push('samples', [load_1.refs('value')])
        copying.go.clear()
        copied = _copy(client, kept, 'difference')
        copying.started.wait(timeout=5)  # it has read 'difference', and holds the state
        total = client.output(kept, 'total')
        copying.go.set()

        assert client.output(copied, 'value').tolist() == total.tolist() == [1.0]
        assert calls == [1]


# Freeze


def test_a_freeze_is_the_record_of_the_last_state_computed_once(
    in_place: Client, loads: list[Record], subtracting: Subtracting
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push('samples', [loads[0].refs('value')])
    sides.push('backgrounds', [loads[1].refs('value')])
    looked = client.output(sides, 'difference')
    plain = client.provenance(sides).request
    total = client.freeze(sides)

    assert total.request == plain
    assert client.output(total, 'difference').tolist() == looked.tolist() == [-1.0]
    assert subtracting.computed == 1  # the look and the freeze read one state
    assert client.output(total, 'difference') is subtracting.returned[0]()  # no copy
    assert total in client.records()


def test_a_push_after_a_freeze_is_refused_while_its_record_is_pending(
    in_place: Client, loads: list[Record], subtracting: Subtracting
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push('samples', [loads[0].refs('value')])
    subtracting.compute.clear()
    total = client.freeze(sides)

    with pytest.raises(SubmitError, match=f'frozen as record {total.id}'):
        sides.push('samples', [loads[1].refs('value')])
    with pytest.raises(SubmitError, match=f'frozen as record {total.id}'):
        client.freeze(sides)
    subtracting.compute.set()
    assert client.output(total, 'total').tolist() == [1.0]


def test_a_frozen_accumulator_is_read_through_its_record(
    in_place: Client, loads: list[Record]
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push('samples', [loads[0].refs('value')])
    total = client.freeze(sides)
    client.wait(total)

    with pytest.raises(LookupError, match=f'read record {total.id} instead'):
        client.output(sides)
    with pytest.raises(SubmitError, match=f'read record {total.id} instead'):
        _copy(client, sides, 'difference')
    with pytest.raises(LookupError, match=f'read record {total.id} instead'):
        client.provenance(sides)
    copied = client.compute(COPY, {'source': {'value': total.ref('difference')}})
    assert client.output(copied, 'value').tolist() == [1.0]


def test_templates_and_rows_reference_a_frozen_record(
    in_place: Client, loads: list[Record]
) -> None:
    client = in_place
    first = _total(client)
    first.push('parts', [loads[0].refs('value')])
    first.push('parts', [loads[1].refs('value')])
    frozen = client.freeze(first)
    second = _total(client)
    second.push('parts', [frozen.refs('value')])
    second.push('parts', [loads[0].refs('value')])
    staged = client.stage(
        Template(COPY, params={'source': {'value': frozen.ref('value')}})
    )

    assert client.output(second, 'value').tolist() == [4.0]
    assert client.output(client.compute(staged, {}), 'value').tolist() == [3.0]


def test_a_freeze_whose_plain_request_would_be_refused_is_refused(
    in_place: Client, loads: list[Record]
) -> None:
    client = in_place
    both = client.accumulator(Template(BOTH, blanks=('samples', 'backgrounds')))
    both.push('samples', [loads[0].refs('value')])

    with pytest.raises(SubmitError, match='backgrounds'):
        client.freeze(both)
    both.push('backgrounds', [loads[1].refs('value')])  # takes pushes again
    total = client.freeze(both)
    assert client.output(total, 'difference').tolist() == [-1.0]


def test_a_cancelled_freeze_leaves_the_accumulator_readable_and_shut(
    in_place: Client, loads: list[Record], subtracting: Subtracting
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push('samples', [loads[0].refs('value')])
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
        sides.push('backgrounds', [loads[1].refs('value')])
    assert client.output(sides, 'total').tolist() == [1.0]
    again = client.freeze(sides)
    assert client.output(again, 'total').tolist() == [1.0]


def test_a_failed_freeze_leaves_the_accumulator_readable_and_shut(
    in_place: Client, loads: list[Record], subtracting: Subtracting
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push('samples', [loads[0].refs('value')])
    subtracting.failing = True
    failed = client.freeze(sides)

    assert client.wait(failed) == Status.FAILED
    with pytest.raises(SubmitError, match=f'frozen as record {failed.id}'):
        sides.push('backgrounds', [loads[1].refs('value')])
    assert client.output(sides, 'total').tolist() == [1.0]
    again = client.freeze(sides)
    assert client.output(again, 'total').tolist() == [1.0]


class GatedParams(BaseModel):
    """A table whose model validator refuses one row, once ``GATE`` opens."""

    parts: list[Parts]

    @model_validator(mode='after')
    def _gated(self) -> Self:
        if len(self.parts) == 1:
            GATE.reached.set()
            GATE.opened.wait(timeout=5)
            raise ValueError('needs two parts')
        return self


class Gate:
    """Where GATED's validator waits: it sets ``reached``, and waits for ``opened``."""

    def __init__(self) -> None:
        self.reached = threading.Event()
        self.opened = threading.Event()


GATE = Gate()
GATED = _spec('gated', GatedParams, Parts)


@pytest.fixture
def gate() -> Gate:
    GATE.reached.clear()
    GATE.opened.clear()
    return GATE


def test_a_push_during_a_freeze_that_is_refused_is_logged(
    datasets: FakeDatasets, gate: Gate
) -> None:
    bind = [
        function_binding(LOAD, lambda sample: {'value': sample['file'], 'extra': 0.0}),
        folding(GATED, operator.add),
    ]
    with local(proposal='p1', datasets=datasets, bind=bind) as client:
        load = client.compute(LOAD, {'sample': {'file': dataset(run=1)}})
        other = client.compute(LOAD, {'sample': {'file': dataset(run=2)}})
        gated = client.accumulator(Template(GATED, blanks=('parts',)))
        gated.push('parts', [load.refs('value')])
        freeze = _start(lambda: client.freeze(gated))
        gate.reached.wait(timeout=5)  # the freeze is being checked
        push = _start(lambda: gated.push('parts', [other.refs('value')]))
        time.sleep(0.05)  # the push reaches the backend, and waits
        gate.opened.set()

        with pytest.raises(SubmitError, match='needs two parts'):
            freeze.result(timeout=5)
        push.result(timeout=5)
        assert client.output(gated, 'value') == 3.0


def test_a_frozen_driver_is_dropped_once_its_readers_are_done(
    in_place: Client,
    loads: list[Record],
    copying: Copying,
    subtracting: Subtracting,
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push('samples', [loads[0].refs('value')])
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
    assert client.output(total, 'difference').tolist() == [1.0]


def test_a_stopped_driver_is_dropped_before_the_accumulator_is_released(
    in_place: Client,
    loads: list[Record],
    datasets: FakeDatasets,
    subtracting: Subtracting,
) -> None:
    client = in_place
    sides = _sides_of(client)
    sides.push('samples', [loads[0].refs('value')])
    datasets.corrupt(datasets.resolve(dataset(run=2)))
    sides.push('samples', [{'value': dataset(run=2)}])  # fails to add

    with pytest.raises(LookupError, match='push 1 failed'):
        client.output(sides, 'difference')
    gc.collect()
    assert subtracting.held[0]() is None  # nothing reads it any more


# Selections


class Grid(BaseModel):
    value: Array(ArraySpec(dims=('x', 'y')))  # type: ignore[valid-type]
    text: OpaqueFile


GRID = _spec('grid', LoadParams, Grid)


def grid(sample: dict[str, float]) -> dict[str, Any]:
    values = np.arange(6.0).reshape(3, 2) * sample['file']
    value = sc.array(dims=['x', 'y'], values=values)
    return {'value': value, 'text': 'a'}


@pytest.fixture
def grids(datasets: FakeDatasets) -> Iterator[Client]:
    """A client of a backend with GRID, a 3x2 array per run, and TOTAL over them."""
    bind = [function_binding(GRID, grid), folding(TOTAL, operator.add)]
    with local(proposal='p1', datasets=datasets, bind=bind) as client:
        yield client


def test_a_selection_copies_part_of_an_output_and_makes_no_record(
    grids: Client,
) -> None:
    client = grids
    record = client.compute(GRID, {'sample': {'file': dataset(run=1)}})
    row = client.output(record, 'value', select={'x': 1})
    corner = client.output(record, 'value', select={'x': slice(0, 2), 'y': 1})
    row.values[0] = 99.0

    assert row.dims == ('y',)
    assert corner.values.tolist() == [1.0, 3.0]
    assert client.output(record, 'value')['x', 1].values.tolist() == [2.0, 3.0]
    assert client.records() == [record]


def test_a_selection_of_an_accumulator_copies_part_of_its_current_state(
    grids: Client,
) -> None:
    client = grids
    runs = [client.compute(GRID, {'sample': {'file': dataset(run=n)}}) for n in (1, 2)]
    total = _total(client)
    cuts = []
    for run in runs:
        total.push('parts', [run.refs('value')])
        cuts.append(client.output(total, 'value', select={'y': 1}))

    assert [c.values.tolist() for c in cuts] == [[1.0, 3.0, 5.0], [3.0, 9.0, 15.0]]
    assert client.records() == runs


def test_a_selection_is_refused_for_an_output_not_declared_as_an_array(
    grids: Client,
) -> None:
    client = grids
    record = client.compute(GRID, {'sample': {'file': dataset(run=1)}})

    with pytest.raises(ValueError, match='needs an array'):
        client.output(record, 'text', select={'x': 0})
    with pytest.raises(ValueError, match='name of an output'):
        client.output(record, select={'x': 0})


def test_a_selection_the_output_cannot_take_raises_what_the_slice_raises(
    grids: Client,
) -> None:
    client = grids
    record = client.compute(GRID, {'sample': {'file': dataset(run=1)}})
    other = client.compute(GRID, {'sample': {'file': dataset(run=2)}})
    total = _total(client)
    total.push('parts', [record.refs('value')])

    with pytest.raises(IndexError):
        client.output(record, 'value', select={'x': 3})
    with pytest.raises(IndexError):
        client.output(total, 'value', select={'x': 3})
    with pytest.raises(sc.DimensionError):
        client.output(total, 'value', select={'z': 0})
    total.push('parts', [other.refs('value')])  # the reads let go of the state
    assert client.output(total, 'value', select={'x': 2}).values.tolist() == [
        12.0,
        15.0,
    ]
