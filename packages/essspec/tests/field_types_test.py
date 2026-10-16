# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
from pathlib import Path, PosixPath
from typing import Annotated, Any, NewType

import pytest
import scipp as sc
from pydantic import BaseModel

from ess.spec.field_types import (
    base,
    element,
    fits,
    is_file,
    is_single,
    optional,
    plain,
    supertypes,
    type_name,
    value_type,
)

SampleFile = NewType('SampleFile', Path)
IofQ = NewType('IofQ', sc.DataArray)
Transmission = NewType('Transmission', sc.DataArray)
ScaledIofQ = NewType('ScaledIofQ', IofQ)


class Parts(BaseModel, arbitrary_types_allowed=True):
    numerator: sc.DataArray
    denominator: sc.DataArray


SampleParts = NewType('SampleParts', Parts)
CanParts = NewType('CanParts', Parts)


def test_optional_removes_none_and_says_whether_it_was_there() -> None:
    assert optional(SampleFile | None) == (SampleFile, True)
    assert optional(SampleFile) == (SampleFile, False)
    assert optional(int | str | None) == (int | str, True)
    assert optional(int | str) == (int | str, False)


def test_annotated_metadata_is_removed_at_any_depth() -> None:
    known = Annotated[SampleFile, 'a validator']
    assert plain(list[known | None]) == list[SampleFile | None]
    assert plain(dict[str, known]) == dict[str, SampleFile]
    assert optional(known | None) == (SampleFile, True)
    assert value_type(list[known]) is SampleFile


def test_element_of_a_list_or_a_dict() -> None:
    assert element(list[SampleFile]) is SampleFile
    assert element(dict[str, IofQ | None]) == IofQ | None
    assert element(SampleFile) is None
    assert element(list[int] | None) is None


@pytest.mark.parametrize(
    'annotation',
    [
        SampleFile,
        SampleFile | None,
        list[SampleFile],
        list[SampleFile | None],
        list[SampleFile] | None,
        dict[str, SampleFile],
    ],
)
def test_value_type_is_the_type_of_one_value_of_a_field(annotation: Any) -> None:
    assert value_type(annotation) is SampleFile


def test_value_type_removes_one_list() -> None:
    assert value_type(list[list[SampleFile]]) == list[SampleFile]


def test_supertypes_remove_newtypes_one_by_one() -> None:
    assert supertypes(ScaledIofQ) == [ScaledIofQ, IofQ, sc.DataArray]
    assert supertypes(sc.DataArray) == [sc.DataArray]
    assert base(ScaledIofQ) is sc.DataArray


@pytest.mark.parametrize(
    'tp', [Path, SampleFile, sc.DataArray, IofQ, Parts, SampleParts, float]
)
def test_a_data_value_is_one_value(tp: Any) -> None:
    assert is_single(tp)


@pytest.mark.parametrize(
    'tp',
    [
        list[Path],
        dict[str, IofQ],
        list,
        dict,
        NewType('Runs', list),
        NewType('Runs', list[Path]),
        IofQ | Transmission,
        IofQ | None,
        type(None),
    ],
)
def test_lists_dicts_and_unions_are_not_data_values(tp: Any) -> None:
    assert not is_single(tp)


def test_a_file_is_a_path_or_a_newtype_of_it() -> None:
    assert is_file(Path)
    assert is_file(PosixPath)
    assert is_file(SampleFile)
    assert not is_file(str)
    assert not is_file(NewType('Name', str))
    assert not is_file(list[Path])


def test_type_name_is_the_name_of_the_newtype() -> None:
    assert type_name(IofQ) == 'IofQ'
    assert type_name(sc.DataArray) == 'DataArray'


@pytest.mark.parametrize(
    ('data', 'field'),
    [
        (IofQ, sc.DataArray),  # an export takes any data array
        (ScaledIofQ, IofQ),
        (sc.DataArray, Transmission),  # a role narrows a general output
        (IofQ, ScaledIofQ),
        (Parts, SampleParts),
        (Parts, CanParts),
        (IofQ, IofQ),
        (PosixPath, SampleFile),  # a subclass
        (IofQ | None, sc.DataArray),
        (IofQ, sc.DataArray | None),
        (list[int], list[int]),
    ],
)
def test_fits(data: Any, field: Any) -> None:
    assert fits(data, field)


@pytest.mark.parametrize(
    ('data', 'field'),
    [
        (IofQ, Transmission),  # two roles of one type
        (SampleParts, CanParts),
        (sc.Variable, sc.DataArray),
        (Path, PosixPath),
        (list[int], list[float]),
        (list[int], list),
        (list[IofQ], list[sc.DataArray]),  # generic aliases fit only themselves
        (IofQ, list[IofQ]),
    ],
)
def test_does_not_fit(data: Any, field: Any) -> None:
    assert not fits(data, field)
