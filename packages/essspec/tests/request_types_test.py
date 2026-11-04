# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal, get_args, get_origin

import pytest
import scipp as sc
from annotated_types import Gt
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    Discriminator,
    Field,
    PlainSerializer,
    create_model,
    field_validator,
    model_validator,
)

from ess.spec import (
    File,
    FileRef,
    OutputRef,
    Referenced,
    WorkflowSpec,
    request_types,
)
from ess.spec.field_types import alternatives, field_metadata, unwrapped
from ess.spec.refs import Loc, ParamsError, Ref
from ess.spec.request_types import held, sources, validated


def _stored(
    model: type[BaseModel], name: str, value: Any, files: request_types.Files
) -> Any:
    """What the record stores of ``value``, as :func:`held` gives it."""
    stored, _ = held(model, name, value, files, _path)
    return stored


RunFile = Annotated[Path, File()]
MaskFile = Annotated[Path, File(kind='mask')]


class Mask(BaseModel, arbitrary_types_allowed=True):
    mask: sc.DataArray


class Sample(BaseModel):
    thickness: float = Field(1.0, gt=0)


class Reduction(BaseModel):
    bins: int = Field(50, ge=1)
    limits: Annotated[float, Gt(0)] = Field(1.0, ge=1)
    angles: list[float] = []


class GivenCentre(BaseModel):
    kind: Literal['given'] = 'given'
    x: float = 0.0


class FindCentre(BaseModel):
    kind: Literal['find']
    tolerance: float = Field(1e-3, gt=0)


Centre = Annotated[GivenCentre | FindCentre, Field(discriminator='kind')]


class Params(BaseModel):
    mask: Referenced[Mask]
    sample: Sample | Referenced[Sample]
    reduction: Reduction | None = None
    beam_centre: Centre
    centres: list[Centre]
    samples: list[Sample | Referenced[Sample]] = Field(min_length=2)
    found: Centre | Referenced[Mask] = GivenCentre()


class MaskParams(BaseModel):
    mask: MaskFile | None = None
    sample: Sample | Referenced[Sample] | None = None
    runs: list[RunFile] = []


class Measured(BaseModel):
    run: RunFile
    thickness: float = Field(1.0, gt=0)
    masks: list[MaskFile] = []


class MeasuredParams(BaseModel):
    measured: Measured


FILE_ID = 'pid-1'
FILE = FileRef(file=FILE_ID)

OUTPUT = OutputRef(record='r1', output='sample')


def _named(name: str, marker: File) -> str:
    """A resolver that takes each name for the identity of a file."""
    return name


def _path(identity: str) -> Path:
    """The local path of each file."""
    return Path(f'/data/{identity}.nxs')


PATHS = {FILE_ID: _path(FILE_ID)}


def _unread(ref: Ref) -> Any:
    raise AssertionError(f'{ref} is read; a file is never read')


def _unknown(name: str, marker: File) -> str:
    raise ValueError(f'unknown file {name}')


def value_of(
    model: type[BaseModel],
    name: str,
    stored: Any,
    read: Callable[[Ref], Any],
    paths: dict[str, Path],
) -> Any:
    """The value of a parameter in the call phase, from what the record stores."""
    return validated(model, name, stored, context={'read': read, 'paths': paths})


def row_sources(model: type[BaseModel], name: str) -> dict[str, Any]:
    """The sources of a parameter, or of one row of a list parameter."""
    info = model.model_fields[name]
    if get_origin(info.annotation) is list:
        tp, _, metadata = unwrapped(get_args(info.annotation)[0])
    else:
        tp, _, metadata = unwrapped(info.annotation, field_metadata(info))
    return sources(tp, metadata)


class NamedCentre(BaseModel):
    beam_centre: Annotated[GivenCentre | FindCentre, Discriminator('kind')]


class Disguised(BaseModel):
    """A group whose field a serializer dumps as a reference to an output."""

    value: Annotated[
        int,
        BeforeValidator(lambda v: int(v['key']) if isinstance(v, dict) else v),
        PlainSerializer(
            lambda v: {'@ref': 'output', 'record': 'r', 'output': 'o', 'key': str(v)}
        ),
    ] = 1


class DisguisedParams(BaseModel):
    group: Disguised
    rows: list[Disguised] = []


def test_a_file_in_a_group_is_held_as_files_resolves_it() -> None:
    asked = []

    def resolve(name: str, marker: File) -> str:
        asked.append((name, marker))
        return 'pid-2'

    value = {'run': FILE, 'masks': [FILE]}
    form = FileRef(file='pid-2').model_dump(by_alias=True)
    assert _stored(MeasuredParams, 'measured', value, resolve) == {
        'run': form,
        'thickness': 1.0,
        'masks': [form],
    }
    assert asked == [('pid-1', File()), ('pid-1', File(kind='mask'))]


@pytest.mark.parametrize(
    ('model', 'name', 'value', 'locs'),
    [
        (MaskParams, 'mask', FILE, [('mask',)]),
        (MaskParams, 'runs', [FILE], [('runs', 0)]),
        (MeasuredParams, 'measured', {'run': FILE}, [('measured', 'run')]),
        (
            MeasuredParams,
            'measured',
            {'run': FILE.model_dump(), 'masks': [FILE]},
            [('measured', 'run'), ('measured', 'masks', 0)],
        ),
    ],
)
def test_a_file_that_files_refuses_is_refused_at_its_place(
    model: type[BaseModel], name: str, value: Any, locs: list[Loc]
) -> None:
    with pytest.raises(ParamsError) as raised:
        _stored(model, name, value, _unknown)
    assert raised.value.errors == tuple((loc, 'unknown file pid-1') for loc in locs)


def test_a_group_that_holds_a_file_is_written_by_its_fields() -> None:
    class Named(BaseModel):
        mask: MaskFile

        @model_validator(mode='before')
        @classmethod
        def _of_name(cls, data: Any) -> Any:
            if isinstance(data, dict) and 'name' in data:
                return {'mask': Path(data['name'])}
            return data

    class NamedParams(BaseModel):
        named: Named

    with pytest.raises(ParamsError) as raised:
        _stored(NamedParams, 'named', {'name': 'mask.xml'}, _named)
    assert raised.value.errors == (
        (('named', 'name'), 'Extra inputs are not permitted'),
        (('named', 'mask'), 'Field required'),
    )
    assert _stored(NamedParams, 'named', {'mask': FILE}, _named) == {
        'mask': FILE.model_dump()
    }


def test_a_discriminator_in_the_metadata_of_a_field_names_the_tag() -> None:
    info = NamedCentre.model_fields['beam_centre']
    tp, _, metadata = unwrapped(info.annotation, field_metadata(info))
    assert alternatives(tp, *metadata) == (
        'kind',
        {'given': GivenCentre, 'find': FindCentre},
    )
    value = {'kind': 'find', 'tolerance': 0.5}
    assert _stored(NamedCentre, 'beam_centre', value, _named) == value
    with pytest.raises(ParamsError) as raised:
        _stored(NamedCentre, 'beam_centre', {'tolerance': 0.5}, _named)
    assert raised.value.errors == (
        (
            ('beam_centre',),
            "Unable to extract tag using discriminator 'kind'",
        ),
    )


def test_a_reference_to_an_output_at_a_parameter_is_held_as_its_json_form() -> None:
    form = OUTPUT.model_dump()
    assert _stored(MaskParams, 'sample', OUTPUT, _named) == form
    given = {'@ref': 'output', 'record': 'r1', 'output': 'sample'}
    assert _stored(MaskParams, 'sample', given, _named) == form


NOT_REFERENCED = (
    'r1.sample is a reference to an output; the field is not typed Referenced[...]'
)


def test_a_reference_to_an_output_at_a_file_model_is_refused() -> None:
    with pytest.raises(ParamsError) as raised:
        _stored(MaskParams, 'mask', OUTPUT, _named)
    assert raised.value.errors == (
        (
            ('mask',),
            NOT_REFERENCED,
        ),
    )


def test_a_reference_to_an_output_at_an_unmarked_group_is_refused() -> None:
    with pytest.raises(ParamsError) as raised:
        _stored(MeasuredParams, 'measured', OUTPUT, _named)
    assert raised.value.errors == (
        (
            ('measured',),
            NOT_REFERENCED,
        ),
    )


def test_a_file_at_a_parameter_typed_a_group_is_refused() -> None:
    with pytest.raises(ParamsError) as raised:
        _stored(MaskParams, 'sample', FILE, _named)
    assert raised.value.errors == (
        (('sample',), 'file pid-1: the field is not a file field'),
    )


def test_a_reference_at_an_element_of_a_list_parameter_stands_for_one_row() -> None:
    assert _stored(MaskParams, 'runs', [FILE], _named) == [FILE.model_dump()]
    assert _stored(Params, 'samples', [{'thickness': 2.0}, OUTPUT], _named) == [
        {'thickness': 2.0},
        OUTPUT.model_dump(),
    ]


def test_a_reference_to_a_whole_list_is_refused() -> None:
    with pytest.raises(ParamsError) as raised:
        _stored(MaskParams, 'runs', OUTPUT, _named)
    assert raised.value.errors == (
        (
            ('runs',),
            'r1.sample is a reference to a whole list; a reference stands for one '
            'element of a list',
        ),
    )


def test_a_reference_to_an_output_at_an_unmarked_union_is_refused() -> None:
    with pytest.raises(ParamsError) as raised:
        _stored(Params, 'beam_centre', OUTPUT, _named)
    assert raised.value.errors == (
        (
            ('beam_centre',),
            NOT_REFERENCED,
        ),
    )


@pytest.mark.parametrize(
    ('name', 'value'),
    [
        ('sample', {'thickness': 2.0}),
        ('sample', OUTPUT),
        ('found', {'kind': 'find', 'tolerance': 0.5}),
        ('found', OUTPUT),
    ],
)
def test_a_union_with_a_referenced_member_takes_either(name: str, value: Any) -> None:
    form = value if isinstance(value, dict) else value.model_dump()
    assert _stored(Params, name, value, _named) == form


@pytest.mark.parametrize(
    ('name', 'value', 'loc', 'msg'),
    [
        ('sample', {'thickness': OUTPUT}, ('sample', 'thickness'), NOT_REFERENCED),
        (
            'reduction',
            {'angles': [1.0, OUTPUT]},
            ('reduction', 'angles', 1),
            NOT_REFERENCED,
        ),
        (
            'samples',
            [{'thickness': OUTPUT}],
            ('samples', 0, 'thickness'),
            NOT_REFERENCED,
        ),
        (
            'beam_centre',
            {'kind': 'given', 'x': OUTPUT},
            ('beam_centre', 'x'),
            NOT_REFERENCED,
        ),
        (
            'measured',
            {'run': OUTPUT},
            ('measured', 'run'),
            NOT_REFERENCED,
        ),
    ],
)
def test_a_reference_to_an_output_at_a_field_not_typed_referenced_is_refused(
    name: str, value: Any, loc: Loc, msg: str
) -> None:
    model = {'sample': MaskParams, 'measured': MeasuredParams}.get(name, Params)
    with pytest.raises(ParamsError) as raised:
        _stored(model, name, value, _named)
    assert raised.value.errors == ((loc, msg),)


def test_a_file_at_a_field_that_is_not_a_file_model_is_refused() -> None:
    with pytest.raises(ParamsError) as raised:
        _stored(Params, 'reduction', {'angles': [1.0, FILE]}, _named)
    assert raised.value.errors == (
        (
            ('reduction', 'angles', 1),
            'file pid-1: the field is not a file field',
        ),
    )


def test_a_group_given_inline_is_held_as_the_json_form_of_it_validated() -> None:
    assert _stored(Params, 'reduction', {'bins': 3}, _named) == {
        'bins': 3,
        'limits': 1.0,
        'angles': [],
    }
    assert _stored(Params, 'reduction', Reduction(bins=3), _named) == _stored(
        Params, 'reduction', {'bins': 3}, _named
    )


def test_a_group_given_inline_is_validated_whole() -> None:
    class Range(BaseModel):
        start: float
        stop: float

        @field_validator('stop')
        @classmethod
        def _after_start(cls, stop: float, info: Any) -> float:
            if stop <= info.data['start']:
                raise ValueError('stop must be after start')
            return stop

    class RangeParams(BaseModel):
        range: Range

    with pytest.raises(ParamsError) as raised:
        _stored(RangeParams, 'range', {'start': 10.0, 'stop': 5.0}, _named)
    ((at, msg),) = raised.value.errors
    assert at == ('range', 'stop')
    assert 'stop must be after start' in msg


def test_an_error_inside_a_union_names_its_place_without_the_tag() -> None:
    with pytest.raises(ParamsError) as raised:
        _stored(Params, 'beam_centre', {'kind': 'find', 'tolerance': -1.0}, _named)
    ((at, _),) = raised.value.errors
    assert at == ('beam_centre', 'tolerance')


@pytest.mark.parametrize(
    ('model', 'name', 'value', 'loc'),
    [
        (MaskParams, 'mask', 'mask.xml', ('mask',)),
        (MaskParams, 'mask', Path('mask.xml'), ('mask',)),
        (MaskParams, 'mask', {'path': 'mask.xml'}, ('mask',)),
        (MaskParams, 'mask', {'file': 'mask.xml'}, ('mask',)),
        (MaskParams, 'runs', ['run.nxs'], ('runs', 0)),
        (
            MeasuredParams,
            'measured',
            {'run': {'file': 'run.nxs'}},
            ('measured', 'run'),
        ),
    ],
)
def test_a_file_given_inline_is_refused(
    model: type[BaseModel], name: str, value: Any, loc: Loc
) -> None:
    with pytest.raises(ParamsError) as raised:
        _stored(model, name, value, _named)
    ((at, msg),) = raised.value.errors
    assert at == loc
    assert 'is not a file; a file field is written as file(...)' in msg


def test_a_value_inline_at_a_file_or_an_output_names_the_sources_taken() -> None:
    class RunOrSample(BaseModel):
        run: RunFile | Referenced[Sample]

    with pytest.raises(ParamsError) as raised:
        _stored(RunOrSample, 'run', {'thickness': 1.0}, _named)
    assert raised.value.errors == (
        (
            ('run',),
            'the field takes a file or a reference to an output',
        ),
    )


@pytest.mark.parametrize(
    ('name', 'value', 'loc'),
    [('group', {'value': 2}, ('group', 'value')), ('rows', [{}], ('rows', 0, 'value'))],
)
def test_a_reference_to_an_output_in_a_json_form_written_inline_is_refused(
    name: str, value: Any, loc: Loc
) -> None:
    with pytest.raises(ParamsError) as raised:
        _stored(DisguisedParams, name, value, _named)
    ((at, msg),) = raised.value.errors
    assert at == loc
    assert msg.endswith(
        'the JSON form of a value written inline holds a reference to an output '
        'that the request did not give'
    )


def test_a_value_written_inline_at_a_referenced_place_is_refused() -> None:
    with pytest.raises(ParamsError) as raised:
        _stored(Params, 'mask', {'mask': None}, _named)
    assert raised.value.errors == (
        (
            ('mask',),
            'is not a reference to an output, as the field is typed Referenced[...]',
        ),
    )


def test_a_value_held_inline_is_made_of_its_json_form() -> None:
    stored = _stored(Params, 'reduction', {'bins': 3}, _named)
    assert value_of(Params, 'reduction', stored, _unread, PATHS) == Reduction(bins=3)


def test_a_file_in_a_group_is_its_local_path_from_paths() -> None:
    form = FILE.model_dump(by_alias=True)
    measured = value_of(
        MeasuredParams,
        'measured',
        {'run': form, 'thickness': 2.0, 'masks': [form]},
        _unread,
        {FILE_ID: Path('run.nxs')},
    )
    assert measured == Measured(
        run=Path('run.nxs'), thickness=2.0, masks=[Path('run.nxs')]
    )


def test_a_file_is_the_local_path_given_for_it() -> None:
    form = FILE.model_dump(by_alias=True)
    paths = {FILE_ID: Path('mask.xml')}
    assert value_of(MaskParams, 'mask', form, _unread, paths) == Path('mask.xml')
    assert value_of(MaskParams, 'runs', [form], _unread, paths) == [Path('mask.xml')]


def test_a_file_without_a_local_path_is_refused() -> None:
    form = FILE.model_dump(by_alias=True)
    with pytest.raises(ParamsError) as raised:
        value_of(MaskParams, 'mask', form, _unread, {})
    assert raised.value.errors == ((('mask',), 'file pid-1 has no local path'),)


def test_an_output_read_for_a_reference_is_kept_as_it_is() -> None:
    class LabelledSample(Sample):
        label: str

    output = LabelledSample(thickness=2.0, label='a')
    stored = OUTPUT.model_dump(by_alias=True)
    made = value_of(MaskParams, 'sample', stored, lambda ref: output, PATHS)
    assert made is output


class Typed(BaseModel):
    x: float = 0.0


class Computed(BaseModel):
    x: float = 0.0


class EitherParams(BaseModel):
    centre: Referenced[Computed] | Typed = Typed()


def test_each_value_is_made_by_the_type_of_its_source() -> None:
    # Computed, the first member, could validate the value written inline too.
    stored = _stored(EitherParams, 'centre', {'x': 1.0}, _named)
    assert type(value_of(EitherParams, 'centre', stored, _unread, PATHS)) is Typed
    ref = OUTPUT.model_dump(by_alias=True)
    data = value_of(EitherParams, 'centre', ref, lambda ref: Computed(x=1.0), PATHS)
    assert type(data) is Computed


def test_the_data_of_a_reference_are_checked_by_the_metadata_of_its_type() -> None:
    def thin(sample: Sample) -> Sample:
        if sample.thickness > 1.0:
            raise ValueError('too thick')
        return sample

    class Checked(BaseModel):
        sample: Referenced[Annotated[Sample, AfterValidator(thin)]]

    ref = OUTPUT.model_dump(by_alias=True)
    assert value_of(Checked, 'sample', ref, lambda ref: Sample(), PATHS) == Sample()
    with pytest.raises(ParamsError, match='too thick'):
        value_of(Checked, 'sample', ref, lambda ref: Sample(thickness=2.0), PATHS)


def test_each_place_takes_the_forms_its_type_allows() -> None:
    inline, file, output = (
        request_types.INLINE,
        request_types.FILE,
        request_types.OUTPUT,
    )
    assert list(row_sources(Params, 'reduction')) == [inline]
    assert list(row_sources(Params, 'sample')) == [inline, output]
    assert list(row_sources(Params, 'mask')) == [output]
    assert list(row_sources(Params, 'beam_centre')) == [inline]
    assert list(row_sources(Params, 'found')) == [inline, output]
    assert list(row_sources(Params, 'samples')) == [inline, output]
    assert list(row_sources(MaskParams, 'mask')) == [file]
    assert list(row_sources(MaskParams, 'runs')) == [file]
    assert list(row_sources(MeasuredParams, 'measured')) == [inline]


def _nexus(path: Path) -> Path:
    if path.suffix != '.nxs':
        raise ValueError('only a NeXus file is allowed here')
    return path


class Checked(BaseModel):
    mask: Annotated[MaskFile, AfterValidator(_nexus)]
    masks: list[Annotated[MaskFile, AfterValidator(_nexus)]] = []


def test_the_validators_of_a_file_field_run_on_its_local_path_when_written() -> None:
    assert _stored(Checked, 'mask', FILE, _named) == FILE.model_dump()
    places = (('mask', FILE, ('mask',)), ('masks', [FILE], ('masks', 0)))
    for name, value, loc in places:
        with pytest.raises(ParamsError) as raised:
            held(Checked, name, value, _named, lambda identity: Path('mask.xml'))
        ((at, msg),) = raised.value.errors
        assert at == loc
        assert 'only a NeXus file is allowed here' in msg


def test_the_validators_of_a_group_that_holds_a_file_run_when_it_is_written() -> None:
    class Measurement(BaseModel):
        run: RunFile

        @model_validator(mode='after')
        def _nexus(self) -> 'Measurement':
            if self.run.suffix != '.nxs':
                raise ValueError('not a NeXus file')
            return self

    class MeasurementParams(BaseModel):
        measurement: Measurement

    value = {'run': FILE}
    assert _stored(MeasurementParams, 'measurement', value, _named) == {
        'run': FILE.model_dump()
    }
    with pytest.raises(ParamsError) as raised:
        held(MeasurementParams, 'measurement', value, _named, lambda i: Path('a.xml'))
    ((at, msg),) = raised.value.errors
    assert at == ('measurement',)
    assert 'not a NeXus file' in msg


def test_a_file_whose_local_path_cannot_be_had_is_refused_at_its_place() -> None:
    def unreadable(identity: str) -> Path:
        raise OSError('offline')

    with pytest.raises(ParamsError) as raised:
        held(MeasuredParams, 'measured', {'run': FILE}, _named, unreadable)
    assert raised.value.errors == (
        (('measured', 'run'), 'file pid-1 cannot be had: offline'),
    )


def test_a_parameter_that_holds_a_reference_gets_no_local_paths_when_written() -> None:
    class Specimen(BaseModel):
        run: RunFile
        sample: Referenced[Sample]

    class SpecimenParams(BaseModel):
        specimen: Specimen

    def unreadable(identity: str) -> Path:
        raise OSError('offline')

    value = {'run': FILE, 'sample': OUTPUT}
    stored, _ = held(SpecimenParams, 'specimen', value, _named, unreadable)
    assert stored == {'run': FILE.model_dump(), 'sample': OUTPUT.model_dump()}


class TaggedCentre(BaseModel):
    """A centre that a finder outputs, with a tag of its own."""

    kind: Literal['found'] = 'found'
    x: float = 0.0


class Found(BaseModel):
    found: TaggedCentre


def test_a_discriminator_on_the_union_written_inline_takes_either_way() -> None:
    params = create_model('Params', centre=(Centre | Referenced[TaggedCentre], Field()))
    WorkflowSpec(
        name='centre',
        version=1,
        title='Centre',
        description='A centre',
        params=params,
        outputs=Found,
    )
    stored = _stored(params, 'centre', {'kind': 'given', 'x': 1.0}, _named)
    assert value_of(params, 'centre', stored, _unread, PATHS) == GivenCentre(x=1.0)
    found = TaggedCentre(x=2.0)
    stored = _stored(params, 'centre', OUTPUT, _named)
    assert value_of(params, 'centre', stored, lambda ref: found, PATHS) is found


@pytest.mark.parametrize(
    'annotation',
    [
        GivenCentre | FindCentre | Referenced[TaggedCentre],
        GivenCentre | Referenced[TaggedCentre],
    ],
)
def test_a_discriminator_on_a_whole_union_with_a_referenced_member_is_refused(
    annotation: Any,
) -> None:
    params = create_model('Params', centre=(annotation, Field(discriminator='kind')))
    with pytest.raises(ValueError, match='is on the union written inline'):
        WorkflowSpec(
            name='centre',
            version=1,
            title='Centre',
            description='A centre',
            params=params,
            outputs=Found,
        )


class Window(BaseModel):
    start: float = 0.0
    stop: float | None = None


class Transmission(BaseModel, arbitrary_types_allowed=True):
    fraction: sc.Variable


class Specimen(BaseModel):
    run: RunFile
    window: Window = Window()
    windows: list[Window] = []
    transmission: Referenced[Transmission] | None = None
    centre: Referenced[Computed] | Typed = Typed()


class SpecimenParams(BaseModel):
    specimen: Specimen
    specimens: list[Specimen] = []


class SpecimenOutputs(BaseModel, arbitrary_types_allowed=True):
    transmission: Transmission
    centre: Computed


def _spec(params: type[BaseModel]) -> WorkflowSpec:
    return WorkflowSpec(
        name='specimen',
        version=1,
        title='Specimen',
        description='A specimen',
        params=params,
        outputs=SpecimenOutputs,
    )


def test_a_group_holds_groups_and_lists_of_groups_written_inline() -> None:
    _spec(SpecimenParams)
    value = {
        'run': FILE,
        'window': {'start': 1.0, 'stop': 2.0},
        'windows': [{'stop': None}, {'start': 3.0}],
    }
    stored = _stored(SpecimenParams, 'specimen', value, _named)
    assert stored['window'] == {'start': 1.0, 'stop': 2.0}
    assert stored['windows'] == [
        {'start': 0.0, 'stop': None},
        {'start': 3.0, 'stop': None},
    ]
    made = value_of(SpecimenParams, 'specimen', stored, _unread, PATHS)
    assert made.window == Window(start=1.0, stop=2.0)
    assert made.windows == [Window(), Window(start=3.0)]


def test_a_field_of_a_nested_group_is_validated_at_its_position() -> None:
    value = {'run': FILE, 'windows': [{}, {'stop': 'x'}]}
    with pytest.raises(ParamsError) as raised:
        _stored(SpecimenParams, 'specimen', value, _named)
    ((at, msg),) = raised.value.errors
    assert at == ('specimen', 'windows', 1, 'stop')
    assert 'valid number' in msg


def test_a_field_typed_x_or_none_takes_none_or_x() -> None:
    for stop in (None, 2.0):
        stored = _stored(
            SpecimenParams, 'specimen', {'run': FILE, 'window': {'stop': stop}}, _named
        )
        made = value_of(SpecimenParams, 'specimen', stored, _unread, PATHS)
        assert made.window.stop == stop


def test_a_reference_at_a_field_of_a_row_is_replaced_by_its_data() -> None:
    data = Transmission(fraction=sc.scalar(0.5))
    rows = [{'run': FILE, 'transmission': OUTPUT}, {'run': FILE}]
    stored = _stored(SpecimenParams, 'specimens', rows, _named)
    assert stored[0]['transmission'] == OUTPUT.model_dump()
    made = value_of(SpecimenParams, 'specimens', stored, lambda ref: data, PATHS)
    assert made[0].transmission is data
    assert made[1].transmission is None


def test_a_field_typed_referenced_or_a_group_takes_either_by_its_source() -> None:
    # Computed, the referenced member, has the fields of Typed, and comes first.
    computed = Computed(x=7.0)
    for centre, made_type in ((OUTPUT, Computed), ({'x': 1.0}, Typed), ({}, Typed)):
        value = {'run': FILE, 'centre': centre}
        stored = _stored(SpecimenParams, 'specimen', value, _named)
        made = value_of(SpecimenParams, 'specimen', stored, lambda ref: computed, PATHS)
        assert type(made.centre) is made_type
    assert made.centre == Typed()


def test_a_group_that_holds_a_reference_runs_its_validators_when_it_runs() -> None:
    class Checked(BaseModel):
        transmission: Referenced[Transmission]

        @model_validator(mode='after')
        def _dimensionless(self) -> 'Checked':
            if self.transmission.fraction.unit != sc.units.one:
                raise ValueError('a fraction is dimensionless')
            return self

    class CheckedParams(BaseModel):
        checked: Checked

    _spec(CheckedParams)
    stored = _stored(CheckedParams, 'checked', {'transmission': OUTPUT}, _named)
    good = Transmission(fraction=sc.scalar(0.5))
    made = value_of(CheckedParams, 'checked', stored, lambda ref: good, PATHS)
    assert made.transmission is good
    bad = Transmission(fraction=sc.scalar(0.5, unit='m'))
    with pytest.raises(ParamsError, match='a fraction is dimensionless'):
        value_of(CheckedParams, 'checked', stored, lambda ref: bad, PATHS)


def test_a_value_written_inline_at_a_field_typed_referenced_is_refused() -> None:
    value = {'run': FILE, 'transmission': {'fraction': 1.0}}
    with pytest.raises(ParamsError) as raised:
        _stored(SpecimenParams, 'specimen', value, _named)
    assert raised.value.errors == (
        (
            ('specimen', 'transmission'),
            'is not a reference to an output, as the field is typed Referenced[...]',
        ),
    )


def test_each_reference_to_an_output_is_given_with_the_type_its_data_fit() -> None:
    other = OutputRef(record='r2', output='transmission')
    rows = [
        {'run': FILE, 'transmission': other},
        {'run': FILE, 'centre': OUTPUT},
        {'run': FILE, 'centre': {'x': 1.0}},
    ]
    _, reads = held(SpecimenParams, 'specimens', rows, _named, _path)
    assert reads == [
        (('specimens', 0, 'transmission'), other, Transmission),
        (('specimens', 1, 'centre'), OUTPUT, Computed),
    ]
    _, reads = held(Params, 'samples', [{'thickness': 2.0}, OUTPUT], _named, _path)
    assert reads == [(('samples', 1), OUTPUT, Sample)]


def test_a_group_that_contains_itself_is_refused() -> None:
    class Node(BaseModel):
        child: 'Node | None' = None

    class Outer(BaseModel):
        nodes: list[Node] = []

    class NodeParams(BaseModel):
        outer: Outer

    with pytest.raises(
        ValueError, match=r'NodeParams\.outer: Outer\.nodes: .*Node contains itself'
    ):
        _spec(NodeParams)


def test_the_default_of_a_group_that_holds_a_reference_is_held_as_its_form() -> None:
    class Holder(BaseModel):
        transmission: Referenced[Transmission] | None = None
        x: float = 1.0

    class Outer(BaseModel):
        holder: Holder = Holder(x=2.0)
        holders: list[Holder] = [Holder()]

    class OuterParams(BaseModel):
        outer: Outer

    _spec(OuterParams)
    assert _stored(OuterParams, 'outer', {}, _named) == {
        'holder': {'transmission': None, 'x': 2.0},
        'holders': [{'transmission': None, 'x': 1.0}],
    }


class Scaled(BaseModel):
    """An alternative whose tag is the name of one of its fields."""

    kind: Literal['scale'] = 'scale'
    scale: float = 1.0
    unit: str = 'm'
    transmission: Referenced[Transmission]

    @field_validator('unit')
    @classmethod
    def _length(cls, unit: str) -> str:
        if unit not in ('m', 'mm'):
            raise ValueError('not a length')
        return unit

    @model_validator(mode='after')
    def _positive(self) -> 'Scaled':
        if self.scale <= 0:
            raise ValueError('the scale is not positive')
        return self


class Fixed(BaseModel):
    kind: Literal['fixed']


class Corrected(BaseModel):
    corr: (
        Annotated[Scaled | Fixed, Field(discriminator='kind')]
        | Referenced[Transmission]
    )


class CorrectedParams(BaseModel):
    rows: list[Corrected] = []


def _run_errors(stored: Any, read: Callable[[Ref], Any]) -> tuple[Any, ...]:
    """The errors of the call phase of ``rows``, from what the record stores."""
    with pytest.raises(ParamsError) as raised:
        value_of(CorrectedParams, 'rows', stored, read, PATHS)
    return raised.value.errors


def test_a_request_error_at_a_field_named_like_a_tag_is_at_its_field() -> None:
    corr = {'kind': 'scale', 'scale': 2.0, 'unit': 1, 'transmission': OUTPUT}
    with pytest.raises(ParamsError) as raised:
        held(CorrectedParams, 'rows', [{'corr': corr}], _named, _path)
    assert raised.value.errors == (
        (('rows', 0, 'corr', 'unit'), 'Input should be a valid string'),
    )


@pytest.mark.parametrize(
    ('corr', 'error'),
    [
        (
            {'unit': 's'},
            (('rows', 0, 'corr', 'unit'), 'Value error, not a length'),
        ),
        (
            {'scale': -1.0},
            (('rows', 0, 'corr'), 'Value error, the scale is not positive'),
        ),
    ],
)
def test_a_call_error_is_at_its_position_in_the_stored_value_once(
    corr: dict[str, Any], error: tuple[Loc, str]
) -> None:
    data = Transmission(fraction=sc.scalar(0.5))
    row = {'corr': {'kind': 'scale', 'transmission': OUTPUT, **corr}}
    stored, _ = held(CorrectedParams, 'rows', [row], _named, _path)
    assert _run_errors(stored, lambda ref: data) == (error,)


def test_data_that_fail_a_check_of_their_type_are_one_error_at_the_reference() -> None:
    def thin(sample: Sample) -> Sample:
        if sample.thickness > 1.0:
            raise ValueError('too thick')
        return sample

    class Either(BaseModel):
        sample: Sample | Referenced[Annotated[Sample, AfterValidator(thin)]]

    class EitherSamples(BaseModel):
        samples: list[Either] = []

    stored, _ = held(EitherSamples, 'samples', [{'sample': OUTPUT}], _named, _path)
    with pytest.raises(ParamsError) as raised:
        value_of(
            EitherSamples, 'samples', stored, lambda r: Sample(thickness=2.0), PATHS
        )
    assert raised.value.errors == (
        (('samples', 0, 'sample'), 'Value error, too thick'),
    )


def test_a_union_of_referenced_groups_takes_the_data_of_any_member() -> None:
    class Pick(BaseModel):
        pick: Referenced[Computed] | Referenced[Transmission]

    data = Transmission(fraction=sc.scalar(0.5))
    ref = OUTPUT.model_dump(by_alias=True)
    assert value_of(Pick, 'pick', ref, lambda r: data, PATHS) is data


def test_a_list_is_validated_whole_and_a_table_without_its_length() -> None:
    one_row = [{'thickness': 2.0}]
    with pytest.raises(ParamsError) as raised:
        held(Params, 'samples', one_row, _named, _path)
    assert raised.value.errors == (
        (('samples',), 'List should have at least 2 items after validation, not 1'),
    )
    stored, _ = held(Params, 'samples', one_row, _named, _path, table=True)
    assert stored == one_row
    assert validated(Params, 'samples', one_row, table=True) == [Sample(thickness=2)]


def test_in_direct_use_a_file_field_takes_a_path() -> None:
    assert validated(MaskParams, 'mask', 'mask.xml') == Path('mask.xml')
    assert validated(MaskParams, 'runs', [Path('run.nxs')]) == [Path('run.nxs')]
