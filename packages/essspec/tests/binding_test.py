# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from collections.abc import Mapping, Sequence
from typing import Any, NewType

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
from ess.spec.binding import fixed_values


class Scale(BaseModel):
    factor: float = 2.0


class Weight(BaseModel):
    w: float


SampleValue = NewType('SampleValue', float)
CanValue = NewType('CanValue', float)
Diff = NewType('Diff', float)


class SubParams(BaseModel):
    scale: Scale = Field(default_factory=Scale)
    weight: Weight | None = None


class SubInputs(BaseModel):
    sample: SampleValue
    can: CanValue | None = None


class SubOutputs(BaseModel):
    diff: Diff


SUB = WorkflowSpec(
    name='sub',
    version=1,
    title='Subtract',
    description='The scaled, weighted sample minus the can.',
    params=SubParams,
    inputs=SubInputs,
    outputs=SubOutputs,
)
SUB_MULTI = SUB.tabulate(
    name='sub-multi', version=1, tables={'sample': ('weight',), 'can': ()}
)


def subtract(params: SubParams, inputs: SubInputs) -> SubOutputs:
    weight = params.weight.w if params.weight else 1.0
    total = weight * inputs.sample - (inputs.can or 0.0)
    return SubOutputs(diff=params.scale.factor * total)


class Recorder:
    """Makes drivers of ``SUB_MULTI``; records what they are made with and pushed."""

    def __init__(self) -> None:
        self.made: list[Mapping[str, Any]] = []
        self.pushes: list[tuple[str, Mapping[str, Sequence[Any]]]] = []

    def make_driver(self, fixed: Mapping[str, Any]) -> 'Subtracting':
        self.made.append(fixed)
        return Subtracting(self, fixed['scale'].factor)


class Subtracting:
    def __init__(self, recorder: Recorder, factor: float) -> None:
        self._recorder = recorder
        self._factor = factor
        self._total = 0.0

    def push(self, table: str, columns: Mapping[str, Sequence[Any]]) -> None:
        self._recorder.pushes.append((table, columns))
        if table == 'sample':
            rows = zip(columns['sample'], columns['weight'], strict=True)
            self._total += sum(s * (w.w if w else 1.0) for s, w in rows)
        else:
            self._total -= sum(columns['can'])

    def outputs(self) -> SubOutputs:
        return SubOutputs(diff=self._factor * self._total)


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def binding(recorder: Recorder) -> DriverBinding:
    return DriverBinding(SUB_MULTI, recorder.make_driver)


SAMPLE_TABLE = {'sample': [5.0, 7.0], 'weight': [None, Weight(w=2.0)]}


def test_fixed_values_fill_in_defaults_and_a_table_without_rows() -> None:
    assert fixed_values(SUB_MULTI, {'scale': Scale(factor=3.0)}, ('sample',)) == {
        'scale': Scale(factor=3.0),
        'can': [],
    }
    assert fixed_values(SUB, {'sample': 5.0}, ('weight',)) == {
        'scale': Scale(),
        'can': None,
        'sample': 5.0,
    }


def test_fixed_values_take_a_table_as_its_columns() -> None:
    assert fixed_values(SUB_MULTI, SAMPLE_TABLE, ('can',)) == {
        'scale': Scale(),
        **SAMPLE_TABLE,
    }


@pytest.mark.parametrize(
    ('fixed', 'blanks', 'reason'),
    [
        (SAMPLE_TABLE, ('sample',), r"\['sample', 'weight'\]: both fixed and blank"),
        ({'weight': [None]}, ('sample',), r"\['weight'\]: both fixed and blank"),
        ({'speed': 1.0}, ('sample',), r"\['speed'\]: not parameters or inputs"),
        ({}, ('weight',), r"\['weight'\]: not parameters, inputs, or tables"),
        ({}, ('can',), r"\['sample'\]: neither fixed nor blank"),
        ({'sample': [5.0]}, ('can',), r"sample: a table is fixed whole, \['weight'\]"),
        (
            {'sample': [5.0], 'weight': [None, None]},
            ('can',),
            'sample: the columns of a table have equal length',
        ),
    ],
)
def test_fixed_values_refuse_names_that_do_not_fit(
    fixed: dict[str, Any], blanks: tuple[str, ...], reason: str
) -> None:
    with pytest.raises(ValueError, match=reason):
        fixed_values(SUB_MULTI, fixed, blanks)


def test_a_driver_is_made_from_the_fixed_values_that_are_not_tables(
    binding: DriverBinding, recorder: Recorder
) -> None:
    binding.make_accumulator({'scale': Scale(factor=3.0)}, ('sample', 'can'))
    assert recorder.made == [{'scale': Scale(factor=3.0)}]
    assert recorder.pushes == []


def test_each_fixed_table_with_rows_is_one_push_of_its_columns(
    binding: DriverBinding, recorder: Recorder
) -> None:
    driver = binding.make_accumulator(SAMPLE_TABLE, ('can',))
    assert recorder.pushes == [('sample', SAMPLE_TABLE)]
    driver.push('can', {'can': [1.0]})
    assert driver.outputs() == SubOutputs(diff=2.0 * (5.0 + 14.0 - 1.0))


def test_a_plain_request_is_an_accumulator_without_blanks_read_once(
    binding: DriverBinding, recorder: Recorder
) -> None:
    params = SUB_MULTI.params(weight=SAMPLE_TABLE['weight'])
    inputs = SUB_MULTI.inputs(sample=SAMPLE_TABLE['sample'], can=[1.0])
    assert binding.compute(params, inputs) == SubOutputs(diff=36.0)
    assert recorder.pushes == [('sample', SAMPLE_TABLE), ('can', {'can': [1.0]})]


@pytest.mark.parametrize(
    ('fixed', 'blanks', 'reason'),
    [
        ({}, ('scale', 'sample'), r"\['scale'\]: the blanks of an accumulator are"),
        ({}, ('runs',), r"\['runs'\]: the blanks of an accumulator are tables"),
        ({}, ('can',), r"\['sample'\]: neither fixed nor blank"),
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


def test_a_function_gets_the_params_and_the_inputs_as_model_instances() -> None:
    binding = function_binding(SUB, subtract)
    assert binding.spec is SUB
    params = SubParams(weight=Weight(w=2.0))
    assert binding.compute(params, SubInputs(sample=5.0, can=1.0)) == SubOutputs(
        diff=18.0
    )


def test_each_binding_serves_the_protocols_of_its_strategy(
    binding: DriverBinding,
) -> None:
    function = function_binding(SUB, subtract)
    assert isinstance(binding, AccumulatorBinding)
    assert not isinstance(binding, StageBinding)
    assert isinstance(function, Binding)
    assert not isinstance(function, StageBinding | AccumulatorBinding)
