# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""An output as a scipp HDF5 file."""

from pathlib import Path
from typing import Any

import pydantic
import pytest
import scipp as sc

from ess.apps import hdf5


class Group(pydantic.BaseModel, arbitrary_types_allowed=True):
    data: sc.DataArray
    note: str | None = None


class Floats(pydantic.BaseModel):
    a: float
    b: float


class Masks(pydantic.BaseModel, arbitrary_types_allowed=True):
    masks: dict[str, sc.Variable]


def _array(n: int = 3) -> sc.DataArray:
    x = sc.arange('x', float(n), unit='m')
    return sc.DataArray(x, coords={'x': x})


def equal(a: Any, b: Any) -> bool:
    """Equality that compares the fields of groups, which hold scipp arrays."""
    if isinstance(a, pydantic.BaseModel):
        return type(a) is type(b) and all(
            equal(getattr(a, name), getattr(b, name)) for name in type(a).model_fields
        )
    if isinstance(a, sc.Variable | sc.DataArray):
        return sc.identical(a, b)
    if isinstance(a, list):
        return len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(equal(a[k], b[k]) for k in a)
    return bool(a == b)


@pytest.mark.parametrize(
    ('tp', 'value'),
    [
        (sc.DataArray, _array()),
        (Group, Group(data=_array(), note=None)),
        (Group, Group(data=_array(), note='text')),
        (Floats, Floats(a=1.5, b=2.5)),
        (
            Masks,
            Masks(masks={'a': sc.arange('x', 3.0), 'b': sc.arange('x', 4.0)}),
        ),
        (list[Group], [Group(data=_array(2)), Group(data=_array(3), note='n')]),
        (float, 1.5),
        (str, 'text'),
        (Group | None, Group(data=_array(), note='text')),
    ],
    ids=[
        'array',
        'group with a field that is None',
        'group with a text field',
        'group of floats',
        'dict of variables in a group',
        'list of groups',
        'float alone',
        'text alone',
        'optional group holding a group',
    ],
)
def test_a_value_read_is_the_value_written(tmp_path: Path, tp: Any, value: Any) -> None:
    path = tmp_path / 'out.h5'

    hdf5.write(value, path)

    assert equal(hdf5.read(tp, path), value)


def test_a_group_is_a_data_group_of_its_fields_without_those_that_are_none(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'out.h5'

    hdf5.write(Group(data=_array(), note=None), path)

    loaded = sc.io.load_hdf5(path)
    assert isinstance(loaded, sc.DataGroup)
    assert list(loaded) == ['data']
    assert sc.identical(loaded['data'], _array())


class Opaque:
    """A class that scipp has no form for."""


class Holder(pydantic.BaseModel, arbitrary_types_allowed=True):
    inner: Opaque


@pytest.mark.parametrize('value', [Opaque(), Holder(inner=Opaque()), [Opaque()]])
def test_a_value_that_scipp_has_no_form_for_is_refused(
    value: object, tmp_path: Path
) -> None:
    with pytest.raises(TypeError, match='Opaque'):
        hdf5.write(value, tmp_path / 'out.h5')

    assert not (tmp_path / 'out.h5').exists()
