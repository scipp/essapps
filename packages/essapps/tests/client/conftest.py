# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from ess.apps import Client, Record, Request, Workspace, file
from ess.apps.log import Log
from ess.apps.testing import FakeFiles, FakeStore, StagedFunction, kept_rows
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
    SAMPLE_RUN,
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
    read_sample,
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
        function_binding(SAMPLE_RUN, read_sample),
        StagedFunction(TERMS, terms),
        kept_rows(TERMS_ROWS, terms),
    ]


@pytest.fixture
def workspace(files: FakeFiles, bindings: list[Binding]) -> Iterator[Workspace]:
    workspace = Workspace(proposal='p1', files=files, bind=bindings)
    yield workspace
    workspace.close()


@pytest.fixture
def client(workspace: Workspace) -> Client:
    return Client(workspace, submitter='anna')


@pytest.fixture
def other(workspace: Workspace) -> Client:
    """A second client of ``workspace``, of another user."""
    return Client(workspace, submitter='bob')


@pytest.fixture
def summing() -> Sum:
    return Sum()


@pytest.fixture
def log() -> Log:
    """The history of ``summed_workspace``."""
    return Log()


@pytest.fixture
def store() -> FakeStore:
    """Where ``summed_workspace`` writes persisted outputs."""
    return FakeStore()


@pytest.fixture
def summed_workspace(
    files: FakeFiles, summing: Sum, log: Log, store: FakeStore
) -> Iterator[Workspace]:
    """A workspace that sums TOTAL with ``summing``."""
    workspace = Workspace(
        proposal='p1',
        files=files,
        bind=[
            function_binding(LOAD, load_run),
            function_binding(SHIFT, shift),
            DriverBinding(TOTAL, summing),
        ],
        log=log,
        store=store,
    )
    yield workspace
    workspace.close()


@pytest.fixture
def summed(summed_workspace: Workspace) -> Client:
    """A client of ``summed_workspace``."""
    return Client(summed_workspace, submitter='anna')


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
def in_place_workspace(
    files: FakeFiles, copying: Copying, subtracting: Subtracting
) -> Iterator[Workspace]:
    """A workspace whose ARRAY_TOTAL and SIDES add arrays in place."""
    bind = [
        function_binding(ARRAY, array),
        folding(ARRAY_TOTAL, add_counts_in_place),
        StagedFunction(COPY, copying),
        DriverBinding(SIDES, subtracting),
        DriverBinding(BOTH, subtracting),
    ]
    workspace = Workspace(proposal='p1', files=files, bind=bind)
    yield workspace
    copying.go.set()
    subtracting.go.set()
    subtracting.compute.set()
    workspace.close()


@pytest.fixture
def in_place(in_place_workspace: Workspace) -> Client:
    """A client of ``in_place_workspace``."""
    return Client(in_place_workspace, submitter='anna')


@pytest.fixture
def loads(in_place: Client) -> list[Record]:
    """The arrays of runs 1 and 2."""
    return in_place.compute(
        [Request(ARRAY, params={'sample': file(run=n)}) for n in (1, 2)]
    )
