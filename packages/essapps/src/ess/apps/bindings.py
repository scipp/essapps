# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Scipp contributors (https://github.com/scipp)
"""
Bindings: the code behind a spec.

A binding is staged with the values that stay fixed and returns a callable
over the rest, the blanks. It may compute what depends only on the fixed
values when staged or on the first call, and hold it; a request outside a
stage is staged with no blanks and called once::

    binding.stage(values, ())()                      # a plain request
    call = binding.stage(fixed, ('bins',))           # a stage in a session
    call(bins=100)

A plain function is a binding that computes nothing ahead. How a binding
computes is invisible in the records: a call through a stage returns what the
plain request returns.

A binding of an accumulator spec may also make element accumulators, which an
accumulator in a session holds, like ``sciline.Accumulator`` for a whole
element::

    held = binding.accumulator()
    held.push({'numerator': n1, 'denominator': d1})
    held.value                       # {'numerator': ..., 'denominator': ...}

Its value after the elements are pushed in order is the output of the plain
request over them. Without one, every snapshot combines all elements.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

Function = Callable[..., Mapping[str, Any]]
"""Takes parameter values by field name, with data read, and returns the outputs."""


@runtime_checkable
class Binding(Protocol):
    def stage(self, fixed: Mapping[str, Any], blanks: Sequence[str]) -> Function:
        """
        A callable over ``blanks`` that computes the outputs with ``fixed``.

        Both this method and the callable may be called from several threads
        at once. Neither may modify the values it is given, and the callable
        may return the same object in several calls: records made through one
        stage share what does not depend on the blanks.
        """
        ...


class ElementAccumulator(Protocol):
    def push(self, element: Mapping[str, Any]) -> None:
        """Add an element: a value for each field of the element model."""
        ...

    @property
    def value(self) -> Mapping[str, Any]:
        """The combination of the elements pushed so far."""
        ...


@runtime_checkable
class AccumulatorBinding(Binding, Protocol):
    def accumulator(self) -> ElementAccumulator:
        """
        A new element accumulator with nothing pushed.

        Its ``value`` is read after every push, and a later push must not
        modify a value read before it.
        """
        ...


@dataclass(frozen=True)
class FunctionBinding:
    """A plain function as a binding: every call computes everything."""

    function: Function

    def stage(self, fixed: Mapping[str, Any], blanks: Sequence[str]) -> Function:
        return functools.partial(self.function, **fixed)


def as_binding(code: Binding | Function) -> Binding:
    """``code`` as a binding; a plain function is wrapped."""
    return code if isinstance(code, Binding) else FunctionBinding(code)
