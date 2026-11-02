# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Root places: where a request writes either a value or a reference, by type.

A request is a JSON object of parameter values. A *root place* is a position
in it where the request may write either a value or a reference to an output
(see :mod:`ess.spec.refs`): the value of a parameter, or an element of a list
parameter. A group written inline holds scalars, file models, and lists of
these, which are not root places: a reference to an output fills a whole
parameter or a whole row. A file is a value, written as the reference to the
file, at a root place or at a field typed a file model::

    {
        'sample': file(run=101),                     # sample
        'centre': finder.ref('centre'),              # centre
        'reduction': {'bins': 50, 'banks': [1, 2]},  # reduction
        'runs': [file(run=102), file(run=103)],      # runs, runs[0], runs[1]
        'specimen': {'run': file(run=104), 'thickness': 2.0},  # specimen
    }

The type at a root place decides what the request writes there, with
``| None`` removed: a root place of type ``X | None`` also takes ``None``.
``X`` is a group or a union of groups (see :mod:`ess.spec._workflow_spec`):

========================================  =====================================
Type at the root place                    The request writes
========================================  =====================================
a file model                              a reference to a file
a group                                   an object of its fields, unless one
                                          is a data value, with a reference to
                                          a file at a field typed a file
                                          model; or a reference to an output
a union of groups                         an object of the fields of one
                                          alternative, with its tag, as for a
                                          group; never a reference
a list, ``list[X]``, at a parameter       a list, each element a root place
                                          of type ``X``
========================================  =====================================

A *scalar* is an ``int``, ``float``, ``str``, ``bool``, ``datetime`` (or
pydantic's ``AwareDatetime`` or ``NaiveDatetime``), an enum whose values are
strings, numbers, or bools, or a ``Literal`` of plain strings, numbers, or
bools, which a request writes inline, as JSON. A field of a group is
*interpreted* if it is a scalar, a file model, or a list of one, possibly
with constraints, ``Annotated[float, Gt(0)]``: the params schema of a spec
has an input for it, the JSON schema of its type, or a reference to a file.
A scalar is never typed ``X | None``, a file model may be, and a list and
its elements never are. Any other field is a *data value*, such as a
scipp array, ``float | None``, a tuple, or a group, which no request writes,
so a group with a data value is given by reference to an output only (see
:func:`unwritten`). A group is validated whole, once, when the request
arrives, so it holds only what is known then: values typed in, and files,
named by their identity. A value that another request computes may not exist
yet; it fills a whole parameter. Of the fields of a group with a data value,
the framework refuses only a ``Path``, a file model, a ``NewType``, or a type
alias, since no output could fill them.

A reference to an output stands for a whole group. A reference to a file
fits a file model whose metadata the file has (see
:class:`~ess.spec.parameters.FileModel`). A reference to an output fits
the root place's type, and no output holds a file model, so that every
file comes from the file source, which checks it. A number, a string, or a
``sc.DataArray`` names no role: every output of its type would fit, so a
value computed by another workflow comes as a group, such as a found beam
centre, and a data value is a field of one::

    class ComputedTransmission(BaseModel, arbitrary_types_allowed=True):
        transmission: sc.DataArray  # a data value: given by reference only

``"@ref"`` is reserved at every depth, so that a reference is found
without the spec (see :func:`ess.spec.refs.positions`). A reference to an
output inside a group written inline is refused.

A *union of groups* is a pydantic discriminated union,
``Annotated[A | B, Field(discriminator='kind')]``, in which each
alternative is a group with the *tag* field ``kind`` typed as a ``Literal``
of one string, its own, such as ``Literal['find']``. The tag is written
inline, never as a reference: it says which alternative's fields the object
has. A union that holds a group holds groups
only, since a value of another type has no tag.

A union is written inline only, and no reference fits it, so that an
output need not be of a tagged class to feed a root place: the producer of a
group need not know its consumers. So a request writes each alternative
inline: no field of it is a data value (see :func:`unwritten`). A parameter
that takes either a value typed in or one from a record is a group,
possibly typed ``X | None``, not a union::

    beam_centre: BeamCentre = BeamCentre()  # typed in, or finder.ref('centre')
    beam_centre: Annotated[GivenCentre | FindCentre, Field(discriminator='kind')]
                                            # typed in, or found in this graph

The params schema of a spec and a form that a UI makes from the models
both follow the table above so: :func:`root_type` gives the type at the root
places of a parameter, and each way to write it has a predicate of its own.
A group is written inline unless it holds a data value
(:func:`written_inline`), and by reference if an output can fit it
(:func:`takes_output`); a union is written inline as one of its
alternatives (:func:`alternatives`). For the two ``beam_centre`` above::

    tp, none, metadata = root_type(Params, 'beam_centre')
    written_inline(tp)  # True for BeamCentre
    takes_output(tp)  # True for BeamCentre
    field_types.is_file_model(tp)  # False: a reference to a file
    alternatives(tp, *metadata)
    # None for BeamCentre; for the union:
    # ('kind', {'given': GivenCentre, 'find': FindCentre})

In the request phase, :func:`held` validates each group, row, or file model
given inline whole, from its JSON form, so that its validators run and each
file is resolved, and the record holds the JSON form of the validated value,
which must read back as it (see :func:`ess.spec.field_types.inline`). An
instance of the group and the object of its fields give one record. In the
call phase, :func:`validated` replaces each reference to an output by its
data, and makes each value of what the record holds.

An error names its *place*, its position in the params, such as
``reduction.bins``; inside a union, without the tag:
``beam_centre.tolerance``.
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
What resolves a file in the request phase: given the name of a file and the
file model at its place, it returns the file's identity, or raises
``ValueError`` (see :class:`~ess.spec.parameters.FileModel`).
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
    ``value`` of the field ``name`` of ``model``, a parameter, as a record
    holds it: the JSON form of a value written inline or of a reference to
    an output, or ``None``; of a list, each element so. Whether the data of
    a reference to an output fit its place is for the caller to check when
    the request is submitted (see :func:`ref_types`). ``value`` is as a
    request gives it, plain data or Python objects, such as an instance of a
    group, a file model, or a :data:`~ess.spec.refs.Ref`, which are turned
    into plain data first.

    A group, a row, or a file model written inline is validated whole, with
    each file resolved by ``files`` (see
    :class:`~ess.spec.parameters.FileModel`), and held as the JSON form of
    the validated value, a field left out at its default (see
    :func:`ess.spec.field_types.inline`). A file that ``files`` did not
    resolve, such as one that a validator of a group makes of a name, is
    refused, since the file source has not checked it, and so is a reference
    to an output in the JSON form of a value written inline, such as one a
    serializer of a field makes, since a reference to an output fills a
    whole parameter or a whole row. Raises
    :class:`~ess.spec.refs.ParamsError` at the place of the first problem.
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
    Why the record cannot hold ``ref``, at a root place if ``at_root``, or
    ``None``; ``resolved`` holds the identities of the files resolved.
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
    """``value``, plain data, of the parameter ``name`` as :func:`held` has it."""
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
    ``value`` of the field ``name`` of ``model``, validated by its field (see
    :func:`~ess.spec.field_types.model_of`): each row of a list field by its
    element type.

    With ``read`` ``None``, ``value`` is as a binding takes it, data. Else it
    is as a record holds it: the JSON form of a value written inline, and a
    reference to an output at a parameter or an element of a list
    parameter. Each reference to an output is replaced by the data ``read``
    gives for it, and the value made of the result. A file is a value: the
    file model made of the reference to its file gets its local path from
    ``paths``, by identity (see :class:`~ess.spec.parameters.FileModel`).
    A value written inline was validated when the request was submitted, and
    its JSON form reads back as it, so this fails only for the data of a
    reference to an output. It is validated in Python mode with
    ``strict=False``: a strict model takes an enum from its string only from
    JSON, and the request phase has already validated each value given
    inline from its JSON form, with the model's strictness (see
    :func:`~ess.spec.field_types.inline`). Raises
    :class:`~ess.spec.refs.ParamsError` at the place of each problem, with
    the position inside the value that pydantic gives.
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
    union = slot.union
    tags = {} if union is None else union[1]

    def place(inner: Loc) -> Loc:
        return (*loc, *(inner[1:] if inner and inner[0] in tags else inner))

    return ParamsError((place(e['loc'][1:]), e['msg']) for e in error.errors())


def _inline(
    loc: Loc, slot: _Slot, value: Any, context: dict[str, Any] | None = None
) -> Any:
    """
    The JSON form of ``value`` written inline at ``loc``, validated with
    ``context`` (see :func:`~ess.spec.field_types.inline`).
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
    The type at the root places of the parameter ``name`` of ``model``, the
    params model of a spec: at the parameter, or at each element of a list
    parameter. It is given without ``| None`` and ``Annotated``, with whether
    it is typed ``X | None``, and with its metadata: that of the field, with
    its discriminator, then of each ``Annotated`` in the type, the outer
    first. The metadata of the field of a list parameter, such as its length,
    is that of the list, so an element has only that of its type::

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
    The tag and the alternatives, by the value of their tag, of the union of
    groups that ``annotation``, possibly typed ``X | None``, is; ``None`` if
    it is not a union with a group in it, such as ``int | str``. ``metadata``
    is that of the field (see :func:`field_metadata`), which may give the
    discriminator, such as ``Field(discriminator='kind')``; one in the
    annotation takes precedence over it. An alternative
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
    The first field of ``group`` that a request does not write, or ``None``
    if there is none: a data value, a field that is not interpreted. A group
    with such a field is given by reference to an output only.
    """
    return next(
        (n for n, f in group.model_fields.items() if not _interpreted(f.annotation)),
        None,
    )


def _interpreted(annotation: Any) -> bool:
    """
    Whether a field typed ``annotation`` is interpreted: a scalar, a file
    model, possibly typed ``X | None``, or a list of one, never typed
    ``list[X] | None`` or ``list[X | None]``.
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
    a number, a string, or a bool; a ``Literal`` of plain values of these,
    not of enum members, which a request writes as their values; or an enum
    whose values are these. A serializer in the metadata of a field may
    still give it another JSON form, which :func:`held` checks.
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
    Whether a request writes a root place of type ``tp`` as an object of the
    fields of ``tp``: a group, not a file model, which a request gives by
    reference to a file, with no data value (see :func:`unwritten`).
    """
    return (
        field_types.is_group(tp)
        and not field_types.is_file_model(tp)
        and unwritten(tp) is None
    )


def takes_output(tp: Any) -> bool:
    """
    Whether a reference to an output fits a root place of type ``tp``: a
    group, not a file model, which a request gives by reference to a file,
    with no field typed a file model, since no output holds one.
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
    Each reference to an output in ``params``, by name, as a record holds
    them, with its place, a parameter or an element of a list parameter, and
    the type at the place, with ``| None`` removed: the type of the data it
    must fit. ``params`` are as :func:`held` makes them, so a reference to
    an output is only at a root place. A file is a value, resolved when the
    request is submitted, so a reference to a file is left out.
    """
    return [
        (loc, ref, field_types.value_type(model.model_fields[str(loc[0])].annotation))
        for loc, ref in iter_refs(params)
        if not isinstance(ref, FileRef)
    ]
