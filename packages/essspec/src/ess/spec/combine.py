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
    total.push({'parts': rows[0]})                   # one row, the same shape
"""

from __future__ import annotations

import copy
import functools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .binding import Function, HeldState, HeldStateBinding


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

    def push(self, rows: Mapping[str, Mapping[str, Any]]) -> None:
        for table, row in rows.items():
            if self._table is None:
                self._table, self.total = table, copy.deepcopy(dict(row))
            elif table != self._table:
                raise ValueError(
                    f'combine takes one table, not {self._table} and {table}'
                )
            else:
                for field, value in row.items():
                    self.total[field] = self._operation(self.total[field], value)

    def outputs(self) -> Mapping[str, Any]:
        return self.total


@dataclass(frozen=True)
class _Combine:
    operation: Callable[[Any, Any], Any]

    def stage(self, fixed: Mapping[str, Any], blanks: Sequence[str]) -> Function:
        return functools.partial(self._compute, **fixed)

    def _compute(self, **table: list[Mapping[str, Any]]) -> Mapping[str, Any]:
        ((name, rows),) = table.items()
        fold = _Fold(self.operation)
        for row in rows:
            fold.push({name: row})
        return fold.total

    def held_state(self, fixed: Mapping[str, Any]) -> HeldState:
        if fixed:
            raise TypeError(f'combine takes only a table, not also {sorted(fixed)}')
        return _Fold(self.operation)


def combine(operation: Callable[[Any, Any], Any]) -> HeldStateBinding:
    """
    The binding of a spec whose only param is one table, and whose outputs
    are the row's fields: each field combined with ``operation``, as in
    ``operation(operation(a, b), c)``.

    A plain request and an accumulator combine the rows in the same order, so
    they give the same value. ``operation(total, row)`` may modify ``total``
    in place, and returns the combined value: with ``operator.iadd`` the
    held state adds in place, and with ``operator.add`` each push makes a new
    value. It must not modify ``row``. ``total`` starts as a copy of the first
    row, so neither way changes the output a row came from. With no rows it
    returns no outputs, so a read of a state with nothing pushed fails, as the
    plain request over no rows does. Since the table is the spec's only param,
    an accumulator that opens with other values stops, and so does one that a
    push adds a second table to.
    """
    return _Combine(operation)
