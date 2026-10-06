# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Each check passes for a binding that keeps its symmetry and fails for one
with a bug that breaks it.

The toy spec has two tables of rows ``{'x': float}``, ``sample`` and ``can``,
and outputs ``diff = scale * (sum of sample x - sum of can x)``.
"""

import functools
import operator
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pytest
import scipp as sc

from ess.spec import Function, HeldState, combine
from ess.spec.testing import (
    assert_close,
    check_arrival_and_order,
    check_caching,
    check_one_row,
)

Rows = Mapping[str, Mapping[str, Any]]


def subtract(sample: list[Rows], can: list[Rows], scale: float) -> dict[str, float]:
    total = sum(r['x'] for r in sample) - sum(r['x'] for r in can)
    return {'diff': scale * total}


class Sums:
    def __init__(self, scale: float) -> None:
        self.scale = scale
        self.sums = {'sample': 0.0, 'can': 0.0}

    def push(self, rows: Rows) -> None:
        for table, row in rows.items():
            self.sums[table] += row['x']

    def outputs(self) -> Mapping[str, float]:
        return {'diff': self.scale * (self.sums['sample'] - self.sums['can'])}


class KeepsLastRow(Sums):
    """Breaks arrival: each push replaces what was pushed before into its table."""

    def push(self, rows: Rows) -> None:
        for table, row in rows.items():
            self.sums[table] = row['x']


class StartsAtFirstSample(Sums):
    """
    Breaks order: the total starts at the first sample row, as a held state
    that takes its shape from the first sample would, and drops the cans
    pushed before it.
    """

    def __init__(self, scale: float) -> None:
        super().__init__(scale)
        self.total: float | None = None

    def push(self, rows: Rows) -> None:
        for table, row in rows.items():
            if table == 'sample':
                self.total = row['x'] + (self.total or 0.0)
            elif self.total is not None:
                self.total -= row['x']

    def outputs(self) -> Mapping[str, Any]:
        return {'diff': None if self.total is None else self.scale * self.total}


class Subtracting:
    def __init__(self, held: type[Sums] = Sums) -> None:
        self._held = held

    def stage(self, fixed: Mapping[str, Any], blanks: Sequence[str]) -> Function:
        return functools.partial(subtract, **fixed)

    def held_state(self, fixed: Mapping[str, Any]) -> HeldState:
        return self._held(**fixed)


class RemembersFirstCall(Subtracting):
    """Breaks caching: a stage holds the outputs of its first call."""

    def stage(self, fixed: Mapping[str, Any], blanks: Sequence[str]) -> Function:
        held: list[Mapping[str, Any]] = []

        def call(**values: Any) -> Mapping[str, Any]:
            if not held:
                held.append(subtract(**fixed, **values))
            return held[0]

        return call


class JoinsInPlace:
    """
    Breaks the rows: joins the lists of one table ``parts`` by adding in
    place to the first row's list, not to a copy of it.
    """

    def __init__(self) -> None:
        self.joined: list[float] | None = None

    def push(self, rows: Rows) -> None:
        row = rows['parts']
        if self.joined is None:
            self.joined = row['x']
        else:
            self.joined += row['x']

    def outputs(self) -> Mapping[str, Any]:
        return {'x': self.joined}


class Joining:
    def stage(self, fixed: Mapping[str, Any], blanks: Sequence[str]) -> Function:
        return combine(operator.add).stage(fixed, blanks)

    def held_state(self, fixed: Mapping[str, Any]) -> HeldState:
        return JoinsInPlace()


TABLES = {
    'sample': [{'x': 5.0}, {'x': 7.0}],
    'can': [{'x': 1.0}, {'x': 2.0}, {'x': 4.0}],
}


def test_arrival_and_order_pass_for_sums_kept_per_table() -> None:
    check_arrival_and_order(Subtracting(), {'scale': 2.0}, TABLES)


def test_arrival_fails_for_a_held_state_that_keeps_only_the_last_row() -> None:
    with pytest.raises(
        AssertionError, match=r"arrival, table by table, state 2 .* 'diff'"
    ):
        check_arrival_and_order(Subtracting(KeepsLastRow), {'scale': 2.0}, TABLES)


def test_order_fails_for_a_held_state_that_drops_rows_of_a_table_pushed_first() -> None:
    with pytest.raises(AssertionError, match='arrival, tables reversed, state 1 '):
        check_arrival_and_order(
            Subtracting(StartsAtFirstSample), {'scale': 2.0}, TABLES
        )


def test_arrival_fails_for_a_held_state_that_modifies_the_rows() -> None:
    with pytest.raises(
        AssertionError, match=r"rows after pushing them, table by table, 'parts'\[0\]"
    ):
        check_arrival_and_order(Joining(), {}, {'parts': [{'x': [1.0]}, {'x': [2.0]}]})


def test_states_whose_plain_request_raises_are_not_read() -> None:
    def ratio(sample: list[Rows], can: list[Rows]) -> dict[str, float]:
        return {'ratio': sum(r['x'] for r in sample) / sum(r['x'] for r in can)}

    class Ratio:
        def __init__(self) -> None:
            self.sums = {'sample': 0.0, 'can': 0.0}

        def push(self, rows: Rows) -> None:
            for table, row in rows.items():
                self.sums[table] += row['x']

        def outputs(self) -> Mapping[str, float]:
            return {'ratio': self.sums['sample'] / self.sums['can']}

    class Ratios:
        def stage(self, fixed: Mapping[str, Any], blanks: Sequence[str]) -> Function:
            return functools.partial(ratio, **fixed)

        def held_state(self, fixed: Mapping[str, Any]) -> HeldState:
            return Ratio()

    check_arrival_and_order(Ratios(), {}, TABLES)
    with pytest.raises(ZeroDivisionError):
        check_arrival_and_order(Ratios(), {}, {'sample': [{'x': 1.0}], 'can': []})


def test_caching_passes_for_a_stage_that_computes_each_call() -> None:
    calls = [{'scale': 1.0}, {'scale': 2.0}, {'scale': 1.0}]
    check_caching(Subtracting(), TABLES, calls)


def test_caching_fails_for_a_stage_that_holds_the_outputs_of_its_first_call() -> None:
    calls = [{'scale': 1.0}, {'scale': 2.0}]
    with pytest.raises(AssertionError, match=r"caching, call 2 \{'scale': 2.0\}"):
        check_caching(RemembersFirstCall(), TABLES, calls)


def single(sample: float, can: float | None, scale: float) -> dict[str, float]:
    return {'diff': scale * (sample - (can or 0.0))}


def test_one_row_passes_if_one_row_per_table_gives_the_single_run_outputs() -> None:
    check_one_row(
        single,
        Subtracting(),
        {'sample': 5.0, 'can': None, 'scale': 2.0},
        {'sample': [{'x': 5.0}], 'can': [], 'scale': 2.0},
    )


def test_one_row_fails_if_the_outputs_differ() -> None:
    with pytest.raises(AssertionError, match=r"one-row, 'diff': 10.0, expected 8.0"):
        check_one_row(
            single,
            Subtracting(),
            {'sample': 5.0, 'can': 1.0, 'scale': 2.0},
            {'sample': [{'x': 5.0}], 'can': [], 'scale': 2.0},
        )


@pytest.mark.parametrize(
    'make', [lambda v: np.array(v), lambda v: sc.array(dims=['q'], values=v)]
)
def test_arrays_are_compared_up_to_rtol(make: Any) -> None:
    def returning(values: list[float]) -> Function:
        return lambda: {'y': make(values)}

    check_one_row(returning([1.0, 2.0]), returning([1.0, 2.0 + 1e-14]), {}, {})
    with pytest.raises(AssertionError, match="one-row, 'y'"):
        check_one_row(returning([1.0, 2.0]), returning([1.0, 2.0 + 1e-6]), {}, {})
    check_one_row(
        returning([1.0, 2.0]), returning([1.0, 2.0 + 1e-6]), {}, {}, rtol=1e-5
    )


def test_assert_close_compares_up_to_rtol_and_names_where_values_differ() -> None:
    assert_close({'a': [1.0, np.array([2.0])]}, {'a': [1.0 + 1e-13, np.array([2.0])]})
    refusals = {
        r"^out: \['b'\], expected \['a'\]$": ({'b': 1.0}, {'a': 1.0}),
        r"^out, 'a': \[1.0\], expected": ({'a': [1.0]}, {'a': [1.0, 2.0]}),
        r"^out\[1\]: 2.1, expected 2.0$": ([1.0, 2.1], [1.0, 2.0]),
    }
    for message, (actual, expected) in refusals.items():
        with pytest.raises(AssertionError, match=message):
            assert_close(actual, expected, where='out')
    with pytest.raises(AssertionError, match=r'^out: 1.1, expected 1.0$'):
        assert_close(1.1, 1.0, rtol=0.01, where='out')
    assert_close(1.1, 1.0, rtol=0.2)
