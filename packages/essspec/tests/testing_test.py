# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Each check passes for a binding or driver that keeps its symmetry and fails
for one with a bug that breaks it.

The toy spec ``SUB`` takes a sample and a can value and gives
``diff = factor * (sample - can)``; ``SUB_MULTI`` sums the rows of each table,
a can row of ``None`` as no can.
"""

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any, NewType

import numpy as np
import pytest
import scipp as sc
from pydantic import BaseModel, Field

from ess.spec import DriverBinding, WorkflowSpec
from ess.spec.binding import fixed_values
from ess.spec.testing import (
    Rows,
    assert_close,
    check_arrival_and_order,
    check_caching,
    check_one_row,
    one_row,
)


class Scale(BaseModel):
    factor: float = 1.0


SampleValue = NewType('SampleValue', float)
CanValue = NewType('CanValue', float)
Diff = NewType('Diff', float)


class SubParams(BaseModel):
    scale: Scale = Field(default_factory=Scale)
    sample: SampleValue
    can: CanValue | None = None


class SubMultiParams(BaseModel):
    scale: Scale = Field(default_factory=Scale)
    sample: list[SampleValue] = Field(min_length=1)
    can: list[CanValue | None] = Field(default_factory=list)


class SubOutputs(BaseModel):
    diff: Diff | None


def _spec(name: str, **fields: Any) -> WorkflowSpec[Any, Any]:
    return WorkflowSpec(name=name, version=1, title=name, description=name, **fields)


SUB = _spec('sub', params=SubParams, outputs=SubOutputs)
SUB_MULTI = _spec(
    'sub-multi', params=SubMultiParams, outputs=SubOutputs, tables=('sample', 'can')
)


def subtract(scale: Scale, sample: float, can: float | None) -> SubOutputs:
    return SubOutputs(diff=scale.factor * (sample - (can or 0.0)))


@dataclass(frozen=True)
class Calls:
    """Plain requests and stages of a spec that call ``function`` with every value."""

    spec: WorkflowSpec[Any, Any]
    function: Callable[..., BaseModel]

    def compute(self, params: BaseModel) -> BaseModel:
        return self.function(**dict(params))

    def make_stage(
        self, fixed: Mapping[str, Any], blanks: Collection[str]
    ) -> Callable[[Mapping[str, Any]], BaseModel]:
        fixed = fixed_values(self.spec, fixed, blanks)
        return lambda values: self.function(**fixed, **values)


class RemembersFirstCall(Calls):
    """Breaks caching: a stage holds the outputs of its first call."""

    def make_stage(
        self, fixed: Mapping[str, Any], blanks: Collection[str]
    ) -> Callable[[Mapping[str, Any]], BaseModel]:
        call = super().make_stage(fixed, blanks)
        held: list[BaseModel] = []

        def remember(values: Mapping[str, Any]) -> BaseModel:
            if not held:
                held.append(call(values))
            return held[0]

        return remember


class Sums:
    """A driver of ``SUB_MULTI``."""

    def __init__(self, fixed: Mapping[str, Any]) -> None:
        self.factor = fixed['scale'].factor
        self.sums = {'sample': 0.0, 'can': 0.0}

    def push(self, table: str, rows: Rows) -> None:
        self.sums[table] += sum(row for row in rows if row is not None)

    def outputs(self) -> SubOutputs:
        return SubOutputs(diff=self.factor * (self.sums['sample'] - self.sums['can']))


class IgnoresCans(Sums):
    """Breaks one-row: drops the cans."""

    def push(self, table: str, rows: Rows) -> None:
        if table == 'sample':
            super().push(table, rows)


class KeepsLastPush(Sums):
    """Breaks arrival: each push replaces what was pushed before into its table."""

    def push(self, table: str, rows: Rows) -> None:
        self.sums[table] = sum(rows)


class StartsAtFirstSample(Sums):
    """
    Breaks order: the total starts at the first sample row, as a driver that
    takes its shape from the first sample would, and drops the cans pushed
    before it.
    """

    def __init__(self, fixed: Mapping[str, Any]) -> None:
        super().__init__(fixed)
        self.total: float | None = None

    def push(self, table: str, rows: Rows) -> None:
        for value in rows:
            if table == 'sample':
                self.total = value + (self.total or 0.0)
            elif self.total is not None:
                self.total -= value

    def outputs(self) -> SubOutputs:
        total = self.total
        return SubOutputs(diff=None if total is None else self.factor * total)


Part = NewType('Part', object)
Joined = NewType('Joined', object)


class PartsParams(BaseModel):
    parts: list[Part] = Field(min_length=1)


class JoinedOutputs(BaseModel):
    joined: Joined | None


PARTS = _spec('parts', params=PartsParams, outputs=JoinedOutputs, tables=['parts'])


class JoinsInPlace:
    """
    Breaks the rows: joins the lists of the table ``parts`` by adding in
    place to the first row's list, not to a copy of it.
    """

    def __init__(self, fixed: Mapping[str, Any]) -> None:
        self.joined: list[float] | None = None

    def push(self, table: str, rows: Rows) -> None:
        for value in rows:
            if self.joined is None:
                self.joined = value
            else:
                self.joined += value

    def outputs(self) -> JoinedOutputs:
        return JoinedOutputs(joined=self.joined)


FIXED = {'scale': Scale(factor=2.0)}
TABLES = {'sample': [5.0, 7.0], 'can': [1.0, 2.0, 4.0]}


def test_arrival_and_order_pass_for_sums_kept_per_table() -> None:
    check_arrival_and_order(Sums, FIXED, TABLES)


def test_arrival_fails_for_a_driver_that_keeps_only_the_last_push() -> None:
    with pytest.raises(
        AssertionError, match=r"arrival, one row per push, table by table, state 2 "
    ):
        check_arrival_and_order(KeepsLastPush, FIXED, TABLES)


def test_order_fails_for_a_driver_that_drops_rows_of_a_table_pushed_first() -> None:
    with pytest.raises(
        AssertionError, match='arrival, one push per table, reversed, state 2 '
    ):
        check_arrival_and_order(StartsAtFirstSample, FIXED, TABLES)


def test_arrival_fails_for_a_driver_that_modifies_the_values_pushed() -> None:
    with pytest.raises(
        AssertionError,
        match=r"values after pushing them, one push per table, 'parts'\[0\]: ",
    ):
        check_arrival_and_order(JoinsInPlace, {}, {'parts': [[1.0], [2.0]]})


def test_states_that_a_driver_refuses_are_not_read() -> None:
    class Ratio(Sums):
        def outputs(self) -> SubOutputs:
            return SubOutputs(diff=self.sums['sample'] / self.sums['can'])

    check_arrival_and_order(Ratio, FIXED, TABLES)
    with pytest.raises(ZeroDivisionError):
        check_arrival_and_order(Ratio, FIXED, {'sample': [1.0], 'can': []})


def test_caching_passes_for_a_stage_that_computes_each_call() -> None:
    calls = [{'scale': Scale(factor=f)} for f in (1.0, 2.0, 1.0)]
    check_caching(Calls(SUB, subtract), {'sample': 5.0, 'can': 1.0}, calls)


def test_caching_fails_for_a_stage_that_holds_the_outputs_of_its_first_call() -> None:
    calls = [{'scale': Scale(factor=f)} for f in (1.0, 2.0)]
    with pytest.raises(AssertionError, match=r"caching, call 2 \{'scale': "):
        check_caching(RemembersFirstCall(SUB, subtract), {'sample': 5.0}, calls)


def test_one_row_gives_each_table_a_row_of_the_value() -> None:
    request = one_row(SUB_MULTI, SubParams(sample=5.0, can=1.0))
    assert request.scale == Scale()
    assert (request.sample, request.can) == ([5.0], [1.0])


def test_one_row_gives_a_row_of_none_for_an_optional_value_that_is_none() -> None:
    assert one_row(SUB_MULTI, SubParams(sample=5.0)).can == [None]


@pytest.mark.parametrize('can', [1.0, None])
def test_one_row_passes_if_one_row_per_table_gives_the_single_run_outputs(
    can: float | None,
) -> None:
    check_one_row(
        Calls(SUB, subtract),
        DriverBinding(SUB_MULTI, Sums),
        SubParams(scale=Scale(factor=2.0), sample=5.0, can=can),
    )


def test_one_row_fails_if_the_outputs_differ() -> None:
    with pytest.raises(AssertionError, match=r"one-row, 'diff': 10.0, expected 8.0"):
        check_one_row(
            Calls(SUB, subtract),
            DriverBinding(SUB_MULTI, IgnoresCans),
            SubParams(scale=Scale(factor=2.0), sample=5.0, can=1.0),
        )


Data = NewType('Data', object)
Out = NewType('Out', object)


class DataParams(BaseModel):
    scale: Scale = Field(default_factory=Scale)
    data: Data | None = None


class DataOutputs(BaseModel):
    out: Out


DATA = _spec('data', params=DataParams, outputs=DataOutputs)


def returning(value: Any) -> Calls:
    return Calls(DATA, lambda **_: DataOutputs(out=value))


@pytest.mark.parametrize(
    'make', [lambda v: np.array(v), lambda v: sc.array(dims=['q'], values=v)]
)
def test_arrays_are_compared_up_to_rtol(make: Callable[[list[float]], Any]) -> None:
    def check(changed: float, **rtol: float) -> None:
        check_one_row(
            returning(make([1.0, 2.0])),
            returning(make([1.0, changed])),
            DataParams(),
            **rtol,
        )

    check(2.0 + 1e-14)
    with pytest.raises(AssertionError, match="one-row, 'out'"):
        check(2.0 + 1e-6)
    check(2.0 + 1e-6, rtol=1e-5)


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


def test_assert_close_compares_models_of_one_type_field_by_field() -> None:
    assert_close(SubOutputs(diff=1.0 + 1e-13), SubOutputs(diff=1.0))
    with pytest.raises(AssertionError, match=r"^out, 'diff': 1.1, expected 1.0$"):
        assert_close(SubOutputs(diff=1.1), SubOutputs(diff=1.0), where='out')
    with pytest.raises(AssertionError, match=r'^out: '):
        assert_close({'diff': 1.0}, SubOutputs(diff=1.0), where='out')


def first_half(data: Any, **_: Any) -> DataOutputs:
    """Breaks the promise on memory: returns a view of the value it reads."""
    return DataOutputs(out=data[: len(data) // 2])


def first_half_copied(data: Any, **_: Any) -> DataOutputs:
    return DataOutputs(out=data[: len(data) // 2].copy())


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
    copied, viewed = Calls(DATA, first_half_copied), Calls(DATA, first_half)
    params = DataParams(data=data)
    check_one_row(copied, copied, params)
    with pytest.raises(
        AssertionError, match=r"^one-row, single-run: output \['out'\]\S* shares"
    ):
        check_one_row(viewed, copied, params)


def test_caching_fails_for_a_call_whose_output_shares_a_mask_with_a_value() -> None:
    def masked(data: sc.DataArray, scale: Scale) -> DataOutputs:
        return DataOutputs(
            out=sc.DataArray(data.data * scale.factor, masks=dict(data.masks))
        )

    data = sc.DataArray(
        sc.array(dims=['q'], values=[1.0, 2.0]),
        masks={'m': sc.array(dims=['q'], values=[False, True])},
    )
    shared = r"(?s)^caching, call 1 .*, plain: output \['out'\]\.masks\['m'\]"
    with pytest.raises(AssertionError, match=shared):
        check_caching(Calls(DATA, masked), {'data': data}, [{'scale': Scale()}])
