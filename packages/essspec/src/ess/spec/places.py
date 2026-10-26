# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Places: where a request writes either a value or a reference, by type.

A request is a JSON object of parameter values. A *place* is a position in
it where the request may write either a value or a reference to data that
exist elsewhere (see :mod:`ess.spec.refs`). The places are the value of each
parameter, each element of a list, and each field of a group, at any depth::

    {
        'sample': {                                    # sample
            'file': dataset(run=101),                  # sample.file
            'transmission': t.ref('transmission'),     # sample.transmission
        },
        'reduction': {'bins': 50, 'mask': m.ref('mask')},  # reduction, .bins, .mask
        'runs': [{'file': dataset(run=102)}],          # runs, runs[0], runs[0].file
    }

The type at a place decides what the request writes there, with ``| None``
removed (a place of type ``X | None`` also takes ``None``), but for a list:
``list[X] | None`` is written whole, as a dict is:

========================================  =====================================
Type at the place                         The request writes
========================================  =====================================
a file: ``Path``                          a reference to a dataset
a group                                   an object of its fields, each a
                                          place, unless one is a data value;
                                          or a reference to an output
a union of groups                         an object of the fields of one
                                          alternative, with its tag, as for a
                                          group; never a reference
a list, ``list[X]``                       a list, each element a place of
                                          type ``X``
``list[X] | None``, ``dict[str, X]``      the value as JSON, whole (*inline*)
any other type with a JSON form           the value as JSON (*inline*)
any other type, a *data value*            nothing: it is a field of a group
========================================  =====================================

A reference stands for a group or a file only. A reference to an output
fits the place's type, and the output's type holds no file, so that a file
comes only from a dataset, which the dataset source checks. A number, a
string, or a ``sc.DataArray`` names no role: every output of its type would
fit, so a value computed by another workflow comes as a group, such as a
found beam centre, and a data value is a field of one::

    class ComputedTransmission(BaseModel, arbitrary_types_allowed=True):
        transmission: sc.DataArray  # a data value: given by reference only

No field of a group that holds a file is a data value: no reference stands
for such a group, so a request writes it field by field (see
:func:`unwritten`).

The contents of a tuple, a set, a dict, a list that may be ``None``, a
``TypedDict``, or a dataclass are not places: such a value is written
whole, inline, and a reference stands neither for it nor for a part of
it. ``"@ref"`` is reserved at every depth, so that a reference is found
without the spec (see :func:`ess.spec.refs.positions`), and a reference at
a position that is not a place is refused.

A *union of groups* is a pydantic discriminated union,
``Annotated[A | B, Field(discriminator='kind')]``, in which each
alternative is a group with the *tag* field ``kind`` typed as a ``Literal``
of one string, its own, such as ``Literal['find']``. The tag is written
inline, never as a reference: it says which alternative's fields the object
has, and so what each of them takes. A union that holds a group holds groups
only, since a value of another type has no tag.

A union is written inline only, and no reference fits it, so that an
output need not be of a tagged class to feed a place: the producer of a
group need not know its consumers. So a request writes each alternative
inline: no field of it is a data value (see :func:`unwritten`). A place
that takes either a value typed in or one from a record is a group,
possibly typed ``X | None``, not a union::

    beam_centre: BeamCentre = BeamCentre()  # typed in, or finder.ref('centre')
    beam_centre: Annotated[GivenCentre | FindCentre, Field(discriminator='kind')]
                                            # typed in, or found in this graph

:func:`place` describes what a request writes at a place, as the table
above has it, for the params schema of a spec and for a form that a UI
makes from the models: a :class:`File`, a :class:`Value`, or the
:class:`Ways` of a group or a union; of a list whose elements are places,
the :class:`Rows`. For the two ``beam_centre`` above::

    Ways(tag=None, inline={None: BeamCentre}, record=BeamCentre, none=False, ...)
    Ways(tag='kind', inline={'given': GivenCentre, 'find': FindCentre},
         record=None, none=False, ...)

A group with no reference inside, at any depth, is an inline value. In the
request phase, :func:`held` validates it whole, from its JSON form, so that
its validators run, and the record holds the JSON form of the validated
group, which must read back as it (see :func:`ess.spec.field_types.inline`).
An instance of the group and the object of its fields give one record. A
group with a reference inside is checked field by field: each field given
inline alone, by its own type and constraints, and each group in it as a
place of its own. Its validators run in the call phase, when each
parameter's value is validated once, whole, with the data of its references
(see :func:`validated`). So a validator that widens what a field of a group
accepts, such as a ``mode='before'`` validator that parses a string, works
only for a group written without references, and a value that only a
validator of a group refuses fails the record, or the push, in the call
phase if the group has a reference inside::

    {'range': {'start': 10.0, 'stop': 5.0}}         # refused at range.stop
    {'range': {'start': 10.0, 'stop': r.ref('x')}}  # with x 5.0, fails at range.stop

An error inside a union names its place without the tag:
``beam_centre.tolerance``, whichever phase finds it.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import PurePath
from types import NoneType, UnionType
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, Discriminator, Field, ValidationError
from pydantic.fields import FieldInfo
from pydantic_core import PydanticSerializationError

from .field_types import (
    element,
    has_json_form,
    holds_file,
    inline,
    is_file,
    is_group,
    is_list,
    model_of,
    optional,
    plain,
    type_name,
)
from .refs import Loc, ParamsError, Ref, iter_refs, map_refs, positions, ref_at

type OnRef = Callable[[Loc, Ref, Any], Any]
"""
What :func:`held` does with a reference at a place: given the place, the
reference, and the type at the place, it returns what the record holds, or
raises.
"""


@dataclass(frozen=True)
class _Slot:
    """
    The type at a place: that of the field ``name`` of ``model``, or of an
    element of it, a list, if ``element``.
    """

    model: type[BaseModel]
    name: str
    element: bool = False

    @property
    def annotation(self) -> Any:
        tp = self.model.model_fields[self.name].annotation
        return get_args(tp)[0] if self.element else tp

    @property
    def is_list(self) -> bool:
        """Whether the place holds a list whose elements are places."""
        return not self.element and is_list(self.model, self.name)

    @property
    def discriminator(self) -> Any:
        """The discriminator of the field, if it is not in the annotation."""
        info = self.model.model_fields[self.name]
        return None if self.element else info.discriminator

    @property
    def one(self) -> _Slot:
        """The type at one place of the field: an element, if it is a list."""
        return _Slot(self.model, self.name, element=self.is_list or self.element)


def held(model: type[BaseModel], name: str, value: Any, on_ref: OnRef) -> Any:
    """
    ``value`` of the field ``name`` of ``model``, a parameter or a field of
    a group, as a record holds it: the JSON form of each place given inline,
    and at each other place what ``on_ref`` returns for its reference, or
    ``None``. A group holds each of its fields, a field left out its default.
    ``value`` is as a request gives it, a group as a mapping of its fields or
    an instance, a reference as a :data:`~ess.spec.refs.Ref` or its JSON form.

    A group with no reference inside is given inline: validated whole, and
    held as the JSON form of the validated group. A group with a reference
    inside holds each of its fields so, each place given inline validated
    alone, by its field (see :func:`ess.spec.field_types.inline`). Raises
    :class:`~ess.spec.refs.ParamsError` at the place of the first problem.
    """
    return _held((name,), _Slot(model, name), value, on_ref)


def _held(loc: Loc, slot: _Slot, value: Any, on_ref: OnRef) -> Any:
    tp, nullable = optional(slot.annotation)
    if (ref := ref_at(loc, value)) is not None:
        if element(tp) is not None:
            whole = 'list' if get_origin(tp) is list else 'dict'
            raise _refused(
                loc,
                f'{ref} is a reference to a whole {whole}; a reference never '
                'stands for a whole list or dict',
            )
        if alternatives(slot.annotation, slot.discriminator) is not None:
            raise _refused(
                loc,
                f'{ref}: a union of groups is written inline, with the tag of an '
                'alternative; a reference stands for a group or a file only',
            )
        if not (is_file(tp) or is_group(tp)):
            raise _refused(
                loc,
                f'{ref}: {type_name(tp)} is given inline; a reference stands for '
                'a group or a file only',
            )
        return on_ref(loc, ref, tp)
    if value is None and nullable:
        return None
    if slot.is_list:
        if not isinstance(value, list):
            raise _refused(loc, f'{value!r} is not a list')
        return [_held((*loc, i), slot.one, v, on_ref) for i, v in enumerate(value)]
    if is_group(tp) or alternatives(slot.annotation, slot.discriminator):
        fields = dict(value) if isinstance(value, BaseModel) else value
        if not isinstance(fields, Mapping):
            raise _refused(
                loc,
                f'{value!r} is not an object; a group is written as an object '
                'of its fields',
            )
        group = _group(loc, slot, fields)
        _written(loc, group)
        if any(ref_at(at, v) is not None for at, v in positions(loc, fields)):
            return _fields(loc, group, fields, on_ref)
        _written_inside(loc, group, fields)
        return _inline(loc, slot, value)
    if is_file(tp):
        raise _refused(
            loc, f'{value!r} is not a dataset; a file is given as dataset(path=...)'
        )
    for at, item in positions(loc, value):
        if (inner := ref_at(at, item)) is not None:
            raise _refused(
                at,
                f'{inner} is inside a value of {type_name(tp)}, given inline, '
                'whole: the contents of a tuple, a dict, or a list that may be '
                'None are not places',
            )
    return _inline(loc, slot, value)


def _written(loc: Loc, group: type[BaseModel]) -> None:
    """Refuse ``group`` written at ``loc`` if it is given by reference only."""
    if (data := unwritten(group)) is not None:
        held_type = type_name(optional(group.model_fields[data].annotation)[0])
        raise _refused(
            loc,
            f'{type_name(group)} is given by reference: no request writes its '
            f'field {data}, of {held_type}',
        )


def _written_inside(loc: Loc, group: type[BaseModel], value: Mapping[str, Any]) -> None:
    """
    Refuse a group given by reference only at a place inside ``value``, the
    fields of ``group`` given inline, at any depth. Nothing is validated, so
    that a validator of ``group`` that widens what a field accepts still
    works; a value that is not what its place takes is left to validating
    ``group`` whole.
    """
    for name, given in value.items():
        if name not in group.model_fields or not (groups := groups_at(group, name)):
            continue
        (inner,) = groups  # a field of a group is never a union
        if not is_list(group, name):
            places = [((*loc, name), given)]
        elif isinstance(given, list):
            places = [((*loc, name, i), v) for i, v in enumerate(given)]
        else:
            continue
        for at, item in places:
            fields = dict(item) if isinstance(item, BaseModel) else item
            if isinstance(fields, Mapping):
                _written(at, inner)
                _written_inside(at, inner, fields)


def _fields(
    loc: Loc, group: type[BaseModel], value: Mapping[str, Any], on_ref: OnRef
) -> dict[str, Any]:
    """The fields of ``group`` at ``loc`` as a record holds them (see :func:`held`)."""
    if unknown := [f for f in value if f not in group.model_fields]:
        raise ParamsError(
            ((*loc, f), 'Extra inputs are not permitted') for f in unknown
        )
    fields: dict[str, Any] = {}
    for name, info in group.model_fields.items():
        at = (*loc, name)
        if name in value:
            given = value[name]
        elif info.is_required():
            raise _refused(at, 'Field required')
        else:
            given = info.get_default(call_default_factory=True)
        fields[name] = _held(at, _Slot(group, name), given, on_ref)
    return fields


def _group(loc: Loc, slot: _Slot, value: Mapping[str, Any]) -> type[BaseModel] | None:
    """
    The group whose fields ``value`` at ``loc`` gives, of a union the
    alternative its tag names; ``None`` if the type at the place is neither
    a group nor a union of groups.
    """
    tp, _ = optional(slot.annotation)
    if is_group(tp):
        return tp
    if (union := alternatives(slot.annotation, slot.discriminator)) is None:
        return None
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
    read: Callable[[Ref], Any] | None = None,
) -> Any:
    """
    ``value`` of the field ``name`` of ``model``, validated once, whole (see
    :func:`~ess.spec.field_types.model_of`): each row of a list field by its
    element type.

    With ``read`` ``None``, ``value`` is as a binding takes it, data. Else it
    is as a record holds it: the JSON form of each place given inline, and a
    reference at each other place. Each reference, at any depth, is replaced
    by the data ``read`` gives for it, and the result validated whole, so
    that every validator of every group in it runs once, with all its fields
    at hand. It is validated in Python mode with ``strict=False``: a strict
    model takes an enum from its string only from JSON, and the request phase
    has already validated each place given inline from its JSON form, with
    the model's strictness (see :func:`~ess.spec.field_types.inline`).
    Raises :class:`~ess.spec.refs.ParamsError` at the place of each problem.
    """
    one = model_of(model, name)
    if read is not None:
        value = map_refs({name: value}, read)[name]
    strict = False if read is not None else None

    def check(loc: Loc, slot: _Slot, v: Any) -> Any:
        try:
            return getattr(one.model_validate({name: v}, strict=strict), name)
        except ValidationError as error:
            raise _at(loc, slot, error) from None

    slot = _Slot(model, name)
    if slot.is_list:
        return [check((name, i), slot.one, v) for i, v in enumerate(value)]
    return check((name,), slot, value)


def _at(loc: Loc, slot: _Slot, error: ValidationError) -> ParamsError:
    """
    ``error`` of a value at ``loc`` of the type ``slot``, validated by a model
    of its field, at the place of each problem. pydantic puts the tag of a
    union after the union's place, ``('beam_centre', 'find', 'tolerance')``,
    which is left out, ``beam_centre.tolerance``, so that the place is the
    same whichever way the alternative is chosen.
    """
    return ParamsError(
        (_place(loc, slot, e['loc'][1:]), e['msg']) for e in error.errors()
    )


def _place(loc: Loc, slot: _Slot, inner: Loc) -> Loc:
    """
    The place ``inner`` inside a value at ``loc`` of the type ``slot``, as
    pydantic gives it, without the tag after each union place. A position
    that is not a place, such as an element of a tuple, is kept as it is.
    """
    rest = list(inner)
    while rest:
        if slot.is_list and isinstance(rest[0], int):
            slot, loc = slot.one, (*loc, rest.pop(0))
            continue
        tp, _ = optional(slot.annotation)
        if is_group(tp):
            group = tp
        elif (
            union := alternatives(slot.annotation, slot.discriminator)
        ) is not None and rest[0] in union[1]:
            group = union[1][rest.pop(0)]
        else:
            break
        if not rest or rest[0] not in group.model_fields:
            break
        slot, loc = _Slot(group, str(rest[0])), (*loc, rest.pop(0))
    return (*loc, *rest)


def _inline(loc: Loc, slot: _Slot, value: Any) -> Any:
    """The JSON form of ``value`` given inline at ``loc`` (see :func:`inline`)."""
    try:
        return inline(slot.model, slot.name, value, whole=not slot.element)
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


@dataclass(frozen=True)
class File:
    """A file: a request writes a reference to a dataset, or ``None`` if ``none``."""

    type: type[PurePath]
    none: bool  # typed X | None


@dataclass(frozen=True)
class Value:
    """
    A value of any type but a file, a group, or a union of groups: a request
    writes it inline, whole, as JSON, or ``None`` if ``none``. A data value
    has no JSON form, and no request writes it: a group that holds one is
    given by reference only (see :func:`unwritten`).
    """

    type: Any  # without | None and Annotated metadata
    metadata: tuple[Any, ...]  # of the field, then of the Annotated in its type
    none: bool  # typed X | None


@dataclass(frozen=True)
class Ways:
    """
    A group, a union of groups, or either typed ``X | None``: the ways a
    request writes it. ``inline`` holds the groups that a request writes as
    an object of their fields, each a place: the alternatives of a union by
    the value of its tag field ``tag``; a group by ``None``, unless it holds
    a data value. ``record`` is the type that an output must fit to be given
    by reference; ``None`` for a union and for a group that holds a file,
    which no reference fits.
    """

    tag: str | None
    inline: dict[str | None, type[BaseModel]]
    record: type[BaseModel] | None
    none: bool  # typed X | None
    metadata: tuple[Any, ...]  # of the field, then of the Annotated in its type


type Place = File | Value | Ways


@dataclass(frozen=True)
class Rows:
    """
    A list whose elements are places, each as ``element`` describes it, and
    ``metadata``, the constraints of the list, such as its length.
    """

    element: Place
    metadata: tuple[Any, ...]


def place(model: type[BaseModel], name: str) -> Place | Rows:
    """
    What a request writes at the field ``name`` of ``model``, a parameter or
    a field of a group (see the table in :mod:`ess.spec.places`); of a list
    whose elements are places, :class:`Rows`. Raises ``ValueError`` as
    :func:`alternatives` does.
    """
    info = model.model_fields[name]
    metadata = tuple(info.metadata)
    if info.discriminator is not None:
        metadata += (Field(discriminator=info.discriminator),)
    if is_list(model, name):
        return Rows(_described(get_args(info.annotation)[0], ()), metadata)
    return _described(info.annotation, metadata)


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


def _described(annotation: Any, metadata: tuple[Any, ...]) -> Place:
    """The description of a place of type ``annotation`` with ``metadata``."""
    tp, inner, none = _unwrapped(annotation)
    metadata = (*metadata, *inner)
    if element(tp) is None:
        if is_file(tp):
            return File(tp, none)
        annotated = Annotated[tp, *metadata] if metadata else tp
        if (union := alternatives(annotated)) is not None:
            tag, groups = union
            return Ways(tag, groups, None, none, metadata)
        if is_group(tp):
            inline = {} if unwritten(tp) is not None else {None: tp}
            return Ways(None, inline, None if holds_file(tp) else tp, none, metadata)
    return Value(tp, metadata, none)


def _unwrapped(annotation: Any) -> tuple[Any, tuple[Any, ...], bool]:
    """
    ``annotation`` without its outer ``Annotated`` and ``| None``, at any
    depth; the metadata of each ``Annotated``, the outer first; and whether
    it had ``| None``. Unlike :func:`~ess.spec.field_types.optional`, it
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
            return tp, metadata, none


def alternatives(
    annotation: Any, discriminator: Any = None
) -> tuple[str, dict[str, Any]] | None:
    """
    The tag and the alternatives, by the value of their tag, of the union of
    groups that ``annotation``, possibly typed ``X | None``, is; ``None`` if
    it is not a union with a group in it, such as ``int | str``. ``discriminator`` is
    that of the field, if the annotation does not carry it. An alternative
    is as declared, without ``Annotated`` metadata. The alternatives are in
    the order of the union.

    Raises ``ValueError`` for a union that the framework cannot take apart
    by the tag of a value: one with a type that is not a group, without a
    discriminator, or with one that is not the name of a field, such as a
    callable ``Discriminator``; one whose tag field has an alias or is not a
    ``Literal`` of one string, such as ``Literal['find']``; one with an
    alternative that a request cannot write inline (see :func:`unwritten`),
    since no reference fits a union.
    """
    tp, metadata, _ = _unwrapped(annotation)
    for meta in metadata:
        if isinstance(meta, FieldInfo) and meta.discriminator is not None:
            discriminator = meta.discriminator
        elif isinstance(meta, Discriminator):
            discriminator = meta
    if get_origin(tp) not in (Union, UnionType):
        return None
    members = [plain(arg) for arg in get_args(tp)]
    if not any(is_group(m) for m in members):
        return None
    if not all(is_group(m) for m in members):
        raise ValueError('a union holds groups only, or no group')
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
            raise ValueError(
                f'{member.__name__}.{data}: no request writes it, since an '
                'alternative of a union is written inline, with its tag, and no '
                'reference fits a union'
            )
        groups[tags[0]] = member  # pydantic refuses two alternatives of one tag
    return discriminator, groups


def _tags(annotation: Any) -> tuple[Any, ...]:
    """The values of ``annotation`` if it is a ``Literal``, else none."""
    return get_args(annotation) if get_origin(annotation) is Literal else ()


def groups_at(model: type[BaseModel], name: str) -> list[type[BaseModel]]:
    """
    The groups at the places of the field ``name`` of ``model``: the group,
    or the alternatives of the union, that it or each element of it is, if
    any. Raises ``ValueError`` as :func:`alternatives` does.
    """
    slot = _Slot(model, name).one
    tp, _ = optional(slot.annotation)
    if is_group(tp):
        return [tp]
    union = alternatives(slot.annotation, slot.discriminator)
    return [] if union is None else list(union[1].values())


@functools.cache
def unwritten(
    group: type[BaseModel], seen: frozenset[type[BaseModel]] = frozenset()
) -> str | None:
    """
    The first field of ``group`` at which a request writes nothing, ``None``
    aside, or ``None`` if there is none. Such a field is a data value, which
    has no JSON form and takes no reference, or a group that holds a file
    and such a field. A group with such a field is given by reference only.
    ``seen`` holds the groups looked into already, so that a group at a
    place inside itself ends.
    """
    seen = seen | {group}
    for name in group.model_fields:
        tp, _ = optional(_Slot(group, name).one.annotation)
        if is_file(tp):
            continue
        if groups := groups_at(group, name):
            (inner,) = groups  # a field of a group is never a union
            if (
                holds_file(inner)
                and inner not in seen
                and unwritten(inner, seen) is not None
            ):
                return name
        elif not has_json_form(group, name):
            return name
    return None


def file_off_place(model: type[BaseModel], name: str) -> bool:
    """
    Whether the field ``name`` of ``model`` holds a file at a position that
    is not a place, such as in a tuple or a dict, where no dataset can give
    it. The fields of a group at a place are checked as the group's own.
    """
    tp, _ = optional(_Slot(model, name).one.annotation)
    if is_file(tp) or groups_at(model, name):
        return False
    return holds_file(tp)


def ref_types(
    model: type[BaseModel], params: Mapping[str, Any]
) -> list[tuple[Loc, Ref, Any]]:
    """
    Each reference in ``params``, by name, as a record holds them, with its
    place and the type at the place, with ``| None`` removed: the type of the
    data it must fit.
    """
    found = []
    for loc, ref in iter_refs(params):
        name, *keys = loc
        slot, value = _Slot(model, str(name)), params[str(name)]
        for i, key in enumerate(keys):
            if isinstance(key, int):
                slot = slot.one
            else:
                if (group := _group(loc[: i + 1], slot, value)) is None:
                    raise _refused(loc, f'{ref} is not at a place')
                slot = _Slot(group, key)
            value = value[key]
        found.append((loc, ref, optional(slot.annotation)[0]))
    return found
