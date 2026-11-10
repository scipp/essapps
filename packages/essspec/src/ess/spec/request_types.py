# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The request type of a spec: what a request may write in its params, as a type.

A params model is typed for the call phase: each field holds the value that
the binding takes, a group, the local path of a file, or data. A request
writes something else in the same field: a group inline, a file by name, or
a reference to an output. This module derives, from the params model, the
type of what a request writes, the *request type*, so that the rules on
what a request writes (``packages/essspec/docs/params.md``) are stated once,
as types, and so that one type both validates a request and is the JSON schema
of the params. The request type is the params type with each
``Referenced[T]`` replaced by a reference to an output whose data fit
``T``, and each file field by a file, at any depth::

    params model (call phase)              request type (request phase)
    -------------------------              ----------------------------
    reduction: Reduction                   Reduction
    mask: Referenced[PixelMask]            OutputRef
    centre: BeamCentre                     BeamCentre | OutputRef
            | Referenced[BeamCentre]
    beam_centre: GivenCentre | Find...     GivenCentre | FindCentre  (by tag)
    sample: RunFile                        FileRef
    runs: list[RunFile]                    list[FileRef]
    samples: list[Sample]                  list[Sample'], with Sample' derived
    # Sample.run: RunFile                    run: FileRef
    # Sample.transmission:                   transmission: OutputRef
    #     Referenced[Transmission]

with ``RunFile`` from :mod:`ess.spec`, a file field (see
:class:`~ess.spec.parameters.File`).

The request type of a field is the union of the *sources* that the field
takes (see :func:`sources`), tagged by :func:`source_of`: a group, a union
of groups, or a scalar written ``inline``, a ``file``, or a reference to an
``output``. The source of a value is known from the value alone, by its key
``"@ref"``. A group that holds neither ``Referenced[...]`` nor a file field,
at any depth, is the group's own class, with its validators. A group that
holds one is a model derived from it without its validators (see
:func:`_group_request_type`): they run when the params model validates the
group (see :func:`ess.spec.validation.validated`). A file is a
:class:`~ess.spec.refs.FileRef`, which a workspace validates with a resolver of
files, :data:`FileResolver`, given the field's
:class:`~ess.spec.parameters.File` marker.
"""

from __future__ import annotations

import dataclasses
import functools
from collections.abc import Callable, Mapping
from typing import Annotated, Any, Final, Union, get_args, get_origin

from annotated_types import MaxLen, MinLen
from pydantic import (
    BaseModel,
    BeforeValidator,
    Discriminator,
    Field,
    GetCoreSchemaHandler,
    Tag,
    ValidationInfo,
    create_model,
)
from pydantic.config import JsonDict
from pydantic.fields import FieldInfo
from pydantic.json_schema import GetJsonSchemaHandler, JsonSchemaValue
from pydantic_core import CoreSchema, PydanticCustomError, core_schema

from . import field_types
from .parameters import REFERENCED, File, not_its_source
from .refs import FileRef, Loc, OutputRef

INLINE: Final = 'inline'
"""A group, or a union of groups, written inline as an object of its fields."""
FILE: Final = 'file'
"""A file, an object with ``"@ref": "file"``."""
OUTPUT: Final = 'output'
"""A reference to an output of a record."""


def source_of(value: Any) -> str | None:
    """
    The source of ``value``, as a request writes it at a field or as a Python
    object: :data:`FILE` or :data:`OUTPUT` for an object with the key
    ``"@ref"`` of that kind, ``None`` for one of no kind, and :data:`INLINE`
    for any other value.
    """
    if isinstance(value, FileRef):
        return FILE
    if isinstance(value, OutputRef):
        return OUTPUT
    if not isinstance(value, Mapping) or '@ref' not in value:
        return INLINE
    return {'file': FILE, 'output': OUTPUT}.get(
        value['@ref'] if isinstance(value['@ref'], str) else ''
    )


def sources(tp: Any, metadata: tuple[Any, ...] = ()) -> dict[str, Any]:
    """
    The sources that a field of type ``tp`` takes, by tag, each the type
    that validates it, with the constraints of the field (see
    :func:`~ess.spec.field_types.unwrapped` for ``tp`` and ``metadata``). Of the
    part of ``tp`` that a request writes (see :func:`~ess.spec.field_types.split`):

    - :data:`INLINE`: the union of groups, with its discriminator, the
      group, or the scalar, each group as :func:`_group_request_type` gives it;
    - :data:`FILE`: a :class:`~ess.spec.refs.FileRef`, resolved by the
      resolver of files given the field's marker (see :data:`FileResolver`), with
      the marker's metadata under ``metadata`` in the JSON schema, which say
      which files to offer. The other metadata of the field are of its ``Path``,
      so the file has none of them.

    Of the groups that ``Referenced[...]`` marks:

    - :data:`OUTPUT`: a reference to an output, with the name of the group,
      or of the union of the groups, as its ``dataType`` in the JSON schema,
      and the groups in its metadata (see
      :func:`ess.spec.validation.validate_request_value`).

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
        taken[FILE] = Annotated[FileRef, _ResolvedFile(marker)]
    elif inline is not None:
        written = (
            _group_request_type(inline) if field_types.is_group(inline) else inline
        )
        taken[INLINE] = (
            Annotated[written, *inline_metadata] if inline_metadata else written
        )
    if referenced:
        data: Any = Union[referenced]  # noqa: UP007
        data_type: JsonDict = {'dataType': field_types.type_name(data)}
        taken[OUTPUT] = Annotated[
            OutputRef,
            Field(json_schema_extra=data_type),
            BeforeValidator(functools.partial(_require_source, OUTPUT)),
            _OutputDataType(data),
        ]
    return taken


@dataclasses.dataclass(frozen=True)
class _ResolvedFile:
    """
    The metadata of the request type of a file field with ``marker``: it
    takes a file only, resolved to the file's identity by the context
    ``files``, if given (see :data:`FileResolver`), which it adds to the context's
    set ``resolved``, and its JSON schema holds the marker's metadata under
    ``metadata``.
    """

    marker: File

    def __get_pydantic_core_schema__(
        self, source: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        return core_schema.no_info_before_validator_function(
            functools.partial(_require_source, FILE),
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


def _require_source(source: str, value: Any) -> Any:
    """``value``, if of ``source``, :data:`FILE` or :data:`OUTPUT`."""
    if source_of(value) != source:
        raise not_its_source(value, file=source == FILE)
    return value


@dataclasses.dataclass(frozen=True)
class _OutputDataType:
    """
    The metadata of a reference to an output whose data must fit ``data``,
    the ``T`` of ``Referenced[T]``, or the union of these (see
    :func:`ess.spec.validation.validate_request_value`).
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
    return Annotated[_group_request_type(group), Field(json_schema_extra=extra)]


_SOURCES: Final = {
    INLINE: 'a value written inline',
    FILE: 'a file',
    OUTPUT: 'a reference to an output',
}


def _source_union(taken: Mapping[str, Any], none: bool) -> Any:
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


def _field_request_type(annotation: Any, metadata: tuple[Any, ...] = ()) -> Any:
    """
    The request type of a field typed ``annotation``, with ``metadata`` (see
    :func:`~ess.spec.field_types.field_metadata`): a list of the request type of
    its element, with the list's constraints, or the :func:`_source_union` union of
    its :func:`sources`. Of a list of elements that hold ``Referenced[...]``
    or a file field, it keeps the length constraints only: the others, such
    as a validator, check the data or the paths, and run when the params
    model validates the list (see :func:`ess.spec.validation.validate_request_value`).
    """
    tp, none, metadata = field_types.unwrapped(annotation, metadata)
    if REFERENCED not in metadata and get_origin(tp) is list:
        (element,) = get_args(tp)
        if field_types.holds_marker(element):
            metadata = tuple(m for m in metadata if isinstance(m, MinLen | MaxLen))
        listed: Any = list[_field_request_type(element)]  # type: ignore[misc]
        made = Annotated[listed, *metadata] if metadata else listed
        return Union[made, None] if none else made  # noqa: UP007
    return _source_union(sources(tp, metadata), none)


@functools.cache
def _group_request_type(group: type[BaseModel]) -> type[BaseModel]:
    """
    The request type of ``group``: the group itself, if it holds neither
    ``Referenced[...]`` nor a file field (see
    :func:`~ess.spec.field_types.holds_marker`), so that validating a request
    validates it whole. Otherwise a model derived from it, of the group's
    name, qualified name, docstring, and config, with each field that holds
    one typed its request type, and its metadata, default, title, and
    description kept. It has no validators of the group, which run when the
    params model validates the group (see
    :func:`ess.spec.validation.validate_request_value`).
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
    tp = _field_request_type(info.annotation, field_types.field_metadata(info))
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
    docstring and fields, each typed its :func:`_field_request_type`, with its field
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
def single_field_request_model(
    model: type[BaseModel], name: str, table: bool
) -> type[BaseModel]:
    """
    A model of the field ``name`` of ``model`` alone, typed its request type,
    with the config of ``model``; without the length constraints of a list
    if it is a ``table``: an accumulator takes a table a few rows at a time,
    and the workspace checks the number of rows of the whole table. It is made
    from the one field, so that a problem of another field does not hide that
    of this one.
    """
    info = model.model_fields[name]
    field: Any = (
        (_field_request_type(info.annotation), ...)
        if table
        else _request_field(name, info)
    )
    return create_model(model.__name__, __config__=model.model_config, **{name: field})


type FileResolver = Callable[[str, File], str]
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


def output_data_type(info: FieldInfo, value: Any, at: Loc) -> Any:
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
    return _output_data_type_in(info.rebuild_annotation())


def _output_data_type_in(tp: Any) -> Any:
    """
    The data of the one :class:`_OutputDataType` in ``tp``, at any depth, or
    ``None``.
    """
    found = [
        m.data
        for m in getattr(tp, '__metadata__', ())
        if isinstance(m, _OutputDataType)
    ]
    found += [
        data for arg in get_args(tp) if (data := _output_data_type_in(arg)) is not None
    ]
    return found[0] if found else None
