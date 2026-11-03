# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The request type of a spec: what a request may write at each place, as a type.

A params model is typed for the call phase: each field holds the value that
the binding takes, a group, a file model with its local path, or data. A
request writes something else at the same place: a group inline, a file by
name, or a reference to an output. This module derives, from the params
model, the type of what a request writes, the *request type*, so that the
rules of :mod:`ess.spec._workflow_spec` on what a request writes are stated
once, as types, and so that one type both validates a request and is the
JSON schema of the params. The request type is the params type with each
``Referenced[T]`` replaced by a reference to an output whose data fit
``T``::

    params model (call phase)              request type (request phase)
    -------------------------              ----------------------------
    reduction: Reduction                   Reduction
    mask: Referenced[PixelMask]            OutputRef | AccumulatorRef
    centre: BeamCentre                     BeamCentre | OutputRef
            | Referenced[BeamCentre]                  | AccumulatorRef
    beam_centre: GivenCentre | Find...     GivenCentre | FindCentre  (by tag)
    sample: SampleRun                      SampleRun                 (@ref file)
    runs: list[RunFile]                    list[RunFile]

The request type of a parameter, or of a row of a list parameter, is the
union of the *sources* that the place takes (see :func:`sources`), tagged by
:func:`source_of`: a group or a union of groups written ``inline``, a
``file``, or a reference to an ``output``. The source of a value is known
from the value alone, by its key ``"@ref"``, so a value is validated by
the type of its source (see :func:`source_model`). A group written inline is
the group's own class, with its validators, and a file is the file model's
own class, which a backend validates with a resolver of files (see
:class:`~ess.spec.parameters.FileModel`). So validating a request by its
request type validates each group whole and resolves each file, and what is
left of the request phase is to check each reference to an output against
history (see :func:`ess.spec.places.ref_types`).

The backend validates each parameter of a request with :func:`held`, which
it gives a resolver of files, :data:`Files`. The record of a request holds
the JSON form of its request type: each group and file validated, each
reference as given. The call phase makes the params model's values from it
(see :func:`ess.spec.places.validated`).
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping
from typing import Annotated, Any, Final, Union

from pydantic import BaseModel, Discriminator, Field, Tag, TypeAdapter, create_model
from pydantic_core import ErrorDetails

from . import field_types, places
from .parameters import FileModel
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


def _not_a_ref(schema: dict[str, Any]) -> None:
    """Refuse an object with the key ``@ref`` in ``schema``: it is a reference."""
    schema['not'] = {'required': ['@ref']}


def sources(tp: Any, metadata: tuple[Any, ...] = ()) -> dict[str, Any]:
    """
    The sources that a parameter or row of type ``tp`` takes, by tag, each the
    type that validates it, with the constraints of the place (see
    :func:`~ess.spec.places.root_type` for ``tp`` and ``metadata``). Of the
    part of ``tp`` that a request writes (see :func:`~ess.spec.places.split`):

    - :data:`INLINE`: the union of groups, with its discriminator, or the
      group;
    - :data:`FILE`: the file model.

    Of the groups that ``Referenced[...]`` marks:

    - :data:`OUTPUT`: a reference to an output, with the name of the group,
      or of the union of the groups, as its ``dataType`` in the JSON schema.

    In the JSON schema, an inline source refuses an object with the key
    ``@ref``, and each alternative of a union requires its tag, since a
    request always writes it, and is titled by its tag (see
    :func:`~ess.spec.places.title`). A form panel shows a widget for each
    source a place takes.
    """
    taken: dict[str, Any] = {}
    inline, inline_metadata, referenced = places.split(tp, *metadata)
    union = None if inline is None else places.alternatives(inline, *inline_metadata)
    if union is not None:
        tag, groups = union
        members = tuple(_alternative(group, tag, v) for v, group in groups.items())
        alternatives = Union[members]  # noqa: UP007
        written = Annotated[alternatives, *inline_metadata, Field(discriminator=tag)]
        taken[INLINE] = Annotated[written, Field(json_schema_extra=_not_a_ref)]
    elif field_types.is_file_model(inline):
        taken[FILE] = Annotated[inline, *inline_metadata] if inline_metadata else inline
    elif inline is not None:
        taken[INLINE] = Annotated[
            inline, *inline_metadata, Field(json_schema_extra=_not_a_ref)
        ]
    if referenced:
        data_type = {'dataType': field_types.type_name(Union[referenced])}  # noqa: UP007
        taken[OUTPUT] = Annotated[
            OutputRef | AccumulatorRef,
            Field(discriminator='kind', json_schema_extra=data_type),
        ]
    return taken


def _alternative(group: type[BaseModel], tag: str, value: str) -> Any:
    """
    The alternative ``group`` of a union, whose tag ``tag`` is ``value``: in
    the JSON schema, the tag is required and titles the alternative. pydantic
    would leave out a tag with a default from ``required``, and title the
    alternative by the class; it requires the tag when it validates.
    """
    extra = {
        'title': places.title(value, group.model_fields[tag]),
        'required': [tag],
    }
    return Annotated[group, Field(json_schema_extra=extra)]


def tagged(taken: Mapping[str, Any], none: bool) -> Any:
    """
    The type of a place that takes the sources ``taken`` (see :func:`sources`):
    the source, if one, else their union tagged by :func:`source_of`; ``| None``
    if ``none``.
    """
    if len(taken) == 1:
        (source,) = taken.values()
    else:
        members = tuple(Annotated[tp, Tag(tag)] for tag, tp in taken.items())
        source = Annotated[Union[members], Discriminator(source_of)]  # noqa: UP007
    return Union[source, None] if none else source  # noqa: UP007


@functools.cache
def row_sources(model: type[BaseModel], name: str) -> dict[str, Any]:
    """
    The :func:`sources` of the parameter ``name`` of ``model``, or of one row
    of it if it is a list parameter.
    """
    tp, _, metadata = places.root_type(model, name)
    return sources(tp, metadata)


@functools.cache
def row_type(model: type[BaseModel], name: str) -> Any:
    """
    The request type of the parameter ``name`` of ``model``, or of one row of
    it if it is a list parameter.
    """
    return tagged(row_sources(model, name), places.root_type(model, name)[1])


def parameter_type(model: type[BaseModel], name: str) -> Any:
    """
    The request type of the parameter ``name`` of ``model``: its
    :func:`row_type`, in a list with the field's length constraints if it is
    a list parameter.
    """
    row = row_type(model, name)
    if not field_types.is_list(model, name):
        return row
    listed: Any = list[row]  # type: ignore[valid-type]
    metadata = model.model_fields[name].metadata
    return Annotated[listed, *metadata] if metadata else listed


@functools.cache
def source_model(model: type[BaseModel], name: str, source: str) -> type[BaseModel]:
    """
    A model of the field ``name`` alone, typed its source ``source`` (see
    :func:`row_sources`), with the config of ``model``: it validates one value
    from that source that a request writes at the parameter, or one row of it.
    """
    field = (row_sources(model, name)[source], ...)
    return create_model(model.__name__, __config__=model.model_config, **{name: field})


@functools.cache
def request_model(model: type[BaseModel]) -> type[BaseModel]:
    """
    The request type of the params model ``model``: a model of the same
    fields, each typed its :func:`parameter_type`, with its title,
    description, and default. Its JSON schema is the params schema of a
    spec (see :meth:`ess.spec.WorkflowSpec.serialize`).
    """
    fields: dict[str, Any] = {}
    for name, info in model.model_fields.items():
        fields[name] = (
            parameter_type(model, name),
            Field(
                # Undefined, and so required, if the field is.
                default=info.get_default(call_default_factory=True),
                title=places.title(name, info),
                description=info.description,
            ),
        )
    return create_model(model.__name__, __config__=model.model_config, **fields)


type Files = Callable[[str, type[FileModel]], str]
"""
Resolves a file in the request phase. It takes the file as the request names
it, such as ``'run:101'``, and the file model of the field, and returns the
file's identity, or raises ``ValueError`` (see
:class:`~ess.spec.parameters.FileModel`).
"""


def held(model: type[BaseModel], name: str, value: Any, files: Files) -> Any:
    """
    The value of the parameter ``name`` of ``model`` as the record stores
    it, in the request phase.

    ``value`` is what the request gives: plain data, or Python objects such
    as an instance of a group or a :data:`~ess.spec.refs.Ref`, which are
    dumped to plain data first. For a list parameter, each row is handled
    on its own. Each is validated from its JSON form by its request type
    (see :func:`source_model`), with each file resolved by
    ``files``, and the JSON form of the validated value is returned (see
    :func:`ess.spec.field_types.inline`): a group, a file, or a reference
    to an output as given. The caller checks that the data of a reference
    to an output fit the parameter (see :func:`~ess.spec.places.ref_types`).

    Refused, besides what the request type refuses: an object with the key
    ``"@ref"`` that is no reference, at any depth; a reference to an output
    inside a value, since it fills a whole parameter or row; a file that
    ``files`` did not resolve, such as one that a validator of a group
    makes from a name, since the file source has not checked it; and a
    reference to an output in the JSON form of a group, such as one that a
    serializer makes. Raises :class:`~ess.spec.refs.ParamsError` at the
    position of each problem.
    """
    resolved: set[str] = set()

    def resolve(file: str, model: type[FileModel]) -> str:
        identity = files(file, model)
        resolved.add(identity)
        return identity

    context = {'files': resolve}
    given = _ANY.dump_python(value)
    if not field_types.is_list(model, name):
        stored = _row(model, name, (name,), given, context)
    elif (ref := ref_at((name,), given)) is not None:
        raise _refused(
            (name,),
            f'{ref} is a reference to a whole list; a reference stands for one '
            'element of a list',
        )
    elif not isinstance(given, list):
        raise _refused((name,), f'{given!r} is not a list')
    else:
        stored = [_row(model, name, (name, i), v, context) for i, v in enumerate(given)]
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


_ANY = TypeAdapter(Any)
"""Dumps Python objects to plain data, keeping a number that is not finite."""


def _row(
    model: type[BaseModel], name: str, loc: Loc, value: Any, context: dict[str, Any]
) -> Any:
    """
    ``value``, plain data at ``loc``, of the parameter ``name`` or of a row
    of it, validated by the type of its source (see
    :func:`source_of`); its JSON form.
    """
    tp, none, metadata = places.root_type(model, name)
    inline, inline_metadata, _ = places.split(tp, *metadata)
    union = None if inline is None else places.alternatives(inline, *inline_metadata)
    for at, item in positions(loc, value):
        ref = ref_at(at, item)
        if at == loc or ref is None or isinstance(ref, FileRef):
            continue
        if union is not None and at == (*loc, union[0]):
            raise _refused(at, f'{ref}: a tag is written inline, never as a reference')
        raise _refused(
            at,
            f'{ref}: a reference to an output fills a whole parameter or a whole '
            'row, never a field of a group',
        )
    if value is None and none:
        return None
    source = source_of(value)
    if source not in row_sources(model, name):
        raise _refused(loc, _source_refused(tp, inline, value, source))

    def problem(error: ErrorDetails) -> tuple[Loc, str]:
        return _problem(loc, union, value, error)

    one = source_model(model, name, source)
    return places.inline_at(loc, one, name, value, problem, whole=True, context=context)


def _problem(
    loc: Loc,
    union: tuple[str, dict[str, Any]] | None,
    value: Any,
    error: ErrorDetails,
) -> tuple[Loc, str]:
    """
    ``error`` of ``value`` at ``loc``, a place of the union ``union`` if not
    ``None``, at its place, with its message. pydantic puts the tag of a
    union after the union's place, ``('beam_centre', 'find', 'tolerance')``,
    which is left out, ``beam_centre.tolerance``, so that the place is the
    same whichever way the alternative is chosen.
    """
    inner, kind = error['loc'][1:], error['type']
    if kind in ('model_type', 'dict_type') and not inner:
        return loc, (
            f'{value!r} is not an object; a group is written as an object of its fields'
        )
    if union is None:
        return (*loc, *inner), error['msg']
    tag, groups = union
    if kind == 'union_tag_not_found' and not inner:
        return loc, f'the tag {tag!r} is not given; it is one of {list(groups)}'
    if kind == 'union_tag_invalid' and not inner:
        return (*loc, tag), f'{value[tag]!r} is not a tag, one of {list(groups)}'
    return (*loc, *inner[1:]), error['msg']


def _source_refused(tp: Any, inline: Any, value: Any, source: str | None) -> str:
    """
    Why a place of type ``tp`` refuses ``value``, of the source ``source``;
    ``inline`` is the part of ``tp`` that a request writes, or ``None`` (see
    :func:`~ess.spec.places.split`).
    """
    name = field_types.type_name(tp)
    if source == FILE:
        return f'file {as_ref(value)} does not fit {name}, which is not a file model'
    if source == OUTPUT:
        ref = as_ref(value)
        if field_types.is_file_model(inline):
            return (
                f'{ref} is a reference to an output; {name} is given by reference '
                'to a file, such as file(path=...)'
            )
        return (
            f'{ref} is a reference to an output; {name} is written inline, since '
            'the spec does not mark the place Referenced[...]'
        )
    if field_types.is_file_model(inline):
        return (
            f'{value!r} is not a reference; {name} is given by reference to a '
            'file, such as file(path=...)'
        )
    return f'{value!r} is not a reference; {name} is given by reference to an output'


def _refused(loc: Loc, msg: str) -> ParamsError:
    return ParamsError([(loc, msg)])
