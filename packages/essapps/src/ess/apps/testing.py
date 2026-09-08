# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Test helpers for workflow authors and for the framework's own tests."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .binding import Factory
from .warm import equal


def _fields(result: Any) -> dict[str, Any]:
    if isinstance(result, BaseModel):
        return {n: getattr(result, n) for n in type(result).model_fields}
    return dict(result)


def assert_warm_equals_cold(factory: Factory, param_sets: Iterable[BaseModel]) -> None:
    """
    Drive one callable through ``param_sets`` and compare each result with a
    fresh callable's; the one check on a warm workflow's reuse rules.
    """
    warm = factory()
    for i, params in enumerate(param_sets):
        cold = _fields(factory()(params))
        got = _fields(warm(params))
        if cold.keys() != got.keys():
            raise AssertionError(f'step {i}: outputs {sorted(got)} != {sorted(cold)}')
        for name in cold:
            if not equal(got[name], cold[name]):
                raise AssertionError(
                    f'step {i}: warm output {name!r} differs from cold'
                )


@dataclass(frozen=True)
class Dataset:
    """What a dataset source yields: a PID, a path where its bytes are, metadata."""

    pid: str
    path: Path
    metadata: dict[str, Any] = field(default_factory=dict)


class FakeDatasetSource:
    """Yields whatever was added; repeats and out-of-order arrival are allowed."""

    def __init__(self) -> None:
        self._datasets: list[Dataset] = []

    def add(self, dataset: Dataset) -> None:
        self._datasets.append(dataset)

    def new_datasets(self, proposal: str) -> list[Dataset]:
        return list(self._datasets)


class FakePublisher:
    """Records what was published; a PID per output."""

    def __init__(self) -> None:
        self.entries: dict[str, dict[str, Any]] = {}

    def publish(self, path: Path, snapshot: dict[str, Any]) -> str:
        pid = f'fake/{len(self.entries) + 1}'
        self.entries[pid] = {'path': path, 'snapshot': snapshot}
        return pid
