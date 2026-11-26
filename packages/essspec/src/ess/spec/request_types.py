# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The request type of a spec: what a request may write in its params, as a type.

A params model holds what the binding receives: a group, the local path of a
file, or data. A request writes a group inline, a file by name, or a reference
to an output. This module derives the *request type* from the params model, so
that one type both validates a request and is the JSON schema of the params.
It replaces each ``Referenced[T]`` by a reference to an output whose data fit
``T``, and each file field by a file, at any depth::

    # params model                              request type
    reduction: Reduction                        # Reduction
    mask: Referenced[PixelMask]                 # OutputRef
    centre: BeamCentre | Referenced[BeamCentre] # BeamCentre | OutputRef
    sample: RunFile                             # FileRef
    runs: list[RunFile]                         # list[FileRef]
    samples: list[Sample]                       # list[Sample'], Sample'.run a FileRef

The request type of a field is the union of the *sources* that the field
accepts (see :func:`sources`), tagged by :func:`source_of`. A group that holds
neither ``Referenced[...]`` nor a file field, at any depth, is its own class,
with its validators. A group that holds one becomes a derived model without
its validators, which run when the params model validates the group (see
:func:`ess.spec.validation.validated`).
"""

from __future__ import annotations

import dataclasses
import functools
from collections.abc import Callable, Mapping
from typing import Annotated, Any, Final, Union, get_args

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
from .field_types import Choice, FieldShape, FileKind, Group, List, Scalar
from .parameters import File, not_its_source
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


def sources(field: FieldShape) -> dict[str, Any]:
    """
    The sources that a field of shape ``field`` accepts, by tag, each the type
    that validates it, with the constraints of the field.

    - :data:`INLINE`: the union of groups, the group, or the scalar.
    - :data:`FILE`: a :class:`~ess.spec.refs.FileRef`, resolved by the
      resolver of files (see :data:`FileResolver`), with the marker's metadata
      under ``metadata`` in the JSON schema. The other metadata of the field
      are of its ``Path``, so the file has none of them.
    - :data:`OUTPUT`: a reference to an output, with the name of the group,
      or of the union of the groups, under ``dataType`` in the JSON schema.

    In the JSON schema, each alternative of a union requires its tag and is
    titled by it.

    Raises
    ------
    ValueError
        For a list, whose element has sources instead.
    """
    taken: dict[str, Any] = {}
    match field.inline:
        case Choice(tag, groups):
            members = tuple(_alternative(g, tag, v) for v, g in groups.items())
            alternatives = Union[members]  # noqa: UP007
            taken[INLINE] = Annotated[
                alternatives, *field.metadata, Field(discriminator=tag)
            ]
        case FileKind(marker):
            taken[FILE] = Annotated[FileRef, _ResolvedFile(marker)]
        case Group(group):
            taken[INLINE] = _annotated(_group_request_type(group), field.metadata)
        case Scalar(scalar):
            taken[INLINE] = _annotated(scalar, field.metadata)
        case List():
            raise ValueError('a list takes no sources; its element does')
    if referenced := field.referenced:
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
    accepts a file only, resolved to the file's identity by the context
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
    The type of a field that accepts the sources ``taken`` (see :func:`sources`):
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
    The request type of a field typed ``annotation``, with ``metadata``.

    Of a list whose elements hold ``Referenced[...]`` or a file field, it
    retains the length constraints only. The others, such as a validator,
    check the data or the paths, and run when the params model validates the
    list.
    """
    field = field_types.shape(annotation, metadata)
    if not isinstance(field.inline, List):
        return _source_union(sources(field), field.none)
    element = field.inline.element
    metadata = field.metadata
    if field_types.holds_marker(element):
        metadata = tuple(m for m in metadata if isinstance(m, MinLen | MaxLen))
    listed: Any = list[_field_request_type(element)]  # type: ignore[misc]
    made = _annotated(listed, metadata)
    return Union[made, None] if field.none else made  # noqa: UP007


def _annotated(tp: Any, metadata: tuple[Any, ...]) -> Any:
    """``tp`` with ``metadata``, if any."""
    return Annotated[tp, *metadata] if metadata else tp


@functools.cache
def _group_request_type(group: type[BaseModel]) -> type[BaseModel]:
    """
    The request type of ``group``: the group itself, if it holds neither
    ``Referenced[...]`` nor a file field, so that validating a request
    validates it whole.

    Otherwise a model derived from it, with the group's name, docstring and
    config, and each field that holds one typed its request type. It has none
    of the group's validators, which run when the params model validates the
    group.
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
    The request type of the params model ``model``, whose JSON schema is the
    params schema of a spec (see :meth:`ess.spec.WorkflowSpec.serialize`).
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
    with the config of ``model``. A ``table`` has no length constraints,
    since an accumulator receives a table a few rows at a time, and the
    workspace checks the number of rows of the whole table. The model has one
    field, so that a problem of another field does not hide that of this one.
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
Resolves a file in the request phase. It accepts the file as the request writes
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
