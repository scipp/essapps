# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Checks of the symmetries that a workflow author promises, for a package's tests.

Each check computes the same outputs in two ways and compares them, so it
needs no known result. With a single-run binding ``single`` and a multi-run
binding ``multi`` of one reduction::

    check_one_row(
        single,
        multi,
        {'run': run_611, 'can': None, 'bins': 100},
        {'sample_runs': [{'run': run_611}], 'can_runs': [], 'bins': 100},
    )
    tables = {
        'sample_runs': [{'run': run_611}, {'run': run_612}],
        'can_runs': [{'run': run_614}],
    }
    check_caching(multi, tables, [{'bins': 50}, {'bins': 100}, {'bins': 50}])
    check_arrival_and_order(multi, {'bins': 100}, tables)

Values are those the binding receives: data read and defaults filled in.
Outputs are compared up to ``rtol``, since adding in another order rounds
differently. A failed check raises ``AssertionError`` naming the symmetry and
the call. The symmetries are those of docs/developer/README.md (Symmetries).
"""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping, Sequence
from typing import Any

from .binding import Binding, Function, HeldStateBinding

Row = Mapping[str, Any]


def check_one_row(
    single: Binding | Function,
    multi: Binding | Function,
    single_values: Mapping[str, Any],
    multi_values: Mapping[str, Any],
    *,
    rtol: float = 1e-12,
) -> None:
    """
    One-row: the multi-run binding with one row per table gives the outputs of
    the single-run binding.

    ``multi_values`` are ``single_values`` with each run in a table of one row,
    and a run left out (``None``) as an empty table.
    """
    _assert_close(
        _plain(multi, multi_values), _plain(single, single_values), rtol, 'one-row'
    )


def check_caching(
    binding: Binding,
    fixed: Mapping[str, Any],
    calls: Sequence[Mapping[str, Any]],
    *,
    rtol: float = 1e-12,
) -> None:
    """
    Caching: each call through one stage gives the outputs of the plain
    request with the same values.

    The stage is made with ``fixed`` and the fields of the calls as blanks, and
    called in the order given. Repeat a call after others, as in
    ``[{'bins': 50}, {'bins': 100}, {'bins': 50}]``, to find a stage whose
    calls change what it holds.
    """
    call = binding.stage(fixed, tuple(calls[0]))
    for n, values in enumerate(calls, start=1):
        expected = binding.stage({**fixed, **values}, ())()
        _assert_close(call(**values), expected, rtol, f'caching, call {n} {values}')


def check_arrival_and_order(
    binding: HeldStateBinding,
    fixed: Mapping[str, Any],
    tables: Mapping[str, Sequence[Row]],
    *,
    rtol: float = 1e-12,
) -> None:
    """
    Arrival and order: after each push, a held state gives the outputs of the
    plain request whose tables hold the rows pushed so far.

    ``tables`` holds every table field the held state fills, with its rows;
    ``fixed`` holds every other parameter. The rows are pushed into a new held
    state for each of several schedules, each table's rows in their order:
    table by table, the tables reversed, a row of each table per push, and one
    row per push alternating between the tables. Every schedule ends with the
    same rows, so arrival in all of them is order too.

    A state whose plain request raises is not read, as the framework would
    refuse to read it; the state with every row is always read. The check
    fails if a push modifies the rows it was given.
    """
    pristine = copy.deepcopy(tables)
    for name, schedule in _schedules(tables):
        held = binding.held_state(fixed)
        pushed: dict[str, list[Row]] = {t: [] for t in tables}
        for n, push in enumerate(schedule, start=1):
            held.push({t: tables[t][i] for t, i in push.items()})
            for t, i in push.items():
                pushed[t].append(pristine[t][i])
            plain = {**fixed, **copy.deepcopy(pushed)}
            try:
                expected = binding.stage(plain, ())()
            except Exception:
                if n == len(schedule):
                    raise
                continue
            where = f'arrival, {name}, state {n} of pushes {schedule[:n]}'
            _assert_close(held.outputs(), expected, rtol, where)
        _assert_close(tables, pristine, 0.0, f'rows after pushing them, {name}')


def _schedules(
    tables: Mapping[str, Sequence[Row]],
) -> list[tuple[str, list[dict[str, int]]]]:
    """
    Named push schedules over the rows of ``tables``, each push as the index
    of its row in each table it fills; schedules that push the same are listed
    once.
    """
    names = list(tables)
    longest = max((len(rows) for rows in tables.values()), default=0)

    def by_table(order: Sequence[str]) -> list[dict[str, int]]:
        return [{t: i} for t in order for i in range(len(tables[t]))]

    candidates = {
        'table by table': by_table(names),
        'tables reversed': by_table(names[::-1]),
        'a row of each table per push': [
            {t: i for t in names if i < len(tables[t])} for i in range(longest)
        ],
        'alternating tables': [
            {t: i} for i in range(longest) for t in names if i < len(tables[t])
        ],
    }
    unique: dict[str, tuple[str, list[dict[str, int]]]] = {}
    for name, schedule in candidates.items():
        unique.setdefault(repr(schedule), (name, schedule))
    return list(unique.values())


def _plain(code: Binding | Function, values: Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(code, Binding):
        return code.stage(values, ())()
    return code(**values)


def _assert_close(actual: Any, expected: Any, rtol: float, where: str) -> None:
    """Raise ``AssertionError`` unless the values agree to ``rtol``, recursively."""
    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping) or actual.keys() != expected.keys():
            got = sorted(actual) if isinstance(actual, Mapping) else actual
            raise AssertionError(f'{where}: {got!r}, expected {sorted(expected)!r}')
        for key in expected:
            _assert_close(actual[key], expected[key], rtol, f'{where}, {key!r}')
    elif isinstance(expected, list | tuple):
        if not isinstance(actual, list | tuple) or len(actual) != len(expected):
            raise AssertionError(f'{where}: {actual!r}, expected {expected!r}')
        for i, (a, e) in enumerate(zip(actual, expected, strict=True)):
            _assert_close(a, e, rtol, f'{where}[{i}]')
    elif type(expected).__module__.partition('.')[0] == 'scipp':
        import scipp as sc
        import scipp.testing

        try:
            scipp.testing.assert_allclose(actual, expected, rtol=sc.scalar(rtol))
        except Exception as error:  # a value of another type raises other errors
            raise AssertionError(f'{where}: {error}') from None
    elif hasattr(expected, '__array__'):
        import numpy as np

        try:
            np.testing.assert_allclose(actual, expected, rtol=rtol)
        except Exception as error:
            raise AssertionError(f'{where}: {error}') from None
    elif isinstance(expected, float):
        if not (
            isinstance(actual, int | float)
            and math.isclose(actual, expected, rel_tol=rtol)
        ):
            raise AssertionError(f'{where}: {actual!r}, expected {expected!r}')
    elif actual != expected:
        raise AssertionError(f'{where}: {actual!r}, expected {expected!r}')
