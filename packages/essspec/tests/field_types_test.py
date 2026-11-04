# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from dataclasses import dataclass
from pathlib import Path, PosixPath
from typing import Annotated, Any, Literal, NewType, get_args

import pytest
import scipp as sc
from pydantic import BaseModel, Field, Strict

from ess.spec import File, Referenced
from ess.spec.field_types import (
    alternatives,
    arguments,
    element,
    field_metadata,
    fields_of,
    file_marker,
    file_of,
    fits,
    has_array,
    holds_marker,
    holds_ref,
    is_alias,
    is_array,
    is_list,
    is_single,
    optional,
    plain,
    reachable,
    split,
    type_name,
    unwrapped,
    value_type,
)
from ess.spec.parameters import REFERENCED

RunFile = Annotated[Path, File()]


class Parts(BaseModel, arbitrary_types_allowed=True):
    numerator: sc.DataArray
    denominator: sc.DataArray


class SampleParts(Parts):
    pass


class CanParts(Parts):
    pass


class BeamCentre(BaseModel):
    kind: Literal['given'] = 'given'
    x: float = 0.0


class FindCentre(BaseModel):
    kind: Literal['find'] = 'find'
    run: RunFile


def test_optional_removes_none_and_says_whether_it_was_there() -> None:
    assert optional(Parts | None) == (Parts, True)
    assert optional(Parts) == (Parts, False)
    assert optional(int | str | None) == (int | str, True)
    assert optional(int | str) == (int | str, False)


def test_annotated_metadata_is_removed_at_any_depth() -> None:
    known = Annotated[Parts, 'a validator']
    assert plain(list[known | None]) == list[Parts | None]
    assert plain(dict[str, known]) == dict[str, Parts]
    assert optional(known | None) == (Parts, True)
    assert value_type(list[known]) is Parts


def test_element_of_a_list_or_a_dict() -> None:
    assert element(list[Parts]) is Parts
    assert element(dict[str, sc.DataArray | None]) == sc.DataArray | None
    assert element(Parts) is None
    assert element(list[int] | None) is None


@pytest.mark.parametrize(
    'annotation',
    [
        Parts,
        Parts | None,
        list[Parts],
        list[Parts | None],
        list[Parts] | None,
        dict[str, Parts],
    ],
)
def test_value_type_is_the_type_of_one_value_of_a_field(annotation: Any) -> None:
    assert value_type(annotation) is Parts


def test_value_type_removes_one_list() -> None:
    assert value_type(list[list[Parts]]) == list[Parts]


@pytest.mark.parametrize('tp', [Path, sc.DataArray, Parts, SampleParts, float])
def test_a_data_value_is_one_value(tp: Any) -> None:
    assert is_single(tp)


@pytest.mark.parametrize(
    'tp',
    [
        list[Path],
        dict[str, sc.DataArray],
        list,
        dict,
        sc.DataArray | sc.Variable,
        sc.DataArray | None,
        type(None),
    ],
)
def test_lists_dicts_and_unions_are_not_data_values(tp: Any) -> None:
    assert not is_single(tp)


@pytest.mark.parametrize('tp', [sc.Variable, sc.DataArray, sc.DataArray | None])
def test_a_type_with_dims_is_an_array(tp: Any) -> None:
    assert is_array(tp)
    assert has_array(tp)


@pytest.mark.parametrize('tp', [float, Parts, list[sc.DataArray], BeamCentre])
def test_a_group_a_list_and_a_scalar_are_not_arrays(tp: Any) -> None:
    assert not is_array(tp)


def test_a_group_has_an_array_if_a_field_is_one() -> None:
    assert has_array(Parts)
    assert has_array(Parts | None)
    assert not has_array(BeamCentre)
    assert not has_array(list[sc.DataArray])


def test_a_file_field_is_a_path_with_its_marker() -> None:
    assert file_marker(Path, File(kind='mask')) == File(kind='mask')
    assert file_marker(Path, 'a validator', File()) == File()
    tp, _, metadata = unwrapped(MaskFile | None)
    assert file_marker(tp, *metadata) == File(kind='mask')
    assert file_marker(Path) is None
    assert file_marker(Parts, File()) is None
    assert file_marker(list[Path], File()) is None


def test_a_file_parameter_is_a_file_field_or_none_or_a_list_of_one() -> None:
    class P(BaseModel):
        run: RunFile
        mask: MaskFile | None = None
        runs: list[RunFile] = []
        masks: list[Annotated[Path, File(kind='mask')]] = Field([], min_length=0)
        parts: Parts | None = None

    fields = P.model_fields
    assert file_of(fields['run']) == File()
    assert file_of(fields['mask']) == File(kind='mask')
    assert file_of(fields['runs']) == File()
    assert file_of(fields['masks']) == File(kind='mask')
    assert file_of(fields['parts']) is None


def test_a_newtype_and_a_type_alias_are_aliases() -> None:
    type Folder = Path
    assert is_alias(NewType('SampleFile', Path))
    assert is_alias(Folder)
    assert not is_alias(Path)
    assert not is_alias(list[Path])


def test_arguments_are_the_type_arguments_at_any_depth() -> None:
    sample_file = NewType('SampleFile', Path)
    assert sample_file in list(arguments(list[sample_file | None]))
    assert sc.DataArray not in list(arguments(Parts | None))  # not its fields


def test_fields_of_a_model_a_dataclass_or_a_typeddict() -> None:
    @dataclass
    class Window:
        start: float
        stop: float

    assert fields_of(Parts) == {'numerator': sc.DataArray, 'denominator': sc.DataArray}
    assert fields_of(Window) == {'start': float, 'stop': float}
    assert fields_of(Path) == {}
    assert fields_of(list[Parts]) == {}


class Node(BaseModel):
    file: RunFile
    children: list['Node'] = []


def test_reachable_looks_through_arguments_and_fields_and_ends_on_recursion() -> None:
    found = list(reachable(Annotated[list[Node] | None, 'a validator']))
    assert Node in found
    assert Path in found
    assert 'a validator' not in found  # not the metadata
    assert sc.DataArray in list(reachable(dict[str, SampleParts]))


class Tree(BaseModel):
    children: list['Tree'] = []


class Holder(BaseModel):
    tree: Tree
    leaves: list[Referenced[BeamCentre]] = []


def test_holds_ref_looks_into_the_fields_of_groups_and_ends_on_recursion() -> None:
    assert holds_ref(Holder | None)
    assert holds_ref(Annotated[list[Holder], 'a validator'])
    assert holds_ref(Referenced[Tree])
    assert not holds_ref(Tree)
    assert not holds_ref(list[BeamCentre])


class Measurement(BaseModel):
    run: RunFile


def test_holds_marker_finds_a_file_field_or_referenced_at_any_depth() -> None:
    assert holds_marker(Holder)
    assert holds_marker(list[Measurement] | None)
    assert holds_marker(RunFile)
    assert not holds_ref(Measurement)
    assert not holds_marker(Tree)
    assert not holds_marker(Path)


def test_type_name_is_the_name_of_the_class() -> None:
    assert type_name(sc.DataArray) == 'DataArray'
    assert type_name(BeamCentre | FindCentre) == 'BeamCentre | FindCentre'


@pytest.mark.parametrize(
    ('data', 'field'),
    [
        (sc.DataArray, sc.DataArray),
        (Parts, Parts),
        (SampleParts, Parts),  # a subclass fits its base
        (PosixPath, Path),
        (sc.DataArray | None, sc.DataArray),
        (sc.DataArray, sc.DataArray | None),
        (list[int], list[int]),
        (SampleParts, SampleParts | CanParts),  # a member of a union
        (CanParts, SampleParts | CanParts | None),
    ],
)
def test_fits(data: Any, field: Any) -> None:
    assert fits(data, field)


@pytest.mark.parametrize(
    ('data', 'field'),
    [
        (Parts, SampleParts),  # a base does not fit its subclass
        (SampleParts, CanParts),  # two subclasses of one base
        (sc.Variable, sc.DataArray),
        (Path, PosixPath),
        (list[int], list[float]),
        (list[int], list),
        (list[SampleParts], list[Parts]),  # generic aliases fit only themselves
        (sc.DataArray, list[sc.DataArray]),
        (BeamCentre | FindCentre, BeamCentre),
        (Parts, SampleParts | CanParts),
    ],
)
def test_does_not_fit(data: Any, field: Any) -> None:
    assert not fits(data, field)


MaskFile = Annotated[Path, File(kind='mask')]


class Mask(BaseModel, arbitrary_types_allowed=True):
    mask: sc.DataArray


class Sample(BaseModel):
    thickness: float = Field(1.0, gt=0)


class Reduction(BaseModel):
    bins: int = Field(50, ge=1)


class Measured(BaseModel):
    run: RunFile
    thickness: float = Field(1.0, gt=0)


class MeasuredParams(BaseModel):
    measured: Measured


Centre = Annotated[BeamCentre | FindCentre, Field(discriminator='kind')]
CENTRES = ('kind', {'given': BeamCentre, 'find': FindCentre})


class Params(BaseModel):
    mask: Referenced[Mask]
    reduction: Reduction | None = None
    beam_centre: Centre
    centres: list[Centre]
    samples: list[Sample | Referenced[Sample]] = Field(min_length=2)
    found: Centre | Referenced[Mask] = BeamCentre()
    either: Referenced[Sample] | Referenced[Mask] | None = None


class MaskParams(BaseModel):
    mask: MaskFile | None = None
    sample: Referenced[Sample] | None = None
    runs: list[RunFile] = []


def _unwrapped(model: type[BaseModel], name: str) -> tuple[Any, bool, tuple[Any, ...]]:
    """The type of a parameter, as a form or the request type reads it."""
    info = model.model_fields[name]
    return unwrapped(info.annotation, field_metadata(info))


def test_an_unmarked_field_is_written_by_the_request() -> None:
    assert _unwrapped(MaskParams, 'mask') == (Path, True, (File(kind='mask'),))
    assert split(Path, File(kind='mask')) == (Path, (File(kind='mask'),), ())
    assert _unwrapped(MeasuredParams, 'measured') == (Measured, False, ())
    assert split(Measured) == (Measured, (), ())


def test_a_field_marked_referenced_is_filled_by_a_reference_only() -> None:
    assert _unwrapped(Params, 'mask') == (Mask, False, (REFERENCED,))
    assert split(Mask, REFERENCED) == (None, (), (Mask,))


def test_the_marker_of_a_field_typed_x_or_none_is_in_its_metadata() -> None:
    assert _unwrapped(MaskParams, 'sample') == (Sample, True, (REFERENCED,))


def test_referenced_x_or_none_keeps_its_none_in_the_type_of_the_data() -> None:
    class MaybeMask(BaseModel):
        mask: Referenced[Mask | None] = None

    assert _unwrapped(MaybeMask, 'mask') == (Mask | None, False, (REFERENCED,))


def test_a_union_with_referenced_members_splits_into_both_parts() -> None:
    tp, none, metadata = unwrapped(
        get_args(Params.model_fields['samples'].annotation)[0]
    )
    assert not none
    assert split(tp, *metadata) == (Sample, (), (Sample,))
    tp, _, metadata = _unwrapped(Params, 'either')
    assert split(tp, *metadata) == (None, (), (Sample, Mask))


def test_the_alternatives_written_keep_their_discriminator() -> None:
    tp, _, metadata = _unwrapped(Params, 'found')
    inline, inline_metadata, referenced = split(tp, *metadata)
    assert referenced == (Mask,)
    assert alternatives(inline, *inline_metadata) == CENTRES


def test_a_group_of_scalars_is_written_inline() -> None:
    assert _unwrapped(Params, 'reduction') == (Reduction, True, ())
    assert split(Reduction) == (Reduction, (), ())


def test_the_metadata_of_the_field_comes_before_that_in_its_type() -> None:
    class Strictly(BaseModel):
        reduction: Annotated[Reduction, 'inner'] | None = Field(None, strict=True)

    assert _unwrapped(Strictly, 'reduction') == (Reduction, True, (Strict(), 'inner'))


def test_a_union_is_written_inline_with_the_tag_of_an_alternative() -> None:
    tp, none, metadata = _unwrapped(Params, 'beam_centre')
    assert not none
    assert alternatives(tp, *metadata) == CENTRES
    assert split(tp, *metadata) == (tp, metadata, ())


def test_a_union_typed_x_or_none_takes_none() -> None:
    class MaybeCentre(BaseModel):
        beam_centre: Centre | None = None

    tp, none, metadata = _unwrapped(MaybeCentre, 'beam_centre')
    assert none
    assert alternatives(tp, *metadata) == CENTRES


def test_the_discriminator_of_a_field_is_in_its_metadata() -> None:
    class ByField(BaseModel):
        beam_centre: BeamCentre | FindCentre = Field(discriminator='kind')

    tp, _, metadata = _unwrapped(ByField, 'beam_centre')
    assert alternatives(tp, *metadata) == CENTRES


def test_unwrapped_keeps_the_metadata_of_the_elements_of_a_list() -> None:
    assert _unwrapped(MaskParams, 'runs') == (list[RunFile], False, ())
    assert is_list(MaskParams, 'runs')
    tp, none, metadata = unwrapped(
        get_args(Params.model_fields['centres'].annotation)[0]
    )
    assert not none
    assert alternatives(tp, *metadata) == CENTRES


def test_a_union_without_a_discriminator_is_refused() -> None:
    class Undiscriminated(BaseModel):
        beam_centre: BeamCentre | FindCentre

    tp, _, metadata = _unwrapped(Undiscriminated, 'beam_centre')
    with pytest.raises(ValueError, match='discriminator'):
        alternatives(tp, *metadata)
