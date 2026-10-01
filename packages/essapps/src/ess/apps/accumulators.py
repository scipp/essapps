# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Accumulator specs: specs that combine a list of elements into one value.

An accumulator spec declares an element model whose fields are data fields.
Its params model has one list per element field, and its outputs model is the
element model, so a combined value can be pushed again.

These belong in ``ess.reduce.spec`` next to ``WorkflowSpec``; they live here
until that is proposed there.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Self

from ess.reduce.spec import WorkflowSpec
from pydantic import BaseModel, create_model, model_validator

from .bindings import AccumulatorBinding, ElementAccumulator, Function


class _Lists(BaseModel):
    """Params of an accumulator spec: position i of each list is one element."""

    @model_validator(mode='after')
    def _same_length(self) -> Self:
        lengths = {len(getattr(self, name)) for name in type(self).model_fields}
        if len(lengths) != 1 or lengths == {0}:
            raise ValueError(
                'every field needs the same number of elements, at least 1'
            )
        return self


def _lists(element: type[BaseModel]) -> type[BaseModel]:
    fields: dict[str, Any] = {
        name: (list[Annotated[info.annotation, *info.metadata]], ...)
        for name, info in element.model_fields.items()
    }
    return create_model(f'{element.__name__}Lists', __base__=_Lists, **fields)


class AccumulatorSpec(WorkflowSpec, frozen=True):
    """A spec whose params are one list per field of ``element``, combined into one."""

    element: type[BaseModel]

    @model_validator(mode='before')
    @classmethod
    def _derive(cls, data: dict[str, Any]) -> dict[str, Any]:
        element = data['element']
        return {
            'title': data['name'],
            'description': f'combines lists of {element.__name__}',
            'params': _lists(element),
            'outputs': element,
            **data,
        }


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
    The binding of an accumulator spec: each field combined with ``operation``.

    A plain request and an accumulator in a session combine the elements in
    the same order, so they give the same value. ``operation`` must return a
    new value and not modify its arguments.
    """
    return _Combine(operation)
