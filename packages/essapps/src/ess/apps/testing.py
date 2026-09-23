# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Test helpers for workflow authors and for the framework's own tests."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import scipp as sc

from .binding import Inputs, Workflow
from .sources import Dataset
from .spec import DatasetRef, Ref, WorkflowSpec, submodel


def equal(a: Any, b: Any) -> bool:
    """Whether two workflow outputs are the same value, scipp objects included."""
    if a is b:
        return True
    if isinstance(a, sc.Variable | sc.DataArray | sc.Dataset | sc.DataGroup):
        return type(a) is type(b) and sc.identical(a, b)
    if isinstance(a, list | tuple) and isinstance(b, list | tuple):
        return len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(equal(a[k], b[k]) for k in a)
    try:
        return bool(a == b)
    except Exception:
        return False


class LocalInputs:
    """
    Inputs over the files a test names by reference; arrays load as scipp HDF5.

    A reference not named here fails, so a test that hands a callable literal
    paths cannot pass by accident.
    """

    def __init__(self, locations: dict[Ref, Path]) -> None:
        self._locations = locations

    def path(self, ref: Ref) -> Path:
        return self._locations[ref]

    def array(self, ref: Ref) -> Any:
        return sc.io.load_hdf5(self.path(ref))


def assert_stage_equals_workflow(
    workflow: Workflow,
    spec: WorkflowSpec,
    params: Mapping[str, Any],
    stage_inputs: Iterable[Mapping[str, Any]],
    inputs: Inputs,
) -> None:
    """
    The one check on a stage: it returns what a plain run with the same values
    returns.

    One stage is built over the parameters named in the first of
    ``stage_inputs``, with ``params`` set, and called with each of
    ``stage_inputs`` in turn, as a session calls a stage it holds. Each result
    is compared with a plain run, the stage with no inputs, of the workflow
    with every value set.
    """
    values = [dict(v) for v in stage_inputs]
    names = tuple(values[0])
    outputs = spec.results
    # As the runner does: the spec's defaults fill what neither sets.
    fields = spec.params.model_fields
    fixed = submodel(
        spec.params,
        [
            name
            for name, info in fields.items()
            if name not in names and (name in params or not info.is_required())
        ],
        'Fixed',
    ).model_validate(params)
    staged = submodel(spec.params, names, 'Staged')
    nothing = submodel(spec.params, (), 'Nothing')()
    stage = workflow.stage(fixed, names, outputs, inputs)
    for i, given in enumerate(values):
        full = spec.params.model_validate({**params, **given})
        expected = dict(workflow.stage(full, (), outputs, inputs)(nothing, {}, inputs))
        got = dict(stage(staged.model_validate(given), {}, inputs))
        for name in outputs:
            if not equal(got[name], expected[name]):
                raise AssertionError(
                    f"step {i}: the stage's output {name!r} differs from a plain run"
                )


class FakeDatasetSource:
    """
    Yields whatever was added; repeats and out-of-order arrival are allowed.

    ``locates=False`` announces datasets whose bytes have not landed, so that a
    run over one fails with ``missing-dataset``. ``located`` is every reference
    the source was asked to locate, in order.
    """

    def __init__(self, *datasets: Dataset, locates: bool = True) -> None:
        self._datasets = list(datasets)
        self._locates = locates
        self.located: list[DatasetRef] = []

    def add(self, dataset: Dataset) -> None:
        self._datasets.append(dataset)

    def new_datasets(self, proposal: str) -> list[Dataset]:
        return list(self._datasets)

    def locate(self, ref: DatasetRef) -> Path | None:
        self.located.append(ref)
        if not self._locates:
            return None
        return next((d.path for d in self._datasets if d.ref == ref), None)


class FakePublisher:
    """Records what was published; a PID per output."""

    def __init__(self) -> None:
        self.entries: dict[str, dict[str, Any]] = {}

    def publish(self, path: Path, snapshot: dict[str, Any]) -> str:
        pid = f'fake/{len(self.entries) + 1}'
        self.entries[pid] = {'path': path, 'snapshot': snapshot}
        return pid
