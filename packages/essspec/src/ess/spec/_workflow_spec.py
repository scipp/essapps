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

A request gives each value inline or by reference, and each row of a
list parameter. A parameter's type decides which of the two it takes, with
``| None`` removed and, for a list parameter, its element type taken (see
:meth:`WorkflowSpec.takes_inline`):

=====================================  ==========  ===========================
A parameter's type                     Inline      By reference
=====================================  ==========  ===========================
a file: ``Path``, or a ``NewType``     no          to a dataset
of it
has a *JSON form*: pydantic makes a    yes         to an output that fits
JSON schema of it, with the params
model's config, and it holds no file
a group that holds a file              field by    to a dataset, for its one
                                       field       required file
anything else, such as a scipp array   no          to an output that fits
=====================================  ==========  ===========================

An *inline value* is the value itself in the request, held in its JSON form;
a reference is described in :mod:`ess.spec.refs`. A *group* is a model, such
as ``Reduction``. A group that holds a file, such as a run and the values
given next to it, is given *field by field*: each of its fields inline or by
reference, as a parameter is, so the file comes from a dataset::

    class SampleRun(BaseModel):
        file: SampleFile
        transmission: TransmissionFraction | None = None

    {'sample': {'file': dataset(run=101), 'transmission': t.ref('transmission')}}
    {'sample': dataset(run=101)}  # the one required file; defaults for the rest

Like the params model, such a group is never validated whole. A request
lives in two phases. In the
*request phase*, before any data is read, a value given by reference is a
reference; in the *call phase*, the backend reads the data each reference
names and builds the params instance from the values and the data, so every
field of it holds the type it declares.

The models are ordinary pydantic models and carry no annotations for the
framework: what the framework needs, it reads from the field types (see
:mod:`ess.spec.field_types`). The rules on them, checked when a spec is made:

- The names of params and outputs are distinct.
- Each parameter's type is a type, possibly optional, or a list of one; not
  a dict or a union of two types.
- A group that holds a file holds it as a field, a file, possibly
  optional; it follows the rules of the params model below, and a field
  of it that is a list or a dict has a JSON form, since no reference stands
  for a list.
- A default is a value, never a reference: of a type with a JSON form, a
  value whose JSON form reads back as the value and is not a reference; of
  any other type, ``None`` or an empty list.
- No parameter sees another: each is validated alone, in every phase. So
  the params model has no field validators, serializers, computed fields, or
  aliases, and no ``default_factory`` that takes the other values. A list
  parameter's field carries only length constraints, ``Field(min_length=2)``:
  a check of one row belongs to the element type. A check of one value
  belongs to the value's type, such as a field validator of a group; so does
  its JSON form, which the framework stores and reads back.
- Each output is a type, possibly optional, or a list or a dict of one.
- Each table is a list parameter.

A check across parameters is the binding's or the driver's, and fails when
the request runs. The params model may state one as a model validator, but
the framework never calls it: a stage and a driver never get a whole params
instance. A binding may run it, ``IofQParams.model_validate(dict(params))``.

A *table* is a list parameter whose *rows* an accumulator takes a push at a
time, such as the sample runs; its rows are distinct. A row with more than
one value, such as a run and the values given next to it, is a group given
field by field. ``tables`` names the tables; :meth:`WorkflowSpec.tabulate`
makes a multi-run spec from a single-run one by naming them. A list
parameter is a table only where ``tables`` names it.

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
from collections.abc import Collection, Sequence
from types import NoneType, UnionType
from typing import Annotated, Any, Self, Union, get_args, get_origin

from annotated_types import MaxLen, MinLen
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    create_model,
    model_validator,
)
from pydantic.fields import FieldInfo
from pydantic_core import PydanticSerializationError

from .field_types import (
    base,
    by_field,
    element,
    file_field,
    has_json_form,
    holds_file,
    inline,
    is_file,
    is_group,
    is_list,
    is_single,
    optional,
    type_name,
    value_type,
)
from .refs import AccumulatorRef, DatasetRef, OutputRef


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

    def takes_inline(self, name: str) -> bool:
        """
        Whether a request may give the parameter ``name``, or each row of it
        if it is a list parameter, inline: its type, with ``| None`` removed
        and for a list parameter its element type taken, has a JSON form, or
        is a group given field by field (see
        :func:`ess.spec.field_types.by_field`).
        """
        tp = value_type(self.params.model_fields[name].annotation)
        return has_json_form(self.params, name) or by_field(tp)

    def has_json_form(self, name: str) -> bool:
        """
        Whether the type of the parameter ``name``, with ``| None`` removed
        and for a list parameter its element type taken, has a JSON form (see
        :func:`ess.spec.field_types.has_json_form`).
        """
        return has_json_form(self.params, name)

    def tabulate(
        self,
        *,
        name: str,
        version: int,
        tables: Sequence[str],
        empty: Collection[str] = (),
        title: str | None = None,
        description: str | None = None,
    ) -> WorkflowSpec[Any, O]:
        """
        A spec over tables, made from this one by naming its parameters that
        become tables::

            IOFQ.tabulate(name='sans-iofq-multi', version=1, tables=('sample', 'can'))

        Each parameter named becomes a ``list`` of its type as written, with
        at least one row, or, if ``empty`` names it, with none by default:
        ``sample: SampleRun`` becomes ``list[SampleRun]``, and
        ``can: CanFile | None = None`` with ``empty=('can',)`` becomes
        ``list[CanFile | None]``, empty by default. The validators and
        constraints of a parameter named, such as an ``AfterValidator`` or
        ``Field(gt=0)``, check each row: they are metadata of the element
        type. Title, description, examples (each as one row), and
        ``json_schema_extra`` of a parameter named are kept. The other
        parameters and the outputs are as in this spec. The params model is
        made anew, without this spec's model validators, which read fields
        whose types change, and has no static type.

        With one row in each table, a request of the new spec is a request of
        this one (see :func:`ess.spec.testing.check_one_row`).
        """
        if self.tables:
            raise ValueError(f'{self.name} has tables already')
        fields = self.params.model_fields
        if unknown := sorted(set(tables) - fields.keys()):
            raise ValueError(f'{unknown}: not parameters of {self.name}')
        if lists := sorted(n for n in tables if element(fields[n].annotation)):
            raise ValueError(f'{lists}: lists already, cannot be tables')
        if not_tables := sorted(set(empty) - set(tables)):
            raise ValueError(f'{not_tables}: empty, but not tables')
        return WorkflowSpec(
            name=name,
            version=version,
            title=title or self.title,
            description=description or self.description,
            code_revision=self.code_revision,
            params=_tabulated(self.params, tables, empty),
            outputs=self.outputs,
            tables=tuple(tables),
        )

    def serialize(self) -> SerializedWorkflowSpec:
        """
        Project to the plain-data form.

        The params schema has each value, or each row of a list parameter,
        in the forms it takes: ``anyOf`` its JSON schema and a reference if it
        takes an inline value, else a reference. A reference's schema is the
        ``@ref`` object of each kind it may be, with the name of the data type
        it must fit in ``dataType``, as the outputs schema has each output.
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
    by reference, and a picker for each parameter that takes a reference
    only, which names the type of the data it takes in ``dataType``; a list
    parameter as an array of these, an optional one with ``null`` in
    ``anyOf``.
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


def _model_validators_only(model: type[BaseModel]) -> list[str]:
    """
    Refuse a decorator of the params model itself but a model validator: a
    field validator, a field or model serializer, or a computed field.
    """
    decorators = model.__pydantic_decorators__
    if found := {
        kind: sorted(getattr(decorators, kind))
        for kind in (
            'validators',
            'field_validators',
            'field_serializers',
            'model_serializers',
            'computed_fields',
        )
        if getattr(decorators, kind)
    }:
        return [
            f'{model.__name__}: no decorators but model validators, {found}; '
            "the check or the JSON form of one value belongs to the value's type"
        ]
    return []


def _params_problems(model: type[BaseModel]) -> list[str]:
    problems = _model_validators_only(model)
    for name, field in model.model_fields.items():
        where = f'{model.__name__}.{name}'
        tp = value_type(field.annotation)
        if not (_one_or_list(field.annotation) and is_single(tp)):
            problems.append(
                f'{where}: a parameter is a type, possibly optional, or a list of one'
            )
            continue
        if any(
            getattr(field, a) is not None
            for a in ('alias', 'validation_alias', 'serialization_alias')
        ):
            problems.append(f'{where}: a parameter has no alias')
        if get_origin(optional(field.annotation)[0]) is list and (
            other := [m for m in field.metadata if not isinstance(m, MinLen | MaxLen)]
        ):
            problems.append(
                f'{where}: a list parameter carries only length constraints, '
                f'not {other}; a check of one row belongs to the element type'
            )
        if by_field(tp) and (group := _group_problems(base(tp))):
            problems.extend(f'{where}: {problem}' for problem in group)
        elif field.default_factory_takes_validated_data:
            problems.append(
                f'{where}: the default_factory of a parameter takes no other values'
            )
        elif not field.is_required() and (problem := _default_problem(model, name)):
            problems.append(f'{where}: {problem}')
    return problems


def _group_problems(group: type[BaseModel]) -> list[str]:
    """
    Refuse a group given field by field whose fields a request cannot give
    each inline or by reference, as the params model's: a decorator but a
    model validator, which the framework never calls, a field with an alias,
    a file deeper than a field, a list or a dict without a JSON form, which no
    reference stands for, and a default that is not a value, since a field
    left out holds its default.
    """
    problems = _model_validators_only(group)
    for name, field in group.model_fields.items():
        where = f'{group.__name__}.{name}'
        tp, _ = optional(field.annotation)
        if any(
            getattr(field, a) is not None
            for a in ('alias', 'validation_alias', 'serialization_alias')
        ):
            problems.append(f'{where}: a field of a group has no alias')
        elif holds_file(tp) and not is_file(tp):
            problems.append(
                f'{where}: a field of a group holds a file only as a file, '
                'possibly optional, given by reference to a dataset'
            )
        elif element(tp) is not None and not has_json_form(group, name):
            problems.append(
                f'{where}: a field of a group that is a list or a dict has a '
                'JSON form; no reference stands for a list'
            )
        elif not field.is_required() and (
            problem := _default_problem(group, name, whole=True)
        ):
            problems.append(f'{where}: {problem}')
    return problems


def _default_problem(
    model: type[BaseModel], name: str, whole: bool = False
) -> str | None:
    """
    Why the default of the field ``name`` of ``model`` is not a value, or
    ``None``: of a type with a JSON form, an inline value (see
    :func:`ess.spec.field_types.inline`), each row of a list unless
    ``whole``, as for a field of a group; of any other type, ``None`` or an
    empty list.
    """
    default = model.model_fields[name].get_default(call_default_factory=True)
    if not has_json_form(model, name):
        if default is None or (isinstance(default, list) and not default):
            return None
        return 'the default of a type without a JSON form is None or an empty list'
    rows = is_list(model, name) and not whole
    if rows and not isinstance(default, list):
        return 'the default of a list parameter is a list'
    try:
        for value in default if rows else [default]:
            inline(model, name, value, whole)
    except (PydanticSerializationError, ValidationError):
        return 'the default has no JSON form that reads back as the value'
    except ValueError as error:
        return f'the default {error}'
    return None


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
    return problems


def _tabulated(
    model: type[BaseModel], tables: Collection[str], empty: Collection[str]
) -> type[BaseModel]:
    """``model`` with the fields named as tables, each a list of its type."""
    fields: dict[str, Any] = {}
    for name, info in model.model_fields.items():
        if name not in tables:
            fields[name] = (info.annotation, info)
            continue
        meta = {
            'title': info.title,
            'description': info.description,
            'json_schema_extra': info.json_schema_extra,
            # An example of a table is one row of it.
            'examples': None if info.examples is None else [[e] for e in info.examples],
        }
        # A row is a value of the field, so the field's validators and
        # constraints check each row.
        row = (
            Annotated[info.annotation, *info.metadata]
            if info.metadata
            else info.annotation
        )
        rows = {'default_factory': list} if name in empty else {'min_length': 1}
        fields[name] = (list[row], Field(**rows, **meta))
    return create_model(
        f'Tabulated{model.__name__}', __config__=model.model_config, **fields
    )


def _params_schema(spec: WorkflowSpec[Any, Any]) -> dict[str, Any]:
    """
    The JSON Schema of the params: that of a model of the same fields, each
    with its title, description, default, and length constraints, and with
    its type replaced by the forms it takes (see :func:`_forms`). As the
    backend does, it refuses a field that a model lacks.
    """
    model: type[BaseModel] = spec.params
    fields: dict[str, Any] = {}
    for name, info in model.model_fields.items():
        tp = value_type(info.annotation)
        if not by_field(tp):
            ref = _ref_form(tp)
        elif file := file_field(base(tp)):
            ref = _ref_form(base(tp).model_fields[file].annotation)
        else:
            ref = None
        fields[name] = (
            _forms(info.annotation, info.metadata, ref, spec.takes_inline(name)),
            Field(
                # Undefined, and so required, if the field is.
                default=info.get_default(call_default_factory=True),
                # pydantic leaves out the title of a field that is a $ref only
                title=info.title or name.replace('_', ' ').title(),
                description=info.description,
            ),
        )
    schema: dict[str, Any] = create_model(
        model.__name__, __config__=model.model_config, **fields
    ).model_json_schema()
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


def _forms(
    tp: Any,
    metadata: Sequence[Any],
    ref: Any,
    takes_inline: bool,
    fields: bool = True,
) -> Any:
    """
    ``tp`` with its value type ``X`` replaced by ``X | ref`` if
    ``takes_inline``, else by ``ref``, so that each value, or each row of a
    list, takes the forms of ``X``; ``ref`` ``None`` takes no reference. A
    group ``X`` given inline, if ``fields``, has each field replaced by the
    forms it takes (see :func:`_group_forms`). ``metadata``, the constraints
    of the field, goes to the list if ``tp`` is one, else to ``X``, so that a
    constraint of a number, say, is not put on a reference.
    """
    origin, args = get_origin(tp), get_args(tp)
    if origin is Annotated:
        meta = [*metadata, *tp.__metadata__]
        return _forms(args[0], meta, ref, takes_inline, fields)
    if origin is list:
        row = _forms(args[0], [], ref, takes_inline, fields)
        rows: Any = list[row]  # type: ignore[valid-type]
        return Annotated[rows, *metadata] if metadata else rows
    if origin in (Union, UnionType):
        return Union[  # noqa: UP007
            tuple(
                arg
                if arg is NoneType
                else _forms(arg, metadata, ref, takes_inline, fields)
                for arg in args
            )
        ]
    if not takes_inline:
        return ref
    if not is_group(tp):
        value = Annotated[tp, *metadata] if metadata else tp
    else:
        # Only an object reads as a reference; JSON Schema's ``required``
        # holds for any other value, so ``not`` would refuse it.
        group = _group_forms(base(tp)) if fields and by_field(tp) else tp
        value = Annotated[group, *metadata, Field(json_schema_extra=_not_a_ref)]
    return value if ref is None else Union[value, ref]  # noqa: UP007


@functools.cache
def _group_forms(group: type[BaseModel]) -> type[BaseModel]:
    """
    A model of the fields of a group given field by field, each replaced by
    the forms it takes: a file by a reference to a dataset, a list or a dict
    by itself, inline only, and any other type by itself, if it has a JSON
    form, or a reference to an output that fits. A group in a field is given
    inline as its own JSON form, never field by field.
    """
    fields: dict[str, Any] = {}
    for name, info in group.model_fields.items():
        tp, _ = optional(info.annotation)
        if element(tp) is not None:
            forms = Annotated[info.annotation, *info.metadata]
        else:
            json = has_json_form(group, name)
            forms = _forms(
                info.annotation, info.metadata, _ref_form(tp), json, fields=False
            )
        fields[name] = (
            forms,
            Field(
                default=info.get_default(call_default_factory=True),
                title=info.title,
                description=info.description,
                examples=info.examples,
                # A reference's dataType is its json_schema_extra.
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
