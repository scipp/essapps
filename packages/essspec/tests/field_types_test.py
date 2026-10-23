# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from pathlib import Path, PosixPath
from typing import Annotated, Any, Literal, NewType

import pytest
import scipp as sc
from pydantic import BaseModel

from ess.spec.field_types import (
    element,
    fits,
    is_file,
    is_single,
    newtype_in,
    optional,
    plain,
    type_name,
    value_type,
)


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
    run: Path


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


def test_a_file_is_a_path_or_a_subclass_of_it() -> None:
    assert is_file(Path)
    assert is_file(PosixPath)
    assert not is_file(str)
    assert not is_file(list[Path])


def test_newtype_in_finds_a_newtype_at_any_depth() -> None:
    sample_file = NewType('SampleFile', Path)
    assert newtype_in(sample_file) is sample_file
    assert newtype_in(list[sample_file | None]) is sample_file
    assert newtype_in(Annotated[dict[str, sample_file], 'a validator']) is sample_file
    assert newtype_in(list[Parts | None]) is None


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
        (BeamCentre, BeamCentre | FindCentre),  # data fit one alternative
        (BeamCentre, BeamCentre | FindCentre | None),
        (SampleParts, BeamCentre | Parts),  # a subclass of one alternative
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
        (sc.DataArray, Parts | sc.Variable),
        (Parts, SampleParts | CanParts),
        (BeamCentre | FindCentre, BeamCentre),
    ],
)
def test_does_not_fit(data: Any, field: Any) -> None:
    assert not fits(data, field)
