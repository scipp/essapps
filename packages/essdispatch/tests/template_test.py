# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Templates written with ``Template.of``, the params model's signature."""

from collections.abc import Iterator
from typing import Any, Self

import pytest
from pydantic import BaseModel, Field, model_validator

from ess.dispatch import BLANK, Client, SubmitError, Template, dataset, local
from ess.dispatch.testing import FakeDatasets
from ess.spec import Array, NexusFile, WorkflowSpec


class Run(BaseModel):
    file: NexusFile


class Factor(BaseModel):
    factor: float = 1.0


class ScaleParams(BaseModel):
    sample: Run
    scaling: Factor = Field(default_factory=Factor)


class TotalParams(BaseModel):
    runs: list[Run] = Field(min_length=1)
    scaling: Factor = Field(default_factory=Factor)


class RangeParams(BaseModel):
    low: Factor
    high: Factor = Field(default_factory=Factor)

    @model_validator(mode='after')
    def _ordered(self) -> Self:
        if self.low.factor > self.high.factor:
            raise ValueError('low above high')
        return self


class Value(BaseModel):
    value: Array()  # type: ignore[valid-type]


SCALE = WorkflowSpec(
    name='scale',
    version=1,
    title='scale',
    description='a run scaled',
    params=ScaleParams,
    outputs=Value,
)
TOTAL = WorkflowSpec(
    name='total',
    version=1,
    title='total',
    description='the sum of runs, scaled',
    params=TotalParams,
    outputs=Value,
)
RANGE = WorkflowSpec(
    name='range',
    version=1,
    title='range',
    description='the width of a range',
    params=RangeParams,
    outputs=Value,
)


def scale(sample: dict[str, Any], scaling: dict[str, Any]) -> dict[str, Any]:
    return {'value': sample['file'] * scaling['factor']}


def total(runs: list[dict[str, Any]], scaling: dict[str, Any]) -> dict[str, Any]:
    return {'value': sum(run['file'] for run in runs) * scaling['factor']}


def width(low: dict[str, Any], high: dict[str, Any]) -> dict[str, Any]:
    return {'value': high['factor'] - low['factor']}


@pytest.fixture
def client() -> Iterator[Client]:
    datasets = FakeDatasets(proposal='p1')
    for n in (1, 2):
        datasets.measure(n, float(n))
    with local(
        proposal='p1',
        datasets=datasets,
        bind={SCALE: scale, TOTAL: total, RANGE: width},
    ) as client:
        yield client


def test_a_stage_takes_a_required_group_as_a_blank(client: Client) -> None:
    stage = client.stage(Template.of(SCALE)(sample=BLANK, scaling=Factor(factor=10.0)))
    records = [
        client.compute(stage, {'sample': Run(file=dataset(run=n))}) for n in (1, 2)
    ]
    assert [client.output(r, 'value') for r in records] == [10.0, 20.0]


def test_an_accumulator_takes_a_table_with_a_minimum_length_as_a_blank(
    client: Client,
) -> None:
    acc = client.accumulator(
        Template.of(TOTAL)(runs=BLANK, scaling=Factor(factor=10.0))
    )
    for n in (1, 2):
        acc.push({'runs': Run(file=dataset(run=n))})
    assert client.output(acc, 'value') == 30.0


def test_a_template_equals_the_one_its_constructor_makes_of_the_same_values() -> None:
    factor = Factor(factor=10.0)
    assert Template.of(SCALE)(sample=BLANK, scaling=factor) == Template(
        SCALE, {'scaling': factor}, blanks=('sample',)
    )
    assert Template.of(TOTAL)(scaling=factor, runs=BLANK) == Template(
        TOTAL, {'scaling': factor}, blanks=('runs',)
    )


def test_the_blanks_are_in_the_order_of_the_params_model_s_fields() -> None:
    template = Template.of(SCALE)(scaling=BLANK, sample=BLANK)
    assert template.blanks == ('sample', 'scaling')


def test_the_params_model_s_own_validators_run_on_the_requests_of_a_template(
    client: Client,
) -> None:
    # The default of high, 1.0, is below low; the blank is not.
    stage = client.stage(Template.of(RANGE)(low=Factor(factor=2.0), high=BLANK))
    record = client.compute(stage, {'high': Factor(factor=5.0)})
    assert client.output(record, 'value') == 3.0
    with pytest.raises(SubmitError, match='low above high'):
        client.submit(stage, {'high': Factor(factor=1.0)})


def test_a_misspelt_parameter_is_refused() -> None:
    with pytest.raises(
        ValueError, match='samples: not a parameter; sample: Field required'
    ):
        Template.of(SCALE)(samples=BLANK)  # type: ignore[call-arg]


def test_a_value_of_the_wrong_type_is_refused() -> None:
    with pytest.raises(
        ValueError, match=r'scaling\.factor: Input should be a valid number'
    ):
        Template.of(SCALE)(sample=BLANK, scaling={'factor': 'x'})  # type: ignore[arg-type]


def test_parameters_given_by_position_are_refused() -> None:
    with pytest.raises(TypeError, match='by name'):
        Template.of(SCALE)(BLANK)  # type: ignore[call-arg]
