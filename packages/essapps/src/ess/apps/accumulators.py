# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
``combine``: a binding that combines the rows of a table, field by field.

A request gives the whole table, and an accumulator takes one row at a time;
both combine the rows in the same order::

    class SumParams(BaseModel):
        parts: list[NormalizationParts]

    rows = [c.refs('numerator', 'denominator') for c in contributions]
    client.submit(PARTS_SUM, {'parts': rows})
    total = client.accumulator(Template(PARTS_SUM, blanks=('parts',)))
    total.push('parts', rows[0])                     # one row, the same shape

``combine`` belongs in ess.reduce next to ``PipelineBinding``; it lives here
until that is proposed there.
"""

from __future__ import annotations

import copy
import functools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .bindings import AccumulatorBinding, ElementAccumulator, Function


class _Fold:
    """
    Each field of one table's rows combined with ``operation``, in push order.

    The first row is copied, so that combining in place never changes the
    output it came from.
    """

    def __init__(self, operation: Callable[[Any, Any], Any]) -> None:
        self._operation = operation
        self._table: str | None = None
        self.total: dict[str, Any] = {}

    def push(self, table: str, row: Mapping[str, Any]) -> None:
        if self._table is None:
            self._table, self.total = table, copy.deepcopy(dict(row))
        elif table != self._table:
            raise ValueError(f'combine takes one table, not {self._table} and {table}')
        else:
            for field, value in row.items():
                self.total[field] = self._operation(self.total[field], value)

    def outputs(self, names: Sequence[str]) -> Mapping[str, Any]:
        return {name: self.total[name] for name in names}


@dataclass(frozen=True)
class _Combine:
    operation: Callable[[Any, Any], Any]

    def stage(self, fixed: Mapping[str, Any], blanks: Sequence[str]) -> Function:
        return functools.partial(self._compute, **fixed)

    def _compute(self, **table: list[Mapping[str, Any]]) -> Mapping[str, Any]:
        ((name, rows),) = table.items()
        fold = _Fold(self.operation)
        for row in rows:
            fold.push(name, row)
        return fold.total

    def accumulator(self, fixed: Mapping[str, Any]) -> ElementAccumulator:
        if fixed:
            raise TypeError(f'combine takes only a table, not also {sorted(fixed)}')
        return _Fold(self.operation)


def combine(operation: Callable[[Any, Any], Any]) -> AccumulatorBinding:
    """
    The binding of a spec whose only param is one table, and whose outputs
    are the row's fields: each field combined with ``operation``, as in
    ``operation(operation(a, b), c)``.

    A plain request and an accumulator combine the rows in the same order, so
    they give the same value. ``operation(total, row)`` may modify ``total``
    in place, and returns the combined value: with ``operator.iadd`` an
    accumulator adds in place, and with ``operator.add`` each push makes a new
    value. It must not modify ``row``. ``total`` starts as a copy of the first
    row, so neither way changes the output a row came from. Since the table is
    the spec's only param, an accumulator that opens with other values is
    refused, and so is a push into a second table.
    """
    return _Combine(operation)
