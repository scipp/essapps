# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Fakes for tests: a dataset source whose datasets a test makes appear as
files, a store held in memory, and bindings over plain functions.
"""

from __future__ import annotations

import copy
import functools
import json
import threading
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ess.spec import DatasetRef, DriverBinding, StageCall, WorkflowSpec
from ess.spec.binding import fixed_values
from ess.spec.testing import instances

from .datasets import Selector

type Spec = WorkflowSpec[Any, Any, Any]


def read_dataset(path: Path) -> Any:
    """The content of a dataset that :class:`FakeDatasets` wrote."""
    return json.loads(path.read_text())


class FakeDatasets:
    """
    A dataset source over files in ``directory``.

    A dataset is identified by its run number, or by its PID if it has one.
    :meth:`measure` writes its content, which must have a JSON form, to a
    file of its own, which a workflow reads with :func:`read_dataset`. A run
    measured again keeps its identity and its place in the order of
    measurement, and its file is written again. A dataset is named by its
    run, its PID, or its path.
    """

    def __init__(self, proposal: str, directory: Path) -> None:
        self.proposal = proposal
        self._directory = directory
        self._paths: dict[DatasetRef, Path] = {}
        self._metadata: dict[DatasetRef, dict[str, Any]] = {}
        self._corrupt: set[DatasetRef] = set()
        self._arrived = threading.Condition()

    def measure(self, run: int, content: Any, **fields: Any) -> DatasetRef:
        """Make run ``run`` appear, or appear again, and return its identity."""
        pid = fields.get('pid')
        ref = DatasetRef(dataset=f'pid:{pid}' if pid else f'uuid:run-{run}')
        path = self._directory / f'run-{run}.json'
        path.write_text(json.dumps(content))
        with self._arrived:
            self._paths[ref] = path
            self._metadata[ref] = {'proposal': self.proposal, 'run': run, **fields}
            self._arrived.notify_all()
        return ref

    def corrupt(self, ref: DatasetRef) -> None:
        self._corrupt.add(ref)

    def repair(self, ref: DatasetRef) -> None:
        self._corrupt.discard(ref)

    def correct(self, ref: DatasetRef, **fields: Any) -> None:
        self._metadata[ref] |= fields

    def resolve(self, name: DatasetRef) -> DatasetRef:
        if name in self._paths:
            return name
        kind, _, value = name.dataset.partition(':')
        for ref, metadata in self._metadata.items():
            known = str(self._paths[ref]) if kind == 'path' else metadata.get(kind)
            if str(known) == value:
                return ref
        raise KeyError(name)

    def path(self, ref: DatasetRef) -> Path:
        if ref in self._corrupt:
            raise OSError('file signature not found')
        return self._paths[ref]

    def metadata(self, ref: DatasetRef) -> dict[str, Any]:
        return dict(self._metadata[ref])

    def list(self, selector: Selector) -> list[DatasetRef]:
        with self._arrived:
            return [r for r, m in self._metadata.items() if selector.matches(m)]

    def watch(self, selector: Selector) -> Iterator[DatasetRef]:
        seen: set[DatasetRef] = set()
        while True:
            with self._arrived:
                self._arrived.wait_for(lambda: set(self.list(selector)) - seen)
                fresh = [r for r in self.list(selector) if r not in seen]
            seen.update(fresh)
            yield from fresh


class FakeStore:
    """
    A store held in memory.

    It keeps a copy of each value written and returns a copy of it, as a
    store that writes files reads back a new value each time. ``fill`` makes
    every write fail, as a full disk does, until ``free``.
    """

    def __init__(self) -> None:
        self._values: dict[tuple[str, str], Any] = {}
        self._full = False
        self._lock = threading.Lock()

    def fill(self) -> None:
        self._full = True

    def free(self) -> None:
        self._full = False

    def write(self, record: str, output: str, value: Any) -> None:
        if self._full:
            raise OSError('no space left on device')
        with self._lock:
            self._values[(record, output)] = copy.deepcopy(value)

    def read(self, record: str, output: str) -> Any:
        with self._lock:
            return copy.deepcopy(self._values[(record, output)])

    def __contains__(self, key: tuple[str, str]) -> bool:
        with self._lock:
            return key in self._values


@dataclass
class StagedFunction:
    """
    A binding that makes stages, over a function that takes instances of the
    spec's params and inputs models and returns one of its outputs model, as
    ``function_binding`` takes. A stage holds nothing: each call calls the
    function. ``staged`` lists the blanks of each stage made, and
    ``fail_next`` makes the next stage fail. Neither is guarded against calls
    from several threads at once, which is fine for tests that read them
    after the calls.
    """

    spec: Spec
    function: Callable[[Any, Any], BaseModel]
    staged: list[tuple[str, ...]] = field(default_factory=list)
    fail_next: bool = False

    def compute(self, params: Any, inputs: Any) -> BaseModel:
        return self.function(params, inputs)

    def make_stage(
        self, fixed: Mapping[str, Any], blanks: Collection[str]
    ) -> StageCall[BaseModel]:
        if self.fail_next:
            self.fail_next = False
            raise OSError('staging failed')
        self.staged.append(tuple(blanks))
        values = fixed_values(self.spec, fixed, blanks)
        return lambda given: self.function(*instances(self.spec, {**values, **given}))


class KeptRows:
    """
    A driver that keeps the rows pushed into each table of ``spec``, and at
    each read calls ``function`` with instances of the spec's params and
    inputs models, of the fixed values and the rows so far.
    """

    def __init__(
        self,
        spec: Spec,
        function: Callable[[Any, Any], BaseModel],
        fixed: Mapping[str, Any],
    ) -> None:
        self._spec = spec
        self._function = function
        self._fixed = fixed
        self._columns: dict[str, list[Any]] = {
            name: [] for index in spec.tables for name in spec.columns(index)
        }

    def push(self, table: str, columns: Mapping[str, Sequence[Any]]) -> None:
        for name, column in columns.items():
            self._columns[name].extend(column)

    def outputs(self) -> BaseModel:
        columns = {n: list(c) for n, c in self._columns.items()}
        return self._function(*instances(self._spec, {**self._fixed, **columns}))


def kept_rows(spec: Spec, function: Callable[[Any, Any], BaseModel]) -> DriverBinding:
    """
    A ``DriverBinding`` whose drivers keep the rows (:class:`KeptRows`):
    ``function`` takes the params and the inputs, as ``function_binding``
    takes, each table as lists of every row so far.

    A fake for tests that need an accumulator over a plain function, not a
    recommended driver: each read computes over every row again. A package
    writes its own driver, which decides how rows combine.
    """
    return DriverBinding(spec, functools.partial(KeptRows, spec, function))
