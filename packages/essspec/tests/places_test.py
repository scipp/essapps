# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal

import pytest
import scipp as sc
from annotated_types import Ge, Gt, MinLen
from pydantic import BaseModel, Field, field_validator

from ess.spec import DatasetModel, DatasetRef
from ess.spec.places import Rows, Value, Ways, held, place, unwritten, validated
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
    file: RunFile
    thickness: float = Field(1.0, gt=0)
    runs: list[RunFile] = []


class Reduction(BaseModel):
    bins: int = Field(50, ge=1)
    limits: Annotated[float, Gt(0)] = Field(1.0, ge=1)
    angles: list[float] = []


class GivenCentre(BaseModel):
    kind: Literal['given'] = 'given'
    x: float = 0.0


class FindCentre(BaseModel):
    kind: Literal['find']
    file: RunFile


class Params(BaseModel):
    mask: Mask
    sample: Sample
    reduction: Reduction | None = None
    beam_centre: Annotated[GivenCentre | FindCentre, Field(discriminator='kind')]
    centres: list[Annotated[GivenCentre | FindCentre, Field(discriminator='kind')]]
    samples: list[Sample] = Field(min_length=2)


class MaskParams(BaseModel):
    mask: MaskFile | None = None
    sample: Sample | None = None
    runs: list[RunFile] = []


def test_a_dataset_model_takes_a_reference_to_a_dataset_only() -> None:
    assert place(Sample, 'file') == Ways(
        tag=None, inline={}, record=RunFile, none=False, metadata=()
    )
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


def test_a_group_with_a_dataset_model_is_written_inline_only() -> None:
    assert place(Params, 'sample') == Ways(
        tag=None, inline={None: Sample}, record=None, none=False, metadata=()
    )


def test_a_plain_group_is_written_inline_or_by_reference() -> None:
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
        beam_centre: (
            Annotated[GivenCentre | FindCentre, Field(discriminator='kind')] | None
        ) = None

    described = place(MaybeCentre, 'beam_centre')
    assert isinstance(described, Ways)
    assert described.inline == {'given': GivenCentre, 'find': FindCentre}
    assert described.none


def test_a_list_parameter_describes_its_rows() -> None:
    assert place(Params, 'samples') == Rows(
        element=Ways(
            tag=None, inline={None: Sample}, record=None, none=False, metadata=()
        ),
        metadata=(MinLen(2),),
    )
    centres = place(Params, 'centres')
    assert isinstance(centres, Rows)
    assert isinstance(centres.element, Ways)
    assert centres.element.inline == {'given': GivenCentre, 'find': FindCentre}


def test_a_list_field_of_a_group_describes_its_elements() -> None:
    assert place(Sample, 'runs') == Rows(
        Ways(tag=None, inline={}, record=RunFile, none=False, metadata=()), ()
    )
    assert place(Reduction, 'angles') == Rows(Value(float, ()), ())


def test_a_union_without_a_discriminator_is_refused() -> None:
    class Undiscriminated(BaseModel):
        beam_centre: GivenCentre | FindCentre

    with pytest.raises(ValueError, match='discriminator'):
        place(Undiscriminated, 'beam_centre')


def test_a_request_writes_no_field_of_a_dataset_model() -> None:
    assert unwritten(MaskFile) == 'path'
    assert unwritten(Sample) is None


def _found(loc: Loc, ref: Ref, tp: Any) -> tuple[Ref, Any]:
    return ref, tp


DATASET = DatasetRef(dataset='pid-1')


def test_a_reference_to_a_dataset_is_held_as_on_ref_gives_it() -> None:
    assert held(MaskParams, 'mask', DATASET, _found) == (DATASET, MaskFile)
    assert held(MaskParams, 'mask', DATASET.model_dump(), _found) == (
        DATASET,
        MaskFile,
    )
    assert held(MaskParams, 'runs', [DATASET], _found) == [(DATASET, RunFile)]
    assert held(MaskParams, 'sample', {'file': DATASET}, _found) == {
        'file': (DATASET, RunFile),
        'thickness': 1.0,
        'runs': [],
    }


@pytest.mark.parametrize(
    ('name', 'value', 'loc'),
    [
        ('mask', 'mask.xml', ('mask',)),
        ('mask', {'path': 'mask.xml'}, ('mask',)),
        ('mask', MaskFile(path='mask.xml'), ('mask',)),
        ('runs', ['run.nxs'], ('runs', 0)),
        ('sample', {'file': 'run.nxs'}, ('sample', 'file')),
        ('sample', {'file': {'path': 'run.nxs'}}, ('sample', 'file')),
        ('sample', {'file': RunFile(path='run.nxs')}, ('sample', 'file')),
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


def test_a_binding_gives_a_dataset_model_as_data() -> None:
    assert validated(
        MaskParams, 'sample', {'file': {'path': Path('run.nxs')}}
    ) == Sample(file=RunFile(path=Path('run.nxs')))
