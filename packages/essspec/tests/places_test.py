# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal

import pytest
import scipp as sc
from annotated_types import Gt
from pydantic import (
    BaseModel,
    Discriminator,
    Field,
    Strict,
    create_model,
    field_validator,
    model_validator,
)

from ess.spec import FileModel, FileRef, OutputRef
from ess.spec.places import (
    alternatives,
    held,
    ref_types,
    root_type,
    takes_output,
    unwritten,
    validated,
)
from ess.spec.refs import Loc, ParamsError, Ref


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
    mask: Mask
    sample: Sample
    reduction: Reduction | None = None
    beam_centre: Centre
    centres: list[Centre]
    samples: list[Sample] = Field(min_length=2)


class MaskParams(BaseModel):
    mask: MaskFile | None = None
    sample: Sample | None = None
    runs: list[RunFile] = []


class Measured(BaseModel):
    run: RunFile
    thickness: float = Field(1.0, gt=0)
    masks: list[MaskFile] = []


class MeasuredParams(BaseModel):
    measured: Measured


FILE = FileRef(file='pid-1')
OUTPUT = OutputRef(record='r1', output='sample')


def _named(name: str, model: type[FileModel]) -> str:
    """A resolver that takes each name for the identity of a file."""
    return name


def test_a_file_model_takes_no_output() -> None:
    assert root_type(MaskParams, 'mask') == (MaskFile, True, ())
    assert not takes_output(MaskFile)


def test_a_group_holding_a_file_model_is_written_inline_only() -> None:
    assert root_type(MeasuredParams, 'measured') == (Measured, False, ())
    assert unwritten(Measured) is None
    assert not takes_output(Measured)


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


def _unknown(name: str, model: type[FileModel]) -> str:
    raise ValueError(f'unknown file {name}')


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


def _unread(ref: Ref) -> Any:
    raise AssertionError(f'{ref} is read; a file is never read')


def test_a_file_model_in_a_group_gets_its_local_path_from_paths() -> None:
    form = FILE.model_dump(by_alias=True)
    measured = validated(
        MeasuredParams,
        'measured',
        {'run': form, 'thickness': 2.0, 'masks': [form]},
        read=_unread,
        paths={FILE.file: Path('run.nxs')},
    )
    assert measured.run.path == Path('run.nxs')
    assert measured.masks[0].path == Path('run.nxs')


def test_a_group_with_a_data_value_takes_a_reference_only() -> None:
    assert root_type(Params, 'mask') == (Mask, False, ())
    assert unwritten(Mask) == 'mask'
    assert takes_output(Mask)


def test_a_group_of_scalars_is_written_inline_or_by_reference() -> None:
    assert root_type(Params, 'reduction') == (Reduction, True, ())
    assert unwritten(Reduction) is None
    assert takes_output(Reduction)


def test_the_metadata_of_the_field_comes_before_that_in_its_type() -> None:
    class Strictly(BaseModel):
        reduction: Annotated[Reduction, 'inner'] | None = Field(None, strict=True)

    assert root_type(Strictly, 'reduction') == (Reduction, True, (Strict(), 'inner'))


def test_a_union_is_written_inline_with_the_tag_of_an_alternative() -> None:
    tp, none, metadata = root_type(Params, 'beam_centre')
    assert not none
    assert alternatives(tp, *metadata) == (
        'kind',
        {'given': GivenCentre, 'find': FindCentre},
    )
    assert not takes_output(tp)


def test_a_union_typed_x_or_none_takes_none() -> None:
    class MaybeCentre(BaseModel):
        beam_centre: Centre | None = None

    tp, none, metadata = root_type(MaybeCentre, 'beam_centre')
    assert none
    assert alternatives(tp, *metadata) == (
        'kind',
        {'given': GivenCentre, 'find': FindCentre},
    )


def test_the_discriminator_of_a_field_is_in_its_metadata() -> None:
    class ByField(BaseModel):
        beam_centre: GivenCentre | FindCentre = Field(discriminator='kind')

    tp, _, metadata = root_type(ByField, 'beam_centre')
    assert alternatives(tp, *metadata) == (
        'kind',
        {'given': GivenCentre, 'find': FindCentre},
    )


class NamedCentre(BaseModel):
    beam_centre: Annotated[GivenCentre | FindCentre, Discriminator('kind')]


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


def test_the_root_type_of_a_list_parameter_is_that_of_an_element() -> None:
    assert root_type(Params, 'samples') == (Sample, False, ())
    tp, none, metadata = root_type(Params, 'centres')
    assert not none
    assert alternatives(tp, *metadata) == (
        'kind',
        {'given': GivenCentre, 'find': FindCentre},
    )


def test_a_union_without_a_discriminator_is_refused() -> None:
    class Undiscriminated(BaseModel):
        beam_centre: GivenCentre | FindCentre

    tp, _, metadata = root_type(Undiscriminated, 'beam_centre')
    with pytest.raises(ValueError, match='discriminator'):
        alternatives(tp, *metadata)


def test_a_request_writes_no_data_value() -> None:
    assert unwritten(MaskFile) is None
    assert unwritten(Mask) == 'mask'
    assert unwritten(Sample) is None


def test_a_reference_to_an_output_at_a_parameter_is_held_as_its_json_form() -> None:
    form = OUTPUT.model_dump()
    assert held(MaskParams, 'sample', OUTPUT, _named) == form
    given = {'@ref': 'output', 'record': 'r1', 'output': 'sample'}
    assert held(MaskParams, 'sample', given, _named) == form
    # Whether its data fit is checked at submit, over ref_types.
    assert held(MaskParams, 'mask', OUTPUT, _named) == form


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


def test_no_reference_fits_a_union() -> None:
    with pytest.raises(ParamsError) as raised:
        held(Params, 'beam_centre', OUTPUT, _named)
    ((at, msg),) = raised.value.errors
    assert at == ('beam_centre',)
    assert 'a union of groups is written inline' in msg


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
    assert 'is not a reference; ' in msg
    assert 'is given by reference to a file, such as file(path=...)' in msg


class Tagged(Enum):
    """An enum whose value is an object: not a scalar."""

    a = {'@ref': 'output'}  # noqa: RUF012


@pytest.mark.parametrize(
    ('tp', 'value', 'shown'),
    [
        (float | None, 1.0, 'float | None'),
        (tuple[float, float], [0.0, 1.0], 'tuple'),
        (Sample, {'thickness': 2.0}, 'Sample'),
        (Tagged, {'@ref': 'output'}, 'Tagged'),
    ],
)
def test_a_group_with_a_field_that_is_not_interpreted_is_given_by_reference(
    tp: Any, value: Any, shown: str
) -> None:
    group = create_model('Group', value=(tp, ...))
    params = create_model('P', group=(group, ...))
    assert unwritten(group) == 'value'
    assert takes_output(group)
    with pytest.raises(ParamsError) as raised:
        held(params, 'group', {'value': value}, _named)
    assert raised.value.errors == (
        (
            ('group',),
            f'Group is given by reference: no request writes its field value, of '
            f'{shown}',
        ),
    )


def test_a_group_with_a_data_value_given_inline_is_refused() -> None:
    with pytest.raises(ParamsError) as raised:
        held(Params, 'mask', {'mask': None}, _named)
    assert raised.value.errors == (
        (
            ('mask',),
            'Mask is given by reference: no request writes its field mask, of '
            'DataArray',
        ),
    )


def test_each_reference_to_an_output_has_the_type_of_its_parameter_or_element() -> None:
    params = {'mask': FILE, 'sample': OUTPUT, 'runs': [FILE, FILE]}
    assert ref_types(MaskParams, params) == [(('sample',), OUTPUT, Sample)]
    assert ref_types(Params, {'samples': [OUTPUT]}) == [
        (('samples', 0), OUTPUT, Sample)
    ]


def test_a_file_model_is_made_of_the_reference_and_the_path_given_for_it() -> None:
    paths = {FILE.file: Path('mask.xml')}
    made = validated(MaskParams, 'mask', FILE, _unread, paths)
    assert made == MaskFile(file=FILE.file)
    assert made.path == Path('mask.xml')
    (run,) = validated(MaskParams, 'runs', [FILE], _unread, paths)
    assert run == RunFile(file=FILE.file)
    assert run.path == Path('mask.xml')


def test_a_file_model_without_a_path_given_has_none() -> None:
    made = validated(MaskParams, 'mask', FILE, _unread)
    with pytest.raises(ValueError, match='only when the request runs'):
        made.path


def test_a_value_held_inline_is_made_of_its_json_form() -> None:
    stored = held(Params, 'reduction', {'bins': 3}, _named)
    assert validated(Params, 'reduction', stored, _unread) == Reduction(bins=3)


def test_an_output_read_for_a_reference_is_kept_as_it_is() -> None:
    class LabelledSample(Sample):
        label: str

    output = LabelledSample(thickness=2.0, label='a')
    made = validated(MaskParams, 'sample', OUTPUT, lambda ref: output)
    assert made is output


def test_a_binding_gives_a_file_model_as_data() -> None:
    given = validated(MaskParams, 'mask', {'file': 'pid-1'})
    assert given == MaskFile(file='pid-1')
