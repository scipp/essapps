# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Workflow specifications: implementation-independent workflow metadata.

A :class:`WorkflowSpec` describes a workflow's user-facing interface — identity,
display metadata, and three pydantic models — without reference to how or
where the workflow is computed:

=======  =======================  ====================  =========================
Model    Holds                    In a request          When the workflow runs
=======  =======================  ====================  =========================
params   configuration: literals  the values            the same values
inputs   data only                a reference per value the data the references name
outputs  results                  (none)                the data the workflow returns
=======  =======================  ====================  =========================

A request lives in two phases. In the *request phase*, before any data is
read, an input is a reference (see :mod:`ess.spec.refs`); in the *call
phase*, the backend reads the data each reference names and builds the
inputs model from them. Params are the same in both phases; the inputs model
exists as an instance only in the call phase, so every field of it holds the
type it declares.

The models are ordinary pydantic models and carry no annotations for the
framework: what the framework needs, it reads from the field types (see
:mod:`ess.spec.field_types`). The rules on them, checked when a spec is made:

- The names of params, inputs, and outputs are distinct, and so are their
  types, with ``| None`` and ``list`` or ``dict`` removed: the type is the
  sciline key of the value. Two parameters of one model get a ``NewType`` each.
- Each parameter is a *group*, a model of literals, possibly optional, or a
  list of groups. Every value in the params model has a JSON form, and none
  is a file: a file is always given by reference, as an input.
- Each input is a data value, possibly optional, or a list of data values. A
  data value is any type but a list, a dict, or a union, after its
  ``NewType``s are removed: a file (``Path``, or a ``NewType`` of it) or an
  object (a scipp type, a model, a ``NewType`` of either). The default of an
  input is ``None`` or an empty list.
- Each output is a data value, possibly optional, or a list or a dict of them.
- The params and inputs models have no pydantic decorators but model
  validators: no field validators, serializers, or computed fields. A check
  of one value belongs to the value's type, such as a field validator of a
  group or of a model that is a data value; so does its JSON form, which the
  framework stores and reads back.
- The index of a table is required, with ``Field(min_length=1)``, or empty by
  default.

A check across fields is a model validator. One of
the params model runs in the request phase of every request; one of the
inputs model runs in the call phase of a plain request only. A check across
params and inputs is the binding's: it fails when the request runs.

A *table* is a set of list fields of equal length, pushed together into an
accumulator. Its *index* is the field that names it, such as the run; its
other *columns* are attached to the index. ``tables`` maps each index to its
attached columns; :meth:`WorkflowSpec.tabulate` makes a multi-run spec from a
single-run one by naming them. A list field is a table only where ``tables``
names it.

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

from collections.abc import Collection, Mapping, Sequence
from types import NoneType
from typing import Annotated, Any, Literal, Self, Union, get_args, get_origin

from annotated_types import MinLen
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    create_model,
    field_validator,
    model_validator,
)
from pydantic.errors import PydanticInvalidForJsonSchema
from pydantic.fields import FieldInfo

from .field_types import (
    base,
    element,
    is_data_value,
    is_file,
    optional,
    type_name,
    value_type,
)

Side = Literal['params', 'inputs']


class NoParams(BaseModel):
    """
    Params model for workflows that take no configuration.

    Workflows always have a params model, so consumers never branch on its
    absence; "takes no parameters" is expressed as a model with no fields.
    Extra fields are rejected so that sending params to such a workflow is an
    error rather than silently ignored.
    """

    model_config = ConfigDict(extra='forbid')


class NoInputs(BaseModel):
    """Inputs model for workflows that take no data; see :class:`NoParams`."""

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
            "consumer could observe: params, inputs, outputs, or semantics."
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


class WorkflowSpec[P: BaseModel, I: BaseModel, O: BaseModel](_SpecFields, frozen=True):
    """
    Implementation-independent specification of a workflow's user interface.

    Holds identity and display metadata and the pydantic model classes of the
    workflow's params, inputs, and outputs. Contains no factory, no executor,
    and no reference to any workflow implementation. The module defining a
    spec must therefore be importable without importing the workflow code, so
    that a service can load and validate every spec it knows without that
    code.

    The spec is generic over its models, which a type checker infers from
    ``params=``, ``inputs=``, and ``outputs=``, so that a binding's
    ``compute(params: P, inputs: I) -> O`` is checked against them. Code
    that takes any spec annotates it ``WorkflowSpec[Any, Any, Any]``.
    """

    # A type checker cannot check a default against P or I, which only
    # params= and inputs= decide; pyright honours this mypy ignore too.
    params: type[P] = Field(  # type: ignore[assignment]
        default=NoParams,
        description=(
            "Pydantic model class of the workflow's configuration, each field "
            "a group or a list of groups. Defaults to :class:`NoParams`."
        ),
    )
    inputs: type[I] = Field(  # type: ignore[assignment]
        default=NoInputs,
        description=(
            "Pydantic model class of the workflow's data, each field a data "
            "value or a list of them. Defaults to :class:`NoInputs`."
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
            *_distinct(self.params, self.inputs, self.outputs),
            *_params_problems(self.params),
            *_inputs_problems(self.inputs),
            *_outputs_problems(self.outputs),
            *_tables_problems(self),
        ]
        if problems:
            raise ValueError(f'{self.name}: {"; ".join(problems)}')
        return self

    def side(self, name: str) -> Side:
        """Whether ``name`` is a parameter or an input; ``KeyError`` if neither."""
        if name in self.params.model_fields:
            return 'params'
        if name in self.inputs.model_fields:
            return 'inputs'
        raise KeyError(f'{name!r}: not a parameter or an input of {self.name}')

    def field(self, name: str) -> FieldInfo:
        """The field of a parameter or an input; ``KeyError`` if neither."""
        return getattr(self, self.side(name)).model_fields[name]

    def columns(self, index: str) -> tuple[str, ...]:
        """The columns of the table of ``index``, the index first."""
        return (index, *self.tables[index])

    def table_of(self, name: str) -> str | None:
        """The index of the table that holds the field ``name``, if any."""
        for index, attached in self.tables.items():
            if name == index or name in attached:
                return index
        return None

    def blank_fields(self, blanks: Collection[str]) -> frozenset[str]:
        """
        The parameters and inputs that ``blanks`` leave open: a blank that is
        an index stands for every column of its table, the index among them.
        """
        return frozenset(
            name
            for blank in blanks
            for name in (self.columns(blank) if blank in self.tables else (blank,))
        )

    def split(self, values: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """
        ``values`` by name, split by side into the parameters and the inputs;
        ``KeyError`` for a name that is neither.
        """
        sides: dict[Side, dict[str, Any]] = {'params': {}, 'inputs': {}}
        for name, value in values.items():
            sides[self.side(name)][name] = value
        return sides['params'], sides['inputs']

    def tabulate(
        self,
        *,
        name: str,
        version: int,
        tables: Sequence[str] | Mapping[str, Sequence[str]],
        title: str | None = None,
        description: str | None = None,
    ) -> WorkflowSpec[Any, Any, O]:
        """
        A spec over tables, made from this one by naming the indexes of its
        tables, each with the columns attached to it::

            IOFQ.tabulate(name='sans-iofq-multi', version=1, tables=('sample', 'can'))
            SANS.tabulate(name='sans-multi', version=1,
                          tables={'sample': ('sample_geometry',), 'can': ()})

        Each field named becomes a ``list`` on its own side. An index is
        ``X``, or ``X | None = None``, and loses ``| None``:
        ``sample: SampleFile`` becomes ``list[SampleFile]`` with at least one
        row, and ``can: CanFile | None = None`` becomes ``list[CanFile]``,
        empty by default. An attached column keeps its type as written,
        ``list[SampleGeometry | None]``, and has no default. Where its type
        is optional, as here, the framework fills a column left out with
        ``None`` in each row; a column of another type is given. The
        validators and constraints of a field named, such as an
        ``AfterValidator`` or ``Field(gt=0)``, check each row. Title,
        description, examples (each as one row), and ``json_schema_extra`` of
        a field named are kept. The other fields, the outputs, and the field
        validators of the types are as in this spec. A model with a field
        named is made anew, without this spec's model validators, which read
        fields whose types change; a model without one is kept. The new
        models have no static type.

        With one row in each table whose index is given, and none where it is
        left out, a request of the new spec is a request of this one (see
        :func:`ess.spec.testing.check_one_row`).
        """
        if self.tables:
            raise ValueError(f'{self.name} has tables already')
        named = _as_tables(tables)
        names = {n for index, attached in named.items() for n in (index, *attached)}
        fields = self.params.model_fields.keys() | self.inputs.model_fields.keys()
        if unknown := sorted(names - fields):
            raise ValueError(f'{unknown}: not parameters or inputs of {self.name}')
        if lists := sorted(n for n in names if element(self.field(n).annotation)):
            raise ValueError(f'{lists}: lists already, cannot be columns')
        if misshapen := sorted(i for i in named if not _index_shape(self.field(i))):
            raise ValueError(f'{misshapen}: an index is X, or X | None = None')
        attached = {n for columns in named.values() for n in columns}
        return WorkflowSpec(
            name=name,
            version=version,
            title=title or self.title,
            description=description or self.description,
            code_revision=self.code_revision,
            params=_tabulated(self.params, named.keys(), attached),
            inputs=_tabulated(self.inputs, named.keys(), attached),
            outputs=self.outputs,
            tables=named,
        )

    def serialize(self) -> SerializedWorkflowSpec:
        """
        Project to the plain-data form.

        The params schema is the params model's JSON Schema. The inputs and
        outputs schemas are made from the field types: each value is a
        reference to data of a type, named in ``dataType``. The projection is
        one-way: pydantic validators do not survive it, so a consumer of the
        serialized form can validate only optimistically.
        """
        return SerializedWorkflowSpec(
            name=self.name,
            version=self.version,
            title=self.title,
            description=self.description,
            code_revision=self.code_revision,
            tables=self.tables,
            params_schema=self.params.model_json_schema(),
            inputs_schema=_data_schema(self.inputs),
            outputs_schema=_data_schema(self.outputs),
        )


class SerializedWorkflowSpec(_SpecFields, frozen=True):
    """
    Plain-data form of a workflow spec, safe to send across process boundaries.

    Produced by :meth:`WorkflowSpec.serialize`; round-trips through JSON. A form
    shows the params schema, and a picker per input the inputs schema: each
    input property names the type of the data it takes in ``dataType``, a list
    field as an array of them, an optional one with ``null`` in ``anyOf``.
    """

    params_schema: dict[str, Any] = Field(
        description="JSON Schema of the workflow's params model."
    )
    inputs_schema: dict[str, Any] = Field(
        description="Schema of the workflow's inputs, by the type of each value."
    )
    outputs_schema: dict[str, Any] = Field(
        description="Schema of the workflow's outputs, by the type of each value."
    )


def _distinct(*models: type[BaseModel]) -> list[str]:
    """Refuse a name, or a type, of two fields of the spec."""
    problems = []
    names: dict[str, str] = {}
    keys: dict[Any, str] = {}
    for model in models:
        for name, field in model.model_fields.items():
            if (other := names.setdefault(name, model.__name__)) != model.__name__:
                problems.append(f'{name}: a field of both {other} and {model.__name__}')
            tp = value_type(field.annotation)
            if (first := keys.setdefault(tp, name)) != name:
                problems.append(
                    f'{first} and {name}: both of type {type_name(tp)}; give '
                    'each a NewType of its own'
                )
    return problems


def _one_or_list(annotation: Any) -> bool:
    """
    Whether ``annotation`` is ``X``, ``X | None``, ``list[X]``, or
    ``list[X | None]``: the shapes of a parameter and of an input.
    """
    tp, is_optional = optional(annotation)
    return element(tp) is None or (get_origin(tp) is list and not is_optional)


def _model_validators_only(model: type[BaseModel]) -> list[str]:
    """
    Refuse a decorator of the params or the inputs model itself but a model
    validator: a field validator, a field or model serializer, or a computed
    field.
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
        group = base(value_type(field.annotation))
        if not (
            _one_or_list(field.annotation)
            and isinstance(group, type)
            and issubclass(group, BaseModel)
        ):
            problems.append(
                f'{model.__name__}.{name}: a parameter is a group, a model of '
                'literals, possibly optional, or a list of groups'
            )
        elif _holds_file(group, set()):
            problems.append(
                f'{model.__name__}.{name}: a parameter holds no file; a file is '
                'an input'
            )
    if not problems:
        try:
            model.model_json_schema()
        except PydanticInvalidForJsonSchema as error:
            problems.append(
                f'{model.__name__}: every parameter has a JSON form: {error}'
            )
    return problems


def _holds_file(tp: Any, seen: set[type[BaseModel]]) -> bool:
    """
    Whether a type, or a field of a model in it, is a file; ``seen`` holds the
    models looked into already, so that a recursive model ends.
    """
    tp = base(tp)
    if is_file(tp):
        return True
    if isinstance(tp, type) and issubclass(tp, BaseModel):
        if tp in seen:
            return False
        seen.add(tp)
        return any(_holds_file(f.annotation, seen) for f in tp.model_fields.values())
    return any(_holds_file(arg, seen) for arg in getattr(tp, '__args__', ()))


def _inputs_problems(model: type[BaseModel]) -> list[str]:
    problems = _model_validators_only(model)
    for name, field in model.model_fields.items():
        if not (
            _one_or_list(field.annotation)
            and is_data_value(value_type(field.annotation))
        ):
            problems.append(
                f'{model.__name__}.{name}: an input is a data value, possibly '
                'optional, or a list of data values'
            )
        elif not field.is_required():
            default = field.get_default(call_default_factory=True)
            if not (default is None or (isinstance(default, list) and not default)):
                problems.append(
                    f'{model.__name__}.{name}: the default of an input is None '
                    'or an empty list'
                )
    return problems


def _outputs_problems(model: type[BaseModel]) -> list[str]:
    return [
        f'{model.__name__}.{name}: an output is a data value, possibly '
        'optional, or a list or a dict of them'
        for name, field in model.model_fields.items()
        if not is_data_value(value_type(field.annotation))
    ]


def _tables_problems(spec: WorkflowSpec[Any, Any, Any]) -> list[str]:
    problems = []
    seen: set[str] = set()
    for index, attached in spec.tables.items():
        for name in (index, *attached):
            if name in seen:
                problems.append(f'{name}: a column of two tables')
            seen.add(name)
            try:
                field = spec.field(name)
            except KeyError as error:
                problems.append(error.args[0])
                continue
            if get_origin(field.annotation) is not list:
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
    """``model`` with the fields named as columns, or itself if it has none."""
    if not (model.model_fields.keys() & {*indexes, *attached}):
        return model
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


def _data_schema(model: type[BaseModel]) -> dict[str, Any]:
    """A schema of a model of data values, by the type of each value."""
    properties = {}
    for name, field in model.model_fields.items():
        prop = _data_type_schema(field.annotation)
        if field.title is not None:
            prop['title'] = field.title
        if field.description is not None:
            prop['description'] = field.description
        if not field.is_required():
            prop['default'] = field.get_default(call_default_factory=True)
        for min_len in (m for m in field.metadata if isinstance(m, MinLen)):
            prop['minItems'] = min_len.min_length
        properties[name] = prop
    required = [n for n, f in model.model_fields.items() if f.is_required()]
    return {
        'title': model.__name__,
        'type': 'object',
        'properties': properties,
        'required': required,
    }


def _data_type_schema(annotation: Any) -> dict[str, Any]:
    tp, is_optional = optional(annotation)
    if (item := element(tp)) is None:
        schema: dict[str, Any] = {'dataType': type_name(tp)}
    elif get_origin(tp) is list:
        schema = {'type': 'array', 'items': _data_type_schema(item)}
    else:
        schema = {'type': 'object', 'additionalProperties': _data_type_schema(item)}
    return {'anyOf': [schema, {'type': 'null'}]} if is_optional else schema
