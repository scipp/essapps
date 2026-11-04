# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)


import threading
from collections.abc import Iterator
from enum import Enum
from pathlib import Path
from typing import Annotated, Any

import pytest
from pydantic import BaseModel, Field, model_validator

from ess.apps import Backend, Client, Request, file
from ess.apps.testing import FakeFiles, StagedFunction, kept_rows, read_file
from ess.spec import File, Referenced, WorkflowSpec, function_binding

SampleRun = Annotated[Path, File()]


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
    a: Referenced[Amount]
    b: Referenced[Amount]


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


MaskFile = Annotated[Path, File(kind='mask')]


LokiMask = Annotated[Path, File(kind='mask', instrument='loki')]


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


class Scaled(BaseModel):
    """A row that holds a reference: validated when the request runs."""

    scale: float = 1.0
    amount: Referenced[Amount]

    @model_validator(mode='after')
    def _not_negative(self) -> 'Scaled':
        if self.scale < 0:
            raise ValueError('the scale is negative')
        return self


class Offset(BaseModel):
    amount: Referenced[Amount]


class ScaledParams(BaseModel):
    rows: list[Scaled] = Field(min_length=1)
    offset: Offset | None = None


def scaled(params: ScaledParams) -> ValueOutputs:
    value = sum(row.scale * row.amount.value for row in params.rows)
    if params.offset is not None:
        value += params.offset.amount.value
    return ValueOutputs(value=Amount(value=value))


SPLIT = _spec('split', outputs=PartsOutputs)


PIECES = _spec('pieces', outputs=PiecesOutputs)


BOXED = _spec('boxed', params=BoxParams)  # a box of a number


MASKED = _spec('masked', params=MaskParams)


LOKI_MASKED = _spec('loki-masked', params=LokiMaskParams)


RUNS = _spec('runs', params=RunsParams, tables=('runs',))


MODES = _spec('modes', params=ModeParams)


SUM = _spec('sum', params=SumParams, tables=('terms',))


SCALED = _spec('scaled', params=ScaledParams, tables=('rows',))


SCALED_STAGE = _spec('scaled-stage', params=ScaledParams, tables=('rows',))


SCALED_ROWS = _spec('scaled-rows', params=ScaledParams, tables=('rows',))


def add(params: AddParams) -> ValueOutputs:
    value = params.a.value + params.b.value + params.shift.offset
    return ValueOutputs(value=Amount(value=value))


def total(params: SumParams) -> ValueOutputs:
    value = sum(term.factor * term.value for term in params.terms)
    return ValueOutputs(value=Amount(value=value))


def _read(path: Path) -> Amount:
    return Amount(value=read_file(path))


def runs(params: RunsParams) -> ValueOutputs:
    value = sum(read_file(run) for run in params.runs)
    return ValueOutputs(value=Amount(value=value))


def split(params: LoadParams) -> PartsOutputs:
    value = read_file(params.sample)
    return PartsOutputs(
        parts={'whole': Amount(value=value), 'half': Amount(value=value / 2)}
    )


def pieces(params: LoadParams) -> PiecesOutputs:
    value = read_file(params.sample)
    return PiecesOutputs(pieces=[Amount(value=value), Amount(value=value / 2)])


class Gate:
    """Holds every LOAD until opened, so that its dependents stay pending."""

    def __init__(self) -> None:
        self.open = threading.Event()
        self.started = threading.Event()

    def load(self, params: LoadParams) -> ValueOutputs:
        run = read_file(params.sample)
        self.started.set()
        self.open.wait(timeout=5)
        if run < 0:
            raise ValueError('negative run')
        return ValueOutputs(value=Amount(value=run))


@pytest.fixture
def gate() -> Gate:
    return Gate()


@pytest.fixture
def files(tmp_path: Path) -> FakeFiles:
    files = FakeFiles(proposal='p1', directory=tmp_path)
    for n, value in ((1, 1.0), (2, 2.0), (3, -1.0)):
        files.measure(n, value)
    return files


@pytest.fixture
def backend(files: FakeFiles, gate: Gate) -> Iterator[Backend]:
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
        function_binding(SCALED, scaled),
        StagedFunction(SCALED_STAGE, scaled),
        kept_rows(SCALED_ROWS, scaled),
    ]
    backend = Backend(files, bindings)
    yield backend
    gate.open.set()
    backend.close()


@pytest.fixture
def client(backend: Backend) -> Client:
    return Client(backend, proposal='p1', submitter='anna')


def load(n: int) -> Request:
    return Request(LOAD, params={'sample': file(run=n)})


def operands(a: Any, b: Any) -> dict[str, Any]:
    return {'a': a, 'b': b}
