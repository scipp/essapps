# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Bindings: the code behind a spec.

A binding is staged with the values that stay fixed and returns a callable
over the rest, the blanks. It may compute what depends only on the fixed
values when staged or on the first call, and hold it; a request outside a
stage is staged with no blanks and called once::

    binding.stage(values, ())()                      # a plain request
    call = binding.stage(fixed, ('bins',))           # a stage
    call(bins=100)

A plain function is a binding that computes nothing ahead. How a binding
computes is invisible in the records: a call through a stage returns what the
plain request returns.

An accumulator holds a held state, made for the values of every field but the
tables it fills. A binding of a spec with table fields may make held states,
like ``ess.reduce.streaming.StreamProcessor``::

    held = binding.accumulator({'scale': 2.0})   # what depends on them, once
    held.push({'runs': {'run': run_611}})        # a row per named table, data read
    held.outputs(['normalized'])                 # {'normalized': ...}

Its outputs after rows are pushed in order are those of the plain request
whose tables hold these rows, with the same other values. It may add each push
in place, and its outputs may be what it holds, not a copy;
:mod:`ess.apps.backend` says why that is safe.

For any other binding, :func:`held_state` makes a held state that keeps the
rows and computes the plain request over them at each read.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

Function = Callable[..., Mapping[str, Any]]
"""
Takes parameter values by field name, with data read, and returns the outputs.

It gets every field of the params model, defaults filled in, so it declares no
defaults of its own.
"""


@runtime_checkable
class Binding(Protocol):
    def stage(self, fixed: Mapping[str, Any], blanks: Sequence[str]) -> Function:
        """
        A callable over ``blanks`` that computes the outputs with ``fixed``.

        Both this method and the callable may be called from several threads
        at once. Neither may modify the values it is given, and the callable
        may return the same object in several calls: records made through one
        stage share what does not depend on the blanks.
        """
        ...


class HeldState(Protocol):
    def push(self, rows: Mapping[str, Mapping[str, Any]]) -> None:
        """
        Add one row to each named table field, with data read.

        The rows of one push enter one state, so a binding may add them at
        once. It may modify what it holds in place, and so change an output
        returned before it, but not the rows. A held state that starts from
        the first row's values copies them, so that adding in place never
        changes the output they came from.
        """
        ...

    def outputs(self, names: Sequence[str]) -> Mapping[str, Any]:
        """
        The named outputs of the spec over the rows pushed so far.

        They may be what the held state holds, not a copy. An output the spec
        declares optional may be left out. A call must not modify what an
        earlier call for the same state returned: running readers still use
        it while later names are computed.
        """
        ...


@runtime_checkable
class AccumulatorBinding(Binding, Protocol):
    def accumulator(self, fixed: Mapping[str, Any]) -> HeldState:
        """
        A new held state with nothing pushed.

        ``fixed`` holds every parameter but the tables it fills, with data
        read and defaults filled in. Each value is typed by its field alone,
        since the params model's own validators may need the tables. Its
        ``push`` and ``outputs`` are called from one thread at a time.
        """
        ...


@dataclass(frozen=True)
class FunctionBinding:
    """A plain function as a binding: every call computes everything."""

    function: Function

    def stage(self, fixed: Mapping[str, Any], blanks: Sequence[str]) -> Function:
        return functools.partial(self.function, **fixed)


def as_binding(code: Binding | Function) -> Binding:
    """``code`` as a binding; a plain function is wrapped."""
    return code if isinstance(code, Binding) else FunctionBinding(code)


class _KeptRows:
    """
    The held state of a binding that makes none: the rows pushed so far.

    ``call`` is the binding staged with the fixed values and the tables as
    blanks. A read calls it with every row so far, so it computes the plain
    request over them. What depends only on the fixed values is computed once
    if the stage holds it, as a stage of ``PipelineBinding`` does; the part
    that depends on the rows is computed again at each read.
    """

    def __init__(self, call: Function, tables: Sequence[str]) -> None:
        self._call = call
        self._rows: dict[str, list[Mapping[str, Any]]] = {t: [] for t in tables}

    def push(self, rows: Mapping[str, Mapping[str, Any]]) -> None:
        for table, row in rows.items():
            self._rows[table].append(row)

    def outputs(self, names: Sequence[str]) -> Mapping[str, Any]:
        outputs = self._call(**{t: list(rows) for t, rows in self._rows.items()})
        return {name: outputs[name] for name in names if name in outputs}


def held_state(
    binding: Binding, fixed: Mapping[str, Any], tables: Sequence[str]
) -> HeldState:
    """
    A held state with nothing pushed, of an accumulator that fills ``tables``.

    It is the binding's own if the binding makes held states, and one that
    keeps the rows otherwise. ``fixed`` is as for
    :meth:`AccumulatorBinding.accumulator`.
    """
    if isinstance(binding, AccumulatorBinding):
        return binding.accumulator(fixed)
    return _KeptRows(binding.stage(fixed, tables), tables)
