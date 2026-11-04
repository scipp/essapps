# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What a request may write at a field, read from the type of the field.

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

The request type, and so the params schema of a spec and the Panel form,
decide what a request writes at a field from its type, with these
functions (see :mod:`ess.spec.request_types`)::

    # The type without | None, whether it is X | None, and its metadata.
    tp, none, metadata = unwrapped(info.annotation, field_metadata(info))
    # The part that a request writes, with its metadata, and the groups
    # whose data a reference fills the field with, each T of Referenced[T].
    inline, inline_metadata, referenced = split(tp, *metadata)
    # A union of groups: show the fields of each alternative. None for any
    # other type.
    alternatives(inline, *inline_metadata)  # ('kind', {'given': ..., ...})
    # A file field, Annotated[Path, File(...)]: offer a picker of the files
    # that fit the marker's metadata. None for any other type.
    file_marker(inline, *inline_metadata)  # File(kind='mask')
    # Groups that a reference fills the field with: offer a picker of
    # records.
    referenced  # (ComputedBeamCentre,)

:func:`field_metadata` gives the metadata of a field with its
discriminator, and :func:`title` the label of a field.
The rules these serve are stated in :mod:`ess.spec._workflow_spec`.
"""

from __future__ import annotations

import dataclasses
import functools
import operator
from collections.abc import Callable, Iterator
from datetime import datetime
from enum import Enum
from pathlib import Path
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
    Discriminator,
    Field,
    NaiveDatetime,
    RootModel,
    Tag,
)
from pydantic.fields import FieldInfo

from .parameters import REFERENCED, File

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


def file_marker(tp: Any, *metadata: Any) -> File | None:
    """
    The marker of a file field, if ``tp``, with ``metadata``, is ``Path``
    marked :class:`~ess.spec.parameters.File`; ``None`` otherwise. ``tp`` and
    ``metadata`` are as :func:`split` gives the part that a request writes::

        file_marker(Path, File(kind='mask'))  # File(kind='mask')
        file_marker(Path)  # None
    """
    if tp is not Path:
        return None
    return next((m for m in metadata if isinstance(m, File)), None)


def file_of(info: FieldInfo) -> File | None:
    """
    The marker of the field with ``info``, if it is a file field, typed
    ``X`` or ``X | None``, or a list of one; ``None`` otherwise.
    """
    tp, _, metadata = unwrapped(info.annotation, field_metadata(info))
    if get_origin(tp) is list:
        tp, _, metadata = unwrapped(get_args(tp)[0])
    return file_marker(tp, *metadata)


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


def is_group(tp: Any) -> bool:
    """
    Whether ``tp`` is a group: a pydantic model, but not a ``RootModel``,
    whose JSON form is its root value, not an object of its fields.
    """
    return _is_model(tp) and not issubclass(tp, RootModel)


def _is_model(tp: Any) -> bool:
    return isinstance(tp, type) and issubclass(tp, BaseModel)


def is_referenced(tp: Any) -> bool:
    """Whether ``tp`` is ``Referenced[T]``, with the marker in its metadata."""
    return get_origin(tp) is Annotated and REFERENCED in tp.__metadata__


def is_file(tp: Any) -> bool:
    """Whether ``tp`` is ``Annotated`` with a :class:`File` marker in its metadata."""
    return get_origin(tp) is Annotated and any(
        isinstance(m, File) for m in tp.__metadata__
    )


def holds_ref(annotation: Any) -> bool:
    """
    Whether ``annotation`` holds ``Referenced[...]``, at any depth, in the
    fields of each group too.
    """
    return _holds(annotation, is_referenced)


def holds_file(annotation: Any) -> bool:
    """
    Whether ``annotation`` holds a file field, at any depth, in the fields
    of each group too.
    """
    return _holds(annotation, is_file)


def holds_marker(annotation: Any) -> bool:
    """
    Whether ``annotation`` holds ``Referenced[...]`` or a file field, at any
    depth, in the fields of each group too: whether a request writes
    something other than its type, a reference or a file, somewhere in it.
    """
    return holds_ref(annotation) or holds_file(annotation)


def _holds(
    annotation: Any, marked: Callable[[Any], bool], seen: frozenset[type] = frozenset()
) -> bool:
    """
    Whether ``marked`` holds for ``annotation`` or a type in it, at any
    depth, in the fields of each group too. ``seen`` holds the groups looked
    into already, so that a group that contains itself ends.
    """
    if marked(annotation):
        return True
    if is_group(annotation):
        return annotation not in seen and any(
            _holds(info.rebuild_annotation(), marked, seen | {annotation})
            for info in annotation.model_fields.values()
        )
    return any(_holds(arg, marked, seen) for arg in get_args(annotation))


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


def is_list(model: type[BaseModel], name: str) -> bool:
    """Whether the field ``name`` of ``model`` is typed ``list[X]``."""
    return get_origin(model.model_fields[name].annotation) is list


def field_type(info: FieldInfo) -> Any:
    """The type of a field with ``info``, with its :func:`field_metadata`."""
    metadata = field_metadata(info)
    return Annotated[info.annotation, *metadata] if metadata else info.annotation


def field_metadata(info: FieldInfo) -> tuple[Any, ...]:
    """
    The metadata of a field with ``info``, with its discriminator, which
    pydantic keeps apart from the rest as ``Field(discriminator='kind')``.
    """
    if info.discriminator is None:
        return tuple(info.metadata)
    return (*info.metadata, Field(discriminator=info.discriminator))


def title(name: str, info: FieldInfo) -> str:
    """
    The title of a field with ``info``: its own, or ``name`` in words,
    ``beam_centre`` as ``Beam Centre``. A form and the params schema of a
    spec label each field so. An alternative of a union is labelled by its
    tag field, titled by the tag's value rather than the field's name, which
    is that of every alternative::

        title('beam_centre', info)  # 'Beam Centre'
        title('find', group.model_fields['kind'])  # 'Find', unless titled
    """
    return info.title or name.replace('_', ' ').title()


def unwrapped(
    annotation: Any, metadata: tuple[Any, ...] = ()
) -> tuple[Any, bool, tuple[Any, ...]]:
    """
    ``annotation``, of a field with ``metadata`` (see
    :func:`field_metadata`), without its outer ``Annotated`` and
    ``| None``, at any depth, up to the marker of ``Referenced[T]``, which
    leaves ``T``; whether it had ``| None``; and ``metadata``, then the
    metadata of each ``Annotated``, the outer first. Unlike
    :func:`~ess.spec.optional`, it keeps the metadata of the
    members of a union and the elements of a list.
    """
    tp, none = annotation, False
    while REFERENCED not in metadata:
        origin, args = get_origin(tp), get_args(tp)
        if origin is Annotated:
            metadata = (*metadata, *tp.__metadata__)
            tp = args[0]
        elif origin in (Union, UnionType) and NoneType in args:
            rest = tuple(a for a in args if a is not NoneType)
            tp = rest[0] if len(rest) == 1 else Union[rest]  # noqa: UP007
            none = True
        else:
            break
    return tp, none, metadata


def split(tp: Any, *metadata: Any) -> tuple[Any, tuple[Any, ...], tuple[Any, ...]]:
    """
    The part of a field of type ``tp`` that a request writes, with its
    metadata; and the types whose data a reference fills the field with,
    the ``T`` of each ``Referenced[T]`` with its own metadata, such as an
    ``AfterValidator``. ``tp`` and ``metadata`` are as :func:`unwrapped`
    gives them. The part written is ``None`` if only a reference fills the
    field. Its metadata are ``metadata``, then those of its ``Annotated``. A
    discriminator in ``metadata`` tells alternatives written inline apart,
    so a single group written inline does not get it::

        split(Reduction)  # (Reduction, (), ())
        split(PixelMask, REFERENCED)  # (None, (), (PixelMask,))
        split(BeamCentre | Referenced[BeamCentre])
        # (BeamCentre, (), (BeamCentre,))
        split(
            Annotated[GivenCentre | FindCentre, Field(discriminator='kind')]
            | Referenced[ComputedBeamCentre]
        )
        # (GivenCentre | FindCentre, (Field(discriminator='kind'),),
        #  (ComputedBeamCentre,))
        split(GivenCentre | Referenced[Centre], Field(discriminator='kind'))
        # (GivenCentre, (), (Centre,))
    """
    if REFERENCED in metadata:
        return None, (), (_data(tp, *metadata),)
    members = get_args(tp) if get_origin(tp) in (Union, UnionType) else (tp,)
    referenced = tuple(
        _data(get_args(m)[0], *m.__metadata__) for m in members if is_referenced(m)
    )
    if not referenced:
        return tp, metadata, ()
    written = tuple(m for m in members if not is_referenced(m))
    if not written:
        return None, (), referenced
    if len(written) == 1:
        metadata = tuple(m for m in metadata if not discriminates(m))
    inline: Any = written[0] if len(written) == 1 else Union[written]  # noqa: UP007
    if get_origin(inline) is not Annotated:
        return inline, metadata, referenced
    return get_args(inline)[0], (*metadata, *inline.__metadata__), referenced


def _data(tp: Any, *metadata: Any) -> Any:
    """``tp`` with ``metadata`` but the marker of ``Referenced[T]``."""
    kept = tuple(m for m in metadata if m is not REFERENCED)
    return Annotated[tp, *kept] if kept else tp


def discriminates(meta: Any) -> bool:
    """Whether ``meta``, metadata of a type, is a discriminator."""
    return isinstance(meta, Discriminator) or (
        isinstance(meta, FieldInfo) and meta.discriminator is not None
    )


def _discriminator(metadata: tuple[Any, ...]) -> Any:
    """
    The discriminator in ``metadata``, the last if several: the name of a
    field or a callable. ``None`` if there is none.
    """
    found = None
    for meta in metadata:
        by = meta.discriminator if isinstance(meta, FieldInfo | Discriminator) else None
        if by is not None:
            found = by.discriminator if isinstance(by, Discriminator) else by
    return found


def alternatives(annotation: Any, *metadata: Any) -> tuple[str, dict[str, Any]] | None:
    """
    The tag field and the alternatives by tag value, if ``annotation``,
    possibly typed ``X | None``, is a union of groups::

        alternatives(GivenCentre | FindCentre, Field(discriminator='kind'))
        # ('kind', {'given': GivenCentre, 'find': FindCentre})

    ``None`` if ``annotation`` is not a union with a group in it, such as
    ``int | str``, or is ``None``, as :func:`split` gives it for a field
    that only a reference fills. ``metadata`` are those of the field (see
    :func:`field_metadata`), which may give the discriminator. A
    discriminator in ``annotation`` takes precedence. The alternatives are
    in the order of the union, without their ``Annotated`` metadata.

    ``annotation`` is the part of a field that a request writes (see
    :func:`split`). Raises ``ValueError`` for a union that breaks a rule of
    :mod:`ess.spec._workflow_spec`: a member that is not a group; no
    discriminator, or one that is not the name of a field, such as a
    callable ``Discriminator``; a tag field with an alias or not typed a
    ``Literal`` of one string.
    """
    if annotation is None:
        return None
    tp, _, inner = unwrapped(annotation)
    discriminator = _discriminator((*metadata, *inner))
    if get_origin(tp) not in (Union, UnionType):
        return None
    members = [plain(arg) for arg in get_args(tp)]
    if not any(is_group(m) for m in members):
        return None
    if not all(is_group(m) for m in members):
        raise ValueError(
            'a union holds groups only, or no group; a value of another type is a '
            'field of a group, such as of an alternative of the union'
        )
    if not isinstance(discriminator, str):
        raise ValueError(
            'a union of groups has a discriminator, the name of its tag field, '
            "such as Field(discriminator='kind')"
        )
    groups: dict[str, Any] = {}
    for member in members:
        where = f'{member.__name__}.{discriminator}'
        info = member.model_fields.get(discriminator)
        tags = () if info is None else _tags(info.annotation)
        if len(tags) != 1 or type(tags[0]) is not str:
            raise ValueError(
                f"{where}: the tag of a union is a Literal of one string, such as "
                "Literal['find']"
            )
        if any(
            getattr(info, a) is not None
            for a in ('alias', 'validation_alias', 'serialization_alias')
        ):
            raise ValueError(f'{where}: a tag has no alias')
        groups[tags[0]] = member  # pydantic refuses two alternatives of one tag
    return discriminator, groups


def _tags(annotation: Any) -> tuple[Any, ...]:
    """The values of ``annotation`` if it is a ``Literal``, else none."""
    return get_args(annotation) if get_origin(annotation) is Literal else ()


def tagged_members(tp: Any, metadata: tuple[Any, ...]) -> dict[Any, Any] | None:
    """
    The members of ``tp`` by tag, if it is a union with a discriminator in
    ``metadata`` (see :func:`unwrapped`): by a callable discriminator, such
    as :func:`~ess.spec.request_types.source_of`, each member with its
    ``Tag``, or by a tag field. ``None`` for any other type.

    Unlike :func:`alternatives`, which checks the union of a spec's model
    against its rules, this reads the types the framework derives too: a
    member that is not a group, and a tag with an alias, such as that of a
    reference to an output.
    """
    if get_origin(tp) not in (Union, UnionType):
        return None
    by = _discriminator(metadata)
    members = get_args(tp)
    if callable(by):
        return {_tag(m): m for m in members}
    if isinstance(by, str):
        return {_tags(plain(m).model_fields[by].annotation)[0]: m for m in members}
    return None


def _tag(member: Any) -> str:
    """The tag of ``member``, a member of a union by a callable discriminator."""
    return next(m.tag for m in member.__metadata__ if isinstance(m, Tag))
