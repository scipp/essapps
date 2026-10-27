# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal

import pytest
import scipp as sc
from annotated_types import Ge, Gt, MinLen
from pydantic import BaseModel, Field, field_validator

from ess.spec import DatasetModel, DatasetRef, OutputRef
from ess.spec.places import (
    Rows,
    Value,
    Ways,
    held,
    place,
    ref_types,
    unwritten,
    validated,
)
from ess.spec.refs import Loc, ParamsError, Ref


class RunFile(DatasetModel, kind='raw'):
    pass


class MaskFile(DatasetModel, kind='mask'):
    @field_validator('path')
    @classmethod
    def _xml(cls, path: Path) -> Path:
        if path.suffix != '.xml':
            raise ValueError('a mask is an XML file')
        return path


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


def test_a_dataset_model_takes_a_reference_to_a_dataset_only() -> None:
    assert place(MaskParams, 'mask') == Ways(
        tag=None, inline={}, record=MaskFile, none=True, metadata=()
    )


def test_a_scalar_is_written_inline_with_its_constraints() -> None:
    assert place(Reduction, 'bins') == Value(int, (Ge(1),))


def test_a_value_has_the_constraints_of_its_field_then_of_its_type() -> None:
    assert place(Reduction, 'limits') == Value(float, (Ge(1), Gt(0)))


def test_a_group_with_a_data_value_takes_a_reference_only() -> None:
    assert place(Params, 'mask') == Ways(
        tag=None, inline={}, record=Mask, none=False, metadata=()
    )


def test_a_group_of_scalars_is_written_inline_or_by_reference() -> None:
    assert place(Params, 'reduction') == Ways(
        tag=None, inline={None: Reduction}, record=Reduction, none=True, metadata=()
    )


def test_a_union_is_written_inline_with_the_tag_of_an_alternative() -> None:
    described = place(Params, 'beam_centre')
    assert isinstance(described, Ways)
    assert described.tag == 'kind'
    assert described.inline == {'given': GivenCentre, 'find': FindCentre}
    assert described.record is None
    assert not described.none


def test_a_union_typed_x_or_none_takes_none() -> None:
    class MaybeCentre(BaseModel):
        beam_centre: Centre | None = None

    described = place(MaybeCentre, 'beam_centre')
    assert isinstance(described, Ways)
    assert described.inline == {'given': GivenCentre, 'find': FindCentre}
    assert described.none


def test_a_list_parameter_describes_its_rows() -> None:
    assert place(Params, 'samples') == Rows(
        element=Ways(
            tag=None, inline={None: Sample}, record=Sample, none=False, metadata=()
        ),
        metadata=(MinLen(2),),
    )
    centres = place(Params, 'centres')
    assert isinstance(centres, Rows)
    assert isinstance(centres.element, Ways)
    assert centres.element.inline == {'given': GivenCentre, 'find': FindCentre}


def test_a_list_field_of_a_group_describes_its_scalars() -> None:
    assert place(Reduction, 'angles') == Rows(Value(float, ()), ())


def test_a_union_without_a_discriminator_is_refused() -> None:
    class Undiscriminated(BaseModel):
        beam_centre: GivenCentre | FindCentre

    with pytest.raises(ValueError, match='discriminator'):
        place(Undiscriminated, 'beam_centre')


def test_a_request_writes_no_field_of_a_dataset_model_or_a_data_value() -> None:
    assert unwritten(MaskFile) == 'path'
    assert unwritten(Mask) == 'mask'
    assert unwritten(Sample) is None


def _found(loc: Loc, ref: Ref, tp: Any) -> tuple[Loc, Ref, Any]:
    return loc, ref, tp


DATASET = DatasetRef(dataset='pid-1')
OUTPUT = OutputRef(record='r1', output='sample')


def test_a_reference_at_a_parameter_is_held_as_on_ref_gives_it() -> None:
    assert held(MaskParams, 'mask', DATASET, _found) == (('mask',), DATASET, MaskFile)
    assert held(MaskParams, 'mask', DATASET.model_dump(), _found) == (
        ('mask',),
        DATASET,
        MaskFile,
    )
    assert held(MaskParams, 'sample', OUTPUT, _found) == (('sample',), OUTPUT, Sample)


def test_a_reference_at_an_element_of_a_list_parameter_stands_for_one_row() -> None:
    assert held(MaskParams, 'runs', [DATASET], _found) == [
        (('runs', 0), DATASET, RunFile)
    ]
    assert held(Params, 'samples', [{'thickness': 2.0}, OUTPUT], _found) == [
        {'thickness': 2.0},
        (('samples', 1), OUTPUT, Sample),
    ]


def test_a_reference_to_a_whole_list_is_refused() -> None:
    with pytest.raises(ParamsError) as raised:
        held(MaskParams, 'runs', OUTPUT, _found)
    assert raised.value.errors == (
        (
            ('runs',),
            'r1.sample is a reference to a whole list; a reference stands for one '
            'element of a list',
        ),
    )


def test_no_reference_fits_a_union() -> None:
    with pytest.raises(ParamsError) as raised:
        held(Params, 'beam_centre', OUTPUT, _found)
    ((at, msg),) = raised.value.errors
    assert at == ('beam_centre',)
    assert 'a union of groups is written inline' in msg


@pytest.mark.parametrize(
    ('name', 'value', 'loc'),
    [
        ('sample', {'thickness': OUTPUT}, ('sample', 'thickness')),
        ('reduction', {'angles': [1.0, DATASET]}, ('reduction', 'angles', 1)),
        ('samples', [{'thickness': OUTPUT}], ('samples', 0, 'thickness')),
        ('beam_centre', {'kind': 'given', 'x': OUTPUT}, ('beam_centre', 'x')),
    ],
)
def test_a_reference_inside_a_group_is_refused(name: str, value: Any, loc: Loc) -> None:
    model = MaskParams if name == 'sample' else Params
    with pytest.raises(ParamsError) as raised:
        held(model, name, value, _found)
    ((at, msg),) = raised.value.errors
    assert at == loc
    assert msg.endswith(
        'a reference stands for a whole group, at a parameter or an element of a '
        'list parameter, never inside a group'
    )


def test_a_group_given_inline_is_held_as_the_json_form_of_it_validated() -> None:
    assert held(Params, 'reduction', {'bins': 3}, _found) == {
        'bins': 3,
        'limits': 1.0,
        'angles': [],
    }
    assert held(Params, 'reduction', Reduction(bins=3), _found) == held(
        Params, 'reduction', {'bins': 3}, _found
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
        held(RangeParams, 'range', {'start': 10.0, 'stop': 5.0}, _found)
    ((at, msg),) = raised.value.errors
    assert at == ('range', 'stop')
    assert 'stop must be after start' in msg


def test_an_error_inside_a_union_names_its_place_without_the_tag() -> None:
    with pytest.raises(ParamsError) as raised:
        held(Params, 'beam_centre', {'kind': 'find', 'tolerance': -1.0}, _found)
    ((at, _),) = raised.value.errors
    assert at == ('beam_centre', 'tolerance')


@pytest.mark.parametrize(
    ('name', 'value', 'loc'),
    [
        ('mask', 'mask.xml', ('mask',)),
        ('mask', {'path': 'mask.xml'}, ('mask',)),
        ('mask', MaskFile(path='mask.xml'), ('mask',)),
        ('runs', ['run.nxs'], ('runs', 0)),
    ],
)
def test_a_dataset_model_given_inline_is_refused(
    name: str, value: Any, loc: Loc
) -> None:
    with pytest.raises(ParamsError) as raised:
        held(MaskParams, name, value, _found)
    ((at, msg),) = raised.value.errors
    assert at == loc
    assert 'is not a dataset; ' in msg
    assert 'is given by reference to a dataset, such as dataset(path=...)' in msg


def test_a_group_with_a_data_value_given_inline_is_refused() -> None:
    with pytest.raises(ParamsError) as raised:
        held(Params, 'mask', {'mask': None}, _found)
    assert raised.value.errors == (
        (
            ('mask',),
            'Mask is given by reference: no request writes its field mask, of '
            'DataArray',
        ),
    )


def test_each_reference_has_the_type_of_its_parameter_or_element() -> None:
    params = {'mask': DATASET, 'sample': OUTPUT, 'runs': [DATASET, DATASET]}
    assert ref_types(MaskParams, params) == [
        (('mask',), DATASET, MaskFile),
        (('sample',), OUTPUT, Sample),
        (('runs', 0), DATASET, RunFile),
        (('runs', 1), DATASET, RunFile),
    ]


def _read(path: str) -> Callable[[Ref], dict[str, Path]]:
    return lambda ref: {'path': Path(path)}


def test_a_dataset_model_is_made_of_the_path_read_for_its_reference() -> None:
    assert validated(MaskParams, 'mask', DATASET, _read('mask.xml')) == MaskFile(
        path=Path('mask.xml')
    )
    assert validated(MaskParams, 'runs', [DATASET], _read('run.nxs')) == [
        RunFile(path=Path('run.nxs'))
    ]


def test_a_validator_of_a_dataset_model_refuses_the_path_at_its_place() -> None:
    with pytest.raises(ParamsError) as raised:
        validated(MaskParams, 'mask', DATASET, _read('mask.nxs'))
    ((at, msg),) = raised.value.errors
    assert at == ('mask', 'path')
    assert 'a mask is an XML file' in msg


def test_a_value_held_inline_is_made_of_its_json_form() -> None:
    stored = held(Params, 'reduction', {'bins': 3}, _found)
    assert validated(Params, 'reduction', stored, _read('unused')) == Reduction(bins=3)


def test_an_output_read_for_a_reference_is_kept_as_it_is() -> None:
    class LabelledSample(Sample):
        label: str

    output = LabelledSample(thickness=2.0, label='a')
    made = validated(MaskParams, 'sample', OUTPUT, lambda ref: output)
    assert made is output


def test_a_binding_gives_a_dataset_model_as_data() -> None:
    assert validated(MaskParams, 'mask', {'path': Path('mask.xml')}) == MaskFile(
        path=Path('mask.xml')
    )
