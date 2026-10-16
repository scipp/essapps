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
params   everything the workflow        a literal or a   the values, and the data
         takes: settings, files, data   reference each   the references name
outputs  results                        (none)           the data the workflow
                                                         returns
=======  =============================  ===============  ==========================

A parameter's type decides the form a request gives it in. A *group*, a
model of literals, is given by value, as a *literal* in its JSON form; any
other type, a file (``Path``, or a ``NewType`` of it) or an object such as a
scipp array or a number, is a *data value*, given by reference (see
:mod:`ess.spec.refs`). A request lives in two phases. In the *request phase*,
before any data is read, a data value is a reference; in the *call phase*,
the backend reads the data each reference names and builds the params
instance from the values and the data, so every field of it holds the type
it declares.

The models are ordinary pydantic models and carry no annotations for the
framework: what the framework needs, it reads from the field types (see
:mod:`ess.spec.field_types`). The rules on them, checked when a spec is made:

- The names of params and outputs are distinct.
- Each parameter's type is a type, possibly optional, or a list of one; not
  a dict or a union of two types. With ``| None`` removed and, for a list
  parameter, its element type taken, a model that pydantic makes a JSON
  schema of, with the params model's config, is a group; any other type is a
  data value. A group holds no file: a file is given by reference to a
  dataset.
- A default is a value, never a reference: of a group, a value whose JSON
  form reads back as the value and is not a reference; of a data value,
  ``None`` or an empty list.
- No parameter sees another: each is validated alone, in every phase. So
  the params model has no field validators, serializers, computed fields, or
  aliases, and no ``default_factory`` that takes the other values. A list
  parameter's field carries only length constraints, ``Field(min_length=2)``:
  a check of one row belongs to the element type. A check of one value
  belongs to the value's type, such as a field validator of a group; so does
  its JSON form, which the framework stores and reads back.
- Each output is a data value, possibly optional, or a list or a dict of them.
- The index of a table is required, with ``Field(min_length=1)``, or empty by
  default.

A check across parameters is the binding's or the driver's, and fails when
the request runs. The params model may state one as a model validator, but
the framework never calls it: a stage and a driver never get a whole params
instance. A binding may run it, ``IofQParams.model_validate(dict(params))``.

A *table* is a set of list parameters of equal length, pushed together into
an accumulator. Its *index* is the parameter that names it, such as the run;
its other *columns* are attached to the index. ``tables`` maps each index to
its attached columns; :meth:`WorkflowSpec.tabulate` makes a multi-run spec
from a single-run one by naming them. A list parameter is a table only where
``tables`` names it.

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
from collections.abc import Collection, Mapping, Sequence
from types import NoneType
from typing import Annotated, Any, Self, Union, get_args, get_origin

from annotated_types import MaxLen, MinLen
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    create_model,
    field_validator,
    model_validator,
)
from pydantic.errors import PydanticInvalidForJsonSchema
from pydantic.fields import FieldInfo
from pydantic_core import PydanticSerializationError, from_json, to_json

from .field_types import (
    base,
    element,
    is_data_value,
    is_file,
    model_of,
    optional,
    type_name,
    value_type,
)


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
    tables: dict[str, tuple[str, ...]] = Field(
        default_factory=dict,
        description=(
            "The tables, each as its index and the columns attached to it. A "
            "sequence of indexes is a table each, without attached columns."
        ),
    )

    def __hash__(self) -> int:
        # The tables are a dict, which pydantic's hash of all fields refuses;
        # equal specs have equal identities, so these suffice.
        return hash((type(self), self.name, self.version))

    @field_validator('tables', mode='before')
    @classmethod
    def _tables_of_names(cls, tables: Any) -> Any:
        if isinstance(tables, Sequence) and not isinstance(tables, str):
            return _as_tables(tables)
        return tables


def _as_tables(
    tables: Sequence[str] | Mapping[str, Sequence[str]],
) -> dict[str, tuple[str, ...]]:
    """Tables by index; a sequence of indexes makes a table of each, alone."""
    if isinstance(tables, Mapping):
        return {index: tuple(attached) for index, attached in tables.items()}
    return dict.fromkeys(tables, ())


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

    def by_value(self, name: str) -> bool:
        """
        Whether a request gives the parameter ``name`` by value: its type is
        a group, or a list of groups. Any other parameter is a data value,
        given by reference.
        """
        return _group(self.params, name)

    def columns(self, index: str) -> tuple[str, ...]:
        """The columns of the table of ``index``, the index first."""
        return (index, *self.tables[index])

    def table_of(self, name: str) -> str | None:
        """The index of the table that holds the parameter ``name``, if any."""
        for index, attached in self.tables.items():
            if name == index or name in attached:
                return index
        return None

    def blank_fields(self, blanks: Collection[str]) -> frozenset[str]:
        """
        The parameters that ``blanks`` leave open: a blank that is an index
        stands for every column of its table, the index among them.
        """
        return frozenset(
            name
            for blank in blanks
            for name in (self.columns(blank) if blank in self.tables else (blank,))
        )

    def tabulate(
        self,
        *,
        name: str,
        version: int,
        tables: Sequence[str] | Mapping[str, Sequence[str]],
        title: str | None = None,
        description: str | None = None,
    ) -> WorkflowSpec[Any, O]:
        """
        A spec over tables, made from this one by naming the indexes of its
        tables, each with the columns attached to it::

            IOFQ.tabulate(name='sans-iofq-multi', version=1, tables=('sample', 'can'))
            SANS.tabulate(name='sans-multi', version=1,
                          tables={'sample': ('sample_geometry',), 'can': ()})

        Each parameter named becomes a ``list``. An index is ``X``, or
        ``X | None = None``, and loses ``| None``: ``sample: SampleFile``
        becomes ``list[SampleFile]`` with at least one row, and
        ``can: CanFile | None = None`` becomes ``list[CanFile]``, empty by
        default. An attached column keeps its type as written,
        ``list[SampleGeometry | None]``, and has no default. Where its type
        is optional, as here, the framework fills a column left out with
        ``None`` in each row; a column of another type is given. The
        validators and constraints of a parameter named, such as an
        ``AfterValidator`` or ``Field(gt=0)``, check each row: they are
        metadata of the element type. Title, description, examples (each as
        one row), and ``json_schema_extra`` of a parameter named are kept.
        The other parameters and the outputs are as in this spec. The params
        model is made anew, without this spec's model validators, which read
        fields whose types change, and has no static type.

        With one row in each table whose index is given, and none where it is
        left out, a request of the new spec is a request of this one (see
        :func:`ess.spec.testing.check_one_row`).
        """
        if self.tables:
            raise ValueError(f'{self.name} has tables already')
        named = _as_tables(tables)
        names = {n for index, attached in named.items() for n in (index, *attached)}
        fields = self.params.model_fields
        if unknown := sorted(names - fields.keys()):
            raise ValueError(f'{unknown}: not parameters of {self.name}')
        if lists := sorted(n for n in names if element(fields[n].annotation)):
            raise ValueError(f'{lists}: lists already, cannot be columns')
        if misshapen := sorted(i for i in named if not _index_shape(fields[i])):
            raise ValueError(f'{misshapen}: an index is X, or X | None = None')
        attached = {n for columns in named.values() for n in columns}
        return WorkflowSpec(
            name=name,
            version=version,
            title=title or self.title,
            description=description or self.description,
            code_revision=self.code_revision,
            params=_tabulated(self.params, named.keys(), attached),
            outputs=self.outputs,
            tables=named,
        )

    def serialize(self) -> SerializedWorkflowSpec:
        """
        Project to the plain-data form.

        The params schema is the JSON Schema of the parameters given by value,
        and has each other parameter as a reference to data of a type, named
        in ``dataType``, as the outputs schema has each output. The
        projection is one-way: pydantic validators do not survive it, so a
        consumer of the serialized form can validate only optimistically.
        """
        return SerializedWorkflowSpec(
            name=self.name,
            version=self.version,
            title=self.title,
            description=self.description,
            code_revision=self.code_revision,
            tables=self.tables,
            params_schema=_params_schema(self),
            outputs_schema=_data_schema(self.outputs),
        )


class SerializedWorkflowSpec(_SpecFields, frozen=True):
    """
    Plain-data form of a workflow spec, safe to send across process boundaries.

    Produced by :meth:`WorkflowSpec.serialize`; round-trips through JSON. A form
    shows the params schema: a parameter given by value by its JSON Schema,
    and a picker for each other parameter, whose property names the type of
    the data it takes in ``dataType``, a list parameter as an array of them,
    an optional one with ``null`` in ``anyOf``.
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
        if not (_one_or_list(field.annotation) and is_data_value(tp)):
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
        if _is_model(base(tp)) and _json_schema(tp, model) and _holds_file(tp, set()):
            problems.append(
                f'{where}: a group holds no file; a file is given by reference '
                'to a dataset'
            )
        elif field.default_factory_takes_validated_data:
            problems.append(
                f'{where}: the default_factory of a parameter takes no other values'
            )
        elif not field.is_required() and (problem := _default_problem(model, name)):
            problems.append(f'{where}: {problem}')
    return problems


def _json_schema(tp: Any, model: type[BaseModel]) -> bool:
    """Whether pydantic makes a JSON schema of ``tp``, with the config of ``model``."""
    try:
        create_model(
            'Value', __config__=model.model_config, value=(tp, ...)
        ).model_json_schema()
    except PydanticInvalidForJsonSchema:
        return False
    return True


@functools.cache
def _group(model: type[BaseModel], name: str) -> bool:
    """
    Whether the type of the field ``name`` of ``model``, with ``| None``
    removed and for a list its element type taken, is a group, or a
    ``NewType`` of one: a model that pydantic makes a JSON schema of, holding
    no file.
    """
    tp = value_type(model.model_fields[name].annotation)
    return (
        _is_model(base(tp)) and not _holds_file(tp, set()) and _json_schema(tp, model)
    )


def _is_model(tp: Any) -> bool:
    return isinstance(tp, type) and issubclass(tp, BaseModel)


def _default_problem(model: type[BaseModel], name: str) -> str | None:
    """
    Why the default of the field ``name`` of ``model`` is not a value, or
    ``None``: of a group, a value whose JSON form reads back as the value and
    is not a reference, nor a list holding one; of a data value, ``None`` or
    an empty list.
    """
    field = model.model_fields[name]
    default = field.get_default(call_default_factory=True)
    if not _group(model, name):
        if default is None or (isinstance(default, list) and not default):
            return None
        return 'the default of a data value is None or an empty list'
    try:
        line = to_json({name: default})
        again = model_of(model, name).model_validate_json(line)
        reads_back = getattr(again, name) == default
    except (PydanticSerializationError, ValidationError):
        reads_back = False
    if not reads_back:
        return 'the default has no JSON form that reads back as the value'
    value = from_json(line)[name]
    if any(isinstance(v, dict) and '@ref' in v for v in _tops(value)):
        return 'a default is a value, not a reference'
    return None


def _tops(value: Any) -> list[Any]:
    """Where a request may hold a reference: the value, or each element of a list."""
    return value if isinstance(value, list) else [value]


def _holds_file(tp: Any, seen: set[type[BaseModel]]) -> bool:
    """
    Whether a type, or a field of a model in it, is a file; ``seen`` holds the
    models looked into already, so that a recursive model ends.
    """
    tp = base(tp)
    if is_file(tp):
        return True
    if _is_model(tp):
        if tp in seen:
            return False
        seen.add(tp)
        return any(_holds_file(f.annotation, seen) for f in tp.model_fields.values())
    return any(_holds_file(arg, seen) for arg in getattr(tp, '__args__', ()))


def _outputs_problems(model: type[BaseModel]) -> list[str]:
    return [
        f'{model.__name__}.{name}: an output is a data value, possibly '
        'optional, or a list or a dict of them'
        for name, field in model.model_fields.items()
        if not is_data_value(value_type(field.annotation))
    ]


def _tables_problems(spec: WorkflowSpec[Any, Any]) -> list[str]:
    problems = []
    seen: set[str] = set()
    fields = spec.params.model_fields
    for index, attached in spec.tables.items():
        for name in (index, *attached):
            if name in seen:
                problems.append(f'{name}: a column of two tables')
            seen.add(name)
            if (field := fields.get(name)) is None:
                problems.append(f'{name}: not a parameter of {spec.name}')
            elif get_origin(field.annotation) is not list:
                problems.append(f'{name}: a column of a table is a list')
            elif name == index and optional(element(field.annotation))[1]:
                problems.append(f'{index}: the index of a table holds no None')
            elif name == index and field.is_required():
                if not any(
                    isinstance(m, MinLen) and m.min_length >= 1 for m in field.metadata
                ):
                    problems.append(
                        f'{index}: a required index has Field(min_length=1)'
                    )
            elif name == index:
                default = field.get_default(call_default_factory=True)
                if not (isinstance(default, list) and not default):
                    problems.append(
                        f'{index}: the index of a table is required, or empty by '
                        'default'
                    )
    return problems


def _tabulated(
    model: type[BaseModel], indexes: Sequence[str], attached: set[str]
) -> type[BaseModel]:
    """``model`` with the fields named as columns."""
    fields: dict[str, Any] = {}
    for name, info in model.model_fields.items():
        if name not in indexes and name not in attached:
            fields[name] = (info.annotation, info)
            continue
        meta = {
            'title': info.title,
            'description': info.description,
            'json_schema_extra': info.json_schema_extra,
            # An example of a column is one row of it.
            'examples': None if info.examples is None else [[e] for e in info.examples],
        }
        # A row is a value of the field, so the field's validators and
        # constraints check each row.
        if name in attached:
            row, rows = info.annotation, {}
        elif info.is_required():
            row, rows = info.annotation, {'min_length': 1}
        else:  # X | None = None, as tabulate checks
            members = [a for a in get_args(info.annotation) if a is not NoneType]
            row = Union[tuple(members)]  # noqa: UP007
            rows = {'default_factory': list}
        if info.metadata:
            row = Annotated[row, *info.metadata]
        fields[name] = (list[row], Field(**rows, **meta))
    return create_model(
        f'Tabulated{model.__name__}', __config__=model.model_config, **fields
    )


def _index_shape(field: FieldInfo) -> bool:
    """Whether ``tabulate`` takes a field as an index: ``X``, or ``X | None = None``."""
    if field.is_required():
        return not optional(field.annotation)[1]
    return (
        optional(field.annotation)[1]
        and field.get_default(call_default_factory=True) is None
    )


def _params_schema(spec: WorkflowSpec[Any, Any]) -> dict[str, Any]:
    """
    The JSON Schema of the parameters given by value, with each other
    parameter as :func:`_data_schema` has it, in the order of the fields.
    """
    model: type[BaseModel] = spec.params
    fields = model.model_fields
    literals: dict[str, Any] = {
        n: (f.annotation, f) for n, f in fields.items() if spec.by_value(n)
    }
    schema: dict[str, Any] = create_model(
        model.__name__, __config__=model.model_config, **literals
    ).model_json_schema()
    by_value = schema.get('properties', {})
    schema['properties'] = {
        n: by_value[n] if n in literals else _data_property(f)
        for n, f in fields.items()
    }
    schema['required'] = [n for n, f in fields.items() if f.is_required()]
    return schema


def _data_schema(model: type[BaseModel]) -> dict[str, Any]:
    """A schema of a model of data values, by the type of each value."""
    return {
        'title': model.__name__,
        'type': 'object',
        'properties': {n: _data_property(f) for n, f in model.model_fields.items()},
        'required': [n for n, f in model.model_fields.items() if f.is_required()],
    }


def _data_property(field: FieldInfo) -> dict[str, Any]:
    """The schema of a field of data values, by the type of each value."""
    prop = _data_type_schema(field.annotation)
    if field.title is not None:
        prop['title'] = field.title
    if field.description is not None:
        prop['description'] = field.description
    if not field.is_required():
        prop['default'] = field.get_default(call_default_factory=True)
    for min_len in (m for m in field.metadata if isinstance(m, MinLen)):
        prop['minItems'] = min_len.min_length
    return prop


def _data_type_schema(annotation: Any) -> dict[str, Any]:
    tp, is_optional = optional(annotation)
    if (item := element(tp)) is None:
        schema: dict[str, Any] = {'dataType': type_name(tp)}
    elif get_origin(tp) is list:
        schema = {'type': 'array', 'items': _data_type_schema(item)}
    else:
        schema = {'type': 'object', 'additionalProperties': _data_type_schema(item)}
    return {'anyOf': [schema, {'type': 'null'}]} if is_optional else schema
