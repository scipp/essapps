# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from pathlib import Path
from typing import Annotated, Any, Literal

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
    FileModel,
    FileRef,
    OutputRef,
    Referenced,
    WorkflowSpec,
    request_types,
)
from ess.spec.places import alternatives, root_type
from ess.spec.refs import Loc, ParamsError, Ref
from ess.spec.request_types import held, row_sources, value_of


class RunFile(FileModel, kind='raw'):
    pass


class MaskFile(FileModel, kind='mask'):
    pass


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


def _named(name: str, model: type[FileModel]) -> str:
    """A resolver that takes each name for the identity of a file."""
    return name


def _unread(ref: Ref) -> Any:
    raise AssertionError(f'{ref} is read; a file is never read')


def _unknown(name: str, model: type[FileModel]) -> str:
    raise ValueError(f'unknown file {name}')


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

    def resolve(name: str, model: type[FileModel]) -> str:
        asked.append((name, model))
        return 'pid-2'

    value = {'run': FILE, 'masks': [FILE]}
    form = FileRef(file='pid-2').model_dump(by_alias=True)
    assert held(MeasuredParams, 'measured', value, resolve) == {
        'run': form,
        'thickness': 1.0,
        'masks': [form],
    }
    assert asked == [('pid-1', RunFile), ('pid-1', MaskFile)]


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
        held(model, name, value, _unknown)
    assert raised.value.errors == tuple((loc, 'unknown file pid-1') for loc in locs)


def test_a_file_that_a_validator_makes_is_refused() -> None:
    class Named(BaseModel):
        mask: MaskFile

        @model_validator(mode='before')
        @classmethod
        def _of_name(cls, data: Any) -> Any:
            if isinstance(data, dict) and 'name' in data:
                ref = {'@ref': 'file', 'file': data['name']}
                return {'mask': MaskFile.model_validate(ref)}
            return data

    class NamedParams(BaseModel):
        named: Named

    with pytest.raises(ParamsError) as raised:
        held(NamedParams, 'named', {'name': 'pid-1'}, _named)
    assert raised.value.errors == (
        (
            ('named', 'mask'),
            'file pid-1 was not resolved: a validator made it, so the file source '
            'has not checked it',
        ),
    )
    assert held(NamedParams, 'named', {'mask': FILE}, _named) == {
        'mask': FILE.model_dump()
    }


def test_a_discriminator_in_the_metadata_of_a_field_names_the_tag() -> None:
    tp, _, metadata = root_type(NamedCentre, 'beam_centre')
    assert alternatives(tp, *metadata) == (
        'kind',
        {'given': GivenCentre, 'find': FindCentre},
    )
    value = {'kind': 'find', 'tolerance': 0.5}
    assert held(NamedCentre, 'beam_centre', value, _named) == value
    with pytest.raises(ParamsError) as raised:
        held(NamedCentre, 'beam_centre', {'tolerance': 0.5}, _named)
    assert raised.value.errors == (
        (
            ('beam_centre',),
            "the tag 'kind' is not given; it is one of ['given', 'find']",
        ),
    )


def test_a_reference_to_an_output_at_a_parameter_is_held_as_its_json_form() -> None:
    form = OUTPUT.model_dump()
    assert held(MaskParams, 'sample', OUTPUT, _named) == form
    given = {'@ref': 'output', 'record': 'r1', 'output': 'sample'}
    assert held(MaskParams, 'sample', given, _named) == form


def test_a_reference_to_an_output_at_a_file_model_is_refused() -> None:
    with pytest.raises(ParamsError) as raised:
        held(MaskParams, 'mask', OUTPUT, _named)
    assert raised.value.errors == (
        (
            ('mask',),
            'r1.sample is a reference to an output; MaskFile is a file model, '
            'written as file(...)',
        ),
    )


def test_a_reference_to_an_output_at_an_unmarked_group_is_refused() -> None:
    with pytest.raises(ParamsError) as raised:
        held(MeasuredParams, 'measured', OUTPUT, _named)
    assert raised.value.errors == (
        (
            ('measured',),
            'r1.sample is a reference to an output; Measured is written inline, '
            'since the spec does not mark the place Referenced[...]',
        ),
    )


def test_a_file_at_a_parameter_typed_a_group_is_refused() -> None:
    with pytest.raises(ParamsError) as raised:
        held(MaskParams, 'sample', FILE, _named)
    assert raised.value.errors == (
        (('sample',), 'file pid-1 does not fit Sample, which is not a file model'),
    )


def test_a_reference_at_an_element_of_a_list_parameter_stands_for_one_row() -> None:
    assert held(MaskParams, 'runs', [FILE], _named) == [FILE.model_dump()]
    assert held(Params, 'samples', [{'thickness': 2.0}, OUTPUT], _named) == [
        {'thickness': 2.0},
        OUTPUT.model_dump(),
    ]


def test_a_reference_to_a_whole_list_is_refused() -> None:
    with pytest.raises(ParamsError) as raised:
        held(MaskParams, 'runs', OUTPUT, _named)
    assert raised.value.errors == (
        (
            ('runs',),
            'r1.sample is a reference to a whole list; a reference stands for one '
            'element of a list',
        ),
    )


def test_a_reference_to_an_output_at_an_unmarked_union_is_refused() -> None:
    with pytest.raises(ParamsError) as raised:
        held(Params, 'beam_centre', OUTPUT, _named)
    assert raised.value.errors == (
        (
            ('beam_centre',),
            'r1.sample is a reference to an output; GivenCentre | FindCentre is '
            'written inline, since the spec does not mark the place Referenced[...]',
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
    assert held(Params, name, value, _named) == form


@pytest.mark.parametrize(
    ('name', 'value', 'loc'),
    [
        ('sample', {'thickness': OUTPUT}, ('sample', 'thickness')),
        ('reduction', {'angles': [1.0, OUTPUT]}, ('reduction', 'angles', 1)),
        ('samples', [{'thickness': OUTPUT}], ('samples', 0, 'thickness')),
        ('beam_centre', {'kind': 'given', 'x': OUTPUT}, ('beam_centre', 'x')),
        ('measured', {'run': OUTPUT}, ('measured', 'run')),
    ],
)
def test_a_reference_to_an_output_inside_a_group_is_refused(
    name: str, value: Any, loc: Loc
) -> None:
    model = {'sample': MaskParams, 'measured': MeasuredParams}.get(name, Params)
    with pytest.raises(ParamsError) as raised:
        held(model, name, value, _named)
    assert raised.value.errors == (
        (
            loc,
            'r1.sample: a reference to an output fills a whole parameter or a '
            'whole row, never a field of a group',
        ),
    )


def test_a_file_at_a_field_that_is_not_a_file_model_is_refused() -> None:
    with pytest.raises(ParamsError) as raised:
        held(Params, 'reduction', {'angles': [1.0, FILE]}, _named)
    ((at, msg),) = raised.value.errors
    assert at == ('reduction', 'angles', 1)
    assert 'valid number' in msg


def test_a_group_given_inline_is_held_as_the_json_form_of_it_validated() -> None:
    assert held(Params, 'reduction', {'bins': 3}, _named) == {
        'bins': 3,
        'limits': 1.0,
        'angles': [],
    }
    assert held(Params, 'reduction', Reduction(bins=3), _named) == held(
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
        held(RangeParams, 'range', {'start': 10.0, 'stop': 5.0}, _named)
    ((at, msg),) = raised.value.errors
    assert at == ('range', 'stop')
    assert 'stop must be after start' in msg


def test_an_error_inside_a_union_names_its_place_without_the_tag() -> None:
    with pytest.raises(ParamsError) as raised:
        held(Params, 'beam_centre', {'kind': 'find', 'tolerance': -1.0}, _named)
    ((at, _),) = raised.value.errors
    assert at == ('beam_centre', 'tolerance')


def test_a_file_model_instance_is_held_as_the_reference_to_its_file() -> None:
    ref = FileRef(file='path:mask.xml')
    held_mask = held(MaskParams, 'mask', MaskFile.local('mask.xml'), _named)
    assert held_mask == ref.model_dump()


@pytest.mark.parametrize(
    ('model', 'name', 'value', 'loc'),
    [
        (MaskParams, 'mask', 'mask.xml', ('mask',)),
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
def test_a_file_model_given_inline_is_refused(
    model: type[BaseModel], name: str, value: Any, loc: Loc
) -> None:
    with pytest.raises(ParamsError) as raised:
        held(model, name, value, _named)
    ((at, msg),) = raised.value.errors
    assert at == loc
    assert 'is not a file; ' in msg
    assert 'is a file model, written as file(...)' in msg


def test_a_value_inline_at_a_file_or_an_output_names_both_references() -> None:
    class RunOrSample(BaseModel):
        run: RunFile | Referenced[Sample]

    with pytest.raises(ParamsError) as raised:
        held(RunOrSample, 'run', {'thickness': 1.0}, _named)
    assert raised.value.errors == (
        (
            ('run',),
            "{'thickness': 1.0} is not a file; RunFile | Sample is a file model, "
            'written as file(...), or a reference to an output',
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
        held(DisguisedParams, name, value, _named)
    ((at, msg),) = raised.value.errors
    assert at == loc
    assert msg.endswith(
        'the JSON form of a value written inline holds a reference to an output, '
        'which fills a whole parameter or a whole row'
    )


def test_a_value_written_inline_at_a_referenced_place_is_refused() -> None:
    with pytest.raises(ParamsError) as raised:
        held(Params, 'mask', {'mask': None}, _named)
    assert raised.value.errors == (
        (
            ('mask',),
            "{'mask': None} is not a reference; Mask is given by reference to an "
            'output',
        ),
    )


def test_a_value_held_inline_is_made_of_its_json_form() -> None:
    stored = held(Params, 'reduction', {'bins': 3}, _named)
    assert value_of(Params, 'reduction', stored, _unread, {}) == Reduction(bins=3)


def test_a_file_model_in_a_group_gets_its_local_path_from_paths() -> None:
    form = FILE.model_dump(by_alias=True)
    measured = value_of(
        MeasuredParams,
        'measured',
        {'run': form, 'thickness': 2.0, 'masks': [form]},
        _unread,
        {FILE_ID: Path('run.nxs')},
    )
    assert measured.run.path == Path('run.nxs')
    assert measured.masks[0].path == Path('run.nxs')


def test_a_file_model_is_made_of_the_reference_and_the_path_given_for_it() -> None:
    form = FILE.model_dump(by_alias=True)
    paths = {FILE_ID: Path('mask.xml')}
    made = value_of(MaskParams, 'mask', form, _unread, paths)
    assert made == MaskFile(file=FILE_ID)
    assert made.path == Path('mask.xml')
    (run,) = value_of(MaskParams, 'runs', [form], _unread, paths)
    assert run == RunFile(file=FILE_ID)
    assert run.path == Path('mask.xml')


def test_a_file_model_without_a_path_given_has_none() -> None:
    made = value_of(MaskParams, 'mask', FILE.model_dump(by_alias=True), _unread, {})
    with pytest.raises(ValueError, match='only when the request runs'):
        made.path


def test_an_output_read_for_a_reference_is_kept_as_it_is() -> None:
    class LabelledSample(Sample):
        label: str

    output = LabelledSample(thickness=2.0, label='a')
    stored = OUTPUT.model_dump(by_alias=True)
    made = value_of(MaskParams, 'sample', stored, lambda ref: output, {})
    assert made is output


class Typed(BaseModel):
    x: float = 0.0


class Computed(BaseModel):
    x: float = 0.0


class EitherParams(BaseModel):
    centre: Referenced[Computed] | Typed = Typed()


def test_each_value_is_made_by_the_type_of_its_source() -> None:
    # Computed, the first member, could validate the value written inline too.
    stored = held(EitherParams, 'centre', {'x': 1.0}, _named)
    assert type(value_of(EitherParams, 'centre', stored, _unread, {})) is Typed
    ref = OUTPUT.model_dump(by_alias=True)
    data = value_of(EitherParams, 'centre', ref, lambda ref: Computed(x=1.0), {})
    assert type(data) is Computed


def test_the_data_of_a_reference_are_checked_by_the_metadata_of_its_type() -> None:
    def thin(sample: Sample) -> Sample:
        if sample.thickness > 1.0:
            raise ValueError('too thick')
        return sample

    class Checked(BaseModel):
        sample: Referenced[Annotated[Sample, AfterValidator(thin)]]

    ref = OUTPUT.model_dump(by_alias=True)
    assert value_of(Checked, 'sample', ref, lambda ref: Sample(), {}) == Sample()
    with pytest.raises(ParamsError, match='too thick'):
        value_of(Checked, 'sample', ref, lambda ref: Sample(thickness=2.0), {})


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


def _only_pid_1(mask: MaskFile) -> MaskFile:
    if mask.file != FILE_ID:
        raise ValueError(f'only {FILE_ID} is allowed here')
    return mask


class Checked(BaseModel):
    mask: Annotated[MaskFile, AfterValidator(_only_pid_1)]
    masks: list[Annotated[MaskFile, AfterValidator(_only_pid_1)]] = []


def test_the_validators_of_a_file_model_at_a_place_run_when_it_is_written() -> None:
    assert held(Checked, 'mask', FILE, _named) == FILE.model_dump()
    other = FileRef(file='pid-2')
    places = (('mask', other, ('mask',)), ('masks', [other], ('masks', 0)))
    for name, value, loc in places:
        with pytest.raises(ParamsError) as raised:
            held(Checked, name, value, _named)
        ((at, msg),) = raised.value.errors
        assert at == loc
        assert f'only {FILE_ID} is allowed here' in msg


class TaggedCentre(BaseModel):
    """A centre that a finder outputs, with a tag of its own."""

    kind: Literal['found'] = 'found'
    x: float = 0.0


class Found(BaseModel):
    found: TaggedCentre


@pytest.mark.parametrize(
    ('annotation', 'field'),
    [
        (Centre | Referenced[TaggedCentre], Field()),
        (
            GivenCentre | FindCentre | Referenced[TaggedCentre],
            Field(discriminator='kind'),
        ),
        (GivenCentre | Referenced[TaggedCentre], Field(discriminator='kind')),
    ],
)
def test_a_discriminator_on_the_inline_union_or_the_whole_union_takes_either_way(
    annotation: Any, field: Any
) -> None:
    params = create_model('Params', centre=(annotation, field))
    WorkflowSpec(
        name='centre',
        version=1,
        title='Centre',
        description='A centre',
        params=params,
        outputs=Found,
    )
    stored = held(params, 'centre', {'kind': 'given', 'x': 1.0}, _named)
    assert value_of(params, 'centre', stored, _unread, {}) == GivenCentre(x=1.0)
    found = TaggedCentre(x=2.0)
    stored = held(params, 'centre', OUTPUT, _named)
    assert value_of(params, 'centre', stored, lambda ref: found, {}) is found
