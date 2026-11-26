# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Workflow specifications: the interface of a workflow, without its code.

``packages/essspec/docs/params.md`` is the guide to writing a spec.

Creating a :class:`WorkflowSpec` runs one model validator. It collects the
problems of every check and raises them together, so that an author sees
all of them at once. Each problem states the model and field it concerns,
and the fix where it is not plain.

The checks take field types apart with :mod:`ess.spec.field_types`, as the
request type and the forms do, so a type that passes here is one they
understand. A default is checked by validating it as a request and as a
binding would, so that a default passes exactly when a request could write
it.

:class:`WorkflowSpec` holds the model classes. :class:`SerializedWorkflowSpec`
is its plain-data form, which a service sends to remote consumers. It has no
inverse: validators do not survive JSON Schema, so authoritative validation
happens in the process that holds the model classes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from enum import Enum
from pathlib import Path, PurePath
from types import NoneType, UnionType
from typing import (
    Annotated,
    Any,
    ForwardRef,
    Literal,
    Self,
    TypeAliasType,
    Union,
    get_args,
    get_origin,
)

from annotated_types import BaseMetadata, GroupedMetadata, MaxLen, MinLen
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    RootModel,
    WrapSerializer,
    model_validator,
)
from pydantic.fields import FieldInfo

from . import field_types, request_types, validation
from .parameters import REFERENCED, File
from .refs import FileRef, ParamsError, iter_refs


class NoParams(BaseModel):
    """
    Params model of a workflow that accepts no parameters.

    It rejects every field, so that params sent to such a workflow raise an
    error.
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
            "The list parameters whose rows an accumulator receives a push at "
            "a time, each a table."
        ),
    )

    def __hash__(self) -> int:
        # The schemas of the serialized form are dicts, which pydantic's hash
        # of all fields refuses; equal specs have equal identities, so these
        # suffice.
        return hash((type(self), self.name, self.version))


class WorkflowSpec[P: BaseModel, O: BaseModel](_SpecFields, frozen=True):
    """
    The interface of a workflow: its identity, display metadata, and the
    params and outputs models.

    It holds no reference to the workflow's code, so that a service can load
    every spec without it. A type checker infers ``P`` and ``O`` from
    ``params=`` and ``outputs=``, and checks a binding's
    ``compute(params: P) -> O`` against them. Code that accepts any spec
    annotates it ``WorkflowSpec[Any, Any]``. See
    ``packages/essspec/docs/params.md``.

    Parameters
    ----------
    name:
        Machine-readable identifier, unique among the specs that a service
        lists.
    version:
        Version of the interface. Increase it on any change that a consumer
        can observe: the params, the outputs, or what the workflow computes.
        :attr:`digest` detects a change of the models under one version.
    title:
        Display title.
    description:
        What the workflow computes.
    params:
        The params model.
    outputs:
        The outputs model. Consumers show the outputs in the order of its
        fields and may select the first.
    tables:
        The list parameters that an accumulator receives a push at a time.
    code_revision:
        Git commit or package version of the workflow code, so that a record
        made from a development branch states what ran. It is not part of the
        identity.

    Raises
    ------
    pydantic.ValidationError
        If the models break a rule of the guide. The message lists every
        problem.
    """

    # A type checker cannot check a default against P, which only params=
    # decides; pyright honours this mypy ignore too.
    params: type[P] = Field(  # type: ignore[assignment]
        default=NoParams,
        description=(
            "Pydantic model class of everything the workflow accepts, each "
            "field of a field type. Defaults to :class:`NoParams`."
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

    @property
    def digest(self) -> str:
        """
        A short hex string of the name, type and requiredness of each field of
        the params and outputs models, at any depth.

        ``packages/essspec/docs/params.md`` (The digest of a spec) states what
        changes it.

        Raises
        ------
        ValueError
            If the type of a field is not resolved, such as a forward
            reference before ``model_rebuild()``, or is not made of classes.
        """
        interface = {
            'params': _interface(self.params, ()),
            'outputs': _interface(self.outputs, ()),
        }
        return hashlib.sha256(_canonical(interface).encode()).hexdigest()[:16]

    def serialize(self) -> SerializedWorkflowSpec:
        """
        The plain-data form of the spec.

        Validators do not survive it, so a consumer of the serialized form can
        validate only optimistically.
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
    Plain-data form of a workflow spec, which round-trips through JSON.

    ``params_schema`` is the JSON schema of the spec's request type (see
    :mod:`ess.spec.request_types`), which also validates a request. At each
    field, at any depth, ``oneOf`` lists the sources that a request may write
    there, and a UI picks a widget by them. A reference to an output has the
    name of the type it must fit under ``dataType``, as the outputs schema
    states the type of each output. A file has the marker's metadata under
    ``metadata``, from which a picker selects the files to offer. The schemas
    of the parameter ``reduction``, a parameter ``centre`` typed
    ``BeamCentre | Referenced[BeamCentre]``, a file field ``mask`` typed
    ``MaskFile``, the definition of a file, and the field ``Reduction.bins``::

        "reduction": {"$ref": "#/$defs/Reduction", ...}
        "centre":    {"oneOf": [{"$ref": "#/$defs/BeamCentre", ...},
                                {"$ref": "#/$defs/OutputRef",
                                 "dataType": "BeamCentre", ...}]}
        "mask":      {"$ref": "#/$defs/FileRef", "metadata": {"kind": "mask"}}
        "FileRef":   {"properties": {"@ref": {"const": "file"}, "file": ...}}
        "bins":      {"type": "integer", "default": 10}
    """

    params_schema: dict[str, Any] = Field(
        description="Schema of the workflow's params."
    )
    outputs_schema: dict[str, Any] = Field(
        description="Schema of the workflow's outputs, by the type of each value."
    )


def _distinct(params: type[BaseModel], outputs: type[BaseModel]) -> list[str]:
    """The problems of a name that is both a parameter and an output."""
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


def _no_decorators(model: type[BaseModel]) -> list[str]:
    """The problems of the decorators and the ``__init__`` of the params model."""
    problems = []
    if found := _decorators(model, _DECORATORS):
        problems.append(f'{model.__name__}: no decorators, {found}; {_ALONE}')
    if model.__init__ is not BaseModel.__init__:
        problems.append(f'{model.__name__}: no __init__ of its own; {_ALONE}')
    return problems


_ALONE = (
    'each parameter is validated and stored alone, so a check or the JSON form '
    "of one value belongs to the value's type, and a check across parameters to "
    'a group that holds them, or to the workflow'
)


_SERIALIZERS = ('field_serializers', 'model_serializers', 'computed_fields')
_DECORATORS = (
    'validators',
    'field_validators',
    'root_validators',
    'model_validators',
    *_SERIALIZERS,
)


def _params_problems(model: type[BaseModel]) -> list[str]:
    problems = [
        *_no_decorators(model),
        *_fields_problems(model, (model,)),
    ]
    return list(dict.fromkeys(problems))


def _fields_problems(
    model: type[BaseModel], stack: tuple[type[BaseModel], ...]
) -> list[str]:
    """
    The problems of each field of ``model``, inside the groups ``stack``, the
    params model first.
    """
    kind = 'a parameter' if len(stack) == 1 else 'a field of a group'
    problems = []
    for name, field in model.model_fields.items():
        where = f'{model.__name__}.{name}'
        if _has_alias(field):
            problems.append(f'{where}: {kind} has no alias')
            continue
        if field.exclude or field.exclude_if is not None:
            problems.append(
                f'{where}: {kind} has no exclude or exclude_if; the record holds '
                'the JSON form, which would leave out the value'
            )
            continue
        metadata = field_types.field_metadata(field)
        if found := _annotated_serializers(field.annotation, metadata):
            problems.append(f'{where}: no serializers, {found}')
            continue
        if found := _type_problems(field.annotation, metadata, stack):
            problems.extend(f'{where}: {problem}' for problem in found)
        elif field.default_factory_takes_validated_data:
            problems.append(f'{where}: the default_factory takes no other values')
        elif not field.is_required() and (problem := _default_problem(model, name)):
            problems.append(f'{where}: {problem}')
    return problems


def _annotated_serializers(annotation: Any, metadata: tuple[Any, ...]) -> list[str]:
    """
    The serializers written with ``Annotated``, such as ``PlainSerializer``,
    in ``metadata`` or in ``annotation``, not inside a class in it.
    """
    found = [type(m).__name__ for m in metadata if isinstance(m, _SERIALIZER_MARKERS)]
    if get_origin(annotation) is Annotated:
        inner = get_args(annotation)[0]
        return [*found, *_annotated_serializers(inner, annotation.__metadata__)]
    for arg in get_args(annotation):
        found.extend(_annotated_serializers(arg, ()))
    return found


_SERIALIZER_MARKERS = (PlainSerializer, WrapSerializer)


def _type_problems(
    annotation: Any, metadata: tuple[Any, ...], stack: tuple[type[BaseModel], ...]
) -> list[str]:
    """
    The problems of a field typed ``annotation``, with ``metadata``, inside
    the groups ``stack``, at any depth.
    """
    if _marks_optional(annotation, metadata):
        return [_OPTIONAL_MARKED]
    tp, none, metadata = field_types.unwrapped(annotation, metadata)
    if field_types.is_alias(tp):
        return [_alias_text(tp)]
    if problem := _marker_problem(tp, metadata):
        return [problem]
    if REFERENCED not in metadata and get_origin(tp) is list:
        item, item_none, _ = field_types.unwrapped(get_args(tp)[0])
        if none:
            return ['a list is not typed list[X] | None; a list without elements is []']
        if item_none:
            return [
                'the element of a list is not typed X | None; each element is a value'
            ]
        if get_origin(item) is list:
            return ['a list is not of lists; its element is a group that holds a list']
        return _type_problems(get_args(tp)[0], (), stack)
    inline, inline_metadata, referenced = field_types.split(tp, *metadata)
    problems = [p for data in referenced for p in _referenced_problems(data)]
    if inline is None:
        return problems
    members = get_args(inline) if get_origin(inline) in (Union, UnionType) else ()
    if (referenced and any(map(field_types.discriminates, metadata))) or any(
        map(field_types.is_referenced, members)
    ):
        return [*problems, _TAGGED_REFERENCE]
    if aliases := [m for m in members if field_types.is_alias(field_types.plain(m))]:
        return [*problems, *map(_alias_text, aliases)]
    if problem := _marker_problem(inline, inline_metadata):
        return [*problems, problem]
    if field_types.file_marker(inline, *inline_metadata) is not None:
        constraints = [m for m in inline_metadata if _is_constraint(m)]
        return [*problems, *map(_constraint_text, constraints)]
    if any(map(field_types.is_file, members)):
        return [*problems, _FILES_UNION]
    try:
        union = field_types.alternatives(inline, *inline_metadata)
    except ValueError as error:
        return [*problems, str(error)]
    if union is not None:
        groups = list(union[1].values())
    elif field_types.is_group(inline):
        groups = [inline]
    elif field_types.is_scalar(inline):
        return problems
    else:
        return [*problems, _not_a_field_type(inline, stack)]
    return [*problems, *(p for g in groups for p in _group_problems(g, stack))]


_TAGGED_REFERENCE = (
    'the discriminator of a union with a member Referenced[...] is on the union '
    "written inline, Annotated[A | B, Field(discriminator='kind')] | "
    'Referenced[C], since a reference to an output has no tag'
)


def _marks_optional(annotation: Any, metadata: tuple[Any, ...]) -> bool:
    """Whether a ``File`` marker in ``metadata`` or ``annotation`` marks X | None."""
    tp = annotation
    while True:
        origin, args = get_origin(tp), get_args(tp)
        if origin in (Union, UnionType) and NoneType in args:
            if any(isinstance(m, File) for m in metadata):
                return True
            rest = tuple(a for a in args if a is not NoneType)
            tp = rest[0] if len(rest) == 1 else Union[rest]  # noqa: UP007
        elif origin in (Union, UnionType):
            return any(_marks_optional(arg, metadata) for arg in args)
        elif origin is Annotated:
            metadata = (*metadata, *tp.__metadata__)
            tp = args[0]
        else:
            return False


_OPTIONAL_MARKED = (
    'File(...) marks a Path, not Path | None; a file field that may be None is '
    'typed Annotated[Path, File(...)] | None, as Referenced[X] | None is'
)


def _is_constraint(metadata: Any) -> bool:
    """
    Whether ``metadata`` is a constraint that pydantic applies, such as
    ``MinLen(2)`` or ``Field(gt=2)``, not a validator or a note.
    """
    if isinstance(metadata, FieldInfo):
        return bool(metadata.metadata)
    return isinstance(metadata, BaseMetadata | GroupedMetadata)


def _constraint_text(constraint: Any) -> str:
    return (
        f'a file field takes no constraint, such as {constraint!r}; a check of '
        'its local path is a validator, such as AfterValidator(...), and the '
        'file source checks the file'
    )


_FILES_UNION = (
    'a field takes one kind of file, so a file field is in a union only with '
    'members Referenced[...] and None; the marker of one file field names the '
    'metadata of the files for the field'
)


def _marker_problem(tp: Any, metadata: tuple[Any, ...]) -> str | None:
    """
    Why the ``File`` markers in ``metadata`` do not make a field of type ``tp``
    a file field, or ``None``.
    """
    markers = [m for m in metadata if isinstance(m, File)]
    if not markers:
        return None
    if tp is not Path:
        return (
            f'File(...) marks a Path, not {field_types.type_name(tp)}; a file field '
            "is typed Annotated[Path, File(...)], such as Annotated[Path, File("
            "kind='mask')]"
        )
    if len(markers) > 1:
        return f'a file field has one File(...) marker, not {markers}'
    return None


def _not_a_field_type(tp: Any, stack: tuple[type[BaseModel], ...]) -> str:
    """Why ``tp`` is not the type of a field inside the groups ``stack``."""
    name = field_types.type_name(tp)
    if _is_path(tp):
        return _UNMARKED_PATH
    if len(stack) > 1:
        group = stack[-1].__name__
        fix = (
            f'or, if the data of an output fill {group}, type the field that '
            f'holds it Referenced[{group}]'
        )
    else:
        fix = (
            'or, if the data of an output fill it, type it Referenced[G], with G '
            'a group that holds them'
        )
    return (
        f'{name} is not a field type, which is a scalar, a file field, a group, '
        f'a union of groups, Referenced[T], X | None, or list[X]; give the field '
        f'one of these types, {fix}'
    )


def _found(annotation: Any, test: Callable[[Any], bool]) -> list[tuple[str, Any]]:
    """
    Each type that ``annotation`` is made of (see
    :func:`~ess.spec.field_types.reachable`) for which ``test`` holds, with
    where it is: ``''`` in ``annotation`` itself, ``'Class.field: '`` in a
    field of a class in it.
    """
    found = [('', t) for t in field_types.arguments(annotation) if test(t)]
    for cls in dict.fromkeys(field_types.reachable(annotation)):
        found.extend(
            (f'{cls.__name__}.{name}: ', t)
            for name, tp in field_types.fields_of(cls).items()
            for t in field_types.arguments(tp)
            if test(t)
        )
    return found


def _alias_problems(annotation: Any) -> list[str]:
    """The problems of each ``NewType`` or type alias that ``annotation`` is made of."""
    return [
        f'{where}{_alias_text(alias)}'
        for where, alias in _found(annotation, field_types.is_alias)
    ]


def _alias_text(alias: Any) -> str:
    kind = 'a type alias' if isinstance(alias, TypeAliasType) else 'a NewType'
    return (
        f'{alias.__name__} is {kind}; a spec names a role by a class, such as a '
        'group, and a binding maps it to a key of its own'
    )


def _output_path_problems(annotation: Any) -> list[str]:
    """The problems of each ``Path`` that ``annotation``, of an output, is made of."""
    found = _found(annotation, _is_path)
    return [
        f'{where}a Path is in the data of an output; no output holds a file or a '
        'Path, so that every file comes from the file source, which checks it'
        for where in dict.fromkeys(where for where, _ in found)
    ]


_UNMARKED_PATH = (
    'a Path without File(...); a parameter or a field that takes a file is a '
    "file field, such as mask: MaskFile with MaskFile = Annotated[Path, File("
    "kind='mask')], written as file(...)"
)


def _is_path(tp: Any) -> bool:
    return isinstance(tp, type) and issubclass(tp, PurePath)


def _has_alias(field: FieldInfo) -> bool:
    return any(
        getattr(field, a) is not None
        for a in ('alias', 'validation_alias', 'serialization_alias')
    )


def _group_problems(
    group: type[BaseModel], stack: tuple[type[BaseModel], ...]
) -> list[str]:
    """The problems of a group that a request writes, inside the groups ``stack``."""
    if group in stack:
        return [
            f'{group.__name__} contains itself; a group holds no field of its own '
            'type, at any depth'
        ]
    problems = []
    if found := _decorators(group, _SERIALIZERS):
        problems.append(f'{group.__name__}: no serializers or computed fields, {found}')
    return [*problems, *_fields_problems(group, (*stack, group))]


def _referenced_problems(data: Any) -> list[str]:
    """The problems of the ``T`` of ``Referenced[T]``."""
    name = field_types.type_name(data)
    if not field_types.is_group(group := field_types.plain(data)):
        return [
            f'Referenced[{name}]: {name} is not a group; a reference fills a field '
            'with a group, so that a picker of records lists the outputs of a class '
            'that names a role: mark each group, Referenced[A] | Referenced[B] | None'
        ]
    problems = [
        *_alias_problems(group),
        *_output_path_problems(group),
    ]
    if field_types.holds_ref(group):
        problems.append(
            f'Referenced[{name}]: {name} holds Referenced[...]; a reference is '
            'never inside the data of an output'
        )
    return problems


def _default_problem(model: type[BaseModel], name: str) -> str | None:
    """
    Why the default of the field ``name`` of ``model`` is not a value that a
    request may write and that validating returns unchanged, or ``None``.
    """
    field = model.model_fields[name]
    default = field.get_default(call_default_factory=True)
    try:
        held, _ = validation.validate_request_value(
            model, name, default, _no_file, _no_path
        )
        if field.default_factory is not None:
            again, _ = validation.validate_request_value(
                model,
                name,
                field.get_default(call_default_factory=True),
                _no_file,
                _no_path,
            )
            if again != held:
                return (
                    f'the default_factory gives another value at each call, {held!r} '
                    f'then {again!r}; a default is a value'
                )
        if refs := [
            (loc, f'{ref} is a reference; a default is a value')
            for loc, ref in iter_refs({name: held})
        ]:
            raise ParamsError(refs)
        made = validation.validated(model, name, default)
    except ParamsError as error:
        return f'the default is not a value a request may write: {error}'
    if made != default:
        return f'the default {default!r} is not the value validating it gives, {made!r}'
    return None


def _no_file(name: str, marker: File) -> str:
    raise ValueError(_NO_FILE)


def _no_path(ref: FileRef) -> Path:
    raise AssertionError('no file of a default is resolved, so none has a path')


_NO_FILE = 'a default names no file; a file field has no default but None or []'


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
        elif field_types.holds_ref(field.rebuild_annotation()):
            problems.append(
                f'{where}: Referenced[...] marks a field of the params, not an output'
            )
        else:
            problems.extend(
                f'{where}: {problem}' for problem in _output_path_problems(tp)
            )
    return problems


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
        elif other := [m for m in field.metadata if not isinstance(m, MinLen | MaxLen)]:
            problems.append(
                f'{name}: a table carries only length constraints, not {other}; '
                'an accumulator takes a few rows at a time, so a check of one row '
                'belongs to the element type'
            )
    return problems


def _params_schema(spec: WorkflowSpec[Any, Any]) -> dict[str, Any]:
    """
    The JSON schema of the spec's request type, which rejects a field that a
    model lacks, as the workspace does.
    """
    model = request_types.request_model(spec.params)
    schema = model.model_json_schema()
    for name, prop in schema.get('properties', {}).items():
        # pydantic leaves out the title of a field that is a $ref, if it is
        # the one it would make of the name.
        prop.setdefault('title', model.model_fields[name].title)
    for group in [schema, *schema.get('$defs', {}).values()]:
        if 'properties' in group:
            group['additionalProperties'] = False
    return schema


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


def _interface(model: type[BaseModel], stack: tuple[type[BaseModel], ...]) -> Any:
    """
    The name, type, and requiredness of each field of ``model``, a group
    inside the groups ``stack``, as plain data for :attr:`WorkflowSpec.digest`.

    Raises
    ------
    ValueError
        If the type of a field cannot be described, naming the field.
    """
    if model in stack:
        # A group that contains itself, which only an output may hold: how far up.
        return {'recursive': len(stack) - stack.index(model)}
    described = {}
    for name, info in model.model_fields.items():
        try:
            tp = _type_interface(field_types.field_type(info), (*stack, model))
        except _Undescribed as error:
            raise ValueError(
                f'{model.__name__}.{name}: {error}; the digest describes a field '
                'by its type, so each type in it is a class, resolved'
            ) from None
        described[name] = {'type': tp, 'required': info.is_required()}
    return described


class _Undescribed(Exception):
    """A type that :func:`_type_interface` cannot describe."""


_SHORT_NAMED = (Path, *field_types.TIMES)
"""
Classes outside ``builtins`` and scipp described by their name alone, a name
that no other class of a spec has.
"""


def _type_interface(tp: Any, stack: tuple[type[BaseModel], ...]) -> Any:
    """The type ``tp`` as plain data, by the rules of :attr:`WorkflowSpec.digest`."""
    origin, args = get_origin(tp), get_args(tp)
    if origin is Annotated:
        if REFERENCED in tp.__metadata__:
            return {'referenced': _type_interface(args[0], stack)}
        if (marker := field_types.file_marker(args[0], *tp.__metadata__)) is not None:
            return {'file': marker.metadata}
        return _type_interface(args[0], stack)
    if origin in (Union, UnionType):
        members = (_type_interface(arg, stack) for arg in args)
        return {'union': sorted(members, key=_canonical)}
    # A Literal and an enum of the same values store the same JSON.
    if origin is Literal:
        values = [v.value if isinstance(v, Enum) else v for v in args]
        return {'values': sorted(values, key=_canonical)}
    if isinstance(tp, type) and issubclass(tp, Enum):
        return {'values': sorted((m.value for m in tp), key=_canonical)}
    if field_types.is_group(tp):
        group = {'group': _interface(tp, stack)}
        if (extra := tp.model_config.get('extra')) not in (None, 'ignore'):
            group['extra'] = extra
        return group
    if isinstance(tp, type) and issubclass(tp, RootModel):
        root = tp.model_fields['root']
        return {'root': _type_interface(field_types.field_type(root), stack)}
    if origin is not None:
        return {_class_name(origin): [_type_interface(a, stack) for a in args]}
    if tp is Ellipsis:
        return '...'
    if isinstance(tp, ForwardRef | str):
        raise _Undescribed(f'{tp!r} is not resolved; call model_rebuild() first')
    if not isinstance(tp, type):
        raise _Undescribed(f'{tp!r} is not a class')
    return _class_name(tp)


def _class_name(cls: type) -> str:
    """
    The name of ``cls`` in a digest: its name alone for a builtin, a time,
    ``Path``, or a scipp type, else its module and qualified name.
    """
    module = cls.__module__.partition('.')[0]
    if cls in _SHORT_NAMED or module in ('builtins', 'scipp'):
        return field_types.type_name(cls)
    return f'{cls.__module__}.{cls.__qualname__}'


def _canonical(data: Any) -> str:
    """The JSON form of ``data``, the same for equal data in any process."""
    return json.dumps(data, sort_keys=True, separators=(',', ':'))
