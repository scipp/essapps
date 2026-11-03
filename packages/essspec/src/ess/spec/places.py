# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What a request may write at a field, read from its type, and the phases in
which a value is validated.

The rules are stated once, in :mod:`ess.spec._workflow_spec`, with the terms
used here: parameter, row, group, field, file, reference to an output, JSON
form. A request is validated one parameter at a time, in two phases, and a
spec checks its defaults the same way:

=============  =================================  ==========================
When           Function                           What it does
=============  =================================  ==========================
spec made      :func:`validated`                  validates the default of
                                                  a field as a binding
                                                  takes it
request phase  :func:`.request_types.held`        validates a parameter by
                                                  its request type, and
                                                  returns what the record
                                                  stores: the JSON form of
                                                  each group and file, and
                                                  each reference to an
                                                  output as it is
call phase     :func:`validated`, with the        validates a parameter by
               context ``read`` and ``paths``     the params model from
                                                  what the record stores:
                                                  each reference to an
                                                  output replaced by its
                                                  data, each file with its
                                                  local path
=============  =================================  ==========================

The *request phase* is when a request is submitted, before any data is
read. The *call phase* is when the request runs, once the backend has read
the data of each reference to an output. The request type of a field (see
:mod:`ess.spec.request_types`) is derived from its type with the functions
here.

The request type, and so the params schema of a spec and the Panel form,
decide what a request writes at a field from its type, with these
functions::

    # The type without | None, whether it is X | None, and its metadata.
    tp, none, metadata = root_type(Params, 'centre')
    # The same of any field typed annotation, with the field's metadata.
    tp, none, metadata = unwrapped(annotation, metadata)
    # The part that a request writes, with its metadata, and the groups
    # whose data a reference fills the field with, each T of Referenced[T].
    inline, inline_metadata, referenced = split(tp, *metadata)
    # A union of groups: show the fields of each alternative. None for any
    # other type.
    alternatives(inline, *inline_metadata)  # ('kind', {'given': ..., ...})
    # A file model: offer a picker of files. Any other group: show its
    # fields.
    field_types.is_file_model(inline)
    # Groups that a reference fills the field with: offer a picker of
    # records.
    referenced  # (ComputedBeamCentre,)

:func:`field_metadata` gives the metadata of a field with its
discriminator, and :func:`title` the label of a field.

An error is a :class:`~ess.spec.refs.ParamsError` that names the position of
each problem in the params, such as ``reduction.bins`` or ``runs[1]``.
"""

from __future__ import annotations

from types import NoneType, UnionType
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, Discriminator, Field, Tag, ValidationError
from pydantic.fields import FieldInfo
from pydantic_core import ErrorDetails, to_json

from . import field_types
from .parameters import REFERENCED, DataRefused
from .refs import Loc, ParamsError, iter_refs


def validated(
    model: type[BaseModel],
    name: str,
    value: Any,
    table: bool = False,
    context: dict[str, Any] | None = None,
) -> Any:
    """
    ``value`` of the field ``name`` of ``model`` as the binding takes it,
    validated by its field alone (see
    :func:`~ess.spec.field_types.model_of`); without the length constraints
    of a list if it is a ``table``. Raises
    :class:`~ess.spec.refs.ParamsError` at the position of each problem in
    ``value`` (see :func:`position`).

    Without ``context``, ``value`` is data, as in direct use, so that each
    field typed ``Referenced[T]`` takes ``T``. A spec checks a default with
    it, and :func:`ess.spec.testing.instance` makes a params instance.

    With ``context``, ``value`` is what the record stores, validated from
    its JSON form, in the call phase. ``context`` holds ``read``, which gives
    the data of each reference to an output (see
    :data:`~ess.spec.parameters.Referenced`), and ``paths``, the local path
    of each file by its identity (see
    :class:`~ess.spec.parameters.FileModel`). A field that a model lacks is
    refused. A group that holds a reference to an output, at any depth, is
    validated only here, so its validators run when the request runs and
    see the data read. Every other group was validated whole when the
    request was submitted too.

    pydantic tries the members of a union with a member ``Referenced[...]``
    in order, each with the stored value. A member written inline is given a
    reference too, and refuses it, since a field that a model lacks is
    refused. So a validator of such a member, such as a
    ``model_validator(mode='before')``, raises ``ValueError`` for a value it
    does not take, as it must for a malformed request when it is submitted:
    any other exception fails the call. When a member written inline refuses
    a value written inline, the error that the member ``Referenced[...]``
    adds, that the value is not a reference, is dropped. When the data read
    for a reference fail a check of ``T``, they are one error, at the
    reference.
    """
    one = field_types.model_of(model, name, table)
    try:
        if context is None:
            made = one.model_validate({name: value})
        else:
            given = to_json({name: value})
            made = one.model_validate_json(given, context=context, extra='forbid')
    except ValidationError as error:
        tp = field_type(one.model_fields[name])
        found = [(position(tp, e['loc'][1:]), e) for e in error.errors()]
        raise ParamsError(
            ((name, *at), e['msg'])
            for at, e in found
            if e['type'] != 'output_ref' or not _inside(at, found)
        ) from None
    except DataRefused as refused:
        at = next(loc for loc, ref in iter_refs({name: value}) if ref == refused.ref)
        msg = '; '.join(dict.fromkeys(e['msg'] for e in refused.errors))
        raise ParamsError([(at, msg)]) from None
    return getattr(made, name)


def _inside(at: Loc, found: list[tuple[Loc, ErrorDetails]]) -> bool:
    """Whether an error of ``found``, but ``output_ref``, is at ``at`` or inside."""
    return any(p[: len(at)] == at and e['type'] != 'output_ref' for p, e in found)


def position(tp: Any, loc: Loc) -> Loc:
    """
    The position in a value of type ``tp`` that ``loc``, the location of a
    pydantic error inside the value, names. ``tp`` is a request type (see
    :mod:`ess.spec.request_types`), or a type of the params model. Of
    ``loc``, each part that steps into the value is kept: a field of a group
    or an index of a list, and the field that an error of a missing field
    names. A part that names a member of a union is not: the tag of a tagged
    union, of a source or of a group, and the label of a member of a union
    with a member ``Referenced[...]``, which only the params model has::

        position(Centre, ('find', 'tolerance'))  # ('tolerance',)

    Read along the type, a field named like a tag is a field. After the
    label of a member of a union with a member ``Referenced[...]``, the
    position is read along the member written inline, whose errors are the
    ones that remain (see :func:`validated`).
    """
    at: list[str | int] = []
    for part in loc:
        tp = _bare(tp)
        if (members := _tagged(tp)) is not None:
            tp = members.get(part, Any)
        elif get_origin(tp) in (Union, UnionType):
            inline, metadata, _ = split(tp)
            tp = Annotated[inline, *metadata] if metadata else inline
        else:
            at.append(part)
            if field_types.is_group(tp) and part in tp.model_fields:
                tp = field_type(tp.model_fields[part])
            elif get_origin(tp) is list:
                tp = get_args(tp)[0]
    return tuple(at)


def _bare(tp: Any) -> Any:
    """``tp`` without ``| None`` and the ``Annotated`` of any but a tagged union."""
    while True:
        origin, args = get_origin(tp), get_args(tp)
        if origin in (Union, UnionType) and NoneType in args:
            rest = tuple(a for a in args if a is not NoneType)
            tp = rest[0] if len(rest) == 1 else Union[rest]  # noqa: UP007
        elif origin is Annotated and _tagged(tp) is None:
            tp = args[0]
        else:
            return tp


def _tagged(tp: Any) -> dict[Any, Any] | None:
    """
    The members of ``tp`` by tag, if it is a tagged union: by a callable
    discriminator, such as :func:`~ess.spec.request_types.source_of`, each
    member with its ``Tag``, or by a tag field. ``None`` for any other type.
    """
    if get_origin(tp) is not Annotated:
        return None
    union, *metadata = get_args(tp)
    if get_origin(union) not in (Union, UnionType):
        return None
    members = [m for m in get_args(union) if m is not NoneType]
    for meta in metadata:
        if callable(by := _discriminator(meta)):
            return {_tag(m): m for m in members}
        if isinstance(by, str):
            plain = map(field_types.plain, members)
            return {
                get_args(g.model_fields[by].annotation)[0]: m
                for g, m in zip(plain, members, strict=True)
            }
    return None


def _discriminator(meta: Any) -> Any:
    """The discriminator in ``meta``, a field's name or a callable, or ``None``."""
    by = meta.discriminator if isinstance(meta, FieldInfo | Discriminator) else None
    return by.discriminator if isinstance(by, Discriminator) else by


def _tag(member: Any) -> str:
    """The tag of ``member``, a member of a union by a callable discriminator."""
    return next(m.tag for m in member.__metadata__ if isinstance(m, Tag))


def field_type(info: FieldInfo) -> Any:
    """The type of a field with ``info``, with its :func:`field_metadata`."""
    metadata = field_metadata(info)
    return Annotated[info.annotation, *metadata] if metadata else info.annotation


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
        return unwrapped(get_args(info.annotation)[0])
    return unwrapped(info.annotation, field_metadata(info))


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


def unwrapped(
    annotation: Any, metadata: tuple[Any, ...] = ()
) -> tuple[Any, bool, tuple[Any, ...]]:
    """
    ``annotation``, of a field with ``metadata`` (see
    :func:`field_metadata`), without its outer ``Annotated`` and
    ``| None``, at any depth, up to the marker of ``Referenced[T]``, which
    leaves ``T``; whether it had ``| None``; and ``metadata``, then the
    metadata of each ``Annotated``, the outer first. Unlike
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
        _data(get_args(m)[0], *m.__metadata__)
        for m in members
        if field_types.is_referenced(m)
    )
    if not referenced:
        return tp, metadata, ()
    written = tuple(m for m in members if not field_types.is_referenced(m))
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
    tp, _, inner = unwrapped(annotation)
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
