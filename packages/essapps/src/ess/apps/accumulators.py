# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Specs over lists: specs that combine a list of elements into one value.

The params of such a spec are a :class:`Lists` model: one list per field of an
element, position i of every list being element i. ``lists_of(Counts)`` makes
one from the model of an element. The outputs are any model; a sum outputs the
element's fields, a mean or a sum into a wider type does not.

An accumulator in a session can be opened on any spec over lists whose binding
makes element accumulators, as ``combine`` does.

These belong in ``ess.reduce.spec`` next to ``WorkflowSpec``; they live here
until that is proposed there.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Self

from pydantic import BaseModel, create_model, model_validator

from .bindings import AccumulatorBinding, ElementAccumulator, Function


class Lists(BaseModel):
    """Params of a spec over lists: position i of each list is element i."""

    @model_validator(mode='after')
    def _same_length(self) -> Self:
        lengths = {len(getattr(self, name)) for name in type(self).model_fields}
        if len(lengths) != 1 or lengths == {0}:
            raise ValueError(
                'every field needs the same number of elements, at least 1'
            )
        return self


def lists_of(element: type[BaseModel]) -> type[Lists]:
    """Params with one list per field of ``element``."""
    fields: dict[str, Any] = {
        name: (list[Annotated[info.annotation, *info.metadata]], ...)
        for name, info in element.model_fields.items()
    }
    return create_model(f'{element.__name__}Lists', __base__=Lists, **fields)


class _Fold:
    """Each field combined with ``operation``, in push order."""

    def __init__(self, operation: Callable[[Any, Any], Any]) -> None:
        self._operation = operation
        self._value: dict[str, Any] | None = None

    def push(self, element: Mapping[str, Any]) -> None:
        if self._value is None:
            self._value = dict(element)
        else:
            value = self._value
            self._value = {f: self._operation(value[f], v) for f, v in element.items()}

    @property
    def value(self) -> Mapping[str, Any]:
        if self._value is None:
            raise ValueError('nothing has been pushed')
        return self._value


@dataclass(frozen=True)
class _Combine:
    operation: Callable[[Any, Any], Any]

    def stage(self, fixed: Mapping[str, Any], blanks: Sequence[str]) -> Function:
        return functools.partial(self._compute, **fixed)

    def _compute(self, **lists: list[Any]) -> Mapping[str, Any]:
        fold = self.accumulator()
        for values in zip(*lists.values(), strict=True):
            fold.push(dict(zip(lists, values, strict=True)))
        return fold.value

    def accumulator(self) -> ElementAccumulator:
        return _Fold(self.operation)


def combine(operation: Callable[[Any, Any], Any]) -> AccumulatorBinding:
    """
    The binding of a spec over lists that outputs the element's fields: each
    field combined with ``operation``, as in ``operation(operation(a, b), c)``.

    A plain request and an accumulator in a session combine the elements in
    the same order, so they give the same value. ``operation`` must return a
    new value and not modify its arguments.
    """
    return _Combine(operation)
