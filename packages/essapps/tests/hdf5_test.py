# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""An output as a scipp HDF5 file."""

from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Any

import numpy as np
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


class Mode(StrEnum):
    FAST = 'fast'
    SLOW = 'slow'


class Level(IntEnum):
    LOW = 1
    HIGH = 2


class Choice(pydantic.BaseModel):
    mode: Mode
    level: Level


class Scaled(pydantic.BaseModel):
    value: float

    @pydantic.field_validator('value')
    @classmethod
    def _scale(cls, value: float) -> float:
        return value * 1000


class Numbers(pydantic.BaseModel, arbitrary_types_allowed=True):
    x: float
    n: int


class Nested(pydantic.BaseModel, arbitrary_types_allowed=True):
    data: sc.DataGroup


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
        (Choice, Choice(mode=Mode.SLOW, level=Level.HIGH)),
        (Mode, Mode.FAST),
        (Level, Level.HIGH),
        (
            Nested,
            Nested(
                data=sc.DataGroup(
                    {'a': _array(), 'inner': sc.DataGroup({'b': _array(2)})}
                )
            ),
        ),
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
        'group with a str enum and an int enum',
        'str enum alone',
        'int enum alone',
        'group holding a data group holding a data group',
    ],
)
def test_a_value_read_is_the_value_written(tmp_path: Path, tp: Any, value: Any) -> None:
    path = tmp_path / 'out.h5'

    hdf5.write(value, path)

    assert equal(hdf5.read(tp, path), value)


def test_a_number_of_numpy_is_read_back_as_the_equal_number(tmp_path: Path) -> None:
    path = tmp_path / 'out.h5'

    hdf5.write(Numbers.model_construct(x=np.float64(1.5), n=np.int64(3)), path)

    assert hdf5.read(Numbers, path) == Numbers(x=1.5, n=3)


def test_a_group_is_read_back_with_the_value_written_not_validated_again(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'out.h5'
    written = Scaled(value=2.0)
    assert written.value == 2000.0

    hdf5.write(written, path)

    assert hdf5.read(Scaled, path).value == 2000.0


class FloatsAndMore(pydantic.BaseModel):
    """``Floats`` after the outputs of its spec gained a field."""

    a: float
    b: float
    c: float


def test_a_group_that_lacks_a_required_field_is_refused_naming_file_type_and_field(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'out.h5'
    hdf5.write(Floats(a=1.0, b=2.0), path)

    with pytest.raises(ValueError, match="field 'c' of FloatsAndMore") as error:
        hdf5.read(FloatsAndMore, path)
    assert str(path) in str(error.value)
    assert 'outputs changed since the value was written' in str(error.value)


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


@pytest.mark.parametrize(
    'value',
    [
        Opaque(),
        Holder(inner=Opaque()),
        [Opaque()],
        [1, None, 3],
        datetime(2026, 1, 1, tzinfo=UTC),
        (1, 2),
        sc.DataGroup({'a': [1, 2]}),
        sc.DataGroup({'a': Opaque()}),
    ],
    ids=[
        'object',
        'group holding an object',
        'list of objects',
        'list holding None',
        'datetime',
        'tuple',
        'data group holding a list',
        'data group holding an object',
    ],
)
def test_a_value_that_scipp_has_no_form_for_is_refused(
    value: object, tmp_path: Path
) -> None:
    with pytest.raises(TypeError):
        hdf5.write(value, tmp_path / 'out.h5')

    assert not (tmp_path / 'out.h5').exists()
