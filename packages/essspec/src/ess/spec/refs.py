# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
References: how a request names data that exist elsewhere.

A request gives a value, or a row of a list parameter, by reference to data
that exist elsewhere: an output of an earlier record, a dataset the framework
did not compute, or, in a request not yet accepted, an output of an
accumulator that the framework holds. A reference stands for a whole value
or a row, never for a whole list, and never for part of a value.
A reference is what a request names and what a record keeps; turning it into
the data a workflow takes, a local path or an object, is the business of
whatever runs the workflow.

The JSON form of a reference is an object with the key ``"@ref"``, which
names its kind, so a reference is found in plain data without a spec::

    {"@ref": "dataset", "dataset": "run:loki/60339"}
    {"@ref": "output", "record": "r17", "output": "centre"}
    {"@ref": "output", "record": "r18", "output": "slices", "key": "2"}
    {"@ref": "accumulator", "accumulator": "a3", "output": "iofq"}
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
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
    are. A file parameter takes a reference to a dataset, and no other
    parameter does (see :func:`ess.spec.field_types.is_file`).
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
A reference: a value, or a row of a list parameter, in a request.

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


def _ref_at(where: str, value: Any) -> Ref | None:
    """:func:`as_ref` of ``value``, whose error names its place ``where``."""
    try:
        return as_ref(value)
    except ValueError as error:
        raise ValueError(f'{where}: {error}') from None


def places(name: str, value: Any) -> list[tuple[str, Any]]:
    """
    The places in the value of the parameter ``name`` where a request may
    hold a reference, each with what it holds: the value, or each element of
    a list, such as ``runs[2]``; never deeper.
    """
    if isinstance(value, list):
        return [(f'{name}[{i}]', item) for i, item in enumerate(value)]
    return [(name, value)]


def iter_refs(params: Mapping[str, Any]) -> Iterator[tuple[str, Ref]]:
    """
    Each reference in the params of a request, with its place, such as
    ``sample`` or ``runs[2]``.

    The params are by name. A reference is a :data:`Ref` or its plain (JSON)
    form, and is looked for at the places :func:`places` names. Any other
    value is skipped; a malformed reference raises ``ValueError`` (see
    :func:`as_ref`).
    """
    for name, value in params.items():
        for where, item in places(name, value):
            if (ref := _ref_at(where, item)) is not None:
                yield where, ref


def map_refs(params: Mapping[str, Any], fn: Callable[[Ref], Any]) -> dict[str, Any]:
    """
    The params of a request, by name, with each reference replaced by
    ``fn(ref)``; the params are as :func:`iter_refs` takes them, and any other
    value is kept.
    """

    def one(name: str, value: Any) -> Any:
        mapped = [
            item if (ref := _ref_at(where, item)) is None else fn(ref)
            for where, item in places(name, value)
        ]
        return mapped if isinstance(value, list) else mapped[0]

    return {name: one(name, value) for name, value in params.items()}
