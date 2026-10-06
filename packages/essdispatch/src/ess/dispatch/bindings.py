# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Fallbacks for code that is not a held-state binding.

A plain function is a binding that computes nothing ahead
(:class:`FunctionBinding`). For any binding that does not make held states,
:func:`open_held_state` makes one that keeps the rows and computes the plain
request over them when a state is read. The protocols are in
:mod:`ess.spec.binding`.
"""

from __future__ import annotations

import functools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ess.spec import Binding, Function, HeldState, HeldStateBinding


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
    request over them, once per state that is read. What depends only on the
    fixed values is computed once if the stage holds it, as a stage of
    ``PipelineBinding`` does; the part that depends on the rows is computed
    again, over every row, for each state that is read.
    """

    def __init__(self, call: Function, tables: Sequence[str]) -> None:
        self._call = call
        self._rows: dict[str, list[Mapping[str, Any]]] = {t: [] for t in tables}

    def push(self, rows: Mapping[str, Mapping[str, Any]]) -> None:
        for table, row in rows.items():
            self._rows[table].append(row)

    def outputs(self) -> Mapping[str, Any]:
        return self._call(**{t: list(rows) for t, rows in self._rows.items()})


def open_held_state(
    binding: Binding, fixed: Mapping[str, Any], tables: Sequence[str]
) -> HeldState:
    """
    A held state with nothing pushed, of an accumulator that fills ``tables``.

    It is the binding's own if the binding makes held states, and one that
    keeps the rows otherwise. ``fixed`` is as for
    :meth:`HeldStateBinding.held_state`.
    """
    if isinstance(binding, HeldStateBinding):
        return binding.held_state(fixed)
    return _KeptRows(binding.stage(fixed, tables), tables)
