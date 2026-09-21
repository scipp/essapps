# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Test helpers for workflow authors and for the framework's own tests."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import scipp as sc
from pydantic import BaseModel

from .binding import Factory, Inputs, Workflow
from .sources import Dataset
from .spec import DatasetRef, OutputRef, Ref, SpecId, WorkflowSpec
from .stages import Stages

CHECKED = SpecId(name='checked', version=1)
"""Stands in for the spec of the workflow under check; each check has its own store."""


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
    factory: Factory, param_sets: Iterable[BaseModel], inputs: Inputs
) -> None:
    """
    The one check on a stage: for every request a stage accepts it returns
    what the workflow returns.

    The requests are driven through the session's own stage store under one
    label, so the stages are the ones a session would build and drop, and each
    result is compared with a fresh call of the stateless callable.
    """
    workflow = factory()
    stages = Stages()
    for i, params in enumerate(param_sets):
        expected = dict(workflow(params, inputs))
        called, _ = stages.workflow_for(CHECKED, workflow, params, inputs, 'tuning')
        got = dict(called(params, inputs))
        if expected.keys() != got.keys():
            raise AssertionError(
                f'step {i}: outputs {sorted(got)} != {sorted(expected)}'
            )
        for name in expected:
            if not equal(got[name], expected[name]):
                raise AssertionError(
                    f"step {i}: the stage's output {name!r} differs from the workflow's"
                )


class _Contributions:
    """Inputs that also serve the contributions a check made, by reference."""

    def __init__(self, inputs: Inputs) -> None:
        self._inputs = inputs
        self._made: dict[Ref, Any] = {}

    def keep(self, value: Any, output: str) -> OutputRef:
        ref = OutputRef(record=f'contribution-{len(self._made)}', output=output)
        self._made[ref] = value
        return ref

    def path(self, ref: Ref) -> Path:
        return self._inputs.path(ref)

    def array(self, ref: Ref) -> Any:
        return self._made[ref] if ref in self._made else self._inputs.array(ref)


def assert_combine_is_associative(
    contribute: Workflow,
    combine: Workflow,
    spec: WorkflowSpec,
    member_params: Iterable[BaseModel],
    params: Mapping[str, Any],
    inputs: Inputs,
) -> None:
    """
    The one check on a declared combine: grouping and order do not matter.

    ``spec`` is the combine spec, whose ``chain`` names the collection parameter
    the contributions fill and the output that may come back as one of its
    elements; ``params`` are the combine's other parameters. The contributions of
    the members are combined in one group, in two groups, one at a time, and in
    reverse order, and each result is compared with the first. Combining in
    groups and one at a time pushes combined values back in, which is what a
    chained series and a fold rely on. Nothing here is specific to a workflow, so
    every spec that declares a chain runs it.
    """
    ((collection, output),) = spec.chain.items()
    members = list(member_params)
    if len(members) < 3:
        raise ValueError('an associativity check needs at least three members')
    served = _Contributions(inputs)
    contributions = [
        served.keep(next(iter(contribute(p, inputs).values())), output) for p in members
    ]

    def combined(parts: list[OutputRef]) -> Mapping[str, Any]:
        return combine(
            spec.params.model_validate({**params, collection: parts}), served
        )

    chained = contributions[0]
    for contribution in contributions[1:]:
        chained = served.keep(combined([chained, contribution])[output], output)
    groupings = {
        'in two groups': [
            served.keep(combined(contributions[:1])[output], output),
            served.keep(combined(contributions[1:])[output], output),
        ],
        'one at a time': [chained],
        'in reverse order': list(reversed(contributions)),
    }
    reference = combined(contributions)
    for how, parts in groupings.items():
        got = combined(parts)
        for name, value in reference.items():
            if not equal(got[name], value):
                raise AssertionError(f'combining {how} changes output {name!r}')


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
