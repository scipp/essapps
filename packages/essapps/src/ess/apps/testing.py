# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Fakes for tests: a file source whose files a test makes appear, a store
held in memory, clients that count the records they read or log the values they
read, and bindings over plain functions.
"""

from __future__ import annotations

import copy
import functools
import json
import threading
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ess.spec import DriverBinding, FileRef, StageCall, WorkflowSpec
from ess.spec.binding import fixed_values
from ess.spec.testing import instance

from .client import Client
from .files import Selector
from .records import Record

type Spec = WorkflowSpec[Any, Any]


_CORRUPT = b'\x00corrupt'
"""What a corrupt file holds (see :meth:`FakeFiles.corrupt`)."""


def read_file(path: Path) -> Any:
    """
    The content of a file that :class:`FakeFiles` wrote; raises ``OSError``
    for a corrupt file.
    """
    content = path.read_bytes()
    if content == _CORRUPT:
        raise OSError('file signature not found')
    return json.loads(content)


class FakeFiles:
    """
    A file source over files in ``directory``.

    A file is identified by its run number, or by the PID of its SciCat
    dataset if it has one. :meth:`measure` writes a file to ``directory``,
    one per identity, ``run-1.json`` or the PID's, with the given content,
    which must have a JSON form, and a workflow reads it with
    :func:`read_file`. A run measured again keeps its identity and its
    position in the order of measurement, and its file is written again. A
    file is named by its run, its PID, or its path. Its metadata are the
    proposal, the run, and the fields :meth:`measure` is given, such as
    ``kind='mask'``; a file without ``kind`` is raw (see
    :class:`~.files.Selector`). :meth:`corrupt` makes the file of one
    identity unreadable, as a file cut short in transfer is: it has a path,
    but :func:`read_file` cannot read it.
    """

    def __init__(self, proposal: str, directory: Path) -> None:
        self.proposal = proposal
        self._directory = directory
        self._paths: dict[FileRef, Path] = {}
        self._content: dict[FileRef, str] = {}
        self._metadata: dict[FileRef, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def measure(self, run: int, content: Any, **fields: Any) -> FileRef:
        """Make run ``run`` appear, or appear again, and return its identity."""
        pid = fields.get('pid')
        ref = FileRef(file=f'pid:{pid}' if pid else f'uuid:run-{run}')
        name = ref.file.partition(':')[2].replace('/', '-')
        path = self._directory / f'{name}.json'
        path.write_text(text := json.dumps(content))
        with self._lock:
            self._paths[ref] = path
            self._content[ref] = text
            self._metadata[ref] = {'proposal': self.proposal, 'run': run, **fields}
        return ref

    def corrupt(self, ref: FileRef) -> None:
        self._paths[ref].write_bytes(_CORRUPT)

    def repair(self, ref: FileRef) -> None:
        self._paths[ref].write_text(self._content[ref])

    def correct(self, ref: FileRef, **fields: Any) -> None:
        self._metadata[ref] |= fields

    def resolve(self, name: FileRef) -> FileRef:
        if name in self._paths:
            return name
        kind, _, value = name.file.partition(':')
        for ref, metadata in self._metadata.items():
            known = str(self._paths[ref]) if kind == 'path' else metadata.get(kind)
            if str(known) == value:
                return ref
        raise KeyError(name)

    def path(self, ref: FileRef) -> Path:
        return self._paths[ref]

    def metadata(self, ref: FileRef) -> dict[str, Any]:
        return dict(self._metadata[ref])

    def list(self, selector: Selector) -> list[FileRef]:
        with self._lock:
            return [r for r, m in self._metadata.items() if selector.matches(m)]


class CountingClient:
    """
    Delegates to a client and counts the records its calls read: ``reads``
    holds the number of records that :meth:`records` returned and that
    :meth:`status` and :meth:`readable` were asked for.
    """

    def __init__(self, client: Client) -> None:
        self._client = client
        self.reads: Counter[str] = Counter()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def records(self, **kwargs: Any) -> list[Record]:
        records = self._client.records(**kwargs)
        self.reads['records'] += len(records)
        return records

    def status(self, records: list[Record]) -> Any:
        self.reads['status'] += len(records)
        return self._client.status(records)

    def readable(self, records: list[Record]) -> Any:
        self.reads['readable'] += len(records)
        return self._client.readable(records)


class RecordingClient:
    """
    Delegates to a client and logs the reads of values: ``outputs`` holds the
    record ID, output name, and ``select`` of each :meth:`output`, and
    ``described`` the record ID and output name of each :meth:`describe`.
    """

    def __init__(self, client: Client) -> None:
        self._client = client
        self.outputs: list[tuple[str, str | None, Mapping[str, Any] | None]] = []
        self.described: list[tuple[str, str]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def output(
        self,
        what: Any,
        name: str | None = None,
        *,
        select: Mapping[str, Any] | None = None,
    ) -> Any:
        self.outputs.append((what.id, name, select))
        return self._client.output(what, name, select=select)

    def describe(self, record: Record, name: str) -> dict[str, Any]:
        self.described.append((record.id, name))
        return self._client.describe(record, name)


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

    def read(self, record: str, output: str, tp: Any) -> Any:
        with self._lock:
            return copy.deepcopy(self._values[(record, output)])

    def __contains__(self, key: tuple[str, str]) -> bool:
        with self._lock:
            return key in self._values


@dataclass
class StagedFunction:
    """
    A binding that makes stages, over a function that takes an instance of
    the spec's params model and returns one of its outputs model, as
    ``function_binding`` takes. A stage holds nothing: each call calls the
    function. ``staged`` lists the blanks of each stage made, and
    ``fail_next`` makes the next stage fail. Neither is guarded against calls
    from several threads at once, which is fine for tests that read them
    after the calls.
    """

    spec: Spec
    function: Callable[[Any], BaseModel]
    staged: list[tuple[str, ...]] = field(default_factory=list)
    fail_next: bool = False

    def compute(self, params: Any) -> BaseModel:
        return self.function(params)

    def make_stage(
        self, fixed: Mapping[str, Any], blanks: Collection[str]
    ) -> StageCall[BaseModel]:
        if self.fail_next:
            self.fail_next = False
            raise OSError('staging failed')
        self.staged.append(tuple(blanks))
        values = fixed_values(self.spec, fixed, blanks)
        return lambda given: self.function(instance(self.spec, {**values, **given}))


class KeptRows:
    """
    A driver that keeps the rows pushed into each table of ``spec``, and at
    each read calls ``function`` with an instance of the spec's params model,
    of the fixed values and the rows so far.
    """

    def __init__(
        self,
        spec: Spec,
        function: Callable[[Any], BaseModel],
        fixed: Mapping[str, Any],
    ) -> None:
        self._spec = spec
        self._function = function
        self._fixed = fixed
        self._rows: dict[str, list[Any]] = {table: [] for table in spec.tables}

    def push(self, table: str, rows: Sequence[Any]) -> None:
        self._rows[table].extend(rows)

    def outputs(self) -> BaseModel:
        rows = {t: list(r) for t, r in self._rows.items()}
        return self._function(instance(self._spec, {**self._fixed, **rows}))


def kept_rows(spec: Spec, function: Callable[[Any], BaseModel]) -> DriverBinding:
    """
    A ``DriverBinding`` whose drivers keep the rows (:class:`KeptRows`):
    ``function`` takes the params, as ``function_binding`` takes, each table
    as lists of every row so far.

    A fake for tests that need an accumulator over a plain function, not a
    recommended driver: each read computes over every row again. A package
    writes its own driver, which decides how rows combine.
    """
    return DriverBinding(spec, functools.partial(KeptRows, spec, function))
