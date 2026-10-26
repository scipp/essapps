# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The field types of a spec, taken apart.

A spec's models carry no annotations for the framework: what the framework
needs, it reads from the field types. A field's type is built from a few
pieces, which these functions take apart::

    BeamCentre | None       optional(...)   -> (BeamCentre, True)
    list[SampleRun]         element(...)    -> SampleRun
    list[BeamCentre | None] value_type(...) -> BeamCentre

Each function sees through ``Annotated`` metadata, such as a validator:
``Annotated[X, ...]`` is ``X`` (see :func:`plain`). :func:`fits` says whether
data of one type can be the value of a field of another, so that an output of
one spec can feed a parameter of another. :func:`model_of` makes a model of one
field, which validates a value, or a row of a list, by that field alone:
:func:`inline` validates a value given inline at one place, and
:func:`ess.spec.places.validated` a parameter's value whole, in the call
phase.
"""

from __future__ import annotations

import dataclasses
import functools
import operator
from collections.abc import Iterable, Iterator
from pathlib import PurePath
from types import NoneType, UnionType
from typing import (
    Annotated,
    Any,
    NewType,
    TypeAliasType,
    Union,
    get_args,
    get_origin,
    get_type_hints,
    is_typeddict,
)

from pydantic import BaseModel, Field, RootModel, ValidationError, create_model
from pydantic.errors import PydanticInvalidForJsonSchema, PydanticSchemaGenerationError
from pydantic_core import PydanticCustomError, from_json, to_json

from .refs import Loc, ParamsError, positions


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


def is_file(tp: Any) -> bool:
    """Whether ``tp`` is a file: ``Path``, or a subclass of it."""
    return isinstance(tp, type) and issubclass(tp, PurePath)


def is_group(tp: Any) -> bool:
    """
    Whether ``tp`` is a group: a pydantic model, but not a ``RootModel``,
    which is written as its root value, not as an object of its fields.
    """
    return _is_model(tp) and not issubclass(tp, RootModel)


def _is_model(tp: Any) -> bool:
    return isinstance(tp, type) and issubclass(tp, BaseModel)


def holds_file(tp: Any, seen: frozenset[type] = frozenset()) -> bool:
    """
    Whether a type, or a field of a model, a dataclass, or a ``TypedDict``
    in it, is a file; ``seen`` holds the classes looked into already, so that
    a recursive class ends.
    """
    if is_file(tp):
        return True
    if fields := fields_of(tp):
        if tp in seen:
            return False
        return any(holds_file(f, seen | {tp}) for f in fields.values())
    return any(holds_file(arg, seen) for arg in getattr(tp, '__args__', ()))


@functools.cache
def has_json_form(model: type[BaseModel], name: str) -> bool:
    """
    Whether the type of the field ``name`` of ``model``, with ``| None``
    removed and for a list its element type taken, has a JSON form: pydantic
    makes a JSON schema of it, with the config of ``model``, and it holds no
    file.
    """
    tp = value_type(model.model_fields[name].annotation)
    if holds_file(tp):
        return False
    try:
        # A field of a model of its own carries the config, which
        # TypeAdapter refuses for a model type.
        create_model(
            'Value', __config__=model.model_config, value=(tp, ...)
        ).model_json_schema()
    except (PydanticInvalidForJsonSchema, PydanticSchemaGenerationError):
        return False
    return True


def type_name(tp: Any) -> str:
    """
    The name of a type, as a serialized spec shows it; of a union, the names
    of its members, ``BeamCentre | FindCentre``.
    """
    if get_origin(tp) in (Union, UnionType):
        return ' | '.join(map(type_name, get_args(tp)))
    return getattr(tp, '__name__', None) or repr(tp)


def fits(data: Any, field: Any) -> bool:
    """
    Whether data of type ``data`` can be the value of a field of type ``field``.

    ``| None`` is removed on both sides. Then the data fit when ``data`` is
    ``field`` or a subclass of it. A generic alias, such as ``list[int]``,
    fits only itself.
    """
    data, _ = optional(data)
    field, _ = optional(field)
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
    value's type. A field of a group is validated so only in the request
    phase, at a place given inline in a group with a reference inside; the
    group's validators run when the group is validated whole (see
    :mod:`ess.spec.places`).

    For a list field, the model validates one row: by its element type alone,
    with that type's metadata, without the field's length constraints, which
    hold for the whole list. A reference stands for one row. With ``whole``,
    as for a field of a group, the model validates the whole field, a list
    too.
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


def json_form(
    model: type[BaseModel], name: str, value: Any, whole: bool = False
) -> Any:
    """
    The JSON form of ``value``, a valid value, or row, of the field ``name``
    of ``model`` (see :func:`model_of`), as its declared type dumps it to
    JSON and JSON reads back: an instance of a subclass of a model type
    without the subclass's fields, and a number that is not finite as
    ``None``.
    """
    made = model_of(model, name, whole).model_construct(**{name: value})
    return from_json(made.model_dump_json(by_alias=True))[name]


def inline(model: type[BaseModel], name: str, value: Any, whole: bool = False) -> Any:
    """
    The JSON form of ``value``, given inline for the field ``name`` of
    ``model``, or a row of it unless ``whole`` (see :func:`model_of`), as the
    framework holds it: of a group, the JSON form of the group validated.

    ``value``, an instance of a group by its dump, is validated from its JSON
    form, with a field that a model lacks refused at any depth, since
    validating would drop it silently. Its JSON form must read back as the
    value. Raises:

    - ``PydanticSerializationError`` if it has no JSON form;
    - ``ValidationError`` if it is not valid, or if its JSON form is not,
      such as of a number that is not finite, which JSON holds as ``null``;
    - :class:`~ess.spec.refs.ParamsError` at each position inside the value,
      without ``name``, that holds a file, which is given by reference to a
      dataset, whose JSON form reads back as another value, such as a field
      of a group whose validator changes a value it has validated already,
      or whose JSON form is an object with the key ``"@ref"``, which reads
      as a reference (see :func:`ess.spec.refs.positions`).
    """
    one = model_of(model, name, whole)
    if isinstance(value, BaseModel):
        # Its own serializer would write a number that is not finite as null.
        value = value.model_dump()
    given = to_json({name: value}, inf_nan_mode='constants')
    made = getattr(one.model_validate_json(given, extra='forbid'), name)
    if files := list(_files((), made)):
        raise ParamsError((loc, _FILE_INLINE) for loc in files)
    stored = json_form(model, name, made, whole)
    try:
        again = getattr(one.model_validate_json(to_json({name: stored})), name)
    except ValidationError as error:
        (first, *_) = error.errors()
        raise ValidationError.from_exception_data(
            one.__name__,
            [{'type': _NO_JSON_FORM_ERROR, 'loc': first['loc'], 'input': None}],
        ) from None
    if again != made:
        raise ParamsError([(_differs((), made, again), _NO_JSON_FORM)])
    if refs := [
        loc for loc, v in positions((), stored) if isinstance(v, dict) and '@ref' in v
    ]:
        raise ParamsError((loc, _READS_AS_REF) for loc in refs)
    return stored


_FILE_INLINE = 'a file is given by reference to a dataset, dataset(path=...)'
_NO_JSON_FORM = 'has no JSON form that reads back as the value'
_READS_AS_REF = 'has a JSON form that reads as a reference'
_NO_JSON_FORM_ERROR = PydanticCustomError('no_json_form', _NO_JSON_FORM)


def _differs(loc: Loc, a: Any, b: Any) -> Loc:
    """
    The first position at which ``a`` and ``b``, two values at ``loc``,
    differ: a field of a group or an element of a list, at any depth.
    """
    if isinstance(a, BaseModel) and type(a) is type(b):
        pairs: Iterable[tuple[str | int, Any, Any]] = (
            (n, getattr(a, n), getattr(b, n)) for n in type(a).model_fields
        )
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        pairs = ((i, x, y) for i, (x, y) in enumerate(zip(a, b, strict=True)))
    else:
        return loc
    for at, x, y in pairs:
        if x != y:
            return _differs((*loc, at), x, y)
    return loc


def _files(loc: Loc, value: Any) -> Iterator[Loc]:
    """
    The position of each file in ``value``, a validated value at ``loc``:
    the value, a field of a group, or an element of a list, at any depth.
    """
    if isinstance(value, PurePath):
        yield loc
    elif isinstance(value, BaseModel):
        for name in type(value).model_fields:
            yield from _files((*loc, name), getattr(value, name))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _files((*loc, i), item)
