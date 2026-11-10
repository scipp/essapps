# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Validation of the parameters of a request, in the request phase and the call phase.

The backend validates each parameter of a request with
:func:`validate_request_value`, which it gives a resolver of files,
:data:`~ess.spec.request_types.FileResolver`, and the local path of a file,
:data:`LocalPath`. It returns what the record holds, the JSON form of the
request type, and each reference to an output with the type its data must
fit, :data:`OutputReads`, which the backend checks against history. The call
phase validates the params model itself from the record (see
:func:`validated`).

A request is validated one parameter at a time, in two phases, and a spec
checks its defaults the same way:

=============  =================================  ==========================
When           Function                           What it does
=============  =================================  ==========================
spec made      :func:`validated`                  validates the default of
                                                  a field as a binding
                                                  takes it
request phase  :func:`validate_request_value`     validates a parameter by
                                                  its request type, and
                                                  returns what the record
                                                  stores: the JSON form of
                                                  each group and file, and
                                                  each reference to an
                                                  output as it is; validates
                                                  a parameter that holds a
                                                  file field and no
                                                  ``Referenced[...]`` by the
                                                  params model too, with
                                                  the context ``paths``
call phase     :func:`validated`, with the        validates a parameter by
               context ``read`` and ``paths``     the params model from
                                                  what the record stores:
                                                  each reference to an
                                                  output replaced by its
                                                  data, each file with its
                                                  local path
=============  =================================  ==========================

The *request phase* is when a request is submitted, before any data is
read. The *call phase* is when the request runs, once the backend has read
the data of each reference to an output. :func:`validated` validates by the
params model, not by the request type; the request types are in
:mod:`ess.spec.request_types`.

An error is a :class:`~ess.spec.refs.ParamsError` that names the position of
each problem in the params, such as ``reduction.bins`` or ``runs[1]``.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path, PurePath
from typing import Any

from pydantic import BaseModel, Field, TypeAdapter, ValidationError, create_model
from pydantic_core import (
    PydanticCustomError,
    PydanticSerializationError,
    from_json,
    to_json,
)

from . import field_types, request_types
from ._param_errors import params_model_errors, request_type_errors
from .parameters import DataRefused
from .refs import FileRef, Loc, OutputRef, ParamsError, Ref, iter_refs

type LocalPath = Callable[[FileRef], Path]
"""
The local path of a file, by its identity, as the file source gives it. It
looks the path up and never raises: a file that cannot be read fails the
workflow that reads it.
"""


type OutputReads = list[tuple[Loc, OutputRef, Any]]
"""
The references to outputs in a value, each with its position and the type
that the output's data must fit: the ``T`` of ``Referenced[T]``, or the
union of these.
"""


def validate_request_value(
    model: type[BaseModel],
    name: str,
    value: Any,
    files: request_types.FileResolver,
    local_path: LocalPath,
    table: bool = False,
) -> tuple[Any, OutputReads]:
    """
    Validate a parameter in the request phase.

    ``value`` is validated from its JSON form by the request type of the
    field, with each file resolved by ``files``. A field that holds a file
    field and no ``Referenced[...]`` is then validated whole by the params
    model, with the local path of each file (see :func:`validated`), so that
    the validators of a group that holds a file, which its request type
    lacks (see :mod:`ess.spec.request_types`), see the path; the record still
    holds the group as the request wrote it. A field that holds
    ``Referenced[...]`` is validated so only in the call phase.

    Parameters
    ----------
    model:
        The params model.
    name:
        The parameter.
    value:
        What the request gives: plain data, or Python objects such as an
        instance of a group or a :data:`~ess.spec.refs.Ref`.
    files:
        Resolves each file, given the marker of its field.
    local_path:
        The local path of a resolved file.
    table:
        Whether the parameter is a table, validated without the length
        constraints of the list (see :func:`_single_field_params_model`).

    Returns
    -------
    :
        The value as the record stores it, the JSON form of the validated
        value (see :func:`_validate_round_trip`), and each reference to an output in it
        with the type its data must fit, read from the request type, so that
        the backend checks it against history without validating again.

    Raises
    ------
    ess.spec.refs.ParamsError
        At the position of each problem (see
        :func:`ess.spec._param_errors.request_type_errors`). Besides what
        the request type and the params model refuse, a file or a reference
        to an output in the JSON form that the request did not give, such as
        one that a serializer makes.
    """
    resolved: set[str] = set()
    context = {'files': files, 'resolved': resolved}
    given = _paths_as_files(_ANY.dump_python(value))
    written = {loc for loc, _ in iter_refs({name: given})}
    one = request_types.single_field_request_model(model, name, table)
    made, stored = _validate_located(one, name, given, context)
    refs = list(iter_refs({name: stored}))
    if problems := [
        (loc, problem)
        for loc, ref in refs
        if (problem := _stored_ref_problem(ref, loc in written, resolved))
    ]:
        raise ParamsError(problems)
    annotation = model.model_fields[name].rebuild_annotation()
    if field_types.holds_file(annotation) and not field_types.holds_ref(annotation):
        paths = local_paths({name: stored}, local_path)
        validated(model, name, stored, table, {'paths': paths})
    info = one.model_fields[name]
    reads = [
        (loc, ref, request_types.output_data_type(info, made, loc[1:]))
        for loc, ref in refs
        if isinstance(ref, OutputRef)
    ]
    return stored, reads


def _paths_as_files(value: Any) -> Any:
    """
    ``value``, plain data, with each ``Path`` in it the file named by that
    path, ``file(path=...)`` (see :meth:`~ess.spec.refs.FileRef.at`).
    """
    if isinstance(value, PurePath):
        return FileRef.at(value).model_dump(by_alias=True)
    if isinstance(value, dict):
        return {k: _paths_as_files(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_paths_as_files(v) for v in value]
    return value


def local_paths(values: Mapping[str, Any], local_path: LocalPath) -> dict[str, Path]:
    """
    The local path of each file in ``values``, values of parameters by name
    as the record stores them, by its identity: the context ``paths`` of
    :func:`validated`.
    """
    return {
        ref.file: local_path(ref)
        for _, ref in iter_refs(values)
        if isinstance(ref, FileRef)
    }


def _stored_ref_problem(ref: Ref, written: bool, resolved: set[str]) -> str | None:
    """
    Why the record cannot store ``ref``, or ``None``. ``written`` says whether
    the request gave a reference at its position, and ``resolved`` holds the
    identity of each file that a file field resolved.
    """
    if isinstance(ref, FileRef):
        if ref.file in resolved:
            return None
        return (
            f'{ref}: the JSON form of a value holds a file that no file field resolved'
        )
    if written:
        return None
    return (
        f'{ref}: the JSON form of a value written inline holds a reference to an '
        'output that the request did not give'
    )


_ANY = TypeAdapter(Any)
"""
Dumps Python objects to plain data, keeping a number that is not finite, and
each ``Path``.
"""


def _validate_located(
    one: type[BaseModel], name: str, value: Any, context: dict[str, Any]
) -> tuple[Any, Any]:
    """
    ``value``, plain data of the field ``name``, validated by ``one``, the
    request type of that field alone, with ``context``, and its JSON form
    (see :func:`_validate_round_trip`). A value without a JSON form
    is validated as it is, since it may be one that the field does not take.
    """
    loc = (name,)
    tp = field_types.field_type(one.model_fields[name])
    try:
        return _validate_round_trip(one, name, value, context=context)
    except PydanticSerializationError as error:
        why = f'cannot be stored as JSON: {error}'
    except ValidationError as error:
        raise ParamsError(request_type_errors(loc, tp, value, error.errors())) from None
    except ParamsError as error:
        raise ParamsError(((*loc, *at), msg) for at, msg in error.errors) from None
    except ValueError as error:
        raise _refused(loc, str(error)) from None
    try:
        one.model_validate({name: value})
    except ValidationError as error:
        raise ParamsError(request_type_errors(loc, tp, value, error.errors())) from None
    raise _refused(loc, why)


def _refused(loc: Loc, msg: str) -> ParamsError:
    return ParamsError([(loc, msg)])


@functools.cache
def _single_field_params_model(
    model: type[BaseModel], name: str, table: bool = False
) -> type[BaseModel]:
    """
    A model of the field ``name`` of ``model`` alone, with its config and
    without the decorators of ``model``: it validates one value by its own
    field. The params model has no decorators; a check of one value belongs
    to the value's type.

    With ``table``, the field is a table, validated without its length
    constraints: an accumulator takes a table a few rows at a time, and the
    backend checks the number of rows of the whole table.
    """
    info = model.model_fields[name]
    field = (info.annotation, Field()) if table else (info.annotation, info)
    return create_model(model.__name__, __config__=model.model_config, **{name: field})


def _json_form(one: type[BaseModel], name: str, value: Any) -> Any:
    """
    The JSON form of ``value``, a valid value of the field ``name`` of
    ``one``, a model of that field alone, as its declared type dumps it to
    JSON and JSON reads back: an instance of a subclass of a model type
    without the subclass's fields, and a number that is not finite as
    ``None``.
    """
    made = one.model_construct(**{name: value})
    return from_json(made.model_dump_json(by_alias=True))[name]


def _validate_round_trip(
    one: type[BaseModel],
    name: str,
    value: Any,
    context: dict[str, Any] | None = None,
) -> tuple[Any, Any]:
    """
    ``value`` validated, and its JSON form, as the record stores it. ``value``
    is a value of the field ``name`` of ``one``, a model of that field alone,
    such as the request type of a parameter.

    ``value`` is plain data, such as :func:`validate_request_value` makes of
    Python objects. It is validated from its JSON form, with ``context``. A
    field that a model lacks is refused at any depth, since validating would
    drop it silently. The JSON form of the validated value must read back as
    a value with the same JSON form, validated without ``context``. Raises:

    - ``PydanticSerializationError`` if it has no JSON form;
    - ``ValidationError`` if it is not valid, or if its JSON form is not,
      such as of a number that is not finite, which JSON holds as ``null``;
    - :class:`~ess.spec.refs.ParamsError` at the position inside the value,
      without ``name``, whose JSON form reads back as another value, such as
      a field of a group whose validator changes a value it has validated
      already.
    """
    given = to_json({name: value}, inf_nan_mode='constants')
    made = getattr(
        one.model_validate_json(given, extra='forbid', context=context), name
    )
    stored = _json_form(one, name, made)
    try:
        again = getattr(one.model_validate_json(to_json({name: stored})), name)
    except ValidationError as error:
        (first, *_) = error.errors()
        raise ValidationError.from_exception_data(
            one.__name__,
            [{'type': _NO_JSON_FORM_ERROR, 'loc': first['loc'], 'input': None}],
        ) from None
    if (read := _json_form(one, name, again)) != stored:
        raise ParamsError([(_first_difference((), stored, read), _NO_JSON_FORM)])
    return made, stored


_NO_JSON_FORM = 'has no JSON form that reads back as the value'
_NO_JSON_FORM_ERROR = PydanticCustomError('no_json_form', _NO_JSON_FORM)


def _first_difference(loc: Loc, a: Any, b: Any) -> Loc:
    """
    The first position at which ``a`` and ``b``, two JSON forms at ``loc``,
    differ: a field of an object or an element of a list, at any depth.
    """
    if isinstance(a, dict) and isinstance(b, dict) and a.keys() == b.keys():
        pairs: Iterable[tuple[str | int, Any, Any]] = ((k, a[k], b[k]) for k in a)
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        pairs = ((i, x, y) for i, (x, y) in enumerate(zip(a, b, strict=True)))
    else:
        return loc
    for at, x, y in pairs:
        if x != y:
            return _first_difference((*loc, at), x, y)
    return loc


def validated(
    model: type[BaseModel],
    name: str,
    value: Any,
    table: bool = False,
    context: dict[str, Any] | None = None,
) -> Any:
    """
    Validate a parameter by the params model, as the binding takes it.

    pydantic tries the members of a union with a member ``Referenced[...]``
    in order, each with the stored value, so a member written inline is
    given a reference too and refuses it. An error that a value is not a
    reference or not a file is dropped where another error, at the same
    position or inside, explains the value. Data read for a reference that
    fail a check of ``T`` are one error, at the reference.

    Parameters
    ----------
    model:
        The params model.
    name:
        The parameter.
    value:
        Without ``context``, data as in direct use: each field typed
        ``Referenced[T]`` takes ``T``, each file field a ``Path``. A spec
        checks a default so, and :func:`ess.spec.testing.instance` makes a
        params instance so. With ``context``, what the record stores,
        validated from its JSON form; a field that a model lacks is refused.
    table:
        Whether the parameter is a table, validated without the length
        constraints of the list (see :func:`_single_field_params_model`).
    context:
        ``paths``, the local path of each file by its identity (see
        :func:`local_paths`), and, in the call phase, ``read``, which gives
        the data of each reference to an output (see
        :data:`~ess.spec.parameters.Referenced`).

    Returns
    -------
    :
        The validated value of the field.

    Raises
    ------
    ess.spec.refs.ParamsError
        At the position of each problem (see
        :func:`ess.spec._param_errors.value_position`).
    """
    one = _single_field_params_model(model, name, table)
    try:
        if context is None:
            made = one.model_validate({name: value})
        else:
            given = to_json({name: value})
            made = one.model_validate_json(given, context=context, extra='forbid')
    except ValidationError as error:
        tp = field_types.field_type(one.model_fields[name])
        raise ParamsError(params_model_errors((name,), tp, error.errors())) from None
    except DataRefused as refused:
        at = next(loc for loc, ref in iter_refs({name: value}) if ref == refused.ref)
        msg = '; '.join(dict.fromkeys(e['msg'] for e in refused.errors))
        raise ParamsError([(at, msg)]) from None
    return getattr(made, name)
