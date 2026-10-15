# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Templates by side, the stages and accumulators made from them, and pushes of
the columns of a table.
"""

import math
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NewType, Self

import pytest
from pydantic import BaseModel, Field, model_validator

from ess.dispatch import Client, SubmitError, Template, dataset, local
from ess.dispatch.testing import FakeDatasets, StagedFunction, kept_rows, read_dataset
from ess.spec import NoInputs, WorkflowSpec

SampleFile = NewType('SampleFile', Path)


class Factor(BaseModel):
    factor: float = 1.0


class Weight(BaseModel):
    value: float = 1.0


class ScaleParams(BaseModel):
    scaling: Factor = Field(default_factory=Factor)
    weight: Weight | None = None


class ScaleInputs(BaseModel):
    sample: SampleFile


class Value(BaseModel):
    value: float


class Low(BaseModel):
    value: float


class High(BaseModel):
    value: float = 1.0


class RangeParams(BaseModel):
    low: Low
    high: High = Field(default_factory=High)

    @model_validator(mode='after')
    def _ordered(self) -> Self:
        if self.low.value > self.high.value:
            raise ValueError('low above high')
        return self


SCALE = WorkflowSpec(
    name='scale',
    version=1,
    title='scale',
    description='a run scaled',
    params=ScaleParams,
    inputs=ScaleInputs,
    outputs=Value,
)
TOTAL = SCALE.tabulate(
    name='total', version=1, tables={'sample': ('weight',), 'scaling': ()}
)
RANGE = WorkflowSpec(
    name='range',
    version=1,
    title='range',
    description='the width of a range',
    params=RangeParams,
    outputs=Value,
)


def scale(params: ScaleParams, inputs: ScaleInputs) -> Value:
    weight = 1.0 if params.weight is None else params.weight.value
    return Value(value=read_dataset(inputs.sample) * params.scaling.factor * weight)


def total(params: Any, inputs: Any) -> Value:
    weights = [1.0 if w is None else w.value for w in params.weight]
    runs = [read_dataset(path) for path in inputs.sample]
    summed = sum(w * x for w, x in zip(weights, runs, strict=True))
    return Value(value=summed * math.prod(f.factor for f in params.scaling))


def width(params: RangeParams, inputs: NoInputs) -> Value:
    return Value(value=params.high.value - params.low.value)


@pytest.fixture
def datasets(tmp_path: Path) -> FakeDatasets:
    datasets = FakeDatasets(proposal='p1', directory=tmp_path)
    for n in (1, 2):
        datasets.measure(n, float(n))
    return datasets


@pytest.fixture
def client(datasets: FakeDatasets) -> Iterator[Client]:
    with local(
        proposal='p1',
        datasets=datasets,
        bind=[
            StagedFunction(SCALE, scale),
            kept_rows(TOTAL, total),
            StagedFunction(RANGE, width),
        ],
    ) as client:
        yield client


# Templates and stages


def test_a_stage_takes_a_required_input_as_a_blank(client: Client) -> None:
    stage = client.stage(
        Template(SCALE, params={'scaling': Factor(factor=10.0)}, blanks=('sample',))
    )
    records = [client.compute(stage, inputs={'sample': dataset(run=n)}) for n in (1, 2)]

    assert [client.output(r, 'value') for r in records] == [10.0, 20.0]


def test_a_call_through_a_stage_records_the_plain_request(client: Client) -> None:
    tune = client.stage(
        Template(SCALE, inputs={'sample': dataset(run=2)}, blanks=('scaling',))
    )
    tuned = client.compute(tune, params={'scaling': Factor(factor=3.0)})
    plain = client.compute(
        SCALE,
        params={'scaling': Factor(factor=3.0)},
        inputs={'sample': dataset(run=2)},
    )

    assert tuned.request == plain.request
    assert client.output(tuned, 'value') == client.output(plain, 'value') == 6.0


def test_the_params_model_s_own_validators_run_on_the_requests_of_a_template(
    client: Client,
) -> None:
    # The default of high, 1.0, is below low; the blank is not.
    stage = client.stage(
        Template(RANGE, params={'low': Low(value=2.0)}, blanks=('high',))
    )
    record = client.compute(stage, params={'high': High(value=5.0)})

    assert client.output(record, 'value') == 3.0
    with pytest.raises(SubmitError, match='low above high'):
        client.submit(stage, params={'high': High(value=1.0)})


def test_a_template_drops_the_values_given_for_its_blanks() -> None:
    factor = Factor(factor=10.0)

    assert Template(
        SCALE,
        params={'scaling': factor},
        inputs={'sample': dataset(run=1)},
        blanks=('sample',),
    ) == Template(SCALE, params={'scaling': {'factor': 10.0}}, blanks=('sample',))


def test_a_fill_gives_each_blank_and_no_fixed_value() -> None:
    template = Template(SCALE, params={'scaling': Factor()}, blanks=('sample',))

    with pytest.raises(ValueError, match=r"missing \['sample'\]"):
        template.fill()
    with pytest.raises(ValueError, match='fixed by the template'):
        template.fill(params={'scaling': Factor()}, inputs={'sample': dataset(run=1)})


def test_a_fixed_value_is_checked_by_its_own_field(client: Client) -> None:
    with pytest.raises(SubmitError, match=r'scaling\.factor: Input should be a valid'):
        client.stage(
            Template(SCALE, params={'scaling': {'factor': 'x'}}, blanks=('sample',))
        )


def test_a_misspelt_name_is_refused(client: Client) -> None:
    with pytest.raises(SubmitError, match=r"\['sampel'\]: not parameters or inputs"):
        client.stage(
            Template(SCALE, inputs={'sampel': dataset(run=1)}, blanks=('scaling',))
        )
    with pytest.raises(SubmitError, match='sampel: a blank, not a parameter'):
        client.stage(Template(SCALE, blanks=('sampel',)))


def test_a_required_value_neither_fixed_nor_blank_is_refused(client: Client) -> None:
    with pytest.raises(SubmitError, match=r"\['sample'\]: neither fixed nor blank"):
        client.stage(Template(SCALE, blanks=('scaling',)))


# Accumulators and pushes


def test_a_push_gives_an_index_and_the_columns_attached_to_it(
    client: Client, datasets: FakeDatasets
) -> None:
    acc = client.accumulator(
        Template(TOTAL, params={'scaling': [Factor(factor=10.0)]}, blanks=('sample',))
    )
    acc.push(sample=[dataset(run=1)])
    acc.push(sample=[dataset(run=2)], weight=[Weight(value=2.0)])

    assert client.output(acc, 'value') == 50.0
    request = client.provenance(acc).request
    assert request.params['weight'] == [None, {'value': 2.0}]
    assert request.datasets() == [datasets.resolve(dataset(run=n)) for n in (1, 2)]


def test_an_index_may_be_a_parameter(client: Client) -> None:
    acc = client.accumulator(
        Template(TOTAL, inputs={'sample': [dataset(run=2)]}, blanks=('scaling',))
    )
    acc.push(scaling=[Factor(factor=3.0), Factor(factor=5.0)])

    assert client.output(acc, 'value') == 30.0
    with pytest.raises(SubmitError, match=r'scaling\[2\]: repeats scaling\[1\]'):
        acc.push(scaling=[{'factor': 5.0}])


def test_a_push_refuses_an_index_value_the_table_holds(
    client: Client, datasets: FakeDatasets
) -> None:
    acc = client.accumulator(Template(TOTAL, blanks=('sample', 'scaling')))
    acc.push(sample=[dataset(run=1)])
    path = datasets.path(datasets.resolve(dataset(run=1)))

    with pytest.raises(SubmitError, match=r'sample\[1\]: repeats sample\[0\]'):
        acc.push(sample=[dataset(path=path)])
    with pytest.raises(SubmitError, match=r'sample\[2\]: repeats sample\[1\]'):
        acc.push(sample=[dataset(run=2), dataset(run=2)])


def test_a_push_refuses_a_column_without_its_index(client: Client) -> None:
    acc = client.accumulator(Template(TOTAL, blanks=('sample',)))

    with pytest.raises(SubmitError, match='pushed without sample'):
        acc.push(weight=[Weight()])


def test_a_push_refuses_a_table_the_template_fixes(client: Client) -> None:
    acc = client.accumulator(Template(TOTAL, blanks=('sample',)))

    with pytest.raises(SubmitError, match="not 'scaling'"):
        acc.push(scaling=[Factor()])


def test_a_push_of_no_rows_is_refused(client: Client) -> None:
    acc = client.accumulator(Template(TOTAL, blanks=('sample',)))

    with pytest.raises(SubmitError, match='no rows'):
        acc.push(sample=[])
    with pytest.raises(SubmitError, match='one table'):
        acc.push()


def test_a_push_checks_each_literal_by_its_column_s_type(client: Client) -> None:
    acc = client.accumulator(Template(TOTAL, blanks=('sample',)))

    with pytest.raises(SubmitError, match=r'weight\.0\.value'):
        acc.push(sample=[dataset(run=1)], weight=[{'value': 'heavy'}])


def test_a_state_with_no_row_in_a_required_index_cannot_be_read(
    client: Client,
) -> None:
    acc = client.accumulator(Template(TOTAL, blanks=('sample',)))

    with pytest.raises(LookupError, match='sample: the table has no row'):
        client.output(acc, 'value')


def test_a_template_leaves_a_whole_table_blank(client: Client) -> None:
    with pytest.raises(
        SubmitError, match=r"tables, by their indexes .*, not \['weight'\]"
    ):
        client.accumulator(Template(TOTAL, blanks=('weight',)))
    with pytest.raises(SubmitError, match='columns of a blank table'):
        client.accumulator(
            Template(TOTAL, params={'weight': [Weight()]}, blanks=('sample',))
        )


def test_a_push_whose_file_cannot_be_read_stops_the_accumulator(
    client: Client, datasets: FakeDatasets
) -> None:
    acc = client.accumulator(Template(TOTAL, blanks=('sample',)))
    datasets.corrupt(datasets.resolve(dataset(run=1)))
    acc.push(sample=[dataset(run=1)])

    with pytest.raises(LookupError, match='push 0 failed: file signature not found'):
        client.output(acc, 'value')
