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

A binding of a spec with table fields may also make element accumulators,
like ``ess.reduce.streaming.StreamProcessor``. An accumulator holds one, made
for the values of every field but the tables it fills, so it needs such a
binding::

    held = binding.accumulator({'scale': 2.0})   # what depends on them, once
    held.push('runs', {'run': run_611})          # one row of a table, data read
    held.outputs(['normalized'])                 # {'normalized': ...}

Its outputs after rows are pushed in order are those of the plain request
whose tables hold these rows, with the same other values. It may add each row
in place, and its outputs may be what it holds, not a copy;
:mod:`ess.apps.backend` says why that is safe.
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


class ElementAccumulator(Protocol):
    def push(self, table: str, row: Mapping[str, Any]) -> None:
        """
        Add a row to the table field ``table``, with data read.

        It may modify what the accumulator holds in place, and so change an
        output returned before it, but not the row. An accumulator that starts
        from the first row's values copies them, so that adding in place never
        changes the output they came from.
        """
        ...

    def outputs(self, names: Sequence[str]) -> Mapping[str, Any]:
        """
        The named outputs of the spec over the rows pushed so far.

        They may be what the accumulator holds, not a copy.
        """
        ...


@runtime_checkable
class AccumulatorBinding(Binding, Protocol):
    def accumulator(self, fixed: Mapping[str, Any]) -> ElementAccumulator:
        """
        A new element accumulator with nothing pushed.

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
