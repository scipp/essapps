# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Specs over a table: specs that combine a list of elements into one value.

The only param of such a spec is a table field (``ess.reduce.spec``): a list
of a flat model, one row per element. A request gives the whole table; an
accumulator takes one row at a time::

    class SumParams(BaseModel):
        parts: list[NormalizationParts]

    rows = [c.refs('numerator', 'denominator') for c in contributions]
    client.submit(PARTS_SUM, {'parts': rows})
    accumulator.push(rows[0])                        # one row, the same shape

The outputs are any model; a sum outputs the row's fields, a mean or a sum
into a wider type does not.

``combine`` belongs in ess.reduce next to ``PipelineBinding``; it lives here
until that is proposed there.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ess.reduce.spec import WorkflowSpec, table_fields
from pydantic import BaseModel

from .bindings import AccumulatorBinding, ElementAccumulator, Function


def element_table(spec: WorkflowSpec) -> tuple[str, type[BaseModel]] | None:
    """The name and row model of the table that is the only param of ``spec``."""
    tables = table_fields(spec.params)
    if len(spec.params.model_fields) != 1 or len(tables) != 1:
        return None
    return next(iter(tables.items()))


class _Fold:
    """Each field combined with ``operation``, in push order."""

    def __init__(self, operation: Callable[[Any, Any], Any]) -> None:
        self._operation = operation
        self._value: dict[str, Any] | None = None

    def push(self, element: Mapping[str, Any]) -> None:
        if self._value is None:
            self._value = dict(element)
        else:
            value = self._value
            self._value = {f: self._operation(value[f], v) for f, v in element.items()}

    @property
    def value(self) -> Mapping[str, Any]:
        if self._value is None:
            raise ValueError('nothing has been pushed')
        return self._value


@dataclass(frozen=True)
class _Combine:
    operation: Callable[[Any, Any], Any]

    def stage(self, fixed: Mapping[str, Any], blanks: Sequence[str]) -> Function:
        return functools.partial(self._compute, **fixed)

    def _compute(self, **table: list[Mapping[str, Any]]) -> Mapping[str, Any]:
        (rows,) = table.values()
        fold = self.accumulator()
        for row in rows:
            fold.push(row)
        return fold.value

    def accumulator(self) -> ElementAccumulator:
        return _Fold(self.operation)


def combine(operation: Callable[[Any, Any], Any]) -> AccumulatorBinding:
    """
    The binding of a spec over a table that outputs the row's fields: each
    field combined with ``operation``, as in ``operation(operation(a, b), c)``.

    A plain request and an accumulator combine the elements in the same
    order, so they give the same value. ``operation`` must return a
    new value and not modify its arguments.
    """
    return _Combine(operation)
