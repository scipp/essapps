# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Templates, the stages and accumulators made from them, and pushes of the
rows of a table.
"""

import math
import re
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, NewType, Self

import pytest
from pydantic import AfterValidator, BaseModel, Field, model_validator

from ess.dispatch import (
    Client,
    FieldError,
    NotReadable,
    Status,
    SubmitError,
    Template,
    dataset,
    local,
)
from ess.dispatch.testing import FakeDatasets, StagedFunction, kept_rows, read_dataset
from ess.spec import DriverBinding, NoParams, StageCall, WorkflowSpec, function_binding

SampleFile = NewType('SampleFile', Path)
PositiveValue = NewType('PositiveValue', float)


class Factor(BaseModel):
    factor: float = 1.0


class Weight(BaseModel):
    value: float = 1.0


class WeightedRun(BaseModel):
    """A run and its weight, if given: a group that holds a file."""

    file: SampleFile
    weight: Weight | None = None


class ScaleParams(BaseModel):
    scaling: Factor | None = None
    sample: WeightedRun


class TotalParams(BaseModel):
    scaling: list[Factor | None]
    sample: list[WeightedRun] = Field(min_length=1)


class Value(BaseModel):
    value: float


def _positive(value: float) -> float:
    if value <= 0:
        raise ValueError('not positive')
    return value


class PositiveParams(BaseModel):
    positive: Annotated[PositiveValue, AfterValidator(_positive)]


class PositivesParams(BaseModel):
    positive: list[Annotated[PositiveValue, AfterValidator(_positive)]] = Field(
        min_length=1
    )


class Made(BaseModel):
    made: PositiveValue


class BoundsParams(BaseModel):
    """Params whose model validator checks two values that are not tables."""

    low: float = 0.0
    high: float = 1.0
    sample: list[SampleFile]

    @model_validator(mode='after')
    def _ordered(self) -> Self:
        if self.low >= self.high:
            raise ValueError('low is not below high')
        return self


class OneRowParams(BaseModel):
    """Params whose model validator reads a table: a check across rows."""

    sample: list[SampleFile]

    @model_validator(mode='after')
    def _one_row(self) -> Self:
        if len(self.sample) > 1:
            raise ValueError('more than one sample')
        return self


class CopiedParams(BaseModel):
    """Params whose model validator returns a copy, not the instance it checks."""

    sample: list[SampleFile]

    @model_validator(mode='after')
    def _copied(self) -> Self:
        return self.model_copy()


def _spec(
    name: str,
    *,
    params: type[BaseModel] = NoParams,
    outputs: type[BaseModel] = Value,
    tables: tuple[str, ...] = (),
) -> WorkflowSpec[Any, Any]:
    return WorkflowSpec(
        name=name,
        version=1,
        title=name,
        description=name,
        params=params,
        outputs=outputs,
        tables=tables,
    )


SCALE = _spec('scale', params=ScaleParams)
TOTAL = _spec('total', params=TotalParams, tables=('sample', 'scaling'))
MAKE = _spec('make', outputs=Made)
USE = _spec('use', params=PositiveParams)
USE_MANY = _spec('use-many', params=PositivesParams, tables=('positive',))
BOUNDS = _spec('bounds', params=BoundsParams, tables=('sample',))
ONE_ROW = _spec('one-row', params=OneRowParams, tables=('sample',))
COPIED = _spec('copied', params=CopiedParams, tables=('sample',))
UNSCALED = Template(TOTAL, params={'scaling': []}, blanks=('sample',))
"""The sample runs of ``TOTAL`` as a blank table, without scaling."""


def scale(params: ScaleParams) -> Value:
    factor = 1.0 if params.scaling is None else params.scaling.factor
    return Value(value=_weighted(params.sample) * factor)


def total(params: TotalParams) -> Value:
    summed = sum(_weighted(run) for run in params.sample)
    return Value(value=summed * math.prod(f.factor for f in params.scaling))


def _weighted(run: WeightedRun) -> float:
    return read_dataset(run.file) * (1.0 if run.weight is None else run.weight.value)


def make(params: NoParams) -> Made:
    return Made(made=-1.0)


def use(params: PositiveParams) -> Value:
    return Value(value=params.positive)


def use_many(params: PositivesParams) -> Value:
    return Value(value=sum(params.positive))


class Count:
    """A driver that counts the rows pushed."""

    def __init__(self, fixed: Mapping[str, Any]) -> None:
        self.rows = 0

    def push(self, table: str, rows: Sequence[Any]) -> None:
        self.rows += len(rows)

    def outputs(self) -> Value:
        return Value(value=self.rows)


@dataclass(frozen=True)
class Counting(DriverBinding):
    """A ``DriverBinding`` that makes stages too: each call a new driver."""

    def make_stage(
        self, fixed: Mapping[str, Any], blanks: Collection[str]
    ) -> StageCall:
        return lambda given: self.make_accumulator({**fixed, **given}, ()).outputs()


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
            function_binding(MAKE, make),
            StagedFunction(USE, use),
            kept_rows(USE_MANY, use_many),
            Counting(BOUNDS, Count),
            Counting(ONE_ROW, Count),
            Counting(COPIED, Count),
        ],
    ) as client:
        yield client


# Templates and stages


def test_a_stage_takes_a_required_value_as_a_blank(client: Client) -> None:
    stage = client.stage(
        Template(SCALE, params={'scaling': Factor(factor=10.0)}, blanks=('sample',))
    )
    records = [client.compute(stage, params={'sample': dataset(run=n)}) for n in (1, 2)]

    assert [client.output(r, 'value') for r in records] == [10.0, 20.0]


def test_a_call_through_a_stage_records_the_plain_request(client: Client) -> None:
    tune = client.stage(
        Template(SCALE, params={'sample': dataset(run=2)}, blanks=('scaling',))
    )
    tuned = client.compute(tune, params={'scaling': Factor(factor=3.0)})
    plain = client.compute(
        SCALE,
        params={'scaling': Factor(factor=3.0), 'sample': dataset(run=2)},
    )

    assert tuned.request == plain.request
    assert client.output(tuned, 'value') == client.output(plain, 'value') == 6.0


def test_a_template_drops_the_values_given_for_its_blanks() -> None:
    factor = Factor(factor=10.0)

    assert Template(
        SCALE,
        params={'scaling': factor, 'sample': dataset(run=1)},
        blanks=('sample',),
    ) == Template(SCALE, params={'scaling': {'factor': 10.0}}, blanks=('sample',))


def test_a_fill_gives_each_blank_and_no_fixed_value() -> None:
    template = Template(SCALE, params={'scaling': Factor()}, blanks=('sample',))

    with pytest.raises(SubmitError, match=r'^sample: a blank, not filled$') as raised:
        template.fill()
    assert raised.value.errors == (
        FieldError(None, ('sample',), 'a blank, not filled'),
    )
    with pytest.raises(SubmitError, match='fixed by the template') as raised:
        template.fill(params={'scaling': Factor(), 'sample': dataset(run=1)})
    assert [e.loc for e in raised.value.errors] == [('scaling',)]


def test_a_call_through_a_stage_without_its_blank_is_refused(client: Client) -> None:
    stage = client.stage(Template(SCALE, blanks=('sample',)))

    with pytest.raises(SubmitError, match=r'^sample: a blank, not filled$'):
        client.submit(stage)


def test_a_fixed_value_is_checked_by_its_own_field(client: Client) -> None:
    with pytest.raises(SubmitError, match=r'scaling\.factor: Input should be a valid'):
        client.stage(
            Template(SCALE, params={'scaling': {'factor': 'x'}}, blanks=('sample',))
        )


def test_a_misspelt_name_is_refused(client: Client) -> None:
    with pytest.raises(SubmitError, match='sampel: not a parameter of'):
        client.stage(
            Template(SCALE, params={'sampel': dataset(run=1)}, blanks=('scaling',))
        )
    with pytest.raises(SubmitError, match='sampel: not a parameter of'):
        client.stage(Template(SCALE, blanks=('sampel',)))


def test_a_required_value_neither_fixed_nor_blank_is_refused(client: Client) -> None:
    with pytest.raises(SubmitError, match=r'^sample: a required parameter, not given$'):
        client.stage(Template(SCALE, blanks=('scaling',)))


# Accumulators and pushes


def test_a_push_gives_rows_of_a_table_each_a_group_or_a_dataset_for_it(
    client: Client, datasets: FakeDatasets
) -> None:
    acc = client.accumulator(
        Template(TOTAL, params={'scaling': [Factor(factor=10.0)]}, blanks=('sample',))
    )
    acc.push(sample=[dataset(run=1)])
    acc.push(sample=[{'file': dataset(run=2), 'weight': Weight(value=2.0)}])

    assert client.output(acc, 'value') == 50.0
    request = client.provenance(acc).request
    assert [row['weight'] for row in request.params['sample']] == [None, {'value': 2.0}]
    assert request.datasets() == [datasets.resolve(dataset(run=n)) for n in (1, 2)]


def test_a_table_may_hold_values_that_are_not_files(client: Client) -> None:
    acc = client.accumulator(
        Template(TOTAL, params={'sample': [dataset(run=2)]}, blanks=('scaling',))
    )
    acc.push(scaling=[Factor(factor=3.0), Factor(factor=5.0)])

    assert client.output(acc, 'value') == 30.0
    with pytest.raises(SubmitError, match=r'scaling\[2\]: repeats scaling\[1\]'):
        acc.push(scaling=[{'factor': 5.0}])


def test_a_push_refuses_a_row_the_table_holds(
    client: Client, datasets: FakeDatasets
) -> None:
    acc = client.accumulator(Template(TOTAL, blanks=('sample', 'scaling')))
    acc.push(sample=[dataset(run=1)])
    path = datasets.path(datasets.resolve(dataset(run=1)))

    with pytest.raises(SubmitError, match=r'sample\[1\]: repeats sample\[0\]'):
        acc.push(sample=[dataset(path=path)])
    with pytest.raises(SubmitError, match=r'sample\[2\]: repeats sample\[1\]'):
        acc.push(sample=[dataset(run=2), dataset(run=2)])


def test_rows_are_distinct_as_a_whole(client: Client) -> None:
    acc = client.accumulator(Template(TOTAL, blanks=('sample', 'scaling')))
    weighted = {'file': dataset(run=1), 'weight': Weight(value=2.0)}
    acc.push(sample=[dataset(run=1), weighted])  # the same file, another weight

    assert client.output(acc, 'value') == 3.0
    with pytest.raises(SubmitError, match=r'sample\[2\]: repeats sample\[1\]'):
        acc.push(sample=[weighted])
    with pytest.raises(SubmitError, match=r'sample\[2\]: repeats sample\[0\]'):
        acc.push(sample=[{'file': dataset(run=1)}])  # as row 0, a bare dataset


def test_a_push_gives_rows_of_blank_tables(client: Client) -> None:
    acc = client.accumulator(UNSCALED)
    row = [dataset(run=1)]
    fixed = r"^scaling: not a blank table of the accumulator, \['sample'\]$"
    refused = [
        (fixed, {'scaling': [Factor()]}),
        (fixed, {'sample': row, 'scaling': [Factor()]}),
        (
            r"^other: not a blank table of the accumulator, \['sample'\]$",
            {'other': row},
        ),
        ('^sample: a push gives no rows$', {'sample': []}),
        ('a push gives rows of a blank table, not none', {}),
    ]
    for reason, rows in refused:
        with pytest.raises(SubmitError, match=reason):
            acc.push(**rows)


def test_a_state_with_no_row_in_a_table_that_needs_one_cannot_be_read(
    client: Client,
) -> None:
    acc = client.accumulator(UNSCALED)

    with pytest.raises(
        LookupError, match='sample: the table has no row; it needs at least 1'
    ):
        client.output(acc, 'value')


def test_the_blanks_of_an_accumulator_are_tables(client: Client) -> None:
    with pytest.raises(
        SubmitError,
        match=r"the blanks of an accumulator are tables, \['sample', 'scaling'\], "
        r"not \['weight'\]",
    ):
        client.accumulator(Template(TOTAL, blanks=('weight',)))


def test_the_table_rules_hold_on_the_fixed_tables_of_a_template(
    client: Client,
) -> None:
    run = [dataset(run=1)]
    refused = {
        r'sample\[1\]: repeats sample\[0\]': Template(
            TOTAL, params={'sample': run * 2}, blanks=('scaling',)
        ),
        'sample: the table has no row': Template(
            TOTAL, params={'sample': []}, blanks=('scaling',)
        ),
    }
    for reason, template in refused.items():
        with pytest.raises(SubmitError, match=reason):
            client.accumulator(template)


def test_a_table_that_may_be_empty_is_given_without_rows(client: Client) -> None:
    plain = client.compute(TOTAL, params={'sample': [dataset(run=1)], 'scaling': []})
    acc = client.accumulator(UNSCALED)
    acc.push(sample=[dataset(run=1)])

    assert client.provenance(acc).request == plain.request
    assert client.output(acc, 'value') == client.output(plain, 'value') == 1.0
    with pytest.raises(SubmitError, match='scaling: a required parameter, not given'):
        client.accumulator(Template(TOTAL, blanks=('sample',)))


def test_a_push_whose_file_cannot_be_read_fails_alone(
    client: Client, datasets: FakeDatasets
) -> None:
    acc = client.accumulator(UNSCALED)
    datasets.corrupt(datasets.resolve(dataset(run=1)))
    bad = acc.push(sample=[dataset(run=1)])

    assert client.wait(bad) == Status.FAILED
    assert client.failure(bad) == 'push 1 failed: file signature not found'
    with pytest.raises(NotReadable, match='sample: the table has no row'):
        client.output(acc, 'value')  # state 1 is state 0
    acc.push(sample=[dataset(run=2)])
    assert client.output(acc, 'value') == 2.0


def test_a_data_value_that_fails_its_type_s_check_fails_where_it_is_read(
    client: Client,
) -> None:
    made = client.compute(MAKE).ref('made')
    stage = client.stage(Template(USE, params={'positive': made}))
    acc = client.accumulator(Template(USE_MANY, blanks=('positive',)))
    pushed = acc.push(positive=[made])

    assert 'not positive' in client.failure(
        client.compute(USE, params={'positive': made})
    )
    assert 'not positive' in client.failure(client.compute(stage))
    assert client.wait(pushed) == Status.FAILED
    assert re.match(r'push 1 failed: (?s:.*)not positive', client.failure(pushed))
    assert client.wait(acc.push(positive=[made])) == Status.FAILED  # again


# Model validators


def test_a_model_validator_that_raises_fails_a_plain_request(client: Client) -> None:
    ordered = client.compute(BOUNDS, params={'sample': [dataset(run=1)]})
    unordered = client.compute(BOUNDS, params={'low': 2.0, 'sample': []})

    assert client.output(ordered, 'value') == 1.0
    assert client.failure(unordered) == 'BoundsParams._ordered: low is not below high'


def test_a_model_validator_checks_each_call_through_a_stage_with_the_fixed_values(
    client: Client,
) -> None:
    stage = client.stage(
        Template(BOUNDS, params={'low': 2.0, 'sample': []}, blanks=('high',))
    )
    above = client.compute(stage, params={'high': 3.0})
    below = client.compute(stage, params={'high': 1.0})
    again = client.compute(stage, params={'high': 4.0})

    assert client.output(above, 'value') == 0.0
    assert client.failure(below) == 'BoundsParams._ordered: low is not below high'
    assert client.output(again, 'value') == 0.0


def test_a_model_validator_checks_an_accumulator_when_it_opens(client: Client) -> None:
    ordered = client.accumulator(
        Template(BOUNDS, params={'low': 0.5}, blanks=('sample',))
    )
    unordered = client.accumulator(
        Template(BOUNDS, params={'low': 2.0}, blanks=('sample',))
    )
    ordered.push(sample=[dataset(run=1)])

    assert client.output(ordered, 'value') == 1.0
    with pytest.raises(
        NotReadable, match=r'opening failed: BoundsParams\._ordered: low is not below'
    ):
        client.output(unordered, 'value')


def test_an_accumulator_whose_model_validator_reads_a_table_fails_to_open(
    client: Client,
) -> None:
    plain = client.compute(ONE_ROW, params={'sample': [dataset(run=1)]})
    acc = client.accumulator(Template(ONE_ROW, blanks=('sample',)))

    assert client.output(plain, 'value') == 1.0
    with pytest.raises(
        NotReadable,
        match=r"opening failed: OneRowParams._one_row reads the table sample, "
        r".*; checks across rows are the driver's",
    ):
        client.output(acc, 'value')


def test_a_model_validator_that_returns_another_instance_fails_the_request(
    client: Client,
) -> None:
    computed = client.compute(COPIED, params={'sample': []})

    assert client.failure(computed) == (
        'CopiedParams._copied: a model validator checks and returns self, '
        'not CopiedParams'
    )
