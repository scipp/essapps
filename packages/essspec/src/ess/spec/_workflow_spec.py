# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Workflow specifications: the interface of a workflow, without its code.

A :class:`WorkflowSpec` names a workflow and holds two pydantic models: the
*params*, everything a request gives the workflow, and the *outputs*, the
results it returns. This docstring states the rules for both models, once.
The other modules of ``ess.spec`` and ``ess.apps`` apply them and link here.

A spec of an I(Q) reduction, with each kind of parameter once::

    class RunFile(FileModel, kind='raw'):     # a file model: a raw data file
        ...

    class Reduction(BaseModel):               # a group of scalars
        bins: Annotated[int, Gt(0)] = 50      # a scalar, with a constraint
        banks: list[int] = []                 # a list of scalars; [] if empty

    class WavelengthRange(BaseModel):
        start: float
        stop: float

    class Specimen(BaseModel):                # a group with files
        run: RunFile                          # a file
        can: RunFile | None = None            # a file, or None
        thickness: float = 1.0

    class PixelMask(BaseModel, arbitrary_types_allowed=True):
        mask: sc.DataArray                    # a data value: PixelMask is given
                                              # by reference to an output only

    class GivenCentre(BaseModel):             # an alternative of a union
        kind: Literal['given'] = 'given'      # its tag
        x: float = 0.0
        y: float = 0.0

    class FindCentre(BaseModel):              # another alternative
        kind: Literal['find'] = 'find'
        tolerance: Annotated[float, Gt(0)] = 1e-3

    class IofQParams(BaseModel):
        reduction: Reduction = Reduction()    # a default is an instance
        range: WavelengthRange | None = None  # None: no range
        specimen: Specimen                    # no default: required
        mask: PixelMask | None = None
        centre: Annotated[GivenCentre | FindCentre, Field(discriminator='kind')]
        transmission: RunFile                 # a file
        sample_runs: list[RunFile]            # a table: no default

    class IofQOutputs(BaseModel, arbitrary_types_allowed=True):
        iofq: sc.DataArray
        pixel_mask: PixelMask                 # fits the parameter mask

    IOFQ = WorkflowSpec(
        name='iofq', version=1, title='I(Q)', description='...',
        params=IofQParams, outputs=IofQOutputs, tables=('sample_runs',),
    )

A request gives the parameters as a dict by name. In this request for
``IOFQ``, ``m`` is the record of an earlier request whose spec has the
output ``pixel_mask``::

    {
        'reduction': {'bins': 100},              # a group: banks takes its default
        'specimen': {'run': file(run=104), 'thickness': 2.0},  # a file in a group
        'mask': m.ref('pixel_mask'),             # a reference to an output
        'centre': {'kind': 'find'},              # the tag picks FindCentre
        'transmission': file(run=99),            # a file
        'sample_runs': [file(run=101), file(run=102)],  # two rows
    }                                            # range is left out: None

Terms
-----

*Parameter*
    A field of the params model, such as ``reduction``. A *list parameter*,
    ``list[X]``, holds *rows*, each of type ``X``. A *table* is a list
    parameter whose rows an accumulator takes a push at a time. ``tables``
    names the tables.
*Group*
    A pydantic model, such as ``Reduction``, but not a ``RootModel``. A
    request writes a group as a JSON object of its *fields*. A field left out
    takes its default.
*Union of groups*
    A pydantic discriminated union, such as the type of ``centre``. Each
    alternative is a group with the *tag* field named by ``discriminator``,
    typed a ``Literal`` of one string. A request writes the fields of one
    alternative and its tag, which says which alternative it is.
*Scalar*
    An ``int``, ``float``, ``str``, ``bool``, or ``datetime`` (also
    pydantic's ``AwareDatetime`` and ``NaiveDatetime``), or an enum or a
    ``Literal`` whose values are of these types.
*File model*, *file*
    A file model is a subclass of :class:`~ess.spec.parameters.FileModel`,
    such as ``RunFile``. Its value is a *file*. A request writes a file as
    ``file(...)``, such as ``file(run=99)``. The backend resolves it to the
    file's identity when the request is submitted. The binding gets the file
    model with the file's local path, ``transmission.path``.
*Interpreted field*, *data value*
    A field of a group is *interpreted* if a form made from the params
    schema has an input for it: a scalar; a file model, possibly typed
    ``X | None``; or a list of scalars or of file models. Constraints, such
    as ``Gt(0)``, are allowed.
    Any other field is a *data value*, such as ``mask: sc.DataArray``,
    ``start: float | None``, ``limits: tuple[float, float]``, or a field
    typed a group. No request can write a data value.
*Reference to an output*
    ``m.ref('pixel_mask')`` names the output ``pixel_mask`` of the record
    ``m``. ``acc.ref(...)`` names an output of an accumulator. The backend
    replaces the reference by the output's data when the request runs.
*JSON form*
    What a value dumps to as JSON, ``model_dump(mode='json')``. The record of
    a request stores the JSON form of each value. The JSON form of a file is
    ``{'@ref': 'file', 'file': 'run:loki/99'}``, and that of a reference to
    an output is ``{'@ref': 'output', 'record': ..., 'output': ...}`` (see
    :mod:`ess.spec.refs`).

What a request writes
---------------------

The type of a parameter, or of the rows of a list parameter, says what a
request writes there:

- A group, such as ``Reduction``: a JSON object of its fields, or a
  reference to an output. A group with a file model, such as ``Specimen``,
  takes no reference to an output. A group with a data value, such as
  ``PixelMask``, takes only a reference to an output.
- A union of groups, such as the type of ``centre``: a JSON object of the
  fields of one alternative, with its tag.
- A file model, such as ``RunFile``: a file.
- ``X | None``: what ``X`` takes, or ``None``.
- ``list[X]``: a list of rows, each what ``X`` takes.

The params schema of a spec (see :meth:`WorkflowSpec.serialize`) and a form
made from the params model follow this list. The rules behind it:

- A reference to an output fills a whole parameter or a whole row, never a
  field of a group. The backend validates a group when the request is
  submitted, so that its validators see all its fields, and an output may
  not exist yet then.
- A file is a value, like a scalar. Its identity is known when the request
  is submitted, so a file can be a field of a group.
- A reference to an output fits a parameter or a row if the output's type is
  the group's class or a subclass of it (see
  :func:`~ess.spec.field_types.fits`). So a parameter is a group: a
  parameter typed ``float`` or ``sc.DataArray`` would fit every output of
  that type, and a picker of records would list unrelated outputs. A number
  is a field of a group, such as ``Reduction.bins``, and an array is a field
  of a group whose class says what it is, such as ``PixelMask``.
- No output holds a file model, so that every file comes from the file
  source, which checks it. So a group with a file model takes no reference to
  an output, and a group with a data value has no file model.
- No reference to an output fits a union. An output that fit one would
  carry the tag of an alternative, so the workflow that computes it would
  know the specs that take it (see ADR 0008). A parameter that takes either
  a value typed in or an output is a group, such as ``BeamCentre``, not a
  union. So no alternative of a union has a data value.
- A default is a value that a request may write, as validating it gives: an
  instance of a group, not a dict, and never a reference or a file. A file
  model at a field of a group has no default but ``None`` or ``[]``.

What a spec refuses
-------------------

The models are ordinary pydantic models: what the framework needs, it reads
from the field types (see :mod:`ess.spec.field_types`). Making a spec checks
them. Each comment below names a rule that a spec breaks, with the fix::

    class Geometry(BaseModel):
        # A parameter or a field that takes a file is typed a file model:
        # geometry: GeometryFile, with class GeometryFile(FileModel, ...).
        file: Path

    class CalibrationFile(FileModel, kind='calibration'):
        # A file model has only the field file, the file's identity. The
        # metadata of the file say the rest.
        run: int

    class Reduction(BaseModel):
        # No aliases: a request writes a field by its name.
        bins: int = Field(10, alias='nbins')

        # No serializers or computed fields: the JSON form of a group is the
        # JSON object of its fields.
        @computed_field
        def width(self) -> float: ...

    class Binning(BaseModel):
        # float | None is a data value, so Binning is given by reference to
        # an output only, and its default Binning() is refused. Use a bool
        # field, or make the optional part a parameter typed X | None.
        start: float | None = None

    class Sample(BaseModel):
        # No NewType or type alias: a class says what a value is.
        run: RunNumber

    class Params(BaseModel):
        # A parameter is a group, a union of groups, or a list of one:
        # bins is a field of Reduction.
        bins: int = 10
        reduction: Reduction
        binning: Binning = Binning()
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
        # No output holds a file model or a Path: every file comes from the
        # file source.
        raw: RunFile

    WorkflowSpec(..., params=Params, outputs=Outputs, tables=('sample_runs',))

A check of one value belongs to the value's type, such as a field validator
of a group. A check across parameters belongs to the binding or the driver,
and fails when the request runs. The params model may state one as a model
validator, but the framework never calls it, since a stage and a driver
never get a whole params instance. A binding may run it,
``IofQParams.model_validate(dict(params))``.

A request gives every table, ``[]`` for one without rows, so that no request
leaves out a table by mistake. The rows of a table are distinct.

Two forms
---------

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

from collections.abc import Callable, Sequence
from pathlib import PurePath
from types import UnionType
from typing import (
    Any,
    Self,
    TypeAliasType,
    Union,
    get_origin,
)

from annotated_types import MaxLen, MinLen
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)
from pydantic.fields import FieldInfo

from . import field_types, places, request_types
from .parameters import FileModel
from .refs import ParamsError


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

    def serialize(self) -> SerializedWorkflowSpec:
        """
        Project to the plain-data form.

        The params schema gives each parameter, or the rows of a list
        parameter, the forms a request may write there (see the module
        docstring), and each field of a group the JSON schema of its type,
        or of a file. :class:`SerializedWorkflowSpec` shows the schema of each
        form. The projection is one-way: pydantic validators do not survive
        it, so a consumer of the serialized form can validate only
        optimistically.
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

    Produced by :meth:`WorkflowSpec.serialize`; round-trips through JSON.
    ``params_schema`` is a JSON schema of the params. At each parameter, or
    the rows of a list parameter, it allows the forms that a request may
    write there (see :mod:`ess.spec._workflow_spec`). A UI made from it
    picks a widget by these forms:

    ==================================  ===================================
    Schema at a parameter or row        Widget
    ==================================  ===================================
    a file only                         a picker of files (a file model)
    the object of a group's fields      the group's fields
    a reference to an output only       a picker of records (a group with
                                        a data value)
    both of the last two                the group's fields, and a choice
                                        to pick a record instead
    ``oneOf``, with ``discriminator``   a choice of the alternatives of a
                                        union, each with its fields
    ``null`` in ``anyOf``               a toggle: ``X | None``
    an array                            a list of rows
    ==================================  ===================================

    The params schema is the JSON schema of the spec's request type (see
    :mod:`ess.spec.request_types`), which also validates a request, so the
    two agree. The forms a place takes are its ``oneOf``. The schema of a
    reference to an output is the ``@ref`` object of each kind it may be
    (see :mod:`ess.spec.refs`), with the name of the type it must fit under
    ``dataType``, as the outputs schema names the type of each output. A
    file is the definition of its file model: the ``@ref`` object of a
    file, with the model's name under ``dataType`` and its metadata under
    ``metadata``, which say which files fit. A field of a group has the
    JSON schema of its type, or of its file model. The schemas of the
    parameter ``reduction``, a parameter ``mask`` typed the file model
    ``MaskFile``, its definition, and the field ``Reduction.bins``::

        "reduction": {"oneOf": [{"$ref": "#/$defs/Reduction", ...},
                                {"oneOf": [OutputRef, AccumulatorRef],
                                 "dataType": "Reduction", ...}]}
        "mask":      {"$ref": "#/$defs/MaskFile"}
        "MaskFile":  {"properties": {"@ref": {"const": "file"}, "file": ...},
                      "dataType": "MaskFile", "metadata": {"kind": "mask"}}
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
    """The decorators of ``model``, and of its bases, of each of ``kinds``, by kind."""
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
_VALIDATORS = ('validators', 'field_validators', 'root_validators', 'model_validators')


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
        tp, _, metadata = places.root_type(model, name)
        try:
            union = places.alternatives(tp, *metadata)
        except ValueError as error:
            problems.append(f'{where}: {error}')
            continue
        if union is not None:
            groups = list(union[1].values())
        else:
            groups = [tp] if field_types.is_group(tp) else []
        if not groups:
            value = field_types.value_type(field.annotation)
            problems.append(
                f'{where}: {field_types.type_name(value)} is not a group; a parameter '
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
    tp, is_optional, item_optional = field_types.taken_apart(annotation)
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
    named by the field of the class that holds it, if any: a class says what
    a value is.
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
    Refuse each ``Path`` that ``annotation`` is made of outside a file
    model, named by the field of the class that holds it, if any: a
    parameter or a field that takes a file is typed a file model, so that
    the file source checks each file a workflow reads.
    """
    found = _found(annotation, _is_path, skip=field_types.is_file_model)
    return [
        f'{where}a Path is outside a file model; a parameter or a field that '
        "takes a file is typed a file model, such as mask: MaskFile with class "
        "MaskFile(FileModel, kind='mask'), and given by reference"
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
    The problems of a group at a parameter or a row.

    A group without a data value is refused if it has a serializer or a
    computed field, which would make its JSON form differ from the object of
    its fields; a field with an alias; a default that is not a value, since a
    field left out takes its default; or a file model at a field with a
    default but ``None`` or ``[]``. Field and model validators of any mode
    are allowed: they run whenever the group is validated (see
    :mod:`ess.spec.places`).

    A group with a data value is given by reference to an output only, so it
    has no file model, as no output does (see :func:`_output_file_problems`).
    Its fields are not checked otherwise. A file model is checked by
    :func:`_file_model_problems` alone.
    """
    if field_types.is_file_model(group):
        return _file_model_problems(group)
    if (data := places.unwritten(group)) is not None:
        held_type = field_types.type_name(group.model_fields[data].annotation)
        return [
            f'{group.__name__} is given by reference to an output only, since no '
            f'request writes its field {data}, of {held_type}: {problem}'
            for problem in _output_file_problems(group)
        ]
    problems = []
    if found := _decorators(group, _SERIALIZERS):
        problems.append(f'{group.__name__}: no serializers or computed fields, {found}')
    for name, field in group.model_fields.items():
        where = f'{group.__name__}.{name}'
        if _has_alias(field):
            problems.append(f'{where}: a field of a group has no alias')
        elif field.default_factory_takes_validated_data:
            problems.append(
                f'{where}: the default_factory of a field of a group takes no other '
                'values'
            )
        elif not field.is_required() and (
            problem := _default_problem(group, name, places.field_value)
        ):
            problems.append(f'{where}: {problem}')
        elif (
            field_types.file_model(field.annotation) is not None
            and not field.is_required()
            and field.get_default(call_default_factory=True) not in (None, [])
        ):
            problems.append(
                f'{where}: a file model at a field has no default but None or '
                '[]: a request gives each file by reference'
            )
    return problems


def _file_model_problems(model: type[BaseModel]) -> list[str]:
    """
    Refuse a file model with a field other than ``file``, that
    declares ``file`` anew, such as with another type, a default, or an
    alias, with a validator, a serializer, ``model_post_init``, ``__eq__``,
    or ``__hash__`` of its own, or with another config but its title: the
    value of a file model is the identity of a file, and the backend
    attaches its local path (see :class:`~ess.spec.parameters.FileModel`).
    """
    if other := [n for n in model.model_fields if n != 'file']:
        return [
            f'{model.__name__}: a file model holds only the field file, not {other}'
        ]
    # The repr of a FieldInfo shows each attribute that a declaration sets.
    if repr(model.model_fields['file']) != repr(FileModel.model_fields['file']):
        return [
            f'{model.__name__}.file: a file model keeps the field file of '
            'FileModel, without a default or an alias'
        ]
    own = {
        kind: found
        for kind in (*_VALIDATORS, *_SERIALIZERS)
        if (found := _own_decorators(model, kind))
    }
    if methods := _own_methods(model):
        return [
            f'{model.__name__}: a file model keeps the model_post_init, __eq__, '
            f'and __hash__ of FileModel, not its own {methods}'
        ]
    if own:
        return [
            f'{model.__name__}: a file model has no validators or serializers '
            f'of its own, {own}; what it would check of a file is metadata of '
            "the model, such as class MaskFile(FileModel, kind='mask'), or "
            "the workflow's to read"
        ]
    if _config(model) != _config(FileModel):
        return [
            f'{model.__name__}: a file model keeps the config of FileModel, '
            'but its title'
        ]
    return []


def _own_decorators(model: type[BaseModel], kind: str) -> list[str]:
    """
    The decorators of ``kind`` of ``model`` that are not FileModel's, by
    function, so that one declared anew under the same name is found.
    """
    inherited = getattr(FileModel.__pydantic_decorators__, kind)
    return sorted(
        name
        for name, d in getattr(model.__pydantic_decorators__, kind).items()
        if name not in inherited
        or _unbound(d.func) is not _unbound(inherited[name].func)
    )


def _unbound(func: Any) -> Any:
    """The function of ``func``, bound to each class for a classmethod."""
    return getattr(func, '__func__', func)


def _own_methods(model: type[BaseModel]) -> list[str]:
    """``__eq__``, ``__hash__``, or ``model_post_init`` that ``model`` declares anew."""
    found = [
        m
        for m in ('__eq__', '__hash__')
        if getattr(model, m) is not getattr(FileModel, m)
    ]
    # pydantic puts a model_post_init of its own in each class with private
    # attributes, which sets them.
    if not model.model_post_init.__module__.startswith('pydantic.'):
        found.append('model_post_init')
    return found


def _config(model: type[BaseModel]) -> dict[str, Any]:
    return {k: v for k, v in model.model_config.items() if k != 'title'}


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
    if a request may write it without a reference to an output or a file
    (see :func:`ess.spec.places.held`).
    """
    held = places.held(model, name, value, _no_file)
    if refs := places.ref_types(model, {name: held}):
        raise ParamsError(
            (loc, f'{ref} is a reference; a default is a value') for loc, ref, _ in refs
        )
    return places.validated(model, name, value)


def _no_file(name: str, model: type[FileModel]) -> str:
    raise ValueError(f'file {name}: a default cannot name a file')


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
                f'{where}: {problem}' for problem in _output_file_problems(tp)
            )
    return problems


def _output_file_problems(annotation: Any) -> list[str]:
    """
    Refuse each file model that ``annotation``, of an output, is made of,
    named by the field of the class that holds it, if any: no output holds
    one, so that every file comes from the file source, which checks it.
    """
    return [
        f'{where}{field_types.type_name(model)} is a file model; no output '
        'holds one, so that every file comes from the file source, which '
        'checks it'
        for where, model in _found(annotation, field_types.is_file_model)
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
    The JSON Schema of the params: that of the spec's request type (see
    :func:`ess.spec.request_types.request_model`), which validates a
    request. As the backend does, it refuses a field that a model lacks. An
    alternative of a union requires its tag, since a request always writes
    it, and the tag is titled by its value unless the field has a title, as
    a parameter is by its name; pydantic's title, made of the name of the
    field, would be that of every alternative.
    """
    model = request_types.request_model(spec.params)
    schema = _one_of_one(model.model_json_schema())
    for name, prop in schema.get('properties', {}).items():
        # pydantic leaves out the title of a field that is a $ref, if it is
        # the one it would make of the name.
        prop.setdefault('title', model.model_fields[name].title)
    defs = schema.get('$defs', {})
    for group in [schema, *defs.values()]:
        if 'properties' in group:
            group['additionalProperties'] = False
    for name in spec.params.model_fields:
        tp, _, metadata = places.root_type(spec.params, name)
        if (union := places.alternatives(tp, *metadata)) is not None:
            _tags_required(schema, *union)
    return schema


def _one_of_one(schema: Any) -> Any:
    """
    ``schema`` with each ``oneOf`` of one schema, that of a place that takes
    one form, replaced by that schema.
    """
    if isinstance(schema, list):
        return [_one_of_one(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    schema = {k: _one_of_one(v) for k, v in schema.items()}
    if len(one := schema.get('oneOf', ())) == 1:
        rest = {k: v for k, v in schema.items() if k != 'oneOf'}
        return {**one[0], **rest}
    return schema


def _tags_required(schema: dict[str, Any], tag: str, groups: dict[str, Any]) -> None:
    """
    Make the tag ``tag`` of each alternative in ``groups``, by tag value, a
    required field without a default in ``schema``, titled by its value.
    """
    mappings = [
        d['mapping']
        for d in _found_in(schema, 'discriminator')
        if d.get('propertyName') == tag
    ]
    for value, group in groups.items():
        for ref in {m[value] for m in mappings if value in m}:
            found = schema['$defs'][ref.removeprefix('#/$defs/')]
            prop = found['properties'][tag]
            prop.pop('default', None)
            prop['title'] = places.title(value, group.model_fields[tag])
            required = {*found.get('required', ()), tag}
            found['required'] = [n for n in found['properties'] if n in required]


def _found_in(schema: Any, key: str) -> list[Any]:
    """The value of each ``key`` of an object in ``schema``, at any depth."""
    if isinstance(schema, list):
        return [v for s in schema for v in _found_in(s, key)]
    if not isinstance(schema, dict):
        return []
    found = [schema[key]] if key in schema else []
    return found + [v for s in schema.values() for v in _found_in(s, key)]


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
