# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What a request may write at a parameter or a row, read from its type, and
the phases in which a value is validated.

The rules are stated once, in :mod:`ess.spec._workflow_spec`, with the terms
used here: parameter, row, group, field, file, reference to an output, data
value, JSON form. A request is validated one parameter at a time, in two
phases, and a spec checks its defaults the same way:

=============  =================================  ==========================
When           Function                           What it does
=============  =================================  ==========================
spec made      :func:`field_value`                validates the default of
                                                  a field of a group as a
                                                  request would write it
request phase  :func:`.request_types.held`        validates a parameter by
                                                  its request type, and
                                                  returns what the record
                                                  stores: the JSON form of
                                                  each group and file, and
                                                  each reference to an
                                                  output as it is
call phase     :func:`.request_types.value_of`    makes the value of a
                                                  parameter from what the
                                                  record stores, each value
                                                  by the type of its
                                                  source: each reference to
                                                  an output replaced by its
                                                  data, each file with its
                                                  local path
=============  =================================  ==========================

The *request phase* is when a request is submitted, before any data is
read. The *call phase* is when the request runs, once the backend has read
the data of each reference to an output. The request type of a place (see
:mod:`ess.spec.request_types`) is derived from its type with the functions
here.

The request type, and so the params schema of a spec and the Panel form,
decide what a request writes at a parameter, or at each row of a list
parameter, from its type, with these functions::

    # The type without | None, whether it is X | None, and its metadata.
    tp, none, metadata = root_type(Params, 'centre')
    # The part that a request writes, with its metadata, and the groups
    # whose data a reference fills the place with, each T of Referenced[T].
    inline, inline_metadata, referenced = split(tp, *metadata)
    # A union of groups: show the fields of each alternative. None for any
    # other type.
    alternatives(inline, *inline_metadata)  # ('kind', {'given': ..., ...})
    # A file model: offer a picker of files. Any other group: show its
    # fields.
    field_types.is_file_model(inline)
    # Groups that a reference fills the place with: offer a picker of
    # records.
    referenced  # (ComputedBeamCentre,)

:func:`unwritten` gives the first data value of a group, :func:`field_metadata`
the metadata of a field with its discriminator, and :func:`title` the label
of a field.

An error is a :class:`~ess.spec.refs.ParamsError` that names the position of
each problem in the params, such as ``reduction.bins`` or ``runs[1]``.
Inside a union of groups, the position leaves out the tag:
``centre.tolerance``.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping
from enum import Enum
from types import NoneType, UnionType
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, Discriminator, Field, ValidationError
from pydantic.fields import FieldInfo
from pydantic_core import ErrorDetails, PydanticSerializationError

from . import field_types
from .parameters import REFERENCED
from .refs import (
    AccumulatorRef,
    FileRef,
    Loc,
    OutputRef,
    ParamsError,
    iter_refs,
)


def validated(model: type[BaseModel], name: str, value: Any) -> Any:
    """
    ``value``, data as a binding takes it, of the parameter ``name`` of
    ``model``, validated by its field alone (see
    :func:`~ess.spec.field_types.model_of`); for a list parameter, each row
    by the element type. A spec checks the default of a parameter with it,
    and :func:`ess.spec.testing.instance` makes a params instance. Raises
    :class:`~ess.spec.refs.ParamsError` at the position of each problem.
    """
    one = field_types.model_of(model, name)

    def check(loc: Loc, v: Any) -> Any:
        try:
            return getattr(one.model_validate({name: v}), name)
        except ValidationError as error:
            raise ParamsError(
                ((*loc, *e['loc'][1:]), e['msg']) for e in error.errors()
            ) from None

    if field_types.is_list(model, name):
        return [check((name, i), v) for i, v in enumerate(value)]
    return check((name,), value)


def field_value(group: type[BaseModel], name: str, value: Any) -> Any:
    """
    ``value`` of the field ``name`` of ``group`` as validating the group
    makes it: a list with its length constraints, and its JSON form checked
    to read back as the value (see :func:`~ess.spec.field_types.inline`). A
    spec checks the default of each field of a group with it. Raises
    :class:`~ess.spec.refs.ParamsError` at ``name``.
    """

    def problem(error: ErrorDetails) -> tuple[Loc, str]:
        return (name, *error['loc'][1:]), error['msg']

    inline_at((name,), group, name, value, problem, whole=True)
    one = field_types.model_of(group, name, whole=True)
    try:
        return getattr(one.model_validate({name: value}), name)
    except ValidationError as error:
        raise ParamsError(map(problem, error.errors())) from None


def inline_at(
    loc: Loc,
    model: type[BaseModel],
    name: str,
    value: Any,
    problem: Callable[[ErrorDetails], tuple[Loc, str]],
    whole: bool,
    context: dict[str, Any] | None = None,
) -> Any:
    """
    The JSON form of ``value`` at ``loc``, of the field ``name`` of
    ``model``, validated with ``context`` (see
    :func:`~ess.spec.field_types.inline`); ``problem`` places each error of
    validation and words it.
    """
    try:
        return field_types.inline(model, name, value, whole=whole, context=context)
    except PydanticSerializationError as error:
        raise _refused(loc, f'cannot be stored as JSON: {error}') from None
    except ValidationError as error:
        raise ParamsError(map(problem, error.errors())) from None
    except ParamsError as error:
        raise ParamsError(((*loc, *at), msg) for at, msg in error.errors) from None
    except ValueError as error:
        raise _refused(loc, str(error)) from None


def _refused(loc: Loc, msg: str) -> ParamsError:
    return ParamsError([(loc, msg)])


def root_type(model: type[BaseModel], name: str) -> tuple[Any, bool, tuple[Any, ...]]:
    """
    The type of the parameter ``name`` of ``model``, or of its rows if it is
    a list parameter: without ``| None`` and ``Annotated``; whether it is
    typed ``X | None``; and its metadata. The metadata are those of the
    field, with its discriminator, then those of each ``Annotated`` in the
    type, the outer first. The metadata of the field of a list parameter,
    such as its length, belong to the list, so a row has only those of its
    type. The marker of ``Referenced[T]`` ends the type at ``T``, the type
    of the data, so ``Referenced[X | None]`` keeps its ``| None``::

        root_type(Params, 'beam_centre')
        # (GivenCentre | FindCentre, False, (Field(discriminator='kind'),))
        root_type(Params, 'mask')  # mask: Referenced[PixelMask] | None = None
        # (PixelMask, True, (REFERENCED,))
        root_type(Params, 'samples')  # samples: list[Sample] = Field(min_length=2)
        # (Sample, False, ())
    """
    info = model.model_fields[name]
    if field_types.is_list(model, name):
        return _unwrapped(get_args(info.annotation)[0])
    return _unwrapped(info.annotation, field_metadata(info))


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
    spec label each place so. An alternative of a union is labelled by its
    tag field, titled by the tag's value rather than the field's name, which
    is that of every alternative::

        title('beam_centre', info)  # 'Beam Centre'
        title('find', group.model_fields['kind'])  # 'Find', unless titled
    """
    return info.title or name.replace('_', ' ').title()


def _unwrapped(
    annotation: Any, metadata: tuple[Any, ...] = ()
) -> tuple[Any, bool, tuple[Any, ...]]:
    """
    ``annotation`` without its outer ``Annotated`` and ``| None``, at any
    depth, up to the marker of ``Referenced[T]``, which leaves ``T``;
    whether it had ``| None``; and ``metadata``, then the metadata of each
    ``Annotated``, the outer first. Unlike
    :func:`~ess.spec.field_types.optional`, it keeps the metadata of the
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
    The part of a place of type ``tp`` that a request writes, with its
    metadata; and the types whose data a reference fills the place with,
    the ``T`` of each ``Referenced[T]`` with its own metadata, such as an
    ``AfterValidator``. ``tp`` and ``metadata`` are as :func:`root_type`
    gives them. The part written is ``None`` if only a reference fills the
    place. Its metadata are ``metadata``, then those of its ``Annotated``. A
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
        metadata = tuple(m for m in metadata if not _discriminates(m))
    inline: Any = written[0] if len(written) == 1 else Union[written]  # noqa: UP007
    if get_origin(inline) is not Annotated:
        return inline, metadata, referenced
    return get_args(inline)[0], (*metadata, *inline.__metadata__), referenced


def is_referenced(tp: Any) -> bool:
    """Whether ``tp`` is ``Referenced[T]``, with the marker in its metadata."""
    return get_origin(tp) is Annotated and REFERENCED in tp.__metadata__


def _data(tp: Any, *metadata: Any) -> Any:
    """``tp`` with ``metadata`` but the marker of ``Referenced[T]``."""
    kept = tuple(m for m in metadata if m is not REFERENCED)
    return Annotated[tp, *kept] if kept else tp


def _discriminates(meta: Any) -> bool:
    """Whether ``meta``, metadata of a type, is a discriminator."""
    return isinstance(meta, Discriminator) or (
        isinstance(meta, FieldInfo) and meta.discriminator is not None
    )


def alternatives(annotation: Any, *metadata: Any) -> tuple[str, dict[str, Any]] | None:
    """
    The tag field and the alternatives by tag value, if ``annotation``,
    possibly typed ``X | None``, is a union of groups::

        alternatives(GivenCentre | FindCentre, Field(discriminator='kind'))
        # ('kind', {'given': GivenCentre, 'find': FindCentre})

    ``None`` if ``annotation`` is not a union with a group in it, such as
    ``int | str``, or is ``None``, as :func:`split` gives it for a place
    that only a reference fills. ``metadata`` are those of the field (see
    :func:`field_metadata`), which may give the discriminator. A
    discriminator in ``annotation`` takes precedence. The alternatives are
    in the order of the union, without their ``Annotated`` metadata.

    ``annotation`` is the part of a place that a request writes (see
    :func:`split`). Raises ``ValueError`` for a union that breaks a rule of
    :mod:`ess.spec._workflow_spec`: a member that is not a group; no
    discriminator, or one that is not the name of a field, such as a
    callable ``Discriminator``; a tag field with an alias or not typed a
    ``Literal`` of one string.
    """
    if annotation is None:
        return None
    tp, _, inner = _unwrapped(annotation)
    discriminator = None
    for meta in (*metadata, *inner):
        if isinstance(meta, FieldInfo) and meta.discriminator is not None:
            discriminator = meta.discriminator
        elif isinstance(meta, Discriminator):
            discriminator = meta
    if get_origin(tp) not in (Union, UnionType):
        return None
    members = [field_types.plain(arg) for arg in get_args(tp)]
    if not any(field_types.is_group(m) for m in members):
        return None
    if not all(field_types.is_group(m) for m in members):
        raise ValueError(
            'a union holds groups only, or no group; a value of another type is a '
            'field of a group, such as of an alternative of the union'
        )
    if isinstance(discriminator, Discriminator):
        discriminator = discriminator.discriminator
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


@functools.cache
def unwritten(group: type[BaseModel]) -> str | None:
    """
    The name of the first data value of ``group``, or ``None`` if it has
    none. A form has no input for a data value, so the place of a group with
    one is marked ``Referenced[...]``.
    """
    return next(
        (n for n, f in group.model_fields.items() if not _interpreted(f.annotation)),
        None,
    )


def _interpreted(annotation: Any) -> bool:
    """
    Whether a field typed ``annotation`` is interpreted: a scalar; a file
    model, possibly typed ``X | None``; or a list of scalars or of file
    models, typed neither ``list[X] | None`` nor ``list[X | None]``.
    """
    tp, none, item_none = field_types.taken_apart(annotation)
    if item_none is not None:
        return not (none or item_none) and (
            _is_scalar(tp) or field_types.is_file_model(tp)
        )
    return field_types.is_file_model(tp) or (not none and _is_scalar(tp))


def _is_scalar(tp: Any) -> bool:
    """
    Whether ``tp`` is a scalar, by the type alone: a type whose JSON form is
    a number, a string, or a bool; a ``Literal`` of such values, not of enum
    members; or an enum whose values are such. A serializer in the metadata
    of a field may still dump it to something else, which
    :func:`~ess.spec.request_types.held` checks.
    """
    if tp in (int, float, str, bool, *field_types.TIMES):
        return True
    if get_origin(tp) is Literal:
        values = get_args(tp)
    elif isinstance(tp, type) and issubclass(tp, Enum):
        values = tuple(member.value for member in tp)
    else:
        return False
    return all(type(v) in (str, int, float, bool) for v in values)


def ref_types(
    model: type[BaseModel], params: Mapping[str, Any]
) -> list[tuple[Loc, OutputRef | AccumulatorRef, Any]]:
    """
    Each reference to an output in ``params``, with its position and the
    type its data must fit: the group of ``Referenced[...]`` at its place,
    or the union of the groups if there are several (see :func:`split`).
    ``params`` are by name, as :func:`~ess.spec.request_types.held` returns
    them, so a reference to an output fills a whole parameter or row that
    the spec marks so. Files are left out.
    """
    return [
        (loc, ref, referenced_type(model, str(loc[0])))
        for loc, ref in iter_refs(params)
        if not isinstance(ref, FileRef)
    ]


def referenced_type(model: type[BaseModel], name: str) -> Any:
    """
    The union of the groups, with their metadata, that a reference fills the
    place ``name`` with.
    """
    tp, _, metadata = root_type(model, name)
    return Union[split(tp, *metadata)[2]]  # noqa: UP007
