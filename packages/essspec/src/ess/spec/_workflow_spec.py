# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Workflow specifications: the interface of a workflow, without its code.

A :class:`WorkflowSpec` names a workflow and holds two pydantic models: the
*params*, everything a request gives the workflow, and the *outputs*, the
results it returns. A spec of an I(Q) reduction::

    class RunFile(DatasetModel, kind='raw'):  # a file: from a dataset
        ...

    class WavelengthRange(BaseModel):
        start: float
        stop: float

    class PixelMask(BaseModel, arbitrary_types_allowed=True):
        mask: sc.DataArray                    # a data value: PixelMask is
                                              # given by reference only

    class Reduction(BaseModel):
        bins: Annotated[int, Gt(0)] = 50      # a scalar, with a constraint
        banks: list[int] = []                 # may be empty: [], not None

    class GivenCentre(BaseModel):
        kind: Literal['given'] = 'given'      # the tag of an alternative
        x: float = 0.0
        y: float = 0.0

    class FindCentre(BaseModel):
        kind: Literal['find'] = 'find'
        tolerance: Annotated[float, Gt(0)] = 1e-3

    class IofQParams(BaseModel):
        reduction: Reduction = Reduction()    # a default is an instance
        range: WavelengthRange | None = None  # may be left out
        mask: PixelMask | None = None
        centre: Annotated[GivenCentre | FindCentre, Field(discriminator='kind')]
        transmission: RunFile
        sample_runs: list[RunFile]            # a table: no default

    class IofQOutputs(BaseModel, arbitrary_types_allowed=True):
        iofq: sc.DataArray
        pixel_mask: PixelMask                 # fits IofQParams.mask of a request

    IOFQ = WorkflowSpec(
        name='iofq', version=1, title='I(Q)', description='...',
        params=IofQParams, outputs=IofQOutputs, tables=('sample_runs',),
    )

Each field of ``IofQParams`` is a *parameter*. A *group* is a model, such
as ``Reduction``, whose fields a request writes as an object. A *data
value* is a value without a JSON form, such as a scipp array. A request for
``IOFQ``, with ``dataset(...)`` a reference to a dataset and
``m.ref('pixel_mask')`` a reference to the output ``pixel_mask`` of an
earlier record ``m``::

    {
        'reduction': {'bins': 100},
        'mask': m.ref('pixel_mask'),
        'centre': {'kind': 'find'},
        'transmission': dataset(run=99),
        'sample_runs': [dataset(run=101), dataset(run=102)],
    }

:mod:`ess.spec.places` describes what a request writes at each root place by
its type, and when each value is validated; :mod:`ess.spec.refs` describes the
references.

The models are ordinary pydantic models: what the framework needs, it reads
from the field types (see :mod:`ess.spec.field_types`). Making a spec checks
them. Each comment below names a rule that a spec breaks, with the fix::

    class Geometry(BaseModel):
        # A file is a parameter of a dataset model, given by reference to a
        # dataset: geometry: GeometryFile, with class
        # GeometryFile(DatasetModel, ...).
        file: Path

    class CalibrationFile(DatasetModel, kind='calibration'):
        # A dataset model holds only path: the dataset's metadata say the rest.
        run: int

    class Reduction(BaseModel):
        # No aliases: a request writes a field by its name.
        bins: int = Field(10, alias='nbins')
        # A field is never X | None: a value that a request may leave out is
        # a parameter typed X | None, or a bool field, such as limit_range: bool.
        start: float | None = None
        # A field of a group is a scalar or a list of scalars: a value of
        # several parts is a group, a parameter of its own.
        limits: tuple[float, float] = (0.0, 1.0)
        # A group is not a field of a group: it is a parameter of its own.
        range: WavelengthRange

        # No serializers or computed fields: the record holds a group as the
        # object of its fields.
        @computed_field
        def width(self) -> float: ...

    class Centring(BaseModel):
        # A union of groups is a parameter, or the element of a list
        # parameter, never a field of a group.
        centre: GivenCentre | FindCentre

    class Sample(BaseModel):
        # No NewType or type alias: a spec names a role by a class.
        run: RunNumber

    class Params(BaseModel):
        # A parameter is a group, a union of groups, or a list of one:
        # bins is a field of Reduction.
        bins: int = 10
        reduction: Reduction
        centring: Centring
        sample: Sample
        geometry: Geometry
        calibration: CalibrationFile
        # A list without rows is [], not None.
        transmissions: list[RunFile] | None = None
        # A row is a value: the element of a list is not X | None.
        masks: list[PixelMask | None] = []
        # Pydantic does not validate a default, so a default is the value
        # validating it gives: WavelengthRange(start=1.0, stop=2.0).
        range: WavelengthRange = {'start': 1.0, 'stop': 2.0}
        # A table has no default.
        sample_runs: list[RunFile] = []
        # A name is of a parameter or of an output, not both.
        iofq: RunFile

        # The params model has model validators only: each parameter is
        # validated alone.
        @field_validator('bins')
        @classmethod
        def _positive(cls, bins: int) -> int: ...

    class Outputs(BaseModel, arbitrary_types_allowed=True):
        iofq: sc.DataArray
        # An output is X, X | None, list[X], or dict[str, X], not a union.
        width: float | str
        # No output holds a dataset model or a Path: a file comes from a
        # dataset.
        raw: RunFile

    WorkflowSpec(..., params=Params, outputs=Outputs, tables=('sample_runs',))

A check of one value belongs to the value's type, such as a field validator
of a group. A check across parameters belongs to the binding or the driver,
and fails when the request runs. The params model may state one as a model
validator, but the framework never calls it, since a stage and a driver
never get a whole params instance. A binding may run it,
``IofQParams.model_validate(dict(params))``.

A *table* is a list parameter whose *rows* an accumulator takes a push at a
time, such as the sample runs; its rows are distinct. ``tables`` names the
tables. A request gives every table, ``[]`` for one without rows, so that no
request leaves out a table by mistake.

Two forms exist, related by a one-way projection:

* :class:`WorkflowSpec` is the in-process form. It holds the model *classes*,
  so consumers in the same process get full pydantic validation.
* :class:`SerializedWorkflowSpec` is the plain-data form produced by
  :meth:`WorkflowSpec.serialize`. It is what a service announces to remote
  consumers. There is intentionally no inverse: validators do not round-trip
  through JSON Schema, and authoritative validation always happens in the
  process owning the model classes.

Both examples are tests, in ``workflow_spec_examples_test.py``.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Sequence
from datetime import datetime
from enum import Enum
from pathlib import PurePath
from types import UnionType
from typing import (
    Annotated,
    Any,
    Literal,
    Self,
    TypeAliasType,
    Union,
    get_args,
    get_origin,
)

from annotated_types import MaxLen, MinLen
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    NaiveDatetime,
    create_model,
    model_validator,
)
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from . import field_types, places
from .parameters import DatasetModel
from .places import Rows, Ways
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
            "a group, a union of groups, or a list of one, possibly typed "
            "X | None. Defaults to :class:`NoParams`."
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
        :func:`ess.spec.field_types.has_json_form`) and is not a dataset
        model, whose JSON form, read, would be a local path rather than the
        dataset's identity.
        """
        tp = field_types.value_type(self.params.model_fields[name].annotation)
        return not field_types.is_dataset(tp) and field_types.has_json_form(
            self.params, name
        )

    def serialize(self) -> SerializedWorkflowSpec:
        """
        Project to the plain-data form.

        The params schema has the value at each place in the forms it takes:
        a dataset model as a reference to a dataset, with the model's
        metadata; a group as ``anyOf`` the object of its fields, each a
        scalar or a list of scalars, and a reference to an output, or one of
        them only; a union of groups as
        ``oneOf`` the object of the fields of each alternative, never a
        reference; a scalar as its JSON schema.
        A reference's schema is the ``@ref`` object of each kind it may be,
        with the name of the data type it must fit in ``dataType``, as the
        outputs schema has each output.
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

    Produced by :meth:`WorkflowSpec.serialize`; round-trips through JSON. A
    UI generated from it reads the widget at each place from the forms the
    params schema gives it. A schema with ``dataType`` is a reference, of the kinds that
    the ``@ref`` const of each object it allows names:

    ==================================  =================================
    Forms at the place                  Widget
    ==================================  =================================
    a reference to a dataset only       a dataset picker (a dataset model)
    the object of a group's fields      the group's fields
    a reference to an output only       a record picker (a group that
                                        holds a data value)
    both                                the group's fields, and a choice
                                        to pick a record instead
    a ``oneOf`` with a                  a choice of the alternatives of a
    ``discriminator``                   union, each with its fields
    any other schema, a scalar's        an input for the value, inline
    ==================================  =================================

    ``dataType`` names the type of the data a picker takes, as the outputs
    schema names the type of each output. A union of groups is a ``oneOf``
    of its alternatives, each the object of its fields with its tag, and has
    no reference form, since no reference fits a union. A list is an array
    of these, and an optional value has ``null`` in ``anyOf``. The metadata
    of a dataset model, under ``metadata``, say which datasets fit. The forms
    at three places, the parameter ``reduction``, a parameter ``mask`` of a
    dataset model ``MaskFile``, and the field ``Reduction.bins``::

        "reduction": {"anyOf": [{"$ref": "#/$defs/Reduction", ...},
                                {"anyOf": [OutputRef, AccumulatorRef],
                                 "dataType": "Reduction"}]}
        "mask":      {"$ref": "#/$defs/DatasetRef", "dataType": "MaskFile",
                      "metadata": {"kind": "mask"}}
        "bins":      {"type": "integer", "default": 10}
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
        if aliases := _alias_problems(field.annotation):
            problems.extend(f'{where}: {problem}' for problem in aliases)
            continue
        if paths := _path_problems(field.annotation):
            problems.extend(f'{where}: {problem}' for problem in paths)
            continue
        if (shape := _shape_problem(field.annotation)) is not None:
            problems.append(f'{where}: {shape}')
            continue
        if _has_alias(field):
            problems.append(f'{where}: a parameter has no alias')
        if get_origin(field_types.optional(field.annotation)[0]) is list and (
            other := [m for m in field.metadata if not isinstance(m, MinLen | MaxLen)]
        ):
            problems.append(
                f'{where}: a list parameter carries only length constraints, '
                f'not {other}; a check of one row belongs to the element type'
            )
        try:
            groups = places.groups_at(model, name)
        except ValueError as error:
            problems.append(f'{where}: {error}')
            continue
        if not groups:
            tp = field_types.value_type(field.annotation)
            problems.append(
                f'{where}: {field_types.type_name(tp)} is not a group; a parameter '
                'is a group, a union of groups, or a list of one, and a value of '
                'another type is a field of a group, such as the bins of a reduction'
            )
        elif inside := [p for group in groups for p in _group_problems(group)]:
            problems.extend(f'{where}: {problem}' for problem in inside)
        elif field.default_factory_takes_validated_data:
            problems.append(
                f'{where}: the default_factory of a parameter takes no other values'
            )
        elif not field.is_required() and (
            problem := _default_problem(model, name, _parameter_value)
        ):
            problems.append(f'{where}: {problem}')
    return problems


def _shape_problem(annotation: Any) -> str | None:
    """
    Why ``annotation`` is not the shape of a parameter, or ``None``: ``X``,
    ``X | None``, or ``list[X]``, with ``X`` a type, not a list, a dict, or
    ``None``, or a union. A list without rows is ``[]``, so a list parameter
    is not ``list[X] | None``, and a row is a value, so its element is not
    ``X | None``.
    """
    tp, is_optional, item_optional = _taken_apart(annotation)
    if item_optional is not None and is_optional:
        return 'a list parameter is not typed list[X] | None; a list without rows is []'
    if item_optional:
        return (
            'the element of a list parameter is not typed X | None; each row is a value'
        )
    if field_types.is_single(tp) or get_origin(tp) in (Union, UnionType):
        return None
    return (
        'a parameter is a group, a union of groups, or a list of one, possibly '
        'typed X | None'
    )


def _taken_apart(annotation: Any) -> tuple[Any, bool, bool | None]:
    """
    The type of one value of ``annotation``, ``X`` of ``X``, ``X | None``,
    ``list[X]``, ``list[X] | None``, or ``list[X | None]``; whether
    ``annotation`` is typed ``X | None``; and for a list whether its element
    is, else ``None``.
    """
    tp, is_optional = field_types.optional(annotation)
    if get_origin(tp) is not list:
        return tp, is_optional, None
    tp, item_optional = field_types.optional(get_args(tp)[0])
    return tp, is_optional, item_optional


def _found(
    annotation: Any,
    test: Callable[[Any], bool],
    skip: Callable[[Any], bool] = lambda cls: False,
) -> list[tuple[str, Any]]:
    """
    Each type that ``annotation`` is made of (see
    :func:`~ess.spec.field_types.reachable`) for which ``test`` holds, with
    where it is: ``''`` in ``annotation`` itself, ``'Class.field: '`` in a
    field of a class in it, unless ``skip`` holds for the class.
    """
    found = [('', t) for t in field_types.arguments(annotation) if test(t)]
    for cls in dict.fromkeys(field_types.reachable(annotation)):
        if not skip(cls):
            found.extend(
                (f'{cls.__name__}.{name}: ', t)
                for name, tp in field_types.fields_of(cls).items()
                for t in field_types.arguments(tp)
                if test(t)
            )
    return found


def _alias_problems(annotation: Any) -> list[str]:
    """
    Refuse each ``NewType`` or type alias that ``annotation`` is made of,
    named by the field of the class that holds it, if any: a spec names a
    role by a class.
    """
    return [
        f'{where}{alias.__name__} is '
        f'{"a type alias" if isinstance(alias, TypeAliasType) else "a NewType"}; '
        'a spec names a role by a class, such as a group, and a binding maps it '
        'to a key of its own'
        for where, alias in _found(annotation, field_types.is_alias)
    ]


def _path_problems(annotation: Any) -> list[str]:
    """
    Refuse each ``Path`` that ``annotation`` is made of outside a dataset
    model, named by the field of the class that holds it, if any: a file is
    a parameter of a dataset model, given by reference to a dataset, so that
    the dataset source checks each file a workflow reads.
    """
    found = _found(annotation, _is_path, skip=field_types.is_dataset)
    return [
        f'{where}a Path is outside a dataset model; a file is a parameter of a '
        "dataset model, such as mask: MaskFile with class MaskFile(DatasetModel, "
        "kind='mask'), given by reference to a dataset"
        for where in dict.fromkeys(where for where, _ in found)
    ]


def _is_path(tp: Any) -> bool:
    return isinstance(tp, type) and issubclass(tp, PurePath)


def _has_alias(field: FieldInfo) -> bool:
    return any(
        getattr(field, a) is not None
        for a in ('alias', 'validation_alias', 'serialization_alias')
    )


def _group_problems(group: type[BaseModel]) -> list[str]:
    """
    Refuse a group at a parameter whose fields a request cannot write: of a
    group without a data value, a serializer or a computed field, which
    would make the group's JSON form differ from the object of its fields, a
    field with an alias, a field that is not interpreted (see
    :func:`_field_problem`), and a default that is not a value, since a
    field left out holds its default. Field and model validators of any mode
    are allowed: they run whenever the group is validated whole (see
    :mod:`ess.spec.places`). A group with a data value is given by reference
    only, to an output, so it holds no dataset model, as no output does (see
    :func:`_output_dataset_problems`); its fields are not looked at
    otherwise. A dataset model is checked by :func:`_dataset_model_problems`
    alone.
    """
    if field_types.is_dataset(group):
        return _dataset_model_problems(group)
    if places.unwritten(group) is not None:
        return [
            f'{group.__name__} is given by reference to an output only: {problem}'
            for problem in _output_dataset_problems(group)
        ]
    problems = []
    if found := _decorators(group, _SERIALIZERS):
        problems.append(f'{group.__name__}: no serializers or computed fields, {found}')
    for name, field in group.model_fields.items():
        where = f'{group.__name__}.{name}'
        if _has_alias(field):
            problems.append(f'{where}: a field of a group has no alias')
        elif (problem := _field_problem(field.annotation)) is not None:
            problems.append(f'{where}: {problem}')
        elif field.default_factory_takes_validated_data:
            problems.append(
                f'{where}: the default_factory of a field of a group takes no other '
                'values'
            )
        elif not field.is_required() and (
            problem := _default_problem(group, name, places.field_value)
        ):
            problems.append(f'{where}: {problem}')
    return problems


def _dataset_model_problems(model: type[BaseModel]) -> list[str]:
    """
    Refuse a dataset model with a field other than ``path``, or that declares
    ``path`` anew, such as with another type, a default, or an alias: the
    backend makes the model of ``{'path': <local path>}``. A field validator
    of ``path`` is allowed.
    """
    if other := [n for n in model.model_fields if n != 'path']:
        return [f'{model.__name__}: a dataset model holds only path, not {other}']
    # The repr of a FieldInfo shows each attribute that a declaration sets.
    if repr(model.model_fields['path']) != repr(DatasetModel.model_fields['path']):
        return [
            f'{model.__name__}.path: a dataset model keeps the path of '
            'DatasetModel, path: Path, without a default or an alias; a check of '
            'the path is a field validator'
        ]
    return []


def _field_problem(annotation: Any) -> str | None:
    """
    Why a field typed ``annotation``, of a group without a data value, is
    not *interpreted*, with the fix, or ``None``. An interpreted field is a
    scalar, ``int``, ``float``, ``str``, ``bool``, ``datetime`` (or pydantic's
    ``AwareDatetime`` or ``NaiveDatetime``), an enum, or a ``Literal``,
    possibly with constraints, ``Annotated[float, Gt(0)]``, or a list of
    scalars, and never ``X | None``, nor a ``Literal`` with ``None``, which
    is ``X | None`` too: a form shows each as an input of its type, and a
    request writes it inline. Any other type, a group, a union, a tuple, a
    dict, or a dataclass, is refused.
    """
    tp, is_optional, item_optional = _taken_apart(annotation)
    if item_optional is not None and (is_optional or item_optional):
        return (
            'a list field is never typed list[X] | None or list[X | None]; a list '
            'without elements is [], and each element is a value'
        )
    if is_optional or (get_origin(tp) is Literal and None in get_args(tp)):
        return (
            'a field is never typed X | None, nor a Literal with None; a value '
            'that a request may leave out is a parameter typed X | None, or a '
            'bool field, such as limit_range: bool'
        )
    if _is_scalar(tp):
        return None
    union = get_origin(tp) in (Union, UnionType)
    if field_types.is_group(tp) or (
        union and any(map(field_types.is_group, get_args(tp)))
    ):
        kind = 'a union of groups' if union else 'a group'
        return (
            f'{field_types.type_name(tp)} is {kind}; a field of a group is a '
            'scalar or a list of scalars: make it a parameter of its own'
        )
    return (
        f'{field_types.type_name(tp)} is not a scalar; a field of a group is a '
        'scalar (int, float, str, bool, datetime, an enum, or a Literal) or a '
        'list of scalars: a value of several parts is a group, a parameter of '
        'its own, such as range: WavelengthRange, not limits: tuple[float, float]'
    )


def _is_scalar(tp: Any) -> bool:
    return (
        tp in (int, float, str, bool, datetime, AwareDatetime, NaiveDatetime)
        or get_origin(tp) is Literal
        or (isinstance(tp, type) and issubclass(tp, Enum))
    )


def _default_problem(
    model: type[BaseModel],
    name: str,
    made_of: Callable[[type[BaseModel], str, Any], Any],
) -> str | None:
    """
    Why the default of the field ``name`` of ``model`` is not a value, or
    ``None``: what a request may write, and the value that ``made_of`` it
    gives, since pydantic does not validate a default, and a binding gets it
    as it is. So a group's default is an instance, not the object of its
    fields. ``made_of`` is :func:`_parameter_value` for a parameter, and
    :func:`ess.spec.places.field_value` for a field of a group, whose default
    is validated whole, with the length constraints of a list.
    """
    default = model.model_fields[name].get_default(call_default_factory=True)
    try:
        made = made_of(model, name, default)
    except ParamsError as error:
        return f'the default is not a value a request may write: {error}'
    if made != default:
        return f'the default {default!r} is not the value validating it gives, {made!r}'
    return None


def _parameter_value(model: type[BaseModel], name: str, value: Any) -> Any:
    """
    ``value`` of the parameter ``name`` of ``model`` as validating it gives,
    if a request may write it with no reference (see
    :func:`ess.spec.places.held`).
    """
    places.held(model, name, value, _no_ref)
    return places.validated(model, name, value)


def _no_ref(loc: Loc, ref: Ref, tp: Any) -> Any:
    raise ParamsError([(loc, f'{ref} is a reference; a default is a value')])


def _outputs_problems(model: type[BaseModel]) -> list[str]:
    problems = []
    for name, field in model.model_fields.items():
        where = f'{model.__name__}.{name}'
        tp = field.annotation
        if aliases := _alias_problems(tp):
            problems.extend(f'{where}: {problem}' for problem in aliases)
        elif not field_types.is_single(field_types.value_type(field.annotation)):
            problems.append(
                f'{where}: an output is a type, possibly typed X | None, or a '
                'list or a dict of one'
            )
        else:
            problems.extend(f'{where}: {problem}' for problem in _path_problems(tp))
            problems.extend(
                f'{where}: {problem}' for problem in _output_dataset_problems(tp)
            )
    return problems


def _output_dataset_problems(annotation: Any) -> list[str]:
    """
    Refuse each dataset model that ``annotation``, of an output, is made of,
    named by the field of the class that holds it, if any: no output holds
    one, so that a file comes from a dataset, which the dataset source
    checks.
    """
    return [
        f'{where}{field_types.type_name(model)} is a dataset model; no output '
        'holds one, so that a file comes from a dataset, which the dataset source '
        'checks'
        for where, model in _found(annotation, field_types.is_dataset)
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
                title=places.title(name, info),
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
    """
    The reference a value of type ``tp`` takes, with the name of ``tp``; of a
    dataset model, a reference to a dataset, with the model's metadata, so
    that a dataset picker lists the datasets that fit.
    """
    if field_types.is_dataset(tp):
        extra = {'dataType': field_types.type_name(tp), 'metadata': tp.metadata}
        return Annotated[DatasetRef, Field(json_schema_extra=extra)]
    data_type = Field(json_schema_extra={'dataType': field_types.type_name(tp)})
    return Annotated[OutputRef | AccumulatorRef, data_type]


def _field_forms(model: type[BaseModel], name: str) -> Any:
    """
    The type of the parameter ``name`` of ``model`` with the type at each of
    its places replaced by the forms it takes (see :func:`_forms`); the
    constraints of a list parameter on the list.
    """
    described = places.place(model, name)
    if isinstance(described, Rows):
        row = _forms(described.element)
        listed: Any = list[row]  # type: ignore[valid-type]
        return Annotated[listed, *described.metadata] if described.metadata else listed
    return _forms(described)


def _forms(described: Ways) -> Any:
    """
    The forms a place takes, as :func:`~ess.spec.places.place` describes it:
    the forms of the fields of each group written inline (see
    :func:`_group_forms`), with the constraints of the place, and a reference
    if one fits, of a dataset model to a dataset.
    """
    forms = []
    if described.inline:
        groups = [_group_forms(g, described.tag) for g in described.inline.values()]
        forms.append(_inline_form(groups, described.metadata))
    if described.record is not None:
        forms.append(_ref_form(described.record))
    form = Union[tuple(forms)]  # noqa: UP007
    return Union[form, None] if described.none else form  # noqa: UP007


def _inline_form(groups: Sequence[Any], metadata: Sequence[Any]) -> Any:
    """The object of the fields of one of ``groups``, inline, never a reference."""
    # Only an object reads as a reference; JSON Schema's ``required`` holds
    # for any other value, so ``not`` would refuse it.
    return Annotated[
        Union[tuple(groups)],  # noqa: UP007
        *metadata,
        Field(json_schema_extra=_not_a_ref),
    ]


@functools.cache
def _group_forms(group: type[BaseModel], tag: str | None = None) -> Any:
    """
    A model of the fields of a group, each a scalar or a list of scalars,
    written inline only, with its constraints; the tag field ``tag`` of an
    alternative of a union without its default, required, since a request
    always writes it. The title of a tag labels its alternative, so a tag
    without one is titled by its value, as a parameter is by its name;
    pydantic's title, made of the name of the field, would be that of every
    alternative.
    """
    fields: dict[str, Any] = {}
    for name, info in group.model_fields.items():
        label = (
            places.title(get_args(info.annotation)[0], info) if name == tag else None
        )
        fields[name] = (
            Annotated[info.annotation, *info.metadata]
            if info.metadata
            else info.annotation,
            Field(
                default=PydanticUndefined
                if name == tag
                else info.get_default(call_default_factory=True),
                title=label or info.title,
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
    tp, is_optional = field_types.optional(annotation)
    if (item := field_types.element(tp)) is None:
        schema: dict[str, Any] = {'dataType': field_types.type_name(tp)}
    elif get_origin(tp) is list:
        schema = {'type': 'array', 'items': _output_type_schema(item)}
    else:
        schema = {'type': 'object', 'additionalProperties': _output_type_schema(item)}
    return {'anyOf': [schema, {'type': 'null'}]} if is_optional else schema
