# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
An output as a scipp HDF5 file, which ``scipp.io.load_hdf5`` reads. It needs
scipp and h5py.

scipp writes arrays, ``DataGroup``, numbers, and text. So a value is written as
these, and read back by the type that the spec declares for the output::

    as the workflow returned it          in the file
    ---------------------------          -----------
    a scipp array                        the array
    a group (a pydantic model)           a DataGroup of its fields
    dict[str, X]                         a DataGroup of its items
    list[X]                              a DataGroup by index, '0', '1', ...
    None, as a field or an item          left out
    a member of an enum                  its value
    a number, text, or a bool alone      a DataGroup with the key 'value'

A value that has no such form is refused with ``TypeError`` when it is written:
a date, a tuple, ``None`` in a list, an object of any other class, and a
``DataGroup`` that holds one of these. A group is read back with the fields
that were written, not validated again, as an instance of the declared type.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, get_args, get_origin

import numpy as np
import scipp as sc
from pydantic import BaseModel

from ess.spec import field_types

_ALONE = 'value'
_ALONE_TYPES = (bool, int, float, str)
_ARRAYS = (sc.Variable, sc.DataArray, sc.Dataset)


def _plain(value: Any) -> Any:
    """``value`` as the file holds it, but for a number or text alone."""
    if isinstance(value, BaseModel):
        value = {name: getattr(value, name) for name in type(value).model_fields}
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, list):
        if any(v is None for v in value):
            raise TypeError('a list that holds None has no form in a scipp HDF5 file')
        value = {str(i): v for i, v in enumerate(value)}
    if isinstance(value, dict):
        return sc.DataGroup({k: _plain(v) for k, v in value.items() if v is not None})
    return _written(value)


def _written(value: Any) -> Any:
    """
    ``value``, if scipp writes it. scipp skips any other, and writes the rest. A
    ``DataGroup`` is read back as it is, so it holds only what scipp writes.
    """
    if isinstance(value, sc.DataGroup):
        return sc.DataGroup({k: _written(v) for k, v in value.items()})
    if not isinstance(value, _ARRAYS) and type(value) not in _ALONE_TYPES:
        raise TypeError(f'a {type(value).__name__} has no form in a scipp HDF5 file')
    return value


def _is(tp: Any, base: type) -> bool:
    return isinstance(tp, type) and issubclass(tp, base)


def _typed(tp: Any, data: Any) -> Any:
    """``data`` of a file as the value of type ``tp`` that was written."""
    tp, _ = field_types.optional(tp)
    if _is(tp, BaseModel):
        # not validated again: the workflow returned a valid value
        return tp.model_construct(
            **{
                name: _typed(info.annotation, data[name]) if name in data else None
                for name, info in tp.model_fields.items()
            }
        )
    if _is(tp, Enum):
        return tp(data)
    if get_origin(tp) is list:
        (element,) = get_args(tp)
        return [_typed(element, data[str(i)]) for i in range(len(data))]
    if get_origin(tp) is dict:
        return {k: _typed(get_args(tp)[1], v) for k, v in data.items()}
    return data


def write(value: Any, path: Path) -> None:
    """Write ``value`` to ``path``, as the module says."""
    plain = _plain(value)
    if not isinstance(plain, (*_ARRAYS, sc.DataGroup)):
        plain = sc.DataGroup({_ALONE: plain})
    sc.io.save_hdf5(plain, path)


def read(tp: Any, path: Path) -> Any:
    """The value of type ``tp`` that :func:`write` wrote to ``path``."""
    data = sc.io.load_hdf5(path)
    plain_type, _ = field_types.optional(tp)
    alone = plain_type in _ALONE_TYPES or _is(plain_type, Enum)
    return _typed(tp, data[_ALONE] if alone else data)
