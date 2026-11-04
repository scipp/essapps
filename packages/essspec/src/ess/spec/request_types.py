# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The request type of a spec: what a request may write in its params, as a type.

A params model is typed for the call phase: each field holds the value that
the binding takes, a group, the local path of a file, or data. A request
writes something else in the same field: a group inline, a file by name, or
a reference to an output. This module derives, from the params model, the
type of what a request writes, the *request type*, so that the rules of
:mod:`ess.spec._workflow_spec` on what a request writes are stated once, as
types, and so that one type both validates a request and is the JSON schema
of the params. The request type is the params type with each
``Referenced[T]`` replaced by a reference to an output whose data fit
``T``, and each file field by a file, at any depth::

    params model (call phase)              request type (request phase)
    -------------------------              ----------------------------
    reduction: Reduction                   Reduction
    mask: Referenced[PixelMask]            OutputRef | AccumulatorRef
    centre: BeamCentre                     BeamCentre | OutputRef
            | Referenced[BeamCentre]                  | AccumulatorRef
    beam_centre: GivenCentre | Find...     GivenCentre | FindCentre  (by tag)
    sample: RunFile                        FileRef
    runs: list[RunFile]                    list[FileRef]
    samples: list[Sample]                  list[Sample'], with Sample' derived
    # Sample.run: RunFile                    run: FileRef
    # Sample.transmission:                   transmission: OutputRef
    #     Referenced[Transmission]                         | AccumulatorRef

with ``RunFile = Annotated[Path, File()]``, a file field (see
:class:`~ess.spec.parameters.File`).

The request type of a field is the union of the *sources* that the field
takes (see :func:`sources`), tagged by :func:`source_of`: a group, a union
of groups, or a scalar written ``inline``, a ``file``, or a reference to an
``output``. The source of a value is known from the value alone, by its key
``"@ref"``. A group that holds neither ``Referenced[...]`` nor a file field,
at any depth, is the group's own class, with its validators. A group that
holds one is a model derived from it without its validators (see
:func:`group_type`): they run when the params model validates the group (see
:func:`validated`). A file is a :class:`~ess.spec.refs.FileRef`, which a
backend validates with a resolver of files, :data:`Files`, given the
field's :class:`~ess.spec.parameters.File` marker.

The backend validates each parameter of a request with :func:`held`, which
it gives a resolver of files, :data:`Files`, and the local path of a file,
:data:`LocalPath`. It returns what the record holds, the JSON form of the
request type, and each reference to an output with the type its data must
fit, :data:`Reads`, which the backend checks against history. The call
phase validates the params model itself from the record (see
:func:`validated`).

A request is validated one parameter at a time, in two phases, and a spec
checks its defaults the same way:

=============  =================================  ==========================
When           Function                           What it does
=============  =================================  ==========================
spec made      :func:`validated`                  validates the default of
                                                  a field as a binding
                                                  takes it
request phase  :func:`held`                       validates a parameter by
                                                  its request type, and
                                                  returns what the record
                                                  stores: the JSON form of
                                                  each group and file, and
                                                  each reference to an
                                                  output as it is; validates
                                                  a parameter that holds a
                                                  file field and no
                                                  ``Referenced[...]`` by the
                                                  params model too, with
                                                  the context ``paths``
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
the data of each reference to an output. :func:`validated` validates by the
params model, not by the request type, so this module holds the request
type and the validation of a value in each phase.

An error is a :class:`~ess.spec.refs.ParamsError` that names the position of
each problem in the params, such as ``reduction.bins`` or ``runs[1]``.
"""

from __future__ import annotations

import dataclasses
import functools
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from types import UnionType
from typing import Annotated, Any, Final, Union, get_args, get_origin

from annotated_types import MaxLen, MinLen
from pydantic import (
    BaseModel,
    BeforeValidator,
    Discriminator,
    Field,
    GetCoreSchemaHandler,
    Tag,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    create_model,
)
from pydantic.config import JsonDict
from pydantic.fields import FieldInfo
from pydantic.json_schema import GetJsonSchemaHandler, JsonSchemaValue
from pydantic_core import (
    CoreSchema,
    ErrorDetails,
    PydanticCustomError,
    PydanticSerializationError,
    core_schema,
    from_json,
    to_json,
)

from . import field_types
from .parameters import REFERENCED, DataRefused, File, not_its_source
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
)

INLINE: Final = 'inline'
"""A group, or a union of groups, written inline as an object of its fields."""
FILE: Final = 'file'
"""A file, an object with ``"@ref": "file"``."""
OUTPUT: Final = 'output'
"""A reference to an output, of a record or of an accumulator."""


def source_of(value: Any) -> str | None:
    """
    The source of ``value``, as a request writes it at a field or as a Python
    object: :data:`FILE` or :data:`OUTPUT` for an object with the key
    ``"@ref"`` of that kind, ``None`` for one of no kind, and :data:`INLINE`
    for any other value.
    """
    if isinstance(value, FileRef):
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
    :func:`~ess.spec.field_types.unwrapped` for ``tp`` and ``metadata``). Of the
    part of ``tp`` that a request writes (see :func:`~ess.spec.field_types.split`):

    - :data:`INLINE`: the union of groups, with its discriminator, the
      group, or the scalar, each group as :func:`group_type` gives it;
    - :data:`FILE`: a :class:`~ess.spec.refs.FileRef`, resolved by the
      resolver of files given the field's marker (see :data:`Files`), with
      the marker's metadata under ``metadata`` in the JSON schema, which say
      which files fit. The other metadata of the field are of its ``Path``,
      so the file has none of them.

    Of the groups that ``Referenced[...]`` marks:

    - :data:`OUTPUT`: a reference to an output, with the name of the group,
      or of the union of the groups, as its ``dataType`` in the JSON schema,
      and the groups in its metadata (see :func:`held`).

    In the JSON schema, each alternative of a union requires its tag, since a
    request always writes it, and is titled by its tag (see
    :func:`~ess.spec.field_types.title`). A form panel shows a widget for each
    source a field takes.
    """
    taken: dict[str, Any] = {}
    inline, inline_metadata, referenced = field_types.split(tp, *metadata)
    union = field_types.alternatives(inline, *inline_metadata)
    if union is not None:
        tag, groups = union
        members = tuple(_alternative(group, tag, v) for v, group in groups.items())
        alternatives = Union[members]  # noqa: UP007
        taken[INLINE] = Annotated[
            alternatives, *inline_metadata, Field(discriminator=tag)
        ]
    elif (marker := field_types.file_marker(inline, *inline_metadata)) is not None:
        taken[FILE] = Annotated[FileRef, _Resolved(marker)]
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
            BeforeValidator(functools.partial(_only, OUTPUT)),
            _Fits(data),
        ]
    return taken


@dataclasses.dataclass(frozen=True)
class _Resolved:
    """
    The metadata of the request type of a file field with ``marker``: it
    takes a file only, resolved to the file's identity by the context
    ``files``, if given (see :data:`Files`), which it adds to the context's
    set ``resolved``, and its JSON schema holds the marker's metadata under
    ``metadata``.
    """

    marker: File

    def __get_pydantic_core_schema__(
        self, source: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_before_validator_function(
            functools.partial(_only, FILE),
            core_schema.with_info_after_validator_function(
                self._resolved, handler(source)
            ),
        )

    def __get_pydantic_json_schema__(
        self, schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return {**handler(schema), 'metadata': self.marker.metadata}

    def _resolved(self, ref: FileRef, info: ValidationInfo) -> FileRef:
        context = info.context or {}
        if (resolve := context.get('files')) is None:
            return ref
        try:
            identity = resolve(ref.file, self.marker)
        except ValueError as error:
            raise PydanticCustomError('file', str(error)) from None
        context['resolved'].add(identity)
        return FileRef(file=identity)


def _only(source: str, value: Any) -> Any:
    """``value``, if of ``source``, :data:`FILE` or :data:`OUTPUT`."""
    if source_of(value) != source:
        raise not_its_source(value, file=source == FILE)
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
        'title': field_types.title(value, group.model_fields[tag]),
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
    :func:`~ess.spec.field_types.field_metadata`): a list of the request type of
    its element, with the list's constraints, or the :func:`tagged` union of
    its :func:`sources`. Of a list of elements that hold ``Referenced[...]``
    or a file field, it keeps the length constraints only: the others, such
    as a validator, check the data or the paths, and run when the params
    model validates the list (see :func:`held`).
    """
    tp, none, metadata = field_types.unwrapped(annotation, metadata)
    if REFERENCED not in metadata and get_origin(tp) is list:
        (element,) = get_args(tp)
        if field_types.holds_marker(element):
            metadata = tuple(m for m in metadata if isinstance(m, MinLen | MaxLen))
        listed: Any = list[type_of(element)]  # type: ignore[misc]
        made = Annotated[listed, *metadata] if metadata else listed
        return Union[made, None] if none else made  # noqa: UP007
    return tagged(sources(tp, metadata), none)


@functools.cache
def group_type(group: type[BaseModel]) -> type[BaseModel]:
    """
    The request type of ``group``: the group itself, if it holds neither
    ``Referenced[...]`` nor a file field (see
    :func:`~ess.spec.field_types.holds_marker`), so that validating a request
    validates it whole. Otherwise a model derived from it, of the group's
    name, qualified name, docstring, and config, with each field that holds
    one typed its request type, and its metadata, default, title, and
    description kept. It has no validators of the group, which run when the
    params model validates the group (see :func:`held`).
    """
    if not field_types.holds_marker(group):
        return group
    fields: dict[str, Any] = {
        name: _request_field(name, info)
        if field_types.holds_marker(info.rebuild_annotation())
        # A field that holds no marker keeps its type and field info.
        else (info.annotation, info)
        for name, info in group.model_fields.items()
    }
    return create_model(
        group.__name__,
        __config__=group.model_config,
        __doc__=group.__doc__,
        __module__=group.__module__,
        __qualname__=group.__qualname__,
        **fields,
    )


def _request_field(name: str, info: FieldInfo) -> tuple[Any, FieldInfo]:
    """
    The field ``name`` with ``info``, typed its request type, with its title,
    description, examples, extra JSON schema, and default.
    """
    tp = type_of(info.annotation, field_types.field_metadata(info))
    # Undefined, and so required, if the field is.
    default = info.get_default(call_default_factory=True)
    field = Field(
        default=default,
        title=field_types.title(name, info),
        description=info.description,
        examples=info.examples,
        json_schema_extra=info.json_schema_extra,
    )
    return tp, field


@functools.cache
def request_model(model: type[BaseModel]) -> type[BaseModel]:
    """
    The request type of the params model ``model``: a model of the same
    docstring and fields, each typed its :func:`type_of`, with its field
    info as :func:`_request_field` keeps it. Its JSON schema is the params
    schema of a spec (see :meth:`ess.spec.WorkflowSpec.serialize`).
    """
    fields: dict[str, Any] = {
        name: _request_field(name, info) for name, info in model.model_fields.items()
    }
    return create_model(
        model.__name__, __config__=model.model_config, __doc__=model.__doc__, **fields
    )


@functools.cache
def _one(model: type[BaseModel], name: str, table: bool) -> type[BaseModel]:
    """
    A model of the field ``name`` of ``model`` alone, typed its request type,
    with the config of ``model``; without the length constraints of a list
    if it is a ``table`` (see :func:`model_of`). It is made from the one
    field, so that a problem of another field does not hide that of this one.
    """
    info = model.model_fields[name]
    field: Any = (
        (type_of(info.annotation), ...) if table else _request_field(name, info)
    )
    return create_model(model.__name__, __config__=model.model_config, **{name: field})


type Files = Callable[[str, File], str]
"""
Resolves a file in the request phase. It takes the file as the request names
it, such as ``'run:101'``, and the marker of the file field, and returns the
file's identity, or raises ``ValueError`` if the file is unknown, if the
request's proposal may not read it, or if its metadata do not fit the
marker's; the message is that of the error (see
:class:`~ess.spec.parameters.File`)::

    def resolve(name: str, marker: File) -> str:
        ...  # the identity of the file, or raise ValueError
"""

type LocalPath = Callable[[FileRef], Path]
"""
The local path of a file, by its identity, as the file source gives it. It
looks the path up and never raises: a file that cannot be read fails the
workflow that reads it.
"""

type Reads = list[tuple[Loc, OutputRef | AccumulatorRef, Any]]
"""
The references to outputs in a value, each with its position and the type
that the output's data must fit: the ``T`` of ``Referenced[T]``, or the
union of these.
"""


def held(
    model: type[BaseModel],
    name: str,
    value: Any,
    files: Files,
    local_path: LocalPath,
    table: bool = False,
) -> tuple[Any, Reads]:
    """
    The value of the field ``name`` of ``model`` as the record stores it, in
    the request phase, and the references to outputs in it.

    ``value`` is what the request gives: plain data, or Python objects such
    as an instance of a group or a :data:`~ess.spec.refs.Ref`, which are
    dumped to plain data first. It is validated from its JSON form by its
    request type, without the length constraints of a list if it is a
    ``table`` (see :func:`model_of`), with each file
    resolved by ``files``, and the JSON form of the validated value is
    returned (see :func:`inline`): a group, a file, or
    a reference to an output as given. The type that the data of each
    reference must fit is read from the request type of the field that
    holds it, in the validated value, so the backend checks the reference
    against history without validating again.

    A field that holds a file field and no ``Referenced[...]`` is then
    validated whole by the params model from the JSON form, with the local
    path of each file (see :func:`local_paths` and :func:`validated`), so
    that the validators of a group that holds a file, which its request type
    lacks (see :func:`group_type`), run now and see the path. The record
    still holds the group as the request wrote it; the workflow gets it as
    its validators make it, when the request runs. A field that holds
    ``Referenced[...]`` is validated so only when the request runs, with the
    data read. Any other field is validated whole by its request type
    already.

    Refused, besides what the request type and the params model refuse: a
    file or a reference to an output in the JSON form that the request did
    not give at a file field, or at a field typed ``Referenced[...]``, such
    as one that a serializer makes. Raises
    :class:`~ess.spec.refs.ParamsError` at the position of each problem (see
    :func:`_located`).
    """
    resolved: set[str] = set()
    context = {'files': files, 'resolved': resolved}
    given = _ANY.dump_python(value)
    written = {loc for loc, _ in iter_refs({name: given})}
    one = _one(model, name, table)
    made, stored = _inline(one, name, given, context)
    refs = list(iter_refs({name: stored}))
    if problems := [
        (loc, problem)
        for loc, ref in refs
        if (problem := _stored_ref_problem(ref, loc in written, resolved))
    ]:
        raise ParamsError(problems)
    annotation = model.model_fields[name].rebuild_annotation()
    if field_types.holds_file(annotation) and not field_types.holds_ref(annotation):
        paths = local_paths({name: stored}, local_path)
        validated(model, name, stored, table, {'paths': paths})
    info = one.model_fields[name]
    reads = [
        (loc, ref, _fits(info, made, loc[1:]))
        for loc, ref in refs
        if isinstance(ref, OutputRef | AccumulatorRef)
    ]
    return stored, reads


def local_paths(values: Mapping[str, Any], local_path: LocalPath) -> dict[str, Path]:
    """
    The local path of each file in ``values``, values of parameters by name
    as the record stores them, by its identity: the context ``paths`` of
    :func:`validated`.
    """
    return {
        ref.file: local_path(ref)
        for _, ref in iter_refs(values)
        if isinstance(ref, FileRef)
    }


def _stored_ref_problem(ref: Ref, written: bool, resolved: set[str]) -> str | None:
    """
    Why the record cannot store ``ref``, or ``None``. ``written`` says whether
    the request gave a reference at its position, and ``resolved`` holds the
    identity of each file that a file field resolved.
    """
    if isinstance(ref, FileRef):
        if ref.file in resolved:
            return None
        return (
            f'{ref}: the JSON form of a value holds a file that no file field resolved'
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
    one: type[BaseModel], name: str, value: Any, context: dict[str, Any]
) -> tuple[Any, Any]:
    """
    ``value``, plain data of the field ``name``, validated by ``one``, the
    request type of that field alone, with ``context``, and its JSON form
    (see :func:`inline`). A value without a JSON form
    is validated as it is, since it may be one that the field does not take.
    """
    loc = (name,)
    tp = field_types.field_type(one.model_fields[name])
    try:
        return inline(one, name, value, context=context)
    except PydanticSerializationError as error:
        why = f'cannot be stored as JSON: {error}'
    except ValidationError as error:
        raise ParamsError(_located(loc, tp, value, error.errors())) from None
    except ParamsError as error:
        raise ParamsError(((*loc, *at), msg) for at, msg in error.errors) from None
    except ValueError as error:
        raise _refused(loc, str(error)) from None
    try:
        one.model_validate({name: value})
    except ValidationError as error:
        raise ParamsError(_located(loc, tp, value, error.errors())) from None
    raise _refused(loc, why)


def _located(
    loc: Loc, tp: Any, value: Any, errors: list[ErrorDetails]
) -> list[tuple[Loc, str]]:
    """
    The ``errors`` of validating ``value``, of the request type ``tp``,
    given at ``loc``, each at its position in ``value`` (see
    :func:`position`), once. The errors at a reference or
    inside it are one error at the reference, since the field does not take
    its source, unless each is an error of a file that the field takes: of
    the file source, or of a validator. At a list, the reference is one to a
    whole list.
    """
    found = [(position(tp, e['loc'][1:]), e['msg'], e['type']) for e in errors]
    for at, item in positions((), value):
        inside = {kind for p, _, kind in found if p[: len(at)] == at}
        if (ref := as_ref(item)) is None or inside <= _TAKEN:
            continue
        found = [f for f in found if f[0][: len(at)] != at]
        why = _whole_list(ref) if 'list_type' in inside else _wrong_source(ref)
        found.append((at, why, ''))
    return list(dict.fromkeys(((*loc, *at), msg) for at, msg, _ in found))


_TAKEN: Final = frozenset({'file', 'value_error', 'assertion_error'})


def _wrong_source(ref: Ref) -> str:
    if isinstance(ref, FileRef):
        return f'file {ref}: the field is not a file field'
    return f'{ref} is a reference to an output; the field is not typed Referenced[...]'


def _whole_list(ref: Any) -> str:
    return (
        f'{ref} is a reference to a whole list; a reference stands for one '
        'element of a list'
    )


def _refused(loc: Loc, msg: str) -> ParamsError:
    return ParamsError([(loc, msg)])


@functools.cache
def model_of(model: type[BaseModel], name: str, table: bool = False) -> type[BaseModel]:
    """
    A model of the field ``name`` of ``model`` alone, with its config and
    without the decorators of ``model``: it validates one value by its own
    field. The params model has no decorators but model validators, which
    the framework never calls; a check of one value belongs to the value's
    type.

    With ``table``, the field is a table, validated without its length
    constraints: an accumulator takes a table a few rows at a time, and the
    backend checks the number of rows of the whole table.
    """
    info = model.model_fields[name]
    field = (info.annotation, Field()) if table else (info.annotation, info)
    return create_model(model.__name__, __config__=model.model_config, **{name: field})


def _json_form(one: type[BaseModel], name: str, value: Any) -> Any:
    """
    The JSON form of ``value``, a valid value of the field ``name`` of
    ``one``, a model of that field alone, as its declared type dumps it to
    JSON and JSON reads back: an instance of a subclass of a model type
    without the subclass's fields, and a number that is not finite as
    ``None``.
    """
    made = one.model_construct(**{name: value})
    return from_json(made.model_dump_json(by_alias=True))[name]


def inline(
    one: type[BaseModel],
    name: str,
    value: Any,
    context: dict[str, Any] | None = None,
) -> tuple[Any, Any]:
    """
    ``value`` validated, and its JSON form, as the record stores it. ``value``
    is a value of the field ``name`` of ``one``, a model of that field alone,
    such as the request type of a parameter.

    ``value`` is plain data, such as :func:`held` makes of
    Python objects. It is validated from its JSON form, with ``context``. A
    field that a model lacks is refused at any depth, since validating would
    drop it silently. The JSON form of the validated value must read back as
    a value with the same JSON form, validated without ``context``. Raises:

    - ``PydanticSerializationError`` if it has no JSON form;
    - ``ValidationError`` if it is not valid, or if its JSON form is not,
      such as of a number that is not finite, which JSON holds as ``null``;
    - :class:`~ess.spec.refs.ParamsError` at the position inside the value,
      without ``name``, whose JSON form reads back as another value, such as
      a field of a group whose validator changes a value it has validated
      already.
    """
    given = to_json({name: value}, inf_nan_mode='constants')
    made = getattr(
        one.model_validate_json(given, extra='forbid', context=context), name
    )
    stored = _json_form(one, name, made)
    try:
        again = getattr(one.model_validate_json(to_json({name: stored})), name)
    except ValidationError as error:
        (first, *_) = error.errors()
        raise ValidationError.from_exception_data(
            one.__name__,
            [{'type': _NO_JSON_FORM_ERROR, 'loc': first['loc'], 'input': None}],
        ) from None
    if (read := _json_form(one, name, again)) != stored:
        raise ParamsError([(_differs((), stored, read), _NO_JSON_FORM)])
    return made, stored


_NO_JSON_FORM = 'has no JSON form that reads back as the value'
_NO_JSON_FORM_ERROR = PydanticCustomError('no_json_form', _NO_JSON_FORM)


def _differs(loc: Loc, a: Any, b: Any) -> Loc:
    """
    The first position at which ``a`` and ``b``, two JSON forms at ``loc``,
    differ: a field of an object or an element of a list, at any depth.
    """
    if isinstance(a, dict) and isinstance(b, dict) and a.keys() == b.keys():
        pairs: Iterable[tuple[str | int, Any, Any]] = ((k, a[k], b[k]) for k in a)
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        pairs = ((i, x, y) for i, (x, y) in enumerate(zip(a, b, strict=True)))
    else:
        return loc
    for at, x, y in pairs:
        if x != y:
            return _differs((*loc, at), x, y)
    return loc


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
    :func:`model_of`); without the length constraints
    of a list if it is a ``table``. Raises
    :class:`~ess.spec.refs.ParamsError` at the position of each problem in
    ``value`` (see :func:`position`).

    Without ``context``, ``value`` is data, as in direct use, so that each
    field typed ``Referenced[T]`` takes ``T``, and each file field a
    ``Path``. A spec checks a default with it, and
    :func:`ess.spec.testing.instance` makes a params instance.

    With ``context``, ``value`` is what the record stores, validated from
    its JSON form. ``context`` holds ``paths``, the local path of each file
    by its identity (see :func:`local_paths`), and, in the call phase,
    ``read``, which gives the data of each reference to an output (see
    :data:`~ess.spec.parameters.Referenced`). A field that a model lacks is
    refused. :func:`held` says which parameters are validated so when the
    request is submitted too.

    pydantic tries the members of a union with a member ``Referenced[...]``
    in order, each with the stored value. A member written inline is given a
    reference too, and refuses it, since a field that a model lacks is
    refused. So a validator of such a member, such as a
    ``model_validator(mode='before')``, raises ``ValueError`` for a value it
    does not take, as it must for a malformed request when it is submitted:
    any other exception fails the call. When a member refuses a value of
    another source, such as a member written inline a reference, the error
    that another member adds, that the value is not a reference or not a
    file, is dropped. When the data read for a reference fail a check of
    ``T``, they are one error, at the reference.
    """
    one = model_of(model, name, table)
    try:
        if context is None:
            made = one.model_validate({name: value})
        else:
            given = to_json({name: value})
            made = one.model_validate_json(given, context=context, extra='forbid')
    except ValidationError as error:
        tp = field_types.field_type(one.model_fields[name])
        found = [(position(tp, e['loc'][1:]), e) for e in error.errors()]
        raise ParamsError(
            ((name, *at), e['msg'])
            for at, e in found
            if e['type'] not in _NOT_ITS_SOURCE or not _inside(at, found)
        ) from None
    except DataRefused as refused:
        at = next(loc for loc, ref in iter_refs({name: value}) if ref == refused.ref)
        msg = '; '.join(dict.fromkeys(e['msg'] for e in refused.errors))
        raise ParamsError([(at, msg)]) from None
    return getattr(made, name)


_NOT_ITS_SOURCE: Final = frozenset({'output_ref', 'file_ref'})
"""The errors of a member of a union that the value is not of its source."""


def _inside(at: Loc, found: list[tuple[Loc, ErrorDetails]]) -> bool:
    """
    Whether an error of ``found``, but one of :data:`_NOT_ITS_SOURCE`, is at
    ``at`` or inside.
    """
    return any(
        p[: len(at)] == at and e['type'] not in _NOT_ITS_SOURCE for p, e in found
    )


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
        tp, _, metadata = field_types.unwrapped(tp)
        if (members := field_types.tagged_members(tp, metadata)) is not None:
            tp = members.get(part, Any)
        elif get_origin(tp) in (Union, UnionType):
            inline, inline_metadata, _ = field_types.split(tp)
            tp = Annotated[inline, *inline_metadata] if inline_metadata else inline
        else:
            at.append(part)
            if field_types.is_group(tp) and part in tp.model_fields:
                tp = field_types.field_type(tp.model_fields[part])
            elif get_origin(tp) is list:
                tp = get_args(tp)[0]
    return tuple(at)
