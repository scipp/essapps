# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The field types of a spec, taken apart.

A spec's models carry no annotations for the framework: what the framework
needs, it reads from the field types. A field's type is built from a few
pieces, which these functions take apart::

    BeamCentre | None       optional(...)   -> (BeamCentre, True)
    list[SampleFile]        element(...)    -> SampleFile
    list[BeamCentre | None] value_type(...) -> BeamCentre
    IofQ                    supertypes(...) -> [IofQ, sc.DataArray]

with ``IofQ = NewType('IofQ', sc.DataArray)``. Each function sees through
``Annotated`` metadata, such as a validator: ``Annotated[X, ...]`` is ``X``
(see :func:`plain`). :func:`fits` says whether data
of one type can be the value of a field of another, so that an output of one
spec can feed a parameter of another. :func:`model_of` makes a model of one
field, which validates a value, or a row of a list, by that field alone, as
:func:`validated` does in the call phase and :func:`inline` for a value
given inline.
"""

from __future__ import annotations

import functools
import operator
from collections.abc import Callable, Mapping
from pathlib import PurePath
from types import NoneType, UnionType
from typing import Annotated, Any, NewType, Union, get_args, get_origin

from pydantic import BaseModel, Field, ValidationError, create_model
from pydantic.errors import PydanticInvalidForJsonSchema, PydanticSchemaGenerationError
from pydantic_core import from_json, to_json

from .refs import Ref, places


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


def is_single(tp: Any) -> bool:
    """
    Whether ``tp`` is the type of a single value: any type but a list, a
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


def is_group(tp: Any) -> bool:
    """Whether ``tp`` is a group: a pydantic model, or a ``NewType`` of one."""
    tp = base(tp)
    return isinstance(tp, type) and issubclass(tp, BaseModel)


def holds_file(tp: Any, seen: frozenset[type[BaseModel]] = frozenset()) -> bool:
    """
    Whether a type, or a field of a model in it, is a file; ``seen`` holds the
    models looked into already, so that a recursive model ends.
    """
    tp = base(tp)
    if is_file(tp):
        return True
    if is_group(tp):
        if tp in seen:
            return False
        return any(
            holds_file(f.annotation, seen | {tp}) for f in tp.model_fields.values()
        )
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


@functools.cache
def model_of(model: type[BaseModel], name: str, whole: bool = False) -> type[BaseModel]:
    """
    A model of one value of the field ``name`` of ``model`` alone, with its
    config and without its validators: it validates one value by its own
    field, as every value of a request is validated, in both phases. The
    params model has no decorators but model validators, which the framework
    never calls; a check of one value belongs to the value's type.

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


def validated(
    model: type[BaseModel],
    name: str,
    value: Any,
    read: Callable[[Ref], Any] | None = None,
    *,
    whole: bool = False,
) -> Any:
    """
    ``value`` of the field ``name`` of ``model``, validated alone (see
    :func:`model_of`): each row of a list field by its element type, unless
    ``whole``.

    With ``read`` ``None``, ``value`` is as a binding takes it, data. Else it
    is as a record holds it: each value, or row, a reference or an inline
    value in its JSON form. A reference is replaced by the data ``read`` gives
    for it, validated from the object, and an inline value is validated from
    its JSON form, as the request phase validated it (see :func:`inline`), so
    that both phases see the same value: a strict enum takes its string only
    from JSON. A group given field by field (see :func:`by_field`), as a
    mapping, is made from its fields, each validated so: like the params
    model, it is never validated whole.
    """
    one = model_of(model, name, whole)
    tp = value_type(model.model_fields[name].annotation)

    def check(v: Any) -> Any:
        if by_field(tp) and isinstance(v, Mapping):
            group = base(tp)
            fields = {f: validated(group, f, x, read, whole=True) for f, x in v.items()}
            return group.model_construct(**fields)
        if read is None:
            made = one.model_validate({name: v})
        elif isinstance(v, Ref):
            made = one.model_validate({name: read(v)})
        else:
            made = one.model_validate_json(to_json({name: v}))
        return getattr(made, name)

    if is_list(model, name) and not whole:
        return [check(v) for v in value]
    return check(value)


def by_field(tp: Any) -> bool:
    """
    Whether a value of type ``tp`` is given field by field: a group that holds
    a file, such as a run and what is given next to it. Each of its fields is
    given inline or by reference, as a parameter is.
    """
    return is_group(tp) and holds_file(tp)


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
    framework holds it.

    ``value`` is validated from its JSON form, with a field that a model lacks
    refused at any depth, since validating would drop it silently. Raises
    ``PydanticSerializationError`` if it has no JSON form, ``ValidationError``
    if it is not valid, and ``ValueError`` if its JSON form does not read back
    as the value, such as a number that is not finite, which JSON holds as
    ``null``, or reads as a reference at a place where a request may hold one
    (see :func:`ess.spec.refs.places`): for a field of a group, ``whole``,
    the field itself.
    """
    one = model_of(model, name, whole)
    given = to_json({name: value}, inf_nan_mode='constants')
    made = getattr(one.model_validate_json(given, extra='forbid'), name)
    stored = json_form(model, name, made, whole)
    try:
        again = one.model_validate_json(to_json({name: stored}))
        reads_back = getattr(again, name) == made
    except ValidationError:
        reads_back = False
    if not reads_back:
        raise ValueError('has no JSON form that reads back as the value')
    if whole:
        items = [stored]
    else:
        rows = [stored] if is_list(model, name) else stored
        items = [item for _, _, item in places(name, rows)]
    if any(isinstance(item, dict) and '@ref' in item for item in items):
        raise ValueError('has a JSON form that reads as a reference')
    return stored
