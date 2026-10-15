# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The field types of a spec, taken apart.

A spec's models carry no annotations for the framework: what the framework
needs, it reads from the field types. A field's type is built from a few
pieces, which these functions take apart::

    CanFile | None          optional(...)   -> (CanFile, True)
    list[SampleFile]        element(...)    -> SampleFile
    list[CanFile | None]    value_type(...) -> CanFile
    IofQ                    supertypes(...) -> [IofQ, sc.DataArray]

with ``IofQ = NewType('IofQ', sc.DataArray)``. Each function sees through
``Annotated`` metadata, such as a validator: ``Annotated[X, ...]`` is ``X``
(see :func:`plain`). :func:`fits` says whether data
of one type can be the value of a field of another, so that an output of one
spec can feed an input of another.
"""

from __future__ import annotations

import functools
import operator
from pathlib import PurePath
from types import NoneType, UnionType
from typing import Annotated, Any, NewType, Union, get_args, get_origin


def plain(tp: Any) -> Any:
    """
    ``tp`` without ``Annotated`` metadata, in a list, a dict, or a union too:
    ``list[Annotated[X, ...] | None]`` becomes ``list[X | None]``.
    """
    origin, args = get_origin(tp), get_args(tp)
    if origin is Annotated:
        return plain(args[0])
    if origin not in (Union, UnionType, list, dict):
        return tp
    plain_args = tuple(plain(arg) for arg in args)
    if plain_args == args:
        return tp  # unchanged, so that a sciline key stays the same object
    if origin in (list, dict):
        return origin[plain_args]
    return functools.reduce(operator.or_, plain_args)


def optional(tp: Any) -> tuple[Any, bool]:
    """``tp`` without ``| None`` and ``Annotated`` metadata, and whether it had it."""
    tp = plain(tp)
    if get_origin(tp) in (Union, UnionType):
        args = [arg for arg in get_args(tp) if arg is not NoneType]
        if len(args) < len(get_args(tp)):
            inner = args[0] if len(args) == 1 else Union[tuple(args)]  # noqa: UP007
            return inner, True
    return tp, False


def element(tp: Any) -> Any | None:
    """
    ``X`` if ``tp`` is ``list[X]`` or ``dict[str, X]``, else ``None``; without
    ``Annotated`` metadata.
    """
    tp = plain(tp)
    origin = get_origin(tp)
    if origin is list:
        return get_args(tp)[0]
    if origin is dict:
        return get_args(tp)[1]
    return None


def value_type(annotation: Any) -> Any:
    """
    The type of one value of a field: ``X`` of ``X``, ``X | None``,
    ``list[X]``, ``list[X | None]``, or ``dict[str, X]``, with ``| None``
    removed before and after the list or dict.
    """
    tp, _ = optional(annotation)
    if (item := element(tp)) is not None:
        tp, _ = optional(item)
    return tp


def supertypes(tp: Any) -> list[Any]:
    """``tp`` and the types it is a ``NewType`` of, removed one by one."""
    chain = [tp]
    while isinstance(chain[-1], NewType):
        chain.append(chain[-1].__supertype__)
    return chain


def base(tp: Any) -> Any:
    """``tp`` with its ``NewType``s removed."""
    return supertypes(tp)[-1]


def is_data_value(tp: Any) -> bool:
    """
    Whether ``tp`` is the type of one data value: any type but a list, a
    dict, a union, or ``None``, once its ``NewType``s are removed.
    """
    tp = base(tp)
    return tp not in (NoneType, list, dict) and get_origin(tp) not in (
        list,
        dict,
        Union,
        UnionType,
    )


def is_file(tp: Any) -> bool:
    """Whether ``tp`` is a file: ``Path``, or a ``NewType`` of it."""
    tp = base(tp)
    return isinstance(tp, type) and issubclass(tp, PurePath)


def type_name(tp: Any) -> str:
    """The name of a type, as a serialized spec shows it."""
    return getattr(tp, '__name__', None) or repr(tp)


def fits(data: Any, field: Any) -> bool:
    """
    Whether data of type ``data`` can be the value of a field of type ``field``.

    ``| None`` is removed on both sides. Then the data fit when ``data``, with
    its ``NewType``s removed one by one, reaches ``field`` or a subclass of it;
    or when ``field``, with its ``NewType``s removed, reaches ``data`` or a
    superclass of it. Generic aliases, such as ``list[int]``, fit only
    themselves. So with ``IofQ`` and ``Transmission`` ``NewType``s of
    ``sc.DataArray``, an ``IofQ`` fits ``sc.DataArray``, a plain
    ``sc.DataArray`` fits ``Transmission`` (a role narrows a general output),
    and an ``IofQ`` does not fit ``Transmission``.
    """
    data, _ = optional(data)
    field, _ = optional(field)
    if get_origin(data) is not None or get_origin(field) is not None:
        return data == field
    return any(_reaches(d, field) for d in supertypes(data)) or any(
        _reaches(data, f) for f in supertypes(field)
    )


def _reaches(sub: Any, sup: Any) -> bool:
    """Whether ``sub`` is ``sup`` or a subclass of it."""
    if sub is sup:
        return True
    return isinstance(sub, type) and isinstance(sup, type) and issubclass(sub, sup)
