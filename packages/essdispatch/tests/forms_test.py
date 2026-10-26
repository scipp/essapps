# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""The value at each place given inline or by reference, as the request chooses."""

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Annotated, Any, Literal

import pytest
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic.dataclasses import dataclass

from ess.dispatch import Backend, Client, Record, Status, SubmitError, Template
from ess.dispatch import dataset as dataset_ref
from ess.dispatch.testing import FakeDatasets, StagedFunction, kept_rows, read_dataset
from ess.spec import OutputRef, WorkflowSpec, function_binding


class Blob:
    """A data value: a type without a JSON form."""

    def __init__(self, value: float) -> None:
        self.value = value


class Shift(BaseModel):
    offset: float = 0.0


class MeasuredTransmission(BaseModel, arbitrary_types_allowed=True):
    """A group that holds a data value, so is given by reference only."""

    fraction: Blob


class Point(BaseModel):
    x: float


class LabelledShift(Shift):
    """A shift with a field that ``Shift`` lacks."""

    label: str = ''


@dataclass(config=ConfigDict(validate_by_name=True))
class Kind:
    """A value, not a group, whose JSON form holds the key ``@ref``."""

    kind: str = Field(alias='@ref')


class SampleRun(BaseModel):
    file: Path


class LoadParams(BaseModel):
    sample: SampleRun


class Run(BaseModel, arbitrary_types_allowed=True):
    """A run and what is given next to it: a group that holds a file."""

    file: Path
    shift: Shift = Field(default_factory=Shift)
    offset: float = 0.0
    weights: list[float] = Field(default_factory=list, max_length=3)
    transmission: MeasuredTransmission | None = None

    @model_validator(mode='after')
    def _offset_not_negative(self) -> 'Run':
        if self.offset < 0:
            raise ValueError('a negative offset')
        return self


class LoadOutputs(BaseModel):
    value: float
    shift: Shift
    labelled: LabelledShift
    shifts: list[Shift]
    transmission: MeasuredTransmission
    point: Point
    values: list[float]
    run: Run


class Terms(BaseModel):
    """
    Fields of types that take no reference: numbers added to a shift, and
    ``kinds``, whose JSON form holds the key ``@ref``.
    """

    offset: float = 0.0
    pair: tuple[float, float] = (0.0, 0.0)
    values: list[float] = Field(default_factory=list)
    kinds: list[Kind] = Field(default_factory=list)


class AddParams(BaseModel):
    shift: Shift = Field(default_factory=Shift)
    transmission: MeasuredTransmission | None = None
    terms: Terms = Field(default_factory=Terms)


class SumParams(BaseModel):
    part: list[Shift] = Field(min_length=1)


class ShiftsParams(BaseModel):
    shifts: list[Shift] = Field(min_length=1)


class Value(BaseModel):
    value: float


class RunsParams(BaseModel):
    runs: list[Run] = Field(min_length=1)
    extra: Run | None = None


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


class Reduction(BaseModel):
    bins: int = 1
    transmission: MeasuredTransmission | None = None
    limits: tuple[float, float] = (0.0, 1.0)


class ByRun(BaseModel):
    """An alternative of a union that holds a file."""

    kind: Literal['run'] = 'run'
    run: Path


class Given(BaseModel):
    kind: Literal['given'] = 'given'
    x: float = 0.0
    shift: Shift = Field(default_factory=Shift)
    transmission: MeasuredTransmission | None = None


class DeepParams(BaseModel):
    reduction: Reduction = Field(default_factory=Reduction)
    centre: Annotated[ByRun | Given, Field(discriminator='kind')] = Given()


class Range(BaseModel):
    start: Point
    stop: Point

    @field_validator('stop')
    @classmethod
    def _after_start(cls, stop: Point, info: ValidationInfo) -> Point:
        start = info.data.get('start')
        if start is not None and stop.x <= start.x:
            raise ValueError('stop must be greater than start')
        return stop


class RangeParams(BaseModel):
    range: Range


class Edges(BaseModel):
    """Edges given as a list or as a string of numbers, held sorted."""

    values: list[float]

    @field_validator('values', mode='before')
    @classmethod
    def _parse(cls, values: Any) -> Any:
        return (
            [float(v) for v in values.split(',')] if isinstance(values, str) else values
        )

    @field_validator('values')
    @classmethod
    def _sorted(cls, values: list[float]) -> list[float]:
        return sorted(values)


class EdgesParams(BaseModel):
    edges: list[Edges] = Field(min_length=1)


class Millimetres(BaseModel):
    """A length given in millimetres, held in metres."""

    value: float

    @field_validator('value')
    @classmethod
    def _to_metres(cls, value: float) -> float:
        return value / 1000


class AnnotatedMillimetres(BaseModel):
    value: Annotated[float, AfterValidator(lambda value: value / 1000)]


class Steps(RootModel[list[float]]):
    """Not a group: written as its root value."""


class Ramp(BaseModel):
    steps: Steps


class StepsParams(BaseModel):
    ramp: Ramp


class LengthParams(BaseModel):
    by_decorator: Millimetres | None = None
    by_type: AnnotatedMillimetres | None = None


class BeamCentre(BaseModel):
    kind: Literal['given'] = 'given'
    x: float = 0.0


class FindCentre(BaseModel):
    kind: Literal['find'] = 'find'
    run: SampleRun | None = None
    tolerance: float = 0.5

    @field_validator('tolerance')
    @classmethod
    def _below_one(cls, tolerance: float) -> float:
        if not 0 < tolerance < 1:
            raise ValueError('tolerance must be between 0 and 1')
        return tolerance


Centre = Annotated[BeamCentre | FindCentre, Field(discriminator='kind')]


class CentredParams(BaseModel):
    beam_centre: Centre = BeamCentre()
    can_centre: Centre | None = None
    centres: list[Centre] = Field(default_factory=list)


class CentreOutputs(BaseModel):
    centre: BeamCentre


class Correction(BaseModel):
    """
    A group that holds no data value, so is given inline, with places in a
    list for a group given by reference only.
    """

    bands: list[MeasuredTransmission] = Field(default_factory=list)


class CorrectionParams(BaseModel):
    correction: Correction = Field(default_factory=Correction)


DEEP = _spec('deep', DeepParams, Value)
RANGE = _spec('range', RangeParams, Value)
EDGES = _spec('edges', EdgesParams, Value, tables=('edges',))
LENGTH = _spec('length', LengthParams, Value)
STEPS = _spec('steps', StepsParams, Value)
CENTRED = _spec('centred', CentredParams, Value)
FIND = _spec('find', LoadParams, CentreOutputs)
CORRECTION = _spec('correction', CorrectionParams, Value)


def load(params: LoadParams) -> LoadOutputs:
    value = read_dataset(params.sample.file)
    return LoadOutputs(
        value=value,
        shift=Shift(offset=value),
        labelled=LabelledShift(offset=value, label='first'),
        shifts=[Shift(offset=value), Shift(offset=2.0)],
        transmission=MeasuredTransmission(fraction=Blob(value)),
        point=Point(x=value),
        values=[value, 2.0],
        run=Run(file=params.sample.file),
    )


def add(params: AddParams) -> Value:
    transmission, terms = params.transmission, params.terms
    return Value(
        value=params.shift.offset
        + terms.offset
        + (0.0 if transmission is None else transmission.fraction.value)
        + sum(terms.pair)
        + sum(terms.values)
    )


def runs(params: RunsParams) -> Value:
    rows = [*params.runs, *([] if params.extra is None else [params.extra])]
    return Value(
        value=sum(
            read_dataset(run.file)
            + run.shift.offset
            + run.offset
            + (0.0 if run.transmission is None else run.transmission.fraction.value)
            for run in rows
        )
    )


def total(params: Any) -> Value:
    return Value(value=sum(shift.offset for shift in params.part))


def deep(params: DeepParams) -> Value:
    by = params.centre
    centre = read_dataset(by.run) if isinstance(by, ByRun) else by.x + by.shift.offset
    transmission = params.reduction.transmission
    return Value(
        value=params.reduction.bins
        + centre
        + (0.0 if transmission is None else transmission.fraction.value)
    )


def centred(params: CentredParams) -> Value:
    centre = params.beam_centre
    if isinstance(centre, BeamCentre):
        return Value(value=centre.x)
    run = 0.0 if centre.run is None else read_dataset(centre.run.file)
    return Value(value=run * centre.tolerance)


def find(params: LoadParams) -> CentreOutputs:
    return CentreOutputs(centre=BeamCentre(x=read_dataset(params.sample.file)))


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
            function_binding(DEEP, deep),
            function_binding(RANGE, lambda p: Value(value=p.range.stop.x)),
            function_binding(EDGES, lambda p: Value(value=p.edges[0].values[0])),
            function_binding(LENGTH, lambda p: Value(value=0.0)),
            function_binding(STEPS, lambda p: Value(value=sum(p.ramp.steps.root))),
            function_binding(CENTRED, centred),
            function_binding(FIND, find),
            function_binding(CORRECTION, lambda p: Value(value=0.0)),
        ],
    )
    yield Client(backend, proposal='p1', submitter='anna')
    backend.close()


@pytest.fixture
def loaded(client: Client) -> Record:
    """LOAD of run 1, whose value is 1.0."""
    return client.compute(LOAD, params={'sample': {'file': dataset_ref(run=1)}})


def test_a_group_is_given_by_value_or_by_reference(
    client: Client, loaded: Record
) -> None:
    terms = {'offset': 1.0, 'values': [2.0]}
    by_reference = client.compute(
        ADD, params={'shift': loaded.ref('shift'), 'terms': terms}
    )
    by_value = client.compute(ADD, params={'shift': Shift(offset=1.0), 'terms': terms})

    assert client.output(by_reference, 'value') == 4.0
    assert client.output(by_value, 'value') == 4.0
    assert by_reference.request.params['shift'] == loaded.ref('shift')
    assert by_value.request.params['shift'] == {'offset': 1.0}


@pytest.mark.parametrize(
    ('params', 'place'),
    [
        (lambda ref: {'terms': {'offset': ref}}, 'terms.offset'),
        (lambda ref: {'terms': {'values': [ref]}}, 'terms.values[0]'),
        (lambda ref: {'shift': {'offset': ref}}, 'shift.offset'),
    ],
)
def test_a_reference_at_a_number_is_refused(
    client: Client,
    loaded: Record,
    params: Callable[[OutputRef], dict[str, Any]],
    place: str,
) -> None:
    # A float names no role: every output of type float would fit.
    ref = loaded.ref('value')

    with pytest.raises(SubmitError) as raised:
        client.submit(ADD, params=params(ref))
    assert str(raised.value) == (
        f'{place}: {ref}: float is given inline; a reference stands for a group or '
        'a file only'
    )


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
                {
                    'file': dataset_ref(run=2),
                    'transmission': loaded.ref('transmission'),
                },
            ]
        },
    )

    assert client.output(computed, 'value') == (1.0 + 1.0 + 2.0) + (2.0 + 0.0 + 1.0)
    first, second = computed.request.params['runs']
    assert first['shift'] == loaded.ref('shift')
    assert first['offset'] == 2.0
    assert second['shift'] == {'offset': 0.0}  # left out, so its default
    assert second['transmission'] == loaded.ref('transmission')


@pytest.mark.parametrize(
    ('row', 'reason'),
    [
        ({'offset': 1.0}, r'runs\[0\]\.file: Field required'),
        (
            {'file': 'a.nxs'},
            r'runs\[0\]\.file: a file is given by reference to a dataset',
        ),
        (
            {'file': dataset_ref(run=1), 'label': 'x'},
            r'runs\[0\]\.label: Extra inputs are not permitted',
        ),
        (
            {'file': dataset_ref(run=1), 'shift': {'offset': dataset_ref(run=1)}},
            r'runs\[0\]\.shift\.offset',
        ),
    ],
)
def test_a_group_with_a_field_at_fault_is_refused(
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


def test_a_reference_to_an_output_that_holds_a_file_is_refused(
    client: Client, loaded: Record
) -> None:
    # The file in the output has passed no dataset check.
    with pytest.raises(
        SubmitError, match=r'^runs\[0\]: .*, of type Run, holds a file; a file is'
    ):
        client.submit(RUNS, params={'runs': [loaded.ref('run')]})


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
        SubmitError,
        match=rf'^{place}: dataset run:\d does not fit Run, which is not a file',
    ):
        client.submit(RUNS, params=params)


def test_a_group_is_validated_whole_in_the_call_phase(client: Client) -> None:
    # Run's model validator refuses a negative offset; the file is given by
    # reference, so the group is validated whole once the dataset is read.
    computed = client.submit(
        RUNS, params={'runs': [{'file': dataset_ref(run=1), 'offset': -1.0}]}
    )

    assert 'a negative offset' in _failed(client, computed)


@pytest.mark.parametrize(
    ('params', 'place'),
    [
        ({'runs': [None]}, ('runs', 0)),
        ({'runs': ['a.nxs']}, ('runs', 0)),
        ({'runs': [{'file': dataset_ref(run=1), 'shift': 1.0}]}, ('runs', 0, 'shift')),
    ],
)
def test_a_value_at_a_group_that_is_not_an_object_is_refused(
    client: Client, params: dict[str, Any], place: tuple[str | int, ...]
) -> None:
    with pytest.raises(SubmitError) as raised:
        client.submit(RUNS, params=params)
    (error,) = raised.value.errors
    assert error.loc == place
    assert error.msg.endswith(
        'is not an object; a group is written as an object of its fields'
    )


def test_a_reference_is_allowed_at_a_group_at_any_depth(
    client: Client, loaded: Record
) -> None:
    computed = client.compute(
        DEEP,
        params={
            'reduction': {'bins': 2, 'transmission': loaded.ref('transmission')},
            'centre': {'kind': 'run', 'run': dataset_ref(run=2)},
        },
    )

    assert client.output(computed, 'value') == 2 + 1.0 + 2.0
    params = computed.request.params
    assert params['reduction'] == {
        'bins': 2,
        'transmission': loaded.ref('transmission'),
        'limits': [0.0, 1.0],
    }
    (run,) = computed.request.datasets()
    assert params['centre'] == {'kind': 'run', 'run': run}


def test_a_reference_is_allowed_at_a_field_of_a_row(
    client: Client, loaded: Record
) -> None:
    file = {'file': dataset_ref(run=1)}
    computed = client.compute(
        RUNS,
        params={
            'runs': [
                file,
                {**file, 'offset': 1.0},
                {
                    **file,
                    'shift': loaded.ref('shift'),
                    'transmission': loaded.ref('transmission'),
                },
            ]
        },
    )

    assert client.output(computed, 'value') == 1.0 + 2.0 + 3.0
    row = computed.request.params['runs'][2]
    assert row['shift'] == loaded.ref('shift')
    assert row['transmission'] == loaded.ref('transmission')


def test_a_reference_at_a_tag_is_refused(client: Client, loaded: Record) -> None:
    centre = {'kind': loaded.ref('value'), 'run': dataset_ref(run=1)}

    with pytest.raises(SubmitError) as raised:
        client.submit(DEEP, params={'centre': centre})
    (error,) = raised.value.errors
    assert error.loc == ('centre', 'kind')
    assert error.msg.endswith('a tag is written inline, never as a reference')


def test_a_union_value_without_its_tag_is_refused(client: Client) -> None:
    with pytest.raises(SubmitError) as raised:
        client.submit(DEEP, params={'centre': {'run': dataset_ref(run=1)}})
    (error,) = raised.value.errors
    assert error.loc == ('centre',)
    assert error.msg == "the tag 'kind' is not given; it is one of ['run', 'given']"


@pytest.mark.parametrize('tag', [['run'], {'kind': 'run'}, 1, 'other'])
def test_a_value_that_is_no_tag_is_refused_at_the_tag(client: Client, tag: Any) -> None:
    centre = {'kind': tag, 'run': dataset_ref(run=1)}

    with pytest.raises(SubmitError) as raised:
        client.submit(DEEP, params={'centre': centre})
    (error,) = raised.value.errors
    assert error.loc == ('centre', 'kind')
    assert error.msg == f"{tag!r} is not a tag, one of ['run', 'given']"


def test_a_reference_in_an_alternative_fits_its_field(
    client: Client, loaded: Record
) -> None:
    given = client.compute(
        DEEP, params={'centre': {'kind': 'given', 'shift': loaded.ref('shift')}}
    )

    assert client.output(given, 'value') == 1 + 1.0
    with pytest.raises(
        SubmitError, match=r'^centre\.shift: .* of type float, does not fit Shift$'
    ):
        client.submit(
            DEEP, params={'centre': {'kind': 'given', 'shift': loaded.ref('value')}}
        )


def test_a_reference_inside_an_inline_value_at_depth_is_refused(
    client: Client, loaded: Record
) -> None:
    reduction = {'limits': [0.0, loaded.ref('value')]}

    with pytest.raises(SubmitError) as raised:
        client.submit(DEEP, params={'reduction': reduction})
    (error,) = raised.value.errors
    assert error.loc == ('reduction', 'limits', 1)
    assert 'is inside a value of tuple, given inline, whole' in error.msg


def test_a_group_without_a_reference_is_validated_whole_when_submitted(
    client: Client,
) -> None:
    backwards = {'start': {'x': 10.0}, 'stop': {'x': 5.0}}
    with pytest.raises(SubmitError) as raised:
        client.submit(RANGE, params={'range': backwards})
    (error,) = raised.value.errors
    assert error.loc == ('range', 'stop')
    assert 'stop must be greater than start' in error.msg


def test_a_group_without_a_reference_is_held_as_validated(client: Client) -> None:
    by_fields = client.compute(EDGES, params={'edges': [{'values': [3.0, 1.0]}]})
    by_instance = client.compute(EDGES, params={'edges': [Edges(values=[3.0, 1.0])]})
    by_string = client.compute(EDGES, params={'edges': [{'values': '3, 1'}]})

    assert by_fields.request == by_instance.request == by_string.request
    assert by_fields.request.params == {'edges': [{'values': [1.0, 3.0]}]}
    assert client.output(by_fields, 'value') == 1.0


def test_rows_without_a_reference_are_compared_as_validated(client: Client) -> None:
    with pytest.raises(SubmitError, match=r'^edges\[1\]: repeats edges\[0\]'):
        client.submit(EDGES, params={'edges': [{'values': [1, 3]}, {'values': '3,1'}]})


@pytest.mark.parametrize(
    ('name', 'group'),
    [('by_decorator', Millimetres), ('by_type', AnnotatedMillimetres)],
)
@pytest.mark.parametrize('as_instance', [False, True])
def test_a_group_whose_validator_changes_a_validated_value_is_refused(
    client: Client, name: str, group: type[BaseModel], as_instance: bool
) -> None:
    value = group(value=5000.0) if as_instance else {'value': 5000.0}

    with pytest.raises(SubmitError) as raised:
        client.submit(LENGTH, params={name: value})
    (error,) = raised.value.errors
    assert error.loc == (name, 'value')
    assert error.msg == 'has no JSON form that reads back as the value'


def test_a_file_in_a_group_given_inline_is_refused(client: Client) -> None:
    centre = {'kind': 'run', 'run': '/data/run1.nxs'}

    with pytest.raises(SubmitError) as raised:
        client.submit(DEEP, params={'centre': centre})
    (error,) = raised.value.errors
    assert error.loc == ('centre', 'run')
    assert error.msg.startswith('a file is given by reference to a dataset')


def test_a_file_given_inline_next_to_a_reference_is_refused(
    client: Client, loaded: Record
) -> None:
    # With a reference in the group, each field is validated alone.
    row = {'file': '/data/run1.nxs', 'shift': loaded.ref('shift')}

    with pytest.raises(SubmitError) as raised:
        client.submit(RUNS, params={'runs': [row]})
    (error,) = raised.value.errors
    assert error.loc == ('runs', 0, 'file')
    assert error.msg == (
        "'/data/run1.nxs' is not a dataset; a file is given as dataset(path=...)"
    )


def test_a_group_with_a_reference_is_validated_whole_in_the_call_phase(
    client: Client, loaded: Record
) -> None:
    after = client.compute(
        RANGE, params={'range': {'start': {'x': 0.0}, 'stop': loaded.ref('point')}}
    )
    before = client.submit(
        RANGE, params={'range': {'start': {'x': 10.0}, 'stop': loaded.ref('point')}}
    )

    assert client.output(after, 'value') == 1.0
    assert _failed(client, before) == (
        'range.stop: Value error, stop must be greater than start'
    )


def test_a_union_parameter_takes_either_alternative(client: Client) -> None:
    find = {'kind': 'find', 'run': {'file': dataset_ref(run=2)}, 'tolerance': 0.5}
    found = client.compute(CENTRED, params={'beam_centre': find})
    given = client.compute(CENTRED, params={'beam_centre': {'kind': 'given', 'x': 3.0}})
    default = client.compute(CENTRED, params={})

    assert [client.output(r, 'value') for r in (found, given, default)] == [
        1.0,
        3.0,
        0.0,
    ]
    (run,) = found.request.datasets()
    assert found.request.params['beam_centre'] == {**find, 'run': {'file': run}}


@pytest.mark.parametrize(
    ('params', 'place'),
    [
        (lambda ref: {'beam_centre': ref}, ('beam_centre',)),
        (lambda ref: {'can_centre': ref}, ('can_centre',)),  # typed U | None
        (lambda ref: {'centres': [BeamCentre(), ref]}, ('centres', 1)),
    ],
)
def test_no_reference_fits_a_union(
    client: Client,
    params: Callable[[OutputRef], dict[str, Any]],
    place: tuple[str | int, ...],
) -> None:
    # Not even an output of an alternative's type.
    centre = client.compute(FIND, params={'sample': {'file': dataset_ref(run=2)}})
    ref = centre.ref('centre')
    with pytest.raises(SubmitError) as raised:
        client.submit(CENTRED, params=params(ref))
    (error,) = raised.value.errors
    assert error.loc == place
    assert error.msg == (
        f'{ref}: a union of groups is written inline, with the tag of an '
        'alternative; a reference stands for a group or a file only'
    )


def test_an_error_inside_a_union_names_its_place_without_the_tag(
    client: Client,
) -> None:
    # Without a reference inside, the alternative is validated whole when
    # submitted; with the run given by reference, once the run is read.
    find = {'kind': 'find', 'tolerance': 2.0}
    with pytest.raises(SubmitError) as raised:
        client.submit(CENTRED, params={'beam_centre': find})
    (error,) = raised.value.errors
    assert error.loc == ('beam_centre', 'tolerance')

    computed = client.submit(
        CENTRED, params={'beam_centre': {**find, 'run': {'file': dataset_ref(run=1)}}}
    )
    assert _failed(client, computed) == (
        'beam_centre.tolerance: Value error, tolerance must be between 0 and 1'
    )


def test_the_schema_of_a_union_has_no_reference_form() -> None:
    properties = CENTRED.serialize().params_schema['properties']
    inline = {k: v for k, v in properties['beam_centre'].items() if k != 'default'}
    can, null = properties['can_centre']['anyOf']

    assert inline == {**can, 'title': 'Beam Centre'}
    assert null == {'type': 'null'}
    assert properties['centres']['items'] == can
    assert 'dataType' not in can
    assert can['discriminator']['propertyName'] == 'kind'


def test_the_schema_of_a_union_requires_the_tag_of_each_alternative() -> None:
    schema = CENTRED.serialize().params_schema
    inline = schema['properties']['beam_centre']

    for alternative in inline['oneOf']:
        form = schema['$defs'][alternative['$ref'].rsplit('/', 1)[1]]
        assert 'kind' in form['required']
        assert 'default' not in form['properties']['kind']


def test_a_root_model_is_written_as_its_root_value(client: Client) -> None:
    schema = STEPS.serialize().params_schema
    steps = schema['$defs']['Ramp']['properties']['steps']
    assert schema['$defs'][steps['$ref'].rsplit('/', 1)[1]]['type'] == 'array'

    computed = client.compute(STEPS, params={'ramp': {'steps': [1.0, 2.0]}})
    assert computed.request.params == {'ramp': {'steps': [1.0, 2.0]}}
    assert client.output(computed, 'value') == 3.0
    with pytest.raises(
        SubmitError, match=r'^ramp\.steps: Input should be a valid array'
    ):
        client.submit(STEPS, params={'ramp': {'steps': {'root': [1.0, 2.0]}}})


def test_the_schema_of_a_group_that_holds_a_file_is_its_fields() -> None:
    schema = RUNS.serialize().params_schema
    row, run = schema['properties']['runs']['items'], schema['$defs']['Run']

    assert row == {'$ref': '#/$defs/Run', 'not': {'required': ['@ref']}}
    assert run['properties']['file'] == {
        '$ref': '#/$defs/DatasetRef',
        'dataType': 'Path',
    }
    assert run['properties']['weights']['maxItems'] == 3
    assert run['required'] == ['file']


def test_a_key_that_an_output_lacks_fails_the_record(
    client: Client, loaded: Record
) -> None:
    ref = loaded.ref('shifts', key='7')
    computed = client.submit(SHIFTS, params={'shifts': [ref]})

    assert _failed(client, computed) == f"{ref}: the output has no element '7'"


def test_a_key_names_an_element_of_an_output(client: Client, loaded: Record) -> None:
    computed = client.compute(
        SHIFTS, params={'shifts': [loaded.ref('shifts', key='1'), Shift()]}
    )

    assert client.output(computed, 'value') == 2.0


def test_a_value_whose_json_form_reads_as_a_reference_is_refused(
    client: Client,
) -> None:
    with pytest.raises(
        SubmitError,
        match=r'^terms\.kinds\[0\]: has a JSON form that reads as a reference$',
    ):
        client.submit(ADD, params={'terms': {'kinds': [Kind(kind='output')]}})


def test_a_group_that_holds_a_data_value_is_given_by_reference_only(
    client: Client, loaded: Record
) -> None:
    computed = client.compute(ADD, params={'transmission': loaded.ref('transmission')})

    assert client.output(computed, 'value') == 1.0
    ref, null = ADD.serialize().params_schema['properties']['transmission']['anyOf']
    assert (ref['dataType'], null) == ('MeasuredTransmission', {'type': 'null'})
    for inline in ({'fraction': 1.0}, MeasuredTransmission(fraction=Blob(1.0))):
        with pytest.raises(
            SubmitError,
            match=r'^transmission: MeasuredTransmission is given by reference: no '
            r'request writes its field fraction, of Blob$',
        ):
            client.submit(ADD, params={'transmission': inline})


@pytest.mark.parametrize(
    ('spec', 'params', 'place'),
    [
        (
            DEEP,
            lambda t: {'reduction': {'bins': 2, 'transmission': t}},
            ('reduction', 'transmission'),
        ),
        (
            CORRECTION,
            lambda t: {'correction': {'bands': [t]}},
            ('correction', 'bands', 0),
        ),
        (
            DEEP,
            lambda t: {'centre': {'kind': 'given', 'transmission': t}},
            ('centre', 'transmission'),
        ),
    ],
)
@pytest.mark.parametrize(
    'inline',
    [{'fraction': 1.0}, MeasuredTransmission(fraction=Blob(1.0))],
)
def test_a_group_given_by_reference_only_is_refused_inside_an_inline_group(
    client: Client,
    spec: WorkflowSpec[Any, Any],
    params: Callable[[Any], dict[str, Any]],
    place: tuple[str | int, ...],
    inline: Any,
) -> None:
    with pytest.raises(SubmitError) as raised:
        client.submit(spec, params=params(inline))
    (error,) = raised.value.errors
    assert error.loc == place
    assert error.msg == (
        'MeasuredTransmission is given by reference: no request writes its field '
        'fraction, of Blob'
    )


@pytest.mark.parametrize(
    ('name', 'output', 'refused'),
    [
        ('shift', 'value', r'shift: .* of type float, does not fit Shift'),
        ('shift', 'transmission', r'shift: .* MeasuredTransmission, does not fit'),
        ('transmission', 'shift', r'transmission: .* Shift, does not fit Measured'),
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
    with pytest.raises(
        SubmitError, match=r'^terms\.pair\[1\]: .* is inside a value of tuple'
    ):
        client.submit(ADD, params={'terms': {'pair': [1.0, loaded.ref('value')]}})


def test_a_reference_to_a_whole_list_is_refused(client: Client, loaded: Record) -> None:
    with pytest.raises(
        SubmitError, match=r'^terms\.values: .* is a reference to a whole list'
    ):
        client.submit(ADD, params={'terms': {'values': loaded.ref('values')}})
    with pytest.raises(SubmitError, match=r'^part: .* is a reference to a whole list'):
        client.submit(SUM, params={'part': loaded.ref('shifts')})


def _failed(client: Client, record: Record) -> str:
    assert client.wait(record) is Status.FAILED
    return client.failure(record)


def test_an_inline_value_and_a_reference_to_equal_data_are_one_row(
    client: Client, loaded: Record
) -> None:
    summed = client.accumulator(Template(SUM, blanks=('part',)))
    summed.push(part=[loaded.ref('shift')])
    assert client.output(summed, 'value') == 1.0

    summed.push(part=[Shift(offset=2.0), Shift(offset=1.0)])  # distinct as held
    with pytest.raises(
        LookupError,
        match=r'push 1 failed: part\[2\]: repeats part\[0\], \{"offset":1.0\}$',
    ):
        client.output(summed, 'value')
    plain = client.submit(
        SUM, params={'part': [Shift(offset=1.0), loaded.ref('shift')]}
    )
    assert _failed(client, plain) == 'part[1]: repeats part[0], {"offset":1.0}'


def test_references_to_two_records_with_equal_data_are_one_row(
    client: Client, loaded: Record
) -> None:
    again = client.compute(LOAD, params={'sample': {'file': dataset_ref(run=1)}})
    plain = client.submit(
        SUM, params={'part': [loaded.ref('shift'), again.ref('shift')]}
    )

    assert _failed(client, plain) == 'part[1]: repeats part[0], {"offset":1.0}'


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
    distinct = client.submit(
        stage, params={'part': [loaded.ref('shift'), Shift(offset=2.0)]}
    )
    repeated = client.submit(
        stage, params={'part': [loaded.ref('shift'), Shift(offset=1.0)]}
    )

    assert client.output(distinct, 'value') == 3.0
    assert _failed(client, repeated) == 'part[1]: repeats part[0], {"offset":1.0}'
