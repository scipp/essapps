# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)


import copy
import threading
import time
import weakref
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import Future
from pathlib import Path
from typing import Annotated, Any

import pytest
import scipp as sc
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ess.apps import Accumulator, Backend, Client, Record, Request, Template, file
from ess.apps.testing import FakeFiles, StagedFunction, kept_rows, read_file
from ess.spec import (
    Binding,
    DriverBinding,
    File,
    Referenced,
    WorkflowSpec,
    function_binding,
)

type Spec = WorkflowSpec[Any, Any]


# The toy specs over numbers. A reference stands for a group only,
# so a number that one workflow outputs and another takes is the field of a
# group, Amount.
class Amount(BaseModel):
    value: float


def _add(a: Amount, b: Amount) -> Amount:
    return Amount(value=a.value + b.value)


SampleRun = Annotated[Path, File()]


RunFile = Annotated[Path, File()]


class Shift(BaseModel):
    offset: float = 0.0


class Factor(BaseModel):
    factor: float = 1.0


class Sample(BaseModel):
    sample: SampleRun


class Value(BaseModel):
    value: Amount


class LoadOutputs(BaseModel):
    value: Amount
    extra: Amount


class ShiftParams(BaseModel):
    shift: Shift | Referenced[Shift] = Field(default_factory=Shift)
    source: Amount | Referenced[Amount]


class ShiftOutputs(BaseModel):
    value: Amount


class ScaleParams(BaseModel):
    scaling: Factor = Field(default_factory=Factor)
    sample: SampleRun


class Parts(BaseModel):
    parts: list[Amount | Referenced[Amount]] = Field(min_length=1)


def _spec(
    name: str,
    *,
    params: type[BaseModel],
    outputs: type[BaseModel] = Value,
    tables: tuple[str, ...] = (),
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
    tags: list[str] = []


class SumParams(BaseModel):
    scale: Factor = Field(default_factory=Factor)
    labels: Labels = Field(default_factory=Labels)
    runs: list[RunFile]
    offset: Amount | Referenced[Amount] | None = None


SUM = _spec('sum', params=SumParams, tables=('runs',))


class WeightedPart(BaseModel):
    """A value and its weight, which has no default."""

    value: float
    weight: float


class WeightedParams(BaseModel):
    parts: list[WeightedPart | Referenced[WeightedPart]] = Field(min_length=1)


WEIGHTED = _spec('weighted', params=WeightedParams, tables=('parts',))


class MeanAndSpread(BaseModel):
    mean: Amount
    spread: Amount | None = None


SPREAD = _spec('spread', params=Parts, outputs=MeanAndSpread, tables=('parts',))


class Reference(BaseModel):
    total: float


class StitchParams(BaseModel):
    reference: Reference
    low: list[Amount | Referenced[Amount]]
    high: list[Amount | Referenced[Amount]]


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
    """A params model with a table of groups."""

    scale: PositiveScale = Field(default_factory=PositiveScale)
    steps: list[Step] = Field(min_length=1)


class WalkParams(StepsParams):
    steps: list[Step] = Field(min_length=2)


WALK = _spec('walk', params=WalkParams, tables=('steps',))


UNTYPED = _spec('untyped', params=Parts, tables=('parts',))  # its driver returns a dict


def _value(value: float) -> Value:
    return Value(value=Amount(value=value))


def load_run(params: Sample) -> LoadOutputs:
    run = read_file(params.sample)
    return LoadOutputs(value=Amount(value=run), extra=Amount(value=-run))


def shift(params: ShiftParams) -> ShiftOutputs:
    return ShiftOutputs(value=Amount(value=params.source.value + params.shift.offset))


def scale(params: ScaleParams) -> Value:
    return _value(read_file(params.sample) * params.scaling.factor)


def weighted(params: Any) -> Value:
    """WEIGHTED as a plain function: each value times its weight, summed."""
    return _value(sum(p.weight * p.value for p in params.parts))


def stitch(params: StitchParams) -> Curve:
    """
    STITCH as a plain function, a joint computation over both tables: their
    values, low ones first, scaled together so that they sum to the reference
    total.
    """
    values = [amount.value for amount in (*params.low, *params.high)]
    total = params.reference.total
    return Curve(curve=[total * value / sum(values) for value in values])


def append_digit(number: Amount, digit: Amount) -> Amount:
    """An order-sensitive combination, slow enough for pushes to overlap."""
    time.sleep(0.001)
    return Amount(value=number.value * 10 + digit.value)


class Fold:
    """
    A driver that combines the rows of one table into the spec's one
    output with ``operation``, as in ``operation(operation(a, b), c)``, in
    push order. The first value is copied, so that combining in place never
    changes the output it came from.
    """

    def __init__(self, spec: Spec, operation: Callable[[Any, Any], Any]) -> None:
        self._outputs = spec.outputs
        (self._name,) = spec.outputs.model_fields
        self._operation = operation
        self._total: Any = None

    def push(self, table: str, rows: Sequence[Any]) -> None:
        for value in rows:
            if self._total is None:
                self._total = copy.deepcopy(value)
            else:
                self._total = self._operation(self._total, value)

    def outputs(self) -> BaseModel:
        return self._outputs(**{self._name: self._total})


def folding(spec: Spec, operation: Callable[[Any, Any], Any]) -> DriverBinding:
    """A binding of a spec over one table, whose output is its rows folded."""
    return DriverBinding(spec, lambda fixed: Fold(spec, operation))


class Averaging:
    """
    The driver of SPREAD, which holds a sum and a count rather than a mean,
    and leaves out the optional spread.
    """

    def __init__(self, fixed: Mapping[str, Any]) -> None:
        self._total = 0.0
        self._count = 0

    def push(self, table: str, rows: Sequence[Any]) -> None:
        self._total += sum(row.value for row in rows)
        self._count += len(rows)

    def outputs(self) -> MeanAndSpread:
        return MeanAndSpread(mean=Amount(value=self._total / self._count))


class _ScaledTotal:
    """``offset`` plus each run times ``scale``; ``runs`` lists the runs pushed."""

    def __init__(self, fixed: Mapping[str, Any]) -> None:
        self._factor = fixed['scale'].factor
        self.total = 0.0 if fixed['offset'] is None else fixed['offset'].value
        self.runs: list[Any] = []

    def push(self, table: str, rows: Sequence[Any]) -> None:
        for run in rows:
            self.runs.append(run)
            self.total += self._factor * read_file(run)

    def outputs(self) -> Value:
        return _value(self.total)


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

    def push(self, table: str, rows: Sequence[Any]) -> None:
        self._total += sum(step.size for step in rows)

    def outputs(self) -> Value:
        return _value(self._factor * self._total)


class Untyped:
    """The driver of UNTYPED, whose outputs are a dict, not a Value."""

    def push(self, table: str, rows: Sequence[Any]) -> None:
        pass

    def outputs(self) -> Any:
        return {'value': Amount(value=0.0)}


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
    return Request(LOAD, params={'sample': file(run=n)})


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
def files(tmp_path: Path) -> FakeFiles:
    files = FakeFiles(proposal='p1', directory=tmp_path)
    for n in (1, 2):
        files.measure(n, float(n))
    return files


@pytest.fixture
def scaled_sum() -> ScaledSum:
    return ScaledSum()


@pytest.fixture
def bindings(
    shifting: StagedFunction,
    scaling: StagedFunction,
    scaled_sum: ScaledSum,
    loading: threading.Event,
) -> list[Binding]:
    def load(params: Sample) -> LoadOutputs:
        loading.wait(timeout=5)
        return load_run(params)

    return [
        function_binding(LOAD, load),
        shifting,
        scaling,
        folding(TOTAL, _add),
        folding(DIGITS, append_digit),
        DriverBinding(SUM, scaled_sum),
        kept_rows(WEIGHTED, weighted),
        DriverBinding(SPREAD, Averaging),
        kept_rows(STITCH, stitch),
        DriverBinding(WALK, Walking),
        DriverBinding(UNTYPED, lambda fixed: Untyped()),
    ]


@pytest.fixture
def backend(files: FakeFiles, bindings: list[Binding]) -> Iterator[Backend]:
    backend = Backend(files, bindings)
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


def _total(client: Client) -> Accumulator:
    """An accumulator of TOTAL, the sum of a table of values."""
    return client.accumulator(Template(TOTAL, blanks=('parts',)))


class Sum:
    """
    Makes drivers that sum like ``Fold(TOTAL, _add)``, and counts the
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
        self._fold = Fold(TOTAL, _add)

    def push(self, table: str, rows: Sequence[Any]) -> None:
        self._owner.adding.set()
        self._owner.go.wait(timeout=5)
        if self._owner.failing:
            self._owner.failing = False
            raise ValueError('cannot add')
        self._owner.pushed += 1
        self._fold.push(table, rows)

    def outputs(self) -> BaseModel:
        return self._fold.outputs()


@pytest.fixture
def summing() -> Sum:
    return Sum()


@pytest.fixture
def summed_backend(files: FakeFiles, summing: Sum) -> Iterator[Backend]:
    """A backend that sums TOTAL with ``summing``."""
    backend = Backend(
        files,
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


# The toy specs over arrays. An array is a data value, which a form has no
# input for, so an array that one workflow outputs and another takes is the field
# of a group, Counts, whose place a reference fills, Referenced[Counts].
class Data(BaseModel):
    """Base of the models over arrays."""

    model_config = ConfigDict(arbitrary_types_allowed=True)


class Counts(Data):
    counts: sc.Variable


def _add_in_place(total: Counts, part: Counts) -> Counts:
    total.counts += part.counts
    return total


class ArrayValue(Data):
    value: Counts


class ArrayParts(Data):
    parts: list[Referenced[Counts]] = Field(min_length=1)


class CopyParams(Data):
    source: Referenced[Counts]


class CopyOutputs(Data):
    value: Counts


class SidesParams(Data):
    samples: list[Referenced[Counts]]
    backgrounds: list[Referenced[Counts]]


class BothParams(Data):
    samples: list[Referenced[Counts]] = Field(min_length=1)
    backgrounds: list[Referenced[Counts]] = Field(min_length=1)


class Sides(Data):
    difference: Counts
    total: Counts


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
    values = [read_file(params.sample)]
    return ArrayValue(value=Counts(counts=sc.array(dims=['x'], values=values)))


def _listed(counts: Counts) -> list[float]:
    return counts.counts.values.tolist()


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
        return CopyOutputs(value=Counts(counts=params.source.counts.copy()))


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
        self.returned: list[weakref.ref[Counts]] = []

    def __call__(self, fixed: Mapping[str, Any]) -> '_Sides':
        driver = _Sides(self)
        self.held.append(weakref.ref(driver))
        return driver


class _Sides:
    def __init__(self, owner: Subtracting) -> None:
        self._owner = owner
        self._sums: dict[str, Any] = {'samples': 0.0, 'backgrounds': 0.0}

    def push(self, table: str, rows: Sequence[Any]) -> None:
        self._owner.adding.set()
        self._owner.go.wait(timeout=5)
        if self._owner.refusing:
            self._owner.refusing = False
            raise ValueError('cannot add')
        for row in rows:
            self._sums[table] += row.counts  # in place from the second row

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
    return Sides(
        difference=Counts(counts=samples - backgrounds),
        total=Counts(counts=samples + backgrounds),
    )


@pytest.fixture
def copying() -> Copying:
    return Copying()


@pytest.fixture
def subtracting() -> Subtracting:
    return Subtracting()


@pytest.fixture
def in_place(
    files: FakeFiles, copying: Copying, subtracting: Subtracting
) -> Iterator[Client]:
    """A client of a backend whose ARRAY_TOTAL and SIDES add arrays in place."""
    bind = [
        function_binding(ARRAY, array),
        folding(ARRAY_TOTAL, _add_in_place),
        StagedFunction(COPY, copying),
        DriverBinding(SIDES, subtracting),
        DriverBinding(BOTH, subtracting),
    ]
    backend = Backend(files, bind)
    yield Client(backend, proposal='p1', submitter='anna')
    copying.go.set()
    subtracting.go.set()
    subtracting.compute.set()
    backend.close()


@pytest.fixture
def loads(in_place: Client) -> list[Record]:
    """The arrays of runs 1 and 2."""
    return in_place.compute(
        [Request(ARRAY, params={'sample': file(run=n)}) for n in (1, 2)]
    )


def _array_total(client: Client) -> Accumulator:
    """An accumulator of ARRAY_TOTAL, the sum of a table of arrays."""
    return client.accumulator(Template(ARRAY_TOTAL, blanks=('parts',)))


def _copy(client: Client, accumulator: Accumulator, output: str = 'value') -> Record:
    return client.submit(COPY, params={'source': accumulator.ref(output)})


def _sides_of(client: Client) -> Accumulator:
    """An accumulator of SIDES, which fills both tables."""
    return client.accumulator(Template(SIDES, blanks=('samples', 'backgrounds')))


def _sides(outputs: Mapping[str, Counts]) -> dict[str, list[float]]:
    return {name: _listed(value) for name, value in outputs.items()}
