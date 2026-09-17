# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Test helpers for workflow authors and for the framework's own tests."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .binding import Factory, Inputs, combining
from .sources import Dataset
from .spec import DatasetRef, Reference
from .warm import equal


class LocalInputs:
    """
    Inputs over the files a test names by reference; arrays load as scipp HDF5.

    A reference not named here fails, so a test that hands a callable literal
    paths cannot pass by accident.
    """

    def __init__(self, locations: dict[Reference, Path]) -> None:
        self._locations = locations

    def path(self, ref: Reference) -> Path:
        return self._locations[ref]

    def array(self, ref: Reference) -> Any:
        import scipp as sc

        return sc.io.load_hdf5(self.path(ref))


def assert_warm_equals_cold(
    factory: Factory, param_sets: Iterable[BaseModel], inputs: Inputs
) -> None:
    """
    Drive one callable through ``param_sets`` and compare each result with a
    fresh callable's; the one check on a warm workflow's reuse rules.
    """
    warm = factory()
    for i, params in enumerate(param_sets):
        cold = dict(factory()(params, inputs))
        got = dict(warm(params, inputs))
        if cold.keys() != got.keys():
            raise AssertionError(f'step {i}: outputs {sorted(got)} != {sorted(cold)}')
        for name in cold:
            if not equal(got[name], cold[name]):
                raise AssertionError(
                    f'step {i}: warm output {name!r} differs from cold'
                )


def assert_combine_is_associative(
    factory: Factory, param_sets: Iterable[BaseModel], inputs: Inputs
) -> None:
    """
    The one check on a declared combine (D15): grouping and order do not matter.

    The contributions of the members are combined in one group, in two groups,
    one at a time, and in reverse order, and each finalized result is compared
    with the first. Combining in groups and one at a time pushes combined values
    back in, which is what a chained series and a fold rely on. The single
    callable is checked to be contribute then finalize. Nothing here is specific
    to a workflow, so every spec that declares a contribution runs it.
    """
    params = list(param_sets)
    if len(params) < 3:
        raise ValueError('an associativity check needs at least three members')
    workflow = combining(factory())
    contributions = [workflow.contribute(p, inputs) for p in params]

    def finalized(parts: list[Any]) -> Mapping[str, Any]:
        return workflow.finalize(workflow.combine(parts), params[0], inputs)

    chained = contributions[0]
    for contribution in contributions[1:]:
        chained = workflow.combine([chained, contribution])
    groupings = {
        'in two groups': [
            workflow.combine(contributions[:1]),
            workflow.combine(contributions[1:]),
        ],
        'one at a time': [chained],
        'in reverse order': list(reversed(contributions)),
    }
    reference = finalized(contributions)
    for how, parts in groupings.items():
        got = finalized(parts)
        for name, value in reference.items():
            if not equal(got[name], value):
                raise AssertionError(f'combining {how} changes output {name!r}')
    one_shot = factory()(params[0], inputs)
    for name, value in workflow.finalize(contributions[0], params[0], inputs).items():
        if not equal(one_shot[name], value):
            raise AssertionError(
                f'the single callable differs from contribute then finalize '
                f'at output {name!r}'
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
