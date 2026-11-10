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
    None, in any of these                left out
    a number, text, or a bool alone      a DataGroup with the key 'value'

A value of any other type is refused with ``TypeError`` when it is written.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, get_args, get_origin

import scipp as sc
from pydantic import BaseModel

from ess.spec import field_types

_ALONE = 'value'
_ARRAYS = (sc.Variable, sc.DataArray, sc.Dataset)


def _plain(value: Any) -> Any:
    """``value`` as the file holds it, but for a number or text alone."""
    if isinstance(value, BaseModel):
        value = {name: getattr(value, name) for name in type(value).model_fields}
    if isinstance(value, list | tuple):
        value = {str(i): v for i, v in enumerate(value)}
    if isinstance(value, dict):
        return sc.DataGroup({k: _plain(v) for k, v in value.items() if v is not None})
    if not isinstance(value, (*_ARRAYS, sc.DataGroup, bool, int, float, str)):
        # scipp would log that it skips the value, and write the rest
        raise TypeError(f'a {type(value).__name__} has no form in a scipp HDF5 file')
    return value


def _typed(tp: Any, data: Any) -> Any:
    """``data`` of a file as the value of type ``tp`` that was written."""
    tp, _ = field_types.optional(tp)
    if isinstance(tp, type) and issubclass(tp, BaseModel):
        fields = tp.model_fields
        return tp(
            **{
                name: _typed(info.annotation, data[name]) if name in data else None
                for name, info in fields.items()
            }
        )
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
    if plain_type in (float, int, str, bool):
        return data[_ALONE]
    return _typed(tp, data)
