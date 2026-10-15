# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Fakes for tests: a dataset source whose datasets a test makes appear, a store
held in memory, and bindings over plain functions.
"""

from __future__ import annotations

import copy
import functools
import threading
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ess.spec import DatasetRef, DriverBinding, StageCall, WorkflowSpec, table_fields

from .datasets import Selector


class FakeDatasets:
    """
    A dataset source held in memory.

    A dataset is identified by its run number, or by its PID if it has one.
    Its content is whatever the test gave to :meth:`measure`, and workflows
    read it as is. A run measured again keeps its identity and its place in
    the order of measurement.
    """

    def __init__(self, proposal: str) -> None:
        self.proposal = proposal
        self._content: dict[DatasetRef, Any] = {}
        self._metadata: dict[DatasetRef, dict[str, Any]] = {}
        self._corrupt: set[DatasetRef] = set()
        self._arrived = threading.Condition()

    def measure(self, run: int, content: Any, **fields: Any) -> DatasetRef:
        """Make run ``run`` appear, or appear again, and return its identity."""
        pid = fields.get('pid')
        ref = DatasetRef(dataset=f'pid:{pid}' if pid else f'uuid:run-{run}')
        with self._arrived:
            self._content[ref] = content
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
        if name in self._content:
            return name
        kind, _, value = name.dataset.partition(':')
        for ref, metadata in self._metadata.items():
            if str(metadata.get(kind)) == value:
                return ref
        raise KeyError(name)

    def read(self, ref: DatasetRef) -> Any:
        if ref in self._corrupt:
            raise OSError('file signature not found')
        return self._content[ref]

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
    A binding that makes stages, over a function that takes each parameter by
    name and returns the outputs by name. A stage holds nothing: each call
    calls the function. ``staged`` lists the blanks of each stage made, and
    ``fail_next`` makes the next stage fail. Neither is guarded against
    calls from several threads at once, which is fine for tests that read
    them after the calls.
    """

    spec: WorkflowSpec[Any]
    function: Callable[..., Mapping[str, Any]]
    staged: list[tuple[str, ...]] = field(default_factory=list)
    fail_next: bool = False

    def compute(self, params: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.function(**params)

    def make_stage(
        self, fixed: Mapping[str, Any], blanks: Collection[str]
    ) -> StageCall:
        if self.fail_next:
            self.fail_next = False
            raise OSError('staging failed')
        self.staged.append(tuple(blanks))
        return lambda values: self.function(**fixed, **values)


class KeptRows:
    """
    A driver that keeps the rows pushed into each of ``tables``, and calls
    ``function`` with them and the fixed values at each read.
    """

    def __init__(
        self,
        function: Callable[..., Mapping[str, Any]],
        tables: Sequence[str],
        fixed: Mapping[str, Any],
    ) -> None:
        self._function = function
        self._fixed = fixed
        self._rows: dict[str, list[Mapping[str, Any]]] = {t: [] for t in tables}

    def push(self, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
        self._rows[table].extend(rows)

    def outputs(self) -> Mapping[str, Any]:
        rows = {t: list(r) for t, r in self._rows.items()}
        return self._function(**self._fixed, **rows)


def kept_rows(
    spec: WorkflowSpec[Any], function: Callable[..., Mapping[str, Any]]
) -> DriverBinding:
    """
    A ``DriverBinding`` whose drivers keep the rows (:class:`KeptRows`):
    ``function`` takes each parameter by name, a table as a list of its rows.

    A fake for tests that need an accumulator over a plain function, not a
    recommended driver: each read computes over every row again. A package
    writes its own driver, which decides how rows combine.
    """
    tables = tuple(table_fields(spec.params))
    return DriverBinding(spec, functools.partial(KeptRows, function, tables))
