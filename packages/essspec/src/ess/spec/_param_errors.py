# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""Translation of pydantic errors into positions in a value."""

from __future__ import annotations

from types import UnionType
from typing import Annotated, Any, Final, Union, get_args, get_origin

from pydantic_core import ErrorDetails

from . import field_types
from .refs import FileRef, Loc, Ref, as_ref, positions


def request_type_errors(
    loc: Loc, tp: Any, value: Any, errors: list[ErrorDetails]
) -> list[tuple[Loc, str]]:
    """
    The ``errors`` of validating ``value``, of the request type ``tp``,
    given at ``loc``, each at its position in ``value`` (see
    :func:`value_position`), once. The errors at a reference or
    inside it are one error at the reference, since the field does not take
    its source, unless each is an error of a file that the field takes: of
    the file source, or of a validator. At a list, the reference is one to a
    whole list.
    """
    found = [(value_position(tp, e['loc'][1:]), e['msg'], e['type']) for e in errors]
    for at, item in positions((), value):
        inside = {kind for p, _, kind in found if p[: len(at)] == at}
        if (ref := as_ref(item)) is None or inside <= _TAKEN:
            continue
        found = [f for f in found if f[0][: len(at)] != at]
        why = _whole_list(ref) if 'list_type' in inside else _wrong_source(ref)
        found.append((at, why, ''))
    return list(dict.fromkeys(((*loc, *at), msg) for at, msg, _ in found))


def params_model_errors(
    loc: Loc, tp: Any, errors: list[ErrorDetails]
) -> list[tuple[Loc, str]]:
    """
    The ``errors`` of validating a value of the params type ``tp`` given at
    ``loc``, each at its position (see :func:`value_position`), without an
    error that a value is not of a member's source where another error, at the
    same position or inside, explains the value.
    """
    found = [(value_position(tp, e['loc'][1:]), e) for e in errors]
    return [
        ((*loc, *at), e['msg'])
        for at, e in found
        if e['type'] not in _NOT_ITS_SOURCE or not _inside(at, found)
    ]


_TAKEN: Final = frozenset({'file', 'value_error', 'assertion_error'})


def _wrong_source(ref: Ref) -> str:
    if isinstance(ref, FileRef):
        return f'file {ref}: the field is not a file field'
    return f'{ref} is a reference to an output; the field is not typed Referenced[...]'


def _whole_list(ref: Any) -> str:
    return (
        f'{ref} is a reference to a whole list; a reference stands for one '
        'element of a list'
    )


_NOT_ITS_SOURCE: Final = frozenset({'output_ref', 'file_ref'})
"""The errors of a member of a union that the value is not of its source."""


def _inside(at: Loc, found: list[tuple[Loc, ErrorDetails]]) -> bool:
    """
    Whether an error of ``found``, but one of :data:`_NOT_ITS_SOURCE`, is at
    ``at`` or inside.
    """
    return any(
        p[: len(at)] == at and e['type'] not in _NOT_ITS_SOURCE for p, e in found
    )


def value_position(tp: Any, loc: Loc) -> Loc:
    """
    The position in a value of type ``tp`` that ``loc``, the location of a
    pydantic error inside the value, names. ``tp`` is a request type (see
    :mod:`ess.spec.request_types`), or a type of the params model. Of
    ``loc``, each part that steps into the value is kept: a field of a group
    or an index of a list, and the field that an error of a missing field
    names. A part that names a member of a union is not: the tag of a tagged
    union, of a source or of a group, and the label of a member of a union
    with a member ``Referenced[...]``, which only the params model has::

        value_position(Centre, ('find', 'tolerance'))  # ('tolerance',)

    Read along the type, a field named like a tag is a field. After the
    label of a member of a union with a member ``Referenced[...]``, the
    position is read along the member written inline, whose errors are the
    ones that remain (see :func:`ess.spec.validation.validated`).
    """
    at: list[str | int] = []
    for part in loc:
        tp, _, metadata = field_types.unwrapped(tp)
        if (members := field_types.tagged_members(tp, metadata)) is not None:
            tp = members.get(part, Any)
        elif get_origin(tp) in (Union, UnionType):
            inline, inline_metadata, _ = field_types.split(tp)
            tp = Annotated[inline, *inline_metadata] if inline_metadata else inline
        else:
            at.append(part)
            if field_types.is_group(tp) and part in tp.model_fields:
                tp = field_types.field_type(tp.model_fields[part])
            elif get_origin(tp) is list:
                tp = get_args(tp)[0]
    return tuple(at)
