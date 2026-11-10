# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)

import copy
import threading
import time
import weakref
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from pathlib import Path
from typing import Annotated, Any

import scipp as sc
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ess.apps import Accumulator, Client, Record, Request, Template, file
from ess.apps.testing import read_file
from ess.spec import (
    DriverBinding,
    File,
    Referenced,
    RunFile,
    SampleRunFile,
    WorkflowSpec,
)

type Spec = WorkflowSpec[Any, Any]


# The toy specs over numbers. A reference stands for a group only,
# so a number that one workflow outputs and another takes is the field of a
# group, Amount.
class Amount(BaseModel):
    value: float


def add_amounts(a: Amount, b: Amount) -> Amount:
    return Amount(value=a.value + b.value)


class Shift(BaseModel):
    offset: float = 0.0


class Factor(BaseModel):
    factor: float = 1.0


class Sample(BaseModel):
    sample: RunFile


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
    sample: RunFile


class Parts(BaseModel):
    parts: list[Amount | Referenced[Amount]] = Field(min_length=1)


def make_spec(
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


LOAD = make_spec('load', params=Sample, outputs=LoadOutputs)


SHIFT = make_spec('shift', params=ShiftParams, outputs=ShiftOutputs)


SCALE = make_spec('scale', params=ScaleParams)


TOTAL = make_spec('total', params=Parts, tables=('parts',))


DIGITS = make_spec('digits', params=Parts, tables=('parts',))


class Labels(BaseModel):
    tags: list[str] = []


class SumParams(BaseModel):
    scale: Factor = Field(default_factory=Factor)
    labels: Labels = Field(default_factory=Labels)
    runs: list[RunFile]
    offset: Amount | Referenced[Amount] | None = None


SUM = make_spec('sum', params=SumParams, tables=('runs',))


class WeightedPart(BaseModel):
    """A value and its weight, which has no default."""

    value: float
    weight: float


class WeightedParams(BaseModel):
    parts: list[WeightedPart | Referenced[WeightedPart]] = Field(min_length=1)


WEIGHTED = make_spec('weighted', params=WeightedParams, tables=('parts',))


class MeanAndSpread(BaseModel):
    mean: Amount
    spread: Amount | None = None


SPREAD = make_spec('spread', params=Parts, outputs=MeanAndSpread, tables=('parts',))


class Reference(BaseModel):
    total: float


class StitchParams(BaseModel):
    reference: Reference
    low: list[Amount | Referenced[Amount]]
    high: list[Amount | Referenced[Amount]]


class Curve(BaseModel):
    curve: list[float]


STITCH = make_spec(
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


WALK = make_spec('walk', params=WalkParams, tables=('steps',))


UNTYPED = make_spec(
    'untyped', params=Parts, tables=('parts',)
)  # its driver returns a dict


NOTHING = make_spec('nothing', params=Sample)  # its workflow returns None as value


class Pieces(BaseModel):
    """Outputs with elements: a run whole and halved, by name and by position."""

    parts: dict[str, Amount]
    pieces: list[Amount]


SPLIT = make_spec('split', params=Sample, outputs=Pieces)


class Amounts(BaseModel):
    amounts: list[Amount] = Field(min_length=1)


SPLIT_TOTAL = make_spec(
    'split-total', params=Amounts, outputs=Pieces, tables=('amounts',)
)  # SPLIT of the sum of a table of values


LokiMask = Annotated[Path, File(kind='mask', instrument='loki')]


class LokiMaskParams(BaseModel):
    mask: LokiMask


LOKI_MASKED = make_spec('loki-masked', params=LokiMaskParams)


class SampleRunParams(BaseModel):
    sample: SampleRunFile


SAMPLE_RUN = make_spec('sample-run', params=SampleRunParams)


class Term(BaseModel):
    """A row that holds a reference inside it."""

    scale: float = 1.0
    amount: Referenced[Amount]


class Offset(BaseModel):
    amount: Referenced[Amount]


class TermsParams(BaseModel):
    terms: list[Term] = Field(min_length=1)
    offset: Offset | None = None


TERMS = make_spec('terms', params=TermsParams, tables=('terms',))


TERMS_ROWS = make_spec('terms-rows', params=TermsParams, tables=('terms',))


def as_value(value: float) -> Value:
    return Value(value=Amount(value=value))


def load_run(params: Sample) -> LoadOutputs:
    run = read_file(params.sample)
    return LoadOutputs(value=Amount(value=run), extra=Amount(value=-run))


def shift(params: ShiftParams) -> ShiftOutputs:
    return ShiftOutputs(value=Amount(value=params.source.value + params.shift.offset))


def scale(params: ScaleParams) -> Value:
    return as_value(read_file(params.sample) * params.scaling.factor)


def halves(value: float) -> Pieces:
    whole, half = (Amount(value=value / n) for n in (1, 2))
    return Pieces(parts={'whole': whole, 'half': half}, pieces=[whole, half])


def split(params: Sample) -> Pieces:
    return halves(read_file(params.sample))


def split_total(params: Amounts) -> Pieces:
    return halves(sum(amount.value for amount in params.amounts))


def read_mask(params: LokiMaskParams) -> Value:
    return as_value(read_file(params.mask))


def read_sample(params: SampleRunParams) -> Value:
    return as_value(read_file(params.sample))


def terms(params: TermsParams) -> Value:
    """``offset`` plus each term's amount times its scale."""
    value = sum(term.scale * term.amount.value for term in params.terms)
    if params.offset is not None:
        value += params.offset.amount.value
    return as_value(value)


def weighted(params: Any) -> Value:
    """WEIGHTED as a plain function: each value times its weight, summed."""
    return as_value(sum(p.weight * p.value for p in params.parts))


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
        return as_value(self.total)


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
        return as_value(self._factor * self._total)


class Untyped:
    """The driver of UNTYPED, whose outputs are a dict, not a Value."""

    def push(self, table: str, rows: Sequence[Any]) -> None:
        pass

    def outputs(self) -> Any:
        return {'value': Amount(value=0.0)}


def start_in_thread(call: Callable[[], Any]) -> Future[Any]:
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


def load_request(n: int) -> Request:
    return Request(LOAD, params={'sample': file(run=n)})


def read_request(record: Record, output: str = 'value') -> Request:
    """A request that reads an output of a record: SHIFT by 0."""
    return Request(SHIFT, params={'source': record.ref(output)})


def stage_shift(client: Client, record: Record) -> Any:
    """A stage of SHIFT of the record's value, with the shift left open."""
    return client.stage(
        Template(SHIFT, params={'source': record.ref('value')}, blanks=('shift',))
    )


def submit_shift(client: Client, stage: Any, offset: float) -> Record:
    return client.submit(stage, params={'shift': Shift(offset=offset)})


def total_accumulator(client: Client) -> Accumulator:
    """An accumulator of TOTAL, the sum of a table of values."""
    return client.accumulator(Template(TOTAL, blanks=('parts',)))


class Sum:
    """
    Makes drivers that sum like ``Fold(TOTAL, add_amounts)``, and counts the
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
        self._fold = Fold(TOTAL, add_amounts)

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


# The toy specs over arrays. An array is a data value, which a form has no
# input for, so an array that one workflow outputs and another takes is the field
# of a group, Counts, whose place a reference fills, Referenced[Counts].
class Data(BaseModel):
    """Base of the models over arrays."""

    model_config = ConfigDict(arbitrary_types_allowed=True)


class Counts(Data):
    counts: sc.Variable


def add_counts_in_place(total: Counts, part: Counts) -> Counts:
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


ARRAY = make_spec('array', params=Sample, outputs=ArrayValue)


ARRAY_TOTAL = make_spec(
    'array-total', params=ArrayParts, outputs=ArrayValue, tables=('parts',)
)


COPY = make_spec('copy', params=CopyParams, outputs=CopyOutputs)


SIDES = make_spec(
    'sides', params=SidesParams, outputs=Sides, tables=('samples', 'backgrounds')
)


BOTH = make_spec(
    'both', params=BothParams, outputs=Sides, tables=('samples', 'backgrounds')
)


def array(params: Sample) -> ArrayValue:
    """A run as an array of one value."""
    values = [read_file(params.sample)]
    return ArrayValue(value=Counts(counts=sc.array(dims=['x'], values=values)))


def listed(counts: Counts) -> list[float]:
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


def array_total_accumulator(client: Client) -> Accumulator:
    """An accumulator of ARRAY_TOTAL, the sum of a table of arrays."""
    return client.accumulator(Template(ARRAY_TOTAL, blanks=('parts',)))


def sides_accumulator(client: Client) -> Accumulator:
    """An accumulator of SIDES, which fills both tables."""
    return client.accumulator(Template(SIDES, blanks=('samples', 'backgrounds')))


def listed_sides(outputs: Mapping[str, Counts]) -> dict[str, list[float]]:
    return {name: listed(value) for name, value in outputs.items()}
