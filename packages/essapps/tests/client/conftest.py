# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from ess.apps import Backend, Client, Record, Request, file
from ess.apps.testing import FakeFiles, StagedFunction, kept_rows
from ess.spec import (
    Binding,
    DriverBinding,
    function_binding,
)

from .toys import (
    ARRAY,
    ARRAY_TOTAL,
    BOTH,
    COPY,
    DIGITS,
    LOAD,
    LOKI_MASKED,
    NOTHING,
    SCALE,
    SHIFT,
    SIDES,
    SPLIT,
    SPLIT_TOTAL,
    SPREAD,
    STITCH,
    SUM,
    TERMS,
    TERMS_ROWS,
    TOTAL,
    UNTYPED,
    WALK,
    WEIGHTED,
    Averaging,
    Copying,
    LoadOutputs,
    Sample,
    ScaledSum,
    Subtracting,
    Sum,
    Untyped,
    Value,
    Walking,
    add_amounts,
    add_counts_in_place,
    append_digit,
    array,
    folding,
    load_request,
    load_run,
    read_mask,
    scale,
    shift,
    split,
    split_total,
    stitch,
    terms,
    weighted,
)


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
def unreadable(files: FakeFiles) -> Request:
    """LOAD of run 3, whose file cannot be read, so that the record fails."""
    files.corrupt(files.measure(3, 3.0))
    return load_request(3)


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
        folding(TOTAL, add_amounts),
        folding(DIGITS, append_digit),
        DriverBinding(SUM, scaled_sum),
        kept_rows(WEIGHTED, weighted),
        DriverBinding(SPREAD, Averaging),
        kept_rows(STITCH, stitch),
        DriverBinding(WALK, Walking),
        DriverBinding(UNTYPED, lambda fixed: Untyped()),
        function_binding(NOTHING, lambda params: Value.model_construct(value=None)),
        function_binding(SPLIT, split),
        kept_rows(SPLIT_TOTAL, split_total),
        function_binding(LOKI_MASKED, read_mask),
        StagedFunction(TERMS, terms),
        kept_rows(TERMS_ROWS, terms),
    ]


@pytest.fixture
def backend(files: FakeFiles, bindings: list[Binding]) -> Iterator[Backend]:
    backend = Backend(files, bindings)
    yield backend
    backend.close()


@pytest.fixture
def client(backend: Backend) -> Client:
    return Client(backend, proposal='p1', submitter='anna')


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
    return summed.compute([load_request(n) for n in (1, 2, 1)])


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
        folding(ARRAY_TOTAL, add_counts_in_place),
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
