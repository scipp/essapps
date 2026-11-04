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
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from ess.apps import Backend, Client, Record, Status, SubmitError, Template
from ess.apps import file as file_ref
from ess.apps.testing import FakeFiles, StagedFunction, kept_rows, read_file
from ess.spec import (
    MaskFile,
    OutputRef,
    Referenced,
    RunFile,
    WorkflowSpec,
    function_binding,
)


class Blob:
    """A type that is not a field type: only the T of Referenced[T] holds it."""

    def __init__(self, value: float) -> None:
        self.value = value


class Shift(BaseModel):
    offset: float = 0.0


class MeasuredTransmission(BaseModel, arbitrary_types_allowed=True):
    """
    A group that holds a type that is not a field type, so a reference fills
    the field that holds it, ``Referenced[...]``.
    """

    fraction: Blob


class LabelledShift(Shift):
    """A shift with a field that ``Shift`` lacks."""

    label: str = ''


class LoadParams(BaseModel):
    sample: RunFile


class LoadOutputs(BaseModel):
    value: float
    shift: Shift
    labelled: LabelledShift
    shifts: list[Shift]
    transmission: MeasuredTransmission


class Terms(BaseModel):
    """Fields of types that take no reference: numbers added to a shift."""

    offset: float = 0.0
    values: list[float] = Field(default_factory=list)


class AddParams(BaseModel):
    shift: Shift | Referenced[Shift] = Field(default_factory=Shift)
    transmission: Referenced[MeasuredTransmission] | None = None
    terms: Terms = Field(default_factory=Terms)


class TypedShift(BaseModel):
    """A shift typed in: the fields of a ``Shift``, of a class of its own."""

    offset: float = 0.0


class EitherShiftParams(BaseModel):
    shift: Referenced[Shift] | TypedShift = Field(default_factory=TypedShift)


class Named(BaseModel):
    name: str


class SumParams(BaseModel):
    part: list[Shift | Referenced[Shift]] = Field(min_length=1)


class ShiftsParams(BaseModel):
    shifts: list[Shift | Referenced[Shift]] = Field(min_length=1)


class Value(BaseModel):
    value: float


class Run(BaseModel):
    """The settings of a run, a row of a table of groups."""

    number: int
    offset: float = 0.0
    weights: list[float] = Field(default_factory=list, max_length=3)

    @model_validator(mode='after')
    def _offset_not_negative(self) -> 'Run':
        if self.offset < 0:
            raise ValueError('a negative offset')
        return self


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
EITHER_SHIFT = _spec('either-shift', EitherShiftParams, Named)
SUM = _spec('sum', SumParams, Value, tables=('part',))
STAGED_SUM = _spec('staged-sum', SumParams, Value, tables=('part',))
SHIFTS = _spec('shifts', ShiftsParams, Value, tables=('shifts',))
RUNS = _spec('runs', RunsParams, Value, tables=('runs',))


class Range(BaseModel):
    start: float
    stop: float

    @field_validator('stop')
    @classmethod
    def _after_start(cls, stop: float, info: ValidationInfo) -> float:
        start = info.data.get('start')
        if start is not None and stop <= start:
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


class LengthParams(BaseModel):
    by_decorator: Millimetres | None = None
    by_type: AnnotatedMillimetres | None = None


class BeamCentre(BaseModel):
    kind: Literal['given'] = 'given'
    x: float = 0.0


class FindCentre(BaseModel):
    kind: Literal['find'] = 'find'
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


class MarkedCentredParams(CentredParams):
    """A centre as an alternative written inline, or by reference to a BeamCentre."""

    beam_centre: Centre | Referenced[BeamCentre] = BeamCentre()


class CentreOutputs(BaseModel):
    centre: BeamCentre


class MaskedParams(BaseModel):
    """A run and its mask, two file fields that take different files."""

    run: RunFile
    mask: MaskFile


RANGE = _spec('range', RangeParams, Value)
EDGES = _spec('edges', EdgesParams, Value, tables=('edges',))
LENGTH = _spec('length', LengthParams, Value)
CENTRED = _spec('centred', CentredParams, Value)
MARKED_CENTRED = _spec('marked-centred', MarkedCentredParams, Value)
FIND = _spec('find', LoadParams, CentreOutputs)
MASKED = _spec('masked', MaskedParams, Value)


def load(params: LoadParams) -> LoadOutputs:
    value = read_file(params.sample)
    return LoadOutputs(
        value=value,
        shift=Shift(offset=value),
        labelled=LabelledShift(offset=value, label='first'),
        shifts=[Shift(offset=value), Shift(offset=2.0)],
        transmission=MeasuredTransmission(fraction=Blob(value)),
    )


def add(params: AddParams) -> Value:
    transmission, terms = params.transmission, params.terms
    return Value(
        value=params.shift.offset
        + terms.offset
        + (0.0 if transmission is None else transmission.fraction.value)
        + sum(terms.values)
    )


def runs(params: RunsParams) -> Value:
    rows = [*params.runs, *([] if params.extra is None else [params.extra])]
    return Value(value=sum(run.number + run.offset + sum(run.weights) for run in rows))


def total(params: Any) -> Value:
    return Value(value=sum(shift.offset for shift in params.part))


def centred(params: CentredParams) -> Value:
    centre = params.beam_centre
    if isinstance(centre, BeamCentre):
        return Value(value=centre.x)
    return Value(value=centre.tolerance)


def masked(params: MaskedParams) -> Value:
    return Value(value=read_file(params.run) + read_file(params.mask))


@pytest.fixture
def client(tmp_path: Path) -> Iterator[Client]:
    files = FakeFiles(proposal='p1', directory=tmp_path)
    files.measure(1, 1.0)
    files.measure(2, 2.0)
    files.measure(3, 10.0, kind='mask')
    backend = Backend(
        files,
        [
            function_binding(LOAD, load),
            function_binding(ADD, add),
            function_binding(
                EITHER_SHIFT, lambda p: Named(name=type(p.shift).__name__)
            ),
            kept_rows(SUM, total),
            StagedFunction(STAGED_SUM, total),
            function_binding(
                SHIFTS, lambda p: Value(value=sum(s.offset for s in p.shifts))
            ),
            function_binding(RUNS, runs),
            function_binding(RANGE, lambda p: Value(value=p.range.stop)),
            function_binding(EDGES, lambda p: Value(value=p.edges[0].values[0])),
            function_binding(LENGTH, lambda p: Value(value=0.0)),
            function_binding(CENTRED, centred),
            function_binding(MARKED_CENTRED, centred),
            function_binding(
                FIND,
                lambda p: CentreOutputs(centre=BeamCentre(x=read_file(p.sample))),
            ),
            function_binding(MASKED, masked),
        ],
    )
    yield Client(backend, proposal='p1', submitter='anna')
    backend.close()


@pytest.fixture
def loaded(client: Client) -> Record:
    """LOAD of run 1, whose value is 1.0."""
    return client.compute(LOAD, params={'sample': file_ref(run=1)})


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


def test_a_value_reaches_the_binding_as_the_member_of_its_source(
    client: Client, loaded: Record
) -> None:
    # Shift, the first member, could validate the value written inline too.
    typed_in = client.compute(EITHER_SHIFT, params={'shift': {'offset': 1.0}})
    by_reference = client.compute(EITHER_SHIFT, params={'shift': loaded.ref('shift')})

    assert client.output(typed_in, 'name') == 'TypedShift'
    assert client.output(by_reference, 'name') == 'Shift'


NOT_REFERENCED = 'is a reference to an output; the field is not typed Referenced[...]'
WHOLE_LIST = (
    'is a reference to a whole list; a reference stands for one element of a list'
)


@pytest.mark.parametrize(
    ('spec', 'params', 'at', 'why'),
    [
        (ADD, lambda ref: {'terms': {'offset': ref}}, 'terms.offset', NOT_REFERENCED),
        (ADD, lambda ref: {'terms': {'values': ref}}, 'terms.values', WHOLE_LIST),
        (
            ADD,
            lambda ref: {'terms': {'values': [ref]}},
            'terms.values[0]',
            NOT_REFERENCED,
        ),
        (ADD, lambda ref: {'shift': {'offset': ref}}, 'shift.offset', NOT_REFERENCED),
        (
            RUNS,
            lambda ref: {'runs': [{'number': ref}]},
            'runs[0].number',
            NOT_REFERENCED,
        ),
    ],
)
def test_a_reference_to_an_output_at_a_field_not_typed_referenced_is_refused(
    client: Client,
    loaded: Record,
    spec: WorkflowSpec[Any, Any],
    params: Callable[[OutputRef], dict[str, Any]],
    at: str,
    why: str,
) -> None:
    ref = loaded.ref('value')

    with pytest.raises(SubmitError) as raised:
        client.submit(spec, params=params(ref))
    assert str(raised.value) == f'{at}: {ref} {why}'


def test_a_reference_at_an_element_of_a_list_stands_for_one_row(
    client: Client, loaded: Record
) -> None:
    computed = client.compute(
        SHIFTS, params={'shifts': [loaded.ref('shift'), Shift(offset=2.0)]}
    )

    assert client.output(computed, 'value') == 1.0 + 2.0
    assert computed.request.params['shifts'] == [loaded.ref('shift'), {'offset': 2.0}]


def test_a_reference_to_a_whole_list_is_refused(client: Client, loaded: Record) -> None:
    ref = loaded.ref('shifts')

    with pytest.raises(SubmitError) as raised:
        client.submit(SUM, params={'part': ref})
    assert str(raised.value) == (
        f'part: {ref} is a reference to a whole list; a reference stands for one '
        'element of a list'
    )


@pytest.mark.parametrize(
    ('row', 'reason'),
    [
        ({'offset': 1.0}, r'runs\[0\]\.number: Field required'),
        (
            {'number': 1, 'label': 'x'},
            r'runs\[0\]\.label: Extra inputs are not permitted',
        ),
        (
            {'number': 1, 'weights': [1.0, 2.0, 3.0, 4.0]},
            r'runs\[0\]\.weights: List should have at most 3 items',
        ),
    ],
)
def test_a_group_with_a_field_at_fault_is_refused(
    client: Client, row: dict[str, Any], reason: str
) -> None:
    with pytest.raises(SubmitError, match=reason):
        client.submit(RUNS, params={'runs': [row]})


def test_a_refusal_names_the_field_of_a_row_by_its_place(client: Client) -> None:
    rows: list[Any] = [{'number': 1}, {'number': 2, 'offset': 'x'}]

    with pytest.raises(SubmitError) as raised:
        client.submit(RUNS, params={'runs': rows})
    (error,) = raised.value.errors
    assert (error.entry, error.loc) == (None, ('runs', 1, 'offset'))
    assert str(raised.value) == f'runs[1].offset: {error.msg}'

    rows[1] = {'@ref': 'run'}
    with pytest.raises(SubmitError, match="is not a kind of reference") as raised:
        client.submit(RUNS, params={'runs': rows})
    assert [e.loc for e in raised.value.errors] == [('runs', 1)]


def test_a_group_written_inline_holds_defaults_for_the_fields_left_out(
    client: Client,
) -> None:
    computed = client.compute(
        RUNS,
        params={'runs': [{'number': 1}], 'extra': {'number': 2, 'offset': 1.0}},
    )

    assert client.output(computed, 'value') == 1 + 2 + 1.0
    assert computed.request.params == {
        'runs': [{'number': 1, 'offset': 0.0, 'weights': []}],
        'extra': {'number': 2, 'offset': 1.0, 'weights': []},
    }


@pytest.mark.parametrize(
    ('params', 'place'),
    [
        ({'runs': [file_ref(run=1)]}, r'runs\[0\]'),
        ({'runs': [{'number': 1}], 'extra': file_ref(run=2)}, 'extra'),
    ],
)
def test_a_file_for_a_whole_group_is_refused(
    client: Client, params: dict[str, Any], place: str
) -> None:
    with pytest.raises(
        SubmitError,
        match=rf'^{place}: file run:\d: the field is not a file field$',
    ):
        client.submit(RUNS, params=params)


def test_a_row_is_validated_whole_when_submitted(client: Client) -> None:
    with pytest.raises(SubmitError) as raised:
        client.submit(RUNS, params={'runs': [{'number': 1, 'offset': -1.0}]})
    (error,) = raised.value.errors
    assert error.loc == ('runs', 0)
    assert 'a negative offset' in error.msg


@pytest.mark.parametrize(
    ('params', 'place'),
    [
        ({'runs': [None]}, ('runs', 0)),
        ({'runs': ['a.nxs']}, ('runs', 0)),
        ({'runs': [{'number': 1}], 'extra': 1.0}, ('extra',)),
    ],
)
def test_a_value_at_a_group_that_is_not_an_object_is_refused(
    client: Client, params: dict[str, Any], place: tuple[str | int, ...]
) -> None:
    with pytest.raises(SubmitError) as raised:
        client.submit(RUNS, params=params)
    (error,) = raised.value.errors
    assert error.loc == place
    assert error.msg == 'Input should be an object'


def test_a_reference_at_a_tag_is_refused(client: Client, loaded: Record) -> None:
    centre = {'kind': loaded.ref('value'), 'tolerance': 0.5}

    with pytest.raises(SubmitError) as raised:
        client.submit(CENTRED, params={'beam_centre': centre})
    (error,) = raised.value.errors
    assert error.loc == ('beam_centre',)
    assert error.msg.endswith(
        "found using 'kind' does not match any of the expected tags: 'given', 'find'"
    )


def test_a_union_value_without_its_tag_is_refused(client: Client) -> None:
    with pytest.raises(SubmitError) as raised:
        client.submit(CENTRED, params={'beam_centre': {'tolerance': 0.5}})
    (error,) = raised.value.errors
    assert error.loc == ('beam_centre',)
    assert error.msg == "Unable to extract tag using discriminator 'kind'"


@pytest.mark.parametrize('tag', [['find'], {'kind': 'find'}, 1, 'other'])
def test_a_value_that_is_no_tag_is_refused_at_the_union(
    client: Client, tag: Any
) -> None:
    centre = {'kind': tag, 'tolerance': 0.5}

    with pytest.raises(SubmitError) as raised:
        client.submit(CENTRED, params={'beam_centre': centre})
    (error,) = raised.value.errors
    assert error.loc == ('beam_centre',)
    assert error.msg == (
        f"Input tag '{tag}' found using 'kind' does not match any of the expected "
        "tags: 'given', 'find'"
    )


def test_a_group_written_inline_is_validated_whole_when_submitted(
    client: Client,
) -> None:
    computed = client.compute(RANGE, params={'range': {'start': 0.0, 'stop': 1.0}})
    assert client.output(computed, 'value') == 1.0

    with pytest.raises(SubmitError) as raised:
        client.submit(RANGE, params={'range': {'start': 10.0, 'stop': 5.0}})
    (error,) = raised.value.errors
    assert error.loc == ('range', 'stop')
    assert 'stop must be greater than start' in error.msg


def test_a_group_written_inline_is_held_as_validated(client: Client) -> None:
    by_fields = client.compute(EDGES, params={'edges': [{'values': [3.0, 1.0]}]})
    by_instance = client.compute(EDGES, params={'edges': [Edges(values=[3.0, 1.0])]})
    by_string = client.compute(EDGES, params={'edges': [{'values': '3, 1'}]})

    assert by_fields.request == by_instance.request == by_string.request
    assert by_fields.request.params == {'edges': [{'values': [1.0, 3.0]}]}
    assert client.output(by_fields, 'value') == 1.0


def test_rows_written_inline_are_compared_as_validated(client: Client) -> None:
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


@pytest.mark.parametrize('inline', ['/data/run1.nxs', {'path': '/data/run1.nxs'}])
def test_a_value_given_inline_at_a_file_field_is_refused(
    client: Client, inline: Any
) -> None:
    with pytest.raises(SubmitError) as raised:
        client.submit(LOAD, params={'sample': inline})
    (error,) = raised.value.errors
    assert error.loc == ('sample',)
    assert error.msg == (
        f'{inline!r} is not a file; a file field is written as file(...)'
    )


def test_a_union_parameter_takes_either_alternative(client: Client) -> None:
    find = {'kind': 'find', 'tolerance': 0.25}
    found = client.compute(CENTRED, params={'beam_centre': find})
    given = client.compute(CENTRED, params={'beam_centre': {'kind': 'given', 'x': 3.0}})
    default = client.compute(CENTRED, params={})

    assert [client.output(r, 'value') for r in (found, given, default)] == [
        0.25,
        3.0,
        0.0,
    ]
    assert found.request.params['beam_centre'] == find


@pytest.mark.parametrize(
    ('params', 'place'),
    [
        (lambda ref: {'beam_centre': ref}, ('beam_centre',)),
        (lambda ref: {'can_centre': ref}, ('can_centre',)),  # typed U | None
        (lambda ref: {'centres': [BeamCentre(), ref]}, ('centres', 1)),
    ],
)
def test_no_reference_fits_a_union_that_the_spec_does_not_mark(
    client: Client,
    params: Callable[[OutputRef], dict[str, Any]],
    place: tuple[str | int, ...],
) -> None:
    # Not even an output of an alternative's type, since a union of groups is
    # written inline unless the spec marks the place Referenced[...].
    centre = client.compute(FIND, params={'sample': file_ref(run=2)})
    ref = centre.ref('centre')
    with pytest.raises(SubmitError) as raised:
        client.submit(CENTRED, params=params(ref))
    (error,) = raised.value.errors
    assert error.loc == place
    assert error.msg == f'{ref} {NOT_REFERENCED}'


def test_a_union_marked_referenced_takes_an_alternative_or_a_reference(
    client: Client,
) -> None:
    found = client.compute(FIND, params={'sample': file_ref(run=2)})
    by_reference = client.compute(
        MARKED_CENTRED, params={'beam_centre': found.ref('centre')}
    )
    by_tag = client.compute(
        MARKED_CENTRED, params={'beam_centre': {'kind': 'find', 'tolerance': 0.25}}
    )

    assert [client.output(r, 'value') for r in (by_reference, by_tag)] == [2.0, 0.25]
    assert by_reference.request.params['beam_centre'] == found.ref('centre')


def test_the_schema_of_a_union_marked_referenced_has_a_reference_form() -> None:
    schema = MARKED_CENTRED.serialize().params_schema['properties']['beam_centre']
    inline, reference = schema['oneOf']

    assert inline['discriminator']['propertyName'] == 'kind'
    assert reference['dataType'] == 'BeamCentre'
    assert reference['discriminator']['propertyName'] == '@ref'


def test_a_reference_is_refused_at_a_group_that_the_spec_does_not_mark(
    client: Client, loaded: Record
) -> None:
    ref = loaded.ref('shift')

    with pytest.raises(SubmitError) as raised:
        client.submit(ADD, params={'terms': ref})
    (error,) = raised.value.errors
    assert error.loc == ('terms',)
    assert error.msg == f'{ref} {NOT_REFERENCED}'


def test_an_error_inside_a_union_names_its_place_without_the_tag(
    client: Client,
) -> None:
    with pytest.raises(SubmitError) as raised:
        client.submit(
            CENTRED, params={'beam_centre': {'kind': 'find', 'tolerance': 2.0}}
        )
    (error,) = raised.value.errors
    assert error.loc == ('beam_centre', 'tolerance')
    assert 'tolerance must be between 0 and 1' in error.msg


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
        assert alternative['required'] == ['kind']


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


def test_a_field_typed_referenced_is_filled_by_reference_only(
    client: Client, loaded: Record
) -> None:
    computed = client.compute(ADD, params={'transmission': loaded.ref('transmission')})

    assert client.output(computed, 'value') == 1.0
    ref, null = ADD.serialize().params_schema['properties']['transmission']['anyOf']
    assert (ref['dataType'], null) == ('MeasuredTransmission', {'type': 'null'})
    for inline in ({'fraction': 1.0}, MeasuredTransmission(fraction=Blob(1.0))):
        with pytest.raises(
            SubmitError,
            match=r'^transmission: is not a reference to an output, as the field is '
            r'typed Referenced\[\.\.\.\]$',
        ):
            client.submit(ADD, params={'transmission': inline})


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


def test_a_file_fits_a_file_field_only(client: Client) -> None:
    with pytest.raises(
        SubmitError, match=r'^shift: file run:1: the field is not a file field$'
    ):
        client.submit(ADD, params={'shift': file_ref(run=1)})


def test_a_file_fits_a_file_field_by_its_metadata(client: Client) -> None:
    computed = client.compute(
        MASKED, params={'run': file_ref(run=1), 'mask': file_ref(run=3)}
    )

    assert client.output(computed, 'value') == 1.0 + 10.0


@pytest.mark.parametrize(
    ('params', 'refused'),
    [
        (
            {'run': file_ref(run=3), 'mask': file_ref(run=3)},
            "run: file run:3 does not fit the field, which takes files with "
            "{'kind': 'raw'}",
        ),
        (
            {'run': file_ref(run=1), 'mask': file_ref(run=2)},
            "mask: file run:2 does not fit the field, which takes files "
            "with {'kind': 'mask'}",
        ),
    ],
)
def test_a_file_that_does_not_fit_a_file_field_is_refused(
    client: Client, params: dict[str, Any], refused: str
) -> None:
    with pytest.raises(SubmitError) as raised:
        client.submit(MASKED, params=params)
    assert str(raised.value) == refused


def test_the_schema_of_a_file_field_holds_its_metadata() -> None:
    schema = MASKED.serialize().params_schema

    assert schema['properties']['mask'] == {
        '$ref': '#/$defs/FileRef',
        'metadata': {'kind': 'mask'},
        'title': 'Mask',
    }
    assert schema['$defs']['FileRef']['properties']['@ref']['const'] == 'file'


def _failed(client: Client, record: Record) -> str:
    assert client.wait(record) is Status.FAILED
    return client.failure(record)


def test_an_inline_value_and_a_reference_to_equal_data_are_two_rows(
    client: Client, loaded: Record
) -> None:
    # Rows are distinct by what the record stores: a reference by its record
    # and output, not by the data it reads.
    summed = client.accumulator(Template(SUM, blanks=('part',)))
    summed.push(part=[loaded.ref('shift')])
    summed.push(part=[Shift(offset=1.0)])
    assert client.output(summed, 'value') == 2.0
    with pytest.raises(SubmitError, match=r'part\[2\]: repeats part\[0\]'):
        summed.push(part=[loaded.ref('shift')])
    plain = client.compute(
        SUM, params={'part': [Shift(offset=1.0), loaded.ref('shift')]}
    )
    assert client.output(plain, 'value') == 2.0


def test_references_to_two_records_with_equal_data_are_two_rows(
    client: Client, loaded: Record
) -> None:
    again = client.compute(LOAD, params={'sample': file_ref(run=1)})
    plain = client.compute(
        SUM, params={'part': [loaded.ref('shift'), again.ref('shift')]}
    )

    assert client.output(plain, 'value') == 2.0


class Sample(BaseModel):
    """A row of a table with a field that a reference fills."""

    run: RunFile
    transmission: Referenced[MeasuredTransmission]


class SamplesParams(BaseModel):
    samples: list[Sample]


SAMPLES = _spec('samples', SamplesParams, Value, tables=('samples',))


def test_a_reference_at_a_field_of_a_row_reaches_the_binding_as_its_data(
    tmp_path: Path,
) -> None:
    files = FakeFiles(proposal='p1', directory=tmp_path)
    files.measure(1, 1.0)
    made: list[MeasuredTransmission] = []
    given: list[MeasuredTransmission] = []

    def measure(params: LoadParams) -> LoadOutputs:
        outputs = load(params)
        made.append(outputs.transmission)
        return outputs

    def samples(params: SamplesParams) -> Value:
        given.extend(row.transmission for row in params.samples)
        return Value(value=sum(r.transmission.fraction.value for r in params.samples))

    backend = Backend(
        files, [function_binding(LOAD, measure), function_binding(SAMPLES, samples)]
    )
    client = Client(backend, proposal='p1', submitter='anna')
    try:
        loaded = client.compute(LOAD, params={'sample': file_ref(run=1)})
        row = {'run': file_ref(run=1), 'transmission': loaded.ref('transmission')}
        computed = client.submit(SAMPLES, params={'samples': [row]})
        assert client.output(computed, 'value') == 1.0
        assert computed.request.params['samples'][0]['transmission'] == loaded.ref(
            'transmission'
        )
        (data,) = made
        assert given == [data]
        assert given[0] is data
        with pytest.raises(SubmitError) as raised:
            client.submit(
                SAMPLES,
                params={'samples': [{**row, 'transmission': loaded.ref('shift')}]},
            )
        assert str(raised.value).startswith(
            f"samples[0].transmission: {loaded.ref('shift')}, of type Shift, does "
            'not fit MeasuredTransmission'
        )
    finally:
        backend.close()
