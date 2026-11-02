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
JSON schema of the params::

    params model (call phase)        request type (request phase)
    -------------------------        ----------------------------
    reduction: Reduction             Reduction | OutputRef | AccumulatorRef
    mask: PixelMask                  OutputRef | AccumulatorRef
    centre: GivenCentre | Find...    GivenCentre | FindCentre      (by tag)
    sample: SampleRun                SampleRun                     (@ref file)
    runs: list[RunFile]              list[RunFile]

The request type of a parameter, or of a row of a list parameter, is the
union of the *forms* that the place takes (see :func:`forms`), tagged by
:func:`form_of`: a group or a union of groups written ``inline``, a
``file``, or a reference to an ``output``. The form of a value is known
from the value alone, by its key ``"@ref"``, so a value is validated by
the type of its form (see :func:`form_model`). A group written inline is
the group's own class, with its validators, and a file is the file model's
own class, which a backend validates with a resolver of files (see
:class:`~ess.spec.parameters.FileModel`). So validating a request by its
request type validates each group whole and resolves each file, and what is
left of the request phase is to check each reference to an output against
history (see :func:`ess.spec.places.ref_types`).

The record of a request holds the JSON form of its request type: each group
and file validated, each reference as given. The call phase makes the
params model's values from it (see :func:`ess.spec.places.validated`).
"""

from __future__ import annotations

import functools
from collections.abc import Mapping
from typing import Annotated, Any, Final, Union

from pydantic import BaseModel, Discriminator, Field, Tag, create_model

from . import field_types, places
from .parameters import FileModel
from .refs import AccumulatorRef, FileRef, OutputRef

INLINE: Final = 'inline'
"""The form of a group, or a union of groups, written as an object of its fields."""
FILE: Final = 'file'
"""The form of a file, an object with ``"@ref": "file"``."""
OUTPUT: Final = 'output'
"""The form of a reference to an output, of a record or of an accumulator."""


def form_of(value: Any) -> str | None:
    """
    The form of ``value``, as a request writes it at a place, or its Python
    form: :data:`FILE` or :data:`OUTPUT` for an object with the key
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


def forms(tp: Any, metadata: tuple[Any, ...] = ()) -> dict[str, Any]:
    """
    The forms that a parameter or row of type ``tp`` takes, by tag, each the
    type that validates it, with the constraints of the place (see
    :func:`~ess.spec.places.root_type` for ``tp`` and ``metadata``):

    - :data:`INLINE`: the union of groups, with its discriminator, or the
      group if it has no data value (see :func:`~ess.spec.places.unwritten`);
    - :data:`FILE`: the file model;
    - :data:`OUTPUT`: a reference to an output, if one can fit ``tp`` (see
      :func:`~ess.spec.places.takes_output`), with the name of ``tp`` as its
      ``dataType`` in the JSON schema.

    In the JSON schema, an inline form refuses an object with the key
    ``@ref``, and each alternative of a union requires its tag, since a
    request always writes it, and is titled by its tag (see
    :func:`~ess.spec.places.title`). A form panel shows a widget for each
    form a place takes.
    """
    taken: dict[str, Any] = {}
    if (union := places.alternatives(tp, *metadata)) is not None:
        tag, groups = union
        members = tuple(_alternative(group, tag, v) for v, group in groups.items())
        alternatives = Union[members]  # noqa: UP007
        inline = Annotated[alternatives, *metadata, Field(discriminator=tag)]
        taken[INLINE] = Annotated[inline, Field(json_schema_extra=_not_a_ref)]
    elif places.written_inline(tp):
        taken[INLINE] = Annotated[tp, *metadata, Field(json_schema_extra=_not_a_ref)]
    if field_types.is_file_model(tp):
        taken[FILE] = Annotated[tp, *metadata] if metadata else tp
    if places.takes_output(tp):
        data_type = {'dataType': field_types.type_name(tp)}
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
    The type of a place that takes the forms ``taken`` (see :func:`forms`):
    the form, if one, else their union tagged by :func:`form_of`; ``| None``
    if ``none``.
    """
    if len(taken) == 1:
        (form,) = taken.values()
    else:
        members = tuple(Annotated[tp, Tag(tag)] for tag, tp in taken.items())
        form = Annotated[Union[members], Discriminator(form_of)]  # noqa: UP007
    return Union[form, None] if none else form  # noqa: UP007


@functools.cache
def row_forms(model: type[BaseModel], name: str) -> dict[str, Any]:
    """
    The :func:`forms` of the parameter ``name`` of ``model``, or of one row
    of it if it is a list parameter.
    """
    tp, _, metadata = places.root_type(model, name)
    return forms(tp, metadata)


@functools.cache
def row_type(model: type[BaseModel], name: str) -> Any:
    """
    The request type of the parameter ``name`` of ``model``, or of one row of
    it if it is a list parameter.
    """
    return tagged(row_forms(model, name), places.root_type(model, name)[1])


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
def form_model(model: type[BaseModel], name: str, form: str) -> type[BaseModel]:
    """
    A model of the field ``name`` alone, typed its form ``form`` (see
    :func:`row_forms`), with the config of ``model``: it validates one value
    of that form that a request writes at the parameter, or one row of it.
    """
    field = (row_forms(model, name)[form], ...)
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
