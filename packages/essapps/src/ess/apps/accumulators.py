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
from collections.abc import Callable, Mapping
from typing import Annotated, Any, Self

from ess.reduce.spec import WorkflowSpec
from pydantic import BaseModel, create_model, model_validator


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


def combine(operation: Callable[[Any, Any], Any]) -> Callable[..., Mapping[str, Any]]:
    """The workflow of an accumulator spec: each field combined with ``operation``."""

    def workflow(**lists: list[Any]) -> dict[str, Any]:
        return {f: functools.reduce(operation, values) for f, values in lists.items()}

    return workflow


class GenericAccumulator:
    """An operation, such as a sum, that makes an accumulator spec for any element."""

    def __init__(self, name: str, version: int = 1) -> None:
        self.name = name
        self.version = version

    @functools.cache  # noqa: B019 - one spec per element; generic accumulators live forever
    def of(self, element: type[BaseModel]) -> AccumulatorSpec:
        """The accumulator spec for ``element``; its name names the element model."""
        return AccumulatorSpec(
            name=f'{self.name}[{element.__name__}]',
            version=self.version,
            element=element,
        )


SUM = GenericAccumulator('sum')
