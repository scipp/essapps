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
from typing import Any

from ess.reduce.spec import WorkflowSpec
from pydantic import BaseModel, create_model


def accumulator_spec(
    name: str, version: int, element: type[BaseModel], *, description: str = ''
) -> WorkflowSpec:
    """A spec whose params are one list per field of ``element``, combined into one."""
    params = create_model(
        f'{element.__name__}Lists',
        **{f: (list[info.annotation], ...) for f, info in element.model_fields.items()},  # type: ignore[call-overload,name-defined]
    )
    return WorkflowSpec(
        name=name,
        version=version,
        title=name,
        description=description or f'combines lists of {element.__name__}',
        params=params,
        outputs=element,
    )


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
    def of(self, element: type[BaseModel]) -> WorkflowSpec:
        """The accumulator spec for ``element``; its name names the element model."""
        return accumulator_spec(
            f'{self.name}[{element.__name__}]', self.version, element
        )


SUM = GenericAccumulator('sum')
