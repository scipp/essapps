# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from pydantic import BaseModel, Field

from ess.spec import (
    AccumulatorBinding,
    Binding,
    DriverBinding,
    StageBinding,
    WorkflowSpec,
    function_binding,
)


class Part(BaseModel):
    value: float


class Scale(BaseModel):
    factor: float = 2.0


class SumParams(BaseModel):
    parts: list[Part]
    extra: list[Part] = Field(default_factory=list)
    scale: Scale = Field(default_factory=Scale)


class Total(BaseModel):
    total: float


SUM = WorkflowSpec(
    name='sum',
    version=1,
    title='sum',
    description='sum',
    params=SumParams,
    outputs=Total,
)


class Recording:
    """Sums the values pushed, times the scale; ``made`` and ``pushes`` record calls."""

    made: list[Mapping[str, Any]]
    pushes: list[tuple[str, list[Any]]]

    def __init__(self, fixed: Mapping[str, Any]) -> None:
        Recording.made.append(fixed)
        self._factor = fixed['scale']['factor']
        self._total = 0.0

    def push(self, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
        Recording.pushes.append((table, list(rows)))
        self._total += sum(row['value'] for row in rows)

    def outputs(self) -> Mapping[str, Any]:
        return {'total': self._factor * self._total}


@pytest.fixture
def binding() -> DriverBinding:
    Recording.made, Recording.pushes = [], []
    return DriverBinding(SUM, Recording)


def test_a_driver_is_made_from_the_fixed_values_that_are_not_tables(
    binding: DriverBinding,
) -> None:
    binding.make_accumulator({'scale': {'factor': 3.0}}, ('parts', 'extra'))
    assert Recording.made == [{'scale': {'factor': 3.0}}]
    assert Recording.pushes == []


def test_each_fixed_table_is_one_push_also_without_rows(
    binding: DriverBinding,
) -> None:
    driver = binding.make_accumulator({'parts': [{'value': 1.0}, {'value': 2.0}]}, ())
    assert Recording.made == [{'scale': {'factor': 2.0}}]
    assert Recording.pushes == [
        ('parts', [{'value': 1.0}, {'value': 2.0}]),
        ('extra', []),
    ]
    assert driver.outputs() == {'total': 6.0}


def test_a_plain_request_is_an_accumulator_without_blanks_read_once(
    binding: DriverBinding,
) -> None:
    assert binding.compute({'parts': [{'value': 1.0}], 'extra': [{'value': 3.0}]}) == {
        'total': 8.0
    }


@pytest.mark.parametrize(
    ('fixed', 'blanks', 'reason'),
    [
        (
            {},
            ('scale', 'parts'),
            r"\['scale'\]: the blanks of an accumulator are tables",
        ),
        ({}, ('runs',), r"\['runs'\]: the blanks of an accumulator are tables"),
        ({'speed': 1.0}, ('parts',), r"\['speed'\]: not parameters of sum"),
        ({}, ('extra',), r"\['parts'\]: neither fixed nor blank"),
        ({'parts': []}, ('parts',), r"\['parts'\]: both fixed and blank"),
    ],
)
def test_an_accumulator_refuses_values_that_do_not_fit(
    binding: DriverBinding,
    fixed: dict[str, Any],
    blanks: tuple[str, ...],
    reason: str,
) -> None:
    with pytest.raises(ValueError, match=reason):
        binding.make_accumulator(fixed, blanks)


def test_a_function_gets_every_parameter_by_name_defaults_filled_in() -> None:
    def total(
        parts: list[dict[str, float]],
        extra: list[dict[str, float]],
        scale: dict[str, float],
    ) -> dict[str, float]:
        return {'total': scale['factor'] * sum(p['value'] for p in [*parts, *extra])}

    binding = function_binding(SUM, total)
    assert binding.spec is SUM
    assert binding.compute({'parts': [{'value': 1.0}]}) == {'total': 2.0}


def test_each_binding_serves_the_protocols_of_its_strategy(
    binding: DriverBinding,
) -> None:
    function = function_binding(SUM, lambda **values: {'total': 0.0})
    assert isinstance(binding, AccumulatorBinding)
    assert not isinstance(binding, StageBinding)
    assert isinstance(function, Binding)
    assert not isinstance(function, StageBinding | AccumulatorBinding)
