# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
References: how a request names data that exist elsewhere.

A request gives each input by reference, never by value: an output of an
earlier record, a dataset the framework did not compute, or, in a request
not yet accepted, an output of an accumulator that the framework holds.
A reference is what a request names and what a record keeps; turning it into
the data a workflow takes, a local path or an object, is the business of
whatever runs the workflow.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from typing import Any

from pydantic import BaseModel, Field


class OutputRef(BaseModel, frozen=True):
    """
    Output ``output`` of record ``record``, or one element ``key`` of it, if
    the output is a list or a dict.
    """

    record: str = Field(min_length=1)
    output: str = Field(min_length=1)
    key: str | None = None

    def __str__(self) -> str:
        key = f'[{self.key}]' if self.key is not None else ''
        return f'{self.record}.{self.output}{key}'


class DatasetRef(BaseModel, frozen=True):
    """
    Data the framework did not compute, named by an identity the framework owns.

    The identity string is opaque to the spec and interpreted only by the
    framework that minted it. A framework with several kinds of identity, a
    catalogue PID, an instrument and run number, a path, makes the string
    self-describing, ``pid:...`` or ``run:...``, and normalizes it before it
    enters a record, because two references are equal only when their strings
    are. A dataset reference fits a file field (see
    :func:`ess.spec.field_types.fits`).
    """

    dataset: str = Field(min_length=1)

    def __str__(self) -> str:
        return self.dataset


class AccumulatorRef(BaseModel, frozen=True):
    """
    Output ``output`` of accumulator ``accumulator``, in a request.

    The framework replaces it when it accepts the request, by the same output of
    a record of the accumulator's state at that moment, so a record never holds
    one.
    """

    accumulator: str = Field(min_length=1)
    output: str = Field(min_length=1)

    def __str__(self) -> str:
        return f'{self.accumulator}.{self.output}'


Ref = OutputRef | DatasetRef | AccumulatorRef
"""
A reference: the value of an input in a request.

An ``AccumulatorRef`` is only a placeholder in a request before it is
submitted; no record holds one.
"""


_OUTPUT_REF_KEYS = frozenset(OutputRef.model_fields)
_ACCUMULATOR_REF_KEYS = frozenset(AccumulatorRef.model_fields)


def as_ref(value: Any) -> Ref | None:
    """The reference a plain value denotes, if it is one."""
    if isinstance(value, Ref):
        return value
    if isinstance(value, dict):
        keys = set(value)
        if {'record', 'output'} <= keys <= _OUTPUT_REF_KEYS:
            return OutputRef.model_validate(value)
        if keys == {'dataset'}:
            return DatasetRef.model_validate(value)
        if keys == _ACCUMULATOR_REF_KEYS:
            return AccumulatorRef.model_validate(value)
    return None


def iter_refs(inputs: Mapping[str, Any]) -> Iterator[tuple[str, Ref]]:
    """
    Each reference in the inputs of a request, with its place, such as
    ``sample`` or ``runs[2]``.

    The inputs are by name, each a reference, ``None``, or a list of these; a
    reference is a :data:`Ref` or its plain (JSON) form. Any other value is
    skipped, for the validation of the inputs to refuse.
    """
    for name, value in inputs.items():
        if isinstance(value, list):
            for i, item in enumerate(value):
                if (ref := as_ref(item)) is not None:
                    yield f'{name}[{i}]', ref
        elif (ref := as_ref(value)) is not None:
            yield name, ref


def map_refs(inputs: Mapping[str, Any], fn: Callable[[Ref], Any]) -> dict[str, Any]:
    """
    The inputs of a request, by name, with each reference replaced by
    ``fn(ref)``; the inputs are as :func:`iter_refs` takes them, and any other
    value is kept.
    """

    def one(value: Any) -> Any:
        return value if (ref := as_ref(value)) is None else fn(ref)

    return {
        name: [one(v) for v in value] if isinstance(value, list) else one(value)
        for name, value in inputs.items()
    }
