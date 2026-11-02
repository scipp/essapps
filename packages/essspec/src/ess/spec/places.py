# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
What a request may write at a parameter or a row, and how its values are
validated.

The rules are stated once, in :mod:`ess.spec._workflow_spec`, with the terms
used here: parameter, row, group, field, file, reference to an output, data
value, JSON form. This module applies them. A request is validated one
parameter at a time, in two phases, and a spec checks its defaults the same
way:

=============  ======================  ======================================
When           Function                What it does
=============  ======================  ======================================
spec made      :func:`field_value`     validates the default of a field of a
                                       group as a request would write it
request phase  :func:`held`            validates a parameter as a request
                                       writes it, and returns what the
                                       record stores: the JSON form of each
                                       group and file, and each reference to
                                       an output as it is
call phase     :func:`validated`       makes the value of a parameter from
                                       what the record stores: each
                                       reference to an output replaced by
                                       its data, each file with its local
                                       path
=============  ======================  ======================================

The *request phase* is when a request is submitted, before any data is
read. The backend passes :func:`held` a resolver of files, :data:`Files`.
The *call phase* is when the request runs, once the backend has read the
data of each reference to an output.

The params schema of a spec and the Panel form both decide what to show at
a parameter, or at each row of a list parameter, from its type, with these
functions::

    # The type without | None, whether it is X | None, and its metadata.
    tp, none, metadata = root_type(Params, 'centre')
    # A union of groups: show the fields of each alternative. None for any
    # other type.
    alternatives(tp, *metadata)  # ('kind', {'given': ..., 'find': ...})
    # A group, not a file model, without a data value: show its fields.
    written_inline(tp)
    # A group, not a file model, without a file model in a field: offer a
    # picker of records.
    takes_output(tp)
    # A file model: offer a picker of files.
    field_types.is_file_model(tp)

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
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import NoneType, UnionType
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, Discriminator, Field, TypeAdapter, ValidationError
from pydantic.fields import FieldInfo
from pydantic_core import PydanticSerializationError

from . import field_types
from .parameters import FileModel
from .refs import (
    AccumulatorRef,
    FileRef,
    Loc,
    OutputRef,
    ParamsError,
    Ref,
    iter_refs,
    map_refs,
    positions,
    ref_at,
)

type Files = Callable[[str, type[FileModel]], str]
"""
Resolves a file in the request phase. It takes the file as the request names
it, such as ``'run:101'``, and the file model of the field, and returns the
file's identity, or raises ``ValueError`` (see
:class:`~ess.spec.parameters.FileModel`).
"""


@dataclass(frozen=True)
class _Slot:
    """
    The type of the field ``name`` of ``model``, a parameter or a field of a
    group, or of an element of it, a list, if ``element``.
    """

    model: type[BaseModel]
    name: str
    element: bool = False

    @property
    def annotation(self) -> Any:
        tp = self.model.model_fields[self.name].annotation
        return get_args(tp)[0] if self.element else tp

    @property
    def union(self) -> tuple[str, dict[str, Any]] | None:
        """The union of groups of the type, as :func:`alternatives` has it."""
        info = self.model.model_fields[self.name]
        field = () if self.element else field_metadata(info)
        return alternatives(self.annotation, *field)


def held(model: type[BaseModel], name: str, value: Any, files: Files) -> Any:
    """
    The value of the parameter ``name`` of ``model`` as the record stores
    it, in the request phase.

    ``value`` is what the request gives: plain data, or Python objects such
    as an instance of a group or a :data:`~ess.spec.refs.Ref`, which are
    dumped to plain data first. For a list parameter, each row is handled
    on its own:

    - A reference to an output is returned as its JSON form. The caller
      checks that its data fit the parameter (see :func:`ref_types`).
    - ``None`` is returned as is, if the parameter is ``X | None``.
    - A group, or a file, is validated whole from its JSON form, with each
      file resolved by ``files``. The JSON form of the validated value is
      returned, with each field left out at its default (see
      :func:`ess.spec.field_types.inline`).

    Refused, besides what validation refuses: a file that ``files`` did not
    resolve, such as one that a validator of a group makes from a name,
    since the file source has not checked it; and a reference to an output
    in the JSON form of a group, such as one that a serializer makes. Raises
    :class:`~ess.spec.refs.ParamsError` at the position of each problem.
    """
    resolved: set[str] = set()

    def resolve(file: str, model: type[FileModel]) -> str:
        identity = files(file, model)
        resolved.add(identity)
        return identity

    stored = _parameter(model, name, _ANY.dump_python(value), {'files': resolve})
    root = 2 if field_types.is_list(model, name) else 1
    problems = [
        (loc, problem)
        for loc, ref in iter_refs({name: stored})
        if (problem := _stored_ref_problem(ref, len(loc) == root, resolved))
    ]
    if problems:
        raise ParamsError(problems)
    return stored


def _stored_ref_problem(ref: Ref, at_root: bool, resolved: set[str]) -> str | None:
    """
    Why the record cannot store ``ref``, or ``None``. ``at_root`` says whether
    ``ref`` fills a whole parameter or row. ``resolved`` holds the identities
    of the files resolved.
    """
    if isinstance(ref, FileRef):
        if ref.file in resolved:
            return None
        return (
            f'file {ref} was not resolved: a validator made it, so the file '
            'source has not checked it'
        )
    if at_root:
        return None
    return (
        f'{ref}: the JSON form of a value written inline holds a reference to an '
        'output, which fills a whole parameter or a whole row'
    )


def _parameter(
    model: type[BaseModel], name: str, value: Any, context: dict[str, Any]
) -> Any:
    """``value``, plain data, of the parameter ``name`` as :func:`held` returns it."""
    if not field_types.is_list(model, name):
        return _held((name,), _Slot(model, name), value, context)
    if (ref := ref_at((name,), value)) is not None:
        raise _refused(
            (name,),
            f'{ref} is a reference to a whole list; a reference stands for one '
            'element of a list',
        )
    if not isinstance(value, list):
        raise _refused((name,), f'{value!r} is not a list')
    slot = _Slot(model, name, element=True)
    return [_held((name, i), slot, v, context) for i, v in enumerate(value)]


_ANY = TypeAdapter(Any)
"""Dumps Python objects to plain data, keeping a number that is not finite."""


def _held(loc: Loc, slot: _Slot, value: Any, context: dict[str, Any]) -> Any:
    tp, nullable = field_types.optional(slot.annotation)
    union = slot.union
    ref = ref_at(loc, value)
    if ref is not None and union is not None:
        raise _refused(
            loc,
            f'{ref}: a union of groups is written inline, with the tag of an '
            'alternative; no reference fits it',
        )
    if ref is not None and not isinstance(ref, FileRef):
        return ref.model_dump()
    if value is None and nullable:
        return None
    if not field_types.is_file_model(tp):
        if isinstance(ref, FileRef):
            raise _refused(
                loc,
                f'file {ref} does not fit {field_types.type_name(tp)}, which is '
                'not a file model',
            )
        if not isinstance(value, Mapping):
            raise _refused(
                loc,
                f'{value!r} is not an object; a group is written as an object '
                'of its fields',
            )
        _written(loc, _group(loc, slot, value))
    for at, item in positions(loc, value):
        if (
            at != loc
            and (ref := ref_at(at, item)) is not None
            and not isinstance(ref, FileRef)
        ):
            raise _refused(
                at,
                f'{ref}: a reference to an output fills a whole parameter or a '
                'whole row, never a field of a group',
            )
    return _inline(loc, slot, value, context)


def _written(loc: Loc, group: type[BaseModel]) -> None:
    """Refuse ``group`` written at ``loc`` if it is given by reference only."""
    if (data := unwritten(group)) is not None:
        held_type = field_types.type_name(group.model_fields[data].annotation)
        raise _refused(
            loc,
            f'{field_types.type_name(group)} is given by reference: no request '
            f'writes its field {data}, of {held_type}',
        )


def _group(loc: Loc, slot: _Slot, value: Mapping[str, Any]) -> type[BaseModel]:
    """
    The group whose fields ``value`` at ``loc`` gives: the group at the
    place, or of a union the alternative its tag names.
    """
    if (union := slot.union) is None:
        return field_types.optional(slot.annotation)[0]
    tag, groups = union
    if tag not in value:
        raise _refused(
            loc, f'the tag {tag!r} is not given; it is one of {list(groups)}'
        )
    if (ref := ref_at((*loc, tag), value[tag])) is not None:
        raise _refused(
            (*loc, tag), f'{ref}: a tag is written inline, never as a reference'
        )
    given = value[tag]
    if not isinstance(given, str) or (group := groups.get(given)) is None:
        raise _refused((*loc, tag), f'{given!r} is not a tag, one of {list(groups)}')
    return group


def validated(
    model: type[BaseModel],
    name: str,
    value: Any,
    read: Callable[[OutputRef | AccumulatorRef], Any] | None = None,
    paths: Mapping[str, Path] | None = None,
) -> Any:
    """
    The value of the parameter ``name`` of ``model``, validated by its field
    alone (see :func:`~ess.spec.field_types.model_of`); for a list
    parameter, each row by the element type.

    With ``read=None``, ``value`` is data, as a binding takes it. Otherwise
    ``value`` is what the record stores (see :func:`held`), in the call
    phase: ``read`` gives the data of each reference to an output, which
    replace it, and ``paths`` gives the local path of each file by its
    identity (see :class:`~ess.spec.parameters.FileModel`).

    The request phase validated each group from its JSON form already, so
    this fails only for the data of a reference to an output. It validates
    in Python mode with ``strict=False``, since a strict model takes an enum
    from its value only in JSON mode, and the request phase applied the
    model's strictness already. Raises
    :class:`~ess.spec.refs.ParamsError` at the position of each problem.
    """
    one = field_types.model_of(model, name)
    if read is not None:
        value = map_refs(
            {name: value}, lambda ref: ref if isinstance(ref, FileRef) else read(ref)
        )[name]
    strict = False if read is not None else None
    context = None if paths is None else {'paths': paths}

    def check(loc: Loc, v: Any) -> Any:
        try:
            given = one.model_validate({name: v}, strict=strict, context=context)
            return getattr(given, name)
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
    slot = _Slot(group, name)
    _inline((name,), slot, value)
    one = field_types.model_of(group, name, whole=True)
    try:
        return getattr(one.model_validate({name: value}), name)
    except ValidationError as error:
        raise _at((name,), slot, error) from None


def _at(loc: Loc, slot: _Slot, error: ValidationError) -> ParamsError:
    """
    ``error`` of a value at ``loc`` of the type ``slot``, validated by a model
    of its field, at the place of each problem. pydantic puts the tag of a
    union after the union's place, ``('beam_centre', 'find', 'tolerance')``,
    which is left out, ``beam_centre.tolerance``, so that the place is the
    same whichever way the alternative is chosen.
    """
    union = slot.union
    tags = {} if union is None else union[1]

    def place(inner: Loc) -> Loc:
        return (*loc, *(inner[1:] if inner and inner[0] in tags else inner))

    return ParamsError((place(e['loc'][1:]), e['msg']) for e in error.errors())


def _inline(
    loc: Loc, slot: _Slot, value: Any, context: dict[str, Any] | None = None
) -> Any:
    """
    The JSON form of ``value``, a group, a file, or a field of a group, at
    ``loc``, validated with ``context`` (see
    :func:`~ess.spec.field_types.inline`).
    """
    try:
        return field_types.inline(
            slot.model, slot.name, value, whole=not slot.element, context=context
        )
    except PydanticSerializationError as error:
        raise _refused(loc, f'cannot be stored as JSON: {error}') from None
    except ValidationError as error:
        raise _at(loc, slot, error) from None
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
    type::

        root_type(Params, 'beam_centre')
        # (GivenCentre | FindCentre, False, (Field(discriminator='kind'),))
        root_type(Params, 'samples')  # samples: list[Sample] = Field(min_length=2)
        # (Sample, False, ())
    """
    info = model.model_fields[name]
    if field_types.is_list(model, name):
        return _unwrapped(get_args(info.annotation)[0])
    tp, none, inner = _unwrapped(info.annotation)
    return tp, none, (*field_metadata(info), *inner)


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


def _unwrapped(annotation: Any) -> tuple[Any, bool, tuple[Any, ...]]:
    """
    ``annotation`` without its outer ``Annotated`` and ``| None``, at any
    depth; whether it had ``| None``; and the metadata of each ``Annotated``,
    the outer first. Unlike :func:`~ess.spec.field_types.optional`, it
    keeps the metadata of the members of a union and the elements of a list.
    """
    tp, metadata, none = annotation, (), False
    while True:
        origin, args = get_origin(tp), get_args(tp)
        if origin is Annotated:
            metadata = (*metadata, *tp.__metadata__)
            tp = args[0]
        elif origin in (Union, UnionType) and NoneType in args:
            rest = tuple(a for a in args if a is not NoneType)
            tp = rest[0] if len(rest) == 1 else Union[rest]  # noqa: UP007
            none = True
        else:
            return tp, none, metadata


def alternatives(annotation: Any, *metadata: Any) -> tuple[str, dict[str, Any]] | None:
    """
    The tag field and the alternatives by tag value, if ``annotation``,
    possibly typed ``X | None``, is a union of groups::

        alternatives(GivenCentre | FindCentre, Field(discriminator='kind'))
        # ('kind', {'given': GivenCentre, 'find': FindCentre})

    ``None`` if ``annotation`` is not a union with a group in it, such as
    ``int | str``. ``metadata`` are those of the field (see
    :func:`field_metadata`), which may give the discriminator. A
    discriminator in ``annotation`` takes precedence. The alternatives are
    in the order of the union, without their ``Annotated`` metadata.

    Raises ``ValueError`` for a union that breaks a rule of
    :mod:`ess.spec._workflow_spec`: a member that is not a group; no
    discriminator, or one that is not the name of a field, such as a
    callable ``Discriminator``; a tag field with an alias or not typed a
    ``Literal`` of one string; an alternative with a data value (see
    :func:`unwritten`).
    """
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
        if (data := unwritten(member)) is not None:
            held_type = field_types.type_name(member.model_fields[data].annotation)
            raise ValueError(
                f'{member.__name__}.{data}, of {held_type}: no request writes it, '
                'since an alternative of a union is written inline, with its tag, '
                'and no reference fits a union; give the field a type that the '
                f'params schema has an input for, or make {member.__name__} a '
                'parameter of its own, given by reference'
            )
        groups[tags[0]] = member  # pydantic refuses two alternatives of one tag
    return discriminator, groups


def _tags(annotation: Any) -> tuple[Any, ...]:
    """The values of ``annotation`` if it is a ``Literal``, else none."""
    return get_args(annotation) if get_origin(annotation) is Literal else ()


@functools.cache
def unwritten(group: type[BaseModel]) -> str | None:
    """
    The name of the first data value of ``group``, or ``None`` if it has
    none. A group with a data value is given by reference to an output only.
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
    of a field may still dump it to something else, which :func:`held`
    checks.
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


def written_inline(tp: Any) -> bool:
    """
    Whether a request may write a parameter or row of type ``tp`` as a JSON
    object of the fields of ``tp``: whether ``tp`` is a group without a data
    value (see :func:`unwritten`). A file model is not, since a request
    writes a file as ``file(...)``.
    """
    return (
        field_types.is_group(tp)
        and not field_types.is_file_model(tp)
        and unwritten(tp) is None
    )


def takes_output(tp: Any) -> bool:
    """
    Whether a reference to an output may fill a parameter or row of type
    ``tp``: whether ``tp`` is a group, not a file model, without a field
    typed a file model, since no output holds a file model.
    """
    return (
        field_types.is_group(tp)
        and not field_types.is_file_model(tp)
        and all(
            field_types.file_model(f.annotation) is None
            for f in tp.model_fields.values()
        )
    )


def ref_types(
    model: type[BaseModel], params: Mapping[str, Any]
) -> list[tuple[Loc, OutputRef | AccumulatorRef, Any]]:
    """
    Each reference to an output in ``params``, with its position and the
    type its data must fit: that of the parameter or its rows, without
    ``| None``. ``params`` are by name, as :func:`held` returns them, so a
    reference to an output fills a whole parameter or row. Files are left
    out.
    """
    return [
        (loc, ref, field_types.value_type(model.model_fields[str(loc[0])].annotation))
        for loc, ref in iter_refs(params)
        if not isinstance(ref, FileRef)
    ]
