# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Workflow specifications: implementation-independent workflow metadata.

A :class:`WorkflowSpec` describes a workflow's user-facing interface — identity,
display metadata, and two pydantic models — without reference to how or
where the workflow is computed:

=======  =============================  ===============  ==========================
Model    Holds                          In a request     When the workflow runs
=======  =============================  ===============  ==========================
params   everything the workflow        each value       the values, and the data
         takes: settings, files, data   inline or by     the references name
                                        reference
outputs  results                        (none)           the data the workflow
                                                         returns
=======  =============================  ===============  ==========================

A request gives the value at each *place* inline or by reference: the value
of each parameter, each element of a list, and each field of a group, at any
depth (see :mod:`ess.spec.places`). The type at a place decides which of the
two it takes, with ``| None`` removed:

=====================================  ==========  ===========================
The type at a place                    Inline      By reference
=====================================  ==========  ===========================
a file: ``Path``, or a ``NewType``     no          to a dataset
of it
a group, or a union of groups          its fields  to an output that fits and
                                                   holds no file
a list, ``list[X]``                    each        no
                                       element a
                                       place
has a *JSON form*: pydantic makes a    yes         to an output that fits;
JSON schema of it, with the config of              none for a list that may
its model, and it holds no file                    be ``None`` or a dict
anything else, such as a scipp array   no          to an output that fits
=====================================  ==========  ===========================

An *inline value* is the value itself in the request, held in its JSON form;
a reference is described in :mod:`ess.spec.refs`. A *group* is a model, such
as ``Reduction``, written as an object of its fields, each a place, so a
file in it comes from a dataset; a ``RootModel`` is not a group, but a type
written as its root value::

    class Sample(BaseModel):
        run: SampleFile
        mask: PixelMask | None = None  # None: no pixels masked

    {'sample': {'run': dataset(run=101), 'mask': m.ref('mask')}}
    {'sample': {'run': dataset(run=101)}}  # defaults for the rest

A request lives in two phases. In the *request phase*, before any data is
read, a value given by reference is a reference, and each place given
inline is validated alone. A group without a reference inside is a value
given inline: it is validated whole, and the record holds it as validated.
Of a group with a reference inside, each field given inline is validated
alone. In the *call phase*, the backend reads the data each reference names
and validates each parameter's value once, whole, so every validator of
every group runs with all its fields at hand, and every field holds the
type it declares.

The models are ordinary pydantic models and carry no annotations for the
framework: what the framework needs, it reads from the field types (see
:mod:`ess.spec.field_types`). The rules on them, checked when a spec is made:

- The names of params and outputs are distinct.
- Each parameter's type is a type, possibly optional, or a list of one; not
  a dict. A type may be a union, such as a union of groups.
- A file is at a place only, possibly optional or in a list: not inside a
  tuple, a dict, or a dataclass, whose contents are not places, so no
  dataset can give it.
- A union of groups, a parameter or inside a group, is discriminated by the
  name of its tag field, not by a callable, so that the tag of a value tells
  its alternative. The tag field of each alternative is a ``Literal`` of one
  string, its own, such as ``Literal['find']``, without an alias. A union
  that holds a group holds groups only, not ``GivenCentre | float``: a value
  of a type that is not a group has no tag. A field of a group that
  is a dict, or a list that may be ``None``, has a JSON form, since its
  contents are not places and no reference stands for a whole one.
- A group has no serializers, computed fields, or aliases: the record holds
  a group as the object of its places, each in the JSON form of its own
  type, and these would make the group's own JSON form differ from it. It
  may have field and model validators of any mode, which run when the group
  is validated whole: in the request phase if it has no reference inside,
  else in the call phase only. So a validator that widens what a field
  accepts works only for a group written without references. It is not at
  a place inside itself, at any depth, such as a tree with
  ``children: list[Node]``, since the schema of the forms at its places
  would not end; a tree is written whole, ``children: list[Node] | None``.
- A default is a value, never a reference: what a request may write at the
  place, in a JSON form that reads back as the value, with no ``@ref``
  object at any depth; of a type without a JSON form, ``None`` or an empty
  list. It is the value that validating it gives, since pydantic does not
  validate a default: a group's default is an instance, not the object of
  its fields, and a tuple's a tuple, not a list.
- No parameter sees another: each is validated alone, in every phase. So
  the params model has no field validators, serializers, computed fields, or
  aliases, and no ``default_factory`` that takes the other values. A list
  parameter's field carries only length constraints, ``Field(min_length=2)``:
  a check of one row belongs to the element type. A check of one value
  belongs to the value's type, such as a field validator of a group; so does
  its JSON form, which the framework stores and reads back.
- Each output is a type, possibly optional, or a list or a dict of one.
- Each table is a list parameter without a default.

A check across parameters is the binding's or the driver's, and fails when
the request runs. The params model may state one as a model validator, but
the framework never calls it: a stage and a driver never get a whole params
instance. A binding may run it, ``IofQParams.model_validate(dict(params))``.

A *table* is a list parameter whose *rows* an accumulator takes a push at a
time, such as the sample runs; its rows are distinct. A row with more than
one value, such as a run and the values given next to it, is a group.
``tables`` names the tables. A list parameter is a table
only where ``tables`` names it. A request gives every table, ``[]`` for one
without rows, so that no request leaves out a table by mistake.

Two forms exist, related by a one-way projection:

* :class:`WorkflowSpec` is the in-process form. It holds the model *classes*,
  so consumers in the same process get full pydantic validation.
* :class:`SerializedWorkflowSpec` is the plain-data form produced by
  :meth:`WorkflowSpec.serialize`. It is what a service announces to remote
  consumers. There is intentionally no inverse: validators do not round-trip
  through JSON Schema, and authoritative validation always happens in the
  process owning the model classes.
"""

from __future__ import annotations

import functools
from collections.abc import Sequence
from types import NoneType, UnionType
from typing import Annotated, Any, Self, Union, get_args, get_origin

from annotated_types import MaxLen, MinLen
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    create_model,
    model_validator,
)
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from .field_types import (
    base,
    element,
    has_json_form,
    holds_file,
    is_file,
    is_group,
    is_list,
    is_single,
    optional,
    type_name,
    value_type,
)
from .places import alternatives, file_off_place, groups_at, held, validated
from .refs import AccumulatorRef, DatasetRef, Loc, OutputRef, ParamsError, Ref


class NoParams(BaseModel):
    """
    Params model for workflows that take no parameters.

    Workflows always have a params model, so consumers never branch on its
    absence; "takes no parameters" is expressed as a model with no fields.
    Extra fields are rejected so that sending params to such a workflow is an
    error rather than silently ignored.
    """

    model_config = ConfigDict(extra='forbid')


class _SpecFields(BaseModel, frozen=True):
    """Metadata fields shared by both forms of the workflow spec."""

    name: str = Field(
        min_length=1,
        description=(
            "Machine-readable workflow identifier. Unique within the context "
            "that enumerates the spec; global uniqueness is the enumerator's "
            "responsibility, not the spec's."
        ),
    )
    version: int = Field(
        ge=1,
        description=(
            "Version of the workflow interface. Increment on any change a "
            "consumer could observe: params, outputs, or semantics."
        ),
    )
    title: str = Field(min_length=1, description="Display title of the workflow.")
    description: str = Field(
        min_length=1, description="Description of what the workflow computes."
    )
    code_revision: str | None = Field(
        default=None,
        description=(
            "Git commit or package version of the workflow code this spec "
            "describes, so that a record made from a development branch is "
            "honest about what ran. Provenance, not identity: the interface "
            "version is ``version``."
        ),
    )
    tables: tuple[str, ...] = Field(
        default=(),
        description=(
            "The list parameters whose rows an accumulator takes a push at a "
            "time, each a table."
        ),
    )

    def __hash__(self) -> int:
        # The schemas of the serialized form are dicts, which pydantic's hash
        # of all fields refuses; equal specs have equal identities, so these
        # suffice.
        return hash((type(self), self.name, self.version))


class WorkflowSpec[P: BaseModel, O: BaseModel](_SpecFields, frozen=True):
    """
    Implementation-independent specification of a workflow's user interface.

    Holds identity and display metadata and the pydantic model classes of the
    workflow's params and outputs. Contains no factory, no executor, and no
    reference to any workflow implementation. The module defining a spec
    must therefore be importable without importing the workflow code, so
    that a service can load and validate every spec it knows without that
    code.

    The spec is generic over its models, which a type checker infers from
    ``params=`` and ``outputs=``, so that a binding's
    ``compute(params: P) -> O`` is checked against them. Code that takes any
    spec annotates it ``WorkflowSpec[Any, Any]``.
    """

    # A type checker cannot check a default against P, which only params=
    # decides; pyright honours this mypy ignore too.
    params: type[P] = Field(  # type: ignore[assignment]
        default=NoParams,
        description=(
            "Pydantic model class of everything the workflow takes: each field "
            "a type, possibly optional, or a list of one. Defaults to "
            ":class:`NoParams`."
        ),
    )
    outputs: type[O] = Field(
        description=(
            "Pydantic model class of the workflow's results. Field title and "
            "description are the display metadata; a field may be optional "
            "when the workflow does not always produce it. Order is "
            "meaningful: consumers present outputs in this order and may "
            "auto-select the first, so put the primary output first."
        ),
    )

    @model_validator(mode='after')
    def _check_models(self) -> Self:
        problems = [
            *_distinct(self.params, self.outputs),
            *_params_problems(self.params),
            *_outputs_problems(self.outputs),
            *_tables_problems(self),
        ]
        if problems:
            raise ValueError(f'{self.name}: {"; ".join(problems)}')
        return self

    def has_json_form(self, name: str) -> bool:
        """
        Whether the type of the parameter ``name``, with ``| None`` removed
        and for a list parameter its element type taken, has a JSON form (see
        :func:`ess.spec.field_types.has_json_form`).
        """
        return has_json_form(self.params, name)

    def serialize(self) -> SerializedWorkflowSpec:
        """
        Project to the plain-data form.

        The params schema has the value at each place in the forms it takes:
        ``anyOf`` its JSON schema and a reference if it takes an inline value,
        else a reference; a group as the object of its fields, each so. A
        reference's schema is the ``@ref`` object of each kind it may be, with
        the name of the data type it must fit in ``dataType``, as the outputs
        schema has each output.
        The projection is one-way: pydantic validators do not survive it, so
        a consumer of the serialized form can validate only optimistically.
        """
        return SerializedWorkflowSpec(
            name=self.name,
            version=self.version,
            title=self.title,
            description=self.description,
            code_revision=self.code_revision,
            tables=self.tables,
            params_schema=_params_schema(self),
            outputs_schema=_outputs_schema(self.outputs),
        )


class SerializedWorkflowSpec(_SpecFields, frozen=True):
    """
    Plain-data form of a workflow spec, safe to send across process boundaries.

    Produced by :meth:`WorkflowSpec.serialize`; round-trips through JSON. A form
    shows the params schema: a group by its fields, with a choice to give it
    by reference, and a picker at each place that takes a reference only,
    which names the type of the data it takes in ``dataType``; a list as an
    array of these, an optional one with ``null`` in ``anyOf``.
    """

    params_schema: dict[str, Any] = Field(
        description="Schema of the workflow's params."
    )
    outputs_schema: dict[str, Any] = Field(
        description="Schema of the workflow's outputs, by the type of each value."
    )


def _distinct(params: type[BaseModel], outputs: type[BaseModel]) -> list[str]:
    """Refuse a name of both a parameter and an output."""
    return [
        f'{name}: a field of both {params.__name__} and {outputs.__name__}'
        for name in params.model_fields.keys() & outputs.model_fields.keys()
    ]


def _one_or_list(annotation: Any) -> bool:
    """
    Whether ``annotation`` is ``X``, ``X | None``, ``list[X]``, or
    ``list[X | None]``: the shapes of a parameter.
    """
    tp, is_optional = optional(annotation)
    return element(tp) is None or (get_origin(tp) is list and not is_optional)


def _decorators(model: type[BaseModel], kinds: Sequence[str]) -> dict[str, list[str]]:
    """The decorators of ``model`` itself of each of ``kinds``, by kind."""
    decorators = model.__pydantic_decorators__
    return {
        kind: sorted(getattr(decorators, kind))
        for kind in kinds
        if getattr(decorators, kind)
    }


def _model_validators_only(model: type[BaseModel]) -> list[str]:
    """
    Refuse a decorator of the params model itself but a model validator: a
    field validator, a field or model serializer, or a computed field.
    """
    kinds = ('validators', 'field_validators', *_SERIALIZERS)
    if found := _decorators(model, kinds):
        return [
            f'{model.__name__}: no decorators but model validators, {found}; '
            "the check or the JSON form of one value belongs to the value's type"
        ]
    return []


_SERIALIZERS = ('field_serializers', 'model_serializers', 'computed_fields')


def _params_problems(model: type[BaseModel]) -> list[str]:
    problems = _model_validators_only(model)
    for name, field in model.model_fields.items():
        where = f'{model.__name__}.{name}'
        tp = value_type(field.annotation)
        union = get_origin(tp) in (Union, UnionType)
        if not (_one_or_list(field.annotation) and (is_single(tp) or union)):
            problems.append(
                f'{where}: a parameter is a type, possibly optional, or a list of one'
            )
            continue
        if _has_alias(field):
            problems.append(f'{where}: a parameter has no alias')
        if get_origin(optional(field.annotation)[0]) is list and (
            other := [m for m in field.metadata if not isinstance(m, MinLen | MaxLen)]
        ):
            problems.append(
                f'{where}: a list parameter carries only length constraints, '
                f'not {other}; a check of one row belongs to the element type'
            )
        if place := _place_problems(model, name, (), set()):
            problems.extend(f'{where}: {problem}' for problem in place)
        elif field.default_factory_takes_validated_data:
            problems.append(
                f'{where}: the default_factory of a parameter takes no other values'
            )
        elif not field.is_required() and (problem := _default_problem(model, name)):
            problems.append(f'{where}: {problem}')
    return problems


def _has_alias(field: FieldInfo) -> bool:
    return any(
        getattr(field, a) is not None
        for a in ('alias', 'validation_alias', 'serialization_alias')
    )


def _place_problems(
    model: type[BaseModel],
    name: str,
    path: tuple[type[BaseModel], ...],
    seen: set[type[BaseModel]],
) -> list[str]:
    """
    Refuse a field of ``model`` whose places a request cannot write: a file
    at a position that is not a place, a union that the framework cannot
    take apart by a value's tag, a group at a place inside itself, and the problems
    of each group at its places, each named by its group (see
    :func:`_group_problems`). ``path`` holds the groups that hold the field,
    and ``seen`` the groups checked already, so that a group at several
    places is checked once.
    """
    try:
        groups = groups_at(model, name)
    except ValueError as error:
        return [str(error)]
    if file_off_place(model, name):
        return [
            'a file is at a place only, possibly optional or in a list: not in '
            'a tuple, a dict, or a dataclass, where no dataset gives it'
        ]
    problems = []
    for group in groups:
        if group in path:
            problems.append(
                f'{group.__name__}: a group is not at a place inside itself'
            )
        elif group not in seen:
            seen.add(group)
            problems.extend(_group_problems(group, (*path, group), seen))
    return problems


def _group_problems(
    group: type[BaseModel],
    path: tuple[type[BaseModel], ...],
    seen: set[type[BaseModel]],
) -> list[str]:
    """
    Refuse a group whose fields a request cannot write each at its place: a
    serializer or a computed field, which would make the group's JSON form
    differ from the object of its places, a field with an alias, a dict or a
    list that may be ``None`` without a JSON form, whose contents are not
    places, the problems of the places of its fields, and a default that is
    not a value, since a field left out holds its default. Field and model
    validators of any mode are allowed: they run whenever the group is
    validated whole (see :mod:`ess.spec.places`). ``path`` holds the groups
    that hold ``group`` and itself.
    """
    problems = []
    if found := _decorators(group, _SERIALIZERS):
        problems.append(f'{group.__name__}: no serializers or computed fields, {found}')
    for name, field in group.model_fields.items():
        where = f'{group.__name__}.{name}'
        tp, _ = optional(field.annotation)
        if _has_alias(field):
            problems.append(f'{where}: a field of a group has no alias')
        elif place := _place_problems(group, name, path, seen):
            problems.extend(f'{where}: {problem}' for problem in place)
        elif (
            not is_list(group, name)
            and element(tp) is not None
            and not has_json_form(group, name)
        ):
            problems.append(
                f'{where}: a field of a group that is a dict, or a list that may '
                'be None, has a JSON form; its contents are not places'
            )
        elif field.default_factory_takes_validated_data:
            problems.append(
                f'{where}: the default_factory of a field of a group takes no other '
                'values'
            )
        elif not field.is_required() and (problem := _default_problem(group, name)):
            problems.append(f'{where}: {problem}')
    return problems


def _default_problem(model: type[BaseModel], name: str) -> str | None:
    """
    Why the default of the field ``name`` of ``model`` is not a value, or
    ``None``: what a request may write at the place, with no reference (see
    :func:`ess.spec.places.held`), and the value that validating it gives,
    since pydantic does not validate a default, and a binding gets it as it
    is. So a group's default is an instance, not the object of its fields.
    """
    default = model.model_fields[name].get_default(call_default_factory=True)
    try:
        held(model, name, default, _no_ref)
        made = validated(model, name, default)
    except ParamsError as error:
        return f'the default is not a value a request may write: {error}'
    if made != default:
        return f'the default {default!r} is not the value validating it gives, {made!r}'
    return None


def _no_ref(loc: Loc, ref: Ref, tp: Any) -> Any:
    raise ParamsError([(loc, f'{ref} is a reference; a default is a value')])


def _outputs_problems(model: type[BaseModel]) -> list[str]:
    return [
        f'{model.__name__}.{name}: an output is a type, possibly optional, '
        'or a list or a dict of one'
        for name, field in model.model_fields.items()
        if not is_single(value_type(field.annotation))
    ]


def _tables_problems(spec: WorkflowSpec[Any, Any]) -> list[str]:
    fields = spec.params.model_fields
    problems = [
        f'{name}: a table named twice'
        for name in sorted(set(spec.tables))
        if spec.tables.count(name) > 1
    ]
    for name in dict.fromkeys(spec.tables):
        if (field := fields.get(name)) is None:
            problems.append(f'{name}: not a parameter of {spec.name}')
        elif get_origin(field.annotation) is not list:
            problems.append(f'{name}: a table is a list parameter')
        elif not field.is_required():
            problems.append(f'{name}: a table has no default')
    return problems


def _params_schema(spec: WorkflowSpec[Any, Any]) -> dict[str, Any]:
    """
    The JSON Schema of the params: that of a model of the same fields, each
    with its title, description, default, and length constraints, and with
    the type at each place replaced by the forms it takes (see
    :func:`_field_forms`). As the backend does, it refuses a field that a
    model lacks.
    """
    model: type[BaseModel] = spec.params
    fields: dict[str, Any] = {}
    for name, info in model.model_fields.items():
        fields[name] = (
            _field_forms(model, name),
            Field(
                # Undefined, and so required, if the field is.
                default=info.get_default(call_default_factory=True),
                title=info.title or name.replace('_', ' ').title(),
                description=info.description,
            ),
        )
    schema: dict[str, Any] = create_model(
        model.__name__, __config__=model.model_config, **fields
    ).model_json_schema()
    for name, prop in schema.get('properties', {}).items():
        # pydantic leaves out the title of a field that is a $ref, if it is
        # the one it would make of the name.
        prop.setdefault('title', fields[name][1].title)
    for group in [schema, *schema.get('$defs', {}).values()]:
        if 'properties' in group:
            group['additionalProperties'] = False
    return schema


def _not_a_ref(schema: dict[str, Any]) -> None:
    """An inline value's schema: an object with the key ``@ref`` is a reference."""
    schema['not'] = {'required': ['@ref']}


def _ref_form(tp: Any) -> Any:
    """The reference a value of type ``tp`` takes, with the name of ``tp``."""
    data_type = Field(json_schema_extra={'dataType': type_name(tp)})
    return Annotated[
        DatasetRef if is_file(tp) else OutputRef | AccumulatorRef, data_type
    ]


def _field_forms(model: type[BaseModel], name: str) -> Any:
    """
    The type of the field ``name`` of ``model`` with the type at each of its
    places replaced by the forms it takes (see :func:`_forms`).
    """
    info = model.model_fields[name]
    metadata = list(info.metadata)
    if info.discriminator is not None:
        metadata.append(Field(discriminator=info.discriminator))
    return _forms(
        info.annotation, metadata, has_json_form(model, name), is_list(model, name)
    )


def _forms(tp: Any, metadata: Sequence[Any], json: bool, rows: bool) -> Any:
    """
    ``tp``, the type at a place, replaced by the forms it takes: a file by a
    reference to a dataset; a group by the forms of its fields (see
    :func:`_group_forms`), a union of groups by those of its alternatives;
    any other type by itself if ``json``, it has a JSON form; and each but a
    file also by a reference to an output, unless it holds a file, or for a
    union of groups, unless each alternative holds one. The elements of a
    list are places if ``rows``; the contents of any other list or dict are
    not, so it is given inline only. ``metadata``, the constraints of the
    field, goes to the list if ``tp`` is one, else to the value, so that a
    constraint of a number, say, is not put on a reference.
    """
    origin, args = get_origin(tp), get_args(tp)
    if origin is Annotated:
        return _forms(args[0], [*metadata, *tp.__metadata__], json, rows)
    if origin is list and rows:
        row = _forms(args[0], [], json, rows=False)
        listed: Any = list[row]  # type: ignore[valid-type]
        return Annotated[listed, *metadata] if metadata else listed
    if origin in (Union, UnionType) and NoneType in args:
        rest = tuple(arg for arg in args if arg is not NoneType)
        inner = rest[0] if len(rest) == 1 else Union[rest]  # noqa: UP007
        return Union[_forms(inner, metadata, json, rows), None]  # noqa: UP007
    value = Annotated[tp, *metadata] if metadata else tp
    if element(tp) is not None:
        return value
    if is_file(tp):
        return _ref_form(tp)
    ref = None if holds_file(tp) else _ref_form(tp)
    if is_group(tp):
        group = _group_forms(base(tp))
        value = Annotated[group, *metadata, Field(json_schema_extra=_not_a_ref)]
    elif (union := alternatives(value)) is not None:
        tag, groups = union
        members = tuple(groups.values())
        forms = tuple(_group_forms(base(m), tag) for m in members)
        # An output fits the union if it fits an alternative without a file.
        if fileless := tuple(m for m in members if not holds_file(m)):
            ref = _ref_form(Union[fileless])  # noqa: UP007
        # Only an object reads as a reference; JSON Schema's ``required``
        # holds for any other value, so ``not`` would refuse it.
        value = Annotated[
            Union[forms],  # noqa: UP007
            *metadata,
            Field(json_schema_extra=_not_a_ref),
        ]
    elif not json:
        return ref
    return value if ref is None else Union[value, ref]  # noqa: UP007


@functools.cache
def _group_forms(group: type[BaseModel], tag: str | None = None) -> Any:
    """
    A model of the fields of a group, each replaced by the forms it takes
    (see :func:`_field_forms`); the tag field ``tag`` of an alternative of a
    union by itself, since a tag is written inline only, and required, since
    a request always writes it. The title of a tag labels its alternative,
    so a tag without one is titled by its value, as a parameter is by its
    name; pydantic's title, made of the name of the field, would be that of
    every alternative.
    """
    fields: dict[str, Any] = {}
    for name, info in group.model_fields.items():
        title = info.title
        if name == tag and title is None:
            title = get_args(info.annotation)[0].replace('_', ' ').title()
        fields[name] = (
            info.annotation if name == tag else _field_forms(group, name),
            Field(
                default=PydanticUndefined
                if name == tag
                else info.get_default(call_default_factory=True),
                title=title,
                description=info.description,
                examples=info.examples,
                **(
                    {}
                    if info.json_schema_extra is None
                    else {'json_schema_extra': info.json_schema_extra}
                ),
            ),
        )
    return create_model(
        group.__name__, __config__=group.model_config, __doc__=group.__doc__, **fields
    )


def _outputs_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The schema of the outputs model, by the type of each output."""
    return {
        'title': model.__name__,
        'type': 'object',
        'properties': {n: _output_property(f) for n, f in model.model_fields.items()},
        'required': [n for n, f in model.model_fields.items() if f.is_required()],
    }


def _output_property(field: FieldInfo) -> dict[str, Any]:
    """The schema of an output, by the type of each value."""
    prop = _output_type_schema(field.annotation)
    if field.title is not None:
        prop['title'] = field.title
    if field.description is not None:
        prop['description'] = field.description
    if not field.is_required():
        prop['default'] = field.get_default(call_default_factory=True)
    for min_len in (m for m in field.metadata if isinstance(m, MinLen)):
        prop['minItems'] = min_len.min_length
    return prop


def _output_type_schema(annotation: Any) -> dict[str, Any]:
    tp, is_optional = optional(annotation)
    if (item := element(tp)) is None:
        schema: dict[str, Any] = {'dataType': type_name(tp)}
    elif get_origin(tp) is list:
        schema = {'type': 'array', 'items': _output_type_schema(item)}
    else:
        schema = {'type': 'object', 'additionalProperties': _output_type_schema(item)}
    return {'anyOf': [schema, {'type': 'null'}]} if is_optional else schema
