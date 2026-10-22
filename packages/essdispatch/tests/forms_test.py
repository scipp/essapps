# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""A value, or a row, given inline or by reference, as the request chooses."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any, NewType

import pytest
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.dataclasses import dataclass

from ess.dispatch import Backend, Client, Record, Status, SubmitError, Template
from ess.dispatch import dataset as dataset_ref
from ess.dispatch.testing import FakeDatasets, StagedFunction, kept_rows, read_dataset
from ess.spec import WorkflowSpec, function_binding

SampleFile = NewType('SampleFile', Path)
Offset = NewType('Offset', float)


class Blob:
    """A type without a JSON form."""

    def __init__(self, value: float) -> None:
        self.value = value


class Shift(BaseModel):
    offset: float = 0.0


class LabelledShift(Shift):
    """A shift with a field that ``Shift`` lacks."""

    label: str = ''


@dataclass(config=ConfigDict(validate_by_name=True))
class Kind:
    """A value, not a group, whose JSON form holds the key ``@ref``."""

    kind: str = Field(alias='@ref')


class LoadParams(BaseModel):
    sample: SampleFile


class LoadOutputs(BaseModel, arbitrary_types_allowed=True):
    value: float
    shift: Shift
    labelled: LabelledShift
    blob: Blob
    values: list[float]


class AddParams(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    shift: Shift = Field(default_factory=Shift)
    offset: Offset | None = None
    blob: Blob | None = None
    pair: tuple[float, float] = (0.0, 0.0)
    terms: list[float] = Field(default_factory=list)
    kind: Kind | None = None


class SumParams(BaseModel):
    part: list[float] = Field(min_length=1)


class ShiftsParams(BaseModel):
    shifts: list[Shift] = Field(min_length=1)


class Value(BaseModel):
    value: float


class Run(BaseModel):
    """A run and what is given next to it: a group that holds a file."""

    file: SampleFile
    shift: Shift = Field(default_factory=Shift)
    offset: Offset | None = None
    weights: list[float] = Field(default_factory=list, max_length=3)

    @model_validator(mode='after')
    def _offset_not_negative(self) -> 'Run':
        if self.offset is not None and self.offset < 0:
            raise ValueError('a negative offset')
        return self


RunAlias = NewType('RunAlias', Run)


class RunsParams(BaseModel):
    runs: list[Run] = Field(min_length=1)
    extra: RunAlias | None = None


def _spec(
    name: str, params: type[BaseModel], outputs: type[BaseModel], **kw: Any
) -> WorkflowSpec[Any, Any]:
    return WorkflowSpec(
        name=name,
        version=1,
        title=name,
        description=name,
        params=params,
        outputs=outputs,
        **kw,
    )


LOAD = _spec('load', LoadParams, LoadOutputs)
ADD = _spec('add', AddParams, Value)
SUM = _spec('sum', SumParams, Value, tables=('part',))
STAGED_SUM = _spec('staged-sum', SumParams, Value, tables=('part',))
SHIFTS = _spec('shifts', ShiftsParams, Value, tables=('shifts',))
RUNS = _spec('runs', RunsParams, Value, tables=('runs',))


def load(params: LoadParams) -> LoadOutputs:
    value = read_dataset(params.sample)
    return LoadOutputs(
        value=value,
        shift=Shift(offset=value),
        labelled=LabelledShift(offset=value, label='first'),
        blob=Blob(value),
        values=[value, 2.0],
    )


def add(params: AddParams) -> Value:
    return Value(
        value=params.shift.offset
        + (params.offset or 0.0)
        + (0.0 if params.blob is None else params.blob.value)
        + sum(params.pair)
        + sum(params.terms)
    )


def runs(params: RunsParams) -> Value:
    rows = [*params.runs, *([] if params.extra is None else [params.extra])]
    return Value(
        value=sum(
            read_dataset(run.file) + run.shift.offset + (run.offset or 0.0)
            for run in rows
        )
    )


def total(params: Any) -> Value:
    return Value(value=sum(params.part))


@pytest.fixture
def client(tmp_path: Path) -> Iterator[Client]:
    datasets = FakeDatasets(proposal='p1', directory=tmp_path)
    datasets.measure(1, 1.0)
    datasets.measure(2, 2.0)
    backend = Backend(
        datasets,
        [
            function_binding(LOAD, load),
            function_binding(ADD, add),
            kept_rows(SUM, total),
            StagedFunction(STAGED_SUM, total),
            function_binding(
                SHIFTS, lambda p: Value(value=sum(s.offset for s in p.shifts))
            ),
            function_binding(RUNS, runs),
        ],
    )
    yield Client(backend, proposal='p1', submitter='anna')
    backend.close()


@pytest.fixture
def loaded(client: Client) -> Record:
    """LOAD of run 1, whose value is 1.0."""
    return client.compute(LOAD, params={'sample': dataset_ref(run=1)})


def test_a_number_or_a_group_is_given_by_value_or_by_reference(
    client: Client, loaded: Record
) -> None:
    by_reference = client.compute(
        ADD,
        params={
            'shift': loaded.ref('shift'),
            'offset': loaded.ref('value'),
            'terms': [loaded.ref('values', key='0'), 2.0],
        },
    )
    by_value = client.compute(
        ADD, params={'shift': Shift(offset=1.0), 'offset': 1.0, 'terms': [1.0, 2.0]}
    )

    assert client.output(by_reference, 'value') == 5.0
    assert client.output(by_value, 'value') == 5.0
    assert by_reference.request.params['shift'] == loaded.ref('shift')
    assert by_value.request.params['shift'] == {'offset': 1.0}


def test_each_field_of_a_group_is_given_inline_or_by_reference(
    client: Client, loaded: Record
) -> None:
    computed = client.compute(
        RUNS,
        params={
            'runs': [
                {
                    'file': dataset_ref(run=1),
                    'shift': loaded.ref('shift'),
                    'offset': 2.0,
                },
                {'file': dataset_ref(run=2), 'offset': loaded.ref('value')},
            ]
        },
    )

    assert client.output(computed, 'value') == (1.0 + 1.0 + 2.0) + (2.0 + 0.0 + 1.0)
    first, second = computed.request.params['runs']
    assert first['shift'] == loaded.ref('shift')
    assert first['offset'] == 2.0
    assert second['shift'] == {'offset': 0.0}  # left out, so its default
    assert second['offset'] == loaded.ref('value')


@pytest.mark.parametrize(
    ('row', 'reason'),
    [
        ({'offset': 1.0}, r'runs\[0\]\.file: a required field, not given'),
        ({'file': 'a.nxs'}, r"runs\[0\]\.file: 'a.nxs' is not a reference"),
        (
            {'file': dataset_ref(run=1), 'label': 'x'},
            r"runs\[0\]: \['label'\]: not fields of Run",
        ),
        (
            {'file': dataset_ref(run=1), 'shift': {'offset': dataset_ref(run=1)}},
            r'runs\[0\]\.shift\.offset',
        ),
    ],
)
def test_a_group_given_field_by_field_is_refused(
    client: Client, row: dict[str, Any], reason: str
) -> None:
    with pytest.raises(SubmitError, match=reason):
        client.submit(RUNS, params={'runs': [row]})


def test_a_refusal_names_the_field_of_a_row_by_its_place(client: Client) -> None:
    rows = [
        {'file': dataset_ref(run=1)},
        {'file': dataset_ref(run=2), 'shift': {'offset': 'x'}},
    ]

    with pytest.raises(SubmitError) as raised:
        client.submit(RUNS, params={'runs': rows})
    (error,) = raised.value.errors
    assert (error.entry, error.loc) == (None, ('runs', 1, 'shift', 'offset'))
    assert str(raised.value) == f'runs[1].shift.offset: {error.msg}'

    rows[1] = {'file': {'@ref': 'run'}}
    with pytest.raises(SubmitError, match="is not a kind of reference") as raised:
        client.submit(RUNS, params={'runs': rows})
    assert [e.loc for e in raised.value.errors] == [('runs', 1, 'file')]


def test_no_reference_stands_for_a_whole_group_that_holds_a_file(
    client: Client, loaded: Record
) -> None:
    with pytest.raises(SubmitError, match=r'runs\[0\]: .* names the whole of Run'):
        client.submit(RUNS, params={'runs': [loaded.ref('shift')]})


def test_a_group_given_its_file_alone_holds_defaults_for_the_rest(
    client: Client,
) -> None:
    computed = client.compute(
        RUNS,
        params={
            'runs': [{'file': dataset_ref(run=1)}],
            'extra': {'file': dataset_ref(run=2)},
        },
    )

    assert client.output(computed, 'value') == 3.0
    (row,) = computed.request.params['runs']
    assert row['file'] != computed.request.params['extra']['file']
    assert row['shift'] == {'offset': 0.0}


@pytest.mark.parametrize(
    ('params', 'place'),
    [
        ({'runs': [dataset_ref(run=1)]}, r'runs\[0\]'),
        (
            {'runs': [{'file': dataset_ref(run=1)}], 'extra': dataset_ref(run=2)},
            'extra',
        ),
    ],
)
def test_a_dataset_for_a_whole_group_is_refused(
    client: Client, params: dict[str, Any], place: str
) -> None:
    with pytest.raises(
        SubmitError, match=rf'^{place}: run:\d names the whole of Run\w*, which holds'
    ):
        client.submit(RUNS, params=params)


def test_the_framework_never_validates_a_group_given_field_by_field_whole(
    client: Client,
) -> None:
    # Run's model validator refuses a negative offset; like the params model's,
    # it is the binding's to run.
    computed = client.compute(
        RUNS, params={'runs': [{'file': dataset_ref(run=1), 'offset': -1.0}]}
    )

    assert client.output(computed, 'value') == 0.0


def test_the_schema_of_a_group_that_holds_a_file_is_its_fields() -> None:
    schema = RUNS.serialize().params_schema
    row, run = schema['properties']['runs']['items'], schema['$defs']['Run']

    assert row == {'$ref': '#/$defs/Run', 'not': {'required': ['@ref']}}
    assert run['properties']['file'] == {
        '$ref': '#/$defs/DatasetRef',
        'dataType': 'SampleFile',
    }
    assert run['properties']['weights']['maxItems'] == 3
    assert run['required'] == ['file']


def test_a_key_that_an_output_lacks_fails_the_record(
    client: Client, loaded: Record
) -> None:
    ref = loaded.ref('values', key='7')
    computed = client.submit(ADD, params={'terms': [ref]})

    assert _failed(client, computed) == f"{ref}: the output has no element '7'"


def test_a_value_whose_json_form_reads_as_a_reference_is_refused(
    client: Client,
) -> None:
    with pytest.raises(
        SubmitError, match=r'^kind: has a JSON form that reads as a reference$'
    ):
        client.submit(ADD, params={'kind': Kind(kind='output')})


def test_a_type_without_a_json_form_takes_a_reference_only(
    client: Client, loaded: Record
) -> None:
    computed = client.compute(ADD, params={'blob': loaded.ref('blob')})

    assert client.output(computed, 'value') == 1.0
    with pytest.raises(
        SubmitError, match=r"^blob: \{'value': 1.0\} is not a reference; Blob has no"
    ):
        client.submit(ADD, params={'blob': {'value': 1.0}})


@pytest.mark.parametrize(
    ('name', 'output', 'refused'),
    [
        ('shift', 'value', r'shift: .* of type float, does not fit Shift'),
        ('offset', 'shift', r'offset: .* of type Shift, does not fit Offset'),
        ('blob', 'value', r'blob: .* of type float, does not fit Blob'),
    ],
)
def test_a_reference_must_fit_its_parameter(
    client: Client, loaded: Record, name: str, output: str, refused: str
) -> None:
    with pytest.raises(SubmitError, match=refused):
        client.submit(ADD, params={name: loaded.ref(output)})


def test_a_dataset_fits_a_file_only(client: Client) -> None:
    with pytest.raises(
        SubmitError, match=r'^shift: dataset run:1 does not fit Shift, which is not'
    ):
        client.submit(ADD, params={'shift': dataset_ref(run=1)})


def test_a_reference_inside_a_value_is_refused(client: Client, loaded: Record) -> None:
    with pytest.raises(SubmitError, match=r'^pair\[1\]: .* is a reference inside'):
        client.submit(ADD, params={'pair': [1.0, loaded.ref('value')]})


def test_a_reference_to_a_whole_list_is_refused(client: Client, loaded: Record) -> None:
    with pytest.raises(SubmitError, match=r'^terms: .* is a reference to a whole list'):
        client.submit(ADD, params={'terms': loaded.ref('values')})
    with pytest.raises(SubmitError, match=r'^part: .* is a reference to a whole list'):
        client.submit(SUM, params={'part': loaded.ref('values')})


def _failed(client: Client, record: Record) -> str | None:
    assert client.wait(record) is Status.FAILED
    return client.failure(record)


def test_an_inline_value_and_a_reference_to_equal_data_are_one_row(
    client: Client, loaded: Record
) -> None:
    summed = client.accumulator(Template(SUM, blanks=('part',)))
    summed.push(part=[loaded.ref('value')])
    assert client.output(summed, 'value') == 1.0

    summed.push(part=[2.0, 1.0])  # distinct as the record holds them
    with pytest.raises(
        LookupError, match=r'push 1 failed: part\[2\]: repeats part\[0\], 1.0$'
    ):
        client.output(summed, 'value')
    plain = client.submit(SUM, params={'part': [1.0, loaded.ref('value')]})
    assert _failed(client, plain) == 'part[1]: repeats part[0], 1.0'


def test_references_to_two_records_with_equal_data_are_one_row(
    client: Client, loaded: Record
) -> None:
    again = client.compute(LOAD, params={'sample': dataset_ref(run=1)})
    plain = client.submit(
        SUM, params={'part': [loaded.ref('value'), again.ref('value')]}
    )

    assert _failed(client, plain) == 'part[1]: repeats part[0], 1.0'


def test_rows_read_are_compared_as_the_element_type_dumps_them(
    client: Client, loaded: Record
) -> None:
    plain = client.submit(
        SHIFTS, params={'shifts': [Shift(offset=1.0), loaded.ref('labelled')]}
    )

    assert _failed(client, plain) == 'shifts[1]: repeats shifts[0], {"offset":1.0}'


def test_a_stage_call_with_a_repeated_row_read_fails(
    client: Client, loaded: Record
) -> None:
    stage = client.stage(Template(STAGED_SUM, blanks=('part',)))
    distinct = client.submit(stage, params={'part': [loaded.ref('value'), 2.0]})
    repeated = client.submit(stage, params={'part': [loaded.ref('value'), 1.0]})

    assert client.output(distinct, 'value') == 3.0
    assert _failed(client, repeated) == 'part[1]: repeats part[0], 1.0'
