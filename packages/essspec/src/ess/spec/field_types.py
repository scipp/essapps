# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Type introspection of the fields of a spec's models.

A spec's models carry no annotations for the framework: what the framework
needs, it reads from the field types. These functions take a field's type
apart::

    BeamCentre | None       optional(...)   -> (BeamCentre, True)
    list[SampleRun]         element(...)    -> SampleRun
    dict[str, IofQ]         value_type(...) -> IofQ

``dict[str, X]`` occurs only in outputs. Each function sees through
``Annotated`` metadata, such as a validator: ``Annotated[X, ...]`` is ``X``
(see :func:`plain`).

:func:`fits` says whether data of one type can be the value of a field of
another, so that an output of one spec can fill a parameter of another.
:func:`model_of` makes a model of one field, which validates a value, or a
row of a list parameter, by that field alone, as
:func:`ess.spec.places.validated` does. :func:`inline` validates a value
of a model of one field from its JSON form in the request phase.
The rules these serve are stated in :mod:`ess.spec._workflow_spec`.
"""

from __future__ import annotations

import dataclasses
import functools
import operator
from collections.abc import Iterable, Iterator
from datetime import datetime
from enum import Enum
from types import NoneType, UnionType
from typing import (
    Annotated,
    Any,
    Literal,
    NewType,
    TypeAliasType,
    Union,
    get_args,
    get_origin,
    get_type_hints,
    is_typeddict,
)

from pydantic import (
    AwareDatetime,
    BaseModel,
    Field,
    NaiveDatetime,
    RootModel,
    ValidationError,
    create_model,
)
from pydantic_core import PydanticCustomError, from_json, to_json

from .parameters import FileModel
from .refs import Loc, ParamsError

TIMES = (datetime, AwareDatetime, NaiveDatetime)
"""The types of a time, which a request writes as a string."""


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


def taken_apart(annotation: Any) -> tuple[Any, bool, bool | None]:
    """
    The type of one value of ``annotation``, ``X`` of ``X``, ``X | None``,
    ``list[X]``, ``list[X] | None``, or ``list[X | None]``; whether
    ``annotation`` is typed ``X | None``; and for a list whether its element
    is, else ``None``.
    """
    tp, is_optional = optional(annotation)
    if get_origin(tp) is not list:
        return tp, is_optional, None
    tp, item_optional = optional(get_args(tp)[0])
    return tp, is_optional, item_optional


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


def reachable(tp: Any, seen: frozenset[type] = frozenset()) -> Iterator[Any]:
    """
    ``tp`` and each type it is made of, at any depth: its type arguments, and
    the field types of each model, dataclass, or ``TypedDict`` among them.
    ``seen`` holds the classes looked into already, so that a recursive class
    ends.
    """
    if get_origin(tp) is Annotated:
        yield from reachable(get_args(tp)[0], seen)  # not the metadata
        return
    yield tp
    if fields := fields_of(tp):
        if tp not in seen:
            for field in fields.values():
                yield from reachable(field, seen | {tp})
        return
    for arg in get_args(tp):
        yield from reachable(arg, seen)


def arguments(tp: Any) -> Iterator[Any]:
    """``tp`` and its type arguments, at any depth, not the fields of a class."""
    yield tp
    for arg in get_args(tp):
        yield from arguments(arg)


def is_alias(tp: Any) -> bool:
    """Whether ``tp`` is a ``NewType`` or a type alias, ``type X = ...``."""
    return isinstance(tp, NewType | TypeAliasType)


def fields_of(tp: Any) -> dict[str, Any]:
    """
    The types of the fields of ``tp`` by name, a model, a dataclass, or a
    ``TypedDict`` class; none of any other type.
    """
    if not isinstance(tp, type):
        return {}
    if _is_model(tp):
        return {n: f.annotation for n, f in tp.model_fields.items()}
    if dataclasses.is_dataclass(tp) or is_typeddict(tp):
        return get_type_hints(tp)
    return {}


def is_single(tp: Any) -> bool:
    """
    Whether ``tp`` is the type of a single value: any type but a list, a
    dict, a union, or ``None``.
    """
    return tp not in (NoneType, list, dict) and get_origin(tp) not in (
        list,
        dict,
        Union,
        UnionType,
    )


def is_file_model(tp: Any) -> bool:
    """Whether ``tp`` is a file model, a subclass of :class:`FileModel`."""
    return isinstance(tp, type) and issubclass(tp, FileModel)


def is_scalar(tp: Any) -> bool:
    """
    Whether ``tp`` is a scalar, by the type alone: a type whose JSON form is
    a number, a string, or a bool; a ``Literal`` of such values, not of enum
    members; or an enum whose values are such.
    """
    if tp in (int, float, str, bool, *TIMES):
        return True
    if get_origin(tp) is Literal:
        values = get_args(tp)
    elif isinstance(tp, type) and issubclass(tp, Enum):
        values = tuple(member.value for member in tp)
    else:
        return False
    return all(type(v) in (str, int, float, bool) for v in values)


def file_model(annotation: Any) -> Any:
    """
    The file model of a field typed ``annotation``, ``X``, ``X | None``,
    or ``list[X]`` of one, or ``None``.
    """
    tp = value_type(annotation)
    return tp if is_file_model(tp) else None


def is_group(tp: Any) -> bool:
    """
    Whether ``tp`` is a group: a pydantic model, but not a ``RootModel``,
    whose JSON form is its root value, not an object of its fields.
    """
    return _is_model(tp) and not issubclass(tp, RootModel)


def _is_model(tp: Any) -> bool:
    return isinstance(tp, type) and issubclass(tp, BaseModel)


def is_array(tp: Any) -> bool:
    """
    Whether ``tp``, ``X`` or ``X | None``, is an array: a type with named
    dimensions, ``dims``, such as a scipp variable or data array.
    """
    tp = optional(tp)[0]
    return isinstance(tp, type) and hasattr(tp, 'dims')


def has_array(tp: Any) -> bool:
    """
    Whether ``tp``, ``X`` or ``X | None``, is an array, or a group with an
    array field (see :func:`is_array`): what a reader slices by dimension,
    or plots.
    """
    tp = optional(tp)[0]
    return is_array(tp) or (
        is_group(tp) and any(is_array(f.annotation) for f in tp.model_fields.values())
    )


def type_name(tp: Any) -> str:
    """
    The name of a type, as a serialized spec shows it, without ``Annotated``
    metadata; of a union, the names of its members, ``BeamCentre |
    FindCentre``; of a generic alias, with its arguments, ``tuple[float,
    float]`` or ``Literal['a', 'b']``.
    """
    tp = plain(tp)
    origin, args = get_origin(tp), get_args(tp)
    if origin in (Union, UnionType):
        return ' | '.join(map(type_name, args))
    if tp is NoneType:
        return 'None'
    if tp is Ellipsis:
        return '...'
    if origin is Literal:
        return f'Literal[{", ".join(map(repr, args))}]'
    if origin is not None:
        return f'{type_name(origin)}[{", ".join(map(type_name, args))}]'
    return getattr(tp, '__name__', None) or repr(tp)


def fits(data: Any, field: Any) -> bool:
    """
    Whether data of type ``data`` can be the value of a field of type ``field``.

    ``| None`` is removed on both sides. Then the data fit when ``data`` is
    ``field`` or a subclass of it, or for a union ``field``, such as the
    groups of ``Referenced[A] | Referenced[B]``, one of its members. A
    generic alias, such as ``list[int]``, fits only itself.
    """
    data, _ = optional(data)
    field, _ = optional(field)
    if get_origin(field) in (Union, UnionType):
        return any(fits(data, member) for member in get_args(field))
    if get_origin(data) is not None or get_origin(field) is not None:
        return data == field
    return data is field or (
        isinstance(data, type) and isinstance(field, type) and issubclass(data, field)
    )


@functools.cache
def model_of(model: type[BaseModel], name: str, whole: bool = False) -> type[BaseModel]:
    """
    A model of one value of the field ``name`` of ``model`` alone, with its
    config and without the decorators of ``model``: it validates one value by
    its own field. The params model has no decorators but model validators,
    which the framework never calls; a check of one value belongs to the
    value's type.

    For a list field, the model validates one row: by its element type alone,
    with that type's metadata, without the field's length constraints, which
    hold for the whole list. A reference stands for one row. With ``whole``,
    the model validates the whole field, a list too, as for a field of a
    group, whose elements are not places.
    """
    info = model.model_fields[name]
    field = (
        (get_args(info.annotation)[0], Field())
        if is_list(model, name) and not whole
        else (info.annotation, info)
    )
    return create_model(model.__name__, __config__=model.model_config, **{name: field})


def is_list(model: type[BaseModel], name: str) -> bool:
    """Whether the field ``name`` of ``model`` is a list, given as a list of rows."""
    return get_origin(model.model_fields[name].annotation) is list


def json_form(one: type[BaseModel], name: str, value: Any) -> Any:
    """
    The JSON form of ``value``, a valid value of the field ``name`` of
    ``one``, a model of that field alone, as its declared type dumps it to
    JSON and JSON reads back: an instance of a subclass of a model type
    without the subclass's fields, and a number that is not finite as
    ``None``.
    """
    made = one.model_construct(**{name: value})
    return from_json(made.model_dump_json(by_alias=True))[name]


def inline(
    one: type[BaseModel],
    name: str,
    value: Any,
    context: dict[str, Any] | None = None,
) -> tuple[Any, Any]:
    """
    ``value`` validated, and its JSON form, as the record stores it. ``value``
    is a value of the field ``name`` of ``one``, a model of that field alone,
    such as the request type of a parameter.

    ``value`` is plain data, such as :func:`ess.spec.request_types.held` makes of
    Python objects. It is validated from its JSON form, with ``context``. A
    field that a model lacks is refused at any depth, since validating would
    drop it silently. The JSON form of the validated value must read back as
    a value with the same JSON form, validated without ``context``. Raises:

    - ``PydanticSerializationError`` if it has no JSON form;
    - ``ValidationError`` if it is not valid, or if its JSON form is not,
      such as of a number that is not finite, which JSON holds as ``null``;
    - :class:`~ess.spec.refs.ParamsError` at the position inside the value,
      without ``name``, whose JSON form reads back as another value, such as
      a field of a group whose validator changes a value it has validated
      already.
    """
    given = to_json({name: value}, inf_nan_mode='constants')
    made = getattr(
        one.model_validate_json(given, extra='forbid', context=context), name
    )
    stored = json_form(one, name, made)
    try:
        again = getattr(one.model_validate_json(to_json({name: stored})), name)
    except ValidationError as error:
        (first, *_) = error.errors()
        raise ValidationError.from_exception_data(
            one.__name__,
            [{'type': _NO_JSON_FORM_ERROR, 'loc': first['loc'], 'input': None}],
        ) from None
    if (read := json_form(one, name, again)) != stored:
        raise ParamsError([(_differs((), stored, read), _NO_JSON_FORM)])
    return made, stored


_NO_JSON_FORM = 'has no JSON form that reads back as the value'
_NO_JSON_FORM_ERROR = PydanticCustomError('no_json_form', _NO_JSON_FORM)


def _differs(loc: Loc, a: Any, b: Any) -> Loc:
    """
    The first position at which ``a`` and ``b``, two JSON forms at ``loc``,
    differ: a field of an object or an element of a list, at any depth.
    """
    if isinstance(a, dict) and isinstance(b, dict) and a.keys() == b.keys():
        pairs: Iterable[tuple[str | int, Any, Any]] = ((k, a[k], b[k]) for k in a)
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        pairs = ((i, x, y) for i, (x, y) in enumerate(zip(a, b, strict=True)))
    else:
        return loc
    for at, x, y in pairs:
        if x != y:
            return _differs((*loc, at), x, y)
    return loc
