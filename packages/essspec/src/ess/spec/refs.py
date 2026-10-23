# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
References: how a request names data that exist elsewhere.

A request gives the value at a place by reference to data that exist
elsewhere, or inline: an output of an earlier record, a dataset the
framework did not compute, or, in a request not yet accepted, an output of an
accumulator that the framework holds. A *place* is the value of a parameter,
an element of a list, or a field of a group, at any depth (see
:mod:`ess.spec.places`). A reference stands for a group or a file only,
never for a whole list.
A reference is what a request names and what a record keeps; turning it into
the data a workflow takes, a local path or an object, is the business of
whatever runs the workflow.

The JSON form of a reference is an object with the key ``"@ref"``, which
names its kind. The key is reserved: an object with it is a reference, at any
depth, so a reference is found in plain data without a spec::

    {"@ref": "dataset", "dataset": "run:loki/60339"}
    {"@ref": "output", "record": "r17", "output": "centre"}
    {"@ref": "output", "record": "r18", "output": "slices", "key": "2"}
    {"@ref": "accumulator", "accumulator": "a3", "output": "iofq"}
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.config import JsonDict


def _kind_required(schema: JsonDict) -> None:
    """
    Require ``"@ref"`` in a reference's JSON schema: without it, an object is
    an inline value, not a reference.
    """
    required = schema.get('required', [])
    schema['required'] = ['@ref', *(required if isinstance(required, list) else [])]


class _Ref(BaseModel, frozen=True):
    """
    A reference. Its field ``kind`` is the key ``"@ref"`` of its JSON form,
    in every dump, the log's among them, and its JSON schema requires it.
    Each class has one kind, so a reference made in Python does not give it.
    A field the class lacks is refused.
    """

    model_config = ConfigDict(
        serialize_by_alias=True, extra='forbid', json_schema_extra=_kind_required
    )


class OutputRef(_Ref, frozen=True):
    """
    Output ``output`` of record ``record``, or one element ``key`` of it, if
    the output is a list or a dict.
    """

    kind: Literal['output'] = Field(default='output', alias='@ref', repr=False)
    record: str = Field(min_length=1)
    output: str = Field(min_length=1)
    key: str | None = None

    def __str__(self) -> str:
        key = f'[{self.key}]' if self.key is not None else ''
        return f'{self.record}.{self.output}{key}'


class DatasetRef(_Ref, frozen=True):
    """
    Data the framework did not compute, named by an identity the framework owns.

    The identity string is opaque to the spec and interpreted only by the
    framework that minted it. A framework with several kinds of identity, a
    catalogue PID, an instrument and run number, a path, makes the string
    self-describing, ``pid:...`` or ``run:...``, and normalizes it before it
    enters a record, because two references are equal only when their strings
    are. A place whose type is a file takes a reference to a dataset, and no
    other place does (see :func:`ess.spec.field_types.is_file`).
    """

    kind: Literal['dataset'] = Field(default='dataset', alias='@ref', repr=False)
    dataset: str = Field(min_length=1)

    def __str__(self) -> str:
        return self.dataset


class AccumulatorRef(_Ref, frozen=True):
    """
    Output ``output`` of accumulator ``accumulator``, in a request.

    The framework replaces it when it accepts the request, by the same output of
    a record of the accumulator's state at that moment, so a record never holds
    one.
    """

    kind: Literal['accumulator'] = Field(
        default='accumulator', alias='@ref', repr=False
    )
    accumulator: str = Field(min_length=1)
    output: str = Field(min_length=1)

    def __str__(self) -> str:
        return f'{self.accumulator}.{self.output}'


Ref = OutputRef | DatasetRef | AccumulatorRef
"""
A reference: the value at a place of a request.

An ``AccumulatorRef`` is only a placeholder in a request before it is
submitted; no record holds one.
"""


_KINDS: dict[str, type[Ref]] = {
    'output': OutputRef,
    'dataset': DatasetRef,
    'accumulator': AccumulatorRef,
}


def as_ref(value: Any) -> Ref | None:
    """
    The reference a plain value denotes, if it is one: a dict with the key
    ``"@ref"``. Raises ``ValueError`` if that names no kind of reference, or
    the other fields do not fit the kind.
    """
    if isinstance(value, Ref):
        return value
    if not isinstance(value, dict) or '@ref' not in value:
        return None
    kind = value['@ref']
    model = _KINDS.get(kind) if isinstance(kind, str) else None
    if model is None:
        raise ValueError(
            f'{value!r}: {kind!r} is not a kind of reference, one of {list(_KINDS)}'
        )
    try:
        return model.model_validate(value)
    except ValidationError as error:
        problems = '; '.join(
            f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in error.errors()
        )
        raise ValueError(f'{value!r}: not a reference to {kind}: {problems}') from None


type Loc = tuple[str | int, ...]
"""
A place in the params of a request, as pydantic's errors give one: the
parameter's name, then the index of an element of a list or the name of a
field of a group, at any depth, such as ``('runs', 2, 'transmission')``.
"""


def loc_text(loc: Loc) -> str:
    """A place as a message names it, such as ``runs[2].transmission``."""
    return ''.join(
        f'[{part}]' if isinstance(part, int) else f'.{part}' if i else part
        for i, part in enumerate(loc)
    )


class ParamsError(ValueError):
    """
    Params that are refused, with each reason at its place: ``errors`` lists
    ``(loc, msg)`` pairs, as pydantic's errors give them, and the message
    joins them, such as ``runs[1]: ...; sample: a required parameter, not
    given``.
    """

    def __init__(self, errors: Iterable[tuple[Loc, str]]) -> None:
        self.errors = tuple(errors)
        super().__init__(self.errors)

    def __str__(self) -> str:
        return '; '.join(f'{loc_text(loc)}: {msg}' for loc, msg in self.errors)


def ref_at(loc: Loc, value: Any) -> Ref | None:
    """:func:`as_ref` of ``value``; its error is a ``ParamsError`` at ``loc``."""
    try:
        return as_ref(value)
    except ValueError as error:
        raise ParamsError([(loc, str(error))]) from None


def positions(loc: Loc, value: Any) -> Iterator[tuple[Loc, Any]]:
    """
    Each position in ``value``, at ``loc``, with what it holds: the value,
    and each element of a list and each field of an object that is not a
    reference, at any depth, such as ``('runs', 2, 'transmission')``.

    These are the places where a request may hold a reference, and the
    positions inside an inline value, such as the elements of a tuple, where
    it may not. Only the spec tells them apart; ``"@ref"`` is reserved at
    each of them, so the references are found without it.
    """
    yield loc, value
    if isinstance(value, list):
        for i, item in enumerate(value):
            yield from positions((*loc, i), item)
    elif _is_object(value):
        for field, item in value.items():
            yield from positions((*loc, field), item)


def _is_object(value: Any) -> bool:
    """Whether ``value`` is an object in its JSON form, but not a reference."""
    return isinstance(value, Mapping) and '@ref' not in value


def iter_refs(params: Mapping[str, Any]) -> Iterator[tuple[Loc, Ref]]:
    """
    Each reference in the params of a request, with its place, such as
    ``('sample', 'run')``, ``('runs', 2)``, or ``('runs', 2, 'transmission')``.

    The params are by name. A reference is a :data:`Ref` or its plain (JSON)
    form, and is looked for at every position (see :func:`positions`). Any
    other value is skipped; a malformed reference raises
    :class:`ParamsError` at its place (see :func:`as_ref`).
    """
    for name, value in params.items():
        for loc, item in positions((name,), value):
            if (ref := ref_at(loc, item)) is not None:
                yield loc, ref


def map_refs(params: Mapping[str, Any], fn: Callable[[Ref], Any]) -> dict[str, Any]:
    """
    The params of a request, by name, with each reference replaced by
    ``fn(ref)``; the params are as :func:`iter_refs` takes them, and any other
    value is kept. A malformed reference raises :class:`ParamsError` at its
    place.
    """
    return {name: _mapped((name,), value, fn) for name, value in params.items()}


def _mapped(loc: Loc, value: Any, fn: Callable[[Ref], Any]) -> Any:
    """
    ``value`` at ``loc`` with each reference replaced by ``fn(ref)``; a
    function of the module, since a nested one that calls itself would hold
    ``fn``, and the data it reads, in a reference cycle.
    """
    if (ref := ref_at(loc, value)) is not None:
        return fn(ref)
    if isinstance(value, list):
        return [_mapped((*loc, i), v, fn) for i, v in enumerate(value)]
    if _is_object(value):
        return {f: _mapped((*loc, f), v, fn) for f, v in value.items()}
    return value
