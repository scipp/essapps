# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Places: where a request writes either a value or a reference, by type.

A request is a JSON object of parameter values. A *place* is a position in
it where the request may write either a value or a reference to data that
exist elsewhere (see :mod:`ess.spec.refs`): the value of a parameter, or an
element of a list parameter. A group written inline holds only scalars, so
there is no place inside it::

    {
        'sample': dataset(run=101),                    # sample
        'centre': finder.ref('centre'),                # centre
        'reduction': {'bins': 50, 'banks': [1, 2]},    # reduction
        'runs': [dataset(run=102), dataset(run=103)],  # runs, runs[0], runs[1]
    }

The type at a place decides what the request writes there, with ``| None``
removed: a place of type ``X | None`` also takes ``None``. ``X`` is a group
or a union of groups (see :mod:`ess.spec._workflow_spec`):

========================================  =====================================
Type at the place                         The request writes
========================================  =====================================
a dataset model                           a reference to a dataset
a group                                   an object of its fields, unless one
                                          is a data value; or a reference to
                                          an output
a union of groups                         an object of the fields of one
                                          alternative, with its tag, as for a
                                          group; never a reference
a list, ``list[X]``, at a parameter       a list, each element a place of
                                          type ``X``
========================================  =====================================

A field of a group is a *scalar*, ``int``, ``float``, ``str``, ``bool``,
``datetime``, an enum, or a ``Literal``, or a list of scalars, which a
request writes inline, as JSON; or a *data value*, a type without a JSON
form, which no request writes: a group that holds one is given by reference
only (see :func:`unwritten`), to an output. So its other fields may be of
any type that an output may hold: anything but a ``Path``, a dataset model,
a ``NewType``, or a type alias, at any depth, since a file comes only from a
dataset, and a spec names a role by a class.

A reference stands for a whole group. A reference to a dataset fits a
dataset model (see :class:`~ess.spec.parameters.DatasetModel`). A reference
to an output fits the place's type, and no output holds a dataset model, so
that a file comes only from a dataset, which the dataset source checks. A
number, a string, or a ``sc.DataArray`` names no role: every output of its
type would fit, so a value computed by another workflow comes as a group,
such as a found beam centre, and a data value is a field of one::

    class ComputedTransmission(BaseModel, arbitrary_types_allowed=True):
        transmission: sc.DataArray  # a data value: given by reference only

``"@ref"`` is reserved at every depth, so that a reference is found
without the spec (see :func:`ess.spec.refs.positions`), and one inside a
group written inline is refused.

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
makes from the models: a :class:`Ways` of a group, a dataset model among
them, or a union; of a list parameter, the :class:`Rows`; of a field of a
group, a :class:`Value`. For the two ``beam_centre`` above::

    Ways(tag=None, inline={None: BeamCentre}, record=BeamCentre, none=False, ...)
    Ways(tag='kind', inline={'given': GivenCentre, 'find': FindCentre},
         record=None, none=False, ...)

In the request phase, :func:`held` validates each group, or row, given
inline whole, from its JSON form, so that its validators run, and the record
holds the JSON form of the validated group, which must read back as it (see
:func:`ess.spec.field_types.inline`). An instance of the group and the
object of its fields give one record. In the call phase, :func:`validated`
replaces each reference by its data, and makes each value of what the record
holds.

An error inside a union names its place without the tag:
``beam_centre.tolerance``.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import NoneType, UnionType
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, Discriminator, Field, ValidationError
from pydantic.fields import FieldInfo
from pydantic_core import PydanticSerializationError

from . import field_types
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
    def discriminator(self) -> Any:
        """The discriminator of the field, if it is not in the annotation."""
        info = self.model.model_fields[self.name]
        return None if self.element else info.discriminator


def held(model: type[BaseModel], name: str, value: Any, on_ref: OnRef) -> Any:
    """
    ``value`` of the field ``name`` of ``model``, a parameter, as a record
    holds it: the JSON form of a value given inline, at a reference what
    ``on_ref`` returns for it, or ``None``; of a list, each element so.
    ``value`` is as a request gives it, a group as a mapping of its fields or
    an instance, a reference as a :data:`~ess.spec.refs.Ref` or its JSON
    form.

    A group, or a row, given inline is validated whole, and held as the JSON
    form of the validated group, a field left out at its default (see
    :func:`ess.spec.field_types.inline`). Raises
    :class:`~ess.spec.refs.ParamsError` at the place of the first problem.
    """
    if not field_types.is_list(model, name):
        return _held((name,), _Slot(model, name), value, on_ref)
    if (ref := ref_at((name,), value)) is not None:
        raise _refused(
            (name,),
            f'{ref} is a reference to a whole list; a reference stands for one '
            'element of a list',
        )
    if not isinstance(value, list):
        raise _refused((name,), f'{value!r} is not a list')
    slot = _Slot(model, name, element=True)
    return [_held((name, i), slot, v, on_ref) for i, v in enumerate(value)]


def _held(loc: Loc, slot: _Slot, value: Any, on_ref: OnRef) -> Any:
    tp, nullable = field_types.optional(slot.annotation)
    union = alternatives(slot.annotation, slot.discriminator)
    if (ref := ref_at(loc, value)) is not None:
        if union is not None:
            raise _refused(
                loc,
                f'{ref}: a union of groups is written inline, with the tag of an '
                'alternative; no reference fits it',
            )
        return on_ref(loc, ref, tp)
    if value is None and nullable:
        return None
    if field_types.is_dataset(tp):
        raise _refused(
            loc,
            f'{value!r} is not a dataset; {field_types.type_name(tp)} is given by '
            'reference to a dataset, such as dataset(path=...)',
        )
    if union is not None or field_types.is_group(tp):
        fields = dict(value) if isinstance(value, BaseModel) else value
        if not isinstance(fields, Mapping):
            raise _refused(
                loc,
                f'{value!r} is not an object; a group is written as an object '
                'of its fields',
            )
        _written(loc, _group(loc, slot, fields))
        for at, item in positions(loc, fields):
            if at != loc and (ref := ref_at(at, item)) is not None:
                raise _refused(
                    at,
                    f'{ref}: a reference stands for a whole group, at a parameter '
                    'or an element of a list parameter, never inside a group',
                )
    return _inline(loc, slot, value)


def _written(loc: Loc, group: type[BaseModel]) -> None:
    """Refuse ``group`` written at ``loc`` if it is given by reference only."""
    if (data := unwritten(group)) is not None:
        held_type = field_types.type_name(
            field_types.optional(group.model_fields[data].annotation)[0]
        )
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
    if (union := alternatives(slot.annotation, slot.discriminator)) is None:
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
    read: Callable[[Ref], Any] | None = None,
) -> Any:
    """
    ``value`` of the field ``name`` of ``model``, validated by its field (see
    :func:`~ess.spec.field_types.model_of`): each row of a list field by its
    element type.

    With ``read`` ``None``, ``value`` is as a binding takes it, data. Else it
    is as a record holds it: the JSON form of a value given inline, and a
    reference at a parameter or an element of a list parameter. Each
    reference is replaced by the data ``read`` gives for it, and the value
    made of the result. A value given inline was validated when the request
    was submitted, and its JSON form reads back as it, so this fails only
    for the data of a reference, such as a dataset model whose validator
    refuses the local path. It is validated in Python mode with
    ``strict=False``: a strict model takes an enum from its string only from
    JSON, and the request phase has already validated each value given
    inline from its JSON form, with the model's strictness (see
    :func:`~ess.spec.field_types.inline`). Raises
    :class:`~ess.spec.refs.ParamsError` at the place of each problem, with
    the position inside the value that pydantic gives.
    """
    one = field_types.model_of(model, name)
    if read is not None:
        value = map_refs({name: value}, read)[name]
    strict = False if read is not None else None

    def check(loc: Loc, v: Any) -> Any:
        try:
            return getattr(one.model_validate({name: v}, strict=strict), name)
        except ValidationError as error:
            raise ParamsError(
                ((*loc, *e['loc'][1:]), e['msg']) for e in error.errors()
            ) from None

    if field_types.is_list(model, name):
        return [check((name, i), v) for i, v in enumerate(value)]
    return check((name,), value)


def field_value(group: type[BaseModel], name: str, value: Any) -> Any:
    """
    ``value`` of the field ``name`` of ``group``, a field of a group, given
    inline, as validating the group makes it: validated whole, a list with
    its length constraints, and its JSON form checked to read back as it (see
    :func:`~ess.spec.field_types.inline`). A spec checks the default of a
    field so. Raises :class:`~ess.spec.refs.ParamsError` at ``name``.
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
    union = alternatives(slot.annotation, slot.discriminator)
    tags = {} if union is None else union[1]

    def place(inner: Loc) -> Loc:
        return (*loc, *(inner[1:] if inner and inner[0] in tags else inner))

    return ParamsError((place(e['loc'][1:]), e['msg']) for e in error.errors())


def _inline(loc: Loc, slot: _Slot, value: Any) -> Any:
    """
    The JSON form of ``value`` given inline at ``loc`` (see
    :func:`~ess.spec.field_types.inline`).
    """
    try:
        return field_types.inline(slot.model, slot.name, value, whole=not slot.element)
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
class Value:
    """
    A field of a group: a scalar, which a request writes inline, as JSON, or
    a data value, which has no JSON form, and no request writes: a group that
    holds one is given by reference only (see :func:`unwritten`).
    """

    type: Any  # without Annotated metadata
    metadata: tuple[Any, ...]  # of the field, then of the Annotated in its type


@dataclass(frozen=True)
class Ways:
    """
    A group, a union of groups, or either typed ``X | None``: the ways a
    request writes it. ``inline`` holds the groups that a request writes as
    an object of their fields: the alternatives of a union by the value of
    its tag field ``tag``; a group by ``None``, unless it holds a data value.
    ``record`` is the type that the data of a reference must fit: an
    output's type, or for a dataset model, which a request gives by
    reference to a dataset only, the model; ``None`` for a union, which no
    reference fits.
    """

    tag: str | None
    inline: dict[str | None, type[BaseModel]]
    record: type[BaseModel] | None
    none: bool  # typed X | None
    metadata: tuple[Any, ...]  # of the field, then of the Annotated in its type


type Place = Value | Ways


@dataclass(frozen=True)
class Rows:
    """
    A list whose elements are places, or of a field of a group, scalars, each
    as ``element`` describes it, and ``metadata``, the constraints of the
    list, such as its length.
    """

    element: Place
    metadata: tuple[Any, ...]


def place(model: type[BaseModel], name: str) -> Place | Rows:
    """
    What a request writes at the field ``name`` of ``model``, a parameter or
    a field of a group (see the table in :mod:`ess.spec.places`); of a list,
    :class:`Rows`. Raises ``ValueError`` as :func:`alternatives` does.
    """
    info = model.model_fields[name]
    metadata = tuple(info.metadata)
    if info.discriminator is not None:
        metadata += (Field(discriminator=info.discriminator),)
    if field_types.is_list(model, name):
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
    if field_types.element(tp) is None:
        annotated = Annotated[tp, *metadata] if metadata else tp
        if (union := alternatives(annotated)) is not None:
            tag, groups = union
            return Ways(tag, groups, None, none, metadata)
        if field_types.is_group(tp):
            inline = {} if unwritten(tp) is not None else {None: tp}
            return Ways(None, inline, tp, none, metadata)
    return Value(tp, metadata)


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
    members = [field_types.plain(arg) for arg in get_args(tp)]
    if not any(field_types.is_group(m) for m in members):
        return None
    if not all(field_types.is_group(m) for m in members):
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
    slot = _Slot(model, name, element=field_types.is_list(model, name))
    tp, _ = field_types.optional(slot.annotation)
    if field_types.is_group(tp):
        return [tp]
    union = alternatives(slot.annotation, slot.discriminator)
    return [] if union is None else list(union[1].values())


@functools.cache
def unwritten(group: type[BaseModel]) -> str | None:
    """
    The first field of ``group`` that a request does not write, or ``None``
    if there is none: a data value, a field without a JSON form; of a dataset
    model, its ``path``, since a request gives a dataset model by reference
    to a dataset only. A group with such a field is given by reference only.
    """
    if field_types.is_dataset(group):
        return 'path'
    return next(
        (n for n in group.model_fields if not field_types.has_json_form(group, n)),
        None,
    )


def ref_types(
    model: type[BaseModel], params: Mapping[str, Any]
) -> list[tuple[Loc, Ref, Any]]:
    """
    Each reference in ``params``, by name, as a record holds them, with its
    place, a parameter or an element of a list parameter, and the type at the
    place, with ``| None`` removed: the type of the data it must fit.
    """
    return [
        (loc, ref, field_types.value_type(model.model_fields[str(loc[0])].annotation))
        for loc, ref in iter_refs(params)
    ]
