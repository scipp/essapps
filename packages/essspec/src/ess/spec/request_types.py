# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The request type of a spec: what a request may write in its params, as a type.

A params model is typed for the call phase: each field holds the value that
the binding takes, a group, a file model with its local path, or data. A
request writes something else in the same field: a group inline, a file by
name, or a reference to an output. This module derives, from the params
model, the type of what a request writes, the *request type*, so that the
rules of :mod:`ess.spec._workflow_spec` on what a request writes are stated
once, as types, and so that one type both validates a request and is the
JSON schema of the params. The request type is the params type with each
``Referenced[T]`` replaced by a reference to an output whose data fit
``T``, at any depth::

    params model (call phase)              request type (request phase)
    -------------------------              ----------------------------
    reduction: Reduction                   Reduction
    mask: Referenced[PixelMask]            OutputRef | AccumulatorRef
    centre: BeamCentre                     BeamCentre | OutputRef
            | Referenced[BeamCentre]                  | AccumulatorRef
    beam_centre: GivenCentre | Find...     GivenCentre | FindCentre  (by tag)
    sample: SampleRun                      SampleRun                 (@ref file)
    runs: list[RunFile]                    list[RunFile]
    samples: list[Sample]                  list[Sample'], with Sample' derived
    # Sample.transmission:                   transmission: OutputRef
    #     Referenced[Transmission]                         | AccumulatorRef

The request type of a field is the union of the *sources* that the field
takes (see :func:`sources`), tagged by :func:`source_of`: a group, a union
of groups, or a scalar written ``inline``, a ``file``, or a reference to an
``output``. The source of a value is known from the value alone, by its key
``"@ref"``. A group that holds no ``Referenced[...]``, at any depth, is the
group's own class, with its validators, so validating a request validates
it whole. A group that holds one is a model derived from it without its
validators (see :func:`group_type`): they run when the request runs, on the
data read (see :func:`ess.spec.places.validated`). A file is the file
model's own class, which a backend validates with a resolver of files (see
:class:`~ess.spec.parameters.FileModel`).

The backend validates each parameter of a request with :func:`held`, which
it gives a resolver of files, :data:`Files`. It returns what the record
holds, the JSON form of the request type: each group and file validated,
each reference as given. It also returns each reference to an output with
the type its data must fit, :data:`Reads`, which the backend checks against
history. The call phase validates the params model itself from the record
(see :func:`ess.spec.places.validated`).
"""

from __future__ import annotations

import dataclasses
import functools
from collections.abc import Callable, Mapping
from typing import Annotated, Any, Final, Union, get_args, get_origin

from pydantic import (
    BaseModel,
    BeforeValidator,
    Discriminator,
    Field,
    Tag,
    TypeAdapter,
    ValidationError,
    create_model,
)
from pydantic.config import JsonDict
from pydantic.fields import FieldInfo
from pydantic_core import (
    ErrorDetails,
    PydanticCustomError,
    PydanticSerializationError,
)

from . import field_types, places
from .parameters import REFERENCED, FileModel
from .refs import (
    AccumulatorRef,
    FileRef,
    Loc,
    OutputRef,
    ParamsError,
    Ref,
    as_ref,
    iter_refs,
    positions,
    ref_at,
)

INLINE: Final = 'inline'
"""A group, or a union of groups, written inline as an object of its fields."""
FILE: Final = 'file'
"""A file, an object with ``"@ref": "file"``."""
OUTPUT: Final = 'output'
"""A reference to an output, of a record or of an accumulator."""


def source_of(value: Any) -> str | None:
    """
    The source of ``value``, as a request writes it at a place or as a Python
    object: :data:`FILE` or :data:`OUTPUT` for an object with the key
    ``"@ref"`` of that kind, ``None`` for one of no kind, and :data:`INLINE`
    for any other value.
    """
    if isinstance(value, FileModel | FileRef):
        return FILE
    if isinstance(value, OutputRef | AccumulatorRef):
        return OUTPUT
    if not isinstance(value, Mapping) or '@ref' not in value:
        return INLINE
    return {'file': FILE, 'output': OUTPUT, 'accumulator': OUTPUT}.get(
        value['@ref'] if isinstance(value['@ref'], str) else ''
    )


def sources(tp: Any, metadata: tuple[Any, ...] = ()) -> dict[str, Any]:
    """
    The sources that a field of type ``tp`` takes, by tag, each the type
    that validates it, with the constraints of the field (see
    :func:`~ess.spec.places.unwrapped` for ``tp`` and ``metadata``). Of the
    part of ``tp`` that a request writes (see :func:`~ess.spec.places.split`):

    - :data:`INLINE`: the union of groups, with its discriminator, the
      group, or the scalar, each group as :func:`group_type` gives it;
    - :data:`FILE`: the file model.

    Of the groups that ``Referenced[...]`` marks:

    - :data:`OUTPUT`: a reference to an output, with the name of the group,
      or of the union of the groups, as its ``dataType`` in the JSON schema,
      and the groups in its metadata (see :func:`held`).

    In the JSON schema, each alternative of a union requires its tag, since a
    request always writes it, and is titled by its tag (see
    :func:`~ess.spec.places.title`). A form panel shows a widget for each
    source a field takes.
    """
    taken: dict[str, Any] = {}
    inline, inline_metadata, referenced = places.split(tp, *metadata)
    union = places.alternatives(inline, *inline_metadata)
    if union is not None:
        tag, groups = union
        members = tuple(_alternative(group, tag, v) for v, group in groups.items())
        alternatives = Union[members]  # noqa: UP007
        taken[INLINE] = Annotated[
            alternatives, *inline_metadata, Field(discriminator=tag)
        ]
    elif field_types.is_file_model(inline):
        taken[FILE] = Annotated[inline, *inline_metadata] if inline_metadata else inline
    elif inline is not None:
        written = group_type(inline) if field_types.is_group(inline) else inline
        taken[INLINE] = (
            Annotated[written, *inline_metadata] if inline_metadata else written
        )
    if referenced:
        data: Any = Union[referenced]  # noqa: UP007
        data_type: JsonDict = {'dataType': field_types.type_name(data)}
        taken[OUTPUT] = Annotated[
            OutputRef | AccumulatorRef,
            Field(discriminator='kind', json_schema_extra=data_type),
            BeforeValidator(_output),
            _Fits(data),
        ]
    return taken


def _output(value: Any) -> Any:
    if source_of(value) != OUTPUT:
        raise PydanticCustomError(
            'output_ref',
            'is not a reference to an output, as the field is typed Referenced[...]',
        )
    return value


@dataclasses.dataclass(frozen=True)
class _Fits:
    """
    The metadata of a reference to an output whose data must fit ``data``,
    the ``T`` of ``Referenced[T]``, or the union of these (see :func:`held`).
    """

    data: Any


def _alternative(group: type[BaseModel], tag: str, value: str) -> Any:
    """
    The alternative ``group`` of a union, whose tag ``tag`` is ``value``: in
    the JSON schema, the tag is required and titles the alternative. pydantic
    would leave out a tag with a default from ``required``, and title the
    alternative by the class; it requires the tag when it validates.
    """
    extra: JsonDict = {
        'title': places.title(value, group.model_fields[tag]),
        'required': [tag],
    }
    return Annotated[group_type(group), Field(json_schema_extra=extra)]


_SOURCES: Final = {
    INLINE: 'a value written inline',
    FILE: 'a file',
    OUTPUT: 'a reference to an output',
}


def tagged(taken: Mapping[str, Any], none: bool) -> Any:
    """
    The type of a field that takes the sources ``taken`` (see :func:`sources`):
    the source, if one, else their union tagged by :func:`source_of`; ``| None``
    if ``none``.
    """
    if len(taken) == 1:
        (source,) = taken.values()
    else:
        members = tuple(Annotated[tp, Tag(tag)] for tag, tp in taken.items())
        takes = ' or '.join(_SOURCES[tag] for tag in taken)
        by_source = Discriminator(
            source_of,
            custom_error_type='source',
            custom_error_message=f'the field takes {takes}',
        )
        source = Annotated[Union[members], by_source]  # noqa: UP007
    return Union[source, None] if none else source  # noqa: UP007


def type_of(annotation: Any, metadata: tuple[Any, ...] = ()) -> Any:
    """
    The request type of a field typed ``annotation``, with ``metadata`` (see
    :func:`~ess.spec.places.field_metadata`): a list of the request type of
    its element, with the list's constraints, or the :func:`tagged` union of
    its :func:`sources`.
    """
    tp, none, metadata = places.unwrapped(annotation, metadata)
    if REFERENCED not in metadata and get_origin(tp) is list:
        listed: Any = list[type_of(get_args(tp)[0])]  # type: ignore[misc]
        made = Annotated[listed, *metadata] if metadata else listed
        return Union[made, None] if none else made  # noqa: UP007
    return tagged(sources(tp, metadata), none)


def holds_ref(annotation: Any) -> bool:
    """
    Whether a field typed ``annotation`` holds ``Referenced[...]``, at any
    depth, but inside its ``T``. A spec refuses a group that contains
    itself, so this ends.
    """
    if places.is_referenced(annotation):
        return True
    if field_types.is_group(annotation):
        return _group_holds_ref(annotation)
    return any(holds_ref(arg) for arg in get_args(annotation))


@functools.cache
def _group_holds_ref(group: type[BaseModel]) -> bool:
    return any(
        holds_ref(info.rebuild_annotation()) for info in group.model_fields.values()
    )


@functools.cache
def group_type(group: type[BaseModel]) -> type[BaseModel]:
    """
    The request type of ``group``: the group itself, if it holds no
    ``Referenced[...]``, so that validating a request validates it whole.
    Otherwise a model derived from it, of the group's name, docstring, and
    config, with each field that holds one typed its request type, and its
    metadata, default, title, and description kept. It has no validators of
    the group, which run when the request runs, on the data read (see
    :func:`~ess.spec.places.validated`).
    """
    if not _group_holds_ref(group):
        return group
    fields: dict[str, Any] = {
        name: _request_field(name, info)
        if holds_ref(info.rebuild_annotation())
        # A field that holds no reference keeps its type and field info.
        else (info.annotation, info)
        for name, info in group.model_fields.items()
    }
    return create_model(
        group.__name__,
        __config__=group.model_config,
        __doc__=group.__doc__,
        __module__=group.__module__,
        **fields,
    )


def _request_field(name: str, info: FieldInfo) -> tuple[Any, FieldInfo]:
    """
    The field ``name`` with ``info``, typed its request type, with its title,
    description, and default.
    """
    tp = type_of(info.annotation, places.field_metadata(info))
    # Undefined, and so required, if the field is.
    default = info.get_default(call_default_factory=True)
    field = Field(
        default=default, title=places.title(name, info), description=info.description
    )
    return tp, field


@functools.cache
def request_model(model: type[BaseModel]) -> type[BaseModel]:
    """
    The request type of the params model ``model``: a model of the same
    fields, each typed its :func:`type_of`, with its title, description, and
    default. Its JSON schema is the params schema of a spec (see
    :meth:`ess.spec.WorkflowSpec.serialize`).
    """
    fields: dict[str, Any] = {
        name: _request_field(name, info) for name, info in model.model_fields.items()
    }
    return create_model(model.__name__, __config__=model.model_config, **fields)


@functools.cache
def _one(model: type[BaseModel], name: str, whole: bool) -> type[BaseModel]:
    """
    A model of the field ``name`` of ``model`` alone, typed its request type,
    with the config of ``model``; of one row of a list parameter, unless
    ``whole``.
    """
    info = model.model_fields[name]
    field: Any
    if field_types.is_list(model, name) and not whole:
        field = (type_of(get_args(info.annotation)[0]), ...)
    else:
        field = _request_field(name, info)
    return create_model(model.__name__, __config__=model.model_config, **{name: field})


type Files = Callable[[str, type[FileModel]], str]
"""
Resolves a file in the request phase. It takes the file as the request names
it, such as ``'run:101'``, and the file model of the field, and returns the
file's identity, or raises ``ValueError`` (see
:class:`~ess.spec.parameters.FileModel`).
"""

type Reads = list[tuple[Loc, OutputRef | AccumulatorRef, Any]]
"""
The references to outputs in a value, each with its position and the type
that the output's data must fit: the ``T`` of ``Referenced[T]``, or the
union of these.
"""


def held(
    model: type[BaseModel], name: str, value: Any, files: Files, whole: bool = False
) -> tuple[Any, Reads]:
    """
    The value of the field ``name`` of ``model`` as the record stores it, in
    the request phase, and the references to outputs in it.

    ``value`` is what the request gives: plain data, or Python objects such
    as an instance of a group or a :data:`~ess.spec.refs.Ref`, which are
    dumped to plain data first. For a list parameter, each row is handled
    on its own, unless ``whole``, as for a field of a group. Each is
    validated from its JSON form by its request type, with each file
    resolved by ``files``, and the JSON form of the validated value is
    returned (see :func:`ess.spec.field_types.inline`): a group, a file, or
    a reference to an output as given. The type that the data of each
    reference must fit is read from the request type of the field that
    holds it, in the validated value, so the backend checks the reference
    against history without validating again.

    Refused, besides what the request type refuses: a file that ``files``
    did not resolve, such as one that a validator of a group makes from a
    name, since the file source has not checked it; and a reference to an
    output in the JSON form that the request did not give, such as one that
    a serializer makes. Raises :class:`~ess.spec.refs.ParamsError` at the
    position of each problem (see :func:`_located`).
    """
    resolved: set[str] = set()

    def resolve(file: str, model: type[FileModel]) -> str:
        identity = files(file, model)
        resolved.add(identity)
        return identity

    context = {'files': resolve}
    given = _ANY.dump_python(value)
    written = {loc for loc, _ in iter_refs({name: given})}
    one = _one(model, name, whole)
    if whole or not field_types.is_list(model, name):
        made, stored = _inline(one, name, (name,), given, context)
    elif (ref := ref_at((name,), given)) is not None:
        raise _refused((name,), _whole_list(ref))
    elif not isinstance(given, list):
        raise _refused((name,), f'{given!r} is not a list')
    else:
        rows = [_inline(one, name, (name, i), v, context) for i, v in enumerate(given)]
        made, stored = [m for m, _ in rows], [s for _, s in rows]
    refs = list(iter_refs({name: stored}))
    if problems := [
        (loc, problem)
        for loc, ref in refs
        if (problem := _stored_ref_problem(ref, loc in written, resolved))
    ]:
        raise ParamsError(problems)
    info = one.model_fields[name]
    reads = [
        (loc, ref, _fits(info, made, loc[1:]))
        for loc, ref in refs
        if isinstance(ref, OutputRef | AccumulatorRef)
    ]
    return stored, reads


def _stored_ref_problem(ref: Ref, written: bool, resolved: set[str]) -> str | None:
    """
    Why the record cannot store ``ref``, or ``None``. ``written`` says whether
    the request gave a reference at its position. ``resolved`` holds the
    identities of the files resolved.
    """
    if isinstance(ref, FileRef):
        if ref.file in resolved:
            return None
        return (
            f'file {ref} was not resolved: a validator made it, so the file '
            'source has not checked it'
        )
    if written:
        return None
    return (
        f'{ref}: the JSON form of a value written inline holds a reference to an '
        'output that the request did not give'
    )


def _fits(info: FieldInfo, value: Any, at: Loc) -> Any:
    """
    The type that the data of the reference to an output at ``at`` in
    ``value``, a validated value of the field ``info``, must fit: read from
    the metadata of the request type of the field that holds it.
    """
    for part in at:
        if isinstance(part, str):
            info = type(value).model_fields[part]
            value = getattr(value, part)
        else:
            value = value[part]
    return _fits_in(info.rebuild_annotation())


def _fits_in(tp: Any) -> Any:
    """The data of the one :class:`_Fits` in ``tp``, at any depth, or ``None``."""
    found = [m.data for m in getattr(tp, '__metadata__', ()) if isinstance(m, _Fits)]
    found += [data for arg in get_args(tp) if (data := _fits_in(arg)) is not None]
    return found[0] if found else None


_ANY = TypeAdapter(Any)
"""Dumps Python objects to plain data, keeping a number that is not finite."""


def _inline(
    one: type[BaseModel], name: str, loc: Loc, value: Any, context: dict[str, Any]
) -> tuple[Any, Any]:
    """
    ``value``, plain data at ``loc``, validated by ``one``, the request type
    of the field ``name`` alone, with ``context``, and its JSON form (see
    :func:`~ess.spec.field_types.inline`). A value without a JSON form is
    validated as it is, since it may be one that the field does not take.
    """
    try:
        return field_types.inline(one, name, value, context=context)
    except PydanticSerializationError as error:
        why = f'cannot be stored as JSON: {error}'
    except ValidationError as error:
        raise ParamsError(_located(loc, value, error.errors())) from None
    except ParamsError as error:
        raise ParamsError(((*loc, *at), msg) for at, msg in error.errors) from None
    except ValueError as error:
        raise _refused(loc, str(error)) from None
    try:
        one.model_validate({name: value})
    except ValidationError as error:
        raise ParamsError(_located(loc, value, error.errors())) from None
    raise _refused(loc, why)


def _located(loc: Loc, value: Any, errors: list[ErrorDetails]) -> list[tuple[Loc, str]]:
    """
    The ``errors`` of validating ``value``, given at ``loc``, each at its
    position in ``value`` (see :func:`_position`), once. The errors at a
    reference or inside it are one error at the reference, since the field
    does not take its source, unless each is an error of a file that the
    field takes: of the file source, or of a validator. At a list, the
    reference is one to a whole list.
    """
    found = [(_position(value, e), e['msg'], e['type']) for e in errors]
    for at, item in positions((), value):
        inside = {kind for p, _, kind in found if p[: len(at)] == at}
        if (ref := as_ref(item)) is None or inside <= _TAKEN:
            continue
        found = [f for f in found if f[0][: len(at)] != at]
        why = _whole_list(ref) if 'list_type' in inside else _wrong_source(ref)
        found.append((at, why, ''))
    return list(dict.fromkeys(((*loc, *at), msg) for at, msg, _ in found))


_TAKEN: Final = frozenset({'file', 'value_error', 'assertion_error'})


def _position(value: Any, error: ErrorDetails) -> Loc:
    """
    The position of ``error`` in ``value``, as given. Of the error's
    location, each element that steps into the value is kept, a key of an
    object there or an index of a list there, and the field that a missing
    field names. The others are tags of unions, of a source or of a group,
    which a position in the value never holds: ``('centre', 'inline',
    'find', 'tolerance')`` is ``centre.tolerance``.
    """
    at: list[str | int] = []
    for part in error['loc'][1:]:
        lists = isinstance(value, list)
        keys = range(len(value)) if lists else value if isinstance(value, dict) else ()
        if part in keys:
            at.append(part)
            value = value[part]
    if error['type'] == 'missing':
        at.append(error['loc'][-1])
    return tuple(at)


def _wrong_source(ref: Ref) -> str:
    if isinstance(ref, FileRef):
        return f'file {ref}: the field is not a file model'
    return f'{ref} is a reference to an output; the field is not typed Referenced[...]'


def _whole_list(ref: Any) -> str:
    return (
        f'{ref} is a reference to a whole list; a reference stands for one '
        'element of a list'
    )


def _refused(loc: Loc, msg: str) -> ParamsError:
    return ParamsError([(loc, msg)])
