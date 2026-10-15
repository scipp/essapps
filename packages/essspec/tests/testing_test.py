# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Each check passes for a binding or driver that keeps its symmetry and fails
for one with a bug that breaks it.

The toy spec has two tables of rows ``{'x': float}``, ``sample`` and ``can``,
and outputs ``diff = scale * (sum of sample x - sum of can x)``. The checks
call only ``compute``, ``make_stage``, and drivers, so the fakes here have no
spec.
"""

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pytest
import scipp as sc

from ess.spec.testing import (
    assert_close,
    check_arrival_and_order,
    check_caching,
    check_one_row,
)

Row = Mapping[str, Any]


@dataclass(frozen=True)
class Calls:
    """Plain requests and stages that call ``function`` with every value."""

    function: Callable[..., Mapping[str, Any]]

    def compute(self, params: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.function(**params)

    def make_stage(
        self, fixed: Mapping[str, Any], blanks: Collection[str]
    ) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
        return lambda values: self.function(**fixed, **values)


def subtract(sample: list[Row], can: list[Row], scale: float) -> dict[str, float]:
    total = sum(r['x'] for r in sample) - sum(r['x'] for r in can)
    return {'diff': scale * total}


class Sums:
    def __init__(self, fixed: Mapping[str, Any]) -> None:
        self.scale = fixed.get('scale', 1.0)
        self.sums = {'sample': 0.0, 'can': 0.0}

    def push(self, table: str, rows: Sequence[Row]) -> None:
        for row in rows:
            self.sums[table] += row['x']

    def outputs(self) -> Mapping[str, float]:
        return {'diff': self.scale * (self.sums['sample'] - self.sums['can'])}


class KeepsLastPush(Sums):
    """Breaks arrival: each push replaces what was pushed before into its table."""

    def push(self, table: str, rows: Sequence[Row]) -> None:
        self.sums[table] = sum(row['x'] for row in rows)


class StartsAtFirstSample(Sums):
    """
    Breaks order: the total starts at the first sample row, as a driver that
    takes its shape from the first sample would, and drops the cans pushed
    before it.
    """

    def __init__(self, fixed: Mapping[str, Any]) -> None:
        super().__init__(fixed)
        self.total: float | None = None

    def push(self, table: str, rows: Sequence[Row]) -> None:
        for row in rows:
            if table == 'sample':
                self.total = row['x'] + (self.total or 0.0)
            elif self.total is not None:
                self.total -= row['x']

    def outputs(self) -> Mapping[str, Any]:
        return {'diff': None if self.total is None else self.scale * self.total}


class JoinsInPlace:
    """
    Breaks the rows: joins the lists of one table ``parts`` by adding in
    place to the first row's list, not to a copy of it.
    """

    def __init__(self, fixed: Mapping[str, Any]) -> None:
        self.joined: list[float] | None = None

    def push(self, table: str, rows: Sequence[Row]) -> None:
        for row in rows:
            if self.joined is None:
                self.joined = row['x']
            else:
                self.joined += row['x']

    def outputs(self) -> Mapping[str, Any]:
        return {'x': self.joined}


TABLES = {
    'sample': [{'x': 5.0}, {'x': 7.0}],
    'can': [{'x': 1.0}, {'x': 2.0}, {'x': 4.0}],
}


def test_arrival_and_order_pass_for_sums_kept_per_table() -> None:
    check_arrival_and_order(Sums, {'scale': 2.0}, TABLES)


def test_arrival_fails_for_a_driver_that_keeps_only_the_last_push() -> None:
    with pytest.raises(
        AssertionError, match=r"arrival, one row per push, table by table, state 2 "
    ):
        check_arrival_and_order(KeepsLastPush, {'scale': 2.0}, TABLES)


def test_order_fails_for_a_driver_that_drops_rows_of_a_table_pushed_first() -> None:
    with pytest.raises(
        AssertionError, match='arrival, one push per table, reversed, state 2 '
    ):
        check_arrival_and_order(StartsAtFirstSample, {'scale': 2.0}, TABLES)


def test_arrival_fails_for_a_driver_that_modifies_the_rows() -> None:
    with pytest.raises(
        AssertionError,
        match=r"rows after pushing them, one push per table, 'parts'\[0\]",
    ):
        check_arrival_and_order(
            JoinsInPlace, {}, {'parts': [{'x': [1.0]}, {'x': [2.0]}]}
        )


def test_states_that_a_driver_refuses_are_not_read() -> None:
    class Ratio(Sums):
        def outputs(self) -> Mapping[str, float]:
            return {'ratio': self.sums['sample'] / self.sums['can']}

    check_arrival_and_order(Ratio, {}, TABLES)
    with pytest.raises(ZeroDivisionError):
        check_arrival_and_order(Ratio, {}, {'sample': [{'x': 1.0}], 'can': []})


def test_caching_passes_for_a_stage_that_computes_each_call() -> None:
    calls = [{'scale': 1.0}, {'scale': 2.0}, {'scale': 1.0}]
    check_caching(Calls(subtract), TABLES, calls)


class RemembersFirstCall(Calls):
    """Breaks caching: a stage holds the outputs of its first call."""

    def make_stage(
        self, fixed: Mapping[str, Any], blanks: Collection[str]
    ) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
        held: list[Mapping[str, Any]] = []

        def call(values: Mapping[str, Any]) -> Mapping[str, Any]:
            if not held:
                held.append(self.function(**fixed, **values))
            return held[0]

        return call


def test_caching_fails_for_a_stage_that_holds_the_outputs_of_its_first_call() -> None:
    calls = [{'scale': 1.0}, {'scale': 2.0}]
    with pytest.raises(AssertionError, match=r"caching, call 2 \{'scale': 2.0\}"):
        check_caching(RemembersFirstCall(subtract), TABLES, calls)


def single(sample: float, can: float, scale: float) -> dict[str, float]:
    return {'diff': scale * (sample - can)}


def test_one_row_passes_if_one_row_per_table_gives_the_single_run_outputs() -> None:
    check_one_row(
        Calls(single),
        Calls(subtract),
        {'sample': 5.0, 'can': 1.0, 'scale': 2.0},
        {'sample': [{'x': 5.0}], 'can': [{'x': 1.0}], 'scale': 2.0},
    )


def test_one_row_fails_if_the_outputs_differ() -> None:
    with pytest.raises(AssertionError, match=r"one-row, 'diff': 10.0, expected 8.0"):
        check_one_row(
            Calls(single),
            Calls(subtract),
            {'sample': 5.0, 'can': 1.0, 'scale': 2.0},
            {'sample': [{'x': 5.0}], 'can': [], 'scale': 2.0},
        )


@pytest.mark.parametrize(
    'make', [lambda v: np.array(v), lambda v: sc.array(dims=['q'], values=v)]
)
def test_arrays_are_compared_up_to_rtol(make: Any) -> None:
    def returning(values: list[float]) -> Calls:
        return Calls(lambda: {'y': make(values)})

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


def first_half(data: Any) -> dict[str, Any]:
    """Breaks the promise on memory: returns a view of the value it reads."""
    return {'half': data[: len(data) // 2]}


def first_half_copied(data: Any) -> dict[str, Any]:
    return {'half': data[: len(data) // 2].copy()}


@pytest.mark.parametrize(
    'data',
    [
        np.array([1.0, 2.0]),
        sc.array(dims=['q'], values=[1.0, 2.0], variances=[1.0, 1.0]),
        sc.DataArray(
            sc.array(dims=['q'], values=[1.0, 2.0]),
            coords={'q': sc.array(dims=['q'], values=[0.1, 0.2])},
        ),
    ],
)
def test_one_row_fails_for_an_output_that_shares_memory_with_a_value(
    data: Any,
) -> None:
    copied, viewed = Calls(first_half_copied), Calls(first_half)
    check_one_row(copied, copied, {'data': data}, {'data': data})
    with pytest.raises(
        AssertionError, match=r"^one-row, single-run: output \['half'\]\S* shares"
    ):
        check_one_row(viewed, copied, {'data': data}, {'data': data})


def test_caching_fails_for_a_call_whose_output_shares_a_mask_with_a_value() -> None:
    def masked(data: sc.DataArray, scale: float) -> dict[str, Any]:
        return {'out': sc.DataArray(data.data * scale, masks=dict(data.masks))}

    data = sc.DataArray(
        sc.array(dims=['q'], values=[1.0, 2.0]),
        masks={'m': sc.array(dims=['q'], values=[False, True])},
    )
    shared = r"(?s)^caching, call 1 .*, plain: output \['out'\]\.masks\['m'\]"
    with pytest.raises(AssertionError, match=shared):
        check_caching(Calls(masked), {'data': data}, [{'scale': 2.0}])
