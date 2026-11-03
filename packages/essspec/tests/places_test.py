# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from typing import Annotated, Any, Literal

import pytest
import scipp as sc
from annotated_types import Gt
from pydantic import BaseModel, Field, Strict

from ess.spec import FileModel, FileRef, OutputRef, Referenced
from ess.spec.parameters import REFERENCED
from ess.spec.places import (
    alternatives,
    ref_types,
    root_type,
    split,
    unwritten,
    validated,
)
from ess.spec.refs import Ref


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
    sample: Sample
    reduction: Reduction | None = None
    beam_centre: Centre
    centres: list[Centre]
    samples: list[Sample | Referenced[Sample]] = Field(min_length=2)
    found: Centre | Referenced[Mask] = GivenCentre()
    either: Referenced[Sample] | Referenced[Mask] | None = None


class MaskParams(BaseModel):
    mask: MaskFile | None = None
    sample: Referenced[Sample] | None = None
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


def _unread(ref: Ref) -> Any:
    raise AssertionError(f'{ref} is read; a file is never read')


def test_an_unmarked_place_is_written_by_the_request() -> None:
    assert root_type(MaskParams, 'mask') == (MaskFile, True, ())
    assert split(MaskFile) == (MaskFile, (), ())
    assert root_type(MeasuredParams, 'measured') == (Measured, False, ())
    assert split(Measured) == (Measured, (), ())
    assert unwritten(Measured) is None


def test_a_place_marked_referenced_is_filled_by_a_reference_only() -> None:
    assert root_type(Params, 'mask') == (Mask, False, (REFERENCED,))
    assert split(Mask, REFERENCED) == (None, (), (Mask,))
    assert unwritten(Mask) == 'mask'


def test_the_marker_of_a_place_typed_x_or_none_is_in_its_metadata() -> None:
    assert root_type(MaskParams, 'sample') == (Sample, True, (REFERENCED,))


def test_referenced_x_or_none_keeps_its_none_in_the_type_of_the_data() -> None:
    class MaybeMask(BaseModel):
        mask: Referenced[Mask | None] = None

    assert root_type(MaybeMask, 'mask') == (Mask | None, False, (REFERENCED,))


def test_a_union_with_referenced_members_splits_into_both_parts() -> None:
    tp, none, metadata = root_type(Params, 'samples')
    assert not none
    assert split(tp, *metadata) == (Sample, (), (Sample,))
    tp, _, metadata = root_type(Params, 'either')
    assert split(tp, *metadata) == (None, (), (Sample, Mask))


def test_the_alternatives_written_keep_their_discriminator() -> None:
    tp, _, metadata = root_type(Params, 'found')
    inline, inline_metadata, referenced = split(tp, *metadata)
    assert referenced == (Mask,)
    assert alternatives(inline, *inline_metadata) == (
        'kind',
        {'given': GivenCentre, 'find': FindCentre},
    )


def test_a_group_of_scalars_is_written_inline() -> None:
    assert root_type(Params, 'reduction') == (Reduction, True, ())
    assert split(Reduction) == (Reduction, (), ())
    assert unwritten(Reduction) is None


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
    assert split(tp, *metadata) == (tp, metadata, ())


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


def test_the_root_type_of_a_list_parameter_is_that_of_an_element() -> None:
    assert root_type(MaskParams, 'runs') == (RunFile, False, ())
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


def test_each_reference_to_an_output_has_the_type_referenced_at_its_place() -> None:
    params = {'mask': FILE, 'sample': OUTPUT, 'runs': [FILE, FILE]}
    assert ref_types(MaskParams, params) == [(('sample',), OUTPUT, Sample)]
    assert ref_types(Params, {'samples': [OUTPUT], 'found': OUTPUT}) == [
        (('samples', 0), OUTPUT, Sample),
        (('found',), OUTPUT, Mask),
    ]


def test_a_reference_at_several_referenced_groups_has_their_union() -> None:
    assert ref_types(Params, {'either': OUTPUT}) == [
        (('either',), OUTPUT, Sample | Mask)
    ]


def test_a_binding_gives_a_file_model_as_data() -> None:
    given = validated(MaskParams, 'mask', {'file': 'pid-1'})
    assert given == MaskFile(file='pid-1')
