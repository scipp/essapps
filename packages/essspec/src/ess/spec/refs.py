# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
The ``@ref`` key: how a request and a record name data that exist elsewhere.

A JSON object with the key ``"@ref"`` is a *reference*, at any depth of a
request's params. The value of ``"@ref"`` names its kind::

    {"@ref": "output", "record": "r17", "output": "centre"}
    {"@ref": "output", "record": "r18", "output": "slices", "key": "2"}
    {"@ref": "file", "file": "run:loki/60339"}

The first two are references to an output: of a record, and of one element
of a list or dict output of a record. The last is the JSON form of a file
(see :class:`~ess.spec.parameters.File`). Since the key marks a reference
wherever it is, code that reads a record lists its references and files
without the spec: :func:`iter_refs` finds each one, and :func:`map_refs`
replaces each one.

Where a request may write each kind is stated in
``packages/essspec/docs/params.md``. Turning a reference into the data a
workflow takes is the business of whatever runs the workflow.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import PurePath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.config import JsonDict


def _kind_required(schema: JsonDict) -> None:
    """
    Require ``"@ref"`` in a reference's JSON schema: an object without it is
    not a reference.
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

    The identity ``record`` is opaque to the spec. In a request, the framework
    accepts the ID of an accumulator in ``record``: it replaces it, when it
    accepts the request, by the record of the accumulator's state at that
    moment, so a record never names an accumulator.
    """

    kind: Literal['output'] = Field(default='output', alias='@ref', repr=False)
    record: str = Field(min_length=1)
    output: str = Field(min_length=1)
    key: str | None = None

    def __str__(self) -> str:
        key = f'[{self.key}]' if self.key is not None else ''
        return f'{self.record}.{self.output}{key}'


class FileRef(_Ref, frozen=True):
    """
    A file, named by an identity the framework owns.

    The identity string is opaque to the spec and interpreted only by the
    framework that minted it. A framework with several kinds of identity, a
    catalogue PID, an instrument and run number, a path, makes the string
    self-describing, ``pid:...`` or ``run:...``, and normalizes it before it
    enters a record, because two references are equal only when their strings
    are. It is the JSON form of a file, which fills a file field (see
    :class:`ess.spec.parameters.File`).
    """

    kind: Literal['file'] = Field(default='file', alias='@ref', repr=False)
    file: str = Field(min_length=1)

    def __str__(self) -> str:
        return self.file

    @classmethod
    def at(cls, path: PurePath | str) -> FileRef:
        """The file named by its local path, ``path:...``."""
        return cls(file=f'path:{path}')


Ref = OutputRef | FileRef
"""A reference, the Python form of an object with the key ``"@ref"``."""


_KINDS: dict[str, type[Ref]] = {'output': OutputRef, 'file': FileRef}


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
A position in the params of a request, as pydantic's errors give one: the
parameter's name, then the index of an element of a list or the name of a
field of a group, at any depth, such as ``('runs', 2, 'thickness')``.
"""


def loc_text(loc: Loc) -> str:
    """A position as a message names it, such as ``runs[2].thickness``."""
    return ''.join(
        f'[{part}]' if isinstance(part, int) else f'.{part}' if i else part
        for i, part in enumerate(loc)
    )


class ParamsError(ValueError):
    """
    Params that are refused, with each reason at its position: ``errors`` lists
    ``(loc, msg)`` pairs, as pydantic's errors give them, and the message
    joins them, such as ``runs[1]: ...; sample: a required parameter, not
    given``.
    """

    def __init__(self, errors: Iterable[tuple[Loc, str]]) -> None:
        self.errors = tuple(errors)
        super().__init__(self.errors)

    def __str__(self) -> str:
        return '; '.join(f'{loc_text(loc)}: {msg}' for loc, msg in self.errors)


def _ref_at(loc: Loc, value: Any) -> Ref | None:
    """:func:`as_ref` of ``value``; its error is a ``ParamsError`` at ``loc``."""
    try:
        return as_ref(value)
    except ValueError as error:
        raise ParamsError([(loc, str(error))]) from None


def positions(loc: Loc, value: Any) -> Iterator[tuple[Loc, Any]]:
    """
    Each position in ``value``, at ``loc``, with what is there: the value
    itself, and each element of a list and each field of an object that is
    not a reference, at any depth, such as ``('runs', 2)`` or
    ``('reduction', 'bins')``.
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
    Each reference in the params of a request, with its position, such as
    ``('sample',)`` or ``('runs', 2)``.

    The params are by name. A reference is a :data:`Ref` or its plain (JSON)
    form, and is looked for at every position (see :func:`positions`). Any
    other value is skipped; a malformed reference raises
    :class:`ParamsError` at its position (see :func:`as_ref`).
    """
    for name, value in params.items():
        for loc, item in positions((name,), value):
            if (ref := _ref_at(loc, item)) is not None:
                yield loc, ref


def map_refs(params: Mapping[str, Any], fn: Callable[[Ref], Any]) -> dict[str, Any]:
    """
    The params of a request, by name, with each reference replaced by
    ``fn(ref)``; the params are as :func:`iter_refs` takes them, and any other
    value is kept. A malformed reference raises :class:`ParamsError` at its
    position.
    """
    return {name: _mapped((name,), value, fn) for name, value in params.items()}


def _mapped(loc: Loc, value: Any, fn: Callable[[Ref], Any]) -> Any:
    """
    ``value`` at ``loc`` with each reference replaced by ``fn(ref)``; a
    function of the module, since a nested one that calls itself would hold
    ``fn``, and the data it reads, in a reference cycle.
    """
    if (ref := _ref_at(loc, value)) is not None:
        return fn(ref)
    if isinstance(value, list):
        return [_mapped((*loc, i), v, fn) for i, v in enumerate(value)]
    if _is_object(value):
        return {f: _mapped((*loc, f), v, fn) for f, v in value.items()}
    return value
